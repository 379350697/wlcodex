"""Conversation callback protocol — inline button actions for Workbench UX.

These handle post-completion actions like "查看 diff", "Codex 验收",
"继续修改".
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIX = "conv"

# Allowed conversation actions
DIFF = "diff"
VERIFY = "verify"
RETRY = "retry"
CONTINUE = "continue"
NEW_CONVO = "new"
RESTORE_WORKBENCH = "restore_workbench"
WORKBENCH_STATUS = "workbench_status"
WORKBENCH_SESSIONS = "workbench_sessions"


@dataclass(frozen=True)
class ConversationCallback:
    conversation_id: int
    action: str


def encode_conversation_callback(conversation_id: int, action: str) -> str:
    """Encode a conversation button callback as 'conv:{id}:{action}'."""
    return f"{PREFIX}:{conversation_id}:{action}"


def decode_conversation_callback(data: str) -> ConversationCallback | None:
    """Decode 'conv:{id}:{action}' into a ConversationCallback."""
    if not data.startswith(f"{PREFIX}:"):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        conversation_id = int(parts[1])
    except ValueError:
        return None
    return ConversationCallback(
        conversation_id=conversation_id,
        action=parts[2],
    )
