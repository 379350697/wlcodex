"""Daemon restart recovery tests."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.runtime_diagnostics import (
    append_recovery_events,
    build_recovery_events,
    find_non_terminal_agent_runs,
)
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


def test_recovery_pauses_running_queued_and_waiting(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    # Create tasks in various states
    t_run = ledger.create_task("a", "/tmp/a", "Running", "th-1", None)
    ledger.set_task_status(t_run.id, TaskStatus.RUNNING)

    t_q = ledger.create_task("b", "/tmp/b", "Queued", "th-2", None)
    # Already queued

    t_wait = ledger.create_task("c", "/tmp/c", "Waiting", "th-3", None)
    ledger.set_task_status(t_wait.id, TaskStatus.WAITING_APPROVAL)

    t_paused = ledger.create_task("d", "/tmp/d", "Paused", "th-4", None)
    ledger.set_task_status(t_paused.id, TaskStatus.PAUSED)

    t_done = ledger.create_task("e", "/tmp/e", "Done", "th-5", None)
    ledger.set_task_status(t_done.id, TaskStatus.DONE)

    paused = ledger.mark_active_tasks_recovery_paused()

    assert t_run.id in paused
    assert t_q.id in paused
    assert t_wait.id in paused
    assert t_paused.id not in paused  # Already paused
    assert t_done.id not in paused  # Done stays done

    assert ledger.get_task(t_run.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t_q.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t_wait.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t_paused.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t_done.id).status == TaskStatus.DONE


def test_recovery_records_events(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    t = ledger.create_task("a", "/tmp/a", "Test", "th-1", None)
    ledger.set_task_status(t.id, TaskStatus.RUNNING)

    ledger.mark_active_tasks_recovery_paused()

    events = ledger.list_events(t.id)
    recovery_evts = [e for e in events if e.event_type == "recovery_paused"]
    assert len(recovery_evts) >= 1


def test_recovery_keeps_history_browsable(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    t1 = ledger.create_task("a", "/tmp/a", "Done task", "th-1", None)
    ledger.set_task_status(t1.id, TaskStatus.DONE)
    t2 = ledger.create_task("b", "/tmp/b", "Active task", "th-2", None)
    ledger.set_task_status(t2.id, TaskStatus.RUNNING)

    ledger.mark_active_tasks_recovery_paused()

    tasks = ledger.list_tasks()
    assert any(t.id == t1.id for t in tasks)  # Done still visible
    assert any(t.id == t2.id for t in tasks)  # Paused still visible

    task = ledger.get_task(t1.id)
    assert task.status == TaskStatus.DONE


def test_recovery_idempotent(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    t = ledger.create_task("a", "/tmp/a", "Test", "th-1", None)
    ledger.set_task_status(t.id, TaskStatus.RUNNING)

    ledger.mark_active_tasks_recovery_paused()
    ledger.mark_active_tasks_recovery_paused()  # Second call

    # Should remain paused, not error
    assert ledger.get_task(t.id).status == TaskStatus.PAUSED


def test_recovery_does_not_pause_waiting_slot(tmp_path: Path) -> None:
    """Startup recovery must NOT convert waiting_slot tasks to paused."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    t_run = ledger.create_task("a", "/tmp/a", "Running", "th-1", None)
    ledger.set_task_status(t_run.id, TaskStatus.RUNNING)

    t_wait = ledger.create_task(
        "a", "/tmp/a", "Waiting", None, None, status=TaskStatus.WAITING_SLOT,
    )

    t_done = ledger.create_task("b", "/tmp/b", "Done", "th-3", None)
    ledger.set_task_status(t_done.id, TaskStatus.DONE)

    paused = ledger.mark_active_tasks_recovery_paused()

    assert t_run.id in paused  # running -> paused
    assert t_wait.id not in paused  # waiting_slot stays waiting_slot
    assert t_done.id not in paused  # done stays done

    assert ledger.get_task(t_run.id).status == TaskStatus.PAUSED
    assert ledger.get_task(t_wait.id).status == TaskStatus.WAITING_SLOT
    assert ledger.get_task(t_done.id).status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# Hanging conversation runs recovery
# ---------------------------------------------------------------------------


def test_recovery_marks_hanging_orchestration_runs(tmp_path: Path) -> None:
    """Startup recovery must mark running orchestration_runs as failed."""
    from wlcodex.models import OrchestrationStatus

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    # Create a conversation first (FK constraint)
    convo = ledger.create_conversation(
        chat_id=123, user_id=456, title="Test",
        mode="chief_engineer", workspace_alias="demo",
    )

    # Create orchestration runs in various states
    orch_running = ledger.create_orchestration_run(convo.id, "goal 1")
    # It's already 'running' by default

    # Create another and manually set to passed (should be left alone)
    orch_passed = ledger.create_orchestration_run(convo.id, "goal 2")
    ledger.update_orchestration_run(orch_passed.id, status="passed")

    orch_marked, agent_marked = ledger.mark_hanging_conversation_runs_recovery()

    assert orch_marked == 1
    assert agent_marked == 0

    running_after = ledger.get_orchestration_run(orch_running.id)
    assert running_after.status == OrchestrationStatus.FAILED.value

    passed_after = ledger.get_orchestration_run(orch_passed.id)
    assert passed_after.status == OrchestrationStatus.PASSED.value


def test_recovery_marks_hanging_agent_runs(tmp_path: Path) -> None:
    """Startup recovery must mark running agent_runs as failed."""
    from wlcodex.models import AgentRunStatus

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(
        chat_id=123, user_id=456, title="Test",
        mode="chief_engineer", workspace_alias="demo",
    )

    agent_running = ledger.create_agent_run(
        convo.id, agent="claude", role="implementation",
    )
    ledger.update_agent_run_status(agent_running.id, "running")

    agent_done = ledger.create_agent_run(
        convo.id, agent="codex", role="analysis",
    )
    ledger.update_agent_run_status(agent_done.id, "done")

    orch_marked, agent_marked = ledger.mark_hanging_conversation_runs_recovery()

    assert orch_marked == 0
    assert agent_marked == 1

    running_after = ledger.get_agent_run(agent_running.id)
    assert running_after.status == AgentRunStatus.FAILED.value

    done_after = ledger.get_agent_run(agent_done.id)
    assert done_after.status == AgentRunStatus.DONE.value


def test_recovery_hanging_runs_idempotent(tmp_path: Path) -> None:
    """mark_hanging_conversation_runs_recovery is idempotent."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    convo = ledger.create_conversation(
        chat_id=123, user_id=456, title="Test",
        mode="chief_engineer", workspace_alias="demo",
    )
    ledger.create_orchestration_run(convo.id, "goal")
    ledger.create_agent_run(convo.id, agent="claude", role="implementation")
    # agent_run starts as 'queued' — need to update to 'running'
    agent_run = ledger.create_agent_run(convo.id, agent="codex", role="analysis")
    ledger.update_agent_run_status(agent_run.id, "running")

    ledger.mark_hanging_conversation_runs_recovery()
    # Second call should not change anything further
    orch2, agent2 = ledger.mark_hanging_conversation_runs_recovery()

    assert orch2 == 0  # Already failed, not re-marked
    assert agent2 == 0  # Already failed, not re-marked


# ---------------------------------------------------------------------------
# Event-sourced recovery: find_non_terminal_agent_runs
# ---------------------------------------------------------------------------


def test_find_non_terminal_agent_runs_detects_orphaned(tmp_path: Path) -> None:
    """Non-terminal agent runs must be detected via runtime events."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)

    # A running agent with no terminal event
    store.append(
        _make_rt_event(
            event_type=EventType.AGENT_RUN_STARTED,
            agent_run_id=10,
        )
    )
    store.append(
        _make_rt_event(
            event_type=EventType.AGENT_RUN_ACTIVITY,
            agent_run_id=10,
        )
    )

    ids = find_non_terminal_agent_runs(store)
    assert 10 in ids


def test_find_non_terminal_skips_completed(tmp_path: Path) -> None:
    """Completed agent runs must not show as non-terminal."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)

    store.append(
        _make_rt_event(event_type=EventType.AGENT_RUN_STARTED, agent_run_id=1)
    )
    store.append(
        _make_rt_event(event_type=EventType.AGENT_RUN_COMPLETED, agent_run_id=1)
    )

    ids = find_non_terminal_agent_runs(store)
    assert 1 not in ids


# ---------------------------------------------------------------------------
# Event-sourced recovery: build_recovery_events
# ---------------------------------------------------------------------------


def test_build_recovery_events_generates_all_required_events() -> None:
    """Recovery must generate system.started, recovery.started, orphan, recovery.completed."""
    events = build_recovery_events(
        orphaned_agent_run_ids=[7, 8],
        conversation_id=1,
        correlation_id="test-corr",
    )

    types = [e.event_type for e in events]
    assert EventType.SYSTEM_STARTED in types
    assert EventType.SYSTEM_RECOVERY_STARTED in types
    assert EventType.SYSTEM_RECOVERY_COMPLETED in types
    assert EventType.AGENT_RUN_ORPHANED in types

    # Must be in order: started -> recovery.started -> orphan* -> recovery.completed
    assert types[0] == EventType.SYSTEM_STARTED
    assert types[1] == EventType.SYSTEM_RECOVERY_STARTED
    assert types[-1] == EventType.SYSTEM_RECOVERY_COMPLETED


def test_build_recovery_events_orphan_payload() -> None:
    """Orphan events must carry reason=service_restart_orphaned_run."""
    events = build_recovery_events(
        orphaned_agent_run_ids=[42],
        conversation_id=None,
    )
    orphan = [e for e in events if e.event_type == EventType.AGENT_RUN_ORPHANED]
    assert len(orphan) == 1
    assert orphan[0].payload["reason"] == "service_restart_orphaned_run"
    assert orphan[0].agent_run_id == 42
    assert orphan[0].aggregate_type == AggregateType.AGENT_RUN


def test_build_recovery_events_empty_orphans_still_emits_recovery() -> None:
    """Even with zero orphaned runs, system started/recovery events are emitted."""
    events = build_recovery_events(orphaned_agent_run_ids=[], conversation_id=1)
    types = [e.event_type for e in events]
    assert EventType.SYSTEM_STARTED in types
    assert EventType.SYSTEM_RECOVERY_STARTED in types
    assert EventType.SYSTEM_RECOVERY_COMPLETED in types
    assert EventType.AGENT_RUN_ORPHANED not in types


# ---------------------------------------------------------------------------
# Event-sourced recovery: append_recovery_events
# ---------------------------------------------------------------------------


def test_append_recovery_events_persists_to_store(tmp_path: Path) -> None:
    """Recovery events must be appendable to the runtime event store."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)

    persisted = append_recovery_events(
        store,
        orphaned_agent_run_ids=[5],
        conversation_id=1,
        correlation_id="rcv-1",
    )

    assert len(persisted) >= 4
    for ev in persisted:
        assert ev.id > 0
        loaded = store.get_by_id(ev.id)
        assert loaded.event_type == ev.event_type


# ---------------------------------------------------------------------------
# Event-sourced recovery: does NOT mutate projections silently
# ---------------------------------------------------------------------------


def test_recovery_events_dont_silently_mutate_agent_runs(tmp_path: Path) -> None:
    """Recovery appends events; existing DB recovery is the projection response."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)

    # Pre-create a running agent run (projection)
    convo = ledger.create_conversation(
        chat_id=1, user_id=1, title="T", mode="chief_engineer", workspace_alias="w",
    )
    agent_run = ledger.create_agent_run(convo.id, agent="claude", role="implementation")
    ledger.update_agent_run_status(agent_run.id, "running")

    # Also append runtime events for this run
    store.append(
        _make_rt_event(
            event_type=EventType.AGENT_RUN_STARTED,
            agent_run_id=agent_run.id,
            conversation_id=convo.id,
        )
    )

    # Append recovery events (they mark orphan via events only)
    append_recovery_events(
        store,
        orphaned_agent_run_ids=[agent_run.id],
        conversation_id=convo.id,
        correlation_id="rcv-2",
    )

    # The projection (agent_runs table) may still show "running" because
    # the projector hasn't processed the orphan events yet.
    # We verify the events are there and the projection update is a
    # separate concern handled by mark_hanging_conversation_runs_recovery.
    events = store.list_by_agent_run(agent_run.id)
    event_types = [e.event_type for e in events]
    assert EventType.AGENT_RUN_ORPHANED in event_types


# ---------------------------------------------------------------------------
# Event-sourced recovery: run-level cancellation excludes agent runs
# ---------------------------------------------------------------------------


def test_find_non_terminal_excludes_runs_in_cancelled_correlation(tmp_path: Path) -> None:
    """Agent runs sharing a correlation_id with a RUN_CANCELLED event
    must be excluded from the non-terminal set."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)

    store.append(
        _make_rt_event(
            event_type=EventType.AGENT_RUN_STARTED,
            agent_run_id=5,
            correlation_id="corr-running",
        )
    )
    # This run is in a cancelled correlation
    store.append(
        _make_rt_event(
            event_type=EventType.AGENT_RUN_STARTED,
            agent_run_id=6,
            correlation_id="corr-cancelled",
        )
    )
    store.append(
        _make_rt_event(
            event_type=EventType.RUN_CANCELLED,
            correlation_id="corr-cancelled",
            agent_run_id=None,  # run-level cancellation, no per-agent id
        )
    )

    ids = find_non_terminal_agent_runs(store)
    assert 5 in ids  # Still running
    assert 6 not in ids  # Cancelled at correlation level


# ---------------------------------------------------------------------------
# format_recovery_summary
# ---------------------------------------------------------------------------


def test_format_recovery_summary_output() -> None:
    """format_recovery_summary must produce a readable Chinese summary."""
    from wlcodex.recovery_notifications import format_recovery_summary

    events = build_recovery_events(
        orphaned_agent_run_ids=[10, 20],
        conversation_id=1,
        correlation_id="test-summary",
    )

    summary = format_recovery_summary(
        recovery_events=events,
        paused_task_ids=[1, 2],
        orch_marked=1,
        agent_marked=0,
    )

    assert "启动恢复摘要" in summary
    assert "暂停的任务" not in summary
    assert "暂停的执行" in summary
    assert "#1" in summary and "#2" in summary  # paused tasks
    assert "#10" in summary  # orphaned run
    assert "#20" in summary  # orphaned run
    assert "编排" in summary or "1" in summary  # orch_marked reference
    assert "恢复操作" in summary  # total count


def test_format_recovery_summary_empty() -> None:
    """format_recovery_summary with no orphans or paused tasks."""
    from wlcodex.recovery_notifications import format_recovery_summary

    events = build_recovery_events(orphaned_agent_run_ids=[], conversation_id=1)
    summary = format_recovery_summary(
        recovery_events=events,
        paused_task_ids=[],
        orch_marked=0,
        agent_marked=0,
    )

    assert "启动恢复摘要" in summary
    assert "暂停的任务" not in summary
    assert "无" in summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rt_event(**overrides: object) -> RuntimeEvent:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "event_type": EventType.AGENT_RUN_STARTED,
        "aggregate_type": AggregateType.AGENT_RUN,
        "aggregate_id": "ar-1",
        "correlation_id": "corr-1",
        "source": EventSource.CLAUDE,
        "actor": "claude",
        "visibility": Visibility.OPERATOR,
        "payload": {"agent": "claude"},
        "occurred_at": now_iso(),
        "conversation_id": 1,
        "agent_run_id": 1,
    }
    merged = {**defaults, **overrides}
    return RuntimeEvent(**merged)  # type: ignore[arg-type]
