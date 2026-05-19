"""Tests for wlcodex.surfaces.product — speaker labels, diff hiding, routing."""

import pytest

from wlcodex.surfaces.product.events import ProductDisplayEvent
from wlcodex.surfaces.product.speaker import product_speaker_line
from wlcodex.surfaces.product.router import (
    ProductRouteDecision,
    product_route_guard,
)


# ── ProductDisplayEvent ────────────────────────────────────────────────────

def test_product_display_event_has_required_fields():
    event = ProductDisplayEvent(
        agent="codex",
        phase="analysis",
        text="我开始分析这个需求。",
    )
    assert event.agent == "codex"
    assert event.phase == "analysis"
    assert event.text == "我开始分析这个需求。"
    assert event.raw_kind is None


def test_product_display_event_defaults():
    event = ProductDisplayEvent(
        agent="claude",
        phase="implementation",
        text="开始实现。",
    )
    assert event.raw_kind is None


def test_product_display_event_with_raw_kind():
    event = ProductDisplayEvent(
        agent="codex",
        phase="verification",
        text="diff --git a/secret.py b/secret.py\n+TOKEN=abc",
        raw_kind="diff",
    )
    assert event.raw_kind == "diff"


def test_product_display_event_with_diff_kind():
    event = ProductDisplayEvent(
        agent="codex",
        phase="implementation",
        text="+TOKEN=abc\n-SECRET=xyz",
        raw_kind="diff",
    )
    assert event.raw_kind == "diff"


def test_product_display_event_with_tool_output_kind():
    event = ProductDisplayEvent(
        agent="claude",
        phase="implementation",
        text="pytest output: 3 passed, 1 failed...",
        raw_kind="tool_output",
    )
    assert event.raw_kind == "tool_output"


def test_product_display_event_allows_system_agent():
    event = ProductDisplayEvent(
        agent="system",
        phase="idle",
        text="conversation started.",
    )
    assert event.agent == "system"


def test_product_display_event_allows_user_agent():
    event = ProductDisplayEvent(
        agent="user",
        phase="idle",
        text="帮我实现这个功能。",
    )
    assert event.agent == "user"


# ── product_speaker_line: speaker labels ───────────────────────────────────

def test_codex_analysis_line_has_speaker_label():
    event = ProductDisplayEvent(
        agent="codex",
        phase="analysis",
        text="我开始分析这个需求。",
    )
    assert product_speaker_line(event) == "codex: 我开始分析这个需求。"


def test_claude_implementation_line_has_speaker_label():
    event = ProductDisplayEvent(
        agent="claude",
        phase="implementation",
        text="现在开始实现。",
    )
    assert product_speaker_line(event) == "claude: 现在开始实现。"


def test_codex_verification_line_has_speaker_label():
    event = ProductDisplayEvent(
        agent="codex",
        phase="verification",
        text="验收通过。改动集中在 3 个文件。",
    )
    assert product_speaker_line(event) == "codex: 验收通过。改动集中在 3 个文件。"


def test_system_agent_line_has_speaker_label():
    event = ProductDisplayEvent(
        agent="system",
        phase="idle",
        text="对话已启动。",
    )
    assert product_speaker_line(event) == "system: 对话已启动。"


# ── product_speaker_line: diff hiding ──────────────────────────────────────

def test_product_line_hides_raw_diff_by_default():
    event = ProductDisplayEvent(
        agent="codex",
        phase="verification",
        text="diff --git a/secret.py b/secret.py\n+TOKEN=abc",
        raw_kind="diff",
    )
    assert product_speaker_line(event) == "codex: 代码改动已记录，可点 查看 diff。"


def test_product_line_hides_claude_diff():
    event = ProductDisplayEvent(
        agent="claude",
        phase="implementation",
        text="+import os\n+os.environ['KEY'] = 'secret'",
        raw_kind="diff",
    )
    assert product_speaker_line(event) == "claude: 代码改动已记录，可点 查看 diff。"


def test_product_line_diff_hiding_ignores_raw_text():
    """When raw_kind=diff, the original text is completely suppressed."""
    event = ProductDisplayEvent(
        agent="codex",
        phase="verification",
        text="SUPER_SECRET_PASSWORD=hunter2",
        raw_kind="diff",
    )
    line = product_speaker_line(event)
    assert "SUPER_SECRET_PASSWORD" not in line
    assert "hunter2" not in line
    assert "查看 diff" in line


# ── product_speaker_line: tool_output hiding ───────────────────────────────

def test_product_line_hides_raw_tool_output():
    event = ProductDisplayEvent(
        agent="claude",
        phase="implementation",
        text="Running: pytest -q\n=============================\n3 passed\n1 failed\n...",
        raw_kind="tool_output",
    )
    assert product_speaker_line(event) == "claude: 工具输出已记录，可点 日志。"


def test_product_line_tool_output_hiding_ignores_raw_text():
    event = ProductDisplayEvent(
        agent="codex",
        phase="verification",
        text="error traceback with file paths and secrets...",
        raw_kind="tool_output",
    )
    line = product_speaker_line(event)
    assert "traceback" not in line
    assert "secrets" not in line
    assert "日志" in line


# ── product_speaker_line: edge cases ───────────────────────────────────────

def test_product_speaker_line_empty_text():
    event = ProductDisplayEvent(
        agent="codex",
        phase="analysis",
        text="",
    )
    assert product_speaker_line(event) == "codex: "


def test_product_speaker_line_unknown_raw_kind_passes_through():
    """Unknown raw_kind should fall through to ordinary text rendering."""
    event = ProductDisplayEvent(
        agent="claude",
        phase="implementation",
        text="some raw data",
        raw_kind="unknown_type",
    )
    assert product_speaker_line(event) == "claude: some raw data"


# ── ProductRouteDecision ───────────────────────────────────────────────────

def test_product_route_decision_values():
    assert ProductRouteDecision.CONTINUE_CONVERSATION.value == "continue_conversation"
    assert ProductRouteDecision.RECORD_PENDING_CONTEXT.value == "record_pending_context"


# ── product_route_guard ────────────────────────────────────────────────────

def test_analysis_phase_continues_conversation():
    decision = product_route_guard(phase="analysis")
    assert decision == ProductRouteDecision.CONTINUE_CONVERSATION


def test_idle_phase_continues_conversation():
    decision = product_route_guard(phase="idle")
    assert decision == ProductRouteDecision.CONTINUE_CONVERSATION


def test_completed_phase_continues_conversation():
    decision = product_route_guard(phase="completed")
    assert decision == ProductRouteDecision.CONTINUE_CONVERSATION


def test_implementation_phase_records_pending_context():
    decision = product_route_guard(phase="implementation")
    assert decision == ProductRouteDecision.RECORD_PENDING_CONTEXT


def test_verification_phase_records_pending_context():
    decision = product_route_guard(phase="verification")
    assert decision == ProductRouteDecision.RECORD_PENDING_CONTEXT


def test_unknown_phase_continues_conversation():
    decision = product_route_guard(phase="some_unknown_phase")
    assert decision == ProductRouteDecision.CONTINUE_CONVERSATION


# ── Cross-surface isolation ────────────────────────────────────────────────

def test_product_surface_does_not_import_terminal():
    """Product Surface must not import from Terminal Surface."""
    with pytest.raises(ImportError):
        from wlcodex.surfaces.product import terminal  # noqa: F811
