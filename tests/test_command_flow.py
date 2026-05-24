from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

from wlcodex.codex_backend import FakeCodexBackend
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
    return CommandController(service, backend, inspector)


@pytest.mark.asyncio
async def test_task_command_creates_task_and_starts_backend(ctrl: CommandController) -> None:
    response = await ctrl.handle("/task demo Fix bug", {"chat_id": 123})

    tasks = ctrl._service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Fix bug"
    assert len(ctrl._backend.turns) == 1
    assert "任务 #1" in response.text


@pytest.mark.asyncio
async def test_continue_command(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Old task", codex_thread_id="thread-old")
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    response = await ctrl.handle("/continue 1 Continue this", {})

    assert "Task #1 continued" in response.text or "#1" in response.text


@pytest.mark.asyncio
async def test_steer_command(ctrl: CommandController) -> None:
    from wlcodex.codex_backend import BackendEvent

    ctrl._service.start_task("demo", "Active task", codex_thread_id="thread-1")
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    await ctrl.handle("/steer 1 Change direction", {})

    assert len(ctrl._backend.steers) == 1
    assert ctrl._backend.steers[0][0] == "thread-1"


@pytest.mark.asyncio
async def test_tail_does_not_call_backend(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Test", codex_thread_id="thread-1")

    pre_turns = len(ctrl._backend.turns)
    await ctrl.handle("/tail 1", {})
    # Backend turns should not change
    assert len(ctrl._backend.turns) == pre_turns


@pytest.mark.asyncio
async def test_health_reports_status(ctrl: CommandController) -> None:
    response = await ctrl.handle("/health", {})
    assert "后端健康" in response.text or "后端异常" in response.text


@pytest.mark.asyncio
async def test_archive_refuses_running(ctrl: CommandController) -> None:
    from wlcodex.codex_backend import BackendEvent

    ctrl._service.start_task("demo", "Running task", codex_thread_id="thread-1")
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    response = await ctrl.handle("/archive 1", {})
    assert "cannot archive" in response.text.lower() or "error" in response.text.lower()


@pytest.mark.asyncio
async def test_unknown_command_returns_usage(ctrl: CommandController) -> None:
    response = await ctrl.handle("/banana", {})
    assert "未知命令" in response.text or "/help" in response.text
