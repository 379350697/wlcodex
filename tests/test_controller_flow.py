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


# ---------------------------------------------------------------------------
# Real-closure tests: prove backend interfaces (not echo) are used
# ---------------------------------------------------------------------------


class FakeClaudeBackendForController:
    """Fake Claude backend that implements real AgentBackend.send interface."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._responses: list[str] = ["Fake Claude implementation result."]
        self.enabled = True

    async def send(self, request):
        from wlcodex.agent_backend import AgentResult
        self.calls.append(request.prompt)
        text = self._responses[len(self.calls) - 1]
        return AgentResult(
            text=text,
            exit_code=0,
            token_input=len(request.prompt) // 4,
            token_output=len(text) // 4,
        )

    def interrupt(self, session_id=None):
        pass

    def health(self):
        return type("h", (), {"is_healthy": True})()


@pytest.fixture
def ctrl_with_claude(tmp_path: Path) -> CommandController:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    backend._codex_responses = [
        "decision: pass\nsummary: Analysis complete.",
    ]
    service = TaskService(ledger, (
        WorkspaceConfig("demo", Path("/tmp/demo"), True),
        WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    claude = FakeClaudeBackendForController()
    return CommandController(service, backend, inspector, ledger=ledger, claude_backend=claude)


@pytest.mark.asyncio
async def test_plain_greeting_replies_without_agent_loop(
    ctrl_with_claude: CommandController,
) -> None:
    response = await ctrl_with_claude.handle_conversation_text(
        "你好",
        {"chat_id": 100, "user_id": 200},
    )

    assert "你好" in response.text
    assert "总工程师编排完成" not in response.text
    assert len(ctrl_with_claude._backend.turns) == 0
    assert len(ctrl_with_claude._claude.calls) == 0


@pytest.mark.asyncio
async def test_claude_direct_uses_real_send_interface(ctrl_with_claude: CommandController) -> None:
    """Claude direct mode must call backend.send(), not echo/fake_response."""
    response = await ctrl_with_claude.handle(
        "/claude 修改 auth.py 添加空值检查",
        {"chat_id": 100, "user_id": 200},
    )
    claude = ctrl_with_claude._claude
    assert hasattr(claude, "calls")
    assert len(claude.calls) == 1
    # The prompt must be a rendered packet, not raw command text
    assert "mode:" in claude.calls[0] or "user_goal:" in claude.calls[0]
    assert "Claude Code 已完成" in response.text


@pytest.mark.asyncio
async def test_claude_direct_sets_active_claude_run_id(ctrl_with_claude: CommandController) -> None:
    """Claude direct must write agent_run.id to active_claude_run_id, NOT active_codex_task_id."""
    await ctrl_with_claude.handle(
        "/claude 修改 README",
        {"chat_id": 100, "user_id": 200},
    )
    active = ctrl_with_claude._ledger.get_active_conversation(100)
    assert active is not None
    # active_claude_run_id must be set
    assert active.active_claude_run_id is not None
    assert active.active_claude_run_id > 0
    # active_codex_task_id must NOT be polluted by Claude
    assert active.active_codex_task_id is None


@pytest.mark.asyncio
async def test_conversation_text_uses_packet_render(ctrl_with_claude: CommandController) -> None:
    """Plain text must send packet.render() to Codex, not raw text."""
    await ctrl_with_claude.handle_conversation_text(
        "帮我分析这个模块",
        {"chat_id": 100, "user_id": 200},
    )
    # Check that start_turn was called with rendered packet, not raw text
    turns = ctrl_with_claude._backend.turns
    assert len(turns) > 0
    _, prompt_sent = turns[-1]
    assert "mode:" in prompt_sent
    assert "user_goal:" in prompt_sent
    # The original raw user text should be inside the packet
    assert "帮我分析这个模块" in prompt_sent


@pytest.mark.asyncio
async def test_orchestrator_uses_send_codex_prompt(ctrl_with_claude: CommandController) -> None:
    """ChiefEngineerOrchestrator must use send_codex_prompt, not echo."""
    from wlcodex.orchestrator import ChiefEngineerOrchestrator
    from wlcodex.context_packets import ContextBudget

    orch = ChiefEngineerOrchestrator(
        ctrl_with_claude._backend,
        ctrl_with_claude._claude,
        max_verify_rounds=1,
    )
    result = await orch.run("修复登录 bug")
    assert result.status == "passed"


@pytest.mark.asyncio
async def test_auto_mode_runs_real_orchestration(ctrl_with_claude: CommandController) -> None:
    """Handle /auto must invoke ChiefEngineerOrchestrator with real backends."""
    response = await ctrl_with_claude.handle(
        "/auto 修复登录 bug",
        {"chat_id": 100, "user_id": 200},
    )
    assert "总工程师编排完成" in response.text


@pytest.mark.asyncio
async def test_auto_mode_hides_english_model_snippets(
    ctrl_with_claude: CommandController,
) -> None:
    response = await ctrl_with_claude.handle(
        "/auto 修复登录 bug",
        {"chat_id": 100, "user_id": 200},
    )

    assert "总工程师编排完成" in response.text
    assert "Analysis complete" not in response.text
    assert "Fake Claude implementation result" not in response.text
    assert "confidence: high" not in response.text
    assert "非中文内容" in response.text


@pytest.mark.asyncio
async def test_claude_completion_buttons_use_conv_protocol(ctrl_with_claude: CommandController) -> None:
    """Claude completion buttons must use conv: protocol, not waiting:."""
    response = await ctrl_with_claude.handle(
        "/claude 修改 auth.py",
        {"chat_id": 100, "user_id": 200},
    )
    assert len(response.buttons) > 0
    for row in response.buttons:
        for btn in row:
            assert btn["callback_data"].startswith("conv:")
            assert "waiting:" not in btn["callback_data"]


@pytest.mark.asyncio
async def test_conversation_callback_diff_action(ctrl_with_claude: CommandController) -> None:
    """conv: diff callback returns diff for the conversation."""
    from wlcodex.conversation_callback import ConversationCallback, DIFF

    # Setup a conversation first via /claude
    await ctrl_with_claude.handle("/claude 修改 auth.py", {"chat_id": 100, "user_id": 200})
    active = ctrl_with_claude._ledger.get_active_conversation(100)
    assert active is not None

    cb = ConversationCallback(conversation_id=active.id, action=DIFF)
    response = await ctrl_with_claude.handle_conversation_callback(cb)
    assert response.text  # Should not error


@pytest.mark.asyncio
async def test_conversation_callback_verify_action(ctrl_with_claude: CommandController) -> None:
    """conv: verify callback triggers Codex verification."""
    from wlcodex.conversation_callback import ConversationCallback, VERIFY

    await ctrl_with_claude.handle("/claude 修改 auth.py", {"chat_id": 100, "user_id": 200})
    active = ctrl_with_claude._ledger.get_active_conversation(100)

    cb = ConversationCallback(conversation_id=active.id, action=VERIFY)
    response = await ctrl_with_claude.handle_conversation_callback(cb)
    assert response.text
    assert "验收" in response.text or "Codex" in response.text


def test_encode_decode_conversation_callback_roundtrip() -> None:
    """Encode and decode a conversation callback should round-trip."""
    from wlcodex.conversation_callback import (
        encode_conversation_callback,
        decode_conversation_callback,
        DIFF,
    )
    encoded = encode_conversation_callback(42, DIFF)
    assert encoded.startswith("conv:")
    decoded = decode_conversation_callback(encoded)
    assert decoded is not None
    assert decoded.conversation_id == 42
    assert decoded.action == DIFF


def test_decode_conversation_callback_rejects_waiting() -> None:
    """decode_conversation_callback must reject waiting: protocol."""
    from wlcodex.conversation_callback import decode_conversation_callback
    assert decode_conversation_callback("waiting:1:diff") is None
    assert decode_conversation_callback("approval:xxx") is None
    assert decode_conversation_callback("not-conv:1:diff") is None


@pytest.mark.asyncio
async def test_stop_with_claude_run_interrupts_claude(ctrl_with_claude: CommandController) -> None:
    """Handle /stop must interrupt Claude when active_claude_run_id is set."""
    await ctrl_with_claude.handle("/claude 修改 auth.py", {"chat_id": 100, "user_id": 200})
    response = await ctrl_with_claude.handle("/stop", {"chat_id": 100, "user_id": 200})
    assert "对话" in response.text
    assert "已停止" in response.text
