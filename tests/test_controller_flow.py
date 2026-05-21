"""Controller flow tests with fake backend."""

import asyncio
from pathlib import Path
import subprocess

import pytest

from wlcodex.agent_backend import AgentStreamEvent
from wlcodex.codex_backend import BackendEvent, FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.claude_permissions import ClaudePermissionState
from wlcodex.conversation_state_machine import BUSY_APPEND, BUSY_INTERRUPT
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.models import AgentRunStatus, OrchestrationStatus, TaskStatus
from wlcodex.orchestration_runner import OrchestrationRunner
from wlcodex.task_service import TaskService
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


def _attach_runtime_runner(
    controller: CommandController,
    *,
    service: TaskService,
    backend: object,
    claude: object,
    ledger: Ledger,
    renderer: object | None = None,
    store: RuntimeEventStore | None = None,
) -> OrchestrationRunner:
    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=claude,
        ledger=ledger,
        interaction_renderer=renderer,
        runtime_event_store=store,
    )
    controller.set_orchestration_runner(runner)
    return runner


async def _drain_runtime_runner(controller: CommandController) -> None:
    runner = controller._orchestration_runner
    assert runner is not None
    for _ in range(20):
        tasks = list(getattr(runner, "_tasks", set()))
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert not getattr(runner, "_tasks", set())
    for _ in range(20):
        tasks = list(getattr(controller, "_background_tasks", set()))
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert not getattr(controller, "_background_tasks", set())


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


@pytest.fixture
def ctrl_with_claude_permission_state(tmp_path: Path) -> CommandController:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    return CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_permission_state=ClaudePermissionState("只规划"),
    )


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
    assert "新工作台" in response.text


@pytest.mark.asyncio
async def test_codex_direct_creates_task(ctrl: CommandController) -> None:
    response = await ctrl.handle("/codex 分析 router.py", {"chat_id": 200, "user_id": 300})

    assert "只交给 Codex" in response.text
    tasks = ctrl._service.list_tasks()
    assert len(tasks) >= 1


@pytest.mark.asyncio
async def test_codex_direct_sends_raw_work_prompt_to_codex(
    ctrl: CommandController,
) -> None:
    await ctrl.handle("/codex 修改 README 并运行测试", {"chat_id": 201, "user_id": 301})

    assert ctrl._backend.turns
    _thread_id, prompt = ctrl._backend.turns[-1]
    assert prompt == "修改 README 并运行测试"


@pytest.mark.asyncio
async def test_codex_command_busy_returns_terminalized_choice_card(
    ctrl_with_claude: CommandController,
) -> None:
    ledger = ctrl_with_claude._ledger
    assert ledger is not None
    active = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="正在执行",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    blocker = ctrl_with_claude._service.reserve_task(
        "wlcodex", "旧任务", telegram_chat_id=100,
    )
    ledger.set_conversation_active_task(active.id, blocker.id)
    ledger.set_task_status(blocker.id, TaskStatus.RUNNING)

    response = await ctrl_with_claude.handle(
        "/codex 你这些审核能做到自动吗",
        {"chat_id": 100, "user_id": 200},
    )

    assert "workspace wlcodex is busy" not in response.text
    assert "当前工作区正在执行" in response.text
    flat_buttons = [button for row in response.buttons for button in row]
    labels = {button["text"] for button in flat_buttons}
    assert "发给当前 Codex" in labels
    assert "打断并执行这句" in labels
    assert "排队稍后" in labels
    assert "新开隔离现场" in labels


@pytest.mark.asyncio
async def test_claude_command_busy_returns_terminalized_choice_card(
    ctrl_with_claude: CommandController,
) -> None:
    ledger = ctrl_with_claude._ledger
    assert ledger is not None
    active = ledger.create_conversation(
        chat_id=101,
        user_id=201,
        title="正在执行",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    blocker = ctrl_with_claude._service.reserve_task(
        "wlcodex", "旧任务", telegram_chat_id=101,
    )
    ledger.set_conversation_active_task(active.id, blocker.id)
    ledger.set_task_status(blocker.id, TaskStatus.RUNNING)

    response = await ctrl_with_claude.handle(
        "/claude 你这些审核能做到自动吗",
        {"chat_id": 101, "user_id": 201},
    )

    assert "workspace wlcodex is busy" not in response.text
    assert "当前工作区正在执行" in response.text
    flat_buttons = [button for row in response.buttons for button in row]
    labels = {button["text"] for button in flat_buttons}
    assert "发给当前 Claude" in labels
    assert "打断并执行这句" in labels
    assert "排队稍后" in labels
    assert "新开隔离现场" in labels


@pytest.mark.asyncio
async def test_busy_append_steers_current_codex_turn(
    ctrl: CommandController,
) -> None:
    ledger = ctrl._ledger
    assert ledger is not None
    active = ledger.create_conversation(
        chat_id=202,
        user_id=302,
        title="正在执行",
        mode="codex_direct",
        workspace_alias="wlcodex",
    )
    task = ctrl._service.reserve_task("wlcodex", "旧任务", telegram_chat_id=202)
    ledger.set_conversation_active_task(active.id, task.id)
    ctrl._service.set_task_thread(task.id, "thread-1")
    ledger.set_active_turn(task.id, "turn-1")
    ledger.set_task_status(task.id, TaskStatus.RUNNING)
    ledger.update_conversation_summary(
        active.id, "[工作区忙待处理] /codex 新插话"
    )

    response = await ctrl.handle_workspace_busy_callback(BUSY_APPEND, active.id)

    assert "已发给当前 Codex" in response.text
    assert ctrl._backend.steers[-1] == ("thread-1", "turn-1", "新插话")


@pytest.mark.asyncio
async def test_busy_interrupt_aborts_current_and_runs_pending_codex(
    ctrl: CommandController,
) -> None:
    ledger = ctrl._ledger
    assert ledger is not None
    active = ledger.create_conversation(
        chat_id=203,
        user_id=303,
        title="正在执行",
        mode="codex_direct",
        workspace_alias="wlcodex",
    )
    task = ctrl._service.reserve_task("wlcodex", "旧任务", telegram_chat_id=203)
    ledger.set_conversation_active_task(active.id, task.id)
    ctrl._service.set_task_thread(task.id, "thread-2")
    ledger.set_active_turn(task.id, "turn-2")
    ledger.set_task_status(task.id, TaskStatus.RUNNING)
    ledger.update_conversation_summary(
        active.id, "[工作区忙待处理] /codex 最新任务"
    )

    response = await ctrl.handle_workspace_busy_callback(BUSY_INTERRUPT, active.id)

    assert "只交给 Codex" in response.text
    assert ctrl._service.get_task(task.id).status == TaskStatus.ABORTED
    assert ("thread-2", "turn-2") in ctrl._backend._interrupts
    assert ctrl._backend.turns[-1][1] == "最新任务"


@pytest.mark.asyncio
async def test_plain_text_creates_conversation_and_task(ctrl: CommandController) -> None:
    # We need ledger for conversation handling
    assert ctrl._ledger is not None

    response = await ctrl.handle_conversation_text(
        "帮我分析 router.py",
        {"chat_id": 100, "user_id": 200},
    )
    assert "看一下" in response.text


@pytest.mark.asyncio
async def test_stop_no_active_conversation(ctrl: CommandController) -> None:
    response = await ctrl.handle("/stop", {"chat_id": 999, "user_id": 999})
    assert "没有活跃对话" in response.text


@pytest.mark.asyncio
async def test_claude_direct_reports_disabled(ctrl: CommandController) -> None:
    response = await ctrl.handle("/claude 修改 README", {"chat_id": 1, "user_id": 2})
    assert "未启用" in response.text


@pytest.mark.asyncio
async def test_status_uses_runtime_events_when_available(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="Runtime",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_STARTED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id="1",
        correlation_id="status-corr",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.OPERATOR,
        payload={"phase": "running_analysis"},
        occurred_at=now_iso(),
        conversation_id=conversation.id,
        orchestration_run_id=1,
    ))
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.AGENT_RUN_STARTED,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id="10",
        correlation_id="status-corr",
        source=EventSource.CLAUDE,
        actor="claude",
        visibility=Visibility.OPERATOR,
        payload={"agent": "claude", "role": "implementation"},
        occurred_at=now_iso(),
        conversation_id=conversation.id,
        orchestration_run_id=1,
        agent_run_id=10,
    ))
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),
    ))
    controller = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        runtime_event_store=store,
    )

    response = await controller.handle("/status", {"chat_id": 100, "user_id": 200})

    # /status must use clean product formatter, not diagnostic dump.
    assert "当前对话：Runtime" in response.text
    assert "模式：总工程师" in response.text
    assert "运行 #" not in response.text
    assert "最近事件" not in response.text
    assert "#10" not in response.text
    assert "Agent 运行记录" not in response.text
    assert "事件总数" not in response.text


@pytest.mark.asyncio
async def test_trace_command_uses_runtime_inspector(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="Trace",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_STARTED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id="1",
        correlation_id="trace-corr",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.OPERATOR,
        payload={"phase": "running_analysis"},
        occurred_at=now_iso(),
        conversation_id=conversation.id,
        orchestration_run_id=1,
    ))
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),
    ))
    controller = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        runtime_event_store=store,
    )

    response = await controller.handle("/trace", {"chat_id": 100, "user_id": 200})

    assert "运行时事件记录" in response.text
    assert "运行开始" in response.text


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
async def test_claude_permission_command_switches_with_chinese_mode(
    ctrl_with_claude_permission_state: CommandController,
) -> None:
    response = await ctrl_with_claude_permission_state.handle(
        "/claude_mode 允许编辑",
        {"chat_id": 999, "user_id": 999},
    )

    assert "当前模式：允许编辑" in response.text
    assert "acceptEdits" not in response.text
    assert ctrl_with_claude_permission_state._claude_permission_state.get() == "acceptEdits"
    assert (
        ctrl_with_claude_permission_state._ledger.get_runtime_setting(
            "claude.permission_mode"
        )
        == "acceptEdits"
    )
    assert response.buttons
    assert all(
        "acceptEdits" not in button["text"]
        for row in response.buttons
        for button in row
    )


@pytest.mark.asyncio
async def test_legacy_commands_still_work(ctrl: CommandController) -> None:
    response = await ctrl.handle("/status", {"chat_id": 1, "user_id": 2})
    assert "当前还没有工作台" in response.text
    assert "/task" not in response.text


@pytest.mark.asyncio
async def test_diff_without_workbench_uses_workbench_copy(ctrl: CommandController) -> None:
    response = await ctrl.handle("/diff", {"chat_id": 123})

    assert "工作台" in response.text
    assert "任务 ID" not in response.text
    assert "任务 #" not in response.text


@pytest.mark.asyncio
async def test_files_without_workbench_uses_workbench_copy(ctrl: CommandController) -> None:
    response = await ctrl.handle("/files", {"chat_id": 123})

    assert "工作台" in response.text
    assert "任务 ID" not in response.text
    assert "任务 #" not in response.text


@pytest.mark.asyncio
async def test_task_command_is_routed_through_legacy_diagnostics(
    ctrl: CommandController,
) -> None:
    class FakeLegacyDiagnostics:
        def __init__(self) -> None:
            self.seen: list[object] = []

        def can_handle(self, command: object) -> bool:
            return command.__class__.__name__ == "StartTaskCommand"

        async def handle(self, command: object, telegram_context: dict | None) -> object:
            self.seen.append(command)
            return type("Response", (), {"text": "legacy adapter", "buttons": []})()

    adapter = FakeLegacyDiagnostics()
    ctrl.set_legacy_diagnostics(adapter)

    response = await ctrl.handle("/task demo Fix the health timeout", {"chat_id": 123})

    assert response.text == "legacy adapter"
    assert len(adapter.seen) == 1


@pytest.mark.asyncio
async def test_legacy_diff_command_with_id(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Test", codex_thread_id="thread-1")
    response = await ctrl.handle("/diff 1", {})
    assert response.text  # Shouldn't error


@pytest.mark.asyncio
async def test_help_shows_new_commands(ctrl: CommandController) -> None:
    response = await ctrl.handle("/help", {})
    assert "WLCodex" in response.text
    assert "默认流程：Codex -> Claude -> Codex" in response.text
    assert "当前视图：驾驶舱" in response.text
    assert "[新工作台]" in response.text
    assert "[接管现场]" in response.text
    assert len(response.text.splitlines()) <= 14


@pytest.mark.asyncio
async def test_sessions_fallback_hides_internal_thread_ids(ctrl: CommandController) -> None:
    ctrl._service.start_task("demo", "Investigate sessions", codex_thread_id="thread-secret")

    response = await ctrl.handle("/sessions", {"chat_id": 500, "user_id": 600})

    assert "thread-secret" not in response.text
    assert "Thread ID" not in response.text


@pytest.mark.asyncio
async def test_status_shows_conversation_when_active(ctrl: CommandController) -> None:
    # First create a conversation via /new
    await ctrl.handle("/new", {"chat_id": 100, "user_id": 200})
    # Then check /status for that chat
    response = await ctrl.handle("/status", {"chat_id": 100, "user_id": 200})
    assert "对话" in response.text or "Codex 直聊" in response.text


@pytest.mark.asyncio
async def test_status_without_workbench_uses_workbench_copy(ctrl: CommandController) -> None:
    response = await ctrl.handle("/status", {"chat_id": 999, "user_id": 999})
    assert "当前还没有工作台" in response.text
    assert "/task" not in response.text


@pytest.mark.asyncio
async def test_verify_with_conversation_calls_codex(ctrl: CommandController) -> None:
    # Create conversation and an agent run first
    await ctrl.handle("/new", {"chat_id": 100, "user_id": 200})
    await ctrl.handle("/codex 分析测试", {"chat_id": 100, "user_id": 200})

    # /verify should find the latest run and attempt Codex verification
    response = await ctrl.handle("/verify 确认修复", {"chat_id": 100, "user_id": 200})
    assert "验收" in response.text


@pytest.mark.asyncio
async def test_verify_without_active_codex_task_hides_internal_task_id(ctrl: CommandController) -> None:
    ledger = ctrl._ledger
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="claude done",
        mode="claude_direct",
        workspace_alias="wlcodex",
    )
    run = ledger.create_agent_run(
        conversation_id=conversation.id,
        agent="claude",
        role="implementation",
        prompt_packet_summary="done",
    )
    ledger.update_agent_run_status(
        run.id,
        AgentRunStatus.DONE.value,
        completion_summary="changed files",
    )

    response = await ctrl.handle("/verify 确认修复", {"chat_id": 100, "user_id": 200})

    assert "验收" in response.text
    assert "任务 #" not in response.text


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

    async def send_streaming(self, request):
        result = await self.send(request)
        yield AgentStreamEvent(delta=result.text, event_type="text")

    def interrupt(self, session_id=None):
        pass

    def health(self):
        return type("h", (), {"is_healthy": True})()


class RecordingInteractionRenderer:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def handle(self, event: object) -> None:
        self.events.append(event)


class StreamingClaudeWritesTrackedFile:
    enabled = True

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.prompts: list[str] = []

    async def send_streaming(self, request):
        self.prompts.append(request.prompt)
        (self.workspace / "tracked.txt").write_text("changed by claude\n", encoding="utf-8")
        yield AgentStreamEvent(delta="Implementation complete.", event_type="text")


class RecordingThreadCodexBackend(FakeCodexBackend):
    def __init__(self) -> None:
        super().__init__()
        self._codex_responses = [
            "Root cause: tracked.txt needs a change. Implementation needed.",
            "decision: pass\nsummary: Verified changed workspace.",
        ]
        self.created_prompt_threads: list[str] = []

    async def send_codex_prompt(
        self,
        workspace_path: str,
        prompt: str,
        *,
        on_thread_created=None,
    ) -> str:
        thread_id = f"hidden-thread-{len(self.created_prompt_threads) + 1}"
        self.created_prompt_threads.append(thread_id)
        if on_thread_created is not None:
            on_thread_created(thread_id)
        await self.start_turn(thread_id, prompt)
        return self._codex_responses[len(self.created_prompt_threads) - 1]


class StreamingClaudeError:
    enabled = True

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def send_streaming(self, request):
        self.prompts.append(request.prompt)
        yield AgentStreamEvent(delta="Claude binary not found", event_type="error")


def _init_git_workspace(path: Path) -> None:
    path.mkdir()
    (path / "tracked.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )


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
    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        runtime_event_store=store,
    )
    _attach_runtime_runner(
        ctrl,
        service=service,
        backend=backend,
        claude=claude,
        ledger=ledger,
        store=store,
    )
    return ctrl


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
async def test_claude_command_runs_claude_only_gate(ctrl_with_claude: CommandController) -> None:
    """/claude must bypass automatic Codex analysis and verification."""
    response = await ctrl_with_claude.handle(
        "/claude 修改 auth.py 添加空值检查",
        {"chat_id": 100, "user_id": 200},
    )
    assert "让 Codex 验收" in response.text
    await _drain_runtime_runner(ctrl_with_claude)
    claude = ctrl_with_claude._claude
    assert hasattr(claude, "calls")
    assert len(claude.calls) == 1
    active = ctrl_with_claude._ledger.get_active_conversation(100)
    assert active is not None
    assert ctrl_with_claude._ledger.list_orchestration_runs(active.id) == []
    assert len(ctrl_with_claude._backend.turns) == 0
    assert claude.calls[0] == "修改 auth.py 添加空值检查"


@pytest.mark.asyncio
async def test_claude_command_records_claude_direct_run(ctrl_with_claude: CommandController) -> None:
    """/claude records a Claude direct run without chief-engineer orchestration."""
    await ctrl_with_claude.handle(
        "/claude 修改 README",
        {"chat_id": 100, "user_id": 200},
    )
    await _drain_runtime_runner(ctrl_with_claude)
    active = ctrl_with_claude._ledger.get_active_conversation(100)
    assert active is not None
    assert active.active_claude_run_id is not None
    assert active.active_claude_run_id > 0
    assert active.active_codex_task_id is not None
    assert ctrl_with_claude._ledger.list_orchestration_runs(active.id) == []
    agent_runs = ctrl_with_claude._ledger.list_agent_runs(active.id)
    assert [(run.agent, run.role) for run in agent_runs] == [
        ("claude", "implementation"),
    ]


@pytest.mark.asyncio
async def test_chief_engineer_uses_runtime_runner_without_renderer(
    tmp_path: Path,
) -> None:
    """Chief mode must not fall back to the legacy non-runtime orchestration path."""

    class RunnerSpy:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start_chief_engineer(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return object()

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", tmp_path, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    claude = FakeClaudeBackendForController()
    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        runtime_event_store=store,
    )
    runner = RunnerSpy()
    ctrl.set_orchestration_runner(runner)

    response = await ctrl.handle(
        "/auto 修复 README",
        {"chat_id": 100, "user_id": 200},
    )

    assert len(runner.calls) == 1
    assert len(claude.calls) == 0
    assert response.already_rendered is False
    assert "已开始" in response.text


@pytest.mark.asyncio
async def test_terminal_state_followup_reuses_same_workbench(
    tmp_path: Path,
) -> None:
    """A failed/passed internal run must not force a new Workbench."""

    class RunnerSpy:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start_chief_engineer(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return object()

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", tmp_path, True),
    ))
    controller = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        claude_backend=FakeClaudeBackendForController(),
        runtime_event_store=store,
    )
    runner = RunnerSpy()
    controller.set_orchestration_runner(runner)

    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="真人历史现场 smoke",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_FAILED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id="1",
        correlation_id="failed-before-followup",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.USER,
        payload={"reason": "previous run failed"},
        occurred_at=now_iso(),
        conversation_id=conversation.id,
        orchestration_run_id=1,
    ))

    await controller.handle_conversation_text(
        "近日金价发我",
        {"chat_id": 100, "user_id": 200},
    )

    active = ledger.get_active_conversation(100)
    assert active is not None
    assert active.id == conversation.id
    assert ledger.get_conversation(conversation.id).archived_at is None
    assert len(runner.calls) == 1
    assert runner.calls[0]["conversation"].id == conversation.id

    closed = store._conn.execute(
        """
        SELECT 1 FROM runtime_events
        WHERE conversation_id = ?
          AND event_type = ?
        """,
        (conversation.id, EventType.CONVERSATION_CLOSED),
    ).fetchone()
    assert closed is None


@pytest.mark.asyncio
async def test_no_implementation_completion_records_pass_not_failed(
    tmp_path: Path,
) -> None:
    """Reply-only/no-op Codex analysis must close cleanly, not as failed."""

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    backend = FakeCodexBackend()
    backend._codex_responses = [
        (
            '{"summary":"default flow ok","needs_implementation":false,'
            '"files_to_touch":[],"implementation_steps":[]}'
        ),
    ]
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", tmp_path, True),
    ))
    claude = FakeClaudeBackendForController()
    controller = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        claude_backend=claude,
        runtime_event_store=store,
    )
    _attach_runtime_runner(
        controller,
        service=service,
        backend=backend,
        claude=claude,
        ledger=ledger,
        store=store,
    )

    await controller.handle_conversation_text(
        "请按默认流程只回复：default flow ok",
        {"chat_id": 100, "user_id": 200},
    )
    await _drain_runtime_runner(controller)

    task = service.list_tasks()[0]
    assert task.status is TaskStatus.DONE
    assert task.last_error == ""
    assert len(claude.calls) == 0

    verification = store._conn.execute(
        """
        SELECT payload_json FROM runtime_events
        WHERE conversation_id = ?
          AND event_type = ?
        """,
        (ledger.get_active_conversation(100).id, EventType.VERIFICATION_DECISION_RECORDED),
    ).fetchone()
    assert verification is not None
    assert '"decision": "pass"' in verification["payload_json"]


@pytest.mark.asyncio
async def test_chief_engineer_refuses_legacy_fallback_without_runtime_runner(
    tmp_path: Path,
) -> None:
    """Chief mode must fail closed instead of running outside runtime_events."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", tmp_path, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    claude = FakeClaudeBackendForController()
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
    )

    response = await ctrl.handle(
        "/auto 修复 README",
        {"chat_id": 100, "user_id": 200},
    )

    assert "编排器未初始化" in response.text
    assert len(backend.turns) == 0
    assert len(claude.calls) == 0


@pytest.mark.asyncio
async def test_conversation_text_uses_packet_render(ctrl_with_claude: CommandController) -> None:
    """Plain text must send packet.render() to Codex, not raw text."""
    await ctrl_with_claude.handle_conversation_text(
        "帮我分析这个模块",
        {"chat_id": 100, "user_id": 200},
    )
    await _drain_runtime_runner(ctrl_with_claude)
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
    assert "已开始" in response.text
    await _drain_runtime_runner(ctrl_with_claude)
    active = ctrl_with_claude._ledger.get_active_conversation(100)
    assert active is not None
    orch_runs = ctrl_with_claude._ledger.list_orchestration_runs(active.id)
    assert orch_runs[0].status == OrchestrationStatus.PASSED.value


@pytest.mark.asyncio
async def test_auto_mode_hides_english_model_snippets(
    ctrl_with_claude: CommandController,
) -> None:
    response = await ctrl_with_claude.handle(
        "/auto 修复登录 bug",
        {"chat_id": 100, "user_id": 200},
    )

    assert "已开始" in response.text
    assert "Analysis complete" not in response.text
    assert "Fake Claude implementation result" not in response.text
    assert "confidence: high" not in response.text


@pytest.mark.asyncio
async def test_streaming_auto_records_full_ledger_and_real_diff(tmp_path: Path) -> None:
    """Natural streaming /auto must leave the same audit trail as legacy orchestration."""
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    backend._codex_responses = [
        "Root cause: tracked.txt needs a change. Implementation needed.",
        "decision: pass\nsummary: Verified changed workspace.",
    ]
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    renderer = RecordingInteractionRenderer()
    claude = StreamingClaudeWritesTrackedFile(workspace)
    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        interaction_renderer=renderer,
        runtime_event_store=store,
    )
    _attach_runtime_runner(
        ctrl,
        service=service,
        backend=backend,
        claude=claude,
        ledger=ledger,
        renderer=renderer,
        store=store,
    )

    response = await ctrl.handle(
        "/auto 修改 tracked.txt",
        {"chat_id": 100, "user_id": 200},
    )

    assert response.already_rendered
    await _drain_runtime_runner(ctrl)
    active = ledger.get_active_conversation(100)
    assert active is not None
    assert active.active_claude_run_id is not None
    assert "总工程师" in active.conversation_summary

    agent_runs = ledger.list_agent_runs(active.id)
    assert [(run.agent, run.role, run.status) for run in agent_runs] == [
        ("codex", "analysis", AgentRunStatus.DONE.value),
        ("claude", "implementation", AgentRunStatus.DONE.value),
        ("codex", "verification", AgentRunStatus.DONE.value),
    ]

    orch_runs = ledger.list_orchestration_runs(active.id)
    assert orch_runs[0].status == OrchestrationStatus.PASSED.value
    decisions = ledger.list_orchestration_decisions(orch_runs[0].id)
    assert decisions
    assert decisions[-1].decision == "verify_passed"

    completed_events = [
        event for event in renderer.events
        if getattr(event, "event_type", "") == "run_completed"
    ]
    assert completed_events
    assert completed_events[-1].metadata["has_diff"] is True


@pytest.mark.asyncio
async def test_streaming_auto_binds_all_hidden_codex_threads_to_task(tmp_path: Path) -> None:
    """Hidden Codex analysis/verify threads must stay routable for EventBridge approvals."""
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = RecordingThreadCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    renderer = RecordingInteractionRenderer()
    claude = StreamingClaudeWritesTrackedFile(workspace)
    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        interaction_renderer=renderer,
        runtime_event_store=store,
    )
    _attach_runtime_runner(
        ctrl,
        service=service,
        backend=backend,
        claude=claude,
        ledger=ledger,
        renderer=renderer,
        store=store,
    )

    response = await ctrl.handle(
        "/auto 修改 tracked.txt",
        {"chat_id": 100, "user_id": 200},
    )

    assert response.already_rendered
    await _drain_runtime_runner(ctrl)
    active = ledger.get_active_conversation(100)
    assert active is not None
    assert active.active_codex_task_id is not None
    thread_ids = ledger.list_task_thread_ids(active.active_codex_task_id)
    assert thread_ids == ["hidden-thread-1", "hidden-thread-2"]


@pytest.mark.asyncio
async def test_streaming_auto_fails_ledger_on_claude_stream_error(tmp_path: Path) -> None:
    """Claude stream errors in /auto must fail the run, not continue to verification."""
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    backend._codex_responses = [
        "Root cause: tracked.txt needs a change. Implementation needed.",
        "decision: pass\nsummary: this verification must not run.",
    ]
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    renderer = RecordingInteractionRenderer()
    claude = StreamingClaudeError()
    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        interaction_renderer=renderer,
        runtime_event_store=store,
    )
    _attach_runtime_runner(
        ctrl,
        service=service,
        backend=backend,
        claude=claude,
        ledger=ledger,
        renderer=renderer,
        store=store,
    )

    response = await ctrl.handle(
        "/auto 修改 tracked.txt",
        {"chat_id": 100, "user_id": 200},
    )

    assert response.already_rendered
    await _drain_runtime_runner(ctrl)
    active = ledger.get_active_conversation(100)
    assert active is not None
    orch_runs = ledger.list_orchestration_runs(active.id)
    assert orch_runs[0].status == OrchestrationStatus.FAILED.value
    assert len(backend.turns) == 1  # analysis only; no Codex verification after Claude error

    agent_runs = ledger.list_agent_runs(active.id)
    assert [(run.agent, run.role, run.status) for run in agent_runs] == [
        ("codex", "analysis", AgentRunStatus.DONE.value),
        ("claude", "implementation", AgentRunStatus.FAILED.value),
    ]

    event_types = [getattr(event, "event_type", "") for event in renderer.events]
    assert "run_failed" in event_types
    assert "run_completed" not in event_types


@pytest.mark.asyncio
async def test_claude_direct_streaming_error_marks_agent_run_failed(tmp_path: Path) -> None:
    """Direct /claude stream errors must not leave the agent run queued."""
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    renderer = RecordingInteractionRenderer()
    backend = FakeCodexBackend()
    claude = StreamingClaudeError()
    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        interaction_renderer=renderer,
        runtime_event_store=store,
    )
    _attach_runtime_runner(
        ctrl,
        service=service,
        backend=backend,
        claude=claude,
        ledger=ledger,
        renderer=renderer,
        store=store,
    )

    response = await ctrl.handle(
        "/claude 修改 tracked.txt",
        {"chat_id": 100, "user_id": 200},
    )

    assert response.already_rendered
    await _drain_runtime_runner(ctrl)
    active = ledger.get_active_conversation(100)
    assert active is not None
    agent_runs = ledger.list_agent_runs(active.id)
    assert [(run.agent, run.role, run.status) for run in agent_runs] == [
        ("claude", "implementation", AgentRunStatus.FAILED.value),
    ]
    assert backend.turns == []


@pytest.mark.asyncio
async def test_claude_command_offers_explicit_codex_verification_action(
    ctrl_with_claude: CommandController,
) -> None:
    """/claude should offer explicit Codex verification, not auto-run it."""
    response = await ctrl_with_claude.handle(
        "/claude 修改 auth.py",
        {"chat_id": 100, "user_id": 200},
    )
    assert "让 Codex 验收" in response.text
    assert response.buttons
    await _drain_runtime_runner(ctrl_with_claude)


@pytest.mark.asyncio
async def test_conversation_callback_diff_action(ctrl_with_claude: CommandController) -> None:
    """conv: diff callback returns diff for the conversation."""
    from wlcodex.conversation_callback import ConversationCallback, DIFF

    # Setup a conversation first via /claude
    await ctrl_with_claude.handle("/claude 修改 auth.py", {"chat_id": 100, "user_id": 200})
    await _drain_runtime_runner(ctrl_with_claude)
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
    await _drain_runtime_runner(ctrl_with_claude)
    active = ctrl_with_claude._ledger.get_active_conversation(100)

    cb = ConversationCallback(conversation_id=active.id, action=VERIFY)
    response = await ctrl_with_claude.handle_conversation_callback(cb)
    assert response.text
    assert "验收" in response.text or "Codex" in response.text


@pytest.mark.asyncio
async def test_verify_response_hides_internal_codex_thread_id(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", tmp_path, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
    )

    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="verify hidden ids",
        mode="claude_direct",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "hidden thread", telegram_chat_id=100)
    task = service.set_task_thread(task.id, "thread-secret")
    ledger.set_conversation_active_task(conversation.id, task.id)
    run = ledger.create_agent_run(
        conversation_id=conversation.id,
        agent="claude",
        role="implementation",
        hidden_task_id=task.id,
        prompt_packet_summary="done",
    )
    ledger.update_agent_run_status(
        run.id,
        AgentRunStatus.DONE.value,
        completion_summary="done",
    )

    response = await ctrl.handle("/verify", {"chat_id": 100, "user_id": 200})

    assert "thread-secret" not in response.text
    assert "线程：" not in response.text
    assert "任务 #" not in response.text


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
    """decode_conversation_callback must reject the legacy waiting prefix."""
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


# ---------------------------------------------------------------------------
# Orchestrator exception → run marked as failed (no hanging runs)
# ---------------------------------------------------------------------------


class _RaisingCodexBackend:
    """Fake backend that raises on send_codex_prompt to test error paths."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.steers: list[tuple] = []

    async def send_codex_prompt(self, workspace: str, prompt: str) -> str:
        raise RuntimeError("Simulated Codex backend crash")

    async def create_thread(self, workspace: str) -> str:
        return "thread-1"

    async def start_turn(self, thread_id: str, prompt: str) -> None:
        self.turns.append((thread_id, prompt))

    def health(self):
        return type("h", (), {"is_healthy": True})()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_orchestrator_exception_marks_runs_as_failed(tmp_path: Path) -> None:
    """When orchestrator raises, orchestration_run and agent_run must be marked failed."""
    from wlcodex.controller import CommandController
    from wlcodex.db import Ledger
    from wlcodex.inspection import TaskInspector
    from wlcodex.task_service import TaskService
    from wlcodex.config import WorkspaceConfig
    from wlcodex.models import OrchestrationStatus, AgentRunStatus

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = _RaisingCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("demo", Path("/tmp/demo"), True),
        WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")

    claude = FakeClaudeBackendForController()

    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        runtime_event_store=store,
    )
    _attach_runtime_runner(
        ctrl,
        service=service,
        backend=backend,
        claude=claude,
        ledger=ledger,
        store=store,
    )

    response = await ctrl.handle(
        "/auto 修复崩溃 bug",
        {"chat_id": 100, "user_id": 200},
    )

    assert "已开始" in response.text
    await _drain_runtime_runner(ctrl)

    # Verify orchestration run is marked as failed (not left running)
    active = ledger.get_active_conversation(100)
    orch_runs = ledger.list_orchestration_runs(active.id)
    assert len(orch_runs) >= 1
    assert orch_runs[0].status == OrchestrationStatus.FAILED.value

    # Verify agent run is marked as failed (not left running)
    agent_runs = ledger.list_agent_runs(active.id)
    analysis_runs = [r for r in agent_runs if r.agent == "codex" and r.role == "analysis"]
    assert len(analysis_runs) >= 1
    assert analysis_runs[0].status == AgentRunStatus.FAILED.value


# ---------------------------------------------------------------------------
# Workbench history and workspace switching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workbenches_command_lists_archived_conversations(ctrl: CommandController) -> None:
    ledger = ctrl._ledger
    first = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="First",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.archive_conversation(first.id)
    ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Second",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle("/workbenches", {"chat_id": 100, "user_id": 7})

    assert "工作台历史" in response.text
    assert "First" in response.text
    assert "Second" in response.text


@pytest.mark.asyncio
async def test_workspaces_command_lists_configured_workspaces(ctrl: CommandController) -> None:
    response = await ctrl.handle("/workspaces", {"chat_id": 100, "user_id": 7})

    assert "可用工作区" in response.text
    assert "wlcodex" in response.text
    flat_buttons = [button for row in response.buttons for button in row]
    assert {
        button["callback_data"] for button in flat_buttons
    } == {
        "settings:workspace:demo",
        "settings:workspace:wlcodex",
    }


@pytest.mark.asyncio
async def test_switch_unknown_workspace_mentions_workspaces(ctrl: CommandController) -> None:
    conversation = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Demo",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle("/switch missing", {"chat_id": 100, "user_id": 7})

    assert conversation.id
    assert "/workspaces" in response.text


@pytest.mark.asyncio
async def test_exec_mode_command_updates_active_workbench_mode(
    ctrl: CommandController,
) -> None:
    conversation = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Mode Demo",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle(
        "/exec_mode codex_direct",
        {"chat_id": 100, "user_id": 7},
    )

    assert "已切换执行模式" in response.text
    assert "Codex 直聊" in response.text
    assert ctrl._ledger.get_conversation(conversation.id).mode == "codex_direct"


@pytest.mark.asyncio
async def test_continue_callback_does_not_create_new_workbench(
    ctrl: CommandController,
) -> None:
    from wlcodex.conversation_callback import CONTINUE, ConversationCallback

    conversation = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Continue Demo",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=conversation.id, action=CONTINUE)
    )

    assert "直接发消息" in response.text
    assert ctrl._ledger.get_active_conversation(100).id == conversation.id
    assert len(ctrl._ledger.list_conversations_by_chat(100, include_archived=True)) == 1


@pytest.mark.asyncio
async def test_status_callback_returns_current_workbench_status(
    ctrl: CommandController,
) -> None:
    from wlcodex.conversation_callback import STATUS, ConversationCallback

    conversation = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Status Demo",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=conversation.id, action=STATUS)
    )

    assert "当前对话：Status Demo" in response.text
    assert ctrl._ledger.get_active_conversation(100).id == conversation.id


@pytest.mark.asyncio
async def test_restore_workbench_callback_restores_archived_conversation(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import ConversationCallback

    ledger = ctrl._ledger
    old = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Old",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.archive_conversation(old.id)
    current = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Current",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=old.id, action="restore_workbench")
    )

    assert "已恢复工作台" in response.text
    assert ledger.get_conversation(old.id).archived_at is None
    assert ledger.get_conversation(current.id).archived_at is not None


@pytest.mark.asyncio
async def test_restore_workbench_records_activation_and_returns_to_product(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),
    ))
    controller = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        runtime_event_store=store,
    )
    old = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Old terminal workbench",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.archive_conversation(old.id)
    current = ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Current",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.CONVERSATION_MODE_SWITCHED,
        aggregate_type=AggregateType.CONVERSATION,
        aggregate_id=str(old.id),
        correlation_id="old-terminal-mode",
        source=EventSource.TELEGRAM,
        actor="user",
        visibility=Visibility.USER,
        payload={
            "chat_id": 100,
            "conversation_id": old.id,
            "from_mode": "product",
            "to_mode": "terminal",
            "active_agent": "codex",
        },
        occurred_at=now_iso(),
        conversation_id=old.id,
    ))

    from wlcodex.conversation_callback import ConversationCallback

    response = await controller.handle_conversation_callback(
        ConversationCallback(conversation_id=old.id, action="restore_workbench")
    )

    assert "已恢复工作台" in response.text
    old_events = store.list_by_conversation(old.id)
    current_events = store.list_by_conversation(current.id)
    old_event_types = [event.event_type for event in old_events]
    current_event_types = [event.event_type for event in current_events]
    assert EventType.CONVERSATION_ACTIVATED in old_event_types
    assert EventType.CONVERSATION_CLOSED in current_event_types
    mode_events = [
        event for event in old_events
        if event.event_type == EventType.CONVERSATION_MODE_SWITCHED
    ]
    assert mode_events[-1].payload["to_mode"] == "product"


# ---------------------------------------------------------------------------
# Workbench history and workspace switching integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_new_history_restore_status_flow(ctrl: CommandController) -> None:
    await ctrl.handle("/new First", {"chat_id": 100, "user_id": 7})
    first = ctrl._ledger.get_active_conversation(100)
    await ctrl.handle("/new Second", {"chat_id": 100, "user_id": 7})

    history = await ctrl.handle("/history", {"chat_id": 100, "user_id": 7})
    assert "First" in history.text
    assert "Second" in history.text

    from wlcodex.conversation_callback import ConversationCallback
    await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=first.id, action="restore_workbench")
    )

    status = await ctrl.handle("/status", {"chat_id": 100, "user_id": 7})
    assert "First" in status.text


@pytest.mark.asyncio
async def test_sessions_remains_current_workbench_scoped(ctrl: CommandController) -> None:
    """Verify /sessions stays Agent-session scoped, not Workbench history."""
    first = ctrl._ledger.create_conversation(
        chat_id=100, user_id=7, title="First",
        mode="chief_engineer", workspace_alias="wlcodex",
    )
    ctrl._ledger.create_agent_run(
        conversation_id=first.id,
        agent="codex",
        role="analysis",
        prompt_packet_summary="First hidden run",
    )
    ctrl._ledger.archive_conversation(first.id)
    second = ctrl._ledger.create_conversation(
        chat_id=100, user_id=7, title="Second",
        mode="chief_engineer", workspace_alias="wlcodex",
    )
    ctrl._ledger.create_agent_run(
        conversation_id=second.id,
        agent="claude",
        role="implementation",
        prompt_packet_summary="Second visible run",
    )

    response = await ctrl.handle("/sessions", {"chat_id": 100, "user_id": 7})

    assert "历史现场" in response.text
    assert "工作台列表" not in response.text
    assert "First" not in response.text
    assert "Second visible run" in response.text
