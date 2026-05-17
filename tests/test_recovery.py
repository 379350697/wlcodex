"""Daemon restart recovery tests."""

from pathlib import Path

from wlcodex.db import Ledger
from wlcodex.models import TaskStatus


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
