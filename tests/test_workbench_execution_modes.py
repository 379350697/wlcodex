"""Test execution-mode behavior: orchestrated, codex-only, claude-only.

Coverage per acceptance criteria:
- /codex runs Codex-only, never calls Claude
- /claude runs Claude-only, no auto Codex analysis or verification
- Plain text defaults to read-only Codex analysis
- /auto runs Codex -> Claude -> Codex
- Claude-only completion offers "让 Codex 验收" action
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wlcodex.agent_backend import AgentRequest, AgentResult, AgentStreamEvent
from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.conversation_callback import VERIFY, ConversationCallback, decode_conversation_callback
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.interaction.events import InteractionEvent
from wlcodex.models import ConversationMode, TaskStatus
from wlcodex.orchestrator import OrchestrationProgress
from wlcodex.runtime_events import RuntimeEvent
from wlcodex.task_service import TaskService


# ---------------------------------------------------------------------------
# Fake Claude backend — records calls so we can assert mode behavior
# ---------------------------------------------------------------------------

class FakeClaudeBackend:
    """Records invocations; raise if controller tries to enqueue Claude."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.interrupt_calls: list = []
        self.send_calls: list[AgentRequest] = []
        self._session_counter = 0

    async def send(self, request: AgentRequest) -> AgentResult:
        """Simulate non-streaming Claude execution."""
        self.send_calls.append(request)
        resume_id = str(request.extra.get("resume_session_id", ""))
        if resume_id:
            session_id = resume_id
        else:
            self._session_counter += 1
            session_id = f"claude-session-{self._session_counter}"
        return AgentResult(text="claude completed", exit_code=0, session_id=session_id)

    async def send_streaming(self, _request: object):
        """Minimal streaming stub."""
        yield AgentStreamEvent(delta="claude said something", event_type="text")

    def run(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("run", kwargs))

    def interrupt(self) -> None:
        self.interrupt_calls.append(True)


# ---------------------------------------------------------------------------
# Fake orchestrator — lets us assert whether the chief-engineer loop was
# started.
# ---------------------------------------------------------------------------

class FakeOrchestrationRunner:
    """Records start_chief_engineer calls so tests can assert routing."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []

    def start_chief_engineer(self, **kwargs: Any) -> None:
        self.starts.append(kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_controller(
    tmp_path: Path,
    *,
    claude: FakeClaudeBackend | None = None,
    orchestrator: FakeOrchestrationRunner | None = None,
    default_mode: str = "chief_engineer",
) -> CommandController:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (WorkspaceConfig("wlcodex", Path("/tmp/wlcodex"), True),))
    inspector = TaskInspector(ledger, tmp_path / "logs")

    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        default_mode=default_mode,
        default_workspace="wlcodex",
    )
    if orchestrator:
        ctrl.set_orchestration_runner(orchestrator)
    return ctrl


def mark_active_task_done(ctrl: CommandController, chat_id: int) -> None:
    active = ctrl._ledger.get_active_conversation(chat_id)
    assert active is not None
    assert active.active_codex_task_id is not None
    ctrl._ledger.set_task_status(active.active_codex_task_id, TaskStatus.DONE)


def has_verify_button(response_text: str, buttons: list[list[dict[str, str]]]) -> bool:
    """Return True if buttons include a VERIFY action (conv:...:verify)."""
    for row in buttons:
        for btn in row:
            cb = decode_conversation_callback(btn["callback_data"])
            if cb is not None and cb.action == VERIFY:
                return True
    return "让 Codex 验收" in response_text


# ---------------------------------------------------------------------------
# Test: Codex-only never enqueues Claude
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_codex_direct_never_calls_claude(tmp_path: Path) -> None:
    """``/codex <prompt>`` routes to Codex only — no Claude calls, no
    orchestrator chief-engineer start."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    resp = await ctrl.handle("/codex 分析这个模块", {"chat_id": 42, "user_id": 1})

    # Must NOT start the chief-engineer loop.
    assert len(runner.starts) == 0, (
        f"Codex-only triggered {len(runner.starts)} orchestrator starts, expected 0"
    )
    # Claude backend must not be called.
    assert len(claude.calls) == 0, (
        f"Codex-only recorded {len(claude.calls)} Claude call(s), expected 0"
    )
    # Codex app-server must have been invoked.
    assert len(ctrl._backend.turns) >= 1, (
        f"Codex backend turns={ctrl._backend.turns}, expected at least 1 turn"
    )
    # Spec L190-192: "这次只交给 Codex，不会调用 Claude 修改代码。"
    assert "只交给 Codex" in resp.text or "不会调用 Claude" in resp.text, (
        f"Codex-only response missing spec label. text={resp.text!r}"
    )


@pytest.mark.asyncio
async def test_codex_direct_command_labels_mode(tmp_path: Path) -> None:
    """CodexDirect response includes mode labelling so user knows it is not
    the /auto orchestrated flow."""
    claude = FakeClaudeBackend(enabled=True)
    ctrl = build_controller(tmp_path, claude=claude)

    resp = await ctrl.handle(
        "/codex 帮我分析",
        {"chat_id": 42, "user_id": 1},
    )

    # Spec L190-192: "这次只交给 Codex，不会调用 Claude 修改代码。"
    assert "只交给 Codex" in resp.text or "不会调用 Claude" in resp.text, (
        f"Codex-only response missing spec label. text={resp.text!r}"
    )


# ---------------------------------------------------------------------------
# Test: Claude-only does not auto-trigger Codex verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claude_direct_does_not_delegate_to_auto_mode(tmp_path: Path) -> None:
    """``/claude <prompt>`` must NOT delegate to handle_auto_mode.

    The key bug: handle_claude_direct was calling handle_auto_mode which
    runs Codex → Claude → Codex.  Claude-only means Claude direct,
    no automatic Codex analysis, no automatic Codex verification.
    """
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle("/claude 修改 README", {"chat_id": 42, "user_id": 1})

    # The conversation mode MUST be CLAUDE_DIRECT not CHIEF_ENGINEER.
    convos = ctrl._ledger.list_conversations_by_chat(42)
    assert convos, "Expected a conversation to be created"
    active = convos[0]
    assert active.mode == ConversationMode.CLAUDE_DIRECT.value, (
        f"Conversation mode is {active.mode!r}, expected {ConversationMode.CLAUDE_DIRECT.value!r}"
    )

    # Claude-only must NOT start the chief-engineer orchestration loop.
    assert len(runner.starts) == 0, (
        f"Claude-only triggered {len(runner.starts)} orchestrator starts, expected 0"
    )

    # Claude-only must NOT create a Codex analysis agent run.
    runs = ctrl._ledger.list_agent_runs(active.id, limit=10)
    codex_runs = [r for r in runs if r.agent == "codex"]
    assert len(codex_runs) == 0, (
        f"Claude-only created {len(codex_runs)} Codex agent runs, expected 0"
    )

    # A Claude agent run should be present.
    claude_runs = [r for r in runs if r.agent == "claude"]
    assert len(claude_runs) >= 1, "Claude-only should create a Claude agent run"

    # Claude must be ACTUALLY invoked (not just a database record).
    # The background task runs asynchronously — wait briefly for it.
    import asyncio as _asyncio
    await _asyncio.sleep(0.1)
    assert len(claude.send_calls) >= 1, (
        f"Claude.send() was NOT called. Agent run is a zombie record.\n"
        f"send_calls={claude.send_calls!r}"
    )


@pytest.mark.asyncio
async def test_claude_direct_response_includes_verify_affordance(tmp_path: Path) -> None:
    """Claude-only completion offers a '让 Codex 验收' action."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    resp = await ctrl.handle("/claude 改一下", {"chat_id": 42, "user_id": 1})

    assert has_verify_button(resp.text, resp.buttons), (
        f"Claude-only response missing verify affordance.\n"
        f"text={resp.text!r}\nbuttons={resp.buttons!r}"
    )


@pytest.mark.asyncio
async def test_claude_direct_disabled_claude_shows_error(tmp_path: Path) -> None:
    """When Claude is not enabled, /claude returns a clear error."""
    claude = FakeClaudeBackend(enabled=False)
    ctrl = build_controller(tmp_path, claude=claude)

    resp = await ctrl.handle("/claude 修 bug", {"chat_id": 42, "user_id": 1})

    assert "未启用" in resp.text or "enable" in resp.text.lower()


# ---------------------------------------------------------------------------
# Test: Plain text is read-only by default; /auto starts orchestration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conversation_text_defaults_to_codex_only(tmp_path: Path) -> None:
    """Ordinary text must not silently start Claude implementation."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle_conversation_text(
        "重做终端体验",
        {"chat_id": 42, "user_id": 1},
    )

    assert runner.starts == []
    convos = ctrl._ledger.list_conversations_by_chat(42)
    assert convos
    assert convos[0].mode == ConversationMode.CHIEF_ENGINEER.value
    runs = ctrl._ledger.list_agent_runs(convos[0].id, limit=10)
    assert [(run.agent, run.role) for run in runs] == [("codex", "analysis")]
    assert ctrl._backend.prompt_turns[-1][2] == "read_only_analysis"
    assert "禁止创建、修改、删除任何工作区文件" in ctrl._backend.turns[-1][1]
    assert "Claude handoff packet" not in ctrl._backend.turns[-1][1]


@pytest.mark.asyncio
async def test_auto_mode_explicit_starts_orchestrated(tmp_path: Path) -> None:
    """``/auto <prompt>`` explicitly starts Codex → Claude → Codex."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle("/auto 修复登录 bug", {"chat_id": 42, "user_id": 1})

    assert len(runner.starts) == 1, (
        f"/auto should trigger exactly 1 orchestrator start, got {len(runner.starts)}"
    )
    convos = ctrl._ledger.list_conversations_by_chat(42)
    assert convos
    assert convos[0].mode == ConversationMode.CHIEF_ENGINEER.value


# ---------------------------------------------------------------------------
# Test: Claude-only conversation mode does not silently change default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claude_direct_conversation_mode_is_claude_direct(tmp_path: Path) -> None:
    """After /claude, the active conversation's mode is CLAUDE_DIRECT."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle("/claude 改个标题", {"chat_id": 42, "user_id": 1})
    resp = await ctrl.handle("/status", {"chat_id": 42, "user_id": 1})

    # Must NOT show chief_engineer mode for this conversation.
    convos = ctrl._ledger.list_conversations_by_chat(42)
    assert convos, "Expected a conversation"
    assert convos[0].mode == ConversationMode.CLAUDE_DIRECT.value, (
        f"Expected claude_direct mode, got {convos[0].mode!r}"
    )


# ---------------------------------------------------------------------------
# Test: Codex-direct also records correct mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_codex_direct_conversation_mode_is_codex_direct(tmp_path: Path) -> None:
    """After /codex, the active conversation's mode is CODEX_DIRECT."""
    claude = FakeClaudeBackend(enabled=True)
    ctrl = build_controller(tmp_path, claude=claude)

    await ctrl.handle("/codex 分析", {"chat_id": 42, "user_id": 1})

    convos = ctrl._ledger.list_conversations_by_chat(42)
    assert convos, "Expected a conversation"
    assert convos[0].mode == ConversationMode.CODEX_DIRECT.value, (
        f"Expected codex_direct mode, got {convos[0].mode!r}"
    )


# ---------------------------------------------------------------------------
# Test: Claude actually runs to completion in background
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claude_direct_actually_invokes_claude_subprocess(tmp_path: Path) -> None:
    """Claude-only MUST actually call Claude, not just create a database record.

    Semantic drift caught: _handle_claude_direct_impl was creating an agent
    run marked 'running' but never invoking Claude.  The run was a zombie.
    """
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle("/claude 修复颜色", {"chat_id": 42, "user_id": 1})

    # Give the background asyncio task time to execute.
    import asyncio as _asyncio
    await _asyncio.sleep(0.1)

    # Claude.send() must have been called exactly once.
    assert len(claude.send_calls) == 1, (
        f"Expected 1 Claude send() call, got {len(claude.send_calls)}. "
        f"Claude was never invoked."
    )
    assert claude.send_calls[0].prompt == "修复颜色"

    # Agent run must have transitioned from 'running' → 'done'.
    convos = ctrl._ledger.list_conversations_by_chat(42)
    assert convos
    runs = ctrl._ledger.list_agent_runs(convos[0].id, limit=10)
    claude_runs = [r for r in runs if r.agent == "claude"]
    assert len(claude_runs) == 1
    assert claude_runs[0].status == "done", (
        f"Claude agent run status is {claude_runs[0].status!r}, expected 'done'"
    )


@pytest.mark.asyncio
async def test_claude_direct_marks_hidden_task_done_and_releases_workspace(tmp_path: Path) -> None:
    """A completed Claude-only run must not leave its hidden task blocking /task."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle("/claude 只回复 ok", {"chat_id": 42, "user_id": 1})

    import asyncio as _asyncio
    await _asyncio.sleep(0.1)

    convos = ctrl._ledger.list_conversations_by_chat(42)
    assert convos
    runs = ctrl._ledger.list_agent_runs(convos[0].id, limit=10)
    claude_runs = [r for r in runs if r.agent == "claude"]
    assert len(claude_runs) == 1
    assert claude_runs[0].hidden_task_id is not None

    hidden_task = ctrl._service.get_task(claude_runs[0].hidden_task_id)
    assert hidden_task.status == TaskStatus.DONE
    assert ctrl._service.blocker_for_workspace("wlcodex") is None

    next_task = ctrl._service.reserve_task("wlcodex", "legacy smoke")
    assert next_task.status == TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# Test: plain text remains read-only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_text_read_only_flow_reuses_chief_engineer_workbench(
    tmp_path: Path,
) -> None:
    """Plain text keeps the workbench but does not start Claude orchestration."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle_conversation_text(
        "帮我修复 dark mode 颜色",
        {"chat_id": 99, "user_id": 2},
    )

    assert runner.starts == []

    convos = ctrl._ledger.list_conversations_by_chat(99)
    assert len(convos) == 1
    assert convos[0].mode == ConversationMode.CHIEF_ENGINEER.value
    runs = ctrl._ledger.list_agent_runs(convos[0].id, limit=10)
    assert [(run.agent, run.role) for run in runs] == [("codex", "analysis")]


@pytest.mark.asyncio
async def test_codex_thread_is_scoped_to_workbench_until_new(tmp_path: Path) -> None:
    """All Codex interactions in one Workbench reuse the same Codex thread."""
    claude = FakeClaudeBackend(enabled=True)
    ctrl = build_controller(tmp_path, claude=claude)
    ctx = {"chat_id": 42, "user_id": 1}

    await ctrl.handle("/new", ctx)
    await ctrl.handle_conversation_text("先分析登录问题", ctx)
    first = ctrl._ledger.get_active_conversation(42)
    assert first is not None
    first_thread = first.codex_thread_id
    assert first_thread
    mark_active_task_done(ctrl, 42)

    await ctrl.handle("/codex 继续按刚才的问题补充分析", ctx)
    same = ctrl._ledger.get_active_conversation(42)
    assert same is not None
    assert same.id == first.id
    assert same.codex_thread_id == first_thread

    turn_threads = [thread_id for thread_id, _prompt in ctrl._backend.turns]
    assert turn_threads[:2] == [first_thread, first_thread]
    mark_active_task_done(ctrl, 42)

    await ctrl.handle("/new", ctx)
    await ctrl.handle_conversation_text("这是新工作台的问题", ctx)
    fresh = ctrl._ledger.get_active_conversation(42)
    assert fresh is not None
    assert fresh.id != first.id
    assert fresh.codex_thread_id
    assert fresh.codex_thread_id != first_thread


@pytest.mark.asyncio
async def test_auto_reuses_workbench_codex_thread(tmp_path: Path) -> None:
    """``/auto`` continues the Codex side of the current Workbench."""
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)
    ctx = {"chat_id": 42, "user_id": 1}

    await ctrl.handle("/new", ctx)
    await ctrl.handle_conversation_text("先查清楚失败根因", ctx)
    active = ctrl._ledger.get_active_conversation(42)
    assert active is not None
    codex_thread_id = active.codex_thread_id
    assert codex_thread_id
    mark_active_task_done(ctrl, 42)

    await ctrl.handle("/auto 按刚才结论执行修复", ctx)

    assert len(runner.starts) == 1
    assert runner.starts[0]["conversation"].id == active.id
    assert runner.starts[0]["codex_thread_id"] == codex_thread_id


@pytest.mark.asyncio
async def test_claude_session_is_scoped_to_workbench_until_new(tmp_path: Path) -> None:
    """Claude direct runs resume the same Claude Code session until /new."""
    claude = FakeClaudeBackend(enabled=True)
    ctrl = build_controller(tmp_path, claude=claude)
    ctx = {"chat_id": 42, "user_id": 1}

    await ctrl.handle("/new", ctx)
    await ctrl.handle("/claude 第一次改 README", ctx)
    import asyncio as _asyncio
    await _asyncio.sleep(0.1)
    first = ctrl._ledger.get_active_conversation(42)
    assert first is not None
    first_session = first.claude_session_id
    assert first_session

    await ctrl.handle("/claude 继续刚才 Claude 的修改", ctx)
    await _asyncio.sleep(0.1)
    same = ctrl._ledger.get_active_conversation(42)
    assert same is not None
    assert same.id == first.id
    assert same.claude_session_id == first_session
    assert claude.send_calls[-1].extra["resume_session_id"] == first_session

    await ctrl.handle("/new", ctx)
    await ctrl.handle("/claude 新工作台不要继承旧 Claude 会话", ctx)
    await _asyncio.sleep(0.1)
    fresh = ctrl._ledger.get_active_conversation(42)
    assert fresh is not None
    assert fresh.id != first.id
    assert fresh.claude_session_id
    assert fresh.claude_session_id != first_session
    assert "resume_session_id" not in claude.send_calls[-1].extra
