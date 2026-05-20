"""Tests for workbench runtime state replay and recovery projection.

Covers replay reconstructing:
- current view (cockpit / onsite)
- execution mode (orchestrated / codex_direct / claude_direct)
- active onsite agent
- cockpit cursor
- onsite cursor
- orphaned onsite session after recovery event
"""

from __future__ import annotations

import pytest

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.runtime_state import (
    WorkbenchRuntimeState,
    replay_workbench_events,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    *,
    correlation_id: str = "corr-test",
    source: str = EventSource.CONTROLLER,
    actor: str = "system",
    visibility: str = Visibility.INTERNAL,
    payload: dict | None = None,
    conversation_id: int | None = 1,
    chat_id: int | None = None,
    orchestration_run_id: int | None = None,
    agent_run_id: int | None = None,
    task_id: int | None = None,
    event_id: int = 0,
) -> RuntimeEvent:
    p = dict(payload or {})
    if chat_id is not None:
        p["chat_id"] = chat_id
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        correlation_id=correlation_id,
        source=source,
        actor=actor,
        visibility=visibility,
        payload=p,
        occurred_at=now_iso(),
        conversation_id=conversation_id,
        orchestration_run_id=orchestration_run_id,
        agent_run_id=agent_run_id,
        task_id=task_id,
        id=event_id,
    )


# ---------------------------------------------------------------------------
# View reconstruction
# ---------------------------------------------------------------------------

class TestViewReplay:
    def test_replay_reconstructs_cockpit_view_from_mode_switch(self):
        events = [
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "product", "to_mode": "cockpit",
                            "active_agent": "claude"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)

        assert state.view == "cockpit"
        assert state.active_agent == "claude"

    def test_replay_reconstructs_onsite_view_from_mode_switch(self):
        events = [
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "cockpit", "to_mode": "onsite",
                            "active_agent": "codex"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)

        assert state.view == "onsite"
        assert state.active_agent == "codex"

    def test_replay_view_switch_preserves_last_switch(self):
        """Last mode switch wins — only the most recent view is active."""
        events = [
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "cockpit", "to_mode": "onsite",
                            "active_agent": ""},
                   event_id=1),
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "onsite", "to_mode": "cockpit",
                            "active_agent": ""},
                   event_id=2),
        ]
        state = replay_workbench_events(events)

        assert state.view == "cockpit"

    def test_replay_supports_legacy_product_terminal_mode_names(self):
        """Legacy 'product' and 'terminal' still map to cockpit/onsite."""
        events = [
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "product", "to_mode": "terminal",
                            "active_agent": "claude"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)

        assert state.view == "onsite"
        assert state.active_agent == "claude"


# ---------------------------------------------------------------------------
# Execution mode reconstruction
# ---------------------------------------------------------------------------

class TestExecutionModeReplay:
    def test_replay_reconstructs_orchestrated_mode(self):
        events = [
            _event(EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"execution_mode": "orchestrated"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.execution_mode == "orchestrated"

    def test_replay_reconstructs_codex_direct_mode(self):
        events = [
            _event(EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"execution_mode": "codex_direct"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.execution_mode == "codex_direct"

    def test_replay_reconstructs_claude_direct_mode(self):
        events = [
            _event(EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"execution_mode": "claude_direct"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.execution_mode == "claude_direct"

    def test_default_execution_mode_is_orchestrated(self):
        state = replay_workbench_events([])
        assert state.execution_mode == "orchestrated"


# ---------------------------------------------------------------------------
# Active onsite agent reconstruction
# ---------------------------------------------------------------------------

class TestActiveAgentReplay:
    def test_active_agent_from_mode_switch(self):
        events = [
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "cockpit", "to_mode": "onsite",
                            "active_agent": "claude"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.active_agent == "claude"

    def test_active_agent_from_terminal_session_attached(self):
        events = [
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "codex",
                            "external_session_id": "ext-42",
                            "strategy": "stream-json"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.active_agent == "codex"
        assert state.onsite_session_status == "attached"
        assert state.onsite_external_session_id == "ext-42"

    def test_active_agent_updated_by_latest_attach(self):
        events = [
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "codex", "external_session_id": "ext-1"},
                   event_id=1),
            _event(EventType.TERMINAL_SESSION_DETACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "codex", "status": "detached"},
                   event_id=2),
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-2",
                   payload={"agent": "claude", "external_session_id": "ext-2"},
                   event_id=3),
        ]
        state = replay_workbench_events(events)
        assert state.active_agent == "claude"
        assert state.onsite_session_status == "attached"
        assert state.onsite_external_session_id == "ext-2"


# ---------------------------------------------------------------------------
# Cursor reconstruction
# ---------------------------------------------------------------------------

class TestCursorReplay:
    def test_cockpit_cursor_from_surface_cursor_advanced(self):
        events = [
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "product", "position": 5},
                   event_id=1),
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "product", "position": 12},
                   event_id=2),
        ]
        state = replay_workbench_events(events)
        assert state.cockpit_cursor == 12

    def test_onsite_cursor_from_surface_cursor_advanced(self):
        events = [
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "terminal", "position": 3},
                   event_id=1),
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "terminal", "position": 7},
                   event_id=2),
        ]
        state = replay_workbench_events(events)
        assert state.onsite_cursor == 7

    def test_cursor_never_goes_backward(self):
        """Only advances cursor forward; ignores decreases."""
        events = [
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "terminal", "position": 10},
                   event_id=1),
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "terminal", "position": 3},
                   event_id=2),
        ]
        state = replay_workbench_events(events)
        assert state.onsite_cursor == 10

    def test_cockpit_cursor_from_cockpit_surface_name(self):
        events = [
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "cockpit", "position": 8},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.cockpit_cursor == 8

    def test_onsite_cursor_from_onsite_surface_name(self):
        events = [
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "onsite", "position": 15},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.onsite_cursor == 15


# ---------------------------------------------------------------------------
# Orphaned onsite session
# ---------------------------------------------------------------------------

class TestOrphanedOnsiteSession:
    def test_orphaned_session_via_onsite_session_orphaned_event(self):
        events = [
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "claude", "external_session_id": "ext-1"},
                   event_id=1),
            _event(EventType.ONSITE_SESSION_ORPHANED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "claude",
                            "reason": "process_lost_on_restart"},
                   event_id=2),
        ]
        state = replay_workbench_events(events)
        assert state.onsite_session_status == "orphaned"
        assert state.active_agent == "claude"
        assert state.onsite_orphan_reason == "process_lost_on_restart"

    def test_orphaned_agent_run_marks_session_orphaned(self):
        """When an agent run is orphaned, the onsite session should reflect it."""
        events = [
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "codex", "external_session_id": "ext-3"},
                   event_id=1),
            _event(EventType.AGENT_RUN_ORPHANED,
                   AggregateType.AGENT_RUN, "ar-1",
                   agent_run_id=5,
                   payload={"agent": "codex", "reason": "connection_lost"},
                   event_id=2),
        ]
        state = replay_workbench_events(events)
        assert state.onsite_session_status == "orphaned"

    def test_orphaned_agent_run_does_not_overwrite_unrelated_active_agent(self):
        """Orphaning a non-active agent must not change active_agent or status."""
        events = [
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "claude", "external_session_id": "ext-claude"},
                   event_id=1),
            _event(EventType.AGENT_RUN_ORPHANED,
                   AggregateType.AGENT_RUN, "ar-1",
                   agent_run_id=5,
                   payload={"agent": "codex", "reason": "connection_lost"},
                   event_id=2),
        ]
        state = replay_workbench_events(events)
        # active_agent must stay "claude" — the orphaned codex run is unrelated
        assert state.active_agent == "claude"
        # onsite session must NOT be orphaned — the claude session is still running
        assert state.onsite_session_status == "attached"
        assert state.onsite_external_session_id == "ext-claude"

    def test_orphaned_agent_sets_active_agent_when_not_set(self):
        """When active_agent is empty, orphan event infers and sets it."""
        events = [
            _event(EventType.AGENT_RUN_ORPHANED,
                   AggregateType.AGENT_RUN, "ar-1",
                   agent_run_id=5,
                   payload={"agent": "codex", "reason": "process_lost"},
                   event_id=1),
        ]
        state = replay_workbench_events(events)
        assert state.active_agent == "codex"
        assert state.onsite_session_status == "orphaned"

    def test_orphaned_not_overwritten_by_reattach(self):
        """Re-attaching after orphan should update status."""
        events = [
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "claude", "external_session_id": "ext-1"},
                   event_id=1),
            _event(EventType.ONSITE_SESSION_ORPHANED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "claude",
                            "reason": "process_lost"},
                   event_id=2),
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-2",
                   payload={"agent": "claude", "external_session_id": "ext-new"},
                   event_id=3),
        ]
        state = replay_workbench_events(events)
        assert state.onsite_session_status == "attached"
        assert state.onsite_external_session_id == "ext-new"


# ---------------------------------------------------------------------------
# Full recovery replay
# ---------------------------------------------------------------------------

class TestFullRecoveryReplay:
    def test_full_workbench_recovery_replay(self):
        """Simulate a restart recovery: replay mixed events and check all fields."""
        events = [
            # Workbench created in cockpit view, orchestrated
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "", "to_mode": "cockpit",
                            "active_agent": ""},
                   event_id=1),
            _event(EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"execution_mode": "orchestrated"},
                   event_id=2),
            # User attaches to onsite with Claude
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "cockpit", "to_mode": "onsite",
                            "active_agent": "claude"},
                   event_id=3),
            _event(EventType.TERMINAL_SESSION_ATTACHED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "claude", "external_session_id": "ext-claude",
                            "strategy": "stream-json"},
                   event_id=4),
            # Cursors advance
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "product", "position": 3},
                   event_id=5),
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "terminal", "position": 42},
                   event_id=6),
            # Daemon restart — session lost
            _event(EventType.ONSITE_SESSION_ORPHANED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"agent": "claude",
                            "reason": "process_lost_on_restart"},
                   event_id=7),
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "onsite", "to_mode": "cockpit",
                            "active_agent": ""},
                   event_id=8),
        ]
        state = replay_workbench_events(events)

        # View
        assert state.view == "cockpit"
        # Execution mode
        assert state.execution_mode == "orchestrated"
        # Active agent (last attach)
        assert state.active_agent == "claude"
        # Cursors
        assert state.cockpit_cursor == 3
        assert state.onsite_cursor == 42
        # Orphaned
        assert state.onsite_session_status == "orphaned"
        assert state.onsite_orphan_reason == "process_lost_on_restart"
        assert state.onsite_external_session_id == "ext-claude"

    def test_empty_replay_has_sane_defaults(self):
        state = replay_workbench_events([])
        assert state.view == "cockpit"
        assert state.execution_mode == "orchestrated"
        assert state.active_agent == ""
        assert state.cockpit_cursor == 0
        assert state.onsite_cursor == 0
        assert state.onsite_session_status == "detached"
        assert state.onsite_orphan_reason == ""


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_replay_is_deterministic(self):
        events = [
            _event(EventType.CONVERSATION_MODE_SWITCHED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"from_mode": "cockpit", "to_mode": "onsite",
                            "active_agent": "codex"},
                   event_id=1),
            _event(EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
                   AggregateType.CONVERSATION, "conv-1",
                   payload={"execution_mode": "claude_direct"},
                   event_id=2),
            _event(EventType.SURFACE_CURSOR_ADVANCED,
                   AggregateType.SURFACE_SESSION, "sess-1",
                   payload={"surface": "terminal", "position": 99},
                   event_id=3),
        ]
        s1 = replay_workbench_events(events)
        s2 = replay_workbench_events(events)
        assert s1.view == s2.view
        assert s1.execution_mode == s2.execution_mode
        assert s1.active_agent == s2.active_agent
        assert s1.cockpit_cursor == s2.cockpit_cursor
        assert s1.onsite_cursor == s2.onsite_cursor
        assert s1.onsite_session_status == s2.onsite_session_status


# ---------------------------------------------------------------------------
# EventType constants existence
# ---------------------------------------------------------------------------

class TestEventConstantsExist:
    def test_workbench_execution_mode_selected_constant(self):
        assert hasattr(EventType, "WORKBENCH_EXECUTION_MODE_SELECTED")
        assert EventType.WORKBENCH_EXECUTION_MODE_SELECTED == \
            "workbench.execution_mode.selected"

    def test_onsite_session_orphaned_constant(self):
        assert hasattr(EventType, "ONSITE_SESSION_ORPHANED")
        assert EventType.ONSITE_SESSION_ORPHANED == \
            "onsite.session.orphaned"
