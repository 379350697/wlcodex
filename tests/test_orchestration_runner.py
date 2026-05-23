import asyncio
from pathlib import Path
from typing import Any

import pytest

from wlcodex.agent_backend import AgentRequest, AgentStreamEvent
from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.interaction.events import InteractionEvent
from wlcodex.models import AgentRunStatus, OrchestrationStatus, TaskStatus
from wlcodex.orchestrator import OrchestrationProgress
from wlcodex.runtime_events import (
    EventType,
)
from wlcodex.orchestration_runner import OrchestrationRunner
from wlcodex.task_service import TaskService


class RecordingRenderer:
    def __init__(self) -> None:
        self.events: list[InteractionEvent] = []

    async def handle(self, event: InteractionEvent) -> None:
        self.events.append(event)


class EnabledClaude:
    enabled = True

    async def send_streaming(self, _request: object):
        yield AgentStreamEvent(delta="implemented", event_type="text")


class RuntimeAwareClaude:
    enabled = True

    def __init__(self) -> None:
        self.sources: list[object | None] = []
        self.current_source: object | None = None

    def set_runtime_source(self, source: object | None) -> None:
        self.current_source = source
        self.sources.append(source)

    async def send_streaming(self, _request: object):
        from wlcodex.claude_stream_parser import ClaudeStreamEvent

        assert self.current_source is not None
        self.current_source.emit(ClaudeStreamEvent(
            runtime_event_type=EventType.TOOL_CALL_STARTED,
            runtime_payload={"tool_name": "Bash", "tool_id": "tool-1"},
        ))
        self.current_source.emit(ClaudeStreamEvent(
            runtime_event_type=EventType.MODEL_USAGE_UPDATED,
            runtime_payload={"input_tokens": 12, "output_tokens": 4},
            agent_usage={"input_tokens": 12, "output_tokens": 4},
        ))
        yield AgentStreamEvent(delta="implemented", event_type="text")


class RuntimeSourceAwareOrchestrator:
    def __init__(self, _codex: object, claude: object) -> None:
        self._claude = claude

    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):

        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="analysis start",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text="analysis",
            full_text="analysis",
            agent="codex",
        )
        async for stream_event in self._claude.send_streaming(
            AgentRequest(prompt="impl", workspace_path=str(conversation_context["workspace"]))
        ):
            yield OrchestrationProgress(
                phase=OrchestrationProgress.IMPL_DELTA,
                text=stream_event.delta,
                agent="claude",
                round_num=1,
            )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.IMPL_COMPLETE,
            text="implemented",
            full_text="implemented",
            agent="claude",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_STARTED,
            text="verify",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_COMPLETE,
            text="decision: pass",
            full_text="decision: pass\nsummary: ok",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text="done",
            full_text="decision: pass\nsummary: ok",
            agent="codex",
            result_status="passed",
            round_num=1,
        )


class RuntimeAwareCodexBackend(FakeCodexBackend):
    def __init__(self) -> None:
        super().__init__()
        self._runtime_event_callback = None

    def set_runtime_event_callback(self, callback):
        self._runtime_event_callback = callback

    async def send_codex_prompt(self, workspace_path, prompt, **kwargs):
        from wlcodex.codex_backend import BackendEvent

        assert self._runtime_event_callback is not None
        self._runtime_event_callback(BackendEvent("token_usage_updated", {
            "inputTokens": 21,
            "outputTokens": 7,
        }))
        return "decision: pass\nsummary: codex ok"


class RuntimeCommandCodexBackend(FakeCodexBackend):
    def __init__(self) -> None:
        super().__init__()
        self._runtime_event_callback = None

    def set_runtime_event_callback(self, callback):
        self._runtime_event_callback = callback

    async def send_codex_prompt(self, workspace_path, prompt, **kwargs):
        from wlcodex.codex_backend import BackendEvent

        assert self._runtime_event_callback is not None
        self._runtime_event_callback(BackendEvent("item_started", {
            "item": {
                "id": "cmd-1",
                "type": "commandExecution",
                "command": "pytest tests/ -q",
            }
        }))
        self._runtime_event_callback(BackendEvent("token_usage_updated", {
            "inputTokens": 21,
            "outputTokens": 7,
        }))
        return "decision: pass\nsummary: codex ok"


class RuntimeSourceAwareCodexOrchestrator:
    def __init__(self, codex: object, _claude: object) -> None:
        self._codex = codex

    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="analysis start",
            agent="codex",
        )
        analysis = await self._codex.send_codex_prompt(
            str(conversation_context["workspace"]),
            "analysis prompt",
            interaction_mode="analysis",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text=analysis,
            full_text=analysis,
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text="done",
            full_text=analysis,
            agent="codex",
            result_status="passed",
            round_num=0,
        )


class MismatchedVerificationCompleteOrchestrator:
    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="analysis start",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text="analysis",
            full_text="analysis",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.IMPL_COMPLETE,
            text="impl",
            full_text="impl",
            agent="claude",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_STARTED,
            text="verify",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_COMPLETE,
            text="decision: retry",
            full_text="decision: retry\nrequired_fix: more work",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text="done",
            full_text="decision: pass\nsummary: inconsistent",
            agent="codex",
            result_status="passed",
            round_num=1,
        )


class NeverFinishingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tasks: list[asyncio.Task[None]] = []

    def start_chief_engineer(self, **kwargs: Any) -> asyncio.Task[None]:
        self.calls.append(kwargs)

        async def never_finish() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(never_finish())
        self.tasks.append(task)
        return task


def _build_runtime(tmp_path: Path) -> tuple[
    Ledger,
    TaskService,
    FakeCodexBackend,
    RecordingRenderer,
    Path,
]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(
        ledger,
        (WorkspaceConfig("wlcodex", workspace, True),),
        task_log_dir=tmp_path / "logs",
    )
    renderer = RecordingRenderer()
    return ledger, service, backend, renderer, workspace


@pytest.mark.asyncio
async def test_controller_starts_staged_auto_collecting_context(
    tmp_path: Path,
) -> None:
    """Staged /auto starts collecting_context via Codex, not eager runner."""
    ledger, service, backend, renderer, _workspace = _build_runtime(tmp_path)
    runner = NeverFinishingRunner()
    controller = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        claude_backend=EnabledClaude(),
        default_workspace="wlcodex",
        interaction_renderer=renderer,
        orchestration_runner=runner,
    )

    response = await asyncio.wait_for(
        controller.handle(
            "/auto 实现一个需要多轮验收的小功能",
            {"chat_id": 100, "user_id": 200},
        ),
        timeout=0.05,
    )

    # Staged /auto renders via interaction_renderer (not via runner)
    assert response.already_rendered is True
    # The runner is NOT called — staged-auto doesn't use start_chief_engineer
    assert len(runner.calls) == 0

    # Orchestration run should be created in collecting_context, running
    orch_runs = ledger.list_orchestration_runs(1)
    assert len(orch_runs) == 1
    assert orch_runs[0].current_step == "collecting_context"
    assert orch_runs[0].status == OrchestrationStatus.RUNNING

    # A codex analysis agent run should be created
    agent_runs = ledger.list_agent_runs(1)
    assert len(agent_runs) == 1
    assert agent_runs[0].agent == "codex"
    assert agent_runs[0].role == "auto_analysis"

    assert any(event.event_type == "run_started" for event in renderer.events)

    for task in runner.tasks:
        task.cancel()
    await asyncio.gather(*runner.tasks, return_exceptions=True)


class FakeStreamingOrchestrator:
    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="Codex 正在分析需求...",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text="analysis",
            full_text="analysis packet",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.IMPL_DELTA,
            text="patch ",
            agent="claude",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.IMPL_DELTA,
            text="done",
            agent="claude",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.IMPL_COMPLETE,
            text="patch done",
            full_text="patch done",
            agent="claude",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_STARTED,
            text="Codex 正在验收...",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_COMPLETE,
            text="decision: pass",
            full_text="decision: pass\nsummary: ok",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text="验收通过",
            full_text="decision: pass\nsummary: ok",
            agent="codex",
            result_status="passed",
            round_num=1,
        )


class RawVerboseStreamingOrchestrator:
    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="RAW_CODEX_ANALYSIS_STARTED",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text="RAW_CODEX_ANALYSIS",
            full_text="RAW_CODEX_ANALYSIS_FULL",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.IMPL_DELTA,
            text="RAW_CLAUDE_DELTA",
            agent="claude",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.IMPL_COMPLETE,
            text="RAW_CLAUDE_IMPL",
            full_text="RAW_CLAUDE_IMPL_FULL",
            agent="claude",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_STARTED,
            text="RAW_VERIFY_STARTED",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.VERIFY_COMPLETE,
            text="RAW_VERIFY_RESULT",
            full_text="decision: pass\nsummary: RAW_VERIFY_RESULT_FULL",
            agent="codex",
            round_num=1,
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text="RAW_COMPLETE_TEXT",
            full_text="decision: pass\nsummary: RAW_COMPLETE_FULL",
            agent="codex",
            result_status="passed",
            round_num=1,
        )


class ReplyOnlyStreamingOrchestrator:
    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="analysis start",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text="wlcodex telegram live ok",
            full_text="wlcodex telegram live ok",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text="wlcodex telegram live ok",
            full_text="wlcodex telegram live ok",
            agent="codex",
            result_status="passed",
            round_num=0,
        )


class JsonReplyOnlyStreamingOrchestrator:
    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        payload = (
            '{"summary":"wlcodex telegram live ok",'
            '"needs_implementation":false,'
            '"files_to_touch":[],'
            '"implementation_steps":[]}'
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="analysis start",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text=payload,
            full_text=payload,
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text=payload,
            full_text=payload,
            agent="codex",
            result_status="passed",
            round_num=0,
        )


class NeedsUserFullTextStreamingOrchestrator:
    async def run_streaming(
        self,
        _prompt: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        full = (
            "诊断完成，需要用户确认。\n"
            "结论：上一版部署生效，但残留诊断需要人工确认。\n"
            "涉及文件：lightfee/runtime.py\n"
            "实施步骤：1. 修复诊断分组；2. 重跑 smoke。\n"
            "验收标准：Telegram 输出完整中文结论，不再只显示失败摘要。"
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="analysis start",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text="analysis done",
            full_text="analysis full",
            agent="codex",
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text="需要用户输入。",
            full_text=full,
            agent="codex",
            result_status="needs_user",
            round_num=0,
        )


@pytest.mark.asyncio
async def test_orchestration_runner_records_passed_background_result(
    tmp_path: Path,
) -> None:
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="新对话",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "实现功能", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "实现功能")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: FakeStreamingOrchestrator(),
    )

    background_task = runner.start_chief_engineer(
        prompt="实现功能",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    assert ledger.get_task(task.id).status == TaskStatus.DONE
    updated_run = ledger.get_orchestration_run(orch_run.id)
    assert updated_run.status == OrchestrationStatus.PASSED
    assert updated_run.verify_round == 1
    assert updated_run.last_codex_analysis == "analysis packet"
    assert updated_run.last_claude_summary == "patch done"
    assert updated_run.last_verification_result == "decision: pass\nsummary: ok"

    runs = ledger.list_agent_runs(conversation.id)
    assert [(run.agent, run.role, run.status) for run in runs] == [
        ("codex", "analysis", AgentRunStatus.DONE),
        ("claude", "implementation", AgentRunStatus.DONE),
        ("codex", "verification", AgentRunStatus.DONE),
    ]
    assert any(event.event_type == "text_delta" for event in renderer.events)
    assert renderer.events[-1].event_type == "run_completed"


@pytest.mark.asyncio
async def test_orchestration_runner_humanizes_telegram_progress_without_raw_model_text(
    tmp_path: Path,
) -> None:
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="新对话",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "实现功能", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "实现功能")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: RawVerboseStreamingOrchestrator(),
    )

    background_task = runner.start_chief_engineer(
        prompt="实现功能",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    visible_text = "\n".join(
        event.text for event in renderer.events if event.event_type == "text_delta"
    )
    assert "RAW_" not in visible_text
    assert "交给 Claude" in visible_text
    assert "验收" in visible_text

    updated_run = ledger.get_orchestration_run(orch_run.id)
    assert updated_run.last_codex_analysis == "RAW_CODEX_ANALYSIS_FULL"
    assert updated_run.last_claude_summary == "RAW_CLAUDE_IMPL_FULL"
    assert "RAW_VERIFY_RESULT_FULL" in updated_run.last_verification_result


@pytest.mark.asyncio
async def test_orchestration_runner_delivers_needs_user_full_text(
    tmp_path: Path,
) -> None:
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    renderer._runtime_progress = object()
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="诊断需要确认",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "诊断任务", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "诊断任务")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=(
            lambda _codex, _claude: NeedsUserFullTextStreamingOrchestrator()
        ),
    )

    background_task = runner.start_chief_engineer(
        prompt="诊断任务",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    visible_text = "\n".join(
        event.text for event in renderer.events if event.event_type == "text_delta"
    )
    assert "结论：上一版部署生效" in visible_text
    assert "涉及文件：lightfee/runtime.py" in visible_text
    assert "实施步骤" in visible_text
    assert "验收标准" in visible_text
    failed = [event for event in renderer.events if event.event_type == "run_failed"][-1]
    assert failed.conversation_id == conversation.id
    assert failed.text == "需要用户输入以继续。"


@pytest.mark.asyncio
async def test_orchestration_runner_suppresses_phase_text_delta_when_runtime_progress_is_enabled(
    tmp_path: Path,
) -> None:
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    renderer._runtime_progress = object()
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="新对话",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "实现功能", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "实现功能")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: RawVerboseStreamingOrchestrator(),
    )

    background_task = runner.start_chief_engineer(
        prompt="实现功能",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    assert any(event.event_type == "runtime_progress" for event in renderer.events)
    visible_text = "\n".join(
        event.text for event in renderer.events if event.event_type == "text_delta"
    )
    assert "我先看需求和改动范围" not in visible_text
    assert "方案看完了" not in visible_text
    assert "Claude 正在改代码" not in visible_text
    assert "Claude 改完了" not in visible_text
    assert "我在验收改动" not in visible_text
    assert "验收结果出来了" not in visible_text


@pytest.mark.asyncio
async def test_orchestration_runner_delivers_reply_only_answer_with_runtime_progress(
    tmp_path: Path,
) -> None:
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    renderer._runtime_progress = object()
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="真人历史现场 smoke 2",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task(
        "wlcodex", "请用中文只回复：wlcodex telegram live ok",
        telegram_chat_id=100,
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "reply only")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: ReplyOnlyStreamingOrchestrator(),
    )

    background_task = runner.start_chief_engineer(
        prompt="请用中文只回复：wlcodex telegram live ok",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    visible_text = "\n".join(
        event.text for event in renderer.events if event.event_type == "text_delta"
    )
    assert "wlcodex telegram live ok" in visible_text
    completed_events = [
        event for event in renderer.events if event.event_type == "run_completed"
    ]
    assert completed_events
    assert completed_events[-1].metadata["runtime_state"].phase == "completed"
    assert renderer.events[-1].event_type == "run_completed"


@pytest.mark.asyncio
async def test_orchestration_runner_delivers_json_reply_summary_only(
    tmp_path: Path,
) -> None:
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    renderer._runtime_progress = object()
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="真人历史现场 smoke 3",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task(
        "wlcodex", "请用中文只回复：wlcodex telegram live ok",
        telegram_chat_id=100,
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "reply only json")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: JsonReplyOnlyStreamingOrchestrator(),
    )

    background_task = runner.start_chief_engineer(
        prompt="请用中文只回复：wlcodex telegram live ok",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    visible_text = "\n".join(
        event.text for event in renderer.events if event.event_type == "text_delta"
    )
    assert "wlcodex telegram live ok" in visible_text
    assert "needs_implementation" not in visible_text
    assert "files_to_touch" not in visible_text


@pytest.mark.asyncio
async def test_orchestration_runner_records_workflow_overhead_usage_events(
    tmp_path: Path,
) -> None:
    """Chief-engineer run records workflow overhead usage events at phase transitions."""
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="Usage test",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "usage test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "usage test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: FakeStreamingOrchestrator(),
    )

    background_task = runner.start_chief_engineer(
        prompt="usage test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    # Check workflow overhead events were recorded
    workflow_events = ledger.list_usage_events(
        orchestration_run_id=orch_run.id, agent="workflow"
    )
    phases = {e.phase for e in workflow_events}
    assert "codex_analysis" in phases
    assert "codex_to_claude_handoff" in phases
    assert "codex_verification" in phases
    assert len(workflow_events) == 3

    for event in workflow_events:
        assert event.agent == "workflow"
        assert event.source == "estimated"
        assert event.workflow_overhead_input_tokens > 0

    # Orchestration-level aggregation should show workflow overhead
    agg = ledger.aggregate_usage(orchestration_run_id=orch_run.id)
    assert agg["workflow"]["requests"] == 3
    assert agg["totals"]["workflow_overhead_input_tokens"] > 0


@pytest.mark.asyncio
async def test_orchestration_runner_legacy_compat_token_fields_preserved(
    tmp_path: Path,
) -> None:
    """Existing tasks.token_input/token_output and agent_runs fields still work."""
    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="Compat", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "compat test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "compat test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: FakeStreamingOrchestrator(),
    )

    background_task = runner.start_chief_engineer(
        prompt="compat test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
    )
    await background_task

    # Agent runs still exist with their token fields
    runs = ledger.list_agent_runs(conversation.id)
    assert len(runs) == 3  # codex analysis, claude impl, codex verify
    for run in runs:
        assert run.status == AgentRunStatus.DONE

    # Orchestration run is properly recorded
    orch = ledger.get_orchestration_run(orch_run.id)
    assert orch.status == OrchestrationStatus.PASSED

    # Conversation summary is updated
    assert "验收通过" in ledger.get_conversation(conversation.id).conversation_summary


# ---------------------------------------------------------------------------
# Runtime event integration tests (Lane G)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_events_emitted_for_full_chief_engineer_loop(
    tmp_path: Path,
) -> None:
    """Every phase transition and agent lifecycle emits runtime events."""
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="RT", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "rt test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "rt test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: FakeStreamingOrchestrator(),
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="rt test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-cid-001",
    )
    await background_task

    events = store.list_by_correlation("test-cid-001")
    event_types = [e.event_type for e in events]

    # Must include run lifecycle events
    assert EventType.RUN_STARTED in event_types
    assert EventType.RUN_PHASE_CHANGED in event_types
    assert EventType.RUN_COMPLETED in event_types

    # Must include agent lifecycle events
    assert EventType.AGENT_RUN_STARTED in event_types
    assert EventType.AGENT_RUN_COMPLETED in event_types

    # Must include verification events
    assert EventType.VERIFICATION_STARTED in event_types
    assert EventType.VERIFICATION_DECISION_RECORDED in event_types
    assert EventType.VERIFICATION_COMPLETED in event_types

    # All events share the same correlation_id
    for e in events:
        assert e.correlation_id == "test-cid-001"

    # Events are in id order
    for i in range(1, len(events)):
        assert events[i].id > events[i - 1].id


@pytest.mark.asyncio
async def test_claude_agent_run_created_before_claude_streams(
    tmp_path: Path,
) -> None:
    """Claude agent_run must be created with 'running' status before Claude emits deltas."""
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="CR", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "cr test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "cr test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: FakeStreamingOrchestrator(),
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="cr test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-cid-002",
    )
    await background_task

    events = store.list_by_correlation("test-cid-002")

    # Find first Claude agent.run.started event and first IMPL_DELTA
    claude_started = None
    first_claude_impl_phase = None
    for e in events:
        if e.event_type == EventType.AGENT_RUN_STARTED and e.actor == "claude":
            claude_started = e
            break
    # Find first run.phase.changed to running_implementation
    for e in events:
        if (e.event_type == EventType.RUN_PHASE_CHANGED
                and e.payload.get("phase") == "running_implementation"):
            first_claude_impl_phase = e
            break

    assert claude_started is not None, "Claude agent.run.started must exist"
    # Claude agent.run.started must be emitted at or before the implementation phase
    if first_claude_impl_phase is not None:
        assert claude_started.id <= first_claude_impl_phase.id, (
            "agent.run.started must come at or before running_implementation phase"
        )

    # The agent_runs table must show a claude run with running/done status
    claude_runs = [
        r for r in ledger.list_agent_runs(conversation.id)
        if r.agent == "claude"
    ]
    assert len(claude_runs) >= 1
    assert claude_runs[0].status in (AgentRunStatus.RUNNING, AgentRunStatus.DONE)


@pytest.mark.asyncio
async def test_verification_retry_not_marked_as_completed(
    tmp_path: Path,
) -> None:
    """verification.retry.requested must be emitted for retry, NOT verification.completed."""
    from wlcodex.runtime_event_store import RuntimeEventStore

    # An orchestrator that always retries verification then passes on round 2
    class RetryThenPassOrchestrator:
        async def run_streaming(
            self,
            _prompt: str,
            conversation_context: dict | None = None,
        ):
            from wlcodex.orchestrator import OrchestrationProgress
            # Round 1
            yield OrchestrationProgress(
                phase=OrchestrationProgress.ANALYSIS_STARTED,
                text="analysis start", agent="codex",
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.ANALYSIS_COMPLETE,
                text="analysis done", full_text="analysis full", agent="codex",
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.IMPL_DELTA,
                text="impl round 1", agent="claude", round_num=1,
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.IMPL_COMPLETE,
                text="impl done r1", full_text="impl done r1", agent="claude", round_num=1,
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.VERIFY_STARTED,
                text="verify start r1", agent="codex", round_num=1,
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.VERIFY_COMPLETE,
                text="decision: retry\nrequired_fix: fix the bug", full_text="decision: retry\nrequired_fix: fix the bug",
                agent="codex", round_num=1,
            )
            # Round 2
            yield OrchestrationProgress(
                phase=OrchestrationProgress.IMPL_DELTA,
                text="impl round 2", agent="claude", round_num=2,
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.IMPL_COMPLETE,
                text="impl done r2", full_text="impl done r2", agent="claude", round_num=2,
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.VERIFY_STARTED,
                text="verify start r2", agent="codex", round_num=2,
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.VERIFY_COMPLETE,
                text="decision: pass\nsummary: ok", full_text="decision: pass\nsummary: ok",
                agent="codex", round_num=2,
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.COMPLETE,
                text="验收通过", full_text="decision: pass\nsummary: ok",
                agent="codex", result_status="passed", round_num=2,
            )

    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="VR", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "vr test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "vr test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: RetryThenPassOrchestrator(),
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="vr test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-cid-003",
    )
    await background_task

    events = store.list_by_correlation("test-cid-003")
    event_types = [e.event_type for e in events]

    # Must contain retry requested
    assert EventType.VERIFICATION_RETRY_REQUESTED in event_types, (
        "verification.retry.requested must be emitted when decision is retry"
    )

    # The retry must appear BEFORE the first verification.completed
    retry_idx = event_types.index(EventType.VERIFICATION_RETRY_REQUESTED)
    completion_indices = [
        i for i, t in enumerate(event_types)
        if t == EventType.VERIFICATION_COMPLETED
    ]
    if completion_indices:
        assert retry_idx < completion_indices[0], (
            "verification.retry.requested must come before verification.completed"
        )

    # run.completed must exist (final pass)
    assert EventType.RUN_COMPLETED in event_types

    # Orchestration run should be marked as passed
    assert ledger.get_orchestration_run(orch_run.id).status == OrchestrationStatus.PASSED


@pytest.mark.asyncio
async def test_runner_blocks_run_completed_after_recorded_retry_decision(
    tmp_path: Path,
) -> None:
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="Mismatch", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "mismatch test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "mismatch test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=(
            lambda _codex, _claude: MismatchedVerificationCompleteOrchestrator()
        ),
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="mismatch test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-cid-mismatch",
    )
    await background_task

    events = store.list_by_correlation("test-cid-mismatch")
    event_types = [e.event_type for e in events]

    assert EventType.VERIFICATION_DECISION_RECORDED in event_types
    assert EventType.VERIFICATION_RETRY_REQUESTED in event_types
    assert EventType.RUN_COMPLETED not in event_types
    assert EventType.RUN_FAILED in event_types
    assert ledger.get_task(task.id).status == TaskStatus.FAILED
    assert ledger.get_orchestration_run(orch_run.id).status == OrchestrationStatus.FAILED


@pytest.mark.asyncio
async def test_correlation_id_links_all_events_in_user_request(
    tmp_path: Path,
) -> None:
    """Every event in a chief-engineer run shares the same correlation_id."""
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="CID", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "cid test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "cid test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: FakeStreamingOrchestrator(),
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="cid test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-cid-004",
    )
    await background_task

    events = store.list_by_correlation("test-cid-004")
    assert len(events) >= 5  # meaningful run has at least a few events

    for e in events:
        assert e.correlation_id == "test-cid-004"

    # Verify events are queryable by agent_run_id
    analysis_events = store.list_by_agent_run(codex_run.id)
    assert any(
        e.event_type == EventType.AGENT_RUN_STARTED for e in analysis_events
    )
    assert any(
        e.event_type == EventType.AGENT_RUN_COMPLETED for e in analysis_events
    )


@pytest.mark.asyncio
async def test_telegram_deterministic_progress_no_raw_model_text(
    tmp_path: Path,
) -> None:
    """Telegram progress must use deterministic Chinese templates, never raw model text."""
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="DP", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "dp test", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "dp test")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=lambda _codex, _claude: RawVerboseStreamingOrchestrator(),
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="dp test",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-cid-005",
    )
    await background_task

    # Telegram visible text must NOT contain raw model output
    visible_text = "\n".join(
        event.text for event in renderer.events if event.event_type == "text_delta"
    )
    assert "RAW_" not in visible_text
    assert "交给 Claude" in visible_text or "Claude" in visible_text

    # Runtime events store the full raw text (as payload/record)
    events = store.list_by_correlation("test-cid-005")
    run_completed_events = [
        e for e in events if e.event_type == "run.completed"
    ]
    assert len(run_completed_events) >= 1

    # Final event is run_completed
    terminal = renderer.events[-1]
    assert terminal.event_type == "run_completed"


@pytest.mark.asyncio
async def test_runner_wires_claude_runtime_source_into_live_backend(
    tmp_path: Path,
) -> None:
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="Claude runtime", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "runtime source", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "runtime source")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")
    claude = RuntimeAwareClaude()

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=claude,
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=RuntimeSourceAwareOrchestrator,
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="runtime source",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-claude-runtime",
    )
    await background_task

    events = store.list_by_correlation("test-claude-runtime")
    event_types = [e.event_type for e in events]
    assert EventType.TOOL_CALL_STARTED in event_types
    assert EventType.MODEL_USAGE_UPDATED in event_types
    usage = [e for e in events if e.event_type == EventType.MODEL_USAGE_UPDATED][0]
    assert usage.actor == "claude"
    assert usage.agent_run_id is not None
    assert usage.payload["input_tokens"] == 12


@pytest.mark.asyncio
async def test_runner_wires_codex_runtime_source_for_analysis_turn(
    tmp_path: Path,
) -> None:
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, _backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    backend = RuntimeAwareCodexBackend()
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="Codex runtime", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "codex runtime", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "codex runtime")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=RuntimeSourceAwareCodexOrchestrator,
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="codex runtime",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-codex-runtime",
    )
    await background_task

    events = store.list_by_correlation("test-codex-runtime")
    usage_events = [e for e in events if e.event_type == EventType.MODEL_USAGE_UPDATED]
    assert usage_events
    assert usage_events[0].actor == "codex"
    assert usage_events[0].agent_run_id == codex_run.id
    heartbeats = [
        event for event in renderer.events
        if event.event_type == "runtime_heartbeat"
    ]
    assert heartbeats
    state = heartbeats[0].metadata["runtime_state"]
    assert state.phase == "running_analysis"
    assert state.active_agent == "codex"
    assert usage_events[0].payload["input_tokens"] == 21


@pytest.mark.asyncio
async def test_runner_heartbeat_tracks_codex_command_status(
    tmp_path: Path,
) -> None:
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger, service, _backend, renderer, workspace = _build_runtime(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    backend = RuntimeCommandCodexBackend()
    conversation = ledger.create_conversation(
        chat_id=100, user_id=200, title="Codex command runtime", mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    task = service.reserve_task("wlcodex", "codex command runtime", telegram_chat_id=100)
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "codex command runtime")
    codex_run = ledger.create_agent_run(conversation.id, "codex", "analysis")
    ledger.update_agent_run_status(codex_run.id, "running")

    runner = OrchestrationRunner(
        task_service=service,
        codex_backend=backend,
        claude_backend=EnabledClaude(),
        ledger=ledger,
        interaction_renderer=renderer,
        orchestrator_factory=RuntimeSourceAwareCodexOrchestrator,
        runtime_event_store=store,
    )

    background_task = runner.start_chief_engineer(
        prompt="codex command runtime",
        conversation=conversation,
        task_id=task.id,
        orchestration_run_id=orch_run.id,
        codex_analysis_run_id=codex_run.id,
        chat_id=100,
        workspace_path=str(workspace),
        correlation_id="test-codex-command-runtime",
    )
    await background_task

    heartbeats = [
        event for event in renderer.events
        if event.event_type == "runtime_heartbeat"
    ]
    command_states = [
        event.metadata["runtime_state"] for event in heartbeats
        if getattr(event.metadata["runtime_state"], "current_command", "")
    ]
    assert command_states
    assert command_states[0].current_command == "pytest tests/ -q"
    assert command_states[0].elapsed_seconds is not None
    assert command_states[0].estimated_remaining


# ============================================================================
# Fix 2: pending-context retry must force Claude implementation in next round
# ============================================================================


def test_build_retry_analysis_includes_all_required_blocks():
    """_build_retry_analysis must produce a prompt with structured retry blocks
    containing required_fix, pending context, original goal, and explicit
    instruction to prioritize pending context."""
    from wlcodex.orchestrator import _build_retry_analysis

    result = _build_retry_analysis(
        required_fix="文件末尾加 pending-context-ok\n同时更新测试文件",
        original_analysis="analysis: create file",
        original_goal="create file",
        round_num=2,
        pending_user_context="补充：文件末尾加 pending-context-ok",
    )

    assert "[RETRY_REQUIRED_FIX]" in result
    assert "文件末尾加 pending-context-ok" in result
    assert "同时更新测试文件" in result
    assert "[LATEST_PENDING_CONTEXT]" in result
    assert "补充：文件末尾加 pending-context-ok" in result
    assert "[ORIGINAL_GOAL]" in result
    assert "create file" in result
    assert "[ORIGINAL_ANALYSIS]" in result
    assert "必须优先完成 pending context" in result


def test_build_retry_analysis_handoff_packet_roundtrip():
    """Claude handoff packet built from retry analysis must preserve all
    retry context (required_fix, pending context, original goal)."""
    from wlcodex.orchestrator import _build_retry_analysis
    from wlcodex.context_packets import build_claude_handoff_packet

    retry_analysis = _build_retry_analysis(
        required_fix="文件末尾加 pending-context-ok",
        original_analysis="analysis: create file",
        original_goal="在 docs/smoke/ 下创建 test.md 写入 product-terminal-smoke-ok",
        round_num=2,
        pending_user_context="补充：文件末尾加 pending-context-ok",
    )

    packet = build_claude_handoff_packet(
        user_goal="在 docs/smoke/ 下创建 test.md 写入 product-terminal-smoke-ok",
        codex_analysis=retry_analysis,
    )
    rendered = packet.render()

    assert "pending-context-ok" in rendered
    assert "[RETRY_REQUIRED_FIX]" in rendered
    assert "[LATEST_PENDING_CONTEXT]" in rendered
    assert "[ORIGINAL_GOAL]" in rendered
    assert "必须优先完成 pending context" in rendered
    assert "product-terminal-smoke-ok" in rendered


# ============================================================================
# Fix 2b: VerificationDecision.parse must not truncate multi-line required_fix
# ============================================================================


def test_required_fix_multiline_not_truncated():
    """required_fix with multiple lines must be fully preserved by parse()."""
    from wlcodex.orchestrator import VerificationDecision

    text = (
        "decision: retry\n"
        "required_fix: 第一行修复要求\n"
        "第二行修复要求：需要同时修改测试文件\n"
        "第三行：验证 diff --check 通过\n"
        "confidence: high\n"
        "summary: 需要补充 pending context 的修改"
    )
    vd = VerificationDecision.parse(text)
    assert vd.decision == "retry"
    assert "第一行修复要求" in vd.required_fix
    assert "第二行修复要求" in vd.required_fix, (
        f"required_fix truncated, got: {vd.required_fix!r}"
    )
    assert "第三行" in vd.required_fix, (
        f"required_fix truncated, got: {vd.required_fix!r}"
    )


def test_required_fix_empty_when_not_present_in_pass():
    """When decision is pass, required_fix should be empty."""
    from wlcodex.orchestrator import VerificationDecision

    vd = VerificationDecision.parse("decision: pass\nsummary: all good")
    assert vd.decision == "pass"
    assert vd.required_fix == ""


# ============================================================================
# Fix 3: build_claude_handoff_packet must include mandatory Chinese constraints
# ============================================================================


MANDATORY_HANDOFF_CONSTRAINTS = [
    "完整闭环",
    "没有漂移",
    "编码前思考",
    "简洁优先",
    "精准修改",
    "目标驱动执行",
]


def test_claude_handoff_packet_includes_mandatory_constraints():
    """Every Claude handoff packet rendered text MUST include the six
    mandatory programming constraints verbatim."""
    from wlcodex.context_packets import build_claude_handoff_packet

    packet = build_claude_handoff_packet(
        user_goal="test goal",
        codex_analysis="test analysis",
    )
    rendered = packet.render()
    for constraint in MANDATORY_HANDOFF_CONSTRAINTS:
        assert constraint in rendered, (
            f"Mandatory constraint '{constraint}' missing from handoff packet:\n{rendered[:1000]}"
        )


def test_claude_handoff_packet_constraints_in_constraints_field():
    """The constraints must appear in the recent_user_constraints or
    handoff_from_codex.constraints section, not just as a fluke in another field."""
    from wlcodex.context_packets import build_claude_handoff_packet

    packet = build_claude_handoff_packet(
        user_goal="test goal",
        codex_analysis="test analysis",
    )
    all_constraints = "; ".join(packet.recent_user_constraints)
    for constraint in MANDATORY_HANDOFF_CONSTRAINTS:
        assert constraint in all_constraints, (
            f"Mandatory constraint '{constraint}' not in constraints list"
        )
