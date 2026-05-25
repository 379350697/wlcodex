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
STATUS = "status"
NEW_CONVO = "new"
RESTORE_WORKBENCH = "restore_workbench"
WORKBENCH_STATUS = "workbench_status"
WORKBENCH_SESSIONS = "workbench_sessions"

# Workbench carryover callback actions
CARRY_START = "carry_start"
CARRY_SHOW = "carry_show"
CARRY_REFRESH = "carry_refresh"
CARRY_CANCEL = "carry_cancel"

# Staged-auto callback actions (imported from auto_workflow for use in
# callback-data encoding). These are re-exported here so that controller.py
# and telegram_app.py can import them from one location.
AUTO_FINAL_PLAN = "auto_final_plan"
AUTO_SHOW_DRAFT = "auto_show_draft"
AUTO_CANCEL = "auto_cancel"
AUTO_SEND_TO_CLAUDE = "auto_send_to_claude"
AUTO_CONTINUE_CONTEXT = "auto_continue_context"
AUTO_REWRITE_PLAN = "auto_rewrite_plan"
AUTO_CODEX_TAKEOVER = "auto_codex_takeover"
AUTO_CLOSE = "auto_close"
AUTO_CODEX_VERIFY = "auto_codex_verify"
AUTO_SEND_REPAIR_TO_CLAUDE = "auto_send_repair_to_claude"
AUTO_REWRITE_REPAIR = "auto_rewrite_repair"
AUTO_INTERRUPT_CLAUDE = "auto_interrupt_claude"
AUTO_VIEW_DIFF = "auto_view_diff"
AUTO_VIEW_STATUS = "auto_view_status"
AUTO_SEND_TO_CODEX = "auto_send_to_codex"
TEAM_VIEW_STATUS = "team_view_status"
TEAM_VIEW_ARTIFACTS = "team_view_artifacts"

AUTO_CALLBACK_ACTIONS = {
    AUTO_FINAL_PLAN, AUTO_SHOW_DRAFT, AUTO_CANCEL,
    AUTO_SEND_TO_CLAUDE, AUTO_CONTINUE_CONTEXT,
    AUTO_REWRITE_PLAN, AUTO_CODEX_TAKEOVER, AUTO_CLOSE,
    AUTO_CODEX_VERIFY, AUTO_SEND_TO_CODEX, AUTO_SEND_REPAIR_TO_CLAUDE,
    AUTO_REWRITE_REPAIR, AUTO_INTERRUPT_CLAUDE,
    AUTO_VIEW_DIFF, AUTO_VIEW_STATUS,
    TEAM_VIEW_STATUS, TEAM_VIEW_ARTIFACTS,
}


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
