"""Controller flow tests with fake backend."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.integration

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
from wlcodex.team_memory import InstinctMemory
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


def _button_actions(buttons: object) -> set[str]:
    actions: set[str] = set()
    for row in buttons or []:
        for button in row or []:
            callback_data = button.get("callback_data", "")
            if isinstance(callback_data, str) and ":" in callback_data:
                actions.add(callback_data.rsplit(":", 1)[-1])
    return actions


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


def _callback_actions(buttons: list[list[dict[str, str]]]) -> set[str]:
    actions: set[str] = set()
    for row in buttons:
        for button in row:
            callback_data = button.get("callback_data", "")
            parts = callback_data.split(":")
            if len(parts) >= 3:
                actions.add(parts[2])
    return actions


def _record_valid_architecture_plan(
    ledger: Ledger,
    *,
    team_run_id: int,
    agent_job_id: int | None = None,
) -> None:
    ledger.record_team_artifact(
        team_run_id=team_run_id,
        agent_job_id=agent_job_id,
        artifact_type="architecture_plan",
        summary="Plan ready with explicit scope and acceptance criteria.",
        payload={
            "summary": "Plan ready with explicit scope and acceptance criteria.",
            "files_or_modules_in_scope": ["tracked.txt"],
            "files_or_modules_out_of_scope": ["unrelated.txt"],
            "impact_notes": "Low local impact.",
            "risk_level": "medium",
            "implementation_steps": ["Update tracked.txt"],
            "acceptance_criteria": ["Focused verification passes"],
            "parallelization_policy": "single writer",
        },
    )


def _record_valid_implementation_report(
    ledger: Ledger,
    *,
    team_run_id: int,
    agent_job_id: int | None = None,
) -> None:
    ledger.record_team_artifact(
        team_run_id=team_run_id,
        agent_job_id=agent_job_id,
        artifact_type="implementation_report",
        summary="Implementation complete with evidence.",
        payload={
            "summary": "Implementation complete with evidence.",
            "changed_files": ["tracked.txt"],
            "diff_summary": "tracked.txt changed.",
            "commands_run": [
                {
                    "command": "pytest tests/test_controller_flow.py -q",
                    "exit_status": 0,
                    "summary": "controller flow tests passed",
                }
            ],
            "tests_attempted": [
                {
                    "command": "pytest tests/test_controller_flow.py -q",
                    "exit_status": 0,
                    "summary": "controller flow tests passed",
                }
            ],
            "known_limitations": ["None known"],
        },
    )


def _record_valid_test_report(
    ledger: Ledger,
    *,
    team_run_id: int,
    agent_job_id: int | None = None,
) -> None:
    ledger.record_team_artifact(
        team_run_id=team_run_id,
        agent_job_id=agent_job_id,
        artifact_type="test_report",
        summary="Focused verification evidence is ready.",
        payload={
            "summary": "Focused verification evidence is ready.",
            "commands_run": [
                {
                    "command": "pytest tests/test_controller_flow.py -q",
                    "exit_status": 0,
                    "summary": "controller flow tests passed",
                }
            ],
            "passed": ["Focused verification"],
            "failed": ["None"],
            "coverage_of_acceptance_criteria": [
                {
                    "criterion": "Focused verification passes",
                    "status": "covered",
                    "evidence": "pytest tests/test_controller_flow.py -q",
                }
            ],
            "failure_evidence": ["None"],
        },
    )


def _auto_event_bridge(
    *,
    service: TaskService,
    ledger: Ledger,
    codex_implementer_enabled: bool = False,
) -> object:
    from wlcodex.event_bridge import EventBridge

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        return 1

    async def edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
        return None

    class ApprovalService:
        async def expire_stale_approvals(self, ledger: Ledger, backend: object) -> None:
            return None

    return EventBridge(
        service,
        FakeCodexBackend(),
        ledger,
        send_telegram,
        edit_telegram,
        ApprovalService(),
        codex_implementer_enabled=codex_implementer_enabled,
    )


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


def test_build_team_context_packet_for_job_records_activation_inputs(
    ctrl: CommandController,
) -> None:
    ledger = ctrl._ledger
    assert ledger is not None
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Team Packet",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orchestration = ledger.create_orchestration_run(
        conversation_id=conversation.id,
        goal="audit implementation evidence",
    )
    team = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orchestration.id,
        goal="audit implementation evidence",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )
    artifact = ledger.record_team_artifact(
        team_run_id=team.id,
        agent_job_id=job.id,
        artifact_type="implementation_report",
        summary="changed auth.py",
        payload={},
    )
    instinct = ledger.upsert_team_instinct(
        InstinctMemory(
            instinct_id="audit-memory-1",
            scope="project",
            workspace_alias="wlcodex",
            role="auditor",
            domain="audit",
            trigger="implementation evidence",
            action="Require current diff and test evidence before approving.",
            confidence=0.8,
            evidence_refs=(f"team_artifact={artifact.id}",),
            status="active",
            created_at=datetime(2026, 5, 24, tzinfo=UTC),
            last_validated_at=datetime(2026, 5, 24, tzinfo=UTC),
        )
    )

    packet = ctrl._build_team_context_packet_for_job(
        team_run=team,
        agent_job=job,
        role="auditor",
        model_profile="codex_gpt",
        resume_state="implementation completed; audit is next",
        output_schema="audit_report",
    )

    canonical = packet.as_json()
    assert canonical["capability_budget"]["max_memory_snippets"] == 2
    assert canonical["skill_activations"] == [
        "code-review",
        "security-review",
        "read",
        "git_diff",
        "shell_readonly",
    ]
    assert (
        canonical["relevant_instincts"][0]["precedence"]
        == "historical_advice_current_evidence_wins"
    )
    assert canonical["source_refs"] == [
        f"team_artifact={artifact.id}",
        f"team_instinct={instinct.id}",
    ]

    activations = ledger.list_team_skill_activations(job.id)
    activation_types = {activation.activation_type for activation in activations}
    assert {"skill", "tool", "memory"} <= activation_types
    assert any(
        activation.activation_type == "memory"
        and activation.activation_id == instinct.instinct_id
        and activation.source == "instinct_memory"
        for activation in activations
    )


def test_build_team_context_packet_for_job_emits_skill_and_budget_events(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", tmp_path, True),
    ))
    ctrl = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        runtime_event_store=store,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Team Packet",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orchestration = ledger.create_orchestration_run(
        conversation_id=conversation.id,
        goal="audit implementation evidence",
    )
    team = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orchestration.id,
        goal="audit implementation evidence",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )

    ctrl._build_team_context_packet_for_job(
        team_run=team,
        agent_job=job,
        role="auditor",
        model_profile="codex_gpt",
        resume_state="implementation completed; audit is next",
        output_schema="audit_report",
    )

    events = store.list_by_conversation(conversation.id, limit=100)
    skill_events = [
        event for event in events
        if event.event_type == EventType.TEAM_SKILL_ACTIVATED
    ]
    budget_events = [
        event for event in events
        if event.event_type == EventType.TEAM_CAPABILITY_BUDGET_APPLIED
    ]

    assert skill_events
    assert budget_events
    assert {
        "team_run_id": team.id,
        "agent_job_id": job.id,
        "activation_type": "skill",
        "activation_id": "code-review",
        "source": "role_default",
    }.items() <= skill_events[0].payload.items()
    assert budget_events[0].payload["team_run_id"] == team.id
    assert budget_events[0].payload["agent_job_id"] == job.id
    assert budget_events[0].payload["budget"]["max_tools"] == 4
    assert "selected_tools" in budget_events[0].payload


def test_build_team_context_packet_for_job_records_activations_idempotently(
    ctrl: CommandController,
) -> None:
    ledger = ctrl._ledger
    assert ledger is not None
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Team Packet",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orchestration = ledger.create_orchestration_run(
        conversation_id=conversation.id,
        goal="audit implementation evidence",
    )
    team = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orchestration.id,
        goal="audit implementation evidence",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )
    ledger.upsert_team_instinct(
        InstinctMemory(
            instinct_id="audit-memory-1",
            scope="project",
            workspace_alias="wlcodex",
            role="auditor",
            domain="audit",
            trigger="implementation evidence",
            action="Require current diff and test evidence before approving.",
            confidence=0.8,
            evidence_refs=("team_artifact=1",),
            status="active",
            created_at=datetime(2026, 5, 24, tzinfo=UTC),
            last_validated_at=datetime(2026, 5, 24, tzinfo=UTC),
        )
    )

    ctrl._build_team_context_packet_for_job(
        team_run=team,
        agent_job=job,
        role="auditor",
        model_profile="codex_gpt",
        resume_state="implementation completed; audit is next",
        output_schema="audit_report",
    )
    first_count = len(ledger.list_team_skill_activations(job.id))

    ctrl._build_team_context_packet_for_job(
        team_run=team,
        agent_job=job,
        role="auditor",
        model_profile="codex_gpt",
        resume_state="implementation completed; audit is next",
        output_schema="audit_report",
    )

    assert len(ledger.list_team_skill_activations(job.id)) == first_count


def test_build_team_context_packet_for_job_uses_configured_role_skills_and_tools(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    ctrl = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        adaptive_team_role_skills={
            "auditor": ("custom-audit-skill",),
        },
        adaptive_team_role_capabilities={
            "auditor": ("custom_read", "custom_diff"),
        },
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Team Packet",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orchestration = ledger.create_orchestration_run(
        conversation_id=conversation.id,
        goal="audit implementation evidence",
    )
    team = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orchestration.id,
        goal="audit implementation evidence",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )

    packet = ctrl._build_team_context_packet_for_job(
        team_run=team,
        agent_job=job,
        role="auditor",
        model_profile="codex_gpt",
        resume_state="implementation completed; audit is next",
        output_schema="audit_report",
    )

    canonical = packet.as_json()
    assert canonical["skills"] == ["custom-audit-skill"]
    assert canonical["allowed_capabilities"] == ["custom_read", "custom_diff"]
    assert "code-review" not in canonical["skills"]
    assert "git_diff" not in canonical["allowed_capabilities"]
    activations = ledger.list_team_skill_activations(job.id)
    assert {
        (activation.activation_type, activation.activation_id)
        for activation in activations
    } >= {
        ("skill", "custom-audit-skill"),
        ("tool", "custom_read"),
        ("tool", "custom_diff"),
    }


def test_build_team_context_packet_for_job_rejects_forbidden_role_capabilities(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    with pytest.raises(
        ValueError,
        match="auditor has forbidden capability shell",
    ):
        CommandController(
            service,
            FakeCodexBackend(),
            TaskInspector(ledger, tmp_path / "logs"),
            ledger=ledger,
            adaptive_team_role_capabilities={
                "auditor": (
                    "read",
                    "shell",
                    "git_diff",
                ),
            },
        )


def test_build_team_context_packet_for_job_rejects_role_mismatch_and_unknown_role(
    ctrl: CommandController,
) -> None:
    ledger = ctrl._ledger
    assert ledger is not None
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Team Packet",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orchestration = ledger.create_orchestration_run(
        conversation_id=conversation.id,
        goal="audit implementation evidence",
    )
    team = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orchestration.id,
        goal="audit implementation evidence",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )

    with pytest.raises(
        ValueError,
        match="agent job role 'auditor'.*requested role 'implementer'",
    ):
        ctrl._build_team_context_packet_for_job(
            team_run=team,
            agent_job=job,
            role="implementer",
            model_profile="codex_gpt",
            resume_state="implementation completed; audit is next",
            output_schema="audit_report",
        )
    assert ledger.list_team_skill_activations(job.id) == []

    with pytest.raises(ValueError, match="unknown team role 'unknown-role'"):
        ctrl._build_team_context_packet_for_job(
            team_run=team,
            agent_job=job,
            role="unknown-role",
            model_profile="codex_gpt",
            resume_state="implementation completed; audit is next",
            output_schema="audit_report",
        )
    assert ledger.list_team_skill_activations(job.id) == []


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
    assert "发给当前 Codex" in labels
    assert "发给当前 Claude" not in labels
    assert "打断并执行这句" in labels
    assert "排队稍后" in labels
    assert "新开隔离现场" in labels


@pytest.mark.asyncio
async def test_auto_command_busy_labels_actual_current_agent(
    ctrl_with_claude: CommandController,
) -> None:
    ledger = ctrl_with_claude._ledger
    assert ledger is not None
    active = ledger.create_conversation(
        chat_id=102,
        user_id=202,
        title="正在执行",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    blocker = ctrl_with_claude._service.reserve_task(
        "wlcodex", "旧任务", telegram_chat_id=102,
    )
    ledger.set_conversation_active_task(active.id, blocker.id)
    ledger.set_task_status(blocker.id, TaskStatus.RUNNING)

    response = await ctrl_with_claude.handle(
        "/auto 是否有死代码",
        {"chat_id": 102, "user_id": 202},
    )

    assert "发给当前 Codex" in response.text
    assert "发给当前 Auto" not in response.text
    flat_buttons = [button for row in response.buttons for button in row]
    labels = {button["text"] for button in flat_buttons}
    assert "发给当前 Codex" in labels
    assert "发给当前 Auto" not in labels


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
    assert "普通消息：Codex 分析/核验" in response.text
    assert "Codex 主导闭环" in response.text or "/auto" in response.text
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
async def test_status_appends_team_summary_for_staged_auto_run(
    ctrl: CommandController,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY

    ledger = ctrl._ledger
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="Auto team",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "修复登录偶发失败",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修复登录偶发失败",
        route="staged_auto",
        risk_level="medium",
    )
    architect_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="architect",
        model_profile="codex_gpt",
        status="done",
        agent_run_id=None,
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="queued",
        agent_run_id=None,
    )
    ledger.record_team_artifact(
        team_run_id=team_run.id,
        agent_job_id=architect_job.id,
        artifact_type="architecture_plan",
        summary="Plan ready",
        payload={},
    )

    response = await ctrl.handle("/status", {"chat_id": 100, "user_id": 200})

    assert "当前对话：Auto team" in response.text
    assert "团队状态：" in response.text
    assert "架构工程师：codex_gpt / 已完成" in response.text
    assert "开发工程师：claude_deepseek / 排队中" in response.text
    assert "architecture_plan: Plan ready" in response.text


@pytest.mark.asyncio
async def test_status_team_summary_uses_latest_job_per_role(
    ctrl: CommandController,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_RUNNING

    ledger = ctrl._ledger
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="Auto team retry",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "修复 flaky 测试",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修复 flaky 测试",
        route="staged_auto",
        risk_level="medium",
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="failed",
        agent_run_id=None,
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=None,
    )

    response = await ctrl.handle("/status", {"chat_id": 100, "user_id": 200})

    assert "开发工程师：codex_gpt / 运行中" in response.text
    assert "开发工程师：claude_deepseek / 已失败" not in response.text
    assert response.text.count("开发工程师：") == 1


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


class StreamingClaudeWithStructuredEvidence:
    enabled = True

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def send_streaming(self, request):
        self.prompts.append(request.prompt)
        yield AgentStreamEvent(delta="Implementation complete.\n", event_type="text")
        yield AgentStreamEvent(
            delta=(
                "```json\n"
                "{"
                "\"implementation_evidence\":{"
                "\"changed_files\":[\"tracked.txt\"],"
                "\"diff_summary\":\"tracked.txt updated\","
                "\"commands_run\":[{"
                "\"command\":\"pytest tests/test_streaming.py -q\","
                "\"exit_status\":0,"
                "\"summary\":\"streaming tests passed\""
                "}],"
                "\"tests_attempted\":[{"
                "\"command\":\"pytest tests/test_streaming.py -q\","
                "\"exit_status\":0,"
                "\"summary\":\"streaming tests passed\""
                "}]"
                "}"
                "}\n"
                "```"
            ),
            event_type="text",
        )

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
async def test_staged_auto_does_not_call_eager_runner(
    tmp_path: Path,
) -> None:
    """Staged /auto starts Codex analysis directly, NOT via orchestration runner."""

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

    # Must NOT call start_chief_engineer — staged auto uses direct Codex analysis
    assert len(runner.calls) == 0, (
        f"staged /auto should not call start_chief_engineer, got {len(runner.calls)} calls"
    )
    # Claude must not be started
    assert len(claude.calls) == 0
    # Orchestration run must be in collecting_context
    active = ledger.get_active_conversation(100)
    runs = ledger.list_orchestration_runs(active.id, limit=1)
    assert runs[0].current_step == "collecting_context"
    assert response.already_rendered is False
    assert ("分析" in response.text or "最终方案" in response.text or "Codex" in response.text)


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
    assert runner.calls == []
    runs = ledger.list_agent_runs(conversation.id, limit=10)
    assert [(run.agent, run.role) for run in runs] == [("codex", "analysis")]

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
async def test_staged_auto_does_not_start_claude_or_eager_pipeline(
    tmp_path: Path,
) -> None:
    """Staged /auto creates a collecting_context run but never starts Claude
    or the eager orchestration pipeline on its own."""

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    backend = FakeCodexBackend()
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

    response = await controller.handle(
        "/auto 查一下为什么偶发失败",
        {"chat_id": 100, "user_id": 200},
    )

    # Must start collecting_context, not Claude
    assert len(claude.calls) == 0
    active = ledger.get_active_conversation(100)
    assert active is not None
    runs = ledger.list_orchestration_runs(active.id, limit=1)
    assert len(runs) == 1
    assert runs[0].current_step == "collecting_context"
    assert runs[0].status == "running"
    # Agent run must be codex auto_analysis
    agent_runs = ledger.list_agent_runs(active.id, limit=5)
    assert any(run.role == "auto_analysis" for run in agent_runs)


@pytest.mark.asyncio
async def test_conversation_text_event_keeps_only_safe_preview(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", tmp_path, True),
    ))
    controller = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        runtime_event_store=store,
    )
    text = "please implement this password=abc123 token=secret123"

    await controller.handle_conversation_text(
        text,
        {"chat_id": 100, "user_id": 200},
    )

    active = ledger.get_active_conversation(100)
    assert active is not None
    events = [
        e for e in store.list_by_conversation(active.id)
        if e.event_type == EventType.USER_MESSAGE_RECEIVED
    ]
    assert len(events) == 1
    payload = events[0].payload
    assert "text" not in payload
    assert "original_text" not in payload
    assert payload["text_length"] == len(text)
    assert "abc123" not in payload["text_preview"]
    assert "secret123" not in payload["text_preview"]


@pytest.mark.asyncio
async def test_staged_auto_starts_without_orchestration_runner(
    tmp_path: Path,
) -> None:
    """Staged /auto starts Codex context collection even without an orchestration
    runner. It no longer requires OrchestrationRunner because it uses direct
    Codex analysis, not the eager pipeline."""
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

    # Staged /auto works without orchestration runner — starts collecting_context
    assert "分析" in response.text or "最终方案" in response.text or "Codex" in response.text
    assert len(claude.calls) == 0

    # Orchestration run must be in collecting_context
    active = ledger.get_active_conversation(100)
    runs = ledger.list_orchestration_runs(active.id, limit=1)
    assert len(runs) == 1
    assert runs[0].current_step == "collecting_context"


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
async def test_auto_mode_starts_staged_context_collection(ctrl_with_claude: CommandController) -> None:
    """Handle /auto must start collecting_context, not an eager pipeline."""
    response = await ctrl_with_claude.handle(
        "/auto 修复登录 bug",
        {"chat_id": 100, "user_id": 200},
    )
    # Staged /auto starts Codex analysis, not an eager pipeline
    assert "最终方案" in response.text or "分析" in response.text or "Codex" in response.text
    # Claude must not be started
    assert len(ctrl_with_claude._claude.calls) == 0
    active = ctrl_with_claude._ledger.get_active_conversation(100)
    assert active is not None
    orch_runs = ctrl_with_claude._ledger.list_orchestration_runs(active.id, limit=1)
    assert orch_runs[0].current_step == "collecting_context"


@pytest.mark.asyncio
async def test_auto_mode_staged_hides_english_model_snippets(
    ctrl_with_claude: CommandController,
) -> None:
    """Staged /auto response mentions context collection, not English model output."""
    response = await ctrl_with_claude.handle(
        "/auto 修复登录 bug",
        {"chat_id": 100, "user_id": 200},
    )

    # Staged /auto starts collecting context, not the eager pipeline
    assert "分析" in response.text or "最终方案" in response.text or "Codex" in response.text
    assert "Analysis complete" not in response.text
    assert "Fake Claude implementation result" not in response.text


@pytest.mark.asyncio
async def test_staged_auto_records_ledger_on_context_collection(tmp_path: Path) -> None:
    """Staged /auto starts collecting_context with proper ledger audit trail."""
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    backend._codex_responses = [
        "Root cause: tracked.txt needs a change. Implementation needed.",
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
    assert "Auto" in active.conversation_summary or "修改" in active.conversation_summary

    # Staged /auto does NOT start Claude automatically
    assert active.active_claude_run_id is None

    # Should have one codex analysis agent run for collecting_context
    agent_runs = ledger.list_agent_runs(active.id)
    assert len(agent_runs) == 1
    assert agent_runs[0].agent == "codex"
    assert agent_runs[0].role == "auto_analysis"

    # Orchestration run should be in collecting_context
    orch_runs = ledger.list_orchestration_runs(active.id)
    assert len(orch_runs) == 1
    assert orch_runs[0].current_step == "collecting_context"
    assert orch_runs[0].status == "running"

    # A run_started event should have been rendered
    started_events = [
        event for event in renderer.events
        if getattr(event, "event_type", "") == "run_started"
    ]
    assert started_events


@pytest.mark.asyncio
async def test_staged_auto_start_does_not_offer_final_plan_while_analysis_runs(tmp_path: Path) -> None:
    """The final-plan gate must not be clickable until the initial Codex
    context-collection task has actually finished."""
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    renderer = RecordingInteractionRenderer()
    store = RuntimeEventStore(ledger._conn)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=StreamingClaudeWritesTrackedFile(workspace),
        interaction_renderer=renderer,
        runtime_event_store=store,
    )

    response = await ctrl.handle(
        "/auto 修改 tracked.txt",
        {"chat_id": 100, "user_id": 200},
    )

    assert response.already_rendered
    started_events = [
        event for event in renderer.events
        if getattr(event, "event_type", "") == "run_started"
    ]
    labels = [
        button["text"]
        for row in (getattr(started_events[0], "buttons", None) or [])
        for button in row
    ]
    assert "生成最终方案" not in labels
    assert "查看状态" in labels


@pytest.mark.asyncio
async def test_auto_final_plan_callback_hides_final_plan_gate_while_generating(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_COLLECTING_CONTEXT, AUTO_FINAL_PLAN
    from wlcodex.conversation_callback import ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=StreamingClaudeWritesTrackedFile(workspace),
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_COLLECTING_CONTEXT,
        last_codex_analysis="已完成上下文收集。",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=conversation.id, action=AUTO_FINAL_PLAN)
    )

    labels = [
        button["text"]
        for row in (response.buttons or [])
        for button in row
    ]
    assert "生成最终方案" not in labels
    assert "查看当前草稿" not in labels
    assert "查看状态" in labels
    assert "取消" in labels
    assert ledger.get_orchestration_run(orch_run.id).status == "running"
    assert backend.prompt_turns == []
    assert backend.turns


@pytest.mark.asyncio
async def test_auto_final_plan_callback_rejects_duplicate_while_generating(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_COLLECTING_CONTEXT, AUTO_FINAL_PLAN
    from wlcodex.conversation_callback import ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=StreamingClaudeWritesTrackedFile(workspace),
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_COLLECTING_CONTEXT,
    )

    callback = ConversationCallback(
        conversation_id=conversation.id,
        action=AUTO_FINAL_PLAN,
    )
    await ctrl.handle_conversation_callback(callback)
    second = await ctrl.handle_conversation_callback(callback)

    labels = [
        button["text"]
        for row in (second.buttons or [])
        for button in row
    ]
    assert "正在生成最终方案" in second.text
    assert "生成最终方案" not in labels
    assert len([
        run for run in ledger.list_agent_runs(conversation.id)
        if run.role == "auto_final_plan"
    ]) == 1


@pytest.mark.asyncio
async def test_auto_final_plan_offers_claude_and_codex_implementers(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_DRAFT_READY,
        AUTO_SEND_TO_CLAUDE,
        AUTO_VIEW_STATUS,
    )
    from wlcodex.conversation_callback import AUTO_SEND_TO_CODEX, ConversationCallback
    from wlcodex.router import AutoModeCommand

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=StreamingClaudeWritesTrackedFile(workspace),
    )

    await ctrl.handle_auto_mode(
        AutoModeCommand("修复登录偶发失败"),
        {"chat_id": 123, "user_id": 456},
    )
    convo = ledger.get_active_conversation(123)
    assert convo is not None
    orch_run = ledger.list_orchestration_runs(convo.id, limit=1)[0]
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修复登录偶发失败，并运行相关登录测试。",
    )

    status_response = await ctrl.handle_conversation_callback(
        ConversationCallback(
            conversation_id=convo.id,
            action=AUTO_VIEW_STATUS,
        )
    )

    actions = _callback_actions(status_response.buttons)
    assert AUTO_SEND_TO_CLAUDE in actions
    assert AUTO_SEND_TO_CODEX in actions


@pytest.mark.asyncio
async def test_auto_mode_creates_architect_team_run_and_context_packet(
    tmp_path: Path,
) -> None:
    from wlcodex.router import AutoModeCommand

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
    )

    await ctrl.handle_auto_mode(
        AutoModeCommand("修复登录偶发失败"),
        {"chat_id": 123, "user_id": 456},
    )

    convo = ledger.get_active_conversation(123)
    assert convo is not None
    orch_run = ledger.list_orchestration_runs(convo.id, limit=1)[0]
    team_run = ledger.get_team_run_for_orchestration(orch_run.id)
    assert team_run is not None
    assert team_run.conversation_id == convo.id
    assert team_run.goal == "修复登录偶发失败"
    assert team_run.route == "staged_auto"
    assert team_run.risk_level == "medium"

    jobs = ledger.list_team_agent_jobs(team_run.id)
    assert len(jobs) == 1
    architect_job = jobs[0]
    assert architect_job.role == "architect"
    assert architect_job.model_profile == "codex_gpt"
    assert architect_job.status == "running"
    assert architect_job.agent_run_id is not None

    packet = ledger.get_team_context_packet_for_job(architect_job.id)
    assert packet is not None
    assert packet.packet["role"] == "architect"
    assert packet.packet["model_profile"] == "codex_gpt"
    assert packet.packet["required_output_schema"] == "implementation_plan"
    assert any(
        "Architect performs investigator duties in v1" in rule
        for rule in packet.packet["handoff_rules"]
    )
    assert packet.prompt_tokens > 0
    assert backend.turns
    _thread_id, prompt_sent = backend.turns[-1]
    assert prompt_sent == packet.prompt_text
    assert "role: architect" in prompt_sent
    assert "allowed_capabilities:" in prompt_sent

    assignment = ledger._conn.execute(
        """
        SELECT role, model_profile, selected_by
        FROM team_assignments
        WHERE team_run_id = ?
        """,
        (team_run.id,),
    ).fetchone()
    assert dict(assignment) == {
        "role": "architect",
        "model_profile": "codex_gpt",
        "selected_by": "policy",
    }


@pytest.mark.asyncio
async def test_auto_mode_marks_team_failed_when_codex_start_fails(
    tmp_path: Path,
) -> None:
    from wlcodex.router import AutoModeCommand

    class FailingStartController(CommandController):
        async def _start_codex_turn_for_conversation(self, **kwargs) -> str:
            raise RuntimeError("codex start boom")

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = FailingStartController(
        service,
        backend,
        inspector,
        ledger=ledger,
    )

    response = await ctrl.handle_auto_mode(
        AutoModeCommand("修复登录偶发失败"),
        {"chat_id": 123, "user_id": 456},
    )

    convo = ledger.get_active_conversation(123)
    assert convo is not None
    orch_run = ledger.list_orchestration_runs(convo.id, limit=1)[0]
    team_run = ledger.get_team_run_for_orchestration(orch_run.id)
    assert team_run is not None
    jobs = ledger.list_team_agent_jobs(team_run.id)
    assert len(jobs) == 1
    updated_orch = ledger.get_orchestration_run(orch_run.id)
    assert updated_orch.status == "failed"
    assert "codex start boom" in updated_orch.last_codex_analysis
    assert ledger.get_team_run(team_run.id).status == "failed"
    assert jobs[0].status == "failed"
    assert response.text


@pytest.mark.asyncio
async def test_auto_mode_marks_team_failed_when_context_packet_build_fails(
    tmp_path: Path,
) -> None:
    from wlcodex.router import AutoModeCommand

    class FailingPacketController(CommandController):
        def _build_team_context_packet_for_job(self, **kwargs) -> object:
            raise RuntimeError("packet boom")

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = FailingPacketController(
        service,
        backend,
        inspector,
        ledger=ledger,
    )

    response = await ctrl.handle_auto_mode(
        AutoModeCommand("修复登录偶发失败"),
        {"chat_id": 123, "user_id": 456},
    )

    convo = ledger.get_active_conversation(123)
    assert convo is not None
    orch_run = ledger.list_orchestration_runs(convo.id, limit=1)[0]
    team_run = ledger.get_team_run_for_orchestration(orch_run.id)
    assert team_run is not None
    jobs = ledger.list_team_agent_jobs(team_run.id)
    assert len(jobs) == 1
    updated_orch = ledger.get_orchestration_run(orch_run.id)
    assert updated_orch.status == "failed"
    assert "packet boom" in updated_orch.last_codex_analysis
    assert ledger.get_team_run(team_run.id).status == "failed"
    assert jobs[0].status == "failed"
    assert response.text
    assert backend.turns == []


@pytest.mark.asyncio
async def test_auto_mode_duplicate_returns_existing_status_without_new_runs(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, AUTO_SEND_TO_CLAUDE
    from wlcodex.conversation_callback import AUTO_SEND_TO_CODEX
    from wlcodex.router import AutoModeCommand

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=StreamingClaudeWritesTrackedFile(workspace),
    )

    await ctrl.handle_auto_mode(
        AutoModeCommand("修复登录偶发失败"),
        {"chat_id": 123, "user_id": 456},
    )
    convo = ledger.get_active_conversation(123)
    assert convo is not None
    orch_run = ledger.list_orchestration_runs(convo.id, limit=1)[0]
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修复登录偶发失败，并运行相关登录测试。",
    )
    team_count = ledger._conn.execute(
        "SELECT COUNT(*) AS n FROM team_runs WHERE conversation_id = ?",
        (convo.id,),
    ).fetchone()["n"]

    response = await ctrl.handle_auto_mode(
        AutoModeCommand("再次修复登录偶发失败"),
        {"chat_id": 123, "user_id": 456},
    )

    actions = _callback_actions(response.buttons)
    assert len(ledger.list_orchestration_runs(convo.id)) == 1
    assert ledger._conn.execute(
        "SELECT COUNT(*) AS n FROM team_runs WHERE conversation_id = ?",
        (convo.id,),
    ).fetchone()["n"] == team_count
    assert AUTO_SEND_TO_CLAUDE in actions
    assert AUTO_SEND_TO_CODEX in actions
    assert "已有 /auto 工作流" in response.text


@pytest.mark.asyncio
async def test_auto_codex_implementer_uses_model_profile_provider_mapping(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, AUTO_VIEW_STATUS
    from wlcodex.conversation_callback import AUTO_SEND_TO_CODEX, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    def make_controller(
        ledger: Ledger,
        *,
        implementer_profiles: tuple[str, ...],
        model_profiles: dict[str, str],
    ) -> CommandController:
        service = TaskService(ledger, (
            WorkspaceConfig("wlcodex", workspace, True),
        ))
        return CommandController(
            service,
            FakeCodexBackend(),
            TaskInspector(ledger, tmp_path / "logs"),
            ledger=ledger,
            implementer_model_profiles=implementer_profiles,
            adaptive_team_model_profiles=model_profiles,
        )

    codex_ledger = Ledger.open(tmp_path / "codex.sqlite3")
    codex_ledger.migrate()
    codex_ctrl = make_controller(
        codex_ledger,
        implementer_profiles=("strong_codex",),
        model_profiles={"strong_codex": "codex"},
    )
    codex_convo = codex_ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    codex_run = codex_ledger.create_orchestration_run(
        codex_convo.id,
        "修复登录偶发失败",
    )
    codex_ledger.update_orchestration_run(
        codex_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修复登录偶发失败。",
    )

    codex_response = await codex_ctrl.handle_conversation_callback(
        ConversationCallback(codex_convo.id, AUTO_VIEW_STATUS)
    )
    assert AUTO_SEND_TO_CODEX in _callback_actions(codex_response.buttons)

    claude_ledger = Ledger.open(tmp_path / "claude.sqlite3")
    claude_ledger.migrate()
    claude_ctrl = make_controller(
        claude_ledger,
        implementer_profiles=("codex_gpt",),
        model_profiles={"codex_gpt": "claude"},
    )
    claude_convo = claude_ledger.create_conversation(
        chat_id=124,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    claude_run = claude_ledger.create_orchestration_run(
        claude_convo.id,
        "修复登录偶发失败",
    )
    claude_ledger.update_orchestration_run(
        claude_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修复登录偶发失败。",
    )

    claude_response = await claude_ctrl.handle_conversation_callback(
        ConversationCallback(claude_convo.id, AUTO_VIEW_STATUS)
    )
    assert AUTO_SEND_TO_CODEX not in _callback_actions(claude_response.buttons)


@pytest.mark.asyncio
async def test_auto_send_to_codex_blocks_team_run_when_gate_a_artifact_missing(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY
    from wlcodex.conversation_callback import AUTO_SEND_TO_CODEX, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    ctrl = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        implementer_model_profiles=("codex_gpt",),
        adaptive_team_model_profiles={"codex_gpt": "codex"},
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修改 tracked.txt，并运行 focused pytest。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CODEX)
    )

    assert "Gate A" in response.text
    assert "architecture_plan" in response.text
    assert ledger.list_team_agent_jobs(team_run.id) == []
    assert ledger.list_agent_runs(conversation.id) == []
    assert backend.turns == []


@pytest.mark.asyncio
async def test_auto_send_to_codex_starts_implementer_with_team_context(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_RUNNING,
        AUTO_DRAFT_READY,
        AUTO_VIEW_STATUS,
        ROLE_AUTO_IMPLEMENTATION,
    )
    from wlcodex.conversation_callback import AUTO_SEND_TO_CODEX, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        implementer_model_profiles=("claude_deepseek", "strong_codex"),
        adaptive_team_model_profiles={
            "claude_deepseek": "claude",
            "strong_codex": "codex",
        },
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "修改 tracked.txt",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修改 tracked.txt，并运行 focused pytest。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    _record_valid_architecture_plan(ledger, team_run_id=team_run.id)

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CODEX)
    )

    updated_orch = ledger.get_orchestration_run(orch_run.id)
    updated_conversation = ledger.get_conversation(conversation.id)
    agent_runs = ledger.list_agent_runs(conversation.id, limit=5)
    implementation_run = agent_runs[0]
    jobs = ledger.list_team_agent_jobs(team_run.id)
    implementer_job = jobs[0]
    assignments = ledger._conn.execute(
        """
        SELECT role, model_profile, selected_by FROM team_assignments
        WHERE team_run_id = ?
        """,
        (team_run.id,),
    ).fetchall()
    context_packet = ledger.get_team_context_packet_for_job(implementer_job.id)
    activations = ledger.list_team_skill_activations(implementer_job.id)

    assert "Codex 开始执行" in response.text
    assert AUTO_VIEW_STATUS in _callback_actions(response.buttons)
    assert updated_orch.status == "running"
    assert updated_orch.current_step == AUTO_CLAUDE_RUNNING
    assert updated_conversation.active_codex_task_id == implementation_run.hidden_task_id
    assert implementation_run.agent == "codex"
    assert implementation_run.role == ROLE_AUTO_IMPLEMENTATION
    assert implementation_run.status == "running"
    assert implementer_job.role == "implementer"
    assert implementer_job.status == "running"
    assert implementer_job.model_profile == "strong_codex"
    assert implementer_job.agent_run_id == implementation_run.id
    assert [dict(row) for row in assignments] == [{
        "role": "implementer",
        "model_profile": "strong_codex",
        "selected_by": "policy",
    }]
    assert context_packet is not None
    assert context_packet.agent_job_id == implementer_job.id
    assert context_packet.packet["role"] == "implementer"
    assert context_packet.packet["model_profile"] == "strong_codex"
    assert "方案：修改 tracked.txt" in context_packet.prompt_text
    assert {activation.activation_type for activation in activations} >= {
        "skill",
        "tool",
    }
    assert backend.turns
    _thread_id, prompt = backend.turns[-1]
    assert "方案：修改 tracked.txt" in prompt
    assert "implementer" in prompt
    assert context_packet.packet["resume_state"].startswith(
        "final plan accepted; implementation selected by user"
    )
    assert context_packet.packet["required_output_schema"] == "implementation_report"


@pytest.mark.asyncio
async def test_auto_final_plan_completion_records_architecture_artifact(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final plan",
        codex_thread_id="thread-final-plan-artifact",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "fix the checkout flow",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-plan-artifact",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the checkout flow",
        route="staged_auto",
        risk_level="medium",
    )
    architect_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="architect",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    bridge = _auto_event_bridge(service=service, ledger=ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-final-plan-artifact", "turnId": "turn-plan"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-plan-artifact",
            "turnId": "turn-plan",
            "delta": "最终方案：修改 checkout.py。\n验收：运行 pytest。",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-final-plan-artifact", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    artifacts = ledger.list_team_artifacts(team_run.id)
    architecture = [a for a in artifacts if a.artifact_type == "architecture_plan"]

    assert updated.current_step == AUTO_DRAFT_READY
    assert len(architecture) == 1
    artifact = architecture[0]
    assert artifact.agent_job_id == architect_job.id
    assert "checkout.py" in artifact.summary
    assert artifact.payload["summary"] == artifact.summary
    assert artifact.payload["risk_level"] == "medium"
    assert artifact.payload["acceptance_criteria"]
    assert artifact.payload["files_or_modules_in_scope"]
    assert artifact.payload["files_or_modules_out_of_scope"]
    assert artifact.payload["impact_notes"]
    assert artifact.payload["parallelization_policy"]
    assert artifact.payload["source"] == "auto_final_plan_completion"


@pytest.mark.asyncio
async def test_auto_final_plan_completion_with_empty_output_skips_architecture_artifact(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final plan",
        codex_thread_id="thread-empty-final-plan",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "fix the checkout flow",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-empty-final-plan",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the checkout flow",
        route="staged_auto",
        risk_level="medium",
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="architect",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    bridge = _auto_event_bridge(service=service, ledger=ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-empty-final-plan", "turnId": "turn-plan"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-empty-final-plan",
            "turnId": "turn-plan",
            "delta": "   \n\t",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-empty-final-plan", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    artifacts = ledger.list_team_artifacts(team_run.id)

    assert updated.current_step == AUTO_DRAFT_READY
    assert [
        artifact for artifact in artifacts
        if artifact.artifact_type == "architecture_plan"
    ] == []


@pytest.mark.asyncio
async def test_codex_implementation_completion_records_report_evidence_from_hidden_task(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_DONE,
        AUTO_CLAUDE_RUNNING,
        AUTO_COMPLETED,
        AUTO_VERIFYING,
        ROLE_AUTO_IMPLEMENTATION,
        ROLE_AUTO_VERIFICATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Implement final plan",
        codex_thread_id="thread-codex-impl-evidence",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "change tracked file",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-codex-impl-evidence",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="change tracked file",
        route="staged_auto",
        risk_level="medium",
    )
    implementer_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    ledger.record_touched_file(task.id, "tracked.txt", "modified")
    ledger.add_event(task.id, "diff_updated", {"diff": "diff --git a/tracked.txt b/tracked.txt\n+updated"})
    bridge = _auto_event_bridge(service=service, ledger=ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-codex-impl-evidence", "turnId": "turn-impl"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-codex-impl-evidence",
            "turnId": "turn-impl",
            "delta": "Implementation complete.",
        },
    ))
    await bridge.process_event(BackendEvent(
        "item_completed",
        {
            "threadId": "thread-codex-impl-evidence",
            "item": {
                "type": "commandExecution",
                "status": "completed",
                "command": "pytest tests/test_checkout.py -q",
            },
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-codex-impl-evidence", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    artifacts = [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "implementation_report"
    ]
    test_reports = [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "test_report"
    ]

    assert updated.current_step == AUTO_CLAUDE_DONE
    assert len(artifacts) == 1
    assert artifacts[0].agent_job_id == implementer_job.id
    assert artifacts[0].payload["changed_files"] == ["tracked.txt"]
    assert "tracked.txt" in artifacts[0].payload["diff_summary"]
    assert artifacts[0].payload["commands_run"] == [
        {
            "command": "pytest tests/test_checkout.py -q",
            "exit_status": 0,
            "summary": "command completed",
        }
    ]
    assert artifacts[0].payload["tests_attempted"] == [
        {
            "command": "pytest tests/test_checkout.py -q",
            "exit_status": 0,
            "summary": "command completed",
        }
    ]
    assert len(test_reports) == 1
    assert test_reports[0].payload["commands_run"] == artifacts[0].payload["tests_attempted"]
    assert test_reports[0].payload["passed"] == ["pytest tests/test_checkout.py -q"]
    assert test_reports[0].payload["coverage_of_acceptance_criteria"]

    verify_task = service.start_task(
        "demo",
        "Verify final result",
        codex_thread_id="thread-codex-verify-happy",
        telegram_chat_id=123,
    )
    ledger.set_conversation_active_task(conversation.id, verify_task.id)
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_verification_result="",
    )
    verify_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=verify_task.id,
        external_session_id="thread-codex-verify-happy",
    )
    ledger.update_agent_run_status(verify_run.id, "running")
    auditor_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="auditor",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=verify_run.id,
    )
    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-codex-verify-happy", "turnId": "turn-verify"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-codex-verify-happy",
            "turnId": "turn-verify",
            "delta": (
                "decision: pass\n"
                "summary: Reviewed focused pytest evidence.\n"
                "test_evidence_refs:\n"
                f"- team_artifact={test_reports[0].id}"
            ),
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-codex-verify-happy", "status": "completed"},
    ))

    completed_orch = ledger.get_orchestration_run(orch_run.id)
    completed_team = ledger.get_team_run(team_run.id)
    updated_auditor_job = [
        job for job in ledger.list_team_agent_jobs(team_run.id)
        if job.id == auditor_job.id
    ][0]

    assert completed_orch.current_step == AUTO_COMPLETED
    assert completed_team is not None
    assert completed_team.status == "completed"
    assert updated_auditor_job.status == "done"


@pytest.mark.asyncio
async def test_auto_send_to_claude_creates_implementer_context_before_backend_starts_and_records_completion(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY
    from wlcodex.conversation_callback import AUTO_SEND_TO_CLAUDE, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    claude = FakeClaudeBackendForController()
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
        implementer_model_profiles=("claude_deepseek",),
        adaptive_team_model_profiles={"claude_deepseek": "claude"},
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修改 tracked.txt，并运行 focused pytest。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    _record_valid_architecture_plan(ledger, team_run_id=team_run.id)

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CLAUDE)
    )

    assert "Claude 开始执行" in response.text
    assert claude.calls == []
    implementer_job = ledger.list_team_agent_jobs(team_run.id)[0]
    context_packet = ledger.get_team_context_packet_for_job(implementer_job.id)
    assert implementer_job.role == "implementer"
    assert implementer_job.model_profile == "claude_deepseek"
    assert implementer_job.status == "running"
    assert context_packet is not None
    assert context_packet.packet["role"] == "implementer"
    assert context_packet.packet["model_profile"] == "claude_deepseek"
    assert context_packet.packet["resume_state"].startswith(
        "final plan accepted; implementation selected by user"
    )
    assert context_packet.packet["required_output_schema"] == "implementation_report"

    background = list(getattr(ctrl, "_background_tasks", set()))
    assert background
    await asyncio.gather(*background)
    await asyncio.sleep(0)

    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]
    artifacts = ledger.list_team_artifacts(team_run.id)
    implementation = [
        artifact
        for artifact in artifacts
        if artifact.artifact_type == "implementation_report"
    ]
    test_reports = [
        artifact
        for artifact in artifacts
        if artifact.artifact_type == "test_report"
    ]
    assert claude.calls
    assert updated_job.status == "done"
    assert len(implementation) == 1
    assert implementation[0].agent_job_id == updated_job.id
    assert implementation[0].payload["summary"] == "Fake Claude implementation result."
    assert implementation[0].payload["changed_files"]
    assert implementation[0].payload["diff_summary"]
    assert implementation[0].payload["source_agent"] == "claude"
    assert implementation[0].payload["commands_run"]
    assert implementation[0].payload["tests_attempted"]
    assert implementation[0].payload["known_limitations"] == ["None known"]
    assert len(test_reports) == 1
    assert test_reports[0].agent_job_id == updated_job.id


@pytest.mark.asyncio
async def test_claude_streaming_completion_accumulates_structured_evidence(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY
    from wlcodex.conversation_callback import AUTO_SEND_TO_CLAUDE, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    renderer = RecordingInteractionRenderer()
    claude = StreamingClaudeWithStructuredEvidence()
    ctrl = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        claude_backend=claude,
        interaction_renderer=renderer,
        implementer_model_profiles=("claude_deepseek",),
        adaptive_team_model_profiles={"claude_deepseek": "claude"},
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修改 tracked.txt，并运行 streaming pytest。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    _record_valid_architecture_plan(ledger, team_run_id=team_run.id)

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CLAUDE)
    )

    assert response.already_rendered is False
    background = list(getattr(ctrl, "_background_tasks", set()))
    assert background
    await asyncio.gather(*background)
    await asyncio.sleep(0)

    artifacts = ledger.list_team_artifacts(team_run.id)
    implementation = [
        artifact for artifact in artifacts
        if artifact.artifact_type == "implementation_report"
    ][0]
    test_report = [
        artifact for artifact in artifacts
        if artifact.artifact_type == "test_report"
    ][0]
    implementer_job = [
        job for job in ledger.list_team_agent_jobs(team_run.id)
        if job.id == implementation.agent_job_id
    ][0]
    agent_run = ledger.get_agent_run(implementer_job.agent_run_id)
    assert "pytest tests/test_streaming.py -q" in agent_run.completion_summary
    assert implementation.payload["commands_run"] == [
        {
            "command": "pytest tests/test_streaming.py -q",
            "exit_status": 0,
            "summary": "streaming tests passed",
        }
    ]
    assert implementation.payload["tests_attempted"] == implementation.payload["commands_run"]
    assert test_report.payload["passed"] == ["pytest tests/test_streaming.py -q"]


@pytest.mark.asyncio
async def test_claude_completion_records_report_evidence_from_active_run_hidden_task(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_DONE, AUTO_CLAUDE_RUNNING

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=FakeClaudeBackendForController(),
        runtime_event_store=store,
        implementer_model_profiles=("claude_deepseek",),
        adaptive_team_model_profiles={"claude_deepseek": "claude"},
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    task = service.start_task(
        "wlcodex",
        "Implement with Claude",
        telegram_chat_id=100,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "claude",
        "auto_implementation",
        hidden_task_id=task.id,
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    ledger.set_conversation_active_claude_run(conversation.id, agent_run.id)
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    implementer_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="running",
        agent_run_id=agent_run.id,
    )
    ledger.record_touched_file(task.id, "tracked.txt", "modified")
    ledger.add_event(task.id, "diff_updated", {"diff": "diff --git a/tracked.txt b/tracked.txt\n+claude"})

    ctrl._transition_auto_claude_completed(
        conversation.id,
        agent_status="done",
        completion_summary="Claude implementation result.",
    )

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]
    artifacts = [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "implementation_report"
    ]

    assert updated.current_step == AUTO_CLAUDE_DONE
    assert updated_job.status == "done"
    assert len(artifacts) == 1
    assert artifacts[0].agent_job_id == implementer_job.id
    assert artifacts[0].payload["changed_files"] == ["tracked.txt"]
    assert "tracked.txt" in artifacts[0].payload["diff_summary"]
    event_types = [
        event.event_type
        for event in store.list_by_conversation(conversation.id, limit=100)
    ]
    assert EventType.TEAM_ARTIFACT_RECORDED in event_types
    assert EventType.TEAM_AGENT_JOB_COMPLETED in event_types


@pytest.mark.asyncio
async def test_auto_send_repair_to_claude_creates_current_implementer_job_context_and_artifacts(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_RETRY_READY
    from wlcodex.conversation_callback import (
        AUTO_SEND_REPAIR_TO_CLAUDE,
        ConversationCallback,
    )

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    claude = FakeClaudeBackendForController()
    ctrl = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        claude_backend=claude,
        implementer_model_profiles=("claude_deepseek",),
        adaptive_team_model_profiles={"claude_deepseek": "claude"},
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_RETRY_READY,
        last_codex_analysis="方案：修改 tracked.txt，并运行 focused pytest。",
        last_claude_summary="第一轮实现完成。",
        last_verification_result="decision: block\nsummary: 需要返工。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_REPAIR_TO_CLAUDE)
    )

    assert "Claude 开始返工" in response.text
    jobs = ledger.list_team_agent_jobs(team_run.id)
    assert len(jobs) == 1
    implementer_job = jobs[0]
    assert implementer_job.role == "implementer"
    assert implementer_job.status == "running"
    assert implementer_job.agent_run_id is not None
    packet = ledger.get_team_context_packet_for_job(implementer_job.id)
    assert packet is not None
    assert packet.packet["role"] == "implementer"
    assert packet.packet["required_output_schema"] == "implementation_report"
    assert "repair selected by user" in packet.packet["resume_state"]

    background = list(getattr(ctrl, "_background_tasks", set()))
    assert background
    await asyncio.gather(*background)
    await asyncio.sleep(0)

    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]
    artifacts = ledger.list_team_artifacts(team_run.id)
    implementation_reports = [
        artifact for artifact in artifacts
        if artifact.artifact_type == "implementation_report"
    ]
    test_reports = [
        artifact for artifact in artifacts
        if artifact.artifact_type == "test_report"
    ]
    assert updated_job.status == "done"
    assert len(implementation_reports) == 1
    assert implementation_reports[0].agent_job_id == updated_job.id
    assert len(test_reports) == 1
    assert test_reports[0].agent_job_id == updated_job.id


@pytest.mark.asyncio
async def test_auto_send_to_codex_marks_team_failed_when_codex_start_fails(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY
    from wlcodex.conversation_callback import AUTO_SEND_TO_CODEX, ConversationCallback

    class FailingStartController(CommandController):
        async def _start_codex_turn_for_conversation(self, **kwargs) -> str:
            raise RuntimeError("codex implementer start boom")

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = FailingStartController(
        service,
        backend,
        inspector,
        ledger=ledger,
        implementer_model_profiles=("codex_gpt",),
        adaptive_team_model_profiles={"codex_gpt": "codex"},
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "修改 tracked.txt",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修改 tracked.txt，并运行 focused pytest。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    _record_valid_architecture_plan(ledger, team_run_id=team_run.id)

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CODEX)
    )

    updated_orch = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    agent_run = ledger.list_agent_runs(conversation.id, limit=1)[0]
    implementer_job = ledger.list_team_agent_jobs(team_run.id)[0]

    assert response.text
    assert updated_orch.status == "failed"
    assert "codex implementer start boom" in updated_orch.last_claude_summary
    assert updated_team is not None
    assert updated_team.status == "failed"
    assert agent_run.status == "failed"
    assert implementer_job.status == "failed"
    assert backend.turns == []


@pytest.mark.asyncio
async def test_auto_verification_records_audit_artifact(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_DONE, AUTO_VERIFYING
    from wlcodex.conversation_callback import AUTO_CODEX_VERIFY, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        runtime_event_store=store,
        auditor_model_profile="auditor_codex",
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_CLAUDE_DONE,
        last_codex_analysis="方案：修改 tracked.txt，并运行 focused pytest。",
        last_claude_summary="实现完成：更新 tracked.txt。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    implementation_task = service.start_task(
        "wlcodex",
        "Implement tracked.txt",
        telegram_chat_id=100,
    )
    implementer_run = ledger.create_agent_run(
        conversation.id,
        "claude",
        "auto_implementation",
        hidden_task_id=implementation_task.id,
    )
    ledger.update_agent_run_status(
        implementer_run.id,
        "done",
        completion_summary="实现完成：更新 tracked.txt。",
    )
    implementer_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="done",
        agent_run_id=implementer_run.id,
    )
    _record_valid_implementation_report(
        ledger,
        team_run_id=team_run.id,
        agent_job_id=implementer_job.id,
    )
    _record_valid_test_report(
        ledger,
        team_run_id=team_run.id,
        agent_job_id=implementer_job.id,
    )
    ledger.set_task_status(implementation_task.id, TaskStatus.DONE)
    wrong_task = service.start_task(
        "wlcodex",
        "Unrelated active Codex task",
        telegram_chat_id=100,
    )
    ledger.set_conversation_active_task(conversation.id, wrong_task.id)
    ledger.set_task_status(wrong_task.id, TaskStatus.DONE)
    ledger.record_touched_file(implementation_task.id, "tracked.txt", "modified")
    ledger.add_event(
        implementation_task.id,
        "diff_updated",
        {"diff": "diff --git a/tracked.txt b/tracked.txt\n+verified evidence"},
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_CODEX_VERIFY)
    )

    updated = ledger.get_orchestration_run(orch_run.id)
    auditor_job = [
        job for job in ledger.list_team_agent_jobs(team_run.id)
        if job.role == "auditor"
    ][0]
    agent_run = ledger.get_agent_run(auditor_job.agent_run_id)
    context_packet = ledger.get_team_context_packet_for_job(auditor_job.id)
    artifacts = ledger.list_team_artifacts(team_run.id)
    verification_request = [
        artifact
        for artifact in artifacts
        if artifact.artifact_type == "verification_request"
    ]

    assert "Codex 开始验收" in response.text
    assert updated.current_step == AUTO_VERIFYING
    assert auditor_job.role == "auditor"
    assert auditor_job.model_profile == "auditor_codex"
    assert auditor_job.status == "running"
    assert auditor_job.agent_run_id == agent_run.id
    assert context_packet is not None
    assert context_packet.packet["role"] == "auditor"
    assert context_packet.packet["model_profile"] == "auditor_codex"
    assert context_packet.packet["required_output_schema"] == "audit_report"
    assert any(
        "Auditor performs tester duties in v1" in rule
        for rule in context_packet.packet["handoff_rules"]
    )
    assert len(verification_request) == 1
    assert (
        verification_request[0].payload["tester_policy"]
        == "Auditor performs tester duties in v1"
    )
    assert verification_request[0].agent_job_id == auditor_job.id
    assert verification_request[0].payload["goal"] == "修改 tracked.txt"
    assert verification_request[0].payload["codex_plan_summary"].startswith("方案")
    assert verification_request[0].payload["implementation_summary"].startswith("实现完成")
    assert verification_request[0].payload["changed_files"] == ["tracked.txt"]
    assert "verified evidence" in verification_request[0].payload["diff_summary"]
    assert verification_request[0].payload["verify_round"] == 1
    artifact_events = [
        event
        for event in store.list_by_conversation(conversation.id, limit=100)
        if event.event_type == EventType.TEAM_ARTIFACT_RECORDED
    ]
    assert any(
        event.payload.get("artifact_type") == "verification_request"
        and event.payload.get("artifact_id") == verification_request[0].id
        for event in artifact_events
    )
    assert backend.turns


@pytest.mark.asyncio
async def test_auto_verification_blocks_team_run_when_gate_b_report_missing(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_DONE, AUTO_RETRY_READY
    from wlcodex.conversation_callback import AUTO_CODEX_VERIFY, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    ctrl = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        auditor_model_profile="auditor_codex",
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_CLAUDE_DONE,
        last_codex_analysis="方案：修改 tracked.txt。",
        last_claude_summary="实现完成：更新 tracked.txt。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_CODEX_VERIFY)
    )

    updated = ledger.get_orchestration_run(orch_run.id)
    assert "Gate B" in response.text
    assert "implementation_report" in response.text
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_RETRY_READY
    assert "auto_send_to_codex" in _button_actions(response.buttons)
    assert ledger.list_team_agent_jobs(team_run.id) == []
    assert ledger.list_agent_runs(conversation.id) == []
    assert backend.turns == []


@pytest.mark.asyncio
async def test_auto_verification_requires_current_implementer_artifacts_after_repair(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_DONE
    from wlcodex.conversation_callback import AUTO_CODEX_VERIFY, ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    ctrl = CommandController(
        service,
        backend,
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
        auditor_model_profile="auditor_codex",
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_CLAUDE_DONE,
        last_codex_analysis="方案：修改 tracked.txt。",
        last_claude_summary="第二轮返工完成但没有新证据。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    first_run = ledger.create_agent_run(conversation.id, "claude", "auto_implementation")
    first_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="done",
        agent_run_id=first_run.id,
    )
    _record_valid_implementation_report(
        ledger,
        team_run_id=team_run.id,
        agent_job_id=first_job.id,
    )
    _record_valid_test_report(
        ledger,
        team_run_id=team_run.id,
        agent_job_id=first_job.id,
    )
    second_run = ledger.create_agent_run(conversation.id, "claude", "auto_repair")
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="done",
        agent_run_id=second_run.id,
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_CODEX_VERIFY)
    )

    assert "Gate B" in response.text
    assert "implementation_report" in response.text
    assert [
        job for job in ledger.list_team_agent_jobs(team_run.id)
        if job.role == "auditor"
    ] == []
    assert backend.turns == []


@pytest.mark.asyncio
async def test_auto_verification_marks_team_failed_when_codex_start_fails(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_DONE
    from wlcodex.conversation_callback import AUTO_CODEX_VERIFY, ConversationCallback

    class FailingStartController(CommandController):
        async def _start_codex_turn_for_conversation(self, **kwargs) -> str:
            raise RuntimeError("codex verification start boom")

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = FailingStartController(
        service,
        backend,
        inspector,
        ledger=ledger,
        auditor_model_profile="auditor_codex",
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_CLAUDE_DONE,
        last_codex_analysis="方案：修改 tracked.txt，并运行 focused pytest。",
        last_claude_summary="实现完成：更新 tracked.txt。",
    )
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="修改 tracked.txt",
        route="staged_auto",
        risk_level="medium",
    )
    implementer_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="done",
        agent_run_id=None,
    )
    _record_valid_implementation_report(
        ledger,
        team_run_id=team_run.id,
        agent_job_id=implementer_job.id,
    )
    _record_valid_test_report(
        ledger,
        team_run_id=team_run.id,
        agent_job_id=implementer_job.id,
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_CODEX_VERIFY)
    )

    updated_orch = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    auditor_job = [
        job for job in ledger.list_team_agent_jobs(team_run.id)
        if job.role == "auditor"
    ][0]
    agent_run = ledger.get_agent_run(auditor_job.agent_run_id)
    task = ledger.get_task(agent_run.hidden_task_id)
    verification_request = [
        artifact
        for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "verification_request"
    ]

    assert response.text
    assert updated_orch.status == "failed"
    assert "codex verification start boom" in updated_orch.last_verification_result
    assert updated_team is not None
    assert updated_team.status == "failed"
    assert task.status == TaskStatus.FAILED
    assert agent_run.status == "failed"
    assert auditor_job.status == "failed"
    assert len(verification_request) == 1
    assert verification_request[0].agent_job_id == auditor_job.id
    assert backend.turns == []


@pytest.mark.asyncio
async def test_auto_codex_paths_without_team_run_do_not_create_team_records(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_DONE, AUTO_DRAFT_READY
    from wlcodex.conversation_callback import (
        AUTO_CODEX_VERIFY,
        AUTO_SEND_TO_CODEX,
        ConversationCallback,
    )

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        implementer_model_profiles=("codex_gpt",),
        adaptive_team_model_profiles={"codex_gpt": "codex"},
        auditor_model_profile="auditor_codex",
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修改 tracked.txt。",
    )

    implement_response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CODEX)
    )

    assert "Codex 开始执行" in implement_response.text
    assert ledger._conn.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0] == 0
    assert ledger._conn.execute("SELECT COUNT(*) FROM team_agent_jobs").fetchone()[0] == 0
    assert ledger._conn.execute("SELECT COUNT(*) FROM team_artifacts").fetchone()[0] == 0
    active = ledger.get_conversation(conversation.id)
    assert active.active_codex_task_id is not None
    ledger.set_task_status(active.active_codex_task_id, TaskStatus.DONE)

    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_CLAUDE_DONE,
        last_claude_summary="实现完成。",
    )
    verify_response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_CODEX_VERIFY)
    )

    assert "Codex 开始验收" in verify_response.text
    assert ledger._conn.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0] == 0
    assert ledger._conn.execute("SELECT COUNT(*) FROM team_agent_jobs").fetchone()[0] == 0
    assert ledger._conn.execute("SELECT COUNT(*) FROM team_artifacts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_auto_verification_completion_records_audit_observations_and_instincts(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_RETRY_READY,
        AUTO_VERIFYING,
        ROLE_AUTO_VERIFICATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Verify implementation",
        codex_thread_id="thread-audit-artifact",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_codex_analysis="方案：修复 workflow。",
        last_claude_summary="实现完成。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=task.id,
        external_session_id="thread-audit-artifact",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    auditor_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="auditor",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    bridge = _auto_event_bridge(service=service, ledger=ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-audit-artifact", "turnId": "turn-audit"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-audit-artifact",
            "turnId": "turn-audit",
            "delta": (
                "decision: block\n"
                "summary: Blocking because regression evidence is missing.\n"
                "findings:\n"
                "- No focused pytest output was provided.\n"
                "missing_evidence:\n"
                "- pytest command output"
            ),
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-audit-artifact", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    artifacts = ledger.list_team_artifacts(team_run.id)
    audit_reports = [
        artifact
        for artifact in artifacts
        if artifact.artifact_type == "audit_report"
    ]
    observations = ledger.list_team_observations(team_run.id)
    instincts = ledger.list_team_instincts(status="candidate")
    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]

    assert updated.current_step == AUTO_RETRY_READY
    assert len(audit_reports) == 1
    report = audit_reports[0]
    assert report.agent_job_id == auditor_job.id
    assert report.payload["decision"] == "block"
    assert "focused pytest" in report.payload["findings"][0]
    assert report.payload["missing_evidence"] == ["pytest command output"]
    assert observations
    assert observations[0].evidence_refs == (f"team_artifact={report.id}",)
    assert instincts
    assert instincts[0].evidence_refs == observations[0].evidence_refs
    assert updated_job.status == "done"


@pytest.mark.asyncio
async def test_repeated_blocking_audit_promotes_instinct_into_next_auditor_packet(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_VERIFYING, ROLE_AUTO_VERIFICATION
    from wlcodex.event_bridge import EventBridge

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    store = RuntimeEventStore(ledger._conn)

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        return 1

    async def edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
        return None

    class ApprovalService:
        async def expire_stale_approvals(self, ledger: Ledger, backend: object) -> None:
            return None

    bridge = EventBridge(
        service,
        FakeCodexBackend(),
        ledger,
        send_telegram,
        edit_telegram,
        ApprovalService(),
        runtime_event_store=store,
    )

    async def run_blocking_audit(thread_id: str, goal: str) -> object:
        task = service.start_task(
            "demo",
            goal,
            codex_thread_id=thread_id,
            telegram_chat_id=123,
        )
        conversation = ledger.create_conversation(
            chat_id=123,
            user_id=456,
            title=goal,
            mode="chief_engineer",
            workspace_alias="demo",
        )
        ledger.set_conversation_active_task(conversation.id, task.id)
        orch_run = ledger.create_orchestration_run(conversation.id, goal)
        ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_VERIFYING,
            last_codex_analysis="方案：修复 workflow。",
            last_claude_summary="实现完成。",
        )
        agent_run = ledger.create_agent_run(
            conversation.id,
            "codex",
            ROLE_AUTO_VERIFICATION,
            hidden_task_id=task.id,
            external_session_id=thread_id,
        )
        ledger.update_agent_run_status(agent_run.id, "running")
        team_run = ledger.create_team_run(
            conversation_id=conversation.id,
            orchestration_run_id=orch_run.id,
            goal=goal,
            route="staged_auto",
            risk_level="medium",
        )
        ledger.create_team_agent_job(
            team_run_id=team_run.id,
            role="auditor",
            model_profile="codex_gpt",
            status="running",
            agent_run_id=agent_run.id,
        )
        await bridge.process_event(BackendEvent(
            "turn_started",
            {"threadId": thread_id, "turnId": f"{thread_id}-turn"},
        ))
        await bridge.process_event(BackendEvent(
            "agent_message_delta",
            {
                "threadId": thread_id,
                "turnId": f"{thread_id}-turn",
                "delta": (
                    "decision: block\n"
                    "summary: Blocking because regression evidence is missing.\n"
                    "findings:\n"
                    "- No focused pytest output was provided.\n"
                    "missing_evidence:\n"
                    "- pytest command output"
                ),
            },
        ))
        await bridge.process_event(BackendEvent(
            "turn_completed",
            {"threadId": thread_id, "status": "completed"},
        ))
        return team_run

    first_team = await run_blocking_audit("thread-audit-memory-1", "first audit")
    assert ledger.list_team_instincts(status="active") == []
    assert ledger.list_team_instincts(status="candidate")

    second_team = await run_blocking_audit("thread-audit-memory-2", "second audit")
    active = ledger.list_team_instincts(status="active")
    assert active

    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="next audit",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    orch = ledger.create_orchestration_run(conversation.id, "third audit")
    next_team = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch.id,
        goal="third audit needs pytest evidence",
        route="staged_auto",
        risk_level="medium",
    )
    next_job = ledger.create_team_agent_job(
        team_run_id=next_team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )
    ctrl = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
    )

    packet = ctrl._build_team_context_packet_for_job(
        team_run=next_team,
        agent_job=next_job,
        role="auditor",
        model_profile="codex_gpt",
        resume_state="audit missing pytest evidence",
        output_schema="audit_report",
    )

    assert packet.as_json()["relevant_instincts"]
    event_types = [
        event.event_type
        for team in (first_team, second_team)
        for event in store.list_by_conversation(team.conversation_id, limit=100)
    ]
    assert EventType.TEAM_OBSERVATION_RECORDED in event_types
    assert EventType.TEAM_INSTINCT_PROMOTED in event_types
    assert first_team.id != second_team.id


@pytest.mark.asyncio
async def test_auto_send_to_claude_rejects_missing_visible_final_plan(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, AUTO_SEND_TO_CLAUDE
    from wlcodex.conversation_callback import ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    claude = StreamingClaudeWritesTrackedFile(workspace)
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=claude,
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ledger.update_orchestration_run(
        ledger.create_orchestration_run(conversation.id, "修改 tracked.txt").id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(
            conversation_id=conversation.id,
            action=AUTO_SEND_TO_CLAUDE,
        )
    )

    labels = [
        button["text"]
        for row in (response.buttons or [])
        for button in row
    ]
    assert "没有可见的最终方案正文" in response.text
    assert "交给 Claude 执行" not in labels
    assert "继续补充" in labels
    assert "重写方案" not in labels
    assert claude.prompts == []


@pytest.mark.asyncio
async def test_auto_rewrite_plan_from_draft_ready_starts_final_plan_generation(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, AUTO_REWRITE_PLAN
    from wlcodex.conversation_callback import ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=StreamingClaudeWritesTrackedFile(workspace),
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "修改 tracked.txt")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="旧方案",
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(
            conversation_id=conversation.id,
            action=AUTO_REWRITE_PLAN,
        )
    )

    labels = [
        button["text"]
        for row in (response.buttons or [])
        for button in row
    ]
    updated = ledger.get_orchestration_run(orch_run.id)
    assert "正在生成最终方案" in response.text
    assert updated.status == "running"
    assert updated.current_step == "collecting_context"
    assert "生成最终方案" not in labels
    assert "查看状态" in labels
    assert backend.prompt_turns == []
    assert backend.turns


@pytest.mark.asyncio
async def test_auto_continue_context_plain_text_stays_in_auto_flow(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CONTINUE_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_CONTEXT_SUPPLEMENT,
    )
    from wlcodex.conversation_callback import ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        backend,
        inspector,
        ledger=ledger,
        claude_backend=StreamingClaudeWritesTrackedFile(workspace),
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch_run = ledger.create_orchestration_run(conversation.id, "查线上问题")
    ledger.update_orchestration_run(
        orch_run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="结论：已有方案。",
    )

    await ctrl.handle_conversation_callback(
        ConversationCallback(
            conversation_id=conversation.id,
            action=AUTO_CONTINUE_CONTEXT,
        )
    )
    response = await ctrl.handle_conversation_text(
        "补充：需要列出新问题、老问题和是否开仓",
        {"chat_id": 100, "user_id": 200},
    )

    updated = ledger.get_orchestration_run(orch_run.id)
    agent_runs = ledger.list_agent_runs(conversation.id)
    assert "已补充信息到当前 /auto 分析" in response.text
    assert updated.status == "running"
    assert updated.current_step == "collecting_context"
    assert agent_runs[-1].role == ROLE_AUTO_CONTEXT_SUPPLEMENT


@pytest.mark.asyncio
async def test_auto_show_draft_returns_enough_final_plan_to_review(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, AUTO_SHOW_DRAFT
    from wlcodex.conversation_callback import ConversationCallback

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    ctrl = CommandController(
        service,
        FakeCodexBackend(),
        inspector,
        ledger=ledger,
    )
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    long_plan = "最终方案：\n" + ("步骤内容\n" * 320) + "TAIL-验收标准"
    ledger.update_orchestration_run(
        ledger.create_orchestration_run(conversation.id, "修改 tracked.txt").id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis=long_plan,
    )

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(
            conversation_id=conversation.id,
            action=AUTO_SHOW_DRAFT,
        )
    )

    assert "当前方案" in response.text
    assert "TAIL-验收标准" in response.text
    labels = [
        button["text"]
        for row in (response.buttons or [])
        for button in row
    ]
    assert "交给 Claude 执行" in labels


@pytest.mark.asyncio
async def test_staged_auto_binds_codex_thread_to_task(tmp_path: Path) -> None:
    """Staged /auto collecting_context must bind its Codex thread to the task."""
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
    # Staged /auto starts one Codex thread for collecting_context analysis
    assert len(thread_ids) == 1
    assert thread_ids[0].startswith("fake-")


@pytest.mark.asyncio
async def test_staged_auto_collecting_context_no_claude_called(tmp_path: Path) -> None:
    """Staged /auto must NOT call Claude; it only starts Codex collecting_context."""
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    backend._codex_responses = [
        "Root cause: tracked.txt needs a change. Implementation needed.",
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

    # Staged /auto does NOT call Claude — only Codex
    # The StreamingClaudeError would fail if called, but it shouldn't be called
    assert active.active_claude_run_id is None

    # Orchestration run should be in collecting_context, running (not failed)
    orch_runs = ledger.list_orchestration_runs(active.id)
    assert len(orch_runs) == 1
    assert orch_runs[0].current_step == "collecting_context"
    assert orch_runs[0].status == "running"

    # Only Codex analysis was run — one turn
    assert len(backend.turns) == 1

    agent_runs = ledger.list_agent_runs(active.id)
    assert len(agent_runs) == 1
    assert agent_runs[0].agent == "codex"
    assert agent_runs[0].role == "auto_analysis"


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


def test_team_runtime_event_and_callback_actions_are_exported() -> None:
    from wlcodex.conversation_callback import (
        AUTO_CALLBACK_ACTIONS,
        AUTO_SEND_TO_CODEX,
        TEAM_VIEW_ARTIFACTS,
        TEAM_VIEW_STATUS,
    )

    assert EventType.TEAM_RUN_REQUESTED == "team.run.requested"
    assert EventType.TEAM_RUN_ROUTED == "team.run.routed"
    assert EventType.TEAM_RUN_STARTED == "team.run.started"
    assert EventType.TEAM_RUN_COMPLETED == "team.run.completed"
    assert EventType.TEAM_RUN_FAILED == "team.run.failed"
    assert EventType.TEAM_AGENT_JOB_QUEUED == "team.agent_job.queued"
    assert EventType.TEAM_AGENT_JOB_STARTED == "team.agent_job.started"
    assert EventType.TEAM_AGENT_JOB_COMPLETED == "team.agent_job.completed"
    assert EventType.TEAM_AGENT_JOB_FAILED == "team.agent_job.failed"
    assert EventType.TEAM_CONTEXT_PACKET_RECORDED == "team.context_packet.recorded"
    assert EventType.TEAM_ARTIFACT_RECORDED == "team.artifact.recorded"
    assert EventType.TEAM_GATE_PASSED == "team.gate.passed"
    assert EventType.TEAM_GATE_FAILED == "team.gate.failed"
    assert EventType.TEAM_ASSIGNMENT_SELECTED == "team.assignment.selected"
    assert EventType.TEAM_ASSIGNMENT_FALLBACK_USED == "team.assignment.fallback_used"
    assert EventType.TEAM_SKILL_ACTIVATED == "team.skill_activated"
    assert EventType.TEAM_CAPABILITY_BUDGET_APPLIED == "team.capability_budget.applied"
    assert EventType.TEAM_OBSERVATION_RECORDED == "team.observation.recorded"
    assert EventType.TEAM_INSTINCT_PROPOSED == "team.instinct.proposed"
    assert EventType.TEAM_INSTINCT_PROMOTED == "team.instinct.promoted"
    assert EventType.TEAM_INSTINCT_DEPRECATED == "team.instinct.deprecated"
    assert EventType.TEAM_INSTINCT_SELECTED == "team.instinct.selected"

    assert AUTO_SEND_TO_CODEX == "auto_send_to_codex"
    assert TEAM_VIEW_STATUS == "team_view_status"
    assert TEAM_VIEW_ARTIFACTS == "team_view_artifacts"
    assert AUTO_SEND_TO_CODEX in AUTO_CALLBACK_ACTIONS
    assert TEAM_VIEW_STATUS in AUTO_CALLBACK_ACTIONS
    assert TEAM_VIEW_ARTIFACTS in AUTO_CALLBACK_ACTIONS


@pytest.mark.asyncio
async def test_team_view_buttons_and_handlers_show_status_and_artifacts(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, build_auto_stage_buttons
    from wlcodex.conversation_callback import (
        ConversationCallback,
        TEAM_VIEW_ARTIFACTS,
        TEAM_VIEW_STATUS,
    )

    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (
        WorkspaceConfig("wlcodex", workspace, True),
    ))
    ctrl = CommandController(
        service,
        FakeCodexBackend(),
        TaskInspector(ledger, tmp_path / "logs"),
        ledger=ledger,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="team view",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation.id, "修复状态展示")
    ledger.update_orchestration_run(
        orch.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="方案：修复状态展示。",
    )
    team = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch.id,
        goal="修复状态展示",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="done",
        agent_run_id=None,
    )
    ledger.record_team_artifact(
        team_run_id=team.id,
        agent_job_id=job.id,
        artifact_type="audit_report",
        summary="Audit passed",
        payload={"decision": "pass"},
    )

    actions = _callback_actions(build_auto_stage_buttons(
        conversation.id,
        AUTO_DRAFT_READY,
        last_codex_analysis="方案：修复状态展示。",
        codex_implementer_enabled=True,
    ))
    assert TEAM_VIEW_STATUS in actions
    assert TEAM_VIEW_ARTIFACTS in actions

    status = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, TEAM_VIEW_STATUS)
    )
    artifacts = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation.id, TEAM_VIEW_ARTIFACTS)
    )

    assert "团队状态" in status.text
    assert "审计工程师" in status.text
    assert "团队证据" in artifacts.text
    assert "audit_report: Audit passed" in artifacts.text


def test_auto_draft_ready_buttons_include_codex_when_enabled() -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, build_auto_stage_buttons
    from wlcodex.conversation_callback import AUTO_SEND_TO_CODEX

    buttons = build_auto_stage_buttons(
        42,
        AUTO_DRAFT_READY,
        last_codex_analysis="方案：修改 auth.py，并运行 pytest tests/test_auth.py -q",
        codex_implementer_enabled=True,
    )
    actions = _callback_actions(buttons)

    assert AUTO_SEND_TO_CODEX in actions


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
    """Fake backend that raises on create_thread to test error paths."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.steers: list[tuple] = []

    async def send_codex_prompt(self, workspace: str, prompt: str) -> str:
        raise RuntimeError("Simulated Codex backend crash")

    async def create_thread(self, workspace: str) -> str:
        raise RuntimeError("Simulated Codex backend crash")

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

    # When Codex fails, we get an error response (not text_delta rendered)
    assert "错误" in response.text or "失败" in response.text or "异常" in response.text or "crash" in response.text.lower()
    await _drain_runtime_runner(ctrl)

    # Verify orchestration run is marked as failed (not left running)
    active = ledger.get_active_conversation(100)
    orch_runs = ledger.list_orchestration_runs(active.id)
    assert len(orch_runs) >= 1
    assert orch_runs[0].status == OrchestrationStatus.FAILED.value

    # Verify agent run is marked as failed (not left running)
    agent_runs = ledger.list_agent_runs(active.id)
    analysis_runs = [r for r in agent_runs if r.agent == "codex" and r.role == "auto_analysis"]
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
    assert "1 个进行中" in response.text
    assert "1 个已归档" in response.text
    assert "点击下方按钮恢复" in response.text


@pytest.mark.asyncio
async def test_workspaces_command_lists_configured_workspaces(ctrl: CommandController) -> None:
    response = await ctrl.handle("/workspaces", {"chat_id": 100, "user_id": 7})

    assert response.text == "选择工作区"
    assert "demo" not in response.text
    assert "wlcodex" not in response.text
    flat_buttons = [button for row in response.buttons for button in row]
    assert {button["text"] for button in flat_buttons} == {"切换 demo", "切换 wlcodex"}
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
    assert "工作台历史" in history.text
    assert "1 个进行中" in history.text
    assert "1 个已归档" in history.text

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


# ═══════════════════════════════════════════════════════════════
# /status shows current interaction (surface) mode
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_status_shows_terminal_surface_mode_after_switch(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="Terminal",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.CONVERSATION_MODE_SWITCHED,
        aggregate_type=AggregateType.CONVERSATION,
        aggregate_id=str(conversation.id),
        correlation_id="surface-corr",
        source=EventSource.CONTROLLER,
        actor="controller",
        visibility=Visibility.USER,
        payload={
            "chat_id": 100,
            "conversation_id": conversation.id,
            "from_mode": "product",
            "to_mode": "terminal",
            "active_agent": "claude",
        },
        occurred_at=now_iso(),
        conversation_id=conversation.id,
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

    assert "当前视图：现场" in response.text
    assert "模式：总工程师" in response.text
    assert "运行 #" not in response.text
    assert "事件总数" not in response.text


@pytest.mark.asyncio
async def test_status_defaults_to_product_without_surface_event(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="Default",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
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

    assert "当前视图：驾驶舱" in response.text
    assert "模式：总工程师" in response.text


# --- Workbench carryover controller tests ---


@pytest.mark.asyncio
async def test_carry_lists_workbench_candidates(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="云上部署核验",
        mode="chief_engineer",
        workspace_alias="lightfeev2",
    )
    ctrl._ledger.update_conversation_summary(source.id, "ALTUSDT 状态收敛未闭环。")
    ctrl._ledger.archive_conversation(source.id)

    response = await ctrl.handle("/carry", {"chat_id": 100, "user_id": 7})

    assert "可接棒历史工作台" in response.text
    assert "云上部署核验" in response.text
    labels = [button["text"] for row in (response.buttons or []) for button in row]
    assert "接棒开新工作台" in labels
    assert "查看接棒摘要" in labels
    assert "刷新摘要" in labels


@pytest.mark.asyncio
async def test_carry_search_filters_workbenches(ctrl: CommandController) -> None:
    hit = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="reduce-only 线上问题",
        mode="chief_engineer",
        workspace_alias="lightfeev2",
    )
    ctrl._ledger.update_conversation_summary(hit.id, "Binance reduce-only 仍失败。")
    ctrl._ledger.archive_conversation(hit.id)
    miss = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Telegram 摘要",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.archive_conversation(miss.id)

    response = await ctrl.handle("/carry reduce-only", {"chat_id": 100, "user_id": 7})

    assert "reduce-only 线上问题" in response.text
    assert "Telegram 摘要" not in response.text


@pytest.mark.asyncio
async def test_carry_by_id_prepares_next_goal_without_execution(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="云上部署核验",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "状态收敛未闭环。")
    ctrl._ledger.archive_conversation(source.id)

    response = await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    assert f"准备从工作台 #{source.id} 接棒" in response.text
    assert "请发送新任务目标" in response.text
    prepared = ctrl._ledger.get_latest_prepared_carryover(100)
    assert prepared is not None
    assert prepared.source_conversation_id == source.id


@pytest.mark.asyncio
async def test_carry_show_callback_displays_full_brief(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import CARRY_SHOW, ConversationCallback

    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "未闭环背景。")

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=source.id, action=CARRY_SHOW)
    )

    assert "接棒摘要" in response.text
    assert "<carryover_context>" in response.text
    assert "未闭环背景" in response.text


@pytest.mark.asyncio
async def test_prepared_carryover_next_text_creates_new_workbench(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="云上部署核验",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "状态收敛未闭环。")
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    response = await ctrl.handle_conversation_text(
        "继续查状态为什么没有收敛",
        {"chat_id": 100, "user_id": 7},
    )

    active = ctrl._ledger.get_active_conversation(100)
    carryover = ctrl._ledger.get_latest_prepared_carryover(100)
    assert "已从工作台" in response.text
    assert active is not None
    assert active.id != source.id
    assert active.workspace_alias == "wlcodex"
    assert "<carryover_context>" in active.conversation_summary
    assert "继续查状态为什么没有收敛" in active.conversation_summary
    assert carryover is None


@pytest.mark.asyncio
async def test_new_command_does_not_consume_prepared_carryover(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    await ctrl.handle("/new Clean", {"chat_id": 100, "user_id": 7})

    active = ctrl._ledger.get_active_conversation(100)
    assert active is not None
    assert active.title == "Clean"
    assert "<carryover_context>" not in active.conversation_summary
    assert ctrl._ledger.get_latest_prepared_carryover(100) is not None


@pytest.mark.asyncio
async def test_carryover_does_not_copy_old_runtime_state(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.set_conversation_active_task(source.id, 123)
    ctrl._ledger.set_conversation_active_claude_run(source.id, 456)
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    await ctrl.handle_conversation_text(
        "新目标",
        {"chat_id": 100, "user_id": 7},
    )

    active = ctrl._ledger.get_active_conversation(100)
    assert active is not None
    assert active.active_codex_task_id is None
    assert active.active_claude_run_id is None
    assert active.codex_thread_id == ""
    assert active.claude_session_id == ""


@pytest.mark.asyncio
async def test_carry_rejects_source_from_another_chat(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=999,
        user_id=7,
        title="Other Chat",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    response = await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    assert "不能接棒其他聊天" in response.text


@pytest.mark.asyncio
async def test_sessions_remains_agent_session_scoped_after_carry(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source Workbench",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.create_agent_run(
        conversation_id=source.id,
        agent="codex",
        role="analysis",
        prompt_packet_summary="source run",
    )
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})
    await ctrl.handle_conversation_text("新目标", {"chat_id": 100, "user_id": 7})

    response = await ctrl.handle("/sessions", {"chat_id": 100, "user_id": 7})

    assert "source run" not in response.text


@pytest.mark.asyncio
async def test_carry_start_callback_prepares_carryover(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import CARRY_START, ConversationCallback

    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "状态收敛。")
    ctrl._ledger.archive_conversation(source.id)

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=source.id, action=CARRY_START)
    )

    assert "准备从工作台" in response.text
    assert "请发送新任务目标" in response.text
    prepared = ctrl._ledger.get_latest_prepared_carryover(100)
    assert prepared is not None


@pytest.mark.asyncio
async def test_carry_cancel_callback_cancels_prepared(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import CARRY_CANCEL, ConversationCallback

    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=source.id, action=CARRY_CANCEL)
    )

    assert "已取消接棒" in response.text
    assert ctrl._ledger.get_latest_prepared_carryover(100) is None


@pytest.mark.asyncio
async def test_carry_refresh_callback_shows_fresh_brief(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import CARRY_REFRESH, ConversationCallback

    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "验证结果：状态收敛仍然失败。")

    response = await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=source.id, action=CARRY_REFRESH)
    )

    assert "摘要已刷新" in response.text
    assert "状态收敛仍然失败" in response.text
    assert "<carryover_context>" in response.text


@pytest.mark.asyncio
async def test_carry_refresh_updates_prepared_brief_for_next_goal(ctrl: CommandController) -> None:
    from wlcodex.conversation_callback import CARRY_REFRESH, ConversationCallback

    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "旧摘要：reduce-only 仍失败。")
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    ctrl._ledger.update_conversation_summary(source.id, "新摘要：状态收敛已修，剩余 WS 重连。")
    await ctrl.handle_conversation_callback(
        ConversationCallback(conversation_id=source.id, action=CARRY_REFRESH)
    )
    await ctrl.handle_conversation_text(
        "接着核验 WS 重连",
        {"chat_id": 100, "user_id": 7},
    )

    active = ctrl._ledger.get_active_conversation(100)
    assert active is not None
    assert "新摘要：状态收敛已修，剩余 WS 重连" in active.conversation_summary
    assert "旧摘要：reduce-only 仍失败" not in active.conversation_summary


@pytest.mark.asyncio
async def test_carryover_brief_includes_evidence_from_runs(ctrl: CommandController) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Evidence Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "背景摘要。")
    codex_run = ctrl._ledger.create_agent_run(
        conversation_id=source.id,
        agent="codex",
        role="auto_analysis",
        prompt_packet_summary="分析输入",
    )
    ctrl._ledger.update_agent_run_status(
        codex_run.id, "done", completion_summary="Codex 诊断：状态收敛失败。"
    )
    claude_run = ctrl._ledger.create_agent_run(
        conversation_id=source.id,
        agent="claude",
        role="implementation",
        prompt_packet_summary="执行输入",
    )
    ctrl._ledger.update_agent_run_status(
        claude_run.id, "done", completion_summary="Claude 完成修复。"
    )
    orch = ctrl._ledger.create_orchestration_run(source.id, "目标")
    ctrl._ledger.update_orchestration_run(
        orch.id,
        status="needs_user",
        current_step="draft_ready",
        last_codex_analysis="最终方案：修改收敛逻辑。",
        last_verification_result="验收失败：reduce-only 仍报400。",
    )

    response = await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    assert "准备从工作台" in response.text
    prepared = ctrl._ledger.get_latest_prepared_carryover(100)
    assert prepared is not None
    brief = prepared.brief_text
    assert "状态收敛失败" in brief
    assert "Claude 完成修复" in brief
    assert "验收失败" in brief
    assert f"agent_run={codex_run.id}" in brief
    assert f"orchestration_run={orch.id}" in brief


@pytest.mark.asyncio
async def test_new_carryover_cancels_previous_prepared(ctrl: CommandController) -> None:
    first = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="First Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    second = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Second Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    await ctrl.handle(f"/carry {first.id}", {"chat_id": 100, "user_id": 7})
    await ctrl.handle(f"/carry {second.id}", {"chat_id": 100, "user_id": 7})

    latest = ctrl._ledger.get_latest_prepared_carryover(100)
    assert latest is not None
    assert latest.source_conversation_id == second.id
    # The first carryover should now be cancelled.
    all_carryovers = ctrl._ledger._conn.execute(
        "SELECT id, status FROM workbench_carryovers WHERE chat_id = ? ORDER BY id",
        (100,),
    ).fetchall()
    statuses = {row["id"]: row["status"] for row in all_carryovers}
    assert statuses[1] == "cancelled"
    assert statuses[2] == "prepared"


@pytest.mark.asyncio
async def test_prepared_carryover_workspace_busy_does_not_archive(ctrl: CommandController) -> None:
    """When target workspace is busy, carryover consumption must not archive
    the current active workbench or consume the prepared carryover."""
    source = ctrl._ledger.create_conversation(
        chat_id=100, user_id=7,
        title="Source", mode="chief_engineer", workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "背景。")
    # Prepare carryover for wlcodex workspace.
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})
    assert ctrl._ledger.get_latest_prepared_carryover(100) is not None

    # Create a running task in the wlcodex workspace to make it busy.
    blocker = ctrl._service.reserve_task("wlcodex", "正在执行的任务", telegram_chat_id=100)
    ctrl._ledger.set_task_status(blocker.id, TaskStatus.RUNNING)

    response = await ctrl.handle_conversation_text(
        "继续查收敛",
        {"chat_id": 100, "user_id": 7},
    )

    # Must report workspace busy, not silently consume.
    assert "当前工作区正在执行" in response.text
    # Prepared carryover must NOT be consumed.
    prepared = ctrl._ledger.get_latest_prepared_carryover(100)
    assert prepared is not None
    assert prepared.status == "prepared"
    # Current active workbench must NOT be archived (source is available since
    # we didn't create an active workbench before this test).
    source_check = ctrl._ledger.get_conversation(source.id)
    assert source_check is not None


@pytest.mark.asyncio
async def test_prepared_carryover_workspace_busy_uses_standard_choice_card(
    ctrl: CommandController,
) -> None:
    source = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Carry Source",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(source.id, "历史背景。")
    ctrl._ledger.archive_conversation(source.id)
    running = ctrl._ledger.create_conversation(
        chat_id=100,
        user_id=7,
        title="Running Workbench",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    blocker = ctrl._service.reserve_task(
        "wlcodex", "正在执行的任务", telegram_chat_id=100,
    )
    ctrl._ledger.set_conversation_active_task(running.id, blocker.id)
    ctrl._ledger.set_task_status(blocker.id, TaskStatus.RUNNING)
    await ctrl.handle(f"/carry {source.id}", {"chat_id": 100, "user_id": 7})

    response = await ctrl.handle_conversation_text(
        "接棒后继续查部署生效",
        {"chat_id": 100, "user_id": 7},
    )

    assert "当前工作区正在执行" in response.text
    labels = {button["text"] for row in response.buttons for button in row}
    assert "发给当前 现场" in labels
    assert "打断并执行这句" in labels
    assert "排队稍后" in labels
    assert "新开隔离现场" in labels
    assert "先不处理" in labels
    callback_ids = {
        button["callback_data"].split(":", 1)[1]
        for row in response.buttons
        for button in row
    }
    assert callback_ids == {str(running.id)}
    prepared = ctrl._ledger.get_latest_prepared_carryover(100)
    assert prepared is not None
    assert prepared.status == "prepared"


@pytest.mark.asyncio
async def test_carry_keyword_searches_agent_run_and_orch_run_summaries(ctrl: CommandController) -> None:
    """Keyword search must match text in agent_run completion_summary and
    orchestration_run fields, not just conversation title/summary."""
    convo = ctrl._ledger.create_conversation(
        chat_id=100, user_id=7,
        title="普通工作台",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(convo.id, "日常维护。")
    ctrl._ledger.archive_conversation(convo.id)
    # Agent run with keyword in completion_summary.
    run = ctrl._ledger.create_agent_run(
        conversation_id=convo.id,
        agent="codex", role="auto_analysis",
        prompt_packet_summary="分析 reduce-only 问题",
    )
    ctrl._ledger.update_agent_run_status(
        run.id, "done", completion_summary="reduce-only 400 错误仍未解决。",
    )

    response = await ctrl.handle("/carry reduce-only", {"chat_id": 100, "user_id": 7})

    # Must find the conversation even though title/summary don't contain "reduce-only".
    assert "普通工作台" in response.text


@pytest.mark.asyncio
async def test_carry_keyword_searches_orch_run_fields(ctrl: CommandController) -> None:
    """Keyword in orchestration_run last_codex_analysis must match."""
    convo = ctrl._ledger.create_conversation(
        chat_id=100, user_id=7,
        title="某次部署",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    ctrl._ledger.update_conversation_summary(convo.id, "部署后检查。")
    ctrl._ledger.archive_conversation(convo.id)
    orch = ctrl._ledger.create_orchestration_run(convo.id, "检查状态收敛")
    ctrl._ledger.update_orchestration_run(
        orch.id,
        status="needs_user",
        current_step="draft_ready",
        last_codex_analysis="诊断结论：ALTUSDT 状态收敛异常，本地与交易所不一致。",
    )

    response = await ctrl.handle("/carry ALTUSDT", {"chat_id": 100, "user_id": 7})

    assert "某次部署" in response.text
