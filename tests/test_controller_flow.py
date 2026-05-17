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
    service = TaskService(ledger, (
        WorkspaceConfig("demo", Path("/tmp/demo"), True),
        WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    return CommandController(service, backend, inspector, ledger=ledger)


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


# ---------------------------------------------------------------------------
# Conversation commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_conversation_command(ctrl: CommandController) -> None:
    response = await ctrl.handle("/new", {"chat_id": 100, "user_id": 200})
    assert "新对话" in response.text or "已创建" in response.text


@pytest.mark.asyncio
async def test_codex_direct_creates_task(ctrl: CommandController) -> None:
    response = await ctrl.handle("/codex 分析 router.py", {"chat_id": 200, "user_id": 300})

    assert "Codex" in response.text or "分析" in response.text
    tasks = ctrl._service.list_tasks()
    assert len(tasks) >= 1


@pytest.mark.asyncio
async def test_plain_text_creates_conversation_and_task(ctrl: CommandController) -> None:
    # We need ledger for conversation handling
    assert ctrl._ledger is not None

    response = await ctrl.handle_conversation_text(
        "帮我分析 router.py",
        {"chat_id": 100, "user_id": 200},
    )
    assert "Codex" in response.text or "分析" in response.text


@pytest.mark.asyncio
async def test_stop_no_active_conversation(ctrl: CommandController) -> None:
    response = await ctrl.handle("/stop", {"chat_id": 999, "user_id": 999})
    assert "没有活跃对话" in response.text


@pytest.mark.asyncio
async def test_claude_direct_reports_disabled(ctrl: CommandController) -> None:
    response = await ctrl.handle("/claude 修改 README", {"chat_id": 1, "user_id": 2})
    assert "未启用" in response.text


@pytest.mark.asyncio
async def test_auto_mode_reports_claude_needed(ctrl: CommandController) -> None:
    response = await ctrl.handle("/auto 修复登录 bug", {"chat_id": 1, "user_id": 2})
    assert "Claude" in response.text or "总工程师" in response.text


@pytest.mark.asyncio
async def test_verify_reports_no_active_conversation(ctrl: CommandController) -> None:
    response = await ctrl.handle("/verify", {"chat_id": 1, "user_id": 2})
    assert "没有活跃对话" in response.text or "请先" in response.text


@pytest.mark.asyncio
async def test_switch_workspace_no_active_conversation(ctrl: CommandController) -> None:
    response = await ctrl.handle("/switch other", {"chat_id": 999, "user_id": 999})
    assert "没有活跃对话" in response.text


@pytest.mark.asyncio
async def test_model_no_active_conversation(ctrl: CommandController) -> None:
    response = await ctrl.handle("/model claude-sonnet-4-6", {"chat_id": 999, "user_id": 999})
    assert "没有活跃对话" in response.text


@pytest.mark.asyncio
async def test_legacy_commands_still_work(ctrl: CommandController) -> None:
    response = await ctrl.handle("/status", {"chat_id": 1, "user_id": 2})
    assert "任务列表" in response.text or "暂无任务" in response.text


@pytest.mark.asyncio
async def test_legacy_diff_command_with_id(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Test", codex_thread_id="thread-1")
    response = await ctrl.handle("/diff 1", {})
    assert response.text  # Shouldn't error


@pytest.mark.asyncio
async def test_help_shows_new_commands(ctrl: CommandController) -> None:
    response = await ctrl.handle("/help", {})
    assert "/codex" in response.text
    assert "/claude" in response.text
    assert "/auto" in response.text
    assert "总工程师" in response.text


@pytest.mark.asyncio
async def test_status_shows_conversation_when_active(ctrl: CommandController) -> None:
    # First create a conversation via /new
    await ctrl.handle("/new", {"chat_id": 100, "user_id": 200})
    # Then check /status for that chat
    response = await ctrl.handle("/status", {"chat_id": 100, "user_id": 200})
    assert "对话" in response.text or "Codex 直聊" in response.text


@pytest.mark.asyncio
async def test_status_falls_back_to_task_list_without_conversation(ctrl: CommandController) -> None:
    response = await ctrl.handle("/status", {"chat_id": 999, "user_id": 999})
    assert "任务列表" in response.text or "暂无任务" in response.text


@pytest.mark.asyncio
async def test_verify_with_conversation_calls_codex(ctrl: CommandController) -> None:
    # Create conversation and an agent run first
    await ctrl.handle("/new", {"chat_id": 100, "user_id": 200})
    await ctrl.handle("/codex 分析测试", {"chat_id": 100, "user_id": 200})

    # /verify should find the latest run and attempt Codex verification
    response = await ctrl.handle("/verify 确认修复", {"chat_id": 100, "user_id": 200})
    assert "验收" in response.text
