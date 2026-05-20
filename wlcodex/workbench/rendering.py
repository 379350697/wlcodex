"""Workbench rendering helpers.

Cockpit and Onsite views render different projections of the same
WorkbenchState.  This module provides rendering primitives that
produce user-facing text without coupling to Telegram delivery.
"""

from __future__ import annotations

from .models import ViewMode, WorkbenchState
from .sessions import AgentSessionResumability, AgentSessionSummary


def render_view_header(state: WorkbenchState) -> str:
    """Return a concise header for the active view (Cockpit or Onsite)."""
    view_name = "驾驶舱" if state.view is ViewMode.COCKPIT else "现场"
    return f"WLCodex · {view_name}"


def render_view_switch_notice(from_view: ViewMode, to_view: ViewMode) -> str:
    """Return the transition copy displayed to the user on view switch."""
    if from_view is ViewMode.COCKPIT and to_view is ViewMode.ONSITE:
        return "已进入接管现场。"
    if from_view is ViewMode.ONSITE and to_view is ViewMode.COCKPIT:
        return "已回到驾驶舱。现场仍在运行，我会继续用摘要跟进。"
    return "视图已切换。"


def render_session_library(sessions: list[AgentSessionSummary]) -> str:
    """Render a Workbench-level historical session library for the user."""
    if not sessions:
        return "这个工作台还没有历史现场。你可以先让 Codex 分析，或让 Claude 开始执行。"

    lines = ["历史现场", ""]
    for s in sessions:
        agent_label = "Claude 现场" if s.agent == "claude" else "Codex 现场"
        lines.append(f"{agent_label} · {s.title} · {s.user_label}")
    return "\n".join(lines)


_RESUMABILITY_LABELS: dict[AgentSessionResumability, str] = {
    AgentSessionResumability.LIVE: "可接管",
    AgentSessionResumability.RESUMABLE: "可继续",
    AgentSessionResumability.SUMMARY_ONLY: "可回顾",
}
