from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wlcodex.runtime_events import EventType, RuntimeEvent


_KIND_BY_EVENT_TYPE = {
    EventType.AGENT_RUN_STARTED: "lifecycle",
    EventType.AGENT_RUN_HEARTBEAT: "lifecycle",
    EventType.AGENT_RUN_ACTIVITY: "activity",
    EventType.USER_MESSAGE_RECEIVED: "user_message",
    EventType.PROVIDER_RAW_FRAME: "provider_raw_frame",
    EventType.PROVIDER_DISPLAY_DELTA: "text_delta",
    EventType.PROVIDER_DISPLAY_COMPLETED: "message_completed",
    EventType.PROVIDER_SEMANTIC_USAGE_UPDATED: "usage_updated",
    EventType.PROVIDER_SEMANTIC_TOOL_CALL_STARTED: "tool_call_started",
    EventType.PROVIDER_SEMANTIC_STATUS: "lifecycle",
    EventType.MODEL_TEXT_DELTA: "text_delta",
    EventType.MODEL_MESSAGE_COMPLETED: "message_completed",
    EventType.MODEL_REASONING_DELTA: "reasoning_delta",
    EventType.MODEL_USAGE_UPDATED: "usage_updated",
    EventType.TOOL_CALL_STARTED: "tool_call_started",
    EventType.TOOL_CALL_PROGRESS: "tool_call_progress",
    EventType.TOOL_CALL_COMPLETED: "tool_call_completed",
    EventType.TOOL_CALL_FAILED: "tool_call_failed",
    EventType.COMMAND_STARTED: "command_started",
    EventType.COMMAND_OUTPUT_DELTA: "command_output",
    EventType.COMMAND_COMPLETED: "command_completed",
    EventType.COMMAND_FAILED: "command_failed",
    EventType.FILE_CHANGED: "file_changed",
    EventType.DIFF_UPDATED: "diff_updated",
    EventType.APPROVAL_REQUESTED: "approval_requested",
    EventType.APPROVAL_RESOLVED: "approval_resolved",
    EventType.AGENT_RUN_COMPLETED: "completed",
    EventType.AGENT_RUN_FAILED: "failed",
}


@dataclass(frozen=True)
class WorkerStreamEvent:
    id: int
    type: str
    kind: str
    agent_run_id: int | None
    conversation_id: int | None
    occurred_at: str
    source: str
    actor: str
    visibility: str
    payload: dict[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "kind": self.kind,
            "agent_run_id": self.agent_run_id,
            "conversation_id": self.conversation_id,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "actor": self.actor,
            "visibility": self.visibility,
            "payload": self.payload,
        }


def stream_event_from_runtime(event: RuntimeEvent) -> WorkerStreamEvent:
    return WorkerStreamEvent(
        id=event.id,
        type=event.event_type,
        kind=(
            "compatibility_event"
            if event.payload.get("compatibility_projection")
            else _KIND_BY_EVENT_TYPE.get(event.event_type, "event")
        ),
        agent_run_id=event.agent_run_id,
        conversation_id=event.conversation_id,
        occurred_at=event.occurred_at,
        source=str(event.source),
        actor=str(event.actor),
        visibility=str(event.visibility),
        payload=dict(event.payload),
    )
