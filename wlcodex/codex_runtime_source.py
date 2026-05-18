"""Codex runtime event source — maps BackendEvent → RuntimeEvent.

Pure conversion layer: stateless, no I/O, testable without app-server.
Each BackendEvent maps to zero or more RuntimeEvent instances using the
shared envelope contract defined in wlcodex.runtime_events.
"""

from __future__ import annotations

from wlcodex.codex_backend import BackendEvent
from wlcodex.runtime_events import (
    SCHEMA_VERSION,
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)

# Type alias for mapping handler functions.
_Handler = object  # Callable[[CodexRuntimeSource, dict], list[RuntimeEvent]]


class CodexRuntimeSource:
    """Maps Codex BackendEvent → RuntimeEvent with correlation context.

    Created once per agent run with the relevant IDs.  The orchestration
    layer calls :meth:`map_event` for each BackendEvent emitted by the
    app-server backend.
    """

    def __init__(
        self,
        correlation_id: str,
        agent_run_id: int,
        *,
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
        task_id: int | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.agent_run_id = agent_run_id
        self.conversation_id = conversation_id
        self.orchestration_run_id = orchestration_run_id
        self.task_id = task_id
        self._causation_id: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def map_event(
        self,
        backend_event: BackendEvent,
        *,
        causation_id: int | None = None,
    ) -> list[RuntimeEvent]:
        """Map a Codex BackendEvent to zero or more RuntimeEvents.

        *causation_id* is forwarded to every RuntimeEvent produced by
        this call.
        """
        handler = _EVENT_MAP.get(backend_event.event_type)
        if handler is None:
            return []
        self._causation_id = causation_id
        return handler(self, backend_event.payload)  # type: ignore[operator]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make(
        self,
        event_type: str,
        payload: dict,
        *,
        aggregate_type: str = AggregateType.AGENT_RUN,
        aggregate_id: str | None = None,
        visibility: str = Visibility.INTERNAL,
        causation_id: int | None = None,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            schema_version=SCHEMA_VERSION,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id if aggregate_id is not None else str(self.agent_run_id),
            correlation_id=self.correlation_id,
            source=EventSource.CODEX,
            actor="codex",
            visibility=visibility,
            payload=payload,
            occurred_at=now_iso(),
            conversation_id=self.conversation_id,
            orchestration_run_id=self.orchestration_run_id,
            agent_run_id=self.agent_run_id,
            task_id=self.task_id,
            causation_id=(
                causation_id if causation_id is not None else self._causation_id
            ),
        )


# ------------------------------------------------------------------
# Per-event-type mapping handlers (module-level pure functions)
# ------------------------------------------------------------------


def _map_thread_started(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    thread_id = payload.get("threadId") or payload.get("id")
    return [src._make(
        EventType.AGENT_RUN_ACTIVITY,
        {"action": "thread_started", "threadId": thread_id},
    )]


def _map_thread_status_changed(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    return [src._make(
        EventType.AGENT_RUN_ACTIVITY,
        {"action": "thread_status_changed", "threadId": payload.get("threadId")},
    )]


def _map_turn_started(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    return [src._make(
        EventType.AGENT_RUN_ACTIVITY,
        {
            "action": "turn_started",
            "threadId": payload.get("threadId"),
            "turnId": payload.get("turnId"),
        },
    )]


def _map_turn_completed(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    turn = payload.get("turn")
    status = ""
    if isinstance(turn, dict):
        status = turn.get("status", "")
    else:
        status = payload.get("status", "")
    return [src._make(
        EventType.AGENT_RUN_ACTIVITY,
        {
            "action": "turn_completed",
            "threadId": payload.get("threadId"),
            "turnId": payload.get("turnId"),
            "status": status,
        },
    )]


def _map_item_started(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    item = payload.get("item")
    if not isinstance(item, dict):
        return []
    item_type = item.get("type", "")
    item_id = item.get("id", "")
    if item_type == "commandExecution":
        command = item.get("command", "")
        if isinstance(command, list):
            command = " ".join(str(p) for p in command)
        return [src._make(
            EventType.COMMAND_STARTED,
            {"itemId": item_id, "command": str(command)},
        )]
    return []


def _map_item_completed(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    item = payload.get("item")
    if not isinstance(item, dict):
        return []
    item_type = item.get("type", "")
    item_id = item.get("id", "")
    if item_type == "commandExecution":
        command = item.get("command", "")
        if isinstance(command, list):
            command = " ".join(str(p) for p in command)
        return [src._make(
            EventType.COMMAND_COMPLETED,
            {"itemId": item_id, "command": str(command)},
        )]
    return []


def _map_agent_message_delta(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    delta = payload.get("delta", "")
    item = payload.get("item")
    item_id = ""
    if isinstance(item, dict):
        item_id = item.get("id", "")
    return [src._make(
        EventType.MODEL_TEXT_DELTA,
        {"delta": delta, "itemId": item_id},
        visibility=Visibility.USER,
    )]


def _map_token_usage(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    usage_payload: dict[str, object] = {}
    usage = payload.get("tokenUsage")
    if isinstance(usage, dict):
        last = usage.get("last")
        if isinstance(last, dict):
            usage_payload.update(_safe_int_fields(last))
        total = usage.get("total")
        if isinstance(total, dict):
            usage_payload["total"] = _safe_int_fields(total)
        ctx = usage.get("modelContextWindow")
        if ctx is not None:
            usage_payload["model_context_window"] = ctx
    else:
        # Legacy flat format — only the known numeric fields
        usage_payload.update(_safe_int_fields(payload))
    if not usage_payload:
        return []
    return [src._make(EventType.MODEL_USAGE_UPDATED, usage_payload)]


def _map_command_output_delta(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    return [src._make(
        EventType.COMMAND_OUTPUT_DELTA,
        {
            "delta": payload.get("delta", ""),
            "itemId": payload.get("itemId", ""),
        },
    )]


def _map_file_change_delta(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    return [src._make(
        EventType.FILE_CHANGED,
        {
            "delta": payload.get("delta", ""),
            "itemId": payload.get("itemId", ""),
            "filePath": payload.get("filePath", ""),
        },
        visibility=Visibility.OPERATOR,
    )]


def _map_diff_updated(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    return [src._make(
        EventType.DIFF_UPDATED,
        {
            "diff": payload.get("diff", ""),
            "threadId": payload.get("threadId", ""),
            "turnId": payload.get("turnId", ""),
        },
    )]


def _map_plan_updated(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    return [src._make(
        EventType.AGENT_RUN_ACTIVITY,
        {"action": "plan_updated", "threadId": payload.get("threadId")},
    )]


def _map_approval_requested(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    approval_id = payload.get("codexRequestId", "")
    summary = _build_approval_summary(payload)
    return [src._make(
        EventType.APPROVAL_REQUESTED,
        {
            **payload,
            "summary": summary,
        },
        aggregate_type=AggregateType.APPROVAL,
        aggregate_id=str(approval_id) if approval_id else None,
        visibility=Visibility.USER,
    )]


def _build_approval_summary(payload: dict) -> str:
    existing = str(payload.get("summary", ""))
    if existing:
        return existing
    kind = str(payload.get("kind", ""))
    command = payload.get("command", "")
    if isinstance(command, list):
        command = " ".join(str(p) for p in command)
    command_str = str(command) if command else ""
    reason = str(payload.get("reason", ""))
    if kind == "command" or command_str:
        summary = f"Run: {command_str}".strip() if command_str else "Run command"
        if reason:
            summary = f"{summary}\nReason: {reason}"
        return summary
    if kind == "file_change":
        file_path = str(payload.get("filePath", payload.get("path", "")))
        changed_files = payload.get("changedFiles", payload.get("fileChanges", ""))
        if isinstance(changed_files, dict):
            files = ", ".join(sorted(changed_files.keys())[:6])
            summary = f"Apply patch: {files}" if files else "Apply patch"
        elif isinstance(changed_files, list):
            summary = f"Apply patch: {', '.join(str(f) for f in changed_files[:6])}"
        elif file_path:
            summary = f"Edit file: {file_path}"
        else:
            summary = "Apply patch"
        if reason:
            summary = f"{summary}\nReason: {reason}"
        return summary
    if kind == "permissions":
        return str(payload.get("summary", "Permission request"))
    return command_str or reason or str(payload.get("kind", "Approval required"))


def _map_approval_resolved(
    src: CodexRuntimeSource, payload: dict
) -> list[RuntimeEvent]:
    approval_id = payload.get("codexRequestId", "")
    response = payload.get("response", {})
    return [src._make(
        EventType.APPROVAL_RESOLVED,
        {
            "codexRequestId": approval_id,
            "decision": response.get("decision", response.get("permissions", "")),
            "scope": response.get("scope", ""),
        },
        aggregate_type=AggregateType.APPROVAL,
        aggregate_id=str(approval_id) if approval_id else None,
        visibility=Visibility.USER,
    )]


# ------------------------------------------------------------------
# Handler dispatch table
# ------------------------------------------------------------------

_EVENT_MAP: dict[str, object] = {
    "thread_started": _map_thread_started,
    "thread_status_changed": _map_thread_status_changed,
    "turn_started": _map_turn_started,
    "turn_completed": _map_turn_completed,
    "item_started": _map_item_started,
    "item_completed": _map_item_completed,
    "agent_message_delta": _map_agent_message_delta,
    "token_usage_updated": _map_token_usage,
    "command_output_delta": _map_command_output_delta,
    "file_change_delta": _map_file_change_delta,
    "diff_updated": _map_diff_updated,
    "plan_updated": _map_plan_updated,
    "approval_requested": _map_approval_requested,
    "approval_resolved": _map_approval_resolved,
}


# ------------------------------------------------------------------
# Payload helpers
# ------------------------------------------------------------------

def _safe_int_fields(d: dict, keys: list[str] | None = None) -> dict[str, int]:
    """Extract safely-coerced integer fields from *d*.

    Non-numeric values are silently skipped so that malformed payloads
    never cause a mapping failure.
    """
    if keys is None:
        keys = ["inputTokens", "outputTokens", "cachedInputTokens",
                "reasoningOutputTokens", "totalTokens"]
    snake_names = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
        "totalTokens": "total_tokens",
    }
    result: dict[str, int] = {}
    for key in keys:
        val = d.get(key)
        if val is None:
            continue
        try:
            result[snake_names.get(key, key)] = int(val)
        except (TypeError, ValueError):
            pass
    return result
