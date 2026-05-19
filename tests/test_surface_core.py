"""Tests for wlcodex.surfaces.core — mode, cursor, checkpoint, and routing."""

import pytest

from wlcodex.surfaces.core.models import (
    ModeSwitchCheckpoint,
    SurfaceCursor,
    SurfaceMode,
    SurfaceRouteDecision,
)
from wlcodex.surfaces.core.router import route_text_by_mode
from wlcodex.surfaces.core.events import (
    CONVERSATION_MODE_SWITCHED,
    SURFACE_CURSOR_ADVANCED,
    TERMINAL_SESSION_ATTACHED,
    TERMINAL_SESSION_DETACHED,
    TERMINAL_SESSION_INPUT_SENT,
    TERMINAL_SESSION_OUTPUT_FRAME,
    TERMINAL_SESSION_ABORTED,
    PRODUCT_DISPLAY_FRAME,
    PRODUCT_PENDING_CONTEXT_RECORDED,
    SURFACE_DELIVERY_SENT,
    SURFACE_DELIVERY_EDITED,
    SURFACE_DELIVERY_FAILED,
)


# ── SurfaceMode ──────────────────────────────────────────────────────────

def test_surface_mode_has_product_and_terminal():
    assert SurfaceMode.PRODUCT is not SurfaceMode.TERMINAL
    assert SurfaceMode.PRODUCT.value == "product"
    assert SurfaceMode.TERMINAL.value == "terminal"


def test_surface_mode_is_immutable():
    with pytest.raises(AttributeError):
        SurfaceMode.PRODUCT.value = "other"  # type: ignore[misc]


# ── SurfaceCursor ────────────────────────────────────────────────────────

def test_surface_cursor_defaults():
    c = SurfaceCursor(surface="product")
    assert c.surface == "product"
    assert c.position == 0


def test_surface_cursor_can_set_position():
    c = SurfaceCursor(surface="terminal", position=42)
    assert c.position == 42


def test_surface_cursor_is_hashable():
    c = SurfaceCursor(surface="product", position=10)
    assert hash(c) == hash(SurfaceCursor(surface="product", position=10))


# ── ModeSwitchCheckpoint ─────────────────────────────────────────────────

def test_mode_switch_checkpoint_preserves_external_sessions():
    checkpoint = ModeSwitchCheckpoint(
        conversation_id=42,
        chat_id=100,
        from_mode=SurfaceMode.PRODUCT,
        to_mode=SurfaceMode.TERMINAL,
        active_agent="claude",
        active_phase="implementation",
        workspace_alias="wlcodex",
        codex_thread_id="thr_1",
        codex_turn_id="turn_1",
        claude_session_id="claude_1",
        product_cursor=SurfaceCursor(surface="product", position=10),
        terminal_cursor=SurfaceCursor(surface="terminal", position=3),
    )

    assert checkpoint.to_mode is SurfaceMode.TERMINAL
    assert checkpoint.claude_session_id == "claude_1"
    assert checkpoint.product_cursor.position == 10
    assert checkpoint.terminal_cursor.position == 3


def test_mode_switch_checkpoint_has_all_required_fields():
    cp = ModeSwitchCheckpoint(
        conversation_id=1,
        chat_id=2,
        from_mode=SurfaceMode.PRODUCT,
        to_mode=SurfaceMode.TERMINAL,
        active_agent="codex",
        active_phase="analysis",
        workspace_alias="test",
        codex_thread_id="thr_x",
        codex_turn_id="turn_x",
        claude_session_id="sess_x",
        product_cursor=SurfaceCursor(surface="product", position=0),
        terminal_cursor=SurfaceCursor(surface="terminal", position=0),
    )
    assert cp.conversation_id == 1
    assert cp.chat_id == 2
    assert cp.from_mode is SurfaceMode.PRODUCT
    assert cp.to_mode is SurfaceMode.TERMINAL
    assert cp.active_agent == "codex"
    assert cp.active_phase == "analysis"
    assert cp.workspace_alias == "test"
    assert cp.codex_thread_id == "thr_x"
    assert cp.codex_turn_id == "turn_x"
    assert cp.claude_session_id == "sess_x"


# ── SurfaceRouteDecision ─────────────────────────────────────────────────

def test_surface_route_decision_values():
    assert SurfaceRouteDecision.PRODUCT_CONVERSATION.value == "product_conversation"
    assert SurfaceRouteDecision.TERMINAL_INPUT.value == "terminal_input"


# ── route_text_by_mode ───────────────────────────────────────────────────

def test_product_mode_routes_text_to_product_controller():
    decision = route_text_by_mode(
        mode=SurfaceMode.PRODUCT,
        text="继续刚才的修改",
        selected_terminal_agent="claude",
    )
    assert decision == SurfaceRouteDecision.PRODUCT_CONVERSATION


def test_terminal_mode_routes_text_to_terminal_session():
    decision = route_text_by_mode(
        mode=SurfaceMode.TERMINAL,
        text="pytest -q",
        selected_terminal_agent="claude",
    )
    assert decision == SurfaceRouteDecision.TERMINAL_INPUT


def test_terminal_route_works_regardless_of_agent():
    for agent in ("claude", "codex"):
        decision = route_text_by_mode(
            mode=SurfaceMode.TERMINAL,
            text="ls",
            selected_terminal_agent=agent,
        )
        assert decision == SurfaceRouteDecision.TERMINAL_INPUT


def test_product_route_ignores_terminal_agent_param():
    decision = route_text_by_mode(
        mode=SurfaceMode.PRODUCT,
        text="hello",
        selected_terminal_agent="codex",
    )
    assert decision == SurfaceRouteDecision.PRODUCT_CONVERSATION


# ── Event constants ──────────────────────────────────────────────────────

def test_event_constants_are_strings():
    assert isinstance(CONVERSATION_MODE_SWITCHED, str)
    assert isinstance(SURFACE_CURSOR_ADVANCED, str)
    assert isinstance(TERMINAL_SESSION_ATTACHED, str)
    assert isinstance(TERMINAL_SESSION_DETACHED, str)
    assert isinstance(TERMINAL_SESSION_INPUT_SENT, str)
    assert isinstance(TERMINAL_SESSION_OUTPUT_FRAME, str)
    assert isinstance(TERMINAL_SESSION_ABORTED, str)
    assert isinstance(PRODUCT_DISPLAY_FRAME, str)
    assert isinstance(PRODUCT_PENDING_CONTEXT_RECORDED, str)
    assert isinstance(SURFACE_DELIVERY_SENT, str)
    assert isinstance(SURFACE_DELIVERY_EDITED, str)
    assert isinstance(SURFACE_DELIVERY_FAILED, str)


def test_event_constants_match_design_spec():
    assert CONVERSATION_MODE_SWITCHED == "conversation.mode.switched"
    assert SURFACE_CURSOR_ADVANCED == "surface.cursor.advanced"
    assert TERMINAL_SESSION_ATTACHED == "terminal.session.attached"
    assert TERMINAL_SESSION_DETACHED == "terminal.session.detached"
    assert TERMINAL_SESSION_INPUT_SENT == "terminal.session.input.sent"
    assert TERMINAL_SESSION_OUTPUT_FRAME == "terminal.session.output.frame"
    assert TERMINAL_SESSION_ABORTED == "terminal.session.aborted"
    assert PRODUCT_DISPLAY_FRAME == "product.display.frame"
    assert PRODUCT_PENDING_CONTEXT_RECORDED == "product.pending_context.recorded"
    assert SURFACE_DELIVERY_SENT == "surface.delivery.sent"
    assert SURFACE_DELIVERY_EDITED == "surface.delivery.edited"
    assert SURFACE_DELIVERY_FAILED == "surface.delivery.failed"
