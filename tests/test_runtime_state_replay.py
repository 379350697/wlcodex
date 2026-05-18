"""Tests for runtime_state.py — pure replay reducers.

Covers agent lifecycle, orchestration pass/fail/retry, approval
lifecycle, timeout events, terminal-state protection, and usage
accumulation.
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
    _AGENT_TERMINAL_STATES,
    _APPROVAL_TERMINAL_STATES,
    _ORCHESTRATION_TERMINAL_STATES,
    RuntimeAgentState,
    RuntimeApprovalState,
    RuntimeOrchestrationState,
    RuntimeStateSnapshot,
    replay_events,
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
    source: str = EventSource.CLAUDE,
    actor: str = "claude",
    visibility: str = Visibility.INTERNAL,
    payload: dict | None = None,
    conversation_id: int | None = 1,
    orchestration_run_id: int | None = None,
    agent_run_id: int | None = None,
    task_id: int | None = None,
    event_id: int = 0,
) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        correlation_id=correlation_id,
        source=source,
        actor=actor,
        visibility=visibility,
        payload=payload or {},
        occurred_at=now_iso(),
        conversation_id=conversation_id,
        orchestration_run_id=orchestration_run_id,
        agent_run_id=agent_run_id,
        task_id=task_id,
        id=event_id,
    )


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------

class TestAgentLifecycle:
    def test_queued_to_running_to_completed(self):
        events = [
            _event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN, "ar-1",
                   agent_run_id=1, payload={"agent": "claude", "role": "implementation"},
                   event_id=1),
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-1",
                   agent_run_id=1, event_id=2),
            _event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN, "ar-1",
                   agent_run_id=1, payload={"note": "thinking"}, event_id=3),
            _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-1",
                   agent_run_id=1,
                   payload={"summary": "All tests pass"},
                   event_id=4),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-1")
        assert agent is not None
        assert agent.status == "completed"
        assert agent.agent == "claude"
        assert agent.role == "implementation"
        assert agent.completion_summary == "All tests pass"
        assert agent.last_activity_id == 4

    def test_queued_to_running_to_failed(self):
        events = [
            _event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN, "ar-2",
                   agent_run_id=2, payload={"agent": "codex", "role": "analysis"},
                   event_id=1),
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-2",
                   agent_run_id=2, event_id=2),
            _event(EventType.AGENT_RUN_FAILED, AggregateType.AGENT_RUN, "ar-2",
                   agent_run_id=2, payload={"reason": "crash"}, event_id=3),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-2")
        assert agent is not None
        assert agent.status == "failed"
        assert agent.is_terminal

    def test_agent_activity_refreshes_last_activity(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-3",
                   agent_run_id=3, event_id=1),
            _event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN, "ar-3",
                   agent_run_id=3, payload={"note": "a"}, event_id=2),
            _event(EventType.AGENT_RUN_HEARTBEAT, AggregateType.AGENT_RUN, "ar-3",
                   agent_run_id=3, event_id=3),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-3")
        assert agent is not None
        assert agent.status == "running"
        assert agent.last_activity_id == 3
        assert agent.last_activity_type == EventType.AGENT_RUN_HEARTBEAT

    def test_waiting_for_approval_then_resume(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-4",
                   agent_run_id=4, event_id=1),
            _event(EventType.AGENT_RUN_WAITING_FOR_APPROVAL, AggregateType.AGENT_RUN, "ar-4",
                   agent_run_id=4, event_id=2),
            _event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN, "ar-4",
                   agent_run_id=4, event_id=3),
            _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-4",
                   agent_run_id=4, event_id=4),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-4")
        assert agent is not None
        assert agent.status == "completed"

    def test_timed_out_and_orphaned_are_terminal(self):
        for etype, expected in [
            (EventType.AGENT_RUN_TIMED_OUT, "timed_out"),
            (EventType.AGENT_RUN_ORPHANED, "orphaned"),
        ]:
            events = [
                _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-t",
                       agent_run_id=10, event_id=1),
                _event(etype, AggregateType.AGENT_RUN, "ar-t",
                       agent_run_id=10, event_id=2),
            ]
            snap = replay_events(events)
            agent = snap.agent("ar-t")
            assert agent is not None
            assert agent.status == expected
            assert agent.is_terminal


# ---------------------------------------------------------------------------
# Terminal state protection — agents
# ---------------------------------------------------------------------------

class TestAgentTerminalProtection:
    def test_completed_agent_not_overwritten_by_activity(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-x",
                   agent_run_id=99, event_id=1),
            _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-x",
                   agent_run_id=99, event_id=2),
            # Late activity after completion — must not change status.
            _event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN, "ar-x",
                   agent_run_id=99, payload={"note": "late"}, event_id=3),
            _event(EventType.AGENT_RUN_HEARTBEAT, AggregateType.AGENT_RUN, "ar-x",
                   agent_run_id=99, event_id=4),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-x")
        assert agent is not None
        assert agent.status == "completed"
        assert agent.last_activity_id == 2  # Not overwritten by activity/heartbeat.

    def test_failed_agent_not_revived_by_started(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-f",
                   agent_run_id=50, event_id=1),
            _event(EventType.AGENT_RUN_FAILED, AggregateType.AGENT_RUN, "ar-f",
                   agent_run_id=50, event_id=2),
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-f",
                   agent_run_id=50, event_id=3),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-f")
        assert agent is not None
        assert agent.status == "failed"

    def test_agent_terminal_states_are_exhaustive(self):
        # Every state in the agent state machine diagram should be covered.
        for state in ["completed", "failed", "timed_out", "cancelled", "orphaned"]:
            assert state in _AGENT_TERMINAL_STATES, f"Missing terminal: {state}"


# ---------------------------------------------------------------------------
# Orchestration lifecycle
# ---------------------------------------------------------------------------

class TestOrchestrationLifecycle:
    def test_full_pass_flow(self):
        events = [
            _event(EventType.RUN_REQUESTED, AggregateType.ORCHESTRATION_RUN, "orch-1",
                   orchestration_run_id=1, payload={"goal": "Fix bug #42"},
                   event_id=1),
            _event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN, "orch-1",
                   orchestration_run_id=1,
                   payload={"phase": "running_analysis"},
                   event_id=2),
            _event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN, "orch-1",
                   orchestration_run_id=1,
                   payload={"phase": "running_implementation", "codex_analysis": "need code fix"},
                   event_id=3),
            _event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN, "orch-1",
                   orchestration_run_id=1,
                   payload={"phase": "running_verification"},
                   event_id=4),
            _event(EventType.VERIFICATION_DECISION_RECORDED, AggregateType.ORCHESTRATION_RUN,
                   "orch-1", orchestration_run_id=1,
                   payload={"decision": "pass"},
                   event_id=5),
            _event(EventType.RUN_COMPLETED, AggregateType.ORCHESTRATION_RUN, "orch-1",
                   orchestration_run_id=1, event_id=6),
        ]
        snap = replay_events(events)

        orch = snap.orchestration("orch-1")
        assert orch is not None
        assert orch.status == "completed"
        assert orch.goal == "Fix bug #42"
        assert orch.current_phase == "completed"
        assert orch.last_verification_result == "pass"

    def test_run_completed_without_pass_verification_is_abnormal(self):
        events = [
            _event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN, "orch-gate",
                   orchestration_run_id=9, event_id=1),
            _event(EventType.RUN_COMPLETED, AggregateType.ORCHESTRATION_RUN, "orch-gate",
                   orchestration_run_id=9, event_id=2),
        ]
        snap = replay_events(events)

        orch = snap.orchestration("orch-gate")
        assert orch is not None
        assert orch.status == "failed"
        assert orch.failure_reason == "run_completed_without_verification_pass"

    def test_fail_flow(self):
        events = [
            _event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN, "orch-2",
                   orchestration_run_id=2, event_id=1),
            _event(EventType.RUN_FAILED, AggregateType.ORCHESTRATION_RUN, "orch-2",
                   orchestration_run_id=2,
                   payload={"reason": "codex unavailable", "last_active_agent": "codex"},
                   event_id=2),
        ]
        snap = replay_events(events)

        orch = snap.orchestration("orch-2")
        assert orch is not None
        assert orch.status == "failed"
        assert orch.failure_reason == "codex unavailable"
        assert orch.last_active_agent == "codex"

    def test_cancel_flow(self):
        events = [
            _event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN, "orch-3",
                   orchestration_run_id=3, event_id=1),
            _event(EventType.RUN_CANCEL_REQUESTED, AggregateType.ORCHESTRATION_RUN, "orch-3",
                   orchestration_run_id=3, event_id=2),
            _event(EventType.RUN_CANCELLED, AggregateType.ORCHESTRATION_RUN, "orch-3",
                   orchestration_run_id=3, event_id=3),
        ]
        snap = replay_events(events)

        orch = snap.orchestration("orch-3")
        assert orch is not None
        assert orch.status == "cancelled"
        assert orch.is_terminal

    def test_retry_flow(self):
        events = [
            _event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN, "orch-4",
                   orchestration_run_id=4, event_id=1),
            _event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN, "orch-4",
                   orchestration_run_id=4,
                   payload={"phase": "running_verification"},
                   event_id=2),
            _event(EventType.VERIFICATION_DECISION_RECORDED, AggregateType.ORCHESTRATION_RUN,
                   "orch-4", orchestration_run_id=4,
                   payload={"decision": "fail", "reason": "tests not passing"},
                   event_id=3),
            _event(EventType.VERIFICATION_RETRY_REQUESTED, AggregateType.ORCHESTRATION_RUN,
                   "orch-4", orchestration_run_id=4,
                   payload={"verify_round": 2},
                   event_id=4),
            _event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN, "orch-4",
                   orchestration_run_id=4,
                   payload={"phase": "retrying_implementation"},
                   event_id=5),
            _event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN, "orch-4",
                   orchestration_run_id=4,
                   payload={"phase": "running_verification", "verify_round": 2},
                   event_id=6),
            _event(EventType.VERIFICATION_DECISION_RECORDED, AggregateType.ORCHESTRATION_RUN,
                   "orch-4", orchestration_run_id=4,
                   payload={"decision": "pass"},
                   event_id=7),
            _event(EventType.RUN_COMPLETED, AggregateType.ORCHESTRATION_RUN, "orch-4",
                   orchestration_run_id=4, event_id=8),
        ]
        snap = replay_events(events)

        orch = snap.orchestration("orch-4")
        assert orch is not None
        assert orch.status == "completed"
        assert orch.verify_round == 2


# ---------------------------------------------------------------------------
# Terminal state protection — orchestration
# ---------------------------------------------------------------------------

class TestOrchestrationTerminalProtection:
    def test_completed_orchestration_not_overwritten_by_phase_change(self):
        events = [
            _event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN, "orch-t",
                   orchestration_run_id=100, event_id=1),
            _event(EventType.VERIFICATION_DECISION_RECORDED,
                   AggregateType.ORCHESTRATION_RUN, "orch-t",
                   orchestration_run_id=100,
                   payload={"decision": "pass"},
                   event_id=2),
            _event(EventType.RUN_COMPLETED, AggregateType.ORCHESTRATION_RUN, "orch-t",
                   orchestration_run_id=100, event_id=3),
            _event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN, "orch-t",
                   orchestration_run_id=100,
                   payload={"phase": "running_analysis"},
                   event_id=4),
        ]
        snap = replay_events(events)

        orch = snap.orchestration("orch-t")
        assert orch is not None
        assert orch.status == "completed"

    def test_orchestration_terminal_states_match_spec(self):
        for state in ["completed", "failed", "cancelled"]:
            assert state in _ORCHESTRATION_TERMINAL_STATES, f"Missing terminal: {state}"


# ---------------------------------------------------------------------------
# Approval lifecycle
# ---------------------------------------------------------------------------

class TestApprovalLifecycle:
    def test_requested_to_resolved(self):
        events = [
            _event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL, "appr-1",
                   task_id=1, agent_run_id=5,
                   payload={"kind": "command", "summary": "rm -rf /tmp/cache",
                            "tool_name": "bash"},
                   event_id=1),
            _event(EventType.APPROVAL_RESOLVED, AggregateType.APPROVAL, "appr-1",
                   task_id=1,
                   payload={"decision": "approve", "resolver": "user"},
                   event_id=2),
        ]
        snap = replay_events(events)

        approval = snap.approval("appr-1")
        assert approval is not None
        assert approval.status == "resolved"
        assert approval.kind == "command"
        assert approval.decision == "approve"
        assert approval.resolver == "user"

    def test_requested_to_expired(self):
        events = [
            _event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL, "appr-2",
                   task_id=1, event_id=1),
            _event(EventType.APPROVAL_EXPIRED, AggregateType.APPROVAL, "appr-2",
                   task_id=1, event_id=2),
        ]
        snap = replay_events(events)

        approval = snap.approval("appr-2")
        assert approval is not None
        assert approval.status == "expired"
        assert approval.is_terminal

    def test_resolved_approval_unchanged_by_expired(self):
        events = [
            _event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL, "appr-3",
                   task_id=1, event_id=1),
            _event(EventType.APPROVAL_RESOLVED, AggregateType.APPROVAL, "appr-3",
                   task_id=1, payload={"decision": "deny"}, event_id=2),
            _event(EventType.APPROVAL_EXPIRED, AggregateType.APPROVAL, "appr-3",
                   task_id=1, event_id=3),
        ]
        snap = replay_events(events)

        approval = snap.approval("appr-3")
        assert approval is not None
        assert approval.status == "resolved"  # Not overwritten.

    def test_approval_terminal_states_match_spec(self):
        for state in ["resolved", "expired", "cancelled"]:
            assert state in _APPROVAL_TERMINAL_STATES, f"Missing terminal: {state}"


# ---------------------------------------------------------------------------
# Timeout events
# ---------------------------------------------------------------------------

class TestTimeoutEvents:
    def test_idle_timeout_recorded_on_agent(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-to",
                   agent_run_id=42, event_id=1),
            _event(EventType.WATCHDOG_IDLE_TIMEOUT, AggregateType.AGENT_RUN, "ar-to",
                   agent_run_id=42, event_id=2,
                   payload={"idle_seconds": 300, "last_activity_id": 1}),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-to")
        assert agent is not None
        assert agent.idle_timeout_at != ""

    def test_hard_timeout_recorded_on_agent(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-ht",
                   agent_run_id=43, event_id=1),
            _event(EventType.WATCHDOG_HARD_TIMEOUT, AggregateType.AGENT_RUN, "ar-ht",
                   agent_run_id=43, event_id=2,
                   payload={"elapsed_seconds": 600}),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-ht")
        assert agent is not None
        assert agent.hard_timeout_at != ""


# ---------------------------------------------------------------------------
# Usage accumulation
# ---------------------------------------------------------------------------

class TestUsageAccumulation:
    def test_usage_events_accumulate_on_agent(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-u",
                   agent_run_id=10, payload={"agent": "claude"},
                   event_id=1),
            _event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN, "ar-u",
                   agent_run_id=10,
                   payload={"input_tokens": 500, "output_tokens": 200, "model": "sonnet"},
                   event_id=2),
            _event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN, "ar-u",
                   agent_run_id=10,
                   payload={"input_tokens": 300, "output_tokens": 150,
                            "cached_input_tokens": 100, "reasoning_output_tokens": 50},
                   event_id=3),
        ]
        snap = replay_events(events)

        agent = snap.agent("ar-u")
        assert agent is not None
        assert agent.token_input == 800
        assert agent.token_output == 350
        assert agent.cached_input_tokens == 100
        assert agent.reasoning_output_tokens == 50
        assert agent.model == "sonnet"

    def test_token_totals_aggregated_by_agent_name(self):
        events = [
            _event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN, "ar-a",
                   agent_run_id=1,
                   payload={"agent": "claude", "input_tokens": 100, "output_tokens": 50},
                   event_id=1),
            _event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN, "ar-b",
                   agent_run_id=2,
                   payload={"agent": "codex", "input_tokens": 200, "output_tokens": 100},
                   event_id=2),
            _event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN, "ar-a",
                   agent_run_id=1,
                   payload={"agent": "claude", "input_tokens": 50, "output_tokens": 25},
                   event_id=3),
        ]
        snap = replay_events(events)

        assert snap.token_totals["claude"]["input_tokens"] == 150
        assert snap.token_totals["claude"]["output_tokens"] == 75
        assert snap.token_totals["codex"]["input_tokens"] == 200
        assert snap.token_totals["codex"]["output_tokens"] == 100


# ---------------------------------------------------------------------------
# Empty / no-op
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_event_list(self):
        snap = replay_events([])
        assert snap.last_event_id == 0
        assert snap.agents == {}
        assert snap.orchestrations == {}
        assert snap.approvals == {}

    def test_unrecognized_event_type_no_crash(self):
        events = [
            _event("some.unknown.event", AggregateType.SYSTEM, "sys-1", event_id=1),
        ]
        snap = replay_events(events)
        assert snap.last_event_id == 1

    def test_multiple_agents_in_one_orchestration(self):
        events = [
            _event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN, "orch-m",
                   orchestration_run_id=1, event_id=1),
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "codex-1",
                   agent_run_id=1, orchestration_run_id=1,
                   payload={"agent": "codex", "role": "analysis"},
                   event_id=2),
            _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "codex-1",
                   agent_run_id=1, event_id=3),
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "claude-1",
                   agent_run_id=2, orchestration_run_id=1,
                   payload={"agent": "claude", "role": "implementation"},
                   event_id=4),
            _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "claude-1",
                   agent_run_id=2, event_id=5),
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "codex-2",
                   agent_run_id=3, orchestration_run_id=1,
                   payload={"agent": "codex", "role": "verification"},
                   event_id=6),
            _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "codex-2",
                   agent_run_id=3, event_id=7),
            _event(EventType.VERIFICATION_DECISION_RECORDED,
                   AggregateType.ORCHESTRATION_RUN, "orch-m",
                   orchestration_run_id=1,
                   payload={"decision": "pass"},
                   event_id=8),
            _event(EventType.RUN_COMPLETED, AggregateType.ORCHESTRATION_RUN, "orch-m",
                   orchestration_run_id=1, event_id=9),
        ]
        snap = replay_events(events)

        assert len(snap.agents) == 3
        assert snap.agent("codex-1").status == "completed"
        assert snap.agent("claude-1").status == "completed"
        assert snap.agent("codex-2").status == "completed"
        assert snap.orchestration("orch-m").status == "completed"

    def test_replay_is_deterministic(self):
        events = [
            _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-d",
                   agent_run_id=1, event_id=1),
            _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-d",
                   agent_run_id=1, event_id=2),
        ]
        snap1 = replay_events(events)
        snap2 = replay_events(events)

        assert snap1.agent("ar-d") == snap2.agent("ar-d")
        assert snap1.last_event_id == snap2.last_event_id
