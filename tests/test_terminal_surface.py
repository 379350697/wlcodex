"""Tests for wlcodex.surfaces.terminal — models, renderer, manager, router, adapters."""

import pytest

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.renderer import render_terminal_frame


# ── Task 4: Models ─────────────────────────────────────────────────────────

def test_terminal_frame_renders_agent_phase_prefix():
    frame = TerminalFrame(
        conversation_id=42,
        agent="claude",
        phase="implementation",
        text="Running pytest -q",
        frame_kind="stdout",
        sequence=7,
    )
    rendered = render_terminal_frame(frame)
    assert rendered == "[claude:implementation] Running pytest -q"


def test_terminal_session_ref_keeps_strategy():
    ref = TerminalSessionRef(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
        status="attached",
    )
    assert ref.strategy == "stream_json"
    assert ref.status == "attached"


def test_terminal_frame_all_fields_preserved():
    frame = TerminalFrame(
        conversation_id=42,
        agent="codex",
        phase="analysis",
        text="diff --git a/x.py b/x.py\n+12 -3",
        frame_kind="diff",
        sequence=15,
    )
    assert frame.conversation_id == 42
    assert frame.agent == "codex"
    assert frame.phase == "analysis"
    assert frame.text == "diff --git a/x.py b/x.py\n+12 -3"
    assert frame.frame_kind == "diff"
    assert frame.sequence == 15


def test_terminal_session_ref_has_required_fields():
    ref = TerminalSessionRef(
        conversation_id=1,
        agent="codex",
        strategy="app_server",
        external_session_id="thr_abc",
        status="attached",
    )
    assert ref.conversation_id == 1
    assert ref.agent == "codex"
    assert ref.strategy == "app_server"
    assert ref.external_session_id == "thr_abc"
    assert ref.status == "attached"


def test_terminal_session_ref_default_status_is_detached():
    ref = TerminalSessionRef(
        conversation_id=7,
        agent="claude",
        strategy="stream_json",
        external_session_id="sess_xyz",
    )
    assert ref.status == "detached"


def test_terminal_frame_default_frame_kind_is_stdout():
    frame = TerminalFrame(
        conversation_id=1,
        agent="claude",
        phase="planning",
        text="hello world",
        sequence=1,
    )
    assert frame.frame_kind == "stdout"


def test_terminal_frame_default_sequence_is_zero():
    frame = TerminalFrame(
        conversation_id=1,
        agent="claude",
        phase="planning",
        text="first",
    )
    assert frame.sequence == 0


def test_render_terminal_frame_includes_text_verbatim():
    frame = TerminalFrame(
        conversation_id=1,
        agent="system",
        phase="startup",
        text="Session ready",
        frame_kind="system",
        sequence=0,
    )
    rendered = render_terminal_frame(frame)
    assert rendered.endswith(" Session ready")
    assert rendered.startswith("[system:startup]")
