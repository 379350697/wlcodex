from __future__ import annotations

import logging
from typing import Any

from wlcodex.relay.events import RelayEvent
from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES
from wlcodex.runtime_events import EventType
from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef

logger = logging.getLogger(__name__)


_DELTA_EVENT_TYPES = {
    EventType.MODEL_TEXT_DELTA,
    EventType.MODEL_REASONING_DELTA,
    EventType.COMMAND_OUTPUT_DELTA,
    EventType.TOOL_CALL_STARTED,
    EventType.TOOL_CALL_PROGRESS,
    EventType.TOOL_CALL_COMPLETED,
    EventType.TOOL_CALL_FAILED,
    "model_text_delta",
    "model_reasoning_delta",
    "command_output_delta",
    "tool_call_started",
    "tool_call_progress",
    "tool_call_completed",
    "tool_call_failed",
}

_COMPLETION_EVENT_TYPES = {
    EventType.AGENT_RUN_COMPLETED,
    EventType.AGENT_RUN_FAILED,
    "agent_run_completed",
    "agent_run_failed",
}


class RelayOnsiteBridge:
    """Attach the active relay role to Onsite and mirror native frames.

    The bridge is intentionally independent of Telegram and the relay web UI:
    it uses Relay events to attach/detach terminal refs, then uses runtime
    events to record append-only frames for the currently active role session.
    """

    def __init__(
        self,
        *,
        relay_service: Any,
        terminal_manager: Any,
    ) -> None:
        self._relay_service = relay_service
        self._terminal_manager = terminal_manager
        self._active_by_task: dict[int, TerminalSessionRef] = {}
        self._ref_by_agent_run: dict[int, TerminalSessionRef] = {}
        self._role_by_agent_run: dict[int, tuple[int, str]] = {}
        self._sequence_by_session: dict[str, int] = {}

    def project_relay_event(self, event: RelayEvent) -> None:
        if event.event_type == "role.streaming":
            self._attach_role(event)
            return
        if event.event_type in {"role.status", "task.completed", "task.interrupted"}:
            self._detach_completed_role(event)

    def project_runtime_event(self, runtime_event: Any) -> None:
        agent_run_id = _agent_run_id(runtime_event)
        if agent_run_id is None:
            return
        ref = self._ref_by_agent_run.get(agent_run_id)
        if ref is None:
            ref = self._attach_from_runtime_mapping(agent_run_id)
        if ref is None:
            return

        event_type = str(getattr(runtime_event, "event_type", "") or "")
        if event_type in _DELTA_EVENT_TYPES:
            frame = self._frame_from_runtime_event(ref, runtime_event)
            if frame is not None:
                self._terminal_manager.record_frame(ref, frame)
        if event_type in _COMPLETION_EVENT_TYPES or _is_failed_activity(runtime_event):
            self._detach_ref(ref)

    def _attach_role(self, event: RelayEvent) -> TerminalSessionRef | None:
        payload = dict(event.payload or {})
        role = str(event.role or payload.get("role") or "").strip()
        native_session_id = str(payload.get("native_session_id") or "").strip()
        if not role or not native_session_id:
            return None

        task_id = int(event.task_id)
        self._detach_task(task_id)
        detail = self._relay_service.get_task(task_id)
        job = next((item for item in detail.role_jobs if item.role == role), None)
        if job is None:
            return None
        provider = str(getattr(job, "provider", "") or payload.get("provider") or "")
        agent = _provider_agent(provider)
        strategy = _provider_strategy(
            provider=provider,
            provider_engine=str(getattr(job, "provider_engine", "") or ""),
        )
        try:
            ref = self._terminal_manager.attach(
                conversation_id=task_id,
                agent=agent,
                strategy=strategy,
                external_session_id=native_session_id,
            )
        except Exception as exc:
            logger.debug(
                "Relay onsite attach skipped: task=%s role=%s provider=%s error=%s",
                task_id,
                role,
                provider,
                exc,
            )
            return None

        self._active_by_task[task_id] = ref
        if job.agent_run_id is not None:
            agent_run_id = int(job.agent_run_id)
            self._ref_by_agent_run[agent_run_id] = ref
            self._role_by_agent_run[agent_run_id] = (task_id, role)
        self._record_system_frame(
            ref,
            role=role,
            text=f"{RELAY_ROLE_DISPLAY_NAMES.get(role, role)} 开始现场执行。",
        )
        return ref

    def _attach_from_runtime_mapping(
        self,
        agent_run_id: int,
    ) -> TerminalSessionRef | None:
        mapping = self._relay_service.role_for_agent_run(agent_run_id)
        if mapping is None:
            return None
        task_id, role = mapping
        detail = self._relay_service.get_task(task_id)
        job = next((item for item in detail.role_jobs if item.role == role), None)
        if job is None or not job.native_session_id:
            return None
        existing = self._active_by_task.get(task_id)
        if (
            existing is not None
            and existing.external_session_id == job.native_session_id
            and existing.status == "attached"
        ):
            self._ref_by_agent_run[agent_run_id] = existing
            self._role_by_agent_run[agent_run_id] = (task_id, role)
            return existing
        event = RelayEvent(
            task_id=task_id,
            event_type="role.streaming",
            sequence=0,
            role=role,
            payload={
                "role": role,
                "provider": job.provider,
                "native_session_id": job.native_session_id,
            },
        )
        return self._attach_role(event)

    def _detach_completed_role(self, event: RelayEvent) -> None:
        if event.event_type in {"task.completed", "task.interrupted"}:
            self._detach_task(int(event.task_id))
            return
        status = str((event.payload or {}).get("status") or "").strip()
        if status not in {"passed", "completed", "waiting", "failed", "blocked", "interrupted"}:
            return
        self._detach_task(int(event.task_id))

    def _detach_task(self, task_id: int) -> None:
        ref = self._active_by_task.pop(task_id, None)
        if ref is not None:
            self._detach_ref(ref)

    def _detach_ref(self, ref: TerminalSessionRef) -> None:
        if ref.status != "attached":
            return
        try:
            self._terminal_manager.detach(ref)
        except Exception:
            logger.debug("Relay onsite detach failed", exc_info=True)
        for agent_run_id, candidate in list(self._ref_by_agent_run.items()):
            if candidate.external_session_id == ref.external_session_id:
                self._ref_by_agent_run.pop(agent_run_id, None)
                self._role_by_agent_run.pop(agent_run_id, None)
        for task_id, candidate in list(self._active_by_task.items()):
            if candidate.external_session_id == ref.external_session_id:
                self._active_by_task.pop(task_id, None)

    def _frame_from_runtime_event(
        self,
        ref: TerminalSessionRef,
        runtime_event: Any,
    ) -> TerminalFrame | None:
        text = _runtime_text(runtime_event)
        event_type = str(getattr(runtime_event, "event_type", "") or "")
        if not text and event_type.startswith("tool.call."):
            payload = dict(getattr(runtime_event, "payload", {}) or {})
            text = str(
                payload.get("summary")
                or payload.get("name")
                or payload.get("tool")
                or event_type
            )
        if not text:
            return None
        agent_run_id = _agent_run_id(runtime_event)
        role = ""
        if agent_run_id is not None:
            role = self._role_by_agent_run.get(agent_run_id, ("", ""))[1]
        frame_kind = _frame_kind(event_type)
        return TerminalFrame(
            conversation_id=ref.conversation_id,
            agent=ref.agent,
            phase=role or "relay",
            text=text,
            frame_kind=frame_kind,
            sequence=self._next_sequence(ref),
        )

    def _record_system_frame(
        self,
        ref: TerminalSessionRef,
        *,
        role: str,
        text: str,
    ) -> None:
        self._terminal_manager.record_frame(
            ref,
            TerminalFrame(
                conversation_id=ref.conversation_id,
                agent=ref.agent,
                phase=role,
                text=text,
                frame_kind="system",
                sequence=self._next_sequence(ref),
            ),
        )

    def _next_sequence(self, ref: TerminalSessionRef) -> int:
        key = ref.external_session_id
        value = self._sequence_by_session.get(key, 0) + 1
        self._sequence_by_session[key] = value
        return value


def _agent_run_id(runtime_event: Any) -> int | None:
    value = getattr(runtime_event, "agent_run_id", None)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _provider_agent(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in {"codex", "claude"}:
        return normalized
    if "codex" in normalized:
        return "codex"
    if "claude" in normalized or "deepseek" in normalized:
        return "claude"
    return normalized


def _provider_strategy(*, provider: str, provider_engine: str) -> str:
    provider = provider.strip().lower()
    provider_engine = provider_engine.strip().lower()
    if provider == "codex" or provider_engine == "app-server":
        return "app_server"
    if provider_engine in {"cli-local", "stream-json"}:
        return "stream_json"
    return provider_engine or "native"


def _runtime_text(runtime_event: Any) -> str:
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    return str(
        payload.get("delta")
        or payload.get("text")
        or payload.get("message")
        or payload.get("content")
        or payload.get("output")
        or payload.get("chunk")
        or ""
    )


def _frame_kind(event_type: str) -> str:
    if event_type.startswith("command."):
        return "stdout" if event_type == EventType.COMMAND_OUTPUT_DELTA else "tool"
    if event_type.startswith("tool.call."):
        return "tool"
    if "failed" in event_type:
        return "error"
    return "stdout"


def _is_failed_activity(runtime_event: Any) -> bool:
    event_type = str(getattr(runtime_event, "event_type", "") or "")
    if event_type not in {EventType.AGENT_RUN_ACTIVITY, "agent_run_activity"}:
        return False
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    return str(payload.get("status") or "").strip().lower() == "failed"
