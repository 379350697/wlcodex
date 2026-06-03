from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from wlcodex.agent_backend import AgentRequest
from wlcodex.native_agents.claude_local_sessions import (
    ClaudeLocalSession,
    ClaudeLocalSessionIndex,
)
from wlcodex.native_agents.runtime_events import (
    NativeAgentRuntimeEmitter,
    extract_native_agent_text,
)
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore


@dataclass(frozen=True)
class _RunOutcome:
    status: str
    claude_session_id: str = ""
    error: str = ""


_CLAUDE_REASONING_EFFORTS = [
    {"reasoningEffort": "low", "description": "轻量"},
    {"reasoningEffort": "medium", "description": "正常"},
    {"reasoningEffort": "high", "description": "深度"},
    {"reasoningEffort": "xhigh", "description": "极深"},
    {"reasoningEffort": "max", "description": "最大"},
]


class ClaudeCliLocalProvider:
    provider = "claude"
    provider_engine = "cli-local"

    def __init__(
        self,
        *,
        engine: Any,
        session_store: NativeAgentSessionStore,
        runtime_store: RuntimeEventStore | None = None,
        default_cwd: str = "",
        session_index: ClaudeLocalSessionIndex | None = None,
    ) -> None:
        self._engine = engine
        self._session_store = session_store
        self._runtime_store = runtime_store
        self._default_cwd = default_cwd
        self._session_index = session_index or ClaudeLocalSessionIndex()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def wait_for_background_tasks(self) -> None:
        while self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks))

    async def status(self) -> NativeAgentStatus:
        enabled = bool(getattr(self._engine, "enabled", False))
        binary_error = _engine_config_value(self._engine, "binary_resolution_error")
        status_code = "ok" if enabled else "disabled"
        message = ""
        if binary_error:
            status_code = "binary_unresolved"
            message = str(binary_error)
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=enabled,
            connected=enabled and not bool(binary_error),
            status_code=status_code,
            message=message,
            metadata={
                "binary": _engine_config_value(self._engine, "binary"),
                "permission_mode": str(getattr(self._engine, "permission_mode", "")),
            },
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": (
                    "Claude Code CLI continuation starts a new prompt turn."
                ),
                "can_interrupt": (
                    "Claude CLI provider does not hold a long-lived process handle yet."
                ),
                "can_resolve_approval": (
                    "Claude CLI permissions are controlled by Claude Code permission mode."
                ),
            },
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        self._import_local_sessions(limit=max(limit, 50))
        return self._session_store.list_recent(
            provider=self.provider,
            provider_engine=self.provider_engine,
            limit=limit,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        model = _engine_config_value(self._engine, "model")
        if not model:
            return []
        effort = _engine_config_value(self._engine, "effort") or "medium"
        return [
            {
                "id": model,
                "model": model,
                "displayName": model,
                "isDefault": True,
                "defaultReasoningEffort": effort,
                "supportedReasoningEfforts": list(_CLAUDE_REASONING_EFFORTS),
                "serviceTiers": [],
            }
        ]

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._ensure_enabled()
        native_session_id = f"claude-cli-{uuid4()}"
        native_turn_id = _new_turn_id()
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=_title_from_prompt(prompt),
            cwd=cwd or self._default_cwd,
            source_kind="claude_cli_local",
            status="running",
            last_turn_id=native_turn_id,
            metadata={},
        )
        self._start_background_prompt(
            session=session,
            native_turn_id=native_turn_id,
            prompt=prompt,
            resume_session_id="",
        )
        return _control_result(
            session,
            status="started",
            turn_id=native_turn_id,
            turn_running=True,
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        self._ensure_enabled()
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"claude-cli-{uuid4()}",
            title="Claude CLI session",
            cwd=cwd or self._default_cwd,
            source_kind="claude_cli_local",
            status="created",
            metadata={},
        )
        return _control_result(session, status="created")

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        session = self._lookup_session(native_session_id, import_local=True)
        session = self._sync_local_transcript(session)
        return {"thread": session.to_json_dict(), "turns": []}

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._lookup_session(native_session_id, import_local=True)
        session = self._sync_local_transcript(session)
        return _control_result(session, status="attached")

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._lookup_session(native_session_id, import_local=True)
        session = self._sync_local_transcript(session)
        return _control_result(session, status="synced")

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._ensure_enabled()
        session = self._lookup_session(native_session_id, import_local=True)
        native_turn_id = _new_turn_id()
        session = self._session_store.update_session(
            session.id,
            status="running",
            last_turn_id=native_turn_id,
        )
        self._start_background_prompt(
            session=session,
            native_turn_id=native_turn_id,
            prompt=prompt,
            resume_session_id=_claude_session_id(session),
        )
        return _control_result(
            session,
            status="continued",
            turn_id=native_turn_id,
            turn_running=True,
        )

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        raise NotImplementedError(
            "Claude CLI provider does not support active-turn steering"
        )

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult:
        raise NotImplementedError("Claude CLI provider does not support interrupt yet")

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        raise KeyError(request_id)

    async def _run_prompt(
        self,
        *,
        prompt: str,
        cwd: str,
        resume_session_id: str,
        session: NativeAgentSession | None = None,
        native_turn_id: str = "",
    ) -> _RunOutcome:
        latest_session_id = resume_session_id
        error = ""
        extra = {"resume_session_id": resume_session_id} if resume_session_id else {}
        request = AgentRequest(prompt=prompt, workspace_path=cwd, extra=extra)
        async for event in self._engine.send_streaming(request):
            event_type = str(getattr(event, "event_type", "") or "")
            event_session_id = str(getattr(event, "session_id", "") or "")
            if event_session_id:
                latest_session_id = event_session_id
            if event_type == "error":
                error = str(getattr(event, "delta", "") or "")
                continue
            text = extract_native_agent_text(event)
            if text:
                self._emit_text_delta(
                    session=session,
                    native_turn_id=native_turn_id,
                    delta=text,
                )
        if error:
            return _RunOutcome(
                status="failed",
                claude_session_id=latest_session_id,
                error=error,
            )
        return _RunOutcome(status="done", claude_session_id=latest_session_id)

    def _update_after_run(
        self,
        session: NativeAgentSession,
        outcome: _RunOutcome,
    ) -> NativeAgentSession:
        metadata = dict(session.metadata)
        if outcome.claude_session_id:
            metadata["claude_session_id"] = outcome.claude_session_id
        if outcome.error:
            metadata["error"] = outcome.error
        else:
            metadata.pop("error", None)
        return self._session_store.update_session(
            session.id,
            status=outcome.status,
            metadata=metadata,
        )

    def _lookup_session(
        self,
        native_session_id: str,
        *,
        import_local: bool = False,
    ) -> NativeAgentSession:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None and import_local:
            local_session = self._session_index.get(native_session_id)
            if local_session is not None:
                session = self._import_local_session(local_session)
        if session is None:
            raise KeyError(native_session_id)
        return session

    def _import_local_sessions(self, *, limit: int) -> None:
        for local_session in self._session_index.list_recent(limit=limit):
            self._import_local_session(local_session)

    def _import_local_session(
        self,
        local_session: ClaudeLocalSession,
    ) -> NativeAgentSession:
        existing = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=local_session.session_id,
        )
        if existing is None:
            existing = self._find_session_by_claude_session_id(local_session.session_id)
        metadata = _local_session_metadata(local_session)
        if existing is not None:
            merged_metadata = dict(existing.metadata)
            merged_metadata.update(metadata)
            return self._session_store.update_session(
                existing.id,
                title=existing.title or local_session.title,
                cwd=existing.cwd or local_session.cwd or self._default_cwd,
                source_kind=existing.source_kind or "claude_cli_local",
                status=existing.status or "done",
                activity_at=local_session.updated_at,
                metadata=merged_metadata,
            )
        return self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=local_session.session_id,
            title=local_session.title,
            cwd=local_session.cwd or self._default_cwd,
            source_kind="claude_cli_local",
            status="done",
            last_turn_id=f"cli-local-history-{local_session.session_id}",
            activity_at=local_session.updated_at,
            metadata=metadata,
        )

    def _find_session_by_claude_session_id(
        self,
        claude_session_id: str,
    ) -> NativeAgentSession | None:
        for session in self._session_store.list_recent(
            provider=self.provider,
            provider_engine=self.provider_engine,
            limit=500,
        ):
            if _claude_session_id(session) == claude_session_id:
                return session
        return None

    def _sync_local_transcript(
        self,
        session: NativeAgentSession,
    ) -> NativeAgentSession:
        if self._runtime_store is None:
            return session
        claude_session_id = _claude_session_id(session) or session.native_session_id
        entries = self._session_index.read_transcript(claude_session_id)
        synced_count = int(session.metadata.get("claude_synced_message_count") or 0)
        if synced_count >= len(entries):
            return session
        emitter = self._emitter()
        if emitter is None:
            return session
        native_turn_id = session.last_turn_id or f"cli-local-history-{claude_session_id}"
        for index, entry in enumerate(entries[synced_count:], start=synced_count):
            entry_turn_id = f"{native_turn_id}-{index}"
            if entry.role == "user":
                emitter.user_message(session, native_turn_id=entry_turn_id, text=entry.text)
            elif entry.role == "assistant":
                emitter.text_delta(session, native_turn_id=entry_turn_id, delta=entry.text)
        metadata = dict(session.metadata)
        metadata["claude_synced_message_count"] = len(entries)
        return self._session_store.update_session(session.id, metadata=metadata)

    def _ensure_enabled(self) -> None:
        if not bool(getattr(self._engine, "enabled", False)):
            raise RuntimeError("Claude CLI provider is disabled")

    def _start_background_prompt(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        resume_session_id: str,
    ) -> None:
        emitter = self._emitter()
        if emitter is not None:
            emitter.started(session, native_turn_id=native_turn_id)
            emitter.user_message(session, native_turn_id=native_turn_id, text=prompt)
        task = asyncio.create_task(
            self._run_prompt_to_terminal_state(
                session=session,
                native_turn_id=native_turn_id,
                prompt=prompt,
                resume_session_id=resume_session_id,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_prompt_to_terminal_state(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        resume_session_id: str,
    ) -> None:
        try:
            outcome = await self._run_prompt(
                prompt=prompt,
                cwd=session.cwd,
                resume_session_id=resume_session_id,
                session=session,
                native_turn_id=native_turn_id,
            )
        except Exception as exc:  # pragma: no cover - defensive task boundary
            outcome = _RunOutcome(
                status="failed",
                claude_session_id=resume_session_id,
                error=str(exc),
            )
        updated = self._update_after_run(session, outcome)
        emitter = self._emitter()
        if emitter is None:
            return
        if outcome.status == "done":
            emitter.completed(updated, native_turn_id=native_turn_id)
        else:
            emitter.failed(
                updated,
                native_turn_id=native_turn_id,
                error=outcome.error or "Claude CLI run failed",
            )

    def _emitter(self) -> NativeAgentRuntimeEmitter | None:
        if self._runtime_store is None:
            return None
        return NativeAgentRuntimeEmitter(
            runtime_store=self._runtime_store,
            provider=self.provider,
            provider_engine=self.provider_engine,
            source_kind="claude_cli_local",
        )

    def _emit_text_delta(
        self,
        *,
        session: NativeAgentSession | None,
        native_turn_id: str,
        delta: str,
    ) -> None:
        if session is None or not native_turn_id:
            return
        emitter = self._emitter()
        if emitter is not None:
            emitter.text_delta(session, native_turn_id=native_turn_id, delta=delta)


def _control_result(
    session: NativeAgentSession,
    *,
    status: str,
    turn_id: str = "",
    turn_running: bool = False,
) -> NativeAgentControlResult:
    return NativeAgentControlResult(
        provider=session.provider,
        provider_engine=session.provider_engine,
        native_session_id=session.native_session_id,
        agent_run_id=session.agent_run_id,
        turn_id=turn_id,
        active_turn_id=turn_id if turn_running else "",
        turn_running=turn_running,
        status=status,
    )


def _title_from_prompt(prompt: str) -> str:
    return prompt.strip()[:80] or "Claude CLI session"


def _claude_session_id(session: NativeAgentSession) -> str:
    return str(session.metadata.get("claude_session_id", "") or "")


def _local_session_metadata(local_session: ClaudeLocalSession) -> dict[str, str]:
    metadata = {
        "claude_session_id": local_session.session_id,
        "claude_source_path": local_session.source_path,
    }
    if local_session.entrypoint:
        metadata["entrypoint"] = local_session.entrypoint
    if local_session.version:
        metadata["version"] = local_session.version
    if local_session.git_branch:
        metadata["git_branch"] = local_session.git_branch
    if local_session.permission_mode:
        metadata["permission_mode"] = local_session.permission_mode
    return metadata


def _new_turn_id() -> str:
    return f"cli-local-turn-{uuid4()}"


def _engine_config_value(engine: Any, field: str) -> str:
    config = getattr(engine, "_config", None)
    return str(getattr(config, field, "") or "")
