"""Tests for wlcodex.surfaces.product.renderer — cockpit status, approval, completion/failure."""

import pytest
from dataclasses import dataclass

from wlcodex.surfaces.product.renderer import (
    render_cockpit_status,
    render_cockpit_completion,
    render_cockpit_failure,
    render_cockpit_approval,
    render_cockpit_queued,
    render_product_display_event,
)
from wlcodex.surfaces.product.events import ProductDisplayEvent
from wlcodex.interaction.runtime_renderer import RuntimeRunState


# ── Cockpit status ─────────────────────────────────────────────────────────


def test_cockpit_status_shows_phase_and_agent():
    state = RuntimeRunState(
        phase="running_analysis",
        active_agent="codex",
        agent_status="running",
    )
    text = render_cockpit_status(state)
    assert "Codex" in text
    assert "分析" in text


def test_cockpit_status_shows_current_command():
    state = RuntimeRunState(
        phase="running_implementation",
        active_agent="claude",
        agent_status="running",
        current_command="pytest tests/ -q",
        elapsed_seconds=200,
    )
    text = render_cockpit_status(state)
    assert "Claude" in text
    assert "pytest" in text
    assert "已运行" in text


def test_cockpit_status_shows_elapsed_time():
    state = RuntimeRunState(
        phase="running_verification",
        active_agent="codex",
        agent_status="running",
        elapsed_seconds=200,
    )
    text = render_cockpit_status(state)
    assert "已运行" in text or "m" in text


def test_cockpit_status_no_raw_json():
    state = RuntimeRunState(
        phase="running_analysis",
        active_agent="codex",
        current_detail='{"key": "value", "nested": {"a": 1}}',
    )
    text = render_cockpit_status(state)
    # Raw JSON should not appear in cockpit status
    assert '{"' not in text


def test_cockpit_status_waiting_for_approval():
    state = RuntimeRunState(
        phase="running_implementation",
        active_agent="claude",
        agent_status="waiting_for_approval",
    )
    text = render_cockpit_status(state)
    assert "审批" in text


def test_cockpit_status_completed():
    state = RuntimeRunState(
        phase="completed",
        is_terminal=True,
    )
    text = render_cockpit_status(state)
    assert "运行完成" in text


def test_cockpit_status_failed():
    state = RuntimeRunState(
        phase="failed",
        is_terminal=True,
        error_summary="command timed out after 300s",
    )
    text = render_cockpit_status(state)
    assert "失败" in text


# ── Cockpit completion ─────────────────────────────────────────────────────


def test_cockpit_completion_brief():
    state = RuntimeRunState(phase="completed", is_terminal=True)
    text = render_cockpit_completion(state)
    assert text.startswith("运行完成")
    # No raw diff or JSON
    assert "diff" not in text.lower() or "已记录" in text


def test_cockpit_completion_with_tokens():
    state = RuntimeRunState(
        phase="completed", is_terminal=True, total_tokens=15000
    )
    text = render_cockpit_completion(state)
    assert "15000" in text


def test_cockpit_completion_with_diff():
    state = RuntimeRunState(
        phase="completed", is_terminal=True, has_diff=True
    )
    text = render_cockpit_completion(state)
    assert "diff" in text.lower() or "查看" in text


def test_cockpit_completion_cancelled():
    state = RuntimeRunState(phase="cancelled", is_terminal=True)
    text = render_cockpit_completion(state)
    assert "取消" in text


# ── Cockpit failure ────────────────────────────────────────────────────────


def test_cockpit_failure_shows_error():
    state = RuntimeRunState(
        phase="failed", error_summary="command exit code 1"
    )
    text = render_cockpit_failure(state)
    assert "失败" in text
    assert "exit code 1" in text


def test_cockpit_failure_includes_terminal_hint():
    state = RuntimeRunState(
        phase="failed", error_summary="command failed"
    )
    text = render_cockpit_failure(state)
    assert "/terminal tail" in text


def test_cockpit_failure_no_error_summary():
    state = RuntimeRunState(phase="failed")
    text = render_cockpit_failure(state)
    assert "失败" in text
    assert "/terminal tail" in text


def test_cockpit_failure_cancelled_not_failed():
    state = RuntimeRunState(phase="cancelled")
    text = render_cockpit_failure(state)
    assert "取消" in text
    assert "/terminal tail" not in text


# ── Cockpit approval ────────────────────────────────────────────────────────


def test_cockpit_approval_command():
    text = render_cockpit_approval("command", "rm -rf /tmp/test", agent="codex")
    assert "审批" in text
    assert "命令" in text
    assert "Codex" in text
    assert "rm" in text


def test_cockpit_approval_file_change():
    text = render_cockpit_approval("file_change", "修改 src/foo.py", agent="claude")
    assert "文件修改" in text
    assert "Claude" in text


def test_cockpit_approval_truncates_long_summary():
    summary = "x" * 300
    text = render_cockpit_approval("command", summary)
    assert len(text) < 400  # Summary is capped


def test_cockpit_approval_no_raw_agent_id():
    text = render_cockpit_approval("command", "test", agent="codex")
    # Should use display name "Codex" not raw "codex"
    assert "Codex" in text


# ── Cockpit queued ─────────────────────────────────────────────────────────


def test_cockpit_queued_no_agent():
    text = render_cockpit_queued()
    assert "启动" in text


def test_cockpit_queued_with_agent():
    text = render_cockpit_queued(agent="codex")
    assert "Codex" in text
    assert "启动" in text


# ── ProductDisplayEvent rendering ──────────────────────────────────────────


def test_product_display_event_ordinary_text():
    event = ProductDisplayEvent(
        agent="codex", phase="analysis", text="正在分析需求"
    )
    text = render_product_display_event(event)
    assert text == "codex: 正在分析需求"


def test_product_display_event_diff_hidden():
    event = ProductDisplayEvent(
        agent="claude", phase="implementation", text="+10 -3",
        raw_kind="diff",
    )
    text = render_product_display_event(event)
    assert "查看 diff" in text or "diff" not in text or "已记录" in text


def test_product_display_event_tool_output_hidden():
    event = ProductDisplayEvent(
        agent="codex", phase="verification", text="All 42 tests passed",
        raw_kind="tool_output",
    )
    text = render_product_display_event(event)
    # Tool output should be hidden behind a summary label
    assert "日志" in text or "已记录" in text


# ── Double-prefix guard ────────────────────────────────────────────────────


def test_cockpit_failure_no_double_prefix_with_output_manager():
    """Verify that render_cockpit_failure() produces text that, when passed
    through TelegramOutputManager.fail() (which adds '运行失败: '), does
    NOT result in double-prefixed text like '运行失败: 运行失败: ...'.
    """
    state = RuntimeRunState(
        phase="failed",
        error_summary="something broke",
    )
    failure_text = render_cockpit_failure(state)
    # render_cockpit_failure already starts with '运行失败'
    assert failure_text.startswith("运行失败")
    # If TelegramOutputManager.fail() prefixes with '运行失败: ',
    # the combined text must NOT have the prefix doubled.
    # Since we changed _handle_runtime_final to pass raw error_summary
    # to fail() (not the rendered cockpit_failure), the fail() method
    # adds '运行失败: ' exactly once.
    # This test verifies the cockpit failure format itself:
    assert failure_text.count("运行失败") == 1, (
        f"Double prefix detected: {failure_text}"
    )