"""Tests for surface event store replay — Task 2: Surface Event Store And Cursor Projection.

replay_surface_state must be a pure function: no SQLite, no side effects.
"""

import pytest

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    RuntimeEvent,
    Visibility,
    now_iso,
    EventType,
)


def _event(event_type, payload, event_id=1, conv_id=42, chat_id=100, conv_str="42"):
    event = RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.CONVERSATION,
        aggregate_id=conv_str,
        correlation_id="corr",
        source=EventSource.TELEGRAM,
        actor="user",
        visibility=Visibility.OPERATOR,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=conv_id,
    )
    event = event.with_id(event_id)
    return event


# ── Basic replay contract ──────────────────────────────────────────────────

def test_replay_surface_state_importable():
    from wlcodex.surfaces.core.store import replay_surface_state
    assert callable(replay_surface_state)


def test_replay_empty_events_returns_empty_state():
    from wlcodex.surfaces.core.store import replay_surface_state
    state = replay_surface_state([])
    assert state.by_chat == {}


# ── Mode switching ─────────────────────────────────────────────────────────

def test_replay_surface_state_tracks_active_mode_and_cursors():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100,
            "conversation_id": 42,
            "from_mode": "product",
            "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.SURFACE_CURSOR_ADVANCED, {
            "chat_id": 100,
            "conversation_id": 42,
            "surface": "terminal",
            "position": 22,
        }, event_id=11),
    ])

    surface = state.by_chat[100]
    assert surface.active_mode == "terminal"
    assert surface.selected_terminal_agent == "claude"
    assert surface.cursors["terminal"].position == 22


def test_replay_mode_switch_back_to_product():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "terminal", "to_mode": "product",
            "active_agent": "codex",
        }, event_id=10),
    ])

    surface = state.by_chat[100]
    assert surface.active_mode == "product"
    assert surface.selected_terminal_agent == "codex"


def test_replay_mode_switch_defaults_from_mode():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
        }, event_id=10),
    ])

    surface = state.by_chat[100]
    assert surface.active_mode == "terminal"
    assert surface.selected_terminal_agent == ""


# ── Cursor advancement ─────────────────────────────────────────────────────

def test_replay_cursor_advances_independently_per_surface():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.SURFACE_CURSOR_ADVANCED, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "terminal", "position": 5,
        }, event_id=11),
        _event(EventType.SURFACE_CURSOR_ADVANCED, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "product", "position": 120,
        }, event_id=12),
        _event(EventType.SURFACE_CURSOR_ADVANCED, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "terminal", "position": 10,
        }, event_id=13),
    ])

    surface = state.by_chat[100]
    assert surface.cursors["product"].position == 120
    assert surface.cursors["terminal"].position == 10


def test_replay_cursor_only_tracks_max_position():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.SURFACE_CURSOR_ADVANCED, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "terminal", "position": 30,
        }, event_id=11),
        _event(EventType.SURFACE_CURSOR_ADVANCED, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "terminal", "position": 15,
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    assert surface.cursors["terminal"].position == 30


# ── Terminal session lifecycle ─────────────────────────────────────────────

def test_replay_terminal_attach_and_detach():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_DETACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "status": "orphaned",
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    assert surface.active_mode == "terminal"
    assert surface.terminal_sessions["claude"].status == "orphaned"
    assert surface.terminal_sessions["claude"].external_session_id == "claude_1"


def test_replay_terminal_attach_preserves_strategy():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "codex",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "codex",
            "external_session_id": "thr_1",
            "strategy": "app_server",
            "status": "attached",
        }, event_id=11),
    ])

    surface = state.by_chat[100]
    session = surface.terminal_sessions["codex"]
    assert session.status == "attached"
    assert session.strategy == "app_server"
    assert session.external_session_id == "thr_1"


def test_replay_terminal_aborted():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_ABORTED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    assert surface.terminal_sessions["claude"].status == "aborted"


# ── Multi-chat isolation ───────────────────────────────────────────────────

def test_replay_isolates_chats():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 200, "conversation_id": 43,
            "from_mode": "terminal", "to_mode": "product",
            "active_agent": "codex",
        }, event_id=11),
    ])

    assert state.by_chat[100].active_mode == "terminal"
    assert state.by_chat[100].selected_terminal_agent == "claude"
    assert state.by_chat[200].active_mode == "product"
    assert state.by_chat[200].selected_terminal_agent == "codex"


# ── Product display frame advances product cursor ──────────────────────────

def test_replay_product_display_frame_advances_cursor():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.PRODUCT_DISPLAY_FRAME, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "product", "position": 5,
        }, event_id=11),
        _event(EventType.PRODUCT_DISPLAY_FRAME, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "product", "position": 10,
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    assert surface.cursors["product"].position == 10


# ── Terminal output frame advances terminal cursor ─────────────────────────

def test_replay_terminal_output_frame_advances_cursor():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_OUTPUT_FRAME, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "terminal", "position": 7,
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_OUTPUT_FRAME, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "terminal", "position": 8,
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    assert surface.cursors["terminal"].position == 8


# ── Default state for uninitialized cursor ─────────────────────────────────

def test_replay_default_cursor_is_zero():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
    ])

    surface = state.by_chat[100]
    assert surface.cursors["product"].position == 0
    assert surface.cursors["terminal"].position == 0


# ── Pending context recording ──────────────────────────────────────────────

def test_replay_pending_context_recorded():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.PRODUCT_PENDING_CONTEXT_RECORDED, {
            "chat_id": 100, "conversation_id": 42,
            "text_preview": "continue with the fix",
            "telegram_message_id": 555,
        }, event_id=11),
    ])

    surface = state.by_chat[100]
    assert len(surface.pending_context) == 1
    assert surface.pending_context[0]["text_preview"] == "continue with the fix"
    assert surface.pending_context[0]["telegram_message_id"] == 555


# ── Terminal failure does not affect product mode state ────────────────────

def test_replay_terminal_detached_does_not_change_mode():
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_DETACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "status": "orphaned",
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    # Mode should NOT change when terminal detaches
    assert surface.active_mode == "terminal"


# ── Event constants exist in EventType class ───────────────────────────────

def test_surface_event_constants_in_event_type():
    assert EventType.CONVERSATION_MODE_SWITCHED == "conversation.mode.switched"
    assert EventType.SURFACE_CURSOR_ADVANCED == "surface.cursor.advanced"
    assert EventType.TERMINAL_SESSION_ATTACHED == "terminal.session.attached"
    assert EventType.TERMINAL_SESSION_DETACHED == "terminal.session.detached"
    assert EventType.TERMINAL_SESSION_INPUT_SENT == "terminal.session.input.sent"
    assert EventType.TERMINAL_SESSION_OUTPUT_FRAME == "terminal.session.output.frame"
    assert EventType.PRODUCT_DISPLAY_FRAME == "product.display.frame"
    assert EventType.PRODUCT_PENDING_CONTEXT_RECORDED == "product.pending_context.recorded"


# ── New aggregate type for surface ─────────────────────────────────────────

def test_surface_is_valid_aggregate_type():
    assert AggregateType.SURFACE_SESSION == "surface_session"
