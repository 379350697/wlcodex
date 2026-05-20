"""Workbench foundation models.

View modes (Cockpit / Onsite) and execution modes (Orchestrated /
Codex-direct / Claude-direct) are separate dimensions.  A user sees
any execution mode through any view.
"""

from dataclasses import dataclass, field
from enum import Enum


class ViewMode(Enum):
    """User-facing view over the remote workbench."""

    COCKPIT = "cockpit"
    ONSITE = "onsite"


class ExecutionMode(Enum):
    """Who does the work — independent of which view the user sees."""

    ORCHESTRATED = "orchestrated"
    CODEX_DIRECT = "codex_direct"
    CLAUDE_DIRECT = "claude_direct"


class WorkbenchRoute(Enum):
    """Decision produced by plain-text routing."""

    ORCHESTRATED_COCKPIT = "orchestrated_cockpit"
    CODEX_DIRECT_COCKPIT = "codex_direct_cockpit"
    CLAUDE_DIRECT_COCKPIT = "claude_direct_cockpit"
    ONSITE_INPUT = "onsite_input"


@dataclass
class WorkbenchState:
    """Durable shared state behind both views and all execution modes."""

    conversation_id: int
    chat_id: int
    workspace_alias: str

    view: ViewMode = ViewMode.COCKPIT
    execution_mode: ExecutionMode = ExecutionMode.ORCHESTRATED

    active_agent: str = ""
    active_phase: str = "idle"

    codex_thread_id: str = ""
    codex_turn_id: str = ""
    claude_session_id: str = ""

    cockpit_cursor: int = 0
    onsite_cursor: int = 0

    onsite_session_refs: dict = field(default_factory=dict)
    pending_approvals: list = field(default_factory=list)
    latest_diff_summary: str = ""
    pending_user_context: str = ""
    latest_user_visible_message_ids: dict = field(default_factory=dict)
