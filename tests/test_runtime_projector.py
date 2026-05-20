"""Tests for RuntimeProjector — compatibility table projections.

Covers agent_runs, orchestration_runs, usage_events, task_events,
approval_requests projections, terminal-state protection, rebuild,
and failure isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_projector import RuntimeProjector


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


def _setup(tmp_path: Path):
    """Create a fresh database with migrated schema, store, and projector."""
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    projector = RuntimeProjector(ledger._conn, store=store)
    return ledger, store, projector


def _row(conn, table: str, row_id: int) -> dict:
    """Fetch a row as a dict by id."""
    cur = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    r = cur.fetchone()
    return dict(r) if r else {}


# ---------------------------------------------------------------------------
# Agent runs projection
# ---------------------------------------------------------------------------

class TestAgentRunProjection:
    def test_agent_lifecycle_projected_to_agent_runs(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        # agent.run.queued → creates agent_runs row
        evt = _event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN, "ar-1",
                     agent_run_id=1, task_id=10,
                     payload={"agent": "claude", "role": "implementation"},
                     event_id=1)
        stored = store.append(evt)
        projector.apply(stored)

        row = _row(ledger._conn, "agent_runs", 1)
        assert row["status"] == "queued"
        assert row["agent"] == "claude"
        assert row["role"] == "implementation"

        # agent.run.started → running
        evt2 = _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-1",
                      agent_run_id=1,
                      payload={"external_session_id": "sess-abc"},
                      event_id=2)
        stored2 = store.append(evt2)
        projector.apply(stored2)

        row = _row(ledger._conn, "agent_runs", 1)
        assert row["status"] == "running"

        # agent.run.completed → done
        evt3 = _event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-1",
                      agent_run_id=1,
                      payload={"summary": "Fixed all bugs"},
                      event_id=3)
        stored3 = store.append(evt3)
        projector.apply(stored3)

        row = _row(ledger._conn, "agent_runs", 1)
        assert row["status"] == "done"
        assert row["completion_summary"] == "Fixed all bugs"

    def test_agent_failed_projected(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)
        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-f",
                            agent_run_id=2, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_FAILED, AggregateType.AGENT_RUN, "ar-f",
                            agent_run_id=2, event_id=2))
        projector.apply(store.get_by_id(2))

        row = _row(ledger._conn, "agent_runs", 2)
        assert row["status"] == "failed"

    def test_agent_timed_out_projected_as_failed(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)
        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-to",
                            agent_run_id=3, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_TIMED_OUT, AggregateType.AGENT_RUN, "ar-to",
                            agent_run_id=3, event_id=2))
        projector.apply(store.get_by_id(2))

        row = _row(ledger._conn, "agent_runs", 3)
        assert row["status"] == "failed"

    def test_agent_orphaned_projected_as_failed(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)
        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-or",
                            agent_run_id=4, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_ORPHANED, AggregateType.AGENT_RUN, "ar-or",
                            agent_run_id=4, event_id=2))
        projector.apply(store.get_by_id(2))

        row = _row(ledger._conn, "agent_runs", 4)
        assert row["status"] == "failed"

    def test_agent_terminal_not_overwritten_by_activity(self, tmp_path: Path):
        """AC: terminal state cannot be overwritten by later non-terminal activity."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-t1",
                            agent_run_id=10, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-t1",
                            agent_run_id=10, event_id=2))
        projector.apply(store.get_by_id(2))

        # Late activity — must not change status from done back to running.
        store.append(_event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN, "ar-t1",
                            agent_run_id=10, event_id=3))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 10)
        assert row["status"] == "done"

    def test_agent_terminal_activity_can_fill_missing_session_ref(self, tmp_path: Path):
        """Late activity may repair a missing external_session_id without reviving the run."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-tref",
                            agent_run_id=12, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-tref",
                            agent_run_id=12, event_id=2))
        projector.apply(store.get_by_id(2))

        store.append(_event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN, "ar-tref",
                            agent_run_id=12, event_id=3,
                            payload={"threadId": "thread-after-done"}))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 12)
        assert row["status"] == "done"
        assert row["external_session_id"] == "thread-after-done"

    def test_agent_terminal_not_overwritten_by_started(self, tmp_path: Path):
        """AC: a completed agent must not be revived by a late agent.run.started."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-t2",
                            agent_run_id=11, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-t2",
                            agent_run_id=11, event_id=2))
        projector.apply(store.get_by_id(2))

        # Late started — must NOT revert "done" → "running".
        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-t2",
                            agent_run_id=11, event_id=3))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 11)
        assert row["status"] == "done"

    def test_agent_run_upsert_idempotent(self, tmp_path: Path):
        """Projecting the same event twice must not crash or duplicate rows."""
        ledger, store, projector = _setup(tmp_path)

        evt = _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-idem",
                     agent_run_id=20, payload={"agent": "claude", "role": "impl"},
                     event_id=1)
        stored = store.append(evt)
        projector.apply(stored)
        projector.apply(stored)  # second apply must not crash

        cur = ledger._conn.execute("SELECT COUNT(*) as c FROM agent_runs WHERE id = 20")
        assert cur.fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# Orchestration runs projection
# ---------------------------------------------------------------------------

class TestOrchestrationRunProjection:
    def test_full_orchestration_flow(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.RUN_REQUESTED, AggregateType.ORCHESTRATION_RUN,
                            "orch-1", orchestration_run_id=1,
                            payload={"goal": "Fix login bug", "max_verify_rounds": 2},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        row = _row(ledger._conn, "orchestration_runs", 1)
        assert row["goal"] == "Fix login bug"
        assert row["max_verify_rounds"] == 2

        store.append(_event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN,
                            "orch-1", orchestration_run_id=1,
                            payload={"phase": "running_analysis"},
                            event_id=2))
        projector.apply(store.get_by_id(2))

        row = _row(ledger._conn, "orchestration_runs", 1)
        assert row["status"] == "running"
        assert row["current_step"] == "running_analysis"

        store.append(_event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN,
                            "orch-1", orchestration_run_id=1,
                            payload={"phase": "running_implementation",
                                     "codex_analysis": "needs auth fix"},
                            event_id=3))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "orchestration_runs", 1)
        assert row["current_step"] == "running_implementation"
        assert row["last_codex_analysis"] == "needs auth fix"

        store.append(_event(EventType.VERIFICATION_DECISION_RECORDED,
                            AggregateType.ORCHESTRATION_RUN,
                            "orch-1", orchestration_run_id=1,
                            payload={"decision": "pass"},
                            event_id=4))
        projector.apply(store.get_by_id(4))

        store.append(_event(EventType.RUN_COMPLETED, AggregateType.ORCHESTRATION_RUN,
                            "orch-1", orchestration_run_id=1, event_id=5))
        projector.apply(store.get_by_id(5))

        row = _row(ledger._conn, "orchestration_runs", 1)
        assert row["status"] == "passed"

    def test_run_completed_without_pass_verification_is_flagged_failed(
        self, tmp_path: Path
    ):
        """run.completed must not mark a projection passed without Codex pass."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN,
                            "orch-gate", orchestration_run_id=40, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.RUN_COMPLETED, AggregateType.ORCHESTRATION_RUN,
                            "orch-gate", orchestration_run_id=40, event_id=2))
        projector.apply(store.get_by_id(2))

        row = _row(ledger._conn, "orchestration_runs", 40)
        assert row["status"] == "failed"
        assert row["last_verification_result"] == "run_completed_without_verification_pass"

    def test_orchestration_failed(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN,
                            "orch-f", orchestration_run_id=10, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.RUN_FAILED, AggregateType.ORCHESTRATION_RUN,
                            "orch-f", orchestration_run_id=10, event_id=2))
        projector.apply(store.get_by_id(2))

        row = _row(ledger._conn, "orchestration_runs", 10)
        assert row["status"] == "failed"

    def test_orchestration_cancelled(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN,
                            "orch-c", orchestration_run_id=20, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.RUN_CANCELLED, AggregateType.ORCHESTRATION_RUN,
                            "orch-c", orchestration_run_id=20, event_id=2))
        projector.apply(store.get_by_id(2))

        row = _row(ledger._conn, "orchestration_runs", 20)
        assert row["status"] == "aborted"

    def test_orchestration_terminal_not_overwritten(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN,
                            "orch-t", orchestration_run_id=30, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.RUN_FAILED, AggregateType.ORCHESTRATION_RUN,
                            "orch-t", orchestration_run_id=30, event_id=2))
        projector.apply(store.get_by_id(2))

        store.append(_event(EventType.RUN_PHASE_CHANGED, AggregateType.ORCHESTRATION_RUN,
                            "orch-t", orchestration_run_id=30,
                            payload={"phase": "running_analysis"},
                            event_id=3))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "orchestration_runs", 30)
        assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# Usage events projection
# ---------------------------------------------------------------------------

class TestUsageEventProjection:
    def test_usage_event_creates_usage_events_row(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN,
                            "ar-u", agent_run_id=1, orchestration_run_id=5,
                            payload={
                                "agent": "claude", "role": "implementation",
                                "phase": "implementation", "request_kind": "chat",
                                "request_index": 1, "model": "claude-sonnet-4-6",
                                "input_tokens": 1500, "output_tokens": 800,
                                "cached_input_tokens": 300, "reasoning_output_tokens": 100,
                                "total_tokens": 2400, "latency_ms": 5200,
                                "source": "exact", "status": "success",
                            },
                            event_id=1))
        projector.apply(store.get_by_id(1))

        row = _row(ledger._conn, "usage_events", 1)
        assert row["agent"] == "claude"
        assert row["input_tokens"] == 1500
        assert row["output_tokens"] == 800
        assert row["cached_input_tokens"] == 300
        assert row["reasoning_output_tokens"] == 100
        assert row["total_tokens"] == 2400
        assert row["agent_run_id"] == 1

    def test_usage_event_updates_agent_run_tokens(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        # Create agent run first.
        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-tok", agent_run_id=5, event_id=1))
        projector.apply(store.get_by_id(1))

        # Usage event should update token totals.
        store.append(_event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN,
                            "ar-tok", agent_run_id=5,
                            payload={"input_tokens": 100, "output_tokens": 50},
                            event_id=2))
        projector.apply(store.get_by_id(2))

        store.append(_event(EventType.MODEL_USAGE_UPDATED, AggregateType.AGENT_RUN,
                            "ar-tok", agent_run_id=5,
                            payload={"input_tokens": 200, "output_tokens": 100},
                            event_id=3))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 5)
        assert row["token_input"] == 300
        assert row["token_output"] == 150


# ---------------------------------------------------------------------------
# Approval projection
# ---------------------------------------------------------------------------

class TestApprovalProjection:
    def test_approval_requested_creates_row(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-1", task_id=1,
                            payload={
                                "kind": "command", "summary": "rm -rf /tmp/cache",
                                "codex_request_id": "req-1",
                                "codex_item_id": "item-1", "codex_turn_id": "turn-1",
                            },
                            event_id=1))
        projector.apply(store.get_by_id(1))

        # Find the approval by codex_request_id.
        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?", ("req-1",)
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"
        assert row["kind"] == "command"

    def test_approval_resolved(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-2", task_id=2,
                            payload={"codex_request_id": "req-2", "kind": "command"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.APPROVAL_RESOLVED, AggregateType.APPROVAL,
                            "appr-2", task_id=2,
                            payload={"codex_request_id": "req-2",
                                     "decision": "approve", "resolver": "user"},
                            event_id=2))
        projector.apply(store.get_by_id(2))

        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?", ("req-2",)
        ).fetchone()
        assert row["status"] == "approved"

    def test_approval_accept_decisions_project_as_approved(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-accept", task_id=2,
                            payload={"codex_request_id": "req-accept", "kind": "command"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.APPROVAL_RESOLVED, AggregateType.APPROVAL,
                            "appr-accept", task_id=2,
                            payload={"codex_request_id": "req-accept",
                                     "decision": "acceptForSession", "resolver": "user"},
                            event_id=2))
        projector.apply(store.get_by_id(2))

        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?",
            ("req-accept",),
        ).fetchone()
        assert row["status"] == "approved"

    def test_approval_denied(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-3", task_id=3,
                            payload={"codex_request_id": "req-3", "kind": "file_change"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.APPROVAL_RESOLVED, AggregateType.APPROVAL,
                            "appr-3", task_id=3,
                            payload={"codex_request_id": "req-3",
                                     "decision": "deny", "resolver": "user"},
                            event_id=2))
        projector.apply(store.get_by_id(2))

        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?", ("req-3",)
        ).fetchone()
        assert row["status"] == "denied"

    def test_approval_expired(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-4", task_id=4,
                            payload={"codex_request_id": "req-4"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.APPROVAL_EXPIRED, AggregateType.APPROVAL,
                            "appr-4", task_id=4,
                            payload={"codex_request_id": "req-4"},
                            event_id=2))
        projector.apply(store.get_by_id(2))

        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?", ("req-4",)
        ).fetchone()
        assert row["status"] == "expired"

    def test_approval_summary_fallback_to_reason(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-sum-1", task_id=10,
                            payload={"codex_request_id": "req-sum-1",
                                     "kind": "command",
                                     "reason": "需要清理缓存"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?",
            ("req-sum-1",)
        ).fetchone()
        assert row is not None
        assert row["summary"] == "需要清理缓存"

    def test_approval_summary_fallback_to_command(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-sum-2", task_id=11,
                            payload={"codex_request_id": "req-sum-2",
                                     "kind": "command",
                                     "command": "pytest tests/"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?",
            ("req-sum-2",)
        ).fetchone()
        assert row is not None
        assert row["summary"] == "pytest tests/"

    def test_approval_summary_not_empty_when_no_fields(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-sum-3", task_id=12,
                            payload={"codex_request_id": "req-sum-3",
                                     "kind": "permissions"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        row = ledger._conn.execute(
            "SELECT * FROM approval_requests WHERE codex_request_id = ?",
            ("req-sum-3",)
        ).fetchone()
        assert row is not None
        assert row["summary"], f"Expected non-empty summary, got {row['summary']!r}"


# ---------------------------------------------------------------------------
# Task events compat projection
# ---------------------------------------------------------------------------

class TestTaskEventCompatProjection:
    def test_important_events_create_task_events_rows(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-te", agent_run_id=1, task_id=10,
                            payload={"agent": "claude"}, event_id=1))
        projector.apply(store.get_by_id(1))

        rows = ledger._conn.execute(
            "SELECT * FROM task_events WHERE task_id = 10 ORDER BY id"
        ).fetchall()
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["runtime_event_id"] == 1

    def test_approval_event_creates_task_event(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.APPROVAL_REQUESTED, AggregateType.APPROVAL,
                            "appr-te", task_id=5,
                            payload={"codex_request_id": "r-te", "kind": "command",
                                     "summary": "delete temp"},
                            event_id=1))
        projector.apply(store.get_by_id(1))

        rows = ledger._conn.execute(
            "SELECT * FROM task_events WHERE task_id = 5 ORDER BY id"
        ).fetchall()
        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Verification projection
# ---------------------------------------------------------------------------

class TestVerificationProjection:
    def test_verification_decision_creates_decision_row(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.RUN_STARTED, AggregateType.ORCHESTRATION_RUN,
                            "orch-v", orchestration_run_id=10, event_id=1))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.VERIFICATION_DECISION_RECORDED,
                            AggregateType.ORCHESTRATION_RUN,
                            "orch-v", orchestration_run_id=10,
                            payload={"decision": "pass", "reason": "tests green",
                                     "next_agent": ""},
                            event_id=2))
        projector.apply(store.get_by_id(2))

        rows = ledger._conn.execute(
            "SELECT * FROM orchestration_decisions WHERE run_id = 10 ORDER BY id"
        ).fetchall()
        assert len(rows) >= 1
        assert rows[0]["decision"] == "pass"

        # Also updated last_verification_result on the run.
        orch = _row(ledger._conn, "orchestration_runs", 10)
        assert orch["last_verification_result"] == "pass"


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------

class TestRebuild:
    def test_rebuild_from_scratch(self, tmp_path: Path):
        ledger, store, projector = _setup(tmp_path)

        # Append and project several events.
        for i, (etype, agg_type, agg_id, ar_id) in enumerate([
            (EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN, "ar-a", 1),
            (EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-a", 1),
            (EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN, "ar-a", 1),
        ], start=1):
            evt = _event(etype, agg_type, agg_id,
                         agent_run_id=ar_id, event_id=i)
            stored = store.append(evt)
            projector.apply(stored)

        # Verify agent_runs is correct.
        row = _row(ledger._conn, "agent_runs", 1)
        assert row["status"] == "done"

        # Now change the status directly to simulate corruption.
        ledger._conn.execute("UPDATE agent_runs SET status = 'queued' WHERE id = 1")
        ledger._conn.commit()
        assert _row(ledger._conn, "agent_runs", 1)["status"] == "queued"

        # Rebuild should restore the correct status.
        count = projector.rebuild(store)
        assert count == 3

        row = _row(ledger._conn, "agent_runs", 1)
        assert row["status"] == "done"

    def test_rebuild_without_store_raises(self, tmp_path: Path):
        ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
        ledger.migrate()
        projector = RuntimeProjector(ledger._conn, store=None)

        with pytest.raises(RuntimeError, match="No RuntimeEventStore"):
            projector.rebuild()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_projection_failure_does_not_delete_events(self, tmp_path: Path):
        """AC: Projection failures do not delete or mutate runtime_events."""
        ledger, store, projector = _setup(tmp_path)

        evt = _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-fi",
                     agent_run_id=1, event_id=1)
        stored = store.append(evt)

        # Cause a projection failure by passing an event that will cause an
        # integrity error — e.g., an event with agent_run_id but no matching
        # conversation_id in conversation_sessions table.  This is fine since
        # the projector just records what it can.
        # Actually, let's test that apply() catches exceptions internally.
        # We can corrupt the connection temporarily.
        ledger._conn.execute("DROP TABLE IF EXISTS agent_runs")
        ledger._conn.commit()

        # This should not raise.
        projector.apply(stored)

        # The event must still exist.
        loaded = store.get_by_id(stored.id)
        assert loaded is not None

    def test_projection_does_not_mutate_runtime_events(self, tmp_path: Path):
        """AC: projection does not mutate runtime_events."""
        ledger, store, projector = _setup(tmp_path)

        evt = _event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN, "ar-mut",
                     agent_run_id=1, event_id=1)
        stored = store.append(evt)
        original_payload = dict(stored.payload)

        projector.apply(stored)

        loaded = store.get_by_id(stored.id)
        assert loaded.payload == original_payload
        assert loaded == stored

    def test_projector_skip_events_without_task_id_for_task_events(self, tmp_path: Path):
        """Events without task_id should not create task_events rows."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-nt", agent_run_id=1, task_id=None, event_id=1))
        projector.apply(store.get_by_id(1))

        count = ledger._conn.execute(
            "SELECT COUNT(*) as c FROM task_events"
        ).fetchone()["c"]
        assert count == 0


# ---------------------------------------------------------------------------
# Fix 1: session_id in AGENT_RUN_COMPLETED / AGENT_RUN_ACTIVITY payload
# must be projected to agent_runs.external_session_id
# ---------------------------------------------------------------------------

class TestAgentRunSessionIdProjection:
    def test_completed_session_id_projected_without_external_session_id(
        self, tmp_path: Path
    ):
        """When payload has session_id but no external_session_id,
        agent_runs.external_session_id must equal session_id after projection."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN,
                            "ar-sess", agent_run_id=1, event_id=1,
                            payload={"agent": "claude", "role": "implementation"}))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-sess", agent_run_id=1, event_id=2))
        projector.apply(store.get_by_id(2))

        # AGENT_RUN_COMPLETED with session_id only (no external_session_id)
        store.append(_event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN,
                            "ar-sess", agent_run_id=1, event_id=3,
                            payload={"session_id": "claude_sess_123"}))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 1)
        assert row["external_session_id"] == "claude_sess_123"

    def test_completed_external_session_id_takes_priority_over_session_id(
        self, tmp_path: Path
    ):
        """When both external_session_id and session_id are present,
        external_session_id takes priority."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN,
                            "ar-prio", agent_run_id=2, event_id=1,
                            payload={"agent": "claude", "role": "implementation"}))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-prio", agent_run_id=2, event_id=2))
        projector.apply(store.get_by_id(2))

        store.append(_event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN,
                            "ar-prio", agent_run_id=2, event_id=3,
                            payload={
                                "external_session_id": "ext_priority",
                                "session_id": "claude_sess_456",
                            }))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 2)
        assert row["external_session_id"] == "ext_priority"

    def test_activity_session_id_projected_without_external_session_id(
        self, tmp_path: Path
    ):
        """AGENT_RUN_ACTIVITY with session_id also propagates to agent_runs."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN,
                            "ar-act", agent_run_id=3, event_id=1,
                            payload={"agent": "claude", "role": "implementation"}))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-act", agent_run_id=3, event_id=2))
        projector.apply(store.get_by_id(2))

        store.append(_event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN,
                            "ar-act", agent_run_id=3, event_id=3,
                            payload={"session_id": "claude_activity_sess"}))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 3)
        assert row["external_session_id"] == "claude_activity_sess"

    def test_activity_thread_id_projected_for_codex_runs(
        self, tmp_path: Path
    ):
        """Codex activity events use threadId as the resumable session ref."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN,
                            "ar-codex-thread", agent_run_id=5, event_id=1,
                            payload={"agent": "codex", "role": "analysis"}))
        projector.apply(store.get_by_id(1))

        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-codex-thread", agent_run_id=5, event_id=2))
        projector.apply(store.get_by_id(2))

        store.append(_event(EventType.AGENT_RUN_ACTIVITY, AggregateType.AGENT_RUN,
                            "ar-codex-thread", agent_run_id=5, event_id=3,
                            payload={"action": "turn_started", "threadId": "codex_thread_123"}))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 5)
        assert row["external_session_id"] == "codex_thread_123"

    def test_completed_session_id_does_not_overwrite_existing_external_session(
        self, tmp_path: Path
    ):
        """A late AGENT_RUN_COMPLETED without session_id must not wipe the
        external_session_id that was already set by an earlier event."""
        ledger, store, projector = _setup(tmp_path)

        store.append(_event(EventType.AGENT_RUN_QUEUED, AggregateType.AGENT_RUN,
                            "ar-keep", agent_run_id=4, event_id=1,
                            payload={"agent": "claude", "role": "implementation"}))
        projector.apply(store.get_by_id(1))

        # STARTED sets external_session_id first
        store.append(_event(EventType.AGENT_RUN_STARTED, AggregateType.AGENT_RUN,
                            "ar-keep", agent_run_id=4, event_id=2,
                            payload={"external_session_id": "early_sess"}))
        projector.apply(store.get_by_id(2))

        # COMPLETED without session_id - must not wipe the existing value
        store.append(_event(EventType.AGENT_RUN_COMPLETED, AggregateType.AGENT_RUN,
                            "ar-keep", agent_run_id=4, event_id=3,
                            payload={"summary": "done"}))
        projector.apply(store.get_by_id(3))

        row = _row(ledger._conn, "agent_runs", 4)
        assert row["external_session_id"] == "early_sess"
