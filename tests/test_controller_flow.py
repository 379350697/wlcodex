"""Controller flow tests with fake backend."""

from pathlib import Path

import pytest

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
    return CommandController(service, backend, inspector)


# ---------------------------------------------------------------------------
# /task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_calls_create_thread_and_start_turn(ctrl: CommandController) -> None:
    response = await ctrl.handle("/task demo Fix the health timeout", {"chat_id": 123})

    tasks = ctrl._service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].workspace_alias == "demo"
    assert tasks[0].title == "Fix the health timeout"

    assert len(ctrl._backend.turns) == 1
    thread_id, prompt = ctrl._backend.turns[0]
    assert prompt == "Fix the health timeout"
    assert "任务 #1" in response.text


# ---------------------------------------------------------------------------
# /continue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_calls_backend_continue_turn(ctrl: CommandController) -> None:
    # Create task via the full controller path to get a turn recorded
    await ctrl.handle("/task demo Old task", {"chat_id": 123})
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    await ctrl.handle("/continue 1 Continue the work", {})

    # continue_turn adds another turn entry
    assert len(ctrl._backend.turns) >= 2


# ---------------------------------------------------------------------------
# /steer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_calls_backend_steer_turn(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Active task", codex_thread_id="thread-1")
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    await ctrl.handle("/steer 1 Stop changing config", {})

    assert len(ctrl._backend.steers) == 1
    assert ctrl._backend.steers[0][2] == "Stop changing config"


@pytest.mark.asyncio
async def test_steer_refuses_done_task(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Done task", codex_thread_id="thread-1")
    ctrl._service._ledger.set_task_status(1, TaskStatus.DONE)

    response = await ctrl.handle("/steer 1 Try to steer", {})

    assert "active turn" in response.text.lower() or "use /continue" in response.text.lower()
    assert len(ctrl._backend.steers) == 0


# ---------------------------------------------------------------------------
# /tail, /events, /diff, /files do NOT call backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_does_not_call_backend(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Test", codex_thread_id="thread-1")
    pre_turns = len(ctrl._backend.turns)

    await ctrl.handle("/tail 1", {})
    assert len(ctrl._backend.turns) == pre_turns


@pytest.mark.asyncio
async def test_events_does_not_call_backend(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Test", codex_thread_id="thread-1")
    pre_turns = len(ctrl._backend.turns)

    await ctrl.handle("/events 1", {})
    assert len(ctrl._backend.turns) == pre_turns


@pytest.mark.asyncio
async def test_diff_does_not_call_backend(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Test", codex_thread_id="thread-1")
    pre_turns = len(ctrl._backend.turns)

    await ctrl.handle("/diff 1", {})
    assert len(ctrl._backend.turns) == pre_turns


@pytest.mark.asyncio
async def test_files_does_not_call_backend(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Test", codex_thread_id="thread-1")
    pre_turns = len(ctrl._backend.turns)

    await ctrl.handle("/files 1", {})
    assert len(ctrl._backend.turns) == pre_turns


# ---------------------------------------------------------------------------
# /archive refuses running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_refuses_running_task(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Running", codex_thread_id="thread-1")
    ctrl._service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    response = await ctrl.handle("/archive 1", {})
    assert "cannot archive" in response.text.lower() or "error" in response.text.lower()
    assert ctrl._service.get_task(1).status != TaskStatus.ARCHIVED


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_backend_and_db_status(ctrl: CommandController) -> None:
    response = await ctrl.handle("/health", {})
    assert "后端健康" in response.text or "后端异常" in response.text
