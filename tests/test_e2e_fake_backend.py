"""End-to-end cockpit acceptance tests using fake backend."""

from pathlib import Path

import pytest

from wlcodex.approval import ApprovalService, decode_approval_callback, encode_approval_callback
from wlcodex.codex_backend import BackendEvent, FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.models import TaskStatus
from wlcodex.task_service import TaskService


@pytest.fixture
def ctrl(tmp_path: Path) -> CommandController:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    return CommandController(service, backend, inspector, ledger=ledger)


# ---------------------------------------------------------------------------
# Full lifecycle: /task → running → approval → done → /tasks → /archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_task_lifecycle_with_approval(ctrl: CommandController) -> None:
    # 1. Start task
    response = await ctrl.handle("/task demo Fix the health timeout", {"chat_id": 123})
    assert "任务 #1" in response.text

    task = ctrl._service.get_task(1)
    assert task.status == TaskStatus.QUEUED
    thread_id = task.codex_thread_id

    # 2. Backend emits turn_started → task becomes running
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": thread_id, "turnId": "turn-1"},
    ))
    assert ctrl._service.get_task(1).status == TaskStatus.RUNNING
    assert ctrl._service.get_task(1).active_turn_id == "turn-1"

    # 3. Backend emits plan update
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="plan_updated",
        payload={"threadId": thread_id, "plan": "Step 1: check; Step 2: fix"},
    ))
    assert ctrl._service.get_task(1).last_phase == "planning"

    # 4. Backend requests approval
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="approval_requested",
        payload={
            "threadId": thread_id,
            "codexRequestId": "req-1",
            "codexItemId": "item-1",
            "codexTurnId": "turn-1",
            "kind": "command",
            "summary": "Run: rm -rf /tmp/test",
            "command": '{"command": "rm -rf /tmp/test"}',
        },
    ))
    assert ctrl._service.get_task(1).status == TaskStatus.WAITING_APPROVAL

    # Resolve approval
    ledger = ctrl._service._ledger
    approvals = ledger.pending_approvals(1)
    assert len(approvals) == 1

    backend = ctrl._backend
    approval_svc = ApprovalService()
    cb = decode_approval_callback(
        encode_approval_callback(approvals[0].id, "approve_once")
    )
    msg = await approval_svc.resolve_callback(cb, backend, ledger)
    assert "已处理" in msg
    assert backend._approval_resolutions == [("req-1", {"decision": "accept"})]

    # 5. Backend emits turn_completed → task becomes done
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_completed",
        payload={"threadId": thread_id, "turnId": "turn-1"},
    ))
    assert ctrl._service.get_task(1).status == TaskStatus.DONE

    # 6. /tasks shows the done task
    response = await ctrl.handle("/tasks", {})
    assert "#1" in response.text
    assert "已完成" in response.text

    # 7. /archive archives the task
    response = await ctrl.handle("/archive 1", {})
    assert "已归档" in response.text
    assert ctrl._service.get_task(1).status == TaskStatus.ARCHIVED


# ---------------------------------------------------------------------------
# /continue uses same thread id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_uses_same_thread_id(ctrl: CommandController) -> None:
    # Start via controller so a turn is recorded
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    await ctrl.handle("/continue 1 Do more work", {})

    task = ctrl._service.get_task(1)
    assert task.codex_thread_id is not None

    # continue_turn should have added another turn
    assert len(ctrl._backend.turns) >= 2


# ---------------------------------------------------------------------------
# /steer refuses after done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_refuses_after_done_and_says_use_continue(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Done task", codex_thread_id="thread-1")
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    response = await ctrl.handle("/steer 1 Try steering done task", {})
    assert "/continue" in response.text or "活跃 turn" in response.text
    assert len(ctrl._backend.steers) == 0


# ---------------------------------------------------------------------------
# /archive task after done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_done_task(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Done task", codex_thread_id="thread-1")
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    response = await ctrl.handle("/archive 1", {})
    assert "已归档" in response.text
    assert ctrl._service.get_task(1).status == TaskStatus.ARCHIVED


# ---------------------------------------------------------------------------
# Second task on same workspace after done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_task_on_same_workspace_after_done(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "First task", codex_thread_id="thread-1")
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    response = await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert "任务 #2" in response.text  # Should create new task, not fail


# ---------------------------------------------------------------------------
# waiting_slot controller tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busy_workspace_creates_waiting_slot_not_error(ctrl: CommandController) -> None:
    """When workspace is busy, /task creates a waiting_slot instead of an error."""
    # Start first task
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    # Second task should create waiting_slot
    response = await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert "等待工作区空闲" in response.text
    assert "阻塞者" in response.text
    assert "#1" in response.text  # blocker id
    assert "队列位置" in response.text

    task2 = ctrl._service.get_task(2)
    assert task2.status == TaskStatus.WAITING_SLOT
    assert task2.codex_thread_id is None

    # Backend.create_thread should NOT have been called for task 2
    assert len(ctrl._backend.threads) == 1


@pytest.mark.asyncio
async def test_show_waiting_task_displays_blocker_and_queue(ctrl: CommandController) -> None:
    """Show waiting task shows blocker id and queue position."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    response = await ctrl.handle("/task 2", {})
    assert "等待中" in response.text or "waiting_slot" in response.text
    assert "#1" in response.text  # blocker id
    assert "队列位置" in response.text


@pytest.mark.asyncio
async def test_abort_waiting_task_succeeds(ctrl: CommandController) -> None:
    """Abort a waiting_slot task."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert ctrl._service.get_task(2).status == TaskStatus.WAITING_SLOT

    response = await ctrl.handle("/abort 2", {})
    assert "已中止" in response.text
    assert ctrl._service.get_task(2).status == TaskStatus.ABORTED


@pytest.mark.asyncio
async def test_continue_waiting_task_returns_error(ctrl: CommandController) -> None:
    """Continue a waiting_slot task should return clear error."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    response = await ctrl.handle("/continue 2 Do work", {})
    assert "waiting_slot" in response.text or "等待中" in response.text or "错误" in response.text


@pytest.mark.asyncio
async def test_steer_waiting_task_returns_error(ctrl: CommandController) -> None:
    """Steer a waiting_slot task should return clear error."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    response = await ctrl.handle("/steer 2 Do work", {})
    assert "waiting_slot" in response.text or "等待中" in response.text or "错误" in response.text


@pytest.mark.asyncio
async def test_drain_on_active_task_done(ctrl: CommandController) -> None:
    """When active task completes, waiting task should auto-start."""
    # Start first task
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    assert ctrl._service.get_task(1).status == TaskStatus.QUEUED
    thread_id = ctrl._service.get_task(1).codex_thread_id
    assert thread_id is not None

    # Set first task to running
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": thread_id, "turnId": "turn-1"},
    ))
    assert ctrl._service.get_task(1).status == TaskStatus.RUNNING

    # Create waiting task while first is running
    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert ctrl._service.get_task(2).status == TaskStatus.WAITING_SLOT

    # Complete first task via backend event
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_completed",
        payload={"threadId": thread_id, "turnId": "turn-1"},
    ))
    assert ctrl._service.get_task(1).status == TaskStatus.DONE

    # Now drain should have promoted task 2 to queued
    assert ctrl._service.get_task(2).status == TaskStatus.WAITING_SLOT

    # drain_workspace is called asynchronously by event_bridge.
    # Here we test the service-level promotion:
    from wlcodex.task_service import drain_workspace
    promoted = await drain_workspace(ctrl._service, ctrl._backend, "demo")
    assert promoted is not None
    assert promoted.id == 2
    assert promoted.status == TaskStatus.QUEUED
    assert promoted.codex_thread_id is not None


@pytest.mark.asyncio
async def test_drain_on_abort_active_task(ctrl: CommandController) -> None:
    """After aborting the active task, waiting task should auto-start."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    # Create waiting task
    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert ctrl._service.get_task(2).status == TaskStatus.WAITING_SLOT

    # Abort the blocker - drain_workspace is called by controller
    response = await ctrl.handle("/abort 1", {})
    assert "已中止" in response.text

    # Task 2 should have been promoted and started
    task2 = ctrl._service.get_task(2)
    assert task2.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert task2.codex_thread_id is not None


@pytest.mark.asyncio
async def test_paused_task_blocks_auto_drain(ctrl: CommandController) -> None:
    """Auto drain should NOT promote waiting tasks when blocker is paused."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.PAUSED)

    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert ctrl._service.get_task(2).status == TaskStatus.WAITING_SLOT

    # drain should not promote because paused still owns workspace
    from wlcodex.task_service import drain_workspace
    result = await drain_workspace(ctrl._service, ctrl._backend, "demo")
    assert result is None
    assert ctrl._service.get_task(2).status == TaskStatus.WAITING_SLOT


@pytest.mark.asyncio
async def test_drain_does_nothing_when_no_waiting_tasks(ctrl: CommandController) -> None:
    """drain_workspace is a no-op when queue is empty."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    from wlcodex.task_service import drain_workspace
    result = await drain_workspace(ctrl._service, ctrl._backend, "demo")
    assert result is None


@pytest.mark.asyncio
async def test_drain_with_missing_prompt_marks_failed(ctrl: CommandController) -> None:
    """When a waiting task has no stored prompt, drain fails it."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    # Corrupt the stored prompt
    ctrl._service._ledger._conn.execute(
        "DELETE FROM task_events WHERE task_id = ?", (2,)
    )
    ctrl._service._ledger._conn.commit()

    # Complete blocker
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    from wlcodex.task_service import drain_workspace
    result = await drain_workspace(ctrl._service, ctrl._backend, "demo")
    assert result is None  # promotion failed
    assert ctrl._service.get_task(2).status == TaskStatus.FAILED
    assert "missing stored prompt" in ctrl._service.get_task(2).last_error


@pytest.mark.asyncio
async def test_approval_callback_still_works(ctrl: CommandController) -> None:
    """Existing approval callbacks must not be broken by waiting_slot changes."""
    # Full approval lifecycle
    await ctrl.handle("/task demo Approve me", {"chat_id": 123})
    thread_id = ctrl._service.get_task(1).codex_thread_id

    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": thread_id, "turnId": "turn-1"},
    ))

    ctrl._service.apply_backend_event(BackendEvent(
        event_type="approval_requested",
        payload={
            "threadId": thread_id,
            "codexRequestId": "req-1",
            "codexItemId": "item-1",
            "codexTurnId": "turn-1",
            "kind": "command",
            "summary": "Run: rm -rf /tmp/test",
            "command": '{"command": "rm -rf /tmp/test"}',
        },
    ))
    assert ctrl._service.get_task(1).status == TaskStatus.WAITING_APPROVAL

    # Resolve approval
    from wlcodex.approval import ApprovalService, decode_approval_callback, encode_approval_callback

    approvals = ctrl._service._ledger.pending_approvals(1)
    assert len(approvals) == 1

    approval_svc = ApprovalService()
    cb = decode_approval_callback(
        encode_approval_callback(approvals[0].id, "approve_once")
    )
    msg = await approval_svc.resolve_callback(cb, ctrl._backend, ctrl._service._ledger)
    assert "已处理" in msg
    assert ctrl._backend._approval_resolutions == [("req-1", {"decision": "accept"})]


# ---------------------------------------------------------------------------
# Waiting callback controller tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waiting_task_response_has_buttons(ctrl: CommandController) -> None:
    """When workspace is busy, the response must include inline keyboard buttons."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)

    response = await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert len(response.buttons) > 0
    button_labels = [b["text"] for row in response.buttons for b in row]
    assert "保留排队" in button_labels
    assert "查看阻塞任务" in button_labels
    assert "Continue 阻塞任务" in button_labels
    assert "中止阻塞并启动队首" in button_labels
    assert "危险：同目录并行" in button_labels
    assert "隔离 worktree 并行" in button_labels


@pytest.mark.asyncio
async def test_keep_callback_returns_same_state(ctrl: CommandController) -> None:
    """Keep callback is a no-op that returns the task card with buttons."""
    from wlcodex.waiting_callback import KEEP, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    cb = WaitingCallback(task_id=2, action=KEEP)
    response = await ctrl.handle_waiting_callback(cb)
    assert "保持排队" in response.text


@pytest.mark.asyncio
async def test_show_blocker_returns_blocker_card(ctrl: CommandController) -> None:
    """Show blocker returns the blocking task's card."""
    from wlcodex.waiting_callback import SHOW_BLOCKER, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    cb = WaitingCallback(task_id=2, action=SHOW_BLOCKER)
    response = await ctrl.handle_waiting_callback(cb)
    assert "阻塞者" in response.text
    assert "任务 #1" in response.text


@pytest.mark.asyncio
async def test_continue_blocker_shows_hint(ctrl: CommandController) -> None:
    """Continue blocker shows /continue hint to user."""
    from wlcodex.waiting_callback import CONTINUE_BLOCKER, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    cb = WaitingCallback(task_id=2, action=CONTINUE_BLOCKER)
    response = await ctrl.handle_waiting_callback(cb)
    assert "/continue 1" in response.text


@pytest.mark.asyncio
async def test_show_blocker_when_blocker_gone(ctrl: CommandController) -> None:
    """Show blocker when blocker has already ended."""
    from wlcodex.waiting_callback import SHOW_BLOCKER, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    cb = WaitingCallback(task_id=2, action=SHOW_BLOCKER)
    response = await ctrl.handle_waiting_callback(cb)
    assert "阻塞者已结束" in response.text


@pytest.mark.asyncio
async def test_abort_blocker_and_start_next_full_flow(ctrl: CommandController) -> None:
    """Abort blocker flow: confirmation step then actual abort."""
    from wlcodex.waiting_callback import (
        ABORT_BLOCKER_CONFIRM, ABORT_BLOCKER_START_NEXT, WaitingCallback,
    )

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    assert ctrl._service.get_task(2).status == TaskStatus.WAITING_SLOT

    # Step 1: request abort — shows confirmation card
    cb1 = WaitingCallback(task_id=2, action=ABORT_BLOCKER_START_NEXT)
    response = await ctrl.handle_waiting_callback(cb1)
    assert "确认中止" in response.text
    assert ctrl._service.get_task(1).status == TaskStatus.RUNNING  # not yet aborted

    # Step 2: confirm — actually aborts
    cb2 = WaitingCallback(task_id=2, action=ABORT_BLOCKER_CONFIRM)
    response = await ctrl.handle_waiting_callback(cb2)
    assert "已中止" in response.text
    assert ctrl._service.get_task(1).status == TaskStatus.ABORTED

    # Task 2 should have been promoted and started
    task2 = ctrl._service.get_task(2)
    assert task2.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert task2.codex_thread_id is not None

    # Verify events recorded
    events = ctrl._service._ledger.list_events(2)
    assert any(e.event_type == "queue_blocker_abort_requested" for e in events)
    assert any(e.event_type == "queue_blocker_aborted" for e in events)
    assert any(e.event_type == "queue_drained" for e in events)


@pytest.mark.asyncio
async def test_abort_blocker_when_blocker_already_ended(ctrl: CommandController) -> None:
    """Abort blocker button when blocker is already done — should drain directly."""
    from wlcodex.waiting_callback import ABORT_BLOCKER_START_NEXT, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    # Blocker ends before callback
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    cb = WaitingCallback(task_id=2, action=ABORT_BLOCKER_START_NEXT)
    response = await ctrl.handle_waiting_callback(cb)
    assert "阻塞者已结束" in response.text

    task2 = ctrl._service.get_task(2)
    assert task2.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)


@pytest.mark.asyncio
async def test_force_parallel_request_shows_warning_with_confirm_button(ctrl: CommandController) -> None:
    """Force parallel request must show danger warning and confirm button."""
    from wlcodex.waiting_callback import FORCE_PARALLEL_REQUEST, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    cb = WaitingCallback(task_id=2, action=FORCE_PARALLEL_REQUEST)
    response = await ctrl.handle_waiting_callback(cb)
    assert "危险操作" in response.text or "同目录并行" in response.text

    # Must have confirm and cancel buttons
    button_labels = [b["text"] for row in response.buttons for b in row]
    assert any("确认并行" in label for label in button_labels)
    assert "取消" in button_labels

    # Verify event recorded
    events = ctrl._service._ledger.list_events(2)
    assert any(e.event_type == "force_parallel_requested" for e in events)


@pytest.mark.asyncio
async def test_force_parallel_confirm_starts_task(ctrl: CommandController) -> None:
    """Force parallel confirm should start the task in the same workspace."""
    from wlcodex.waiting_callback import FORCE_PARALLEL_CONFIRM, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    cb = WaitingCallback(task_id=2, action=FORCE_PARALLEL_CONFIRM)
    response = await ctrl.handle_waiting_callback(cb)
    assert "强制并行启动" in response.text

    task2 = ctrl._service.get_task(2)
    assert task2.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert task2.is_force_parallel
    assert task2.codex_thread_id is not None

    # Verify events
    events = ctrl._service._ledger.list_events(2)
    assert any(e.event_type == "force_parallel_confirmed" for e in events)
    assert any(e.event_type == "force_parallel_started" for e in events)


@pytest.mark.asyncio
async def test_force_parallel_confirm_when_task_no_longer_waiting(ctrl: CommandController) -> None:
    """Force parallel confirm when task was already promoted should handle gracefully."""
    from wlcodex.waiting_callback import FORCE_PARALLEL_CONFIRM, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    # Task gets promoted by another path before callback
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    from wlcodex.task_service import drain_workspace
    await drain_workspace(ctrl._service, ctrl._backend, "demo")
    # Task 2 should now be queued/running

    cb = WaitingCallback(task_id=2, action=FORCE_PARALLEL_CONFIRM)
    response = await ctrl.handle_waiting_callback(cb)
    # Should not crash — just report not waiting
    assert "不在等待状态" in response.text or "force_parallel_no_longer_needed" in response.text


@pytest.mark.asyncio
async def test_force_parallel_does_not_weaken_lock(ctrl: CommandController) -> None:
    """After force_parallel, subsequent normal /task should still create waiting_slot."""
    from wlcodex.waiting_callback import FORCE_PARALLEL_CONFIRM, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    # Force parallel start task 2
    cb = WaitingCallback(task_id=2, action=FORCE_PARALLEL_CONFIRM)
    await ctrl.handle_waiting_callback(cb)
    ctrl._service._ledger.set_task_status(2, TaskStatus.RUNNING)

    # Create task 3 — should still go to waiting_slot
    response = await ctrl.handle("/task demo Third task", {"chat_id": 123})
    assert "等待工作区空闲" in response.text
    assert ctrl._service.get_task(3).status == TaskStatus.WAITING_SLOT


@pytest.mark.asyncio
async def test_worktree_isolated_creates_worktree_and_starts(ctrl: CommandController) -> None:
    """Worktree isolated button creates worktree and starts task there."""
    from wlcodex.waiting_callback import WORKTREE_ISOLATED, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})

    cb = WaitingCallback(task_id=2, action=WORKTREE_ISOLATED)

    # git worktree will fail in test (no git repo), so expect failure
    response = await ctrl.handle_waiting_callback(cb)
    # Should report failure gracefully, not crash
    assert "失败" in response.text
    assert ctrl._service.get_task(2).status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_worktree_done_buttons_for_completed_task(ctrl: CommandController) -> None:
    """Completed worktree task should show worktree done buttons."""
    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)
    ctrl._service._ledger.set_worktree_info(1, "/tmp/wt/task-1", "wlcodex/task-1-test")

    response = await ctrl.handle("/task 1", {})
    assert len(response.buttons) > 0
    button_labels = [b["text"] for row in response.buttons for b in row]
    assert "查看 diff" in button_labels
    assert "合并到主工作区" in button_labels
    assert "丢弃 worktree" in button_labels
    assert "保留 worktree" in button_labels


@pytest.mark.asyncio
async def test_worktree_done_diff_handles_missing_path(ctrl: CommandController) -> None:
    """Worktree diff should work without crashing even with missing path."""
    from wlcodex.waiting_callback import WORKTREE_DIFF, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)
    ctrl._service._ledger.set_worktree_info(1, "/nonexistent/path", "wlcodex/task-1")

    cb = WaitingCallback(task_id=1, action=WORKTREE_DIFF)
    response = await ctrl.handle_worktree_done_callback(cb)
    # Should not crash
    assert "diff" in response.text.lower() or "暂无" in response.text


@pytest.mark.asyncio
async def test_worktree_done_keep(ctrl: CommandController) -> None:
    """Worktree keep returns confirmation."""
    from wlcodex.waiting_callback import WORKTREE_KEEP, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)
    ctrl._service._ledger.set_worktree_info(1, "/tmp/wt/task-1", "wlcodex/task-1")

    cb = WaitingCallback(task_id=1, action=WORKTREE_KEEP)
    response = await ctrl.handle_worktree_done_callback(cb)
    assert "已保留" in response.text


@pytest.mark.asyncio
async def test_worktree_done_rejects_non_worktree_task(ctrl: CommandController) -> None:
    """Worktree done callbacks should reject tasks without worktree."""
    from wlcodex.waiting_callback import WORKTREE_KEEP, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    cb = WaitingCallback(task_id=1, action=WORKTREE_KEEP)
    response = await ctrl.handle_worktree_done_callback(cb)
    assert "不是 worktree 任务" in response.text


@pytest.mark.asyncio
async def test_waiting_callback_nonexistent_task(ctrl: CommandController) -> None:
    """Waiting callback for nonexistent task returns error."""
    from wlcodex.waiting_callback import KEEP, WaitingCallback

    cb = WaitingCallback(task_id=999, action=KEEP)
    response = await ctrl.handle_waiting_callback(cb)
    assert "不存在" in response.text


@pytest.mark.asyncio
async def test_waiting_callback_for_non_waiting_task(ctrl: CommandController) -> None:
    """Waiting callback for a task no longer in waiting_slot returns status message."""
    from wlcodex.waiting_callback import ABORT_BLOCKER_START_NEXT, WaitingCallback

    await ctrl.handle("/task demo First task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.RUNNING)
    await ctrl.handle("/task demo Second task", {"chat_id": 123})
    # Promote task 2 before callback
    ctrl._service._ledger.set_task_status(2, TaskStatus.QUEUED)

    cb = WaitingCallback(task_id=2, action=ABORT_BLOCKER_START_NEXT)
    response = await ctrl.handle_waiting_callback(cb)
    assert "已不在等待状态" in response.text


# ---------------------------------------------------------------------------
# Real git worktree integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def git_service(tmp_path: Path) -> TaskService:
    """TaskService backed by a real git repo workspace."""
    import subprocess

    ws_path = tmp_path / "workspace"
    ws_path.mkdir()
    subprocess.run(["git", "-C", str(ws_path), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(ws_path), "config", "user.email", "test@test"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(ws_path), "config", "user.name", "test"],
        check=True, capture_output=True,
    )
    # Initial commit so the repo has a HEAD for worktree creation
    (ws_path / "README.md").write_text("# test")
    subprocess.run(["git", "-C", str(ws_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(ws_path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    wt_root = str(tmp_path / "worktrees")
    return TaskService(
        ledger,
        (WorkspaceConfig("demo", ws_path, True),),
        task_log_dir=tmp_path / "logs",
        worktree_root=wt_root,
    )


def test_setup_worktree_creates_branch_and_path(git_service: TaskService) -> None:
    """setup_worktree creates a real git worktree outside the workspace."""
    import os

    git_service.start_task("demo", "Blocker", codex_thread_id="thread-1")
    git_service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = git_service.reserve_waiting_task("demo", "Worktree task", blocker_task_id=1)

    task, wt_path, branch = git_service.setup_worktree(w.id, slug="test")

    assert os.path.isdir(wt_path)
    assert branch.startswith("wlcodex/task-")
    assert task.worktree_path == wt_path
    assert task.worktree_branch == branch

    # Verify worktree is OUTSIDE the workspace — workspace git status must stay clean
    import subprocess
    ws = git_service.get_workspace("demo")
    status = subprocess.run(
        ["git", "-C", str(ws.path), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    assert status.stdout.strip() == ""  # workspace is clean


def test_setup_worktree_path_is_absolute(git_service: TaskService) -> None:
    """The returned worktree path must be absolute for create_thread."""
    import os

    git_service.start_task("demo", "Blocker", codex_thread_id="thread-1")
    git_service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = git_service.reserve_waiting_task("demo", "WT task", blocker_task_id=1)

    _, wt_path, _ = git_service.setup_worktree(w.id, slug="test")
    assert os.path.isabs(wt_path)


def test_worktree_merge_succeeds_on_clean_workspace(git_service: TaskService) -> None:
    """Merge worktree into clean workspace succeeds."""
    import subprocess

    git_service.start_task("demo", "Blocker", codex_thread_id="thread-1")
    git_service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = git_service.reserve_waiting_task("demo", "WT task", blocker_task_id=1)

    task, wt_path, branch = git_service.setup_worktree(w.id, slug="test")

    # Make a commit in the worktree so there's something to merge
    (Path(wt_path) / "new_file.txt").write_text("worktree change")
    subprocess.run(["git", "-C", wt_path, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", wt_path, "commit", "-m", "worktree commit"],
        check=True, capture_output=True,
    )

    # Merge should succeed
    msg = git_service.merge_worktree(w.id)
    assert "已合并" in msg


def test_worktree_merge_refuses_dirty_workspace(git_service: TaskService) -> None:
    """Merge must refuse when main workspace has uncommitted changes."""
    import subprocess

    ws = git_service.get_workspace("demo")
    git_service.start_task("demo", "Blocker", codex_thread_id="thread-1")
    git_service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = git_service.reserve_waiting_task("demo", "WT task", blocker_task_id=1)

    git_service.setup_worktree(w.id, slug="test")

    # Dirty the workspace
    (Path(str(ws.path)) / "dirty.txt").write_text("uncommitted")
    subprocess.run(["git", "-C", str(ws.path), "add", "."], check=True, capture_output=True)

    with pytest.raises(RuntimeError, match="未提交"):
        git_service.merge_worktree(w.id)


def test_worktree_discard_cleans_up(git_service: TaskService) -> None:
    """Discard removes worktree directory and branch, workspace unaffected."""
    import os
    import subprocess

    ws = git_service.get_workspace("demo")
    git_service.start_task("demo", "Blocker", codex_thread_id="thread-1")
    git_service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = git_service.reserve_waiting_task("demo", "WT task", blocker_task_id=1)

    task, wt_path, branch = git_service.setup_worktree(w.id, slug="test")
    assert os.path.isdir(wt_path)

    msg = git_service.discard_worktree(w.id)
    assert "已丢弃" in msg
    assert not os.path.exists(wt_path)

    # Workspace must remain clean (no side effects)
    status = subprocess.run(
        ["git", "-C", str(ws.path), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    assert status.stdout.strip() == ""


def test_setup_worktree_refuses_path_inside_workspace(tmp_path: Path) -> None:
    """setup_worktree must refuse if worktree_root is inside the workspace."""
    import subprocess

    ws_path = tmp_path / "ws"
    ws_path.mkdir()
    subprocess.run(["git", "-C", str(ws_path), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_path), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_path), "config", "user.name", "t"], check=True, capture_output=True)
    (ws_path / "f").write_text(".")
    subprocess.run(["git", "-C", str(ws_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_path), "commit", "-m", "init"], check=True, capture_output=True)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    # worktree_root inside the workspace — should be rejected
    wt_root_inside = str(ws_path / "runtime" / "worktrees")
    service = TaskService(
        ledger,
        (WorkspaceConfig("demo", ws_path, True),),
        worktree_root=wt_root_inside,
    )

    service.start_task("demo", "Blocker", codex_thread_id="thread-1")
    service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = service.reserve_waiting_task("demo", "WT task", blocker_task_id=1)

    with pytest.raises(RuntimeError, match="inside workspace"):
        service.setup_worktree(w.id, slug="test")


def test_worktree_merge_conflict_reports_and_aborts(git_service: TaskService) -> None:
    """Merge conflict aborts the merge and leaves workspace clean."""
    import subprocess

    ws = git_service.get_workspace("demo")
    git_service.start_task("demo", "Blocker", codex_thread_id="thread-1")
    git_service._ledger.set_task_status(1, TaskStatus.RUNNING)
    w = git_service.reserve_waiting_task("demo", "WT task", blocker_task_id=1)

    task, wt_path, branch = git_service.setup_worktree(w.id, slug="test")

    # Create conflicting changes: same file in both workspace and worktree
    (Path(str(ws.path)) / "shared.txt").write_text("main branch content")
    subprocess.run(["git", "-C", str(ws.path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(ws.path), "commit", "-m", "main change"],
        check=True, capture_output=True,
    )

    (Path(wt_path) / "shared.txt").write_text("worktree conflicting content")
    subprocess.run(["git", "-C", wt_path, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", wt_path, "commit", "-m", "worktree change"],
        check=True, capture_output=True,
    )

    # Merge should report conflict, not raise
    msg = git_service.merge_worktree(w.id)
    assert "冲突" in msg
    assert "人工处理" in msg

    # Verify merge was aborted — workspace should be back to clean
    status = subprocess.run(
        ["git", "-C", str(ws.path), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    assert status.stdout.strip() == ""
