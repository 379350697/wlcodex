"""Task 1: Workbench Foundation Contracts — test suite.

Tests ViewMode / ExecutionMode separation, WorkbenchRoute decisions,
and plain-text routing through WorkbenchState.
"""

from wlcodex.workbench.models import (
    ExecutionMode,
    ViewMode,
    WorkbenchRoute,
    WorkbenchState,
)
from wlcodex.workbench.routing import route_plain_text
from wlcodex.workbench.rendering import render_view_header, render_view_switch_notice


# ── View / execution mode separation ──────────────────────────────


def test_cockpit_plain_text_defaults_to_orchestrated_mode():
    state = WorkbenchState(
        conversation_id=42,
        chat_id=100,
        workspace_alias="wlcodex",
        view=ViewMode.COCKPIT,
        execution_mode=ExecutionMode.ORCHESTRATED,
        active_agent="",
        active_phase="idle",
    )

    route = route_plain_text(state, "重做终端手机体验")

    assert route is WorkbenchRoute.ORCHESTRATED_COCKPIT


def test_onsite_plain_text_goes_to_selected_live_session():
    state = WorkbenchState(
        conversation_id=42,
        chat_id=100,
        workspace_alias="wlcodex",
        view=ViewMode.ONSITE,
        execution_mode=ExecutionMode.ORCHESTRATED,
        active_agent="claude",
        active_phase="implementation",
    )

    route = route_plain_text(state, "继续修失败测试")

    assert route is WorkbenchRoute.ONSITE_INPUT


def test_execution_mode_does_not_change_view_mode():
    state = WorkbenchState(
        conversation_id=42,
        chat_id=100,
        workspace_alias="wlcodex",
        view=ViewMode.COCKPIT,
        execution_mode=ExecutionMode.CLAUDE_DIRECT,
        active_agent="claude",
        active_phase="implementation",
    )

    assert state.view is ViewMode.COCKPIT
    assert state.execution_mode is ExecutionMode.CLAUDE_DIRECT


# ── Enum value uniqueness ─────────────────────────────────────────


def test_view_mode_values_are_distinct():
    assert ViewMode.COCKPIT.value != ViewMode.ONSITE.value


def test_execution_mode_values_are_distinct():
    values = {m.value for m in ExecutionMode}
    assert len(values) == 3
    assert ExecutionMode.ORCHESTRATED.value == "orchestrated"
    assert ExecutionMode.CODEX_DIRECT.value == "codex_direct"
    assert ExecutionMode.CLAUDE_DIRECT.value == "claude_direct"


def test_workbench_route_values_are_distinct():
    values = {r.value for r in WorkbenchRoute}
    assert len(values) == 4


# ── All Cockpit routes covered ────────────────────────────────────


def test_codex_direct_cockpit_routing():
    state = WorkbenchState(
        conversation_id=1,
        chat_id=1,
        workspace_alias="test",
        view=ViewMode.COCKPIT,
        execution_mode=ExecutionMode.CODEX_DIRECT,
        active_agent="codex",
        active_phase="analysis",
    )

    route = route_plain_text(state, "review this diff")

    assert route is WorkbenchRoute.CODEX_DIRECT_COCKPIT


def test_claude_direct_cockpit_routing():
    state = WorkbenchState(
        conversation_id=1,
        chat_id=1,
        workspace_alias="test",
        view=ViewMode.COCKPIT,
        execution_mode=ExecutionMode.CLAUDE_DIRECT,
        active_agent="claude",
        active_phase="implementation",
    )

    route = route_plain_text(state, "fix the bug in auth")

    assert route is WorkbenchRoute.CLAUDE_DIRECT_COCKPIT


# ── WorkbenchState defaults ──────────────────────────────────────


def test_workbench_state_defaults():
    state = WorkbenchState(
        conversation_id=1,
        chat_id=1,
        workspace_alias="test",
    )

    assert state.view is ViewMode.COCKPIT
    assert state.execution_mode is ExecutionMode.ORCHESTRATED
    assert state.active_agent == ""
    assert state.active_phase == "idle"
    assert state.codex_thread_id == ""
    assert state.codex_turn_id == ""
    assert state.claude_session_id == ""
    assert state.cockpit_cursor == 0
    assert state.onsite_cursor == 0
    assert state.onsite_session_refs == {}
    assert state.pending_approvals == []
    assert state.latest_diff_summary == ""
    assert state.pending_user_context == ""
    assert state.latest_user_visible_message_ids == {}


def test_workbench_state_covers_all_spec_fields():
    """Every field listed in spec §Workbench State must exist on the dataclass."""
    state = WorkbenchState(
        conversation_id=1,
        chat_id=2,
        workspace_alias="test",
    )

    # Spec: "workbench id, mapped to the active conversation id"
    assert hasattr(state, "conversation_id")

    # Spec: "chat id"
    assert hasattr(state, "chat_id")

    # Spec: "workspace alias"
    assert hasattr(state, "workspace_alias")

    # Spec: "active view: cockpit or onsite"
    assert hasattr(state, "view")
    assert isinstance(state.view, ViewMode)

    # Spec: "active execution mode: orchestrated, codex_direct, claude_direct"
    assert hasattr(state, "execution_mode")
    assert isinstance(state.execution_mode, ExecutionMode)

    # Spec: "active phase"
    assert hasattr(state, "active_phase")

    # Spec: "active agent"
    assert hasattr(state, "active_agent")

    # Spec: "active Codex thread id when present"
    assert hasattr(state, "codex_thread_id")

    # Spec: "active Codex turn id when present"
    assert hasattr(state, "codex_turn_id")

    # Spec: "active Claude session id when present"
    assert hasattr(state, "claude_session_id")

    # Spec: "active onsite session references by agent"
    assert hasattr(state, "onsite_session_refs")
    assert isinstance(state.onsite_session_refs, dict)

    # Spec: "cockpit cursor"
    assert hasattr(state, "cockpit_cursor")

    # Spec: "onsite cursor"
    assert hasattr(state, "onsite_cursor")

    # Spec: "latest diff summary"
    assert hasattr(state, "latest_diff_summary")

    # Spec: "pending approvals"
    assert hasattr(state, "pending_approvals")
    assert isinstance(state.pending_approvals, list)

    # Spec: "pending user context"
    assert hasattr(state, "pending_user_context")

    # Spec: "latest user-visible message ids when needed for edits"
    assert hasattr(state, "latest_user_visible_message_ids")
    assert isinstance(state.latest_user_visible_message_ids, dict)


# ── Onsite routing is unconditional on execution mode ──────────────


def test_onsite_input_overrides_execution_mode():
    """When view is ONSITE, plain text routes to ONSITE_INPUT regardless of execution mode."""
    for em in ExecutionMode:
        state = WorkbenchState(
            conversation_id=1,
            chat_id=1,
            workspace_alias="test",
            view=ViewMode.ONSITE,
            execution_mode=em,
            active_agent="claude",
            active_phase="implementation",
        )

        route = route_plain_text(state, "hello")

        assert route is WorkbenchRoute.ONSITE_INPUT, (
            f"ONSITE view with {em} should route ONSITE_INPUT"
        )


# ── Rendering helpers ──────────────────────────────────────────────


def test_render_view_header_in_cockpit_view():
    state = WorkbenchState(
        conversation_id=1, chat_id=1, workspace_alias="test",
        view=ViewMode.COCKPIT,
    )
    header = render_view_header(state)
    assert "驾驶舱" in header
    assert "WLCodex" in header


def test_render_view_header_name_changes_with_view():
    cockpit_state = WorkbenchState(
        conversation_id=1, chat_id=1, workspace_alias="test",
        view=ViewMode.COCKPIT,
    )
    onsite_state = WorkbenchState(
        conversation_id=1, chat_id=1, workspace_alias="test",
        view=ViewMode.ONSITE,
    )

    assert "驾驶舱" in render_view_header(cockpit_state)
    assert "现场" in render_view_header(onsite_state)


def test_render_view_switch_notice_cockpit_to_onsite():
    notice = render_view_switch_notice(ViewMode.COCKPIT, ViewMode.ONSITE)
    assert "接管现场" in notice


def test_render_view_switch_notice_onsite_to_cockpit_matches_spec():
    notice = render_view_switch_notice(ViewMode.ONSITE, ViewMode.COCKPIT)

    # Spec line 353-355 exact copy
    assert "已回到驾驶舱" in notice
    assert "现场仍在运行" in notice
    assert "摘要跟进" in notice
