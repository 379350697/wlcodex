import asyncio
from pathlib import Path
from typing import Any

import pytest

from wlcodex.agent_backend import AgentStreamEvent
from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.interaction.events import InteractionEvent
from wlcodex.models import AgentRunStatus, OrchestrationStatus, TaskStatus
from wlcodex.orchestrator import OrchestrationProgress
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
async def test_controller_starts_background_orchestration_and_returns(
    tmp_path: Path,
) -> None:
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
        controller.handle_conversation_text(
            "实现一个需要多轮验收的小功能",
            {"chat_id": 100, "user_id": 200},
        ),
        timeout=0.05,
    )

    assert response.already_rendered is True
    assert response.text == ""
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["prompt"] == "实现一个需要多轮验收的小功能"
    assert call["chat_id"] == 100
    assert call["conversation"].id == 1
    assert ledger.get_orchestration_run(call["orchestration_run_id"]).status == (
        OrchestrationStatus.RUNNING
    )
    assert ledger.get_agent_run(call["codex_analysis_run_id"]).status == (
        AgentRunStatus.RUNNING
    )
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
