"""Workbench rendering helpers.

Cockpit and Onsite views render different projections of the same
WorkbenchState.  This module provides rendering primitives that
produce user-facing text without coupling to Telegram delivery.
"""

from .models import ViewMode, WorkbenchState


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
