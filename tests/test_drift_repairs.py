"""Drift repair tests — workspace locks, task reservation, continue state."""

from pathlib import Path

import pytest

from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.models import TaskStatus
from wlcodex.task_service import TaskService, WorkspaceBusy


def make_service(tmp_path: Path, allow_write: bool = True) -> TaskService:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    return TaskService(ledger, (WorkspaceConfig("demo", tmp_path, allow_write),))


def test_start_task_rejects_read_only_workspace(tmp_path: Path) -> None:
    service = make_service(tmp_path, allow_write=False)
    with pytest.raises(PermissionError):
        service.reserve_task("demo", "write something", telegram_chat_id=123)


@pytest.mark.asyncio
async def test_controller_creates_waiting_slot_when_workspace_busy(tmp_path: Path) -> None:
    """When workspace is busy, /task creates a waiting_slot instead of an error.

    Renamed from test_controller_does_not_create_thread_when_workspace_busy
    to reflect the new waiting_slot behavior.
    """
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", tmp_path, True),))
    backend = FakeCodexBackend()
    ctrl = CommandController(service, backend, TaskInspector(ledger, tmp_path / "logs"))

    first = service.start_task("demo", "first", codex_thread_id="thread-1")
    ledger.set_task_status(first.id, TaskStatus.RUNNING)

    response = await ctrl.handle("/task demo second", {"chat_id": 123})

    # Busy workspace creates waiting_slot, not error
    assert "等待工作区空闲" in response.text
    # No new Codex thread created (first task started via service directly)
    assert len(backend.threads) == 0


def test_continue_requires_workspace_available(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    original = service.start_task("demo", "original", codex_thread_id="thread-1")
    service._ledger.set_task_status(original.id, TaskStatus.DONE)
    active = service.start_task("demo", "active", codex_thread_id="thread-2")
    service._ledger.set_task_status(active.id, TaskStatus.RUNNING)

    with pytest.raises(WorkspaceBusy):
        service.continue_task(original.id, "continue")


def test_continue_moves_done_task_to_queued(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    task = service.start_task("demo", "original", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.DONE)

    updated = service.continue_task(task.id, "continue")

    assert updated.status == TaskStatus.QUEUED


def test_continue_rejects_running_task(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    task = service.start_task("demo", "running", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.RUNNING)

    with pytest.raises(RuntimeError):
        service.continue_task(task.id, "continue")


def test_paused_task_blocks_new_write_task(tmp_path: Path) -> None:
    """A paused task still holds the workspace write slot — new writes must fail."""
    service = make_service(tmp_path)
    paused = service.start_task("demo", "paused", codex_thread_id="thread-1")
    service._ledger.set_task_status(paused.id, TaskStatus.PAUSED)

    with pytest.raises(WorkspaceBusy):
        service.reserve_task("demo", "new attempt", telegram_chat_id=123)


def test_paused_abort_releases_workspace(tmp_path: Path) -> None:
    """Aborting a paused task releases the workspace for new writes."""
    service = make_service(tmp_path)
    paused = service.start_task("demo", "paused", codex_thread_id="thread-1")
    service._ledger.set_task_status(paused.id, TaskStatus.PAUSED)

    aborted = service.abort_task(paused.id)
    assert aborted.status == TaskStatus.ABORTED

    new_task = service.reserve_task("demo", "fresh", telegram_chat_id=123)
    assert new_task.status == TaskStatus.QUEUED


def test_paused_continue_unblocks_for_same_task(tmp_path: Path) -> None:
    """A paused task can be continued (its own workspace slot is excluded)."""
    service = make_service(tmp_path)
    task = service.start_task("demo", "paused", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    continued = service.continue_task(task.id, "resume work")
    assert continued.status == TaskStatus.QUEUED


def test_reserve_task_creates_queued_task_without_thread(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    task = service.reserve_task("demo", "new task", telegram_chat_id=123)

    assert task.status == TaskStatus.QUEUED
    assert task.codex_thread_id is None
    assert task.telegram_chat_id == 123
