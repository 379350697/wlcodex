from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess
import uuid
from collections.abc import Callable

from wlcodex.agent_backend import AgentRequest
from wlcodex.codex_runtime_source import CodexRuntimeSource
from wlcodex.context_packets import ContextBudget, approx_tokens, trim_to_budget
from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.runtime_renderer import RuntimeRunState
from wlcodex.models import ConversationSession, TaskStatus
from wlcodex.orchestration_progress_text import render_user_progress_text
from wlcodex.orchestrator import (
    ChiefEngineerOrchestrator,
    OrchestrationProgress,
    VerificationDecision,
    _detect_claude_direct_delivery_drift,
    _detect_verification_delivery_drift,
    _parse_last_complete_json,
)
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.task_service import TaskService

logger = logging.getLogger(__name__)


def _accepts_keyword(func: object, name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == name
        for parameter in signature.parameters.values()
    )


def _visible_analysis_reply(text: str) -> str:
    """Extract a human-readable reply from Codex analysis text.

    Handles concatenated JSON objects (Codex may emit multiple {..}{..}).
    Returns natural-language Chinese — never raw JSON keys.
    """
    stripped = text.strip()
    if not stripped:
        return ""

    # Try the last complete JSON object first (most likely the final conclusion).
    parsed = _parse_last_complete_json(stripped)
    if isinstance(parsed, dict):
        summary = str(parsed.get("summary", "")).strip()
        if summary:
            return summary
        # Fallback: look for any string field with content
        for key in ("message", "title", "conclusion", "result"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    # If the text starts with JSON, strip the JSON prefix and use the prose.
    if stripped.startswith("{"):
        # Try to find the end of the last JSON object
        depth = 0
        last_close = -1
        for i, ch in enumerate(stripped):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    last_close = i
        if last_close > 0:
            after_json = stripped[last_close + 1:].strip()
            if after_json and not after_json.startswith("{"):
                return after_json

    return stripped


class _TaskBoundCodexBackend:
    def __init__(
        self,
        backend: object,
        service: TaskService,
        task_id: int,
        *,
        store: object | None = None,
        correlation_id: str = "",
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
    ) -> None:
        self._backend = backend
        self._service = service
        self._task_id = task_id
        self._store = store
        self._correlation_id = correlation_id
        self._conversation_id = conversation_id
        self._orchestration_run_id = orchestration_run_id
        self._runtime_source: CodexRuntimeSource | None = None
        self._last_runtime_event_id: int | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._backend, name)

    def _bind_thread(self, thread_id: str) -> None:
        self._service.set_task_thread(self._task_id, thread_id)

    def set_runtime_context(self, *, agent_run_id: int, role: str) -> None:
        if self._store is None or not self._correlation_id:
            self._runtime_source = None
            return
        self._runtime_source = CodexRuntimeSource(
            correlation_id=self._correlation_id,
            agent_run_id=agent_run_id,
            conversation_id=self._conversation_id,
            orchestration_run_id=self._orchestration_run_id,
            task_id=self._task_id,
        )
        self._runtime_role = role

    async def send_codex_prompt(
        self,
        workspace_path: str,
        prompt: str,
        **kwargs: object,
    ) -> str:
        send_codex_prompt = getattr(self._backend, "send_codex_prompt")
        supported_kwargs = {
            key: value
            for key, value in kwargs.items()
            if _accepts_keyword(send_codex_prompt, key)
        }
        previous_callback = getattr(self._backend, "_runtime_event_callback", None)
        runtime_wired = False
        if self._runtime_source is not None and hasattr(
            self._backend, "set_runtime_event_callback"
        ):
            def _runtime_callback(event: object) -> None:
                if previous_callback is not None:
                    previous_callback(event)
                source = self._runtime_source
                if source is None or self._store is None:
                    return
                for runtime_event in source.map_event(
                    event, causation_id=self._last_runtime_event_id,
                ):
                    stored = self._store.append(runtime_event)
                    self._last_runtime_event_id = stored.id

            self._backend.set_runtime_event_callback(_runtime_callback)
            runtime_wired = True
        try:
            return await send_codex_prompt(
                workspace_path,
                prompt,
                on_thread_created=self._bind_thread,
                **supported_kwargs,
            )
        finally:
            if runtime_wired:
                self._backend.set_runtime_event_callback(previous_callback)


class _ClaudeRuntimeWrapper:
    """Wraps a Claude backend to emit runtime events before, during, and after
    ``send_streaming``.  Creates the agent_run row and emits
    ``agent.run.started`` BEFORE the first delta so Claude is observable
    from the moment it launches.
    """

    def __init__(
        self,
        backend: object,
        *,
        store: object,
        correlation_id: str,
        conversation_id: int,
        orchestration_run_id: int,
        task_id: int,
        ledger: object,
        prompt_packet_summary: str = "",
    ) -> None:
        self._backend = backend
        self._store = store
        self._correlation_id = correlation_id
        self._conversation_id = conversation_id
        self._orchestration_run_id = orchestration_run_id
        self._task_id = task_id
        self._ledger = ledger
        self._prompt_packet_summary = prompt_packet_summary
        self._agent_run_id: int | None = None
        self._events: list[RuntimeEvent] = []

    @property
    def enabled(self) -> bool:
        return getattr(self._backend, "enabled", False)

    @property
    def agent_run_id(self) -> int | None:
        return self._agent_run_id

    @property
    def activity_events(self) -> list[RuntimeEvent]:
        return list(self._events)

    def _configure_backend_runtime_source(self, agent_run_id: int) -> object | None:
        if self._store is None:
            return None
        try:
            from wlcodex.claude_runtime_source import ClaudeRuntimeSource
        except Exception:
            return None
        source = ClaudeRuntimeSource(
            self._store,
            correlation_id=self._correlation_id,
            agent_run_id=agent_run_id,
            conversation_id=self._conversation_id,
            orchestration_run_id=self._orchestration_run_id,
            task_id=self._task_id,
        )
        setter = getattr(self._backend, "set_runtime_source", None)
        if callable(setter):
            setter(source)
            return source
        if hasattr(self._backend, "_runtime_source"):
            setattr(self._backend, "_runtime_source", source)
            return source
        return None

    def _restore_backend_runtime_source(self, previous: object | None) -> None:
        setter = getattr(self._backend, "set_runtime_source", None)
        if callable(setter):
            setter(previous)
            return
        if hasattr(self._backend, "_runtime_source"):
            setattr(self._backend, "_runtime_source", previous)

    async def send_streaming(self, request: AgentRequest):
        """Create agent_run, emit started, then forward deltas with activity events."""
        # Create agent_run BEFORE launching Claude subprocess
        agent_run = self._ledger.create_agent_run(
            conversation_id=self._conversation_id,
            agent="claude",
            role="implementation",
            prompt_packet_summary=self._prompt_packet_summary[:120],
        )
        self._agent_run_id = agent_run.id
        self._ledger.update_agent_run_status(agent_run.id, "running")
        self._ledger.set_conversation_active_claude_run(
            self._conversation_id, agent_run.id
        )

        # Emit agent.run.started
        if self._store is not None:
            agg_id = str(agent_run.id)
            started_event = RuntimeEvent(
                schema_version=1,
                event_type=EventType.AGENT_RUN_STARTED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=agg_id,
                correlation_id=self._correlation_id,
                source=EventSource.ORCHESTRATOR,
                actor="claude",
                visibility=Visibility.OPERATOR,
                payload={
                    "agent": "claude",
                    "role": "implementation",
                    "prompt_summary": self._prompt_packet_summary[:200],
                },
                occurred_at=now_iso(),
                conversation_id=self._conversation_id,
                orchestration_run_id=self._orchestration_run_id,
                agent_run_id=agent_run.id,
                task_id=self._task_id,
            )
            self._store.append(started_event)
            self._events.append(started_event)

        previous_runtime_source = getattr(self._backend, "_runtime_source", None)
        runtime_source = self._configure_backend_runtime_source(agent_run.id)

        error_text = ""
        had_error = False
        backend_exception = False
        try:
            async for stream_event in self._backend.send_streaming(request):
                if stream_event.event_type == "error":
                    had_error = True
                    error_text = stream_event.delta
                if self._store is not None and agent_run:
                    activity = RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.AGENT_RUN_ACTIVITY,
                        aggregate_type=AggregateType.AGENT_RUN,
                        aggregate_id=agg_id,
                        correlation_id=self._correlation_id,
                        source=EventSource.CLAUDE,
                        actor="claude",
                        visibility=Visibility.INTERNAL,
                        payload={
                            "delta_preview": (stream_event.delta or "")[:200],
                            "event_type": stream_event.event_type,
                        },
                        occurred_at=now_iso(),
                        conversation_id=self._conversation_id,
                        orchestration_run_id=self._orchestration_run_id,
                        agent_run_id=agent_run.id,
                        task_id=self._task_id,
                    )
                    self._store.append(activity)
                    self._events.append(activity)
                yield stream_event
        except Exception as exc:
            had_error = True
            error_text = str(exc)
            backend_exception = True
        finally:
            if runtime_source is not None:
                self._restore_backend_runtime_source(previous_runtime_source)

        # Emit agent.run.completed or agent.run.failed
        if self._store is not None and agent_run:
            if had_error:
                fail_event = RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.AGENT_RUN_FAILED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=agg_id,
                    correlation_id=self._correlation_id,
                    source=EventSource.ORCHESTRATOR,
                    actor="claude",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "agent": "claude",
                        "role": "implementation",
                        "reason": error_text[:500] or "Claude streaming returned error",
                    },
                    occurred_at=now_iso(),
                    conversation_id=self._conversation_id,
                    orchestration_run_id=self._orchestration_run_id,
                    agent_run_id=agent_run.id,
                    task_id=self._task_id,
                )
                self._store.append(fail_event)
                self._events.append(fail_event)
            else:
                complete_event = RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.AGENT_RUN_COMPLETED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=agg_id,
                    correlation_id=self._correlation_id,
                    source=EventSource.ORCHESTRATOR,
                    actor="claude",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "agent": "claude",
                        "role": "implementation",
                    },
                    occurred_at=now_iso(),
                    conversation_id=self._conversation_id,
                    orchestration_run_id=self._orchestration_run_id,
                    agent_run_id=agent_run.id,
                    task_id=self._task_id,
                )
                self._store.append(complete_event)
                self._events.append(complete_event)

        # Re-raise backend exceptions so the orchestrator sees the failure.
        # stream_event error events are already handled by the orchestrator
        # and must not be re-raised (orchestrator already yielded FAILED).
        if backend_exception:
            if error_text:
                raise RuntimeError(error_text) from None
            raise RuntimeError("Claude streaming failed")


class OrchestrationRunner:
    """Owns long-running chief-engineer orchestration outside Telegram handlers."""

    def __init__(
        self,
        *,
        task_service: TaskService,
        codex_backend: object,
        claude_backend: object,
        ledger: object,
        interaction_renderer: object | None = None,
        orchestrator_factory: Callable[[object, object], object] | None = None,
        runtime_event_store: object | None = None,
    ) -> None:
        self._service = task_service
        self._codex = codex_backend
        self._claude = claude_backend
        self._ledger = ledger
        self._interaction_renderer = interaction_renderer
        self._orchestrator_factory = orchestrator_factory or ChiefEngineerOrchestrator
        self._store = runtime_event_store
        self._tasks: set[asyncio.Task[None]] = set()

    def start_chief_engineer(
        self,
        *,
        prompt: str,
        conversation: ConversationSession,
        task_id: int,
        orchestration_run_id: int,
        codex_analysis_run_id: int,
        chat_id: int,
        workspace_path: str,
        correlation_id: str = "",
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._run_chief_engineer(
                prompt=prompt,
                conversation=conversation,
                task_id=task_id,
                orchestration_run_id=orchestration_run_id,
                codex_analysis_run_id=codex_analysis_run_id,
                chat_id=chat_id,
                workspace_path=workspace_path,
                correlation_id=correlation_id or str(uuid.uuid4()),
            ),
            name=f"chief-engineer-{orchestration_run_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_unexpected_failure)
        return task

    def _log_unexpected_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Background orchestration crashed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _emit_event(self, event: RuntimeEvent) -> RuntimeEvent:
        if self._store is None:
            return event
        return self._store.append(event)

    def _build_runtime_state(
        self,
        phase: str,
        active_agent: str = "",
        agent_status: str = "",
        retry_count: int = 0,
        is_terminal: bool = False,
    ) -> RuntimeRunState:
        return RuntimeRunState(
            phase=phase,
            active_agent=active_agent,
            agent_status=agent_status,
            retry_count=retry_count,
            is_terminal=is_terminal,
        )

    def _uses_runtime_progress(self) -> bool:
        if self._interaction_renderer is not None and hasattr(
            self._interaction_renderer,
            "has_runtime_status_surface",
        ):
            return bool(self._interaction_renderer.has_runtime_status_surface())
        return bool(
            self._interaction_renderer is not None
            and getattr(self._interaction_renderer, "_runtime_progress", None)
        )

    def _record_workflow_overhead(
        self,
        *,
        phase: str,
        overhead_input_tokens: int,
        conversation_id: int,
        orchestration_run_id: int,
        task_id: int,
        agent_run_id: int | None = None,
        status: str = "completed",
    ) -> None:
        """Record a workflow overhead usage event. Never raises."""
        try:
            self._ledger.record_usage_event(
                agent="workflow",
                role="overhead",
                phase=phase,
                request_kind="overhead",
                source="estimated",
                workflow_overhead_input_tokens=overhead_input_tokens,
                status=status,
                conversation_id=conversation_id,
                orchestration_run_id=orchestration_run_id,
                agent_run_id=agent_run_id,
                task_id=task_id,
            )
        except Exception:
            pass

    async def _run_chief_engineer(
        self,
        *,
        prompt: str,
        conversation: ConversationSession,
        task_id: int,
        orchestration_run_id: int,
        codex_analysis_run_id: int,
        chat_id: int,
        workspace_path: str,
        correlation_id: str = "",
    ) -> None:
        cid = correlation_id or str(uuid.uuid4())
        codex = _TaskBoundCodexBackend(
            self._codex,
            self._service,
            task_id,
            store=self._store,
            correlation_id=cid,
            conversation_id=conversation.id,
            orchestration_run_id=orchestration_run_id,
        )
        codex.set_runtime_context(
            agent_run_id=codex_analysis_run_id,
            role="analysis",
        )

        # Wrap Claude backend so runtime events are emitted before/after
        # every send_streaming call and during every delta.
        claude_backend = _ClaudeRuntimeWrapper(
            self._claude,
            store=self._store,
            correlation_id=cid,
            conversation_id=conversation.id,
            orchestration_run_id=orchestration_run_id,
            task_id=task_id,
            ledger=self._ledger,
            prompt_packet_summary=prompt[:120],
        )
        orch = self._orchestrator_factory(codex, claude_backend)
        # Inject pending user context from mid-implementation/verification follow-ups.
        if self._store is not None:
            try:
                pending = self._store._conn.execute(
                    """
                    SELECT payload_json FROM runtime_events
                    WHERE conversation_id = ?
                      AND event_type = 'conversation.pending_context.recorded'
                    ORDER BY id ASC
                    """,
                    (conversation.id,),
                ).fetchall()
                if pending:
                    pending_texts = []
                    for row in pending:
                        payload = __import__("json").loads(str(row["payload_json"]))
                        preview = payload.get("text_preview", "")
                        if preview:
                            pending_texts.append(preview)
                    if pending_texts and hasattr(orch, "set_pending_user_context"):
                        orch.set_pending_user_context("; ".join(pending_texts))
            except Exception:
                logger.debug("Failed to query pending context for verification", exc_info=True)

        # Emit run.started
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.RUN_STARTED,
            aggregate_type=AggregateType.ORCHESTRATION_RUN,
            aggregate_id=str(orchestration_run_id),
            correlation_id=cid,
            source=EventSource.ORCHESTRATOR,
            actor="orchestrator",
            visibility=Visibility.OPERATOR,
            payload={"goal": prompt, "phase": "running_analysis"},
            occurred_at=now_iso(),
            conversation_id=conversation.id,
            orchestration_run_id=orchestration_run_id,
            task_id=task_id,
        ))

        terminal_sent = False
        orch_result_status = "running"
        verify_round = 0
        codex_analysis_text = ""
        claude_implementation_text = ""
        verification_text = ""
        terminal_text = ""
        codex_analysis_status = "running"
        implementation_notice_sent = False
        codex_verification_run_id: int | None = None
        last_verification_decision = ""

        try:
            async for progress in orch.run_streaming(
                prompt,
                conversation_context={"workspace": workspace_path},
            ):
                if terminal_sent:
                    continue

                if progress.phase == OrchestrationProgress.IMPL_DELTA:
                    if progress.text:
                        claude_implementation_text += progress.text
                        if not implementation_notice_sent:
                            implementation_notice_sent = True
                            # Emit Claude agent.run.started if wrapper hasn't already
                            # (wrapper does this for real orchestrations; here as fallback
                            # for fake orchestrators used in tests)
                            if claude_backend.agent_run_id is None:
                                claude_run = self._ledger.create_agent_run(
                                    conversation_id=conversation.id,
                                    agent="claude",
                                    role="implementation",
                                    prompt_packet_summary=prompt[:120],
                                )
                                self._ledger.update_agent_run_status(claude_run.id, "running")
                                self._ledger.set_conversation_active_claude_run(
                                    conversation.id, claude_run.id
                                )
                                # Track fallback agent_run_id so IMPL_COMPLETE doesn't create another
                                # (only needed when wrapper isn't used, e.g. fake orchestrators)
                                claude_backend._agent_run_id = claude_run.id
                                self._emit_event(RuntimeEvent(
                                    schema_version=1,
                                    event_type=EventType.AGENT_RUN_STARTED,
                                    aggregate_type=AggregateType.AGENT_RUN,
                                    aggregate_id=str(claude_run.id),
                                    correlation_id=cid,
                                    source=EventSource.ORCHESTRATOR,
                                    actor="claude",
                                    visibility=Visibility.OPERATOR,
                                    payload={"agent": "claude", "role": "implementation"},
                                    occurred_at=now_iso(),
                                    conversation_id=conversation.id,
                                    orchestration_run_id=orchestration_run_id,
                                    agent_run_id=claude_run.id,
                                    task_id=task_id,
                                ))
                            # Emit run.phase.changed to running_implementation
                            self._emit_event(RuntimeEvent(
                                schema_version=1,
                                event_type=EventType.RUN_PHASE_CHANGED,
                                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                                aggregate_id=str(orchestration_run_id),
                                correlation_id=cid,
                                source=EventSource.ORCHESTRATOR,
                                actor="orchestrator",
                                visibility=Visibility.OPERATOR,
                                payload={"phase": "running_implementation", "verify_round": progress.round_num or verify_round},
                                occurred_at=now_iso(),
                                conversation_id=conversation.id,
                                orchestration_run_id=orchestration_run_id,
                                task_id=task_id,
                            ))
                            if not self._uses_runtime_progress():
                                await self._emit_text_delta(
                                    chat_id,
                                    task_id,
                                    conversation.id,
                                    "\n\n" + render_user_progress_text(
                                        progress.phase,
                                        first_impl_delta=True,
                                    ),
                                )
                            # Send runtime progress to renderer
                            if self._interaction_renderer is not None:
                                await self._interaction_renderer.handle(
                                    InteractionEvent(
                                        event_type="runtime_progress",
                                        chat_id=chat_id,
                                        task_id=task_id,
                                        conversation_id=conversation.id,
                                        metadata={
                                            "runtime_state": self._build_runtime_state(
                                                "running_implementation", "claude", "running",
                                                retry_count=(progress.round_num or 1) - 1,
                                            ),
                                        },
                                    )
                                )
                elif progress.phase in (
                    OrchestrationProgress.ANALYSIS_STARTED,
                    OrchestrationProgress.ANALYSIS_COMPLETE,
                    OrchestrationProgress.IMPL_COMPLETE,
                    OrchestrationProgress.VERIFY_STARTED,
                    OrchestrationProgress.VERIFY_COMPLETE,
                ):
                    await self._handle_phase_progress(
                        progress=progress,
                        chat_id=chat_id,
                        task_id=task_id,
                        conversation_id=conversation.id,
                    )
                    if progress.phase == OrchestrationProgress.ANALYSIS_STARTED:
                        codex.set_runtime_context(
                            agent_run_id=codex_analysis_run_id,
                            role="analysis",
                        )
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.RUN_PHASE_CHANGED,
                            aggregate_type=AggregateType.ORCHESTRATION_RUN,
                            aggregate_id=str(orchestration_run_id),
                            correlation_id=cid,
                            source=EventSource.ORCHESTRATOR,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={"phase": "running_analysis"},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            task_id=task_id,
                        ))
                        # Emit agent.run.started for codex analysis
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.AGENT_RUN_STARTED,
                            aggregate_type=AggregateType.AGENT_RUN,
                            aggregate_id=str(codex_analysis_run_id),
                            correlation_id=cid,
                            source=EventSource.CODEX,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={"agent": "codex", "role": "analysis"},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            agent_run_id=codex_analysis_run_id,
                            task_id=task_id,
                        ))
                        if self._interaction_renderer is not None:
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="runtime_progress",
                                    chat_id=chat_id,
                                    task_id=task_id,
                                    conversation_id=conversation.id,
                                    metadata={
                                        "runtime_state": self._build_runtime_state(
                                            "running_analysis", "codex", "running",
                                        ),
                                    },
                                )
                            )
                    elif progress.phase == OrchestrationProgress.ANALYSIS_COMPLETE:
                        codex_analysis_text = progress.full_text or progress.text
                        self._ledger.update_agent_run_status(
                            codex_analysis_run_id,
                            "done",
                            completion_summary=codex_analysis_text[:2000],
                        )
                        codex_analysis_status = "done"
                        # Emit agent.run.completed for codex analysis
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.AGENT_RUN_COMPLETED,
                            aggregate_type=AggregateType.AGENT_RUN,
                            aggregate_id=str(codex_analysis_run_id),
                            correlation_id=cid,
                            source=EventSource.CODEX,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={"agent": "codex", "role": "analysis", "completion_summary": codex_analysis_text[:2000]},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            agent_run_id=codex_analysis_run_id,
                            task_id=task_id,
                        ))
                        self._record_workflow_overhead(
                            phase="codex_analysis",
                            overhead_input_tokens=approx_tokens(codex_analysis_text),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            agent_run_id=codex_analysis_run_id,
                            task_id=task_id,
                        )
                    elif progress.phase == OrchestrationProgress.IMPL_COMPLETE:
                        claude_implementation_text = progress.full_text or progress.text

                        # Detect Claude direct-delivery drift and emit security events.
                        claude_drift = _detect_claude_direct_delivery_drift(
                            claude_implementation_text
                        )
                        claude_agent_run_id = claude_backend.agent_run_id
                        for drift_finding in claude_drift:
                            self._emit_event(RuntimeEvent(
                                schema_version=1,
                                event_type=(
                                    EventType.SECURITY_TOKEN_ACCESS_ATTEMPTED
                                    if "token" in drift_finding.lower()
                                    else EventType.SECURITY_DELIVERY_BLOCKED
                                ),
                                aggregate_type=AggregateType.AGENT_RUN,
                                aggregate_id=str(claude_agent_run_id or orchestration_run_id),
                                correlation_id=cid,
                                source=EventSource.ORCHESTRATOR,
                                actor="orchestrator",
                                visibility=Visibility.OPERATOR,
                                payload={
                                    "finding": drift_finding,
                                    "agent": "claude",
                                    "role": "implementation",
                                },
                                occurred_at=now_iso(),
                                conversation_id=conversation.id,
                                orchestration_run_id=orchestration_run_id,
                                task_id=task_id,
                            ))

                        # Use the wrapper's agent_run_id if available (already created
                        # before send_streaming), otherwise create one as fallback.
                        impl_run_id = claude_backend.agent_run_id
                        if impl_run_id is None:
                            claude_run = self._ledger.create_agent_run(
                                conversation_id=conversation.id,
                                agent="claude",
                                role="implementation",
                                prompt_packet_summary=prompt[:120],
                            )
                            impl_run_id = claude_run.id
                        self._ledger.update_agent_run_status(
                            impl_run_id,
                            "done",
                            completion_summary=claude_implementation_text[:2000],
                        )
                        self._record_workflow_overhead(
                            phase="codex_to_claude_handoff",
                            overhead_input_tokens=approx_tokens(claude_implementation_text),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            agent_run_id=impl_run_id,
                            task_id=task_id,
                        )
                        if self._interaction_renderer is not None:
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="runtime_progress",
                                    chat_id=chat_id,
                                    task_id=task_id,
                                    conversation_id=conversation.id,
                                    metadata={
                                        "runtime_state": self._build_runtime_state(
                                            "running_implementation", "claude", "running",
                                            retry_count=(progress.round_num or 1) - 1,
                                        ),
                                    },
                                )
                            )
                    elif progress.phase == OrchestrationProgress.VERIFY_STARTED:
                        verify_round = progress.round_num or verify_round
                        verify_run = self._ledger.create_agent_run(
                            conversation_id=conversation.id,
                            agent="codex",
                            role="verification",
                            prompt_packet_summary=(
                                claude_implementation_text or prompt
                            )[:120],
                        )
                        codex_verification_run_id = verify_run.id
                        self._ledger.update_agent_run_status(
                            verify_run.id,
                            "running",
                        )
                        codex.set_runtime_context(
                            agent_run_id=verify_run.id,
                            role="verification",
                        )
                        # Re-query pending user context before verification.
                        # Mid-implementation follow-ups are stored as
                        # conversation.pending_context.recorded events and
                        # must be reviewed by Codex at this phase boundary.
                        had_pending_for_verify = False
                        if self._store is not None:
                            try:
                                pending = self._store._conn.execute(
                                    """
                                    SELECT payload_json FROM runtime_events
                                    WHERE conversation_id = ?
                                      AND event_type = 'conversation.pending_context.recorded'
                                    ORDER BY id ASC
                                    """,
                                    (conversation.id,),
                                ).fetchall()
                                if pending:
                                    pending_texts = []
                                    for row in pending:
                                        payload = __import__("json").loads(
                                            str(row["payload_json"])
                                        )
                                        preview = payload.get("text_preview", "")
                                        if preview:
                                            pending_texts.append(preview)
                                    if pending_texts and hasattr(
                                        orch, "set_pending_user_context"
                                    ):
                                        orch.set_pending_user_context(
                                            "; ".join(pending_texts)
                                        )
                                        had_pending_for_verify = True
                            except Exception:
                                logger.debug(
                                    "Failed to re-query pending context for verification",
                                    exc_info=True,
                                )
                        # Emit run.phase.changed to running_verification
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.RUN_PHASE_CHANGED,
                            aggregate_type=AggregateType.ORCHESTRATION_RUN,
                            aggregate_id=str(orchestration_run_id),
                            correlation_id=cid,
                            source=EventSource.ORCHESTRATOR,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={"phase": "running_verification", "verify_round": verify_round},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            task_id=task_id,
                        ))
                        # Emit verification.started
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.VERIFICATION_STARTED,
                            aggregate_type=AggregateType.ORCHESTRATION_RUN,
                            aggregate_id=str(orchestration_run_id),
                            correlation_id=cid,
                            source=EventSource.ORCHESTRATOR,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={"verify_round": verify_round},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            task_id=task_id,
                        ))
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.AGENT_RUN_STARTED,
                            aggregate_type=AggregateType.AGENT_RUN,
                            aggregate_id=str(verify_run.id),
                            correlation_id=cid,
                            source=EventSource.CODEX,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={"agent": "codex", "role": "verification"},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            agent_run_id=verify_run.id,
                            task_id=task_id,
                        ))
                        if self._interaction_renderer is not None:
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="runtime_progress",
                                    chat_id=chat_id,
                                    task_id=task_id,
                                    conversation_id=conversation.id,
                                    metadata={
                                        "runtime_state": self._build_runtime_state(
                                            "running_verification", "codex", "running",
                                            retry_count=(progress.round_num or 1) - 1,
                                        ),
                                    },
                                )
                            )
                    elif progress.phase == OrchestrationProgress.VERIFY_COMPLETE:
                        verify_round = progress.round_num or verify_round
                        verification_text = progress.full_text or progress.text
                        # Detect Codex verification delivery drift.
                        verify_drift = _detect_verification_delivery_drift(verification_text)
                        for drift_finding in verify_drift:
                            self._emit_event(RuntimeEvent(
                                schema_version=1,
                                event_type=(
                                    EventType.SECURITY_TOKEN_ACCESS_ATTEMPTED
                                    if "token" in drift_finding.lower()
                                    else EventType.SECURITY_DELIVERY_BLOCKED
                                ),
                                aggregate_type=AggregateType.AGENT_RUN,
                                aggregate_id=str(codex_verification_run_id or orchestration_run_id),
                                correlation_id=cid,
                                source=EventSource.ORCHESTRATOR,
                                actor="codex",
                                visibility=Visibility.OPERATOR,
                                payload={
                                    "finding": drift_finding,
                                    "agent": "codex",
                                    "role": "verification",
                                },
                                occurred_at=now_iso(),
                                conversation_id=conversation.id,
                                orchestration_run_id=orchestration_run_id,
                                task_id=task_id,
                            ))
                        # Parse the verification decision
                        try:
                            vd = VerificationDecision.parse(verification_text)
                            actual_decision = vd.decision
                        except Exception:
                            actual_decision = "need_user"
                        # If Codex verification itself shows drift, force retry.
                        if verify_drift and actual_decision == "pass":
                            actual_decision = "retry"
                        last_verification_decision = actual_decision
                        # Emit verification.decision.recorded
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.VERIFICATION_DECISION_RECORDED,
                            aggregate_type=AggregateType.ORCHESTRATION_RUN,
                            aggregate_id=str(orchestration_run_id),
                            correlation_id=cid,
                            source=EventSource.ORCHESTRATOR,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={"decision": actual_decision, "reason": verification_text[:500], "verify_round": verify_round},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            task_id=task_id,
                        ))
                        # Emit pending_context.reviewed if Codex reviewed pending context
                        if had_pending_for_verify:
                            self._emit_event(RuntimeEvent(
                                schema_version=1,
                                event_type=EventType.CONVERSATION_PENDING_CONTEXT_REVIEWED,
                                aggregate_type=AggregateType.CONVERSATION,
                                aggregate_id=str(conversation.id),
                                correlation_id=cid,
                                source=EventSource.CODEX,
                                actor="codex",
                                visibility=Visibility.OPERATOR,
                                payload={"reviewed_at_phase": "verification",
                                         "verify_round": verify_round,
                                         "conversation_id": conversation.id},
                                occurred_at=now_iso(),
                                conversation_id=conversation.id,
                                orchestration_run_id=orchestration_run_id,
                                task_id=task_id,
                            ))

                        # Emit verification.retry.requested NOT verification.completed for retry
                        if actual_decision == "retry":
                            self._emit_event(RuntimeEvent(
                                schema_version=1,
                                event_type=EventType.VERIFICATION_RETRY_REQUESTED,
                                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                                aggregate_id=str(orchestration_run_id),
                                correlation_id=cid,
                                source=EventSource.ORCHESTRATOR,
                                actor="codex",
                                visibility=Visibility.OPERATOR,
                                payload={"verify_round": verify_round, "reason": verification_text[:500]},
                                occurred_at=now_iso(),
                                conversation_id=conversation.id,
                                orchestration_run_id=orchestration_run_id,
                                task_id=task_id,
                            ))
                        if codex_verification_run_id is None:
                            verify_run = self._ledger.create_agent_run(
                                conversation_id=conversation.id,
                                agent="codex",
                                role="verification",
                                prompt_packet_summary=verification_text[:120],
                            )
                            codex_verification_run_id = verify_run.id
                        else:
                            verify_run = self._ledger.get_agent_run(
                                codex_verification_run_id
                            )
                        self._ledger.update_agent_run_status(
                            verify_run.id,
                            "done",
                            completion_summary=verification_text[:2000],
                        )
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.AGENT_RUN_COMPLETED,
                            aggregate_type=AggregateType.AGENT_RUN,
                            aggregate_id=str(verify_run.id),
                            correlation_id=cid,
                            source=EventSource.CODEX,
                            actor="codex",
                            visibility=Visibility.OPERATOR,
                            payload={
                                "agent": "codex",
                                "role": "verification",
                                "completion_summary": verification_text[:2000],
                            },
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            agent_run_id=verify_run.id,
                            task_id=task_id,
                        ))
                        self._record_workflow_overhead(
                            phase="codex_verification",
                            overhead_input_tokens=approx_tokens(verification_text),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            agent_run_id=verify_run.id,
                            task_id=task_id,
                        )
                        # Emit verification.completed (only if decision is NOT retry)
                        if actual_decision != "retry":
                            self._emit_event(RuntimeEvent(
                                schema_version=1,
                                event_type=EventType.VERIFICATION_COMPLETED,
                                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                                aggregate_id=str(orchestration_run_id),
                                correlation_id=cid,
                                source=EventSource.ORCHESTRATOR,
                                actor="codex",
                                visibility=Visibility.OPERATOR,
                                payload={"decision": actual_decision, "verify_round": verify_round},
                                occurred_at=now_iso(),
                                conversation_id=conversation.id,
                                orchestration_run_id=orchestration_run_id,
                                task_id=task_id,
                            ))
                elif progress.phase == OrchestrationProgress.FAILED:
                    terminal_sent = True
                    orch_result_status = "failed"
                    verify_round = progress.round_num or verify_round
                    terminal_text = progress.full_text or progress.text
                    self._ledger.set_task_status(task_id, TaskStatus.FAILED)
                    if progress.agent == "claude":
                        impl_run_id = claude_backend.agent_run_id
                        if impl_run_id is None:
                            claude_run = self._ledger.create_agent_run(
                                conversation_id=conversation.id,
                                agent="claude",
                                role="implementation",
                                prompt_packet_summary=prompt[:120],
                            )
                            impl_run_id = claude_run.id
                        self._ledger.update_agent_run_status(
                            impl_run_id,
                            "failed",
                            completion_summary=terminal_text[:2000],
                        )
                        self._ledger.set_conversation_active_claude_run(
                            conversation.id, impl_run_id
                        )
                    elif progress.agent == "codex" and not codex_analysis_text:
                        self._ledger.update_agent_run_status(
                            codex_analysis_run_id,
                            "failed",
                            completion_summary=terminal_text[:2000],
                        )
                        codex_analysis_status = "failed"
                    # Emit run.failed
                    self._emit_event(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.RUN_FAILED,
                        aggregate_type=AggregateType.ORCHESTRATION_RUN,
                        aggregate_id=str(orchestration_run_id),
                        correlation_id=cid,
                        source=EventSource.ORCHESTRATOR,
                        actor="orchestrator",
                        visibility=Visibility.USER,
                        payload={
                            "reason": terminal_text[:500],
                            "last_active_agent": progress.agent or "unknown",
                        },
                        occurred_at=now_iso(),
                        conversation_id=conversation.id,
                        orchestration_run_id=orchestration_run_id,
                        task_id=task_id,
                    ))
                    await self._emit_failed(chat_id, task_id, progress.text)
                elif progress.phase == OrchestrationProgress.COMPLETE:
                    terminal_sent = True
                    orch_result_status = progress.result_status or "passed"
                    verify_round = progress.round_num or verify_round
                    if progress.text or progress.full_text:
                        terminal_text = progress.full_text or progress.text
                    if orch_result_status == "passed":
                        if last_verification_decision and last_verification_decision != "pass":
                            reason = (
                                "refusing run.completed after verification decision "
                                f"{last_verification_decision}"
                            )
                            orch_result_status = "failed"
                            terminal_text = reason
                            self._ledger.set_task_status(task_id, TaskStatus.FAILED)
                            self._ledger.update_orchestration_run(
                                orchestration_run_id,
                                status="failed",
                                current_step="failed",
                                last_verification_result=reason,
                            )
                            self._emit_event(RuntimeEvent(
                                schema_version=1,
                                event_type=EventType.RUN_FAILED,
                                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                                aggregate_id=str(orchestration_run_id),
                                correlation_id=cid,
                                source=EventSource.ORCHESTRATOR,
                                actor="orchestrator",
                                visibility=Visibility.USER,
                                payload={
                                    "reason": reason,
                                    "verify_round": verify_round,
                                    "verification_decision": last_verification_decision,
                                },
                                occurred_at=now_iso(),
                                conversation_id=conversation.id,
                                orchestration_run_id=orchestration_run_id,
                                task_id=task_id,
                            ))
                            await self._emit_failed(
                                chat_id,
                                task_id,
                                reason,
                            )
                            continue
                        if not last_verification_decision:
                            analysis_only = (
                                not claude_implementation_text
                                and not verification_text
                            )
                            if analysis_only:
                                reply_text = (
                                    terminal_text
                                    or codex_analysis_text
                                )
                                reply_text = _visible_analysis_reply(reply_text)
                                if reply_text:
                                    await self._emit_text_delta(
                                        chat_id,
                                        task_id,
                                        conversation.id,
                                        reply_text,
                                    )
                                last_verification_decision = "pass"
                                self._emit_event(RuntimeEvent(
                                    schema_version=1,
                                    event_type=EventType.VERIFICATION_DECISION_RECORDED,
                                    aggregate_type=AggregateType.ORCHESTRATION_RUN,
                                    aggregate_id=str(orchestration_run_id),
                                    correlation_id=cid,
                                    source=EventSource.ORCHESTRATOR,
                                    actor="codex",
                                    visibility=Visibility.OPERATOR,
                                    payload={
                                        "decision": "pass",
                                        "reason": (
                                            terminal_text
                                            or codex_analysis_text
                                            or "Codex determined no implementation needed."
                                        )[:500],
                                        "verify_round": 0,
                                        "implicit": True,
                                    },
                                    occurred_at=now_iso(),
                                    conversation_id=conversation.id,
                                    orchestration_run_id=orchestration_run_id,
                                    task_id=task_id,
                                ))
                        self._ledger.set_task_status(task_id, TaskStatus.DONE)
                        # Emit verification.completed + run.completed
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.RUN_COMPLETED,
                            aggregate_type=AggregateType.ORCHESTRATION_RUN,
                            aggregate_id=str(orchestration_run_id),
                            correlation_id=cid,
                            source=EventSource.ORCHESTRATOR,
                            actor="orchestrator",
                            visibility=Visibility.USER,
                            payload={"verify_round": verify_round},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            task_id=task_id,
                        ))
                        await self._emit_completed(
                            chat_id, task_id, conversation.id, workspace_path,
                            runtime_state=self._build_runtime_state(
                                "completed", is_terminal=True,
                            ),
                        )
                    elif orch_result_status == "needs_user":
                        self._ledger.set_task_status(task_id, TaskStatus.FAILED)
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.RUN_FAILED,
                            aggregate_type=AggregateType.ORCHESTRATION_RUN,
                            aggregate_id=str(orchestration_run_id),
                            correlation_id=cid,
                            source=EventSource.ORCHESTRATOR,
                            actor="orchestrator",
                            visibility=Visibility.USER,
                            payload={"reason": "needs_user", "verify_round": verify_round},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            task_id=task_id,
                        ))
                        await self._emit_failed(
                            chat_id,
                            task_id,
                            progress.text or "需要用户输入以继续。",
                        )
                    else:
                        self._ledger.set_task_status(task_id, TaskStatus.FAILED)
                        self._emit_event(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.RUN_FAILED,
                            aggregate_type=AggregateType.ORCHESTRATION_RUN,
                            aggregate_id=str(orchestration_run_id),
                            correlation_id=cid,
                            source=EventSource.ORCHESTRATOR,
                            actor="orchestrator",
                            visibility=Visibility.USER,
                            payload={"reason": "verification_failed", "verify_round": verify_round},
                            occurred_at=now_iso(),
                            conversation_id=conversation.id,
                            orchestration_run_id=orchestration_run_id,
                            task_id=task_id,
                        ))
                        await self._emit_failed(
                            chat_id,
                            task_id,
                            progress.text or "编排未通过验收。",
                        )
        except Exception as exc:
            logger.exception("Chief engineer background orchestration failed")
            terminal_sent = True
            orch_result_status = "failed"
            terminal_text = str(exc)
            self._ledger.set_task_status(task_id, TaskStatus.FAILED)
            self._ledger.update_orchestration_run(
                orchestration_run_id,
                status="failed",
                current_step="error",
                last_verification_result=str(exc)[:500],
            )
            self._ledger.update_agent_run_status(
                codex_analysis_run_id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            # Emit run.failed for exception
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_FAILED,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=str(orchestration_run_id),
                correlation_id=cid,
                source=EventSource.ORCHESTRATOR,
                actor="orchestrator",
                visibility=Visibility.USER,
                payload={"reason": str(exc)[:500], "last_active_agent": "unknown"},
                occurred_at=now_iso(),
                conversation_id=conversation.id,
                orchestration_run_id=orchestration_run_id,
                task_id=task_id,
            ))
            await self._emit_failed(chat_id, task_id, str(exc))

        if not codex_analysis_text and orch_result_status == "failed":
            codex_analysis_text = terminal_text
        if verification_text:
            decision = (
                "verify_passed"
                if orch_result_status == "passed"
                else "verify_failed_retry"
            )
            self._ledger.record_orchestration_decision(
                run_id=orchestration_run_id,
                decision=decision,
                reason=verification_text[:500],
                next_agent="" if orch_result_status == "passed" else "claude",
            )
        self._ledger.update_orchestration_run(
            orchestration_run_id,
            status=orch_result_status if orch_result_status != "running" else "failed",
            verify_round=verify_round,
            current_step="verify" if orch_result_status == "passed" else "retry",
            last_codex_analysis=codex_analysis_text[:500],
            last_claude_summary=claude_implementation_text[:500],
            last_verification_result=(verification_text or terminal_text)[:500],
        )
        codex_run_status = codex_analysis_status
        if codex_run_status == "running":
            codex_run_status = "failed" if orch_result_status == "failed" else "done"
        if codex_analysis_text or codex_run_status == "failed":
            self._ledger.update_agent_run_status(
                codex_analysis_run_id,
                codex_run_status,
                completion_summary=codex_analysis_text[:2000],
            )

        status_labels = {
            "passed": "验收通过",
            "failed": "验证失败",
            "needs_user": "需要用户输入",
        }
        label = status_labels.get(orch_result_status, orch_result_status)
        self._ledger.update_conversation_summary(
            conversation.id,
            trim_to_budget(
                f"总工程师第{verify_round}轮: {label}",
                ContextBudget().conversation_summary_tokens,
            ),
        )

    async def _handle_phase_progress(
        self,
        *,
        progress: OrchestrationProgress,
        chat_id: int,
        task_id: int,
        conversation_id: int,
    ) -> None:
        if self._uses_runtime_progress():
            return
        if progress.text:
            text = render_user_progress_text(progress.phase)
            if text:
                await self._emit_text_delta(
                    chat_id,
                    task_id,
                    conversation_id,
                    "\n\n" + text,
                )

    async def _emit_text_delta(
        self,
        chat_id: int,
        task_id: int,
        conversation_id: int,
        text: str,
    ) -> None:
        if self._interaction_renderer is None:
            return
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="text_delta",
                chat_id=chat_id,
                task_id=task_id,
                conversation_id=conversation_id,
                text=text,
            )
        )

    async def _emit_completed(
        self,
        chat_id: int,
        task_id: int,
        conversation_id: int,
        workspace_path: str,
        runtime_state: RuntimeRunState | None = None,
    ) -> None:
        if self._interaction_renderer is None:
            return
        task = self._service.get_task(task_id)
        metadata = {
            "has_diff": (
                _workspace_has_changes(workspace_path)
                or bool(task.changed_file_count)
            ),
        }
        if runtime_state is not None:
            metadata["runtime_state"] = runtime_state
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="run_completed",
                chat_id=chat_id,
                task_id=task_id,
                conversation_id=conversation_id,
                metadata=metadata,
            )
        )

    async def _emit_failed(self, chat_id: int, task_id: int, text: str) -> None:
        if self._interaction_renderer is None:
            return
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="run_failed",
                chat_id=chat_id,
                task_id=task_id,
                text=text,
            )
        )


def _workspace_has_changes(workspace_path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", workspace_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())
