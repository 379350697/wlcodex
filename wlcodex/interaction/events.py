from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

InteractionEventType = Literal[
    "run_started",
    "text_delta",
    "tool_activity",
    "approval_requested",
    "run_completed",
    "run_failed",
    "status_refresh",
    "runtime_progress",
    "runtime_heartbeat",
    "runtime_final",
]


@dataclass
class InteractionEvent:
    event_type: InteractionEventType
    chat_id: int
    conversation_id: int | None = None
    task_id: int | None = None
    thread_id: str = ""
    text: str = ""
    summary: str = ""
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
