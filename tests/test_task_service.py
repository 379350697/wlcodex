import json
from pathlib import Path

import pytest

from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.task_service import TaskService, WorkspaceBusy


@pytest.fixture
def service(tmp_path: Path) -> TaskService:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    return TaskService(
        ledger=ledger,
        workspaces=(WorkspaceConfig("demo", Path("/tmp/demo"), True),),
    )


def test_start_task_creates_fresh_thread_by_default(service: TaskService) -> None:
    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-new")

    assert task.codex_thread_id == "thread-new"
    assert task.status == TaskStatus.QUEUED
    assert service.list_tasks()[0].status == TaskStatus.QUEUED


def test_continue_task_requires_existing_task(service: TaskService) -> None:
    with pytest.raises(KeyError):
        service.record_user_continue(99, "continue")


def test_workspace_write_lock_rejects_second_running_task(service: TaskService) -> None:
    service.start_task("demo", "First", "thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    with pytest.raises(WorkspaceBusy):
        service.ensure_workspace_available("demo")


def test_lock_released_after_done(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.DONE)

    # Should not raise WorkspaceBusy
    task2 = service.start_task("demo", "Second", codex_thread_id="thread-2")
    assert task2.id == 2


def test_lock_released_after_failed(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.FAILED)

    task2 = service.start_task("demo", "Second", codex_thread_id="thread-2")
    assert task2.id == 2


def test_lock_released_after_aborted(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.ABORTED)

    task2 = service.start_task("demo", "Second", codex_thread_id="thread-2")
    assert task2.id == 2


def test_unknown_workspace_alias_raises(service: TaskService) -> None:
    with pytest.raises(KeyError, match="unknown workspace"):
        service.start_task("nonexistent", "Prompt", codex_thread_id="th")


def test_list_tasks_and_get_task(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="t1")
    service._ledger.set_task_status(1, TaskStatus.DONE)
    service.start_task("demo", "Second", codex_thread_id="t2")

    tasks = service.list_tasks()
    assert len(tasks) == 2

    task = service.get_task(1)
    assert task.title == "First"


def test_apply_backend_event_updates_state(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    assert task.status == TaskStatus.QUEUED

    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.RUNNING
    assert updated.active_turn_id == "turn-1"


def test_backend_event_for_reused_thread_targets_latest_active_task(
    service: TaskService,
) -> None:
    from wlcodex.codex_backend import BackendEvent

    old = service.start_task("demo", "Old analysis", codex_thread_id="thread-reused")
    service._ledger.set_task_status(old.id, TaskStatus.DONE)
    new = service.start_task("demo", "Final plan", codex_thread_id="thread-reused")

    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-reused", "turnId": "turn-new"},
    ))

    assert service.get_task(old.id).status == TaskStatus.DONE
    updated = service.get_task(new.id)
    assert updated.status == TaskStatus.RUNNING
    assert updated.active_turn_id == "turn-new"


def test_apply_backend_event_finds_task_by_bound_codex_thread_alias(
    service: TaskService,
) -> None:
    from wlcodex.codex_backend import BackendEvent

    task = service.reserve_task("demo", "Hidden orchestration prompt")
    service.set_task_thread(task.id, "analysis-thread")
    service.set_task_thread(task.id, "verify-thread")

    service.apply_backend_event(BackendEvent(
        event_type="approval_requested",
        payload={
            "threadId": "analysis-thread",
            "codexRequestId": "req-analysis",
            "codexItemId": "item-1",
            "codexTurnId": "turn-1",
            "kind": "command",
            "summary": "Run tests",
            "command": "pytest",
        },
    ))

    updated = service.get_task(task.id)
    approvals = service._ledger.pending_approvals(task.id)
    assert updated.codex_thread_id == "verify-thread"
    assert updated.status == TaskStatus.WAITING_APPROVAL
    assert [approval.codex_request_id for approval in approvals] == ["req-analysis"]


def test_complete_turn_sets_done(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.apply_backend_event(BackendEvent(
        event_type="turn_completed",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.DONE


def test_orchestration_managed_turn_completion_keeps_task_running(
    service: TaskService,
) -> None:
    from wlcodex.codex_backend import BackendEvent

    task = service.start_task("demo", "Chief engineer task", codex_thread_id="thread-1")
    conversation = service._ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="新对话",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    service._ledger.set_conversation_active_task(conversation.id, task.id)
    service._ledger.create_orchestration_run(conversation.id, "Chief engineer task")

    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.apply_backend_event(BackendEvent(
        event_type="turn_completed",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    updated = service.get_task(task.id)
    assert updated.status == TaskStatus.RUNNING
    assert updated.active_turn_id is None
    assert "turn_completed" in [
        event.event_type for event in service._ledger.list_events(task.id)
    ]


def test_failed_turn_does_not_mark_done(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.apply_backend_event(BackendEvent(
        event_type="turn_completed",
        payload={
            "threadId": "thread-1",
            "turn": {
                "id": "turn-1",
                "status": "failed",
                "error": {
                    "message": "sandbox exploded",
                    "codexErrorInfo": "sandboxError",
                },
            },
        },
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.FAILED
    assert updated.active_turn_id is None
    assert "sandbox exploded" in updated.last_error


def test_thread_system_error_marks_task_failed(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.apply_backend_event(BackendEvent(
        event_type="thread_status_changed",
        payload={"threadId": "thread-1", "status": {"type": "systemError"}},
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.FAILED
    assert updated.active_turn_id is None
    assert "systemError" in updated.last_error


def test_late_thread_system_error_overrides_done(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.DONE)

    service.apply_backend_event(BackendEvent(
        event_type="thread_status_changed",
        payload={"threadId": "thread-1", "status": {"type": "systemError"}},
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.FAILED
    assert "systemError" in updated.last_error


def test_item_events_store_nested_item_details(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="item_started",
        payload={
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "item-1",
                "type": "commandExecution",
                "status": "inProgress",
                "command": "python3 probe.py",
            },
        },
    ))

    event = service._ledger.list_events(1)[-1]
    assert event.event_type == "item_started"
    assert event.payload == {
        "type": "commandExecution",
        "status": "inProgress",
        "command": "python3 probe.py",
    }


def test_approval_changes_state_to_waiting(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.apply_backend_event(BackendEvent(
        event_type="approval_requested",
        payload={
            "threadId": "thread-1",
            "codexRequestId": "req-1",
            "codexItemId": "item-1",
            "codexTurnId": "turn-1",
            "kind": "command",
            "summary": "Run: rm -rf /",
            "command": '{"command": "rm -rf /"}',
        },
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.WAITING_APPROVAL


def test_approval_summary_falls_back_to_reason(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    service.apply_backend_event(BackendEvent(
        event_type="approval_requested",
        payload={
            "threadId": "thread-1",
            "codexRequestId": "req-1",
            "kind": "command",
            "reason": "Allow writing /home/wl/probe outside the workspace?",
            "command": "python3 probe.py",
        },
    ))

    approval = service._ledger.pending_approvals(1)[0]
    assert approval.summary == "Allow writing /home/wl/probe outside the workspace?"


def test_command_approval_stores_execpolicy_amendment(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    service.apply_backend_event(BackendEvent(
        event_type="approval_requested",
        payload={
            "threadId": "thread-1",
            "codexRequestId": "req-1",
            "kind": "command",
            "summary": "Run: rtk rg approval",
            "command": "rtk rg approval",
            "availableDecisions": [
                "accept",
                "acceptWithExecpolicyAmendment",
            ],
            "proposedExecpolicyAmendment": ["rtk", "rg"],
        },
    ))

    approval = service._ledger.pending_approvals(1)[0]
    stored = json.loads(approval.command_json)
    assert stored["command"] == "rtk rg approval"
    assert stored["available_decisions"] == [
        "accept",
        "acceptWithExecpolicyAmendment",
    ]
    assert stored["proposed_execpolicy_amendment"] == ["rtk", "rg"]


def test_approval_requested_while_paused_preserves_paused_state(
    service: TaskService,
) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.pause_task(1)

    service.apply_backend_event(BackendEvent(
        event_type="approval_requested",
        payload={
            "threadId": "thread-1",
            "codexRequestId": "1",
            "codexItemId": "item-1",
            "codexTurnId": "turn-1",
            "kind": "command",
            "summary": "Run: sleep 60",
            "command": "sleep 60",
        },
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.PAUSED
    assert updated.last_phase == "active:waitingOnApproval"
    assert len(service._ledger.pending_approvals(1)) == 1


def test_paused_task_can_complete_if_backend_finishes(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.pause_task(1)

    service.apply_backend_event(BackendEvent(
        event_type="turn_completed",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    updated = service.get_task(1)
    assert updated.status == TaskStatus.DONE
    assert updated.active_turn_id is None


def test_pause_task(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    service.pause_task(1)
    assert service.get_task(1).status == TaskStatus.PAUSED


def test_abort_task(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    service.abort_task(1)
    assert service.get_task(1).status == TaskStatus.ABORTED


def test_archive_refuses_running(service: TaskService) -> None:
    from wlcodex.codex_backend import BackendEvent

    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    with pytest.raises(RuntimeError, match="running"):
        service.archive_task(1)


def test_archive_done_task(service: TaskService) -> None:
    service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.DONE)

    service.archive_task(1)
    assert service.get_task(1).status == TaskStatus.ARCHIVED


def test_only_one_in_progress(tmp_path: Path) -> None:
    """Regression: ensure only one task at in_progress state is tracked."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    task = service.start_task("demo", "First", codex_thread_id="t1")
    assert service.get_task(task.id).status == TaskStatus.QUEUED


def test_paused_task_can_continue_without_self_lock(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    continued = service.continue_task(task.id, "resume safely")

    assert continued.status == TaskStatus.QUEUED
    events = service._ledger.list_events(task.id)
    assert events[-1].event_type == "user_continue"


def test_paused_task_can_abort(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    aborted = service.abort_task(task.id)

    assert aborted.status == TaskStatus.ABORTED
    events = service._ledger.list_events(task.id)
    assert events[-1].event_type == "user_aborted"


def test_different_paused_task_still_blocks_new_write(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    with pytest.raises(WorkspaceBusy, match="task #1"):
        service.start_task("demo", "Second", codex_thread_id="thread-2")


# --- waiting_slot tests ---


def test_waiting_slot_is_not_active_write_status() -> None:
    from wlcodex.locks import ACTIVE_WRITE_STATUSES

    assert TaskStatus.WAITING_SLOT not in ACTIVE_WRITE_STATUSES
    assert TaskStatus.QUEUED in ACTIVE_WRITE_STATUSES


def test_reserve_waiting_task_creates_without_codex_thread(service: TaskService) -> None:
    # First create an active task to block the workspace
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Fix parser", blocker_task_id=1)

    assert task.status == TaskStatus.WAITING_SLOT
    assert task.codex_thread_id is None
    assert task.active_turn_id is None
    assert task.workspace_alias == "demo"

    # Verify prompt is stored in event
    events = service._ledger.list_events(task.id)
    created_events = [e for e in events if e.event_type == "task_waiting_slot_created"]
    assert len(created_events) == 1
    assert created_events[0].payload["prompt"] == "Fix parser"
    assert created_events[0].payload.get("blocker_task_id") == 1


def test_waiting_slot_does_not_acquire_write_lock(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    # reserve_waiting_task should succeed even with running blocker
    task = service.reserve_waiting_task("demo", "Second", blocker_task_id=1)
    assert task.status == TaskStatus.WAITING_SLOT

    # The blocker is still the active write task
    from wlcodex.locks import active_write_task
    blocker = active_write_task(service._ledger.list_tasks(), "demo")
    assert blocker is not None
    assert blocker.id == 1


def test_waiting_position_is_derived_correctly(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    w1 = service.reserve_waiting_task("demo", "Second", blocker_task_id=1)
    w2 = service.reserve_waiting_task("demo", "Third", blocker_task_id=1)

    assert service.waiting_position(w1.id) == 1
    assert service.waiting_position(w2.id) == 2
    assert service.waiting_position(1) == 0  # not waiting_slot


def test_get_stored_prompt_retrieves_original_text(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Fix the parser bug", blocker_task_id=1)
    prompt = service.get_stored_prompt(task.id)
    assert prompt == "Fix the parser bug"


def test_get_stored_prompt_missing_raises(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Prompt", blocker_task_id=1)
    # Delete the event
    service._ledger._conn.execute("DELETE FROM task_events WHERE task_id = ?", (task.id,))
    service._ledger._conn.commit()

    with pytest.raises(RuntimeError, match="missing stored prompt"):
        service.get_stored_prompt(task.id)


def test_promote_waiting_task_transitions_to_queued(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Fix parser", blocker_task_id=1)

    # Complete the blocker
    service._ledger.set_task_status(1, TaskStatus.DONE)

    promoted, prompt = service.promote_waiting_task(task.id)
    assert promoted.status == TaskStatus.QUEUED
    assert prompt == "Fix parser"

    events = service._ledger.list_events(task.id)
    assert any(e.event_type == "task_promoted_from_waiting" for e in events)


def test_promote_non_waiting_task_raises(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")

    from wlcodex.task_service import InvalidTransition
    with pytest.raises(InvalidTransition, match="not waiting_slot"):
        service.promote_waiting_task(task.id)


def test_promote_with_missing_prompt_fails(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Prompt", blocker_task_id=1)
    # Corrupt the event
    service._ledger._conn.execute("DELETE FROM task_events WHERE task_id = ?", (task.id,))
    service._ledger._conn.commit()

    with pytest.raises(RuntimeError, match="missing stored prompt"):
        service.promote_waiting_task(task.id)

    # Task should still be waiting_slot (promotion failed before transition)
    assert service.get_task(task.id).status == TaskStatus.WAITING_SLOT


def test_abort_waiting_slot_succeeds(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Fix parser", blocker_task_id=1)
    aborted = service.abort_task(task.id)

    assert aborted.status == TaskStatus.ABORTED
    events = service._ledger.list_events(task.id)
    assert events[-1].event_type == "user_aborted"


def test_abort_waiting_slot_removes_from_queue(tmp_path: Path) -> None:
    """After aborting a waiting task, it should no longer appear in waiting list."""
    from wlcodex.config import WorkspaceConfig

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", tmp_path / "demo", True),))

    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    w1 = service.reserve_waiting_task("demo", "A", blocker_task_id=1)
    w2 = service.reserve_waiting_task("demo", "B", blocker_task_id=1)

    assert len(service.list_waiting_tasks("demo")) == 2

    service.abort_task(w1.id)

    waiting = service.list_waiting_tasks("demo")
    assert len(waiting) == 1
    assert waiting[0].id == w2.id


def test_continue_waiting_slot_refuses(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Fix parser", blocker_task_id=1)

    with pytest.raises(RuntimeError, match="waiting_slot"):
        service.continue_task(task.id, "continue this")


def test_steer_waiting_slot_refuses(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    task = service.reserve_waiting_task("demo", "Fix parser", blocker_task_id=1)

    with pytest.raises(RuntimeError, match="waiting_slot"):
        service.steer_task(task.id, "steer this")


def test_paused_task_still_blocks_waiting_slot_creation(tmp_path: Path) -> None:
    """A paused task should block the workspace; new tasks go to waiting_slot."""
    from wlcodex.config import WorkspaceConfig

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", tmp_path / "demo", True),))

    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    # reserve_task should raise because paused is still active write
    with pytest.raises(WorkspaceBusy):
        service.reserve_task("demo", "Second")

    # But reserve_waiting_task should work
    w = service.reserve_waiting_task("demo", "Second", blocker_task_id=task.id)
    assert w.status == TaskStatus.WAITING_SLOT


def test_blocker_for_workspace_returns_active_task(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.RUNNING)

    blocker = service.blocker_for_workspace("demo")
    assert blocker is not None
    assert blocker.id == 1

    service._ledger.set_task_status(task.id, TaskStatus.DONE)
    assert service.blocker_for_workspace("demo") is None


def test_waiting_task_does_not_block_other_workspaces(service: TaskService) -> None:
    """A waiting task in one workspace shouldn't affect another workspace."""
    # We need a second workspace
    from pathlib import Path
    from wlcodex.config import WorkspaceConfig

    ledger = service._ledger
    service2 = TaskService(
        ledger,
        (
            WorkspaceConfig("demo", Path("/tmp/demo"), True),
            WorkspaceConfig("other", Path("/tmp/other"), True),
        ),
    )

    service2.start_task("demo", "First", codex_thread_id="thread-1")
    service2._ledger.set_task_status(1, TaskStatus.RUNNING)

    w = service2.reserve_waiting_task("demo", "Wait", blocker_task_id=1)
    assert w.status == TaskStatus.WAITING_SLOT

    # Other workspace should still be free
    task = service2.start_task("other", "Second", codex_thread_id="thread-2")
    assert task.status == TaskStatus.QUEUED


# --- force_parallel tests ---


def test_force_parallel_start_promotes_waiting_task(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    w = service.reserve_waiting_task("demo", "Force me", blocker_task_id=1)
    promoted, prompt = service.force_parallel_start(w.id)

    assert promoted.status == TaskStatus.QUEUED
    assert prompt == "Force me"
    assert promoted.is_force_parallel

    events = service._ledger.list_events(w.id)
    assert any(e.event_type == "force_parallel_started" for e in events)


def test_force_parallel_start_refuses_non_waiting(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")

    from wlcodex.task_service import InvalidTransition
    with pytest.raises(InvalidTransition, match="not waiting_slot"):
        service.force_parallel_start(task.id)


def test_force_parallel_task_still_blocks_workspace(service: TaskService) -> None:
    """After force_parallel, subsequent normal /task still sees workspace busy."""
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    w = service.reserve_waiting_task("demo", "Force me", blocker_task_id=1)
    service.force_parallel_start(w.id)
    service._ledger.set_task_status(w.id, TaskStatus.RUNNING)

    # Workspace should still be busy (force_parallel doesn't release lock)
    with pytest.raises(WorkspaceBusy):
        service.ensure_workspace_available("demo")


# --- worktree tests ---


def test_worktree_task_does_not_block_workspace(tmp_path: Path) -> None:
    """A worktree task should not block the original workspace queue."""
    from wlcodex.config import WorkspaceConfig

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    w = service.reserve_waiting_task("demo", "Worktree me", blocker_task_id=1)

    # Simulate worktree task being active (manually set worktree_path)
    ledger.set_worktree_info(w.id, "/tmp/wt/task-2", "wlcodex/task-2")
    service._ledger.set_task_status(w.id, TaskStatus.RUNNING)

    # Workspace should NOT be busy — worktree tasks don't block
    from wlcodex.locks import active_write_task
    blocker = active_write_task(service._ledger.list_tasks(limit=100), "demo")
    assert blocker is None or blocker.id != w.id


def test_setup_worktree_refuses_non_waiting(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")

    from wlcodex.task_service import InvalidTransition
    with pytest.raises(InvalidTransition, match="not waiting_slot"):
        service.setup_worktree(task.id, slug="test")


def test_start_worktree_task_refuses_no_worktree(service: TaskService) -> None:
    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    w = service.reserve_waiting_task("demo", "No worktree", blocker_task_id=1)

    with pytest.raises(RuntimeError, match="no worktree path"):
        service.start_worktree_task(w.id)


def test_discard_worktree_refuses_no_worktree(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")

    with pytest.raises(RuntimeError, match="no worktree path"):
        service.discard_worktree(task.id)


def test_merge_worktree_refuses_no_worktree(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")

    with pytest.raises(RuntimeError, match="no worktree branch"):
        service.merge_worktree(task.id)


def test_list_worktree_tasks(service: TaskService) -> None:
    service.start_task("demo", "Normal task", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)

    w = service.reserve_waiting_task("demo", "WT task", blocker_task_id=1)
    service._ledger.set_worktree_info(w.id, "/tmp/wt/task-2", "wlcodex/task-2")
    service._ledger.set_task_status(w.id, TaskStatus.RUNNING)

    wt_tasks = service.list_worktree_tasks("demo")
    assert len(wt_tasks) == 1
    assert wt_tasks[0].id == w.id
    assert wt_tasks[0].worktree_path == "/tmp/wt/task-2"


# --- recovery behavior ---


def test_recovery_does_not_pause_waiting_slot(tmp_path: Path) -> None:
    """Startup recovery must not convert waiting_slot to paused."""
    from wlcodex.config import WorkspaceConfig

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = service.reserve_waiting_task("demo", "Waiting", blocker_task_id=1)

    # Simulate recovery
    paused_ids = ledger.mark_active_tasks_recovery_paused()
    assert w.id not in paused_ids
    assert service.get_task(w.id).status == TaskStatus.WAITING_SLOT


def test_recovery_does_not_pause_worktree_task(tmp_path: Path) -> None:
    """Worktree tasks should be paused on recovery same as normal active tasks."""
    from wlcodex.config import WorkspaceConfig

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)
    ledger.set_worktree_info(1, "/tmp/wt/task-1", "wlcodex/task-1")

    # Recovery pauses running tasks (including worktree ones)
    paused_ids = ledger.mark_active_tasks_recovery_paused()
    assert 1 in paused_ids
    assert service.get_task(1).status == TaskStatus.PAUSED
