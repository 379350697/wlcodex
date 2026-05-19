"""Tests for Task 8: Restart Recovery And Reattach Semantics.

Recovery replays runtime events as source of truth.  Terminal orphan/detach
must not affect product cursor or product active usability.
"""

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


# ── Replay handles terminal.session.aborted ───────────────────────────────

def test_replay_terminal_aborted_preserves_status():
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
            "external_session_id": "claude_abc",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_ABORTED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_abc",
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    assert surface.terminal_sessions["claude"].status == "aborted"


# ── Recovery: terminal detached/orphaned does not change product mode ─────

def test_terminal_orphaned_does_not_change_active_mode():
    """Terminal detach must only affect Terminal Surface, not active_mode."""
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
    # active_mode unchanged by terminal detach
    assert surface.active_mode == "terminal"
    # terminal session status reflects orphan
    assert surface.terminal_sessions["claude"].status == "orphaned"


def test_terminal_detach_does_not_modify_product_cursor():
    """Terminal detach must NOT modify product cursor position."""
    from wlcodex.surfaces.core.store import replay_surface_state

    state = replay_surface_state([
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.PRODUCT_DISPLAY_FRAME, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "product", "position": 15,
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=12),
        _event(EventType.TERMINAL_SESSION_DETACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "status": "orphaned",
        }, event_id=13),
    ])

    surface = state.by_chat[100]
    # Product cursor must be preserved unchanged
    assert surface.cursors["product"].position == 15


def test_active_mode_replayable_after_terminal_orphan():
    """After terminal orphan, active_mode must still be replayable from events."""
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
            "external_session_id": "claude_ses_1",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
        _event(EventType.SURFACE_CURSOR_ADVANCED, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "terminal", "position": 8,
        }, event_id=12),
        _event(EventType.TERMINAL_SESSION_DETACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_ses_1",
            "status": "orphaned",
        }, event_id=13),
        # Switch back to product after terminal orphan
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "terminal", "to_mode": "product",
            "active_agent": "codex",
        }, event_id=14),
    ])

    surface = state.by_chat[100]
    # Product mode is active after switch-back
    assert surface.active_mode == "product"
    # Terminal session still shows orphaned status
    assert surface.terminal_sessions["claude"].status == "orphaned"
    # Product cursor still independent
    assert surface.cursors["product"].surface == "product"


# ── RecoveryManager startup policy ────────────────────────────────────────

class FakeProcessChecker:
    """Pluggable process checker for testing recovery without real pid checks."""

    def __init__(self, alive_sessions: set | None = None):
        self._alive = alive_sessions or set()

    def is_process_alive(self, external_session_id: str) -> bool:
        return external_session_id in self._alive


def test_recovery_startup_attached_process_alive_stays_attached():
    """Attached session + process alive -> status stays attached."""
    from wlcodex.recovery import RecoveryManager

    events = [
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_live_1",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
    ]

    checker = FakeProcessChecker(alive_sessions={"claude_live_1"})
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    surface = state.by_chat[100]
    # Process is alive -> status attached (no new detached events)
    assert surface.terminal_sessions["claude"].status == "attached"
    assert surface.active_mode == "terminal"
    # No new orphan events appended
    detached_events = [e for e in new_events
                       if e.event_type == EventType.TERMINAL_SESSION_DETACHED]
    assert len(detached_events) == 0


def test_recovery_startup_attached_process_missing_append_orphaned():
    """Attached session + process missing -> append detached/orphaned event."""
    from wlcodex.recovery import RecoveryManager

    events = [
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_dead_1",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
    ]

    checker = FakeProcessChecker(alive_sessions=set())  # nothing alive
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    # Should have appended a detached/orphaned event
    detached_events = [e for e in new_events
                       if e.event_type == EventType.TERMINAL_SESSION_DETACHED]
    assert len(detached_events) == 1
    orphan_event = detached_events[0]
    assert orphan_event.payload["agent"] == "claude"
    assert orphan_event.payload["external_session_id"] == "claude_dead_1"
    assert orphan_event.payload["status"] == "orphaned"

    # After applying the new orphan event, state reflects it
    all_events = events + new_events
    from wlcodex.surfaces.core.store import replay_surface_state
    final_state = replay_surface_state(all_events)
    surface = final_state.by_chat[100]
    assert surface.terminal_sessions["claude"].status == "orphaned"


def test_recovery_product_mode_usable_when_terminal_orphaned():
    """Product mode active -> keep product mode usable even if terminal orphaned."""
    from wlcodex.recovery import RecoveryManager

    events = [
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "terminal", "to_mode": "product",
            "active_agent": "codex",
        }, event_id=10),
        _event(EventType.PRODUCT_DISPLAY_FRAME, {
            "chat_id": 100, "conversation_id": 42,
            "surface": "product", "position": 25,
        }, event_id=11),
        # Terminal was attached earlier and is now orphaned after restart
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_dead_2",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=12),
    ]

    checker = FakeProcessChecker(alive_sessions=set())
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    surface = state.by_chat[100]
    # Product mode is the active mode
    assert surface.active_mode == "product"
    # Product cursor is intact
    assert surface.cursors["product"].position == 25
    # Terminal session marked as attached in initial replay (before new events)
    # but the new orphan event was appended
    detached_events = [e for e in new_events
                       if e.event_type == EventType.TERMINAL_SESSION_DETACHED]
    assert len(detached_events) == 1


def test_recovery_mixed_multiple_chats():
    """Recovery handles multiple chats independently."""
    from wlcodex.recovery import RecoveryManager

    events = [
        # Chat 100: terminal mode, claude attached (process dead)
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10, chat_id=100, conv_id=42, conv_str="42"),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_orphan",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11, chat_id=100, conv_id=42, conv_str="42"),
        # Chat 200: product mode
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 200, "conversation_id": 43,
            "from_mode": "terminal", "to_mode": "product",
            "active_agent": "codex",
        }, event_id=12, chat_id=200, conv_id=43, conv_str="43"),
        _event(EventType.PRODUCT_DISPLAY_FRAME, {
            "chat_id": 200, "conversation_id": 43,
            "surface": "product", "position": 10,
        }, event_id=13, chat_id=200, conv_id=43, conv_str="43"),
    ]

    checker = FakeProcessChecker(alive_sessions=set())
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    # Chat 100: claude session orphaned
    assert state.by_chat[100].terminal_sessions["claude"].status == "attached"
    orphan_for_100 = [e for e in new_events
                      if e.payload.get("external_session_id") == "claude_orphan"]
    assert len(orphan_for_100) == 1

    # Chat 200: product mode unaffected
    assert state.by_chat[200].active_mode == "product"
    assert state.by_chat[200].cursors["product"].position == 10


def test_recovery_no_terminal_sessions_no_orphan_events():
    """When no terminal sessions exist, no orphan events should be produced."""
    from wlcodex.recovery import RecoveryManager

    events = [
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "terminal", "to_mode": "product",
            "active_agent": "codex",
        }, event_id=10),
    ]

    checker = FakeProcessChecker(alive_sessions=set())
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    # Only bookend events (started + completed), no orphan events
    orphan_events = [e for e in new_events
                     if e.event_type == EventType.TERMINAL_SESSION_DETACHED]
    assert len(orphan_events) == 0
    assert state.by_chat[100].active_mode == "product"


def test_recovery_aborted_session_not_rechecked():
    """Already aborted sessions should not be rechecked for process liveness."""
    from wlcodex.recovery import RecoveryManager

    events = [
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_aborted",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_ABORTED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_aborted",
        }, event_id=12),
    ]

    # Process checker says alive but session is aborted -> no re-attach
    checker = FakeProcessChecker(alive_sessions={"claude_aborted"})
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    surface = state.by_chat[100]
    assert surface.terminal_sessions["claude"].status == "aborted"
    # Only bookend events, no re-attach or orphan for aborted session
    orphan_events = [e for e in new_events
                     if e.event_type == EventType.TERMINAL_SESSION_DETACHED]
    assert len(orphan_events) == 0


def test_recovery_detached_session_not_rechecked():
    """Already detached sessions should not be rechecked for process liveness."""
    from wlcodex.recovery import RecoveryManager

    events = [
        _event(EventType.CONVERSATION_MODE_SWITCHED, {
            "chat_id": 100, "conversation_id": 42,
            "from_mode": "product", "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event(EventType.TERMINAL_SESSION_ATTACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_detached",
            "strategy": "stream_json",
            "status": "attached",
        }, event_id=11),
        _event(EventType.TERMINAL_SESSION_DETACHED, {
            "chat_id": 100, "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_detached",
            "status": "detached",
        }, event_id=12),
    ]

    checker = FakeProcessChecker(alive_sessions={"claude_detached"})
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    surface = state.by_chat[100]
    assert surface.terminal_sessions["claude"].status == "detached"
    # Only bookend events, no re-attach or orphan for already-detached session
    orphan_events = [e for e in new_events
                     if e.event_type == EventType.TERMINAL_SESSION_DETACHED]
    assert len(orphan_events) == 0


def test_recovery_manager_system_recovery_events():
    """Recovery manager emits system.recovery.started and system.recovery.completed."""
    from wlcodex.recovery import RecoveryManager

    events: list = []
    checker = FakeProcessChecker()
    manager = RecoveryManager(process_checker=checker)

    state, new_events = manager.recover(events)

    # Recovery bookend events
    started = [e for e in new_events if e.event_type == EventType.SYSTEM_RECOVERY_STARTED]
    completed = [e for e in new_events if e.event_type == EventType.SYSTEM_RECOVERY_COMPLETED]
    assert len(started) == 1
    assert len(completed) == 1
