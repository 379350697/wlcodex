import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
)
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.models import HandoffPacket
from wlcodex.relay.service import (
    RelayService,
    _plain_followup_visible_text,
    _provider_approval_confirmation_kind,
)
from wlcodex.relay.store import RelayStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


def _strict_json_envelope(
    *,
    role: str = "director",
    artifact_type: str = "routing_decision",
    handoff_to: str = "",
    summary: str = "route accepted",
) -> str:
    payload: dict[str, Any] = {
        "status": "passed",
        "reason": "valid strict json",
        "role": role,
        "artifact_type": artifact_type,
        "handoff_to": handoff_to,
        "summary": summary,
        "evidence_refs": ["tests/test_relay_service.py"],
        "open_questions": [],
        "next_action": "continue",
    }
    if artifact_type == "routing_decision":
        payload.update(
            {
                "complexity": "medium",
                "risk": "medium",
                "route": "core_relay",
                "required_roles": ["director", "implementer", "auditor"],
                "acceptance_criteria": ["strict JSON is accepted"],
                "stop_conditions": ["stop on protocol error"],
                "requires_user_approval": False,
            }
        )
    return json.dumps(payload, ensure_ascii=False)


class FakeProvider:
    provider = "claude"
    provider_engine = "sdk-test"

    def __init__(self, *, can_start: bool = True) -> None:
        self.can_start = can_start
        self.calls: list[tuple[Any, ...]] = []

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_start_session=self.can_start,
            can_continue_session=True,
        )

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        index = len([call for call in self.calls if call[0] == "start_session"])
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"native-{index}",
            agent_run_id=100 + index,
            status="started",
        )

    async def interrupt_session(self, native_session_id: str, turn_id: str = ""):
        self.calls.append(("interrupt_session", native_session_id, turn_id))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=101,
            status="interrupted",
        )

    async def sync_session(self, native_session_id: str):
        self.calls.append(("sync_session", native_session_id))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=101,
            status="synced",
        )

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs: Any):
        self.calls.append(("continue_session", native_session_id, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=201,
            status="continued",
        )


def test_provider_approval_confirmation_kind_normalizes_adapter_variants() -> None:
    assert _provider_approval_confirmation_kind("command") == "command_approval"
    assert _provider_approval_confirmation_kind("file-change") == "file_change_approval"
    assert _provider_approval_confirmation_kind("file_change") == "file_change_approval"
    assert _provider_approval_confirmation_kind("permission") == "permission_approval"
    assert _provider_approval_confirmation_kind("permissions") == "permission_approval"
    assert _provider_approval_confirmation_kind("plan_choice") == "plan_choice"


class FakeCodexProvider(FakeProvider):
    provider = "codex"
    provider_engine = "app-server"

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_start_session=self.can_start,
            can_continue_session=True,
            can_resolve_approval=True,
        )

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        index = len([call for call in self.calls if call[0] == "start_session"])
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"native-codex-{index}",
            agent_run_id=700 + index,
            turn_id=f"turn-codex-{index}",
            active_turn_id=f"turn-codex-{index}",
            turn_running=True,
            status="started",
        )

    async def resolve_approval(self, request_id: str, body: dict[str, Any]):
        self.calls.append(("resolve_approval", request_id, body))
        return {"request_id": request_id, "status": "resolved"}


class FakeAntigravityProvider(FakeProvider):
    provider = "antigravity"
    provider_engine = "cli-local"


class ActiveTurnProvider(FakeProvider):
    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_start_session=True,
            can_continue_session=True,
            can_steer_active_turn=True,
        )

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="native-active",
            agent_run_id=301,
            turn_id="turn-1",
            active_turn_id="turn-1",
            turn_running=True,
            status="started",
        )

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ):
        self.calls.append(("steer_session", native_session_id, expected_turn_id, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=302,
            turn_id=expected_turn_id,
            active_turn_id=expected_turn_id,
            turn_running=True,
            status="steered",
        )


class FailingContinueProvider(FakeProvider):
    async def continue_session(self, native_session_id: str, prompt: str, **kwargs: Any):
        self.calls.append(("continue_session", native_session_id, prompt, kwargs))
        raise RuntimeError("continue transport failed")


class UnverifiedStartProvider(FakeProvider):
    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="",
            agent_run_id=0,
            status="failed",
        )


def _service(tmp_path) -> tuple[RelayService, FakeProvider]:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = FakeProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    return service, provider


def _active_turn_service(tmp_path) -> tuple[RelayService, ActiveTurnProvider]:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = ActiveTurnProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    return service, provider


def test_create_task_records_board_and_queues_director(tmp_path) -> None:
    service, _provider = _service(tmp_path)

    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )

    detail = service.get_task(task.id)
    assert detail.task.id == task.id
    assert detail.role_jobs[0].role == "director"
    assert detail.role_jobs[0].status == "queued"
    assert [event.event_type for event in service.events_for_task(task.id)] == [
        "task.created",
        "role.queued",
        "artifact.created",
    ]


def test_codex_plan_first_architect_dispatches_provider_plan_mode(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = FakeCodexProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="codex",
    )
    task = service.create_task(
        title="Relay",
        prompt="Plan it",
        workspace="/repo",
        provider="codex",
        execution_mode="plan_first",
    )

    asyncio.run(service.dispatch_role(task.id, "architect"))

    assert provider.calls[-1][0] == "start_session"
    assert provider.calls[-1][3]["collaboration_mode"] == {"mode": "plan"}


def test_claude_plan_first_architect_dispatches_permission_plan_mode(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Plan it",
        workspace="/repo",
        provider="claude",
        execution_mode="plan_first",
    )

    asyncio.run(service.dispatch_role(task.id, "architect"))

    assert provider.calls[-1][0] == "start_session"
    assert provider.calls[-1][3]["permission_mode"] == "plan"


def test_antigravity_plan_first_uses_prompt_fallback_without_unknown_flags(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = FakeAntigravityProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="antigravity",
    )
    task = service.create_task(
        title="Relay",
        prompt="Plan it",
        workspace="/repo",
        provider="antigravity",
        execution_mode="plan_first",
    )

    asyncio.run(service.dispatch_role(task.id, "architect"))

    kwargs = provider.calls[-1][3]
    assert "permission_mode" not in kwargs
    assert "collaboration_mode" not in kwargs
    metadata = [
        artifact
        for artifact in service.get_task(task.id).artifacts
        if artifact.get("artifact_type") == "role_dispatch_metadata"
        and artifact.get("relay_role") == "architect"
    ][-1]
    assert metadata["provider_mode"]["provider_mode"] == "prompt_plan_fallback"
    attempt = service._store.lifecycle.latest_attempt(task.id, 1, "architect")
    assert attempt.execution_mode == "plan_first"
    assert attempt.team_strategy == "none"
    assert attempt.provider_mode["provider_mode"] == "prompt_plan_fallback"


def test_legacy_team_mode_normalizes_to_auto_with_subagents_allowed(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Use helpers if useful",
        workspace="/repo",
        provider="claude",
        execution_mode="team",
    )

    execution = service._store.lifecycle.round_execution(task.id, 1)

    assert execution["execution_mode"] == "auto"
    assert execution["execution_strategy"] == {
        "allow_subagents": "auto",
        "subagent_decision_json": {},
    }


def test_legacy_team_strategy_allows_subagents_without_preserving_manual_strategy(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Review if useful",
        workspace="/repo",
        provider="claude",
        execution_mode="goal",
        team_strategy="code_review",
    )

    execution = service._store.lifecycle.round_execution(task.id, 1)

    assert execution["execution_mode"] == "goal"
    assert execution["execution_strategy"] == {
        "allow_subagents": "auto",
        "subagent_decision_json": {},
    }
    assert "team_strategy" not in execution["execution_strategy"]


def test_plan_first_with_subagents_keeps_provider_plan_mapping(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Plan it",
        workspace="/repo",
        provider="claude",
        execution_mode="plan_first",
        allow_subagents="auto",
    )

    asyncio.run(service.dispatch_role(task.id, "architect"))

    assert provider.calls[-1][0] == "start_session"
    assert provider.calls[-1][3]["permission_mode"] == "plan"
    metadata = [
        artifact
        for artifact in service.get_task(task.id).artifacts
        if artifact.get("artifact_type") == "role_dispatch_metadata"
        and artifact.get("relay_role") == "architect"
    ][-1]
    assert metadata["provider_mode"]["provider_mode"] == "claude_plan"
    assert metadata["provider_mode"]["allow_subagents"] == "auto"
    assert metadata["provider_mode"]["subagent_decision_json"]["provider"] == "claude"
    assert metadata["provider_mode"]["subagent_decision_json"]["capability"] == "builtin_subagents"
    execution = service._store.lifecycle.round_execution(task.id, 1)
    assert execution["execution_strategy"]["allow_subagents"] == "auto"
    assert execution["execution_strategy"]["subagent_decision_json"]["provider"] == "claude"
    assert (
        execution["execution_strategy"]["subagent_decision_json"]["capability"]
        == "builtin_subagents"
    )
    assert service._store.lifecycle.latest_attempt(task.id, 1, "architect").team_strategy == "none"


def test_subagents_do_not_use_provider_team_topology_mode(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Use helpers if useful",
        workspace="/repo",
        provider="claude",
        execution_mode="team",
    )

    asyncio.run(service.dispatch_role(task.id, "director"))

    metadata = [
        artifact
        for artifact in service.get_task(task.id).artifacts
        if artifact.get("artifact_type") == "role_dispatch_metadata"
        and artifact.get("relay_role") == "director"
    ][-1]
    assert metadata["provider_mode"]["execution_mode"] == "auto"
    assert metadata["provider_mode"]["allow_subagents"] == "auto"
    assert metadata["provider_mode"]["provider_mode"] != "provider_team_topology"
    assert service._store.lifecycle.latest_attempt(task.id, 1, "director").team_strategy == "none"


def test_subagent_decision_is_scoped_to_each_role_provider(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    claude = FakeProvider()
    codex = FakeCodexProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([claude, codex]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Relay",
        prompt="Plan with mixed providers",
        workspace="/repo",
        provider="claude",
        role_providers={
            "director": "claude",
            "architect": "codex",
            "implementer": "claude",
            "tester": "claude",
            "auditor": "claude",
        },
        execution_mode="plan_first",
        allow_subagents="auto",
    )

    asyncio.run(service.dispatch_role(task.id, "director"))
    asyncio.run(service.dispatch_role(task.id, "architect"))

    artifacts = [
        artifact
        for artifact in service.get_task(task.id).artifacts
        if artifact.get("artifact_type") == "role_dispatch_metadata"
    ]
    director_metadata = next(
        artifact for artifact in artifacts if artifact.get("relay_role") == "director"
    )
    architect_metadata = next(
        artifact for artifact in artifacts if artifact.get("relay_role") == "architect"
    )
    assert (
        director_metadata["provider_mode"]["subagent_decision_json"]["capability"]
        == "builtin_subagents"
    )
    assert (
        architect_metadata["provider_mode"]["subagent_decision_json"]["capability"]
        == "explicit_subagents"
    )
    execution = service._store.lifecycle.round_execution(task.id, 1)
    assert execution["execution_strategy"]["subagent_decision_json"]["provider"] == "codex"


@pytest.mark.asyncio
async def test_running_user_input_defaults_to_pending_next_round(tmp_path) -> None:
    service, provider = _active_turn_service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    assert service.get_task(task.id).current_round_id == 1

    pending = service.queue_user_input(task.id, "do this after the current turn")

    assert pending.status == "pending"
    assert pending.queued_after_round_id == 1
    assert service.get_task(task.id).current_round_id == 1
    assert [call[0] for call in provider.calls] == ["start_session"]


@pytest.mark.asyncio
async def test_pending_input_can_be_steered_into_active_attempt(tmp_path) -> None:
    service, provider = _active_turn_service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    pending = service.queue_user_input(task.id, "use this constraint now")

    steered = await service.steer_active_attempt(task.id, pending.id)

    assert steered.status == "steered"
    assert steered.steered_round_id == 1
    assert steered.steered_role == "director"
    assert service.get_task(task.id).current_round_id == 1
    assert [call[0] for call in provider.calls] == ["start_session", "steer_session"]
    assert provider.calls[-1][2] == "turn-1"
    event = service.events_for_task(task.id)[-1]
    assert event.event_type == "user.input_steered"
    assert event.payload["text"] == "use this constraint now"
    assert event.payload["steered_round_id"] == 1
    assert event.payload["steered_role"] == "director"
    assert event.payload["guidance_artifact_id"]


@pytest.mark.asyncio
async def test_terminal_round_consumes_pending_input_into_next_round(tmp_path) -> None:
    service, provider = _active_turn_service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    pending = service.queue_user_input(task.id, "start this next")
    service._store.update_task_status(task.id, "completed")

    consumed = await service.consume_pending_after_round(task.id, 1, dispatch_next=False)

    detail = service.get_task(task.id)
    assert consumed is not None
    assert consumed.id == pending.id
    assert consumed.status == "consumed"
    assert consumed.consumed_round_id == 2
    assert detail.current_round_id == 2
    assert detail.task.status == "running"
    assert next(job for job in detail.role_jobs if job.role == "director").status == "queued"
    assert [call[0] for call in provider.calls] == ["start_session"]


@pytest.mark.asyncio
async def test_final_summary_auto_consumes_pending_input_after_terminal_round(tmp_path) -> None:
    service, provider = _active_turn_service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "passed",
                "reason": "director only",
                "role": "director",
                "artifact_type": "routing_decision",
                "handoff_to": "",
                "summary": "Director can answer directly.",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "finish",
                "complexity": "low",
                "risk": "low",
                "route": "director_only",
                "required_roles": ["director"],
                "acceptance_criteria": ["final summary"],
                "stop_conditions": [],
                "requires_user_approval": False,
            }
        ),
        dispatch_next=False,
    )
    pending = service.queue_user_input(task.id, "run this after the summary")

    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "passed",
                "reason": "done",
                "role": "director",
                "artifact_type": "final_summary",
                "handoff_to": "",
                "summary": "First round complete.",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "done",
            }
        ),
        dispatch_next=False,
    )

    detail = service.get_task(task.id)
    consumed = service._store.get_pending_input(task.id, pending.id)
    assert consumed is not None
    assert consumed.status == "consumed"
    assert consumed.consumed_round_id == 2
    assert detail.current_round_id == 2
    assert detail.task.status == "running"
    assert next(job for job in detail.role_jobs if job.role == "director").status == "queued"
    assert [call[0] for call in provider.calls] == []


@pytest.mark.asyncio
async def test_plan_approval_continues_current_round(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Plan then execute",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "passed",
                "reason": "plan first",
                "role": "director",
                "artifact_type": "routing_decision",
                "handoff_to": "",
                "summary": "Architect should plan.",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "plan",
                "complexity": "medium",
                "risk": "medium",
                "route": "core_relay",
                "required_roles": ["director", "architect", "implementer"],
                "acceptance_criteria": ["approved plan", "implemented"],
                "stop_conditions": [],
                "requires_user_approval": False,
            }
        ),
        dispatch_next=False,
    )
    await service.handle_role_output(
        task.id,
        "architect",
        json.dumps(
            {
                "status": "waiting",
                "reason": "needs approval",
                "role": "architect",
                "artifact_type": "architecture_plan",
                "handoff_to": "",
                "summary": "Plan A",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "approve plan",
            }
        ),
        dispatch_next=False,
    )
    waiting_detail = service.get_task(task.id)
    waiting_execution = service._store.lifecycle.round_execution(task.id, 1)
    assert waiting_detail.task.status == "waiting_user"
    assert waiting_execution["waiting_reason"] == "plan_approval"
    plan_artifact = next(
        artifact
        for artifact in reversed(waiting_detail.artifacts)
        if artifact["artifact_type"] == "architecture_plan"
    )

    result = await service.apply_round_control(
        task.id,
        waiting_detail.current_round_id,
        decision="approve_plan",
        artifact_id=int(plan_artifact["id"]),
        dispatch_next=False,
    )

    detail = service.get_task(task.id)
    assert result["round_id"] == 1
    assert detail.current_round_id == 1
    assert detail.task.status == "running"
    assert service._store.lifecycle.round_execution(task.id, 1)["waiting_reason"] == "none"
    assert next(job for job in detail.role_jobs if job.role == "architect").status == "passed"
    assert next(job for job in detail.role_jobs if job.role == "implementer").status == "queued"


@pytest.mark.asyncio
async def test_plan_approval_rejects_plan_artifact_from_old_round(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Plan then supersede",
        workspace="/repo",
        provider="claude",
    )
    old_plan = service._store.save_artifact(
        task.id,
        "architect",
        "architecture_plan",
        {
            "role": "architect",
            "relay_role": "architect",
            "status": "waiting",
            "artifact_type": "architecture_plan",
            "round_id": 1,
            "summary": "Old plan",
        },
        summary="Old plan",
    )
    service.queue_user_input(task.id, "new boundary")
    await service.consume_pending_after_round(task.id, 1, dispatch_next=False)
    current_round = service.get_task(task.id).current_round_id

    with pytest.raises(ValueError, match="current round"):
        await service.apply_round_control(
            task.id,
            current_round,
            decision="approve_plan",
            artifact_id=old_plan.id,
            dispatch_next=False,
        )

    assert service.get_task(task.id).current_round_id == current_round


def test_passed_role_does_not_surface_stale_role_error(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Recovered director",
        prompt="Run full relay",
        workspace="/repo",
        provider="claude",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "error": "invalid json: Expecting value",
        },
        summary="invalid json: Expecting value",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "artifact_type": "routing_decision",
            "status": "passed",
            "role": "director",
            "route": "full_relay",
            "required_roles": ["director", "architect"],
            "summary": "恢复为完整接力。",
        },
        summary="恢复为完整接力。",
    )
    service._store.update_role_status(task.id, "director", "passed")

    detail = service.get_task(task.id)
    director = next(job for job in detail.role_jobs if job.role == "director")

    assert director.status == "passed"
    assert director.error_message == ""


def test_dispatch_role_uses_native_agent_registry_provider(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(service.dispatch_role(task.id, "director"))

    assert provider.calls[0][0] == "start_session"
    detail = service.get_task(task.id)
    director = next(job for job in detail.role_jobs if job.role == "director")
    assert director.native_session_id == "native-1"
    assert director.provider == "claude"
    assert director.status == "streaming"


def test_dispatch_role_continues_existing_role_native_session(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="claude",
        provider_engine="sdk-test",
        native_session_id="native-implementer-old",
        agent_run_id=155,
        turn_id="turn-old",
        active_turn_id="turn-old",
        turn_running=False,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "implementer", "queued")

    asyncio.run(service.dispatch_role(task.id, "implementer"))

    assert provider.calls[0][0] == "continue_session"
    assert provider.calls[0][1] == "native-implementer-old"
    assert all(call[0] != "start_session" for call in provider.calls)
    detail = service.get_task(task.id)
    implementer = next(job for job in detail.role_jobs if job.role == "implementer")
    assert implementer.native_session_id == "native-implementer-old"
    assert implementer.agent_run_id == 201
    assert implementer.status == "streaming"


def test_resume_role_redispatches_blocked_role(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_task_status(task.id, "blocked")
    service._store.update_role_status(task.id, "implementer", "blocked")
    service._store.save_artifact(
        task.id,
        "implementer",
        "role_error",
        {"error": "temporary provider stopped"},
        summary="temporary provider stopped",
    )

    asyncio.run(service.resume_role(task.id, "implementer"))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["implementer"].status == "streaming"
    assert jobs["implementer"].provider == "claude"
    assert jobs["implementer"].native_session_id == "native-1"
    assert jobs["implementer"].error_message == ""
    assert provider.calls[0][0] == "start_session"
    assert [event.event_type for event in service.events_for_task(task.id)][-3:] == [
        "role.queued",
        "role.streaming",
        "dispatch.verified",
    ]


def test_resume_role_force_redispatches_streaming_role(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "implementer"))

    asyncio.run(service.resume_role(task.id, "implementer", force=True))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["implementer"].status == "streaming"
    assert jobs["implementer"].native_session_id == "native-1"
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
    ]


def test_create_task_snapshots_role_providers_and_dispatches_each_role_provider(
    tmp_path,
) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    codex = FakeCodexProvider()
    claude = FakeProvider()
    antigravity = FakeAntigravityProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([codex, claude, antigravity]),
        default_provider="codex",
    )
    service.save_config(
        {
            "director": "codex",
            "architect": "claude",
            "implementer": "antigravity",
            "tester": "claude",
            "auditor": "codex",
        }
    )

    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="codex",
    )

    detail = service.get_task(task.id)
    assert detail.task.role_providers == {
        "director": "codex",
        "architect": "claude",
        "implementer": "antigravity",
        "tester": "claude",
        "auditor": "codex",
    }
    assert {job.role: job.provider for job in detail.role_jobs} == detail.task.role_providers

    asyncio.run(service.dispatch_role(task.id, "implementer"))

    assert antigravity.calls[0][0] == "start_session"
    assert not codex.calls
    assert not claude.calls
    implementer = next(
        job for job in service.get_task(task.id).role_jobs if job.role == "implementer"
    )
    assert implementer.provider == "antigravity"
    assert implementer.native_session_id == "native-1"


def test_config_exposes_explicitly_configured_roles_without_autofill(
    tmp_path,
) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([FakeCodexProvider(), FakeProvider()]),
        default_provider="codex",
        configured_roles=("architect", "implementer", "investigator"),
    )

    config = service.config()

    assert [role["role"] for role in config["configured_roles"]] == [
        "architect",
        "implementer",
    ]
    assert [role["role"] for role in config["roles"]] == [
        "director",
        "architect",
        "implementer",
        "tester",
        "auditor",
    ]


def test_create_task_rejects_unknown_role_provider_assignment(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([FakeCodexProvider()]),
        default_provider="codex",
    )

    with pytest.raises(ValueError, match="unknown relay role: investigator"):
        service.create_task(
            title="Relay",
            prompt="Build it",
            workspace="/repo",
            provider="codex",
            role_providers={
                "director": "codex",
                "architect": "codex",
                "implementer": "codex",
                "tester": "codex",
                "auditor": "codex",
                "investigator": "codex",
            },
        )


def test_dispatch_role_blocks_task_when_provider_cannot_start(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = FakeProvider(can_start=False)
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(service.dispatch_role(task.id, "director"))

    detail = service.get_task(task.id)
    director = next(job for job in detail.role_jobs if job.role == "director")
    assert detail.task.status == "blocked"
    assert director.status == "blocked"
    assert director.fallback_reason == "provider cannot start native sessions"


def test_invalid_envelope_blocks_advancement(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )

    result = asyncio.run(service.handle_role_output(task.id, "implementer", '{"status": "passed"}'))

    detail = service.get_task(task.id)
    tester = next(job for job in detail.role_jobs if job.role == "tester")
    implementer = next(job for job in detail.role_jobs if job.role == "implementer")
    assert result.ok is False
    assert detail.task.status == "blocked"
    assert tester.status == "idle"
    assert implementer.status == "blocked"
    assert implementer.error_message.startswith("missing required fields")
    assert any(
        artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "implementer"
        for artifact in detail.artifacts
    )


def test_invalid_streamed_envelope_retries_format_once(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="请回答今日天气。",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    result = asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            '{"artifact_type":"routing_decisioncomplexitylow"}',
        )
    )

    detail = service.get_task(task.id)
    director = next(job for job in detail.role_jobs if job.role == "director")
    assert result.ok is False
    assert detail.task.status == "running"
    assert director.status == "streaming"
    assert provider.calls[-1][0] == "continue_session"
    assert "只重新输出一个合法 JSON object" in provider.calls[-1][2]
    assert '"artifact_type": "routing_decision"' in provider.calls[-1][2]
    assert any(
        artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "director"
        and artifact.get("retry_kind") == "format"
        for artifact in detail.artifacts
    )
    assert any(
        artifact.get("artifact_type") == "role_artifact_invalid"
        and artifact.get("relay_role") == "director"
        and str(artifact.get("error") or "").startswith("missing required fields")
        for artifact in detail.artifacts
    )


def test_malformed_director_routing_blocks_explicit_full_relay_after_retry(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt=(
            "请按完整五角色接力流程审查，不要修改任何文件，不要提交，不要部署。"
            "总工程师、架构工程师、开发工程师、测试工程师、审核工程师都要参与。"
        ),
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {"retry_kind": "format", "error": "invalid json"},
        summary="format retry already attempted",
    )

    result = asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            (
                '{"artifact_type":"routing_decisioncomplexityhighevidence_refs":[]'
                'routefullstatuspassedrequired_rolesdirector"}'
            ),
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    role_errors = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "director"
    ]
    assert result.ok is False
    assert detail.task.status == "blocked"
    assert detail.routing_decision is None
    assert jobs["director"].status == "blocked"
    assert jobs["architect"].status == "idle"
    assert [call[0] for call in provider.calls] == ["start_session"]
    assert all("recovered_as" not in artifact for artifact in role_errors)


def test_successful_role_envelopes_dispatch_next_roles(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation and testing required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "tester", "auditor"],
              "acceptance_criteria": ["implemented", "tested", "audited"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "tester",
              "summary": "Implementation ready",
              "evidence_refs": ["x"],
              "open_questions": [],
              "next_action": "test"
            }
            """,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            """
            {
              "status": "passed",
              "reason": "tested",
              "role": "tester",
              "artifact_type": "test_report",
              "handoff_to": "auditor",
              "summary": "Tests ready",
              "evidence_refs": ["x"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
        )
    )

    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["tester"].status == "passed"
    assert jobs["auditor"].status == "streaming"
    start_calls = [call for call in provider.calls if call[0] == "start_session"]
    assert len(start_calls) == 2
    assert "role: tester" in start_calls[0][2]
    assert "role: auditor" in start_calls[1][2]


def test_core_relay_code_development_requires_auditor_after_implementer(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer"],
              "acceptance_criteria": ["implemented", "reviewed"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    assert detail.routing_decision is not None
    assert detail.routing_decision["required_roles"] == [
        "director",
        "implementer",
        "auditor",
    ]

    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "",
              "summary": "Implementation ready",
              "evidence_refs": ["x"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
        )
    )

    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["implementer"].status == "passed"
    assert jobs["auditor"].status == "streaming"
    assert provider.calls[-1][0] == "start_session"
    assert "role: auditor" in provider.calls[-1][2]


def test_auditor_failed_review_returns_to_implementer_for_rework(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="实现一个 UI 修复，并由审核工程师复核。",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation and review required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer"],
              "acceptance_criteria": ["implemented", "reviewed"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "",
              "summary": "Implementation ready",
              "evidence_refs": ["x"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
            dispatch_next=False,
        )
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "auditor",
            """
            {
              "status": "failed",
              "reason": "review found a regression",
              "role": "auditor",
              "artifact_type": "audit_report",
              "handoff_to": "implementer",
              "summary": "输入框遮挡最后一条消息，需要回炉。",
              "evidence_refs": ["screenshot"],
              "open_questions": [],
              "next_action": "rework"
            }
            """,
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["auditor"].status == "failed"
    assert jobs["implementer"].status == "queued"

    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "reworked",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "",
              "summary": "遮挡问题已修复",
              "evidence_refs": ["test"],
              "open_questions": [],
              "next_action": "audit again"
            }
            """,
            dispatch_next=False,
        )
    )

    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["implementer"].status == "passed"
    assert jobs["auditor"].status == "queued"


def test_role_cannot_handoff_to_role_outside_routing_decision(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="实现并测试，不需要审计",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation and testing only",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay without audit.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "tester"],
              "acceptance_criteria": ["implemented", "tested"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "auditor",
              "summary": "Implementation ready",
              "evidence_refs": ["x"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["implementer"].status == "blocked"
    assert jobs["implementer"].error_message == (
        "handoff_to 审核工程师 before required role 测试工程师 completed"
    )
    assert jobs["auditor"].status == "idle"


def test_last_required_role_returns_to_director_instead_of_default_unrequired_role(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="实现并测试，不需要审计",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation and testing only",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay without audit.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "tester"],
              "acceptance_criteria": ["implemented", "tested"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    service._store.update_role_status(task.id, "implementer", "passed")

    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            """
            {
              "status": "passed",
              "reason": "tested",
              "role": "tester",
              "artifact_type": "test_report",
              "handoff_to": "",
              "summary": "Tests ready",
              "evidence_refs": ["x"],
              "open_questions": [],
              "next_action": "director summary"
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert jobs["auditor"].status == "streaming"
    assert jobs["director"].status == "passed"
    assert provider.calls[-1][0] == "start_session"
    assert "role: auditor" in provider.calls[-1][2]


def test_dispatch_role_includes_role_targeted_handoff_even_when_not_latest(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="architect",
        to_role="implementer",
        packet=HandoffPacket(
            from_role="architect",
            to_role="implementer",
            summary="Implement this older architecture handoff",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="implement",
        ),
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="tester",
        to_role="auditor",
        packet=HandoffPacket(
            from_role="tester",
            to_role="auditor",
            summary="Latest but auditor-only handoff",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="audit",
        ),
    )

    asyncio.run(service.dispatch_role(task.id, "implementer"))

    prompt = provider.calls[-1][2]
    assert "Implement this older architecture handoff" in prompt
    assert "Latest but auditor-only handoff" not in prompt


def test_user_followup_routes_to_director_only(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(service.add_user_message(task.id, "Latest user instruction"))

    call_names = [call[0] for call in provider.calls]
    assert call_names == ["start_session", "continue_session"]
    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["director"].status == "streaming"
    assert jobs["architect"].status == "idle"
    assert "Latest user instruction" in provider.calls[1][2]
    assert "Build it" in provider.calls[1][2]


def test_user_followup_persists_and_forwards_attachments(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(
        service.add_user_message(
            task.id,
            "看附件处理",
            images=[
                {
                    "filename": "screen.png",
                    "mime_type": "image/png",
                    "url": "data:image/png;base64,aGVsbG8=",
                }
            ],
            files=[
                {
                    "filename": "trace.log",
                    "mime_type": "text/plain",
                    "text": "first line\nsecond line",
                    "size": 22,
                }
            ],
        )
    )

    assert provider.calls[1][0] == "continue_session"
    assert "看附件处理" in provider.calls[1][2]
    assert "trace.log" in provider.calls[1][2]
    assert "first line" in provider.calls[1][2]
    assert provider.calls[1][3]["images"][0]["filename"] == "screen.png"
    detail = service.get_task(task.id)
    followup = next(
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "user_followup"
    )
    assert followup["images"][0]["filename"] == "screen.png"
    assert followup["files"][0]["filename"] == "trace.log"


def test_user_followup_moves_waiting_task_back_to_running(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "waiting",
              "reason": "needs user input",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Need clarification",
              "evidence_refs": [],
              "open_questions": ["Which path should we take?"],
              "next_action": "wait for user",
              "complexity": "standard",
              "risk": "medium",
              "route": "waiting_user",
              "required_roles": ["director"],
              "acceptance_criteria": ["clarify route"],
              "stop_conditions": [],
              "requires_user_approval": true
            }
            """,
        )
    )
    assert service.get_task(task.id).task.status == "waiting_user"

    asyncio.run(service.add_user_message(task.id, "Take the safer path"))

    detail = service.get_task(task.id)
    assert detail.task.status == "running"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "streaming"


def test_round_control_continue_resumes_waiting_role_in_same_round(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "waiting",
              "reason": "needs user input",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Need clarification",
              "evidence_refs": [],
              "open_questions": ["Which path should we take?"],
              "next_action": "wait for user",
              "complexity": "standard",
              "risk": "medium",
              "route": "waiting_user",
              "required_roles": ["director"],
              "acceptance_criteria": ["clarify route"],
              "stop_conditions": [],
              "requires_user_approval": true
            }
            """,
            dispatch_next=False,
        )
    )

    result = asyncio.run(
        service.apply_round_control(
            task.id,
            1,
            decision="continue",
        )
    )

    detail = service.get_task(task.id)
    assert result["role"] == "director"
    assert result["round_id"] == 1
    assert detail.current_round_id == 1
    assert detail.task.status == "running"
    assert service._store.lifecycle.round_execution(task.id, 1)["waiting_reason"] == "none"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "streaming"
    assert provider.calls[-1][0] == "start_session"


def test_round_control_revise_records_comment_for_current_waiting_round(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "waiting",
              "reason": "needs user input",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Need clarification",
              "evidence_refs": [],
              "open_questions": ["Which path should we take?"],
              "next_action": "wait for user",
              "complexity": "standard",
              "risk": "medium",
              "route": "waiting_user",
              "required_roles": ["director"],
              "acceptance_criteria": ["clarify route"],
              "stop_conditions": [],
              "requires_user_approval": true
            }
            """,
            dispatch_next=False,
        )
    )

    asyncio.run(
        service.apply_round_control(
            task.id,
            1,
            decision="revise_plan",
            comment="我不同意默认选项，先只生成 HTML。",
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    assert detail.current_round_id == 1
    assert detail.task.status == "running"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "queued"
    followup = next(
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "user_followup"
    )
    assert followup["round_id"] == 1
    assert followup["input_disposition"] == "current_waiting_round"
    assert "先只生成 HTML" in followup["text"]


def test_waiting_envelope_persists_confirmation_options(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Waiting options",
        prompt="Need a style decision",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            json.dumps(
                {
                    "status": "waiting",
                    "reason": "needs user direction",
                    "role": "director",
                    "artifact_type": "routing_decision",
                    "handoff_to": "",
                    "summary": "Need UI style direction.",
                    "evidence_refs": [],
                    "open_questions": ["Which visual style should be used?"],
                    "confirmation_options": [
                        {
                            "id": "minimal",
                            "label": "简约风格",
                            "summary": "更接近原生 Codex。",
                            "instruction": "采用简约、克制、手机原生风格。",
                        },
                        {
                            "id": "cyber",
                            "label": "赛博风格",
                            "summary": "更强视觉冲击。",
                            "instruction": "采用赛博风格，但仍保持可读。",
                        },
                    ],
                    "next_action": "wait for user choice",
                    "complexity": "standard",
                    "risk": "medium",
                    "route": "waiting_user",
                    "required_roles": ["director"],
                    "acceptance_criteria": ["style confirmed"],
                    "stop_conditions": [],
                    "requires_user_approval": True,
                }
            ),
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    routing = next(
        artifact
        for artifact in reversed(detail.artifacts)
        if artifact.get("artifact_type") == "routing_decision"
    )
    assert detail.task.status == "waiting_user"
    assert routing["confirmation_source"] == "relay_prompt_fallback"
    assert routing["confirmation_kind"] == "relay_question"
    assert routing["confirmation_options"] == [
        {
            "id": "minimal",
            "label": "简约风格",
            "summary": "更接近原生 Codex。",
            "instruction": "采用简约、克制、手机原生风格。",
        },
        {
            "id": "cyber",
            "label": "赛博风格",
            "summary": "更强视觉冲击。",
            "instruction": "采用赛博风格，但仍保持可读。",
        },
    ]


def test_round_execution_records_fallback_confirmation_provenance(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Fallback waiting",
        prompt="Need a style decision",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            json.dumps(
                {
                    "status": "waiting",
                    "reason": "needs user direction",
                    "role": "director",
                    "artifact_type": "routing_decision",
                    "handoff_to": "",
                    "summary": "Need UI style direction.",
                    "evidence_refs": [],
                    "open_questions": ["Which visual style should be used?"],
                    "confirmation_options": [
                        {
                            "id": "minimal",
                            "label": "简约风格",
                            "summary": "更接近原生 Codex。",
                            "instruction": "采用简约、克制、手机原生风格。",
                        }
                    ],
                    "next_action": "wait for user choice",
                    "complexity": "standard",
                    "risk": "medium",
                    "route": "waiting_user",
                    "required_roles": ["director"],
                    "acceptance_criteria": ["style confirmed"],
                    "stop_conditions": [],
                    "requires_user_approval": True,
                }
            ),
            dispatch_next=False,
        )
    )

    execution = service._store.lifecycle.round_execution(task.id, 1)
    confirmation = execution["confirmation"]
    assert confirmation["source"] == "relay_prompt_fallback"
    assert confirmation["kind"] == "relay_question"
    assert confirmation["provider_request_id"] == ""
    assert confirmation["runtime_event_id"] == 0
    assert confirmation["role"] == "director"


def test_codex_approval_runtime_event_records_native_confirmation_provenance(
    tmp_path,
) -> None:
    ledger = Ledger.open(tmp_path / "relay.sqlite3")
    ledger.migrate()
    provider = FakeCodexProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="codex",
    )
    task = service.create_task(
        title="Native approval",
        prompt="Run tests",
        workspace="/repo",
        provider="codex",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    runtime_store = RuntimeEventStore(ledger._conn)
    event = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.APPROVAL_REQUESTED,
            aggregate_type=AggregateType.APPROVAL,
            aggregate_id="req-1",
            correlation_id="approval-corr",
            source=EventSource.CODEX,
            actor="codex",
            visibility=Visibility.USER,
            payload={
                "codexRequestId": "req-1",
                "kind": "command",
                "summary": "Run: pytest",
                "turnId": "turn-codex-1",
            },
            occurred_at=now_iso(),
            agent_run_id=701,
            task_id=task.id,
        )
    )

    asyncio.run(service.handle_runtime_event(event))

    detail = service.get_task(task.id)
    execution = service._store.lifecycle.round_execution(task.id, 1)
    confirmation = execution["confirmation"]
    assert detail.task.status == "waiting_user"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "waiting"
    assert confirmation["source"] == "provider_native_approval"
    assert confirmation["kind"] == "command_approval"
    assert confirmation["provider_request_id"] == "req-1"
    assert confirmation["runtime_event_id"] == event.id
    assert confirmation["provider"] == "codex"
    assert confirmation["native_session_id"] == "native-codex-1"

    result = asyncio.run(
        service.apply_round_control(
            task.id,
            1,
            decision="continue",
            dispatch_next=False,
        )
    )

    assert result["confirmation_source"] == "provider_native_approval"
    assert provider.calls[-1] == (
        "resolve_approval",
        "req-1",
        {"action": "approve_once"},
    )
    assert not any(call[0] == "continue_session" for call in provider.calls)


def test_round_control_continue_records_selected_confirmation_option(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Waiting options",
        prompt="Need a style decision",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            json.dumps(
                {
                    "status": "waiting",
                    "reason": "needs user direction",
                    "role": "director",
                    "artifact_type": "routing_decision",
                    "handoff_to": "",
                    "summary": "Need UI style direction.",
                    "evidence_refs": [],
                    "open_questions": ["Which visual style should be used?"],
                    "confirmation_options": [
                        {
                            "id": "minimal",
                            "label": "简约风格",
                            "summary": "更接近原生 Codex。",
                            "instruction": "采用简约、克制、手机原生风格。",
                        }
                    ],
                    "next_action": "wait for user choice",
                    "complexity": "standard",
                    "risk": "medium",
                    "route": "waiting_user",
                    "required_roles": ["director"],
                    "acceptance_criteria": ["style confirmed"],
                    "stop_conditions": [],
                    "requires_user_approval": True,
                }
            ),
            dispatch_next=False,
        )
    )

    result = asyncio.run(
        service.apply_round_control(
            task.id,
            1,
            decision="continue",
            selected_option_id="minimal",
            selected_option_label="简约风格",
            selected_option_instruction="采用简约、克制、手机原生风格。",
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    followup = next(
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "user_followup"
    )
    assert result["selected_option_id"] == "minimal"
    assert followup["round_id"] == 1
    assert followup["input_disposition"] == "current_waiting_round"
    assert followup["selected_option_label"] == "简约风格"
    assert followup["text"] == "采用简约、克制、手机原生风格。"
    assert detail.current_round_id == 1
    assert detail.task.status == "running"


def test_user_followup_starts_clean_visible_turn_after_blocked_role(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "blocked")
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_role_status(task.id, "implementer", "blocked")
    service._store.update_role_status(task.id, "auditor", "passed")

    asyncio.run(service.add_user_message(task.id, "按新一轮继续处理"))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["director"].status == "streaming"
    assert jobs["implementer"].status == "idle"
    assert jobs["auditor"].status == "idle"
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
    ]


def test_user_followup_starts_clean_visible_turn_after_interrupted_task(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "interrupted")
    service._store.update_role_status(task.id, "director", "interrupted")
    service._store.update_role_status(task.id, "implementer", "blocked")
    service._store.update_role_status(task.id, "auditor", "passed")

    asyncio.run(service.add_user_message(task.id, "接力暂停了，可以继续吗"))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["director"].status == "streaming"
    assert jobs["implementer"].status == "idle"
    assert jobs["auditor"].status == "idle"
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
    ]


def test_user_followup_on_completed_task_records_visible_turn_and_resumes(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")

    asyncio.run(service.add_user_message(task.id, "继续解释为什么没有显示"))

    detail = service.get_task(task.id)
    followups = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "user_followup"
    ]
    assert detail.task.status == "running"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "streaming"
    assert followups[-1]["text"] == "继续解释为什么没有显示"
    assert followups[-1]["target_role"] == "director"
    assert followups[-1]["context_packet_id"]


def test_user_followup_continue_emits_dispatch_verified(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(service.add_user_message(task.id, "Latest user instruction"))

    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert event_types[-2:] == ["role.streaming", "dispatch.verified"]


def test_director_followup_plain_text_completion_is_visible_and_completes_turn(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续解释为什么没有显示"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "问题在主会话投影层，"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert detail.task.status == "completed"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "passed"
    assert followup_responses[-1]["text"] == "问题在主会话投影层，"


def test_director_followup_provider_display_delta_is_visible(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续解释"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.PROVIDER_DISPLAY_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "provider 原文回答"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert followup_responses[-1]["text"] == "provider 原文回答"


def test_director_followup_provider_display_delta_ignores_compatibility_projection(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续解释"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    provider_delta = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.PROVIDER_DISPLAY_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "provider 原文回答"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    legacy_delta = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "delta": "provider 原文回答",
                "compatibility_projection": EventType.MODEL_TEXT_DELTA,
            },
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(provider_delta)
    service.project_runtime_event(legacy_delta)
    service.project_runtime_event(completed)

    output_events = [
        event
        for event in service.events_for_task(task.id)
        if event.event_type == "role.output_delta"
    ]
    followup_responses = [
        artifact
        for artifact in service.get_task(task.id).artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert [event.payload["delta"] for event in output_events[-1:]] == ["provider 原文回答"]
    assert followup_responses[-1]["text"] == "provider 原文回答"


def test_director_followup_bad_json_provider_display_delta_streams_to_ui(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续解释"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    delta = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.PROVIDER_DISPLAY_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": '{"routing_decision":'},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(delta)

    output_events = [
        event
        for event in service.events_for_task(task.id)
        if event.event_type == "role.output_delta"
    ]
    assert output_events[-1].payload["delta"] == '{"routing_decision":'


def test_followup_malformed_routing_envelope_records_visible_response_and_invalid_semantic(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续按开发审核流程处理"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    bad_protocol_text = (
        '{"artifact_type":"routing_decisioncomplexitymedium'
        "routecore_relayrequired_rolesdirectorimplementerauditor"
        'statuspassedsummary core：先清理，再残行为一致结果"}'
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": bad_protocol_text},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    runtime_events = RuntimeEventStore(service._store._ledger._conn).list_by_agent_run_tail(
        201,
        limit=20,
    )
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    invalid_artifacts = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_artifact_invalid"
        and artifact.get("relay_role") == "director"
    ]
    role_errors = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "director"
    ]
    semantic_invalid = [
        event
        for event in runtime_events
        if event.event_type == EventType.PROVIDER_SEMANTIC_ARTIFACT_INVALID
    ]
    assert followup_responses[-1]["text"] == _plain_followup_visible_text(bad_protocol_text)
    assert invalid_artifacts[-1]["error"].startswith("missing required fields")
    assert invalid_artifacts[-1]["runtime_event_id"] == completed.id
    assert semantic_invalid[-1].payload["error"].startswith("missing required fields")
    assert detail.task.status == "waiting_user"
    assert detail.routing_decision is None
    assert jobs["director"].status == "waiting"
    assert jobs["implementer"].status == "idle"
    assert len([call for call in provider.calls if call[0] == "continue_session"]) == 1
    assert role_errors == []


def test_malformed_routing_envelope_blocks_after_format_retry_is_spent(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {"retry_kind": "format", "error": "invalid json", "round_id": 1},
        summary="previous format retry already used",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            '{"artifact_type":"routing_decisioncomplexitymedium'
            "routecore_relayrequired_rolesdirectorimplementerauditor"
            'statuspassedsummary core：先清理，再残行为一致结果"}',
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    role_errors = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "director"
    ]
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert detail.routing_decision is None
    assert all("recovered_as" not in artifact for artifact in role_errors)
    assert "missing required fields" in role_errors[-1]["error"]


def test_format_retry_budget_resets_for_new_followup_round(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {"retry_kind": "format", "error": "invalid json", "round_id": 1},
        summary="previous round format retry already used",
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_task_status(task.id, "completed")

    asyncio.run(service.add_user_message(task.id, "继续处理刚才的问题"))
    provider.calls.clear()
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            '{"artifact_type":"routing_decisioncomplexitymedium'
            "routecore_relayrequired_rolesdirectorimplementerauditor"
            'statuspassedsummary core：先清理，再残行为一致结果"}',
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    current_round_errors = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "director"
        and artifact.get("round_id") == 2
    ]
    assert detail.current_round_id == 2
    assert detail.task.status == "running"
    assert jobs["director"].status == "streaming"
    assert any(call[0] == "continue_session" for call in provider.calls)
    assert current_round_errors[-1]["retry_kind"] == "format"


def test_relay_store_rejects_unknown_artifact_type(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="audit artifact type boundaries",
        workspace="/repo",
        provider="claude",
    )

    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {"error": "format failed"},
        summary="format failed",
    )

    with pytest.raises(ValueError, match="unknown relay artifact_type: weather_answer"):
        service._store.save_artifact(
            task.id,
            "director",
            "weather_answer",
            {"summary": "not a relay artifact"},
            summary="not a relay artifact",
        )


def test_followup_director_waiting_handoff_dispatches_implementer(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="修复 Marvis relay UI",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "status": "passed",
            "reason": "需要开发和审核闭环",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "implementer",
            "summary": "按核心接力处理。",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给开发工程师处理",
            "route": "core_relay",
            "required_roles": ["director", "implementer", "auditor"],
            "requires_user_approval": False,
        },
        summary="按核心接力处理。",
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.update_role_status(task.id, "auditor", "passed")
    service._store.update_task_status(task.id, "completed")

    asyncio.run(service.add_user_message(task.id, "把附件区白底改成和对话页同底色"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "status": "completed",
                "text": json.dumps(
                    {
                        "status": "waiting",
                        "reason": "用户要求修改 UI，需要开发处理。",
                        "role": "director",
                        "artifact_type": "final_summary",
                        "handoff_to": "implementer",
                        "summary": "我懂你的意思：附件区不应该有独立白底，下一步交给开发工程师改成和对话页同底色。",
                        "evidence_refs": [],
                        "open_questions": [],
                        "next_action": "开发工程师修改附件区背景并提交审核",
                    },
                    ensure_ascii=False,
                ),
            },
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"
    assert jobs["auditor"].status == "idle"
    assert any(
        artifact.get("artifact_type") == "handoff_packet"
        and artifact.get("from_role") == "director"
        and artifact.get("handoff_to") == "implementer"
        for artifact in detail.artifacts
    )
    assert provider.calls[-1][0] == "start_session"


def test_followup_starts_new_round_and_current_detail_ignores_old_waiting_summary(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay rounds",
        prompt="先修复顶部图标。",
        workspace="/repo",
        provider="claude",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "status": "passed",
            "reason": "需要开发和审核",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "implementer",
            "summary": "第一轮交给开发。",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "开发处理",
            "route": "core_relay",
            "required_roles": ["director", "implementer", "auditor"],
            "requires_user_approval": False,
        },
        summary="第一轮交给开发。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "status": "waiting",
            "reason": "旧轮等待用户验收",
            "role": "director",
            "artifact_type": "final_summary",
            "handoff_to": "",
            "summary": "旧轮等待用户验收。",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "等待用户确认",
        },
        summary="旧轮等待用户验收。",
    )
    service._store.update_task_status(task.id, "completed")
    service._store.update_role_status(task.id, "director", "passed")

    asyncio.run(service.add_user_message(task.id, "继续修复 handoff 提示"))

    detail = service.get_task(task.id)
    rounds = {
        artifact.get("artifact_type"): artifact.get("round_id")
        for artifact in detail.artifacts
        if artifact.get("artifact_type") in {"user_followup", "relay_board"}
    }
    assert max(int(artifact.get("round_id") or 0) for artifact in detail.artifacts) == 2
    assert rounds["user_followup"] == 2
    assert detail.routing_decision is None
    assert detail.task.status == "running"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "streaming"


def test_runtime_projection_tags_events_with_current_agent_run_round(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay rounds",
        prompt="先修复顶部图标。",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "第二轮继续修复"))

    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    current_round_delta = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-current-round",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "当前轮输出"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(current_round_delta)

    native_events = [
        event
        for event in service.events_for_task(task.id)
        if event.event_type == "role.native_event"
    ]
    delta_events = [
        event
        for event in service.events_for_task(task.id)
        if event.event_type == "role.output_delta"
    ]
    assert native_events[-1].payload["round_id"] == 2
    assert delta_events[-1].payload["round_id"] == 2


def test_followup_completion_uses_current_native_turn_delta_not_old_completed_text(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续说明 task28 是否能对话"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    old_completed_text = json.dumps(
        {
            "role_envelope": {
                "artifact_type": "final_summary",
                "role": "director",
                "status": "passed",
                "summary": "旧 turn 的 workspace 结论",
                "handoff_to": "",
                "next_action": "",
                "open_questions": [],
                "evidence_refs": [],
            }
        },
        ensure_ascii=False,
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_MESSAGE_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"text": old_completed_text, "native_turn_id": "turn-old"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "delta": "task28 现在可以继续对话。",
                "native_turn_id": "turn-current",
            },
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed", "native_turn_id": "turn-current"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert detail.task.status == "completed"
    assert followup_responses[-1]["text"] == "task28 现在可以继续对话。"
    assert "旧 turn" not in followup_responses[-1]["text"]


def test_active_runtime_scan_ignores_old_turn_completion_for_pending_followup(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "第三次接续验证"))
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="claude",
        provider_engine="sdk-test",
        native_session_id="native-1",
        agent_run_id=201,
        turn_id="turn-current",
        active_turn_id="turn-current",
        turn_running=True,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_MESSAGE_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"text": "旧 turn 的最终回复", "native_turn_id": "turn-old"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    projected = asyncio.run(service.scan_active_native_runtime_events(runtime_store))

    detail = service.get_task(task.id)
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert projected == 0
    assert followup_responses == []
    assert detail.task.status == "running"

    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "最终正常", "native_turn_id": "turn-current"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "native_turn_id": "turn-current",
            },
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    projected = asyncio.run(service.scan_active_native_runtime_events(runtime_store))

    detail = service.get_task(task.id)
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert projected == 2
    assert followup_responses[-1]["text"] == "最终正常"
    assert followup_responses[-1]["runtime_event_id"] == completed.id
    assert followup_responses[-1]["native_turn_id"] == "turn-current"


def test_runtime_completion_uses_provider_read_session_before_bad_delta(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": '{"artifact_type":"routing_decision","role"'},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )
    completed_text = json.dumps(
        {
            "status": "passed",
            "reason": "implementation and audit required",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "",
            "summary": "Use provider read_session transcript.",
            "evidence_refs": ["provider.read_session"],
            "open_questions": [],
            "next_action": "implement then audit",
            "complexity": "medium",
            "risk": "medium",
            "route": "core_relay",
            "required_roles": ["director", "implementer", "auditor"],
            "acceptance_criteria": ["implemented", "audited"],
            "stop_conditions": [],
            "requires_user_approval": False,
        },
        ensure_ascii=False,
    )

    async def read_session(native_session_id: str):
        provider.calls.append(("read_session", native_session_id))
        return {
            "thread": {"native_session_id": native_session_id},
            "turns": [{"role": "assistant", "text": completed_text, "native_turn_id": "turn-1"}],
        }

    provider.read_session = read_session
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert ("sync_session", "native-1") in provider.calls
    assert ("read_session", "native-1") in provider.calls
    assert detail.routing_decision is not None
    assert detail.routing_decision["summary"] == "Use provider read_session transcript."
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"


def test_followup_plain_text_can_complete_from_provider_read_session(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续解释为什么没有显示"))
    completed_text = "问题在主会话投影层，现在已按同任务多轮接续展示。"

    async def read_session(native_session_id: str):
        provider.calls.append(("read_session", native_session_id))
        return {
            "thread": {"native_session_id": native_session_id},
            "turns": [
                {
                    "role": "assistant",
                    "text": completed_text,
                    "native_turn_id": "turn-followup",
                }
            ],
        }

    provider.read_session = read_session
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed", "native_turn_id": "turn-followup"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert ("sync_session", "native-1") in provider.calls
    assert ("read_session", "native-1") in provider.calls
    assert detail.task.status == "completed"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "passed"
    assert followup_responses[-1]["text"] == completed_text
    assert followup_responses[-1]["native_turn_id"] == "turn-followup"


def test_followup_malformed_read_session_protocol_records_visible_response(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续按开发审核流程处理"))
    bad_protocol_text = (
        '{"artifact_type":"routing_decisioncomplexitymedium'
        "routecore_relayrequired_rolesdirectorimplementerauditor"
        'statuspassedsummary core：先清理，再残行为一致结果"}'
    )

    async def read_session(native_session_id: str):
        provider.calls.append(("read_session", native_session_id))
        return {
            "thread": {"native_session_id": native_session_id},
            "turns": [
                {
                    "role": "assistant",
                    "text": bad_protocol_text,
                    "native_turn_id": "turn-followup",
                }
            ],
        }

    provider.read_session = read_session
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "completed", "native_turn_id": "turn-followup"},
            occurred_at=now_iso(),
            agent_run_id=201,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    invalid_artifacts = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_artifact_invalid"
        and artifact.get("relay_role") == "director"
    ]
    assert followup_responses[-1]["text"] == _plain_followup_visible_text(bad_protocol_text)
    assert invalid_artifacts[-1]["error"].startswith("missing required fields")
    assert detail.task.status == "waiting_user"
    assert detail.routing_decision is None
    assert jobs["director"].status == "waiting"
    assert jobs["implementer"].status == "idle"


def test_plain_followup_visible_text_hides_fused_protocol_summary() -> None:
    text = (
        '{"artifact_type":"final_summary","evidence_refs":[],"handoff_to":"",'
        '"next_actionopen_questionsreason接续验证。'
        'roledirectorstatuspassedsummary已修复"}'
    )

    assert (
        _plain_followup_visible_text(text)
        == "总工程师的结构化输出已由系统处理，原始协议内容不在主会话展示。"
    )


def test_plain_followup_visible_text_hides_fragmented_protocol_after_readable_prefix() -> None:
    text = (
        "我会先修输入框对齐，再替换底部导航图标。"
        "CSS 已经收口，旧伪元素已删除，编译确认通过。"
        '{"artifact_type":"final_summary","evidence_refs":["python -m py_compile"],'
        '"handoff_to":"","next_actionopen_questions":[],"reason完成交互本地'
        'roledirectorstatuspassedsummary区“请输入”椭圆麦克风 风格为空可更新 缓存版本"}'
    )

    assert (
        _plain_followup_visible_text(text)
        == "总工程师的结构化输出已由系统处理，原始协议内容不在主会话展示。"
    )


def test_plain_followup_visible_text_does_not_guess_result_tail_before_protocol_payload() -> None:
    text = (
        "我会先修输入框对齐。"
        "中间过程文字很多，没有完整断句"
        "CSS 已经收口形mar-relay-nav-icon-chatclock/tool/person已删编译确认确实提交双数据"
        '{"artifact_type":"final_summary","evidence_refs":[],'
        '"reason完成交互本地roledirectorstatuspassedsummary区“请输入”椭圆麦克风"}'
    )

    assert (
        _plain_followup_visible_text(text)
        == "总工程师的结构化输出已由系统处理，原始协议内容不在主会话展示。"
    )


def test_scan_stale_native_role_does_not_close_pending_followup_from_old_session_read(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "completed")
    asyncio.run(service.add_user_message(task.id, "继续说明 task28 是否能对话"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.INTERNAL,
            payload={
                "action": "turn_started",
                "status": "running",
                "native_turn_id": "turn-current",
            },
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=201,
        )
    )
    old_completed_text = """
    {
      "status": "passed",
      "reason": "old result",
      "role": "director",
      "artifact_type": "final_summary",
      "handoff_to": "",
      "summary": "旧 session 读取结果",
      "evidence_refs": [],
      "open_questions": [],
      "next_action": ""
    }
    """

    async def read_session(native_session_id: str):
        provider.calls.append(("read_session", native_session_id))
        return {
            "thread": {"native_session_id": native_session_id},
            "turns": [{"role": "assistant", "text": old_completed_text}],
        }

    provider.read_session = read_session

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    detail = service.get_task(task.id)
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert changed == 0
    assert ("read_session", "native-1") not in provider.calls
    assert followup_responses == []
    assert detail.task.status == "running"
    assert {job.role: job.status for job in detail.role_jobs}["director"] == "streaming"


def test_scan_stale_native_role_syncs_then_blocks_without_assistant_output(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.INTERNAL,
            payload={"activity": "turn_started"},
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    assert changed == 1
    assert ("sync_session", "native-1") in provider.calls
    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert (
        jobs["director"].error_message
        == "native provider stayed running without assistant output for 301s (limit 300s)"
    )


def test_scan_stale_native_role_completes_synced_role_envelope_delta(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.INTERNAL,
            payload={"activity": "turn_started"},
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )

    async def sync_session(native_session_id: str):
        provider.calls.append(("sync_session", native_session_id))
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={
                    "delta": """
                    {
                      "status": "passed",
                      "reason": "implementation and testing required",
                      "role": "director",
                      "artifact_type": "routing_decision",
                      "handoff_to": "",
                      "summary": "Core relay.",
                      "evidence_refs": [],
                      "open_questions": [],
                      "next_action": "implement",
                      "complexity": "medium",
                      "risk": "medium",
                      "route": "core_relay",
                      "required_roles": ["director", "implementer", "tester"],
                      "acceptance_criteria": ["implemented", "tested"],
                      "stop_conditions": [],
                      "requires_user_approval": false
                    }
                    """
                },
                occurred_at=now.isoformat(),
                agent_run_id=101,
            )
        )
        return NativeAgentControlResult(
            provider=provider.provider,
            provider_engine=provider.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=101,
            status="synced",
        )

    provider.sync_session = sync_session

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    assert changed == 1
    assert ("sync_session", "native-1") in provider.calls
    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"
    assert jobs["director"].error_message == ""
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "sync_session",
        "start_session",
    ]


def test_scan_stale_native_role_recovers_interrupted_task_with_active_role(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_task_status(task.id, "interrupted")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.INTERNAL,
            payload={"activity": "turn_started"},
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )

    async def sync_session(native_session_id: str):
        provider.calls.append(("sync_session", native_session_id))
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={
                    "delta": """
                    {
                      "status": "passed",
                      "reason": "implementation and audit required",
                      "role": "director",
                      "artifact_type": "routing_decision",
                      "handoff_to": "",
                      "summary": "继续处理接续任务。",
                      "evidence_refs": [],
                      "open_questions": [],
                      "next_action": "implement",
                      "complexity": "medium",
                      "risk": "medium",
                      "route": "core_relay",
                      "required_roles": ["director", "implementer", "auditor"],
                      "acceptance_criteria": ["implemented", "audited"],
                      "stop_conditions": [],
                      "requires_user_approval": false
                    }
                    """
                },
                occurred_at=now.isoformat(),
                agent_run_id=101,
            )
        )
        return NativeAgentControlResult(
            provider=provider.provider,
            provider_engine=provider.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=101,
            status="synced",
        )

    provider.sync_session = sync_session

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert changed == 1
    assert detail.task.status == "running"
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"
    assert ("sync_session", "native-1") in provider.calls


def test_scan_stale_native_role_uses_complete_protocol_delta_before_fragments(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="claude",
        provider_engine="sdk-test",
        native_session_id="native-1",
        agent_run_id=101,
        turn_id="turn-current",
        active_turn_id="turn-current",
        turn_running=True,
        dispatch_verified=True,
    )
    detail = service.get_task(task.id)
    director = next(job for job in detail.role_jobs if job.role == "director")
    turn_id = director.active_turn_id or director.turn_id
    service._store.update_task_status(task.id, "interrupted")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    for delta in (
        '{"artifact_type":"routing_decision"',
        '"summary":"broken prefix"',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={"delta": delta, "native_turn_id": turn_id},
                occurred_at=(now - timedelta(seconds=303)).isoformat(),
                agent_run_id=101,
            )
        )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "delta": """
                {
                  "status": "passed",
                  "reason": "implementation and audit required",
                  "role": "director",
                  "artifact_type": "routing_decision",
                  "handoff_to": "",
                  "summary": "继续处理接续任务。",
                  "evidence_refs": [],
                  "open_questions": [],
                  "next_action": "implement",
                  "complexity": "medium",
                  "risk": "medium",
                  "route": "core_relay",
                  "required_roles": ["director", "developer_engineer", "audit_engineer"],
                  "acceptance_criteria": ["implemented", "audited"],
                  "stop_conditions": [],
                  "requires_user_approval": false
                }
                """,
                "native_turn_id": turn_id,
            },
            occurred_at=(now - timedelta(seconds=302)).isoformat(),
            agent_run_id=101,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "native_turn_id": turn_id,
            },
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert changed == 1
    assert detail.routing_decision is not None
    assert detail.routing_decision["required_roles"] == [
        "director",
        "implementer",
        "auditor",
    ]
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"


def test_scan_stale_native_role_prefers_late_complete_delta_over_bad_stream(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    for delta in (
        '{"artifact_type":"routing_decision"',
        '"role":"director","status":"passed"',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={"delta": delta, "native_turn_id": "turn-late"},
                occurred_at=(now - timedelta(seconds=302)).isoformat(),
                agent_run_id=101,
            )
        )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "native_turn_id": "turn-late",
            },
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={
                "delta": """
                {
                  "status": "passed",
                  "reason": "implementation and audit required",
                  "role": "director",
                  "artifact_type": "routing_decision",
                  "handoff_to": "",
                  "summary": "继续执行最新接续需求。",
                  "evidence_refs": [],
                  "open_questions": [],
                  "next_action": "交给开发工程师处理。",
                  "complexity": "medium",
                  "risk": "medium",
                  "route": "core_relay",
                  "required_roles": ["director", "implementer", "auditor"],
                  "acceptance_criteria": ["开发完成", "审核通过"],
                  "stop_conditions": [],
                  "requires_user_approval": false
                }
                """,
                "native_turn_id": "turn-late",
            },
            occurred_at=now.isoformat(),
            agent_run_id=101,
        )
    )

    changed = asyncio.run(
        service._complete_stale_native_delta_if_ready(
            runtime_store,
            task_id=task.id,
            role="director",
            agent_run_id=101,
            after_id=0,
            provider_name="claude",
            native_session_id="native-1",
            allow_read_session=False,
            current_turn_id="turn-late",
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    routing = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "routing_decision"
    ]
    assert changed is True
    assert routing[-1]["summary"] == "继续执行最新接续需求。"
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"


def test_scan_stale_native_role_prefers_completed_message_over_malformed_codex_deltas(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="claude",
        provider_engine="app-server",
        native_session_id="native-1",
        agent_run_id=101,
        turn_id="turn-codex",
        active_turn_id="turn-codex",
        turn_running=True,
        dispatch_verified=True,
    )
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    for delta in (
        '{"',
        "artifact",
        '_type":"routing_decision","',
        "status",
        "passed",
        "reason",
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.PROVIDER_DISPLAY_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CODEX,
                actor="codex",
                visibility=Visibility.USER,
                payload={
                    "delta": delta,
                    "text": delta,
                    "itemId": "item-24",
                    "native_turn_id": "turn-codex",
                },
                occurred_at=(now - timedelta(seconds=303)).isoformat(),
                agent_run_id=101,
            )
        )
    completed_json = json.dumps(
        {
            "artifact_type": "routing_decision",
            "status": "passed",
            "reason": "completed item is authoritative",
            "role": "director",
            "handoff_to": "",
            "summary": "Use completed item text.",
            "evidence_refs": ["item_completed"],
            "open_questions": [],
            "next_action": "finish",
            "complexity": "medium",
            "risk": "medium",
            "route": "core_relay",
            "required_roles": ["director", "implementer", "auditor"],
            "acceptance_criteria": ["completed source accepted"],
            "stop_conditions": [],
            "requires_user_approval": False,
        },
        ensure_ascii=False,
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.PROVIDER_DISPLAY_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CODEX,
            actor="codex",
            visibility=Visibility.USER,
            payload={
                "text": completed_json,
                "itemId": "item-24",
                "native_turn_id": "turn-codex",
                "display_source": "provider",
            },
            occurred_at=(now - timedelta(seconds=302)).isoformat(),
            agent_run_id=101,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CODEX,
            actor="codex",
            visibility=Visibility.INTERNAL,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "native_turn_id": "turn-codex",
            },
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    invalid_artifacts = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_artifact_invalid"
    ]
    routing_artifacts = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "routing_decision"
    ]
    assert changed == 1
    assert jobs["director"].status == "passed"
    assert jobs["director"].error_message == ""
    assert invalid_artifacts == []
    assert routing_artifacts[-1]["summary"] == "Use completed item text."


def test_scan_stale_native_role_prefers_synced_completed_message_over_bad_delta(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": '{"artifact_type":"routing_decision","role"'},
            occurred_at=(now - timedelta(seconds=302)).isoformat(),
            agent_run_id=101,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.INTERNAL,
            payload={"activity": "turn_started"},
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )

    async def sync_session(native_session_id: str):
        provider.calls.append(("sync_session", native_session_id))
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_MESSAGE_COMPLETED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={
                    "text": """
                    {
                      "status": "passed",
                      "reason": "implementation and testing required",
                      "role": "director",
                      "artifact_type": "routing_decision",
                      "handoff_to": "",
                      "summary": "Use completed transcript.",
                      "evidence_refs": ["model.message.completed"],
                      "open_questions": [],
                      "next_action": "implement",
                      "complexity": "medium",
                      "risk": "medium",
                      "route": "core_relay",
                      "required_roles": ["director", "implementer", "tester"],
                      "acceptance_criteria": ["implemented", "tested"],
                      "stop_conditions": [],
                      "requires_user_approval": false
                    }
                    """,
                    "native_thread_id": native_session_id,
                    "native_turn_id": "turn-1",
                },
                occurred_at=now.isoformat(),
                agent_run_id=101,
            )
        )
        return NativeAgentControlResult(
            provider=provider.provider,
            provider_engine=provider.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=101,
            status="synced",
        )

    provider.sync_session = sync_session

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert changed == 1
    assert ("sync_session", "native-1") in provider.calls
    assert detail.task.status == "running"
    assert jobs["director"].status == "passed"
    assert jobs["director"].error_message == ""
    assert jobs["implementer"].status == "streaming"
    routing = service.get_task(task.id).routing_decision
    assert routing is not None
    assert routing["summary"] == "Use completed transcript."


def test_scan_stale_native_role_uses_provider_read_session_before_bad_delta(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": '{"artifact_type":"routing_decision","role"'},
            occurred_at=(now - timedelta(seconds=302)).isoformat(),
            agent_run_id=101,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.INTERNAL,
            payload={"activity": "turn_started"},
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )
    completed_text = """
    {
      "status": "passed",
      "reason": "implementation and testing required",
      "role": "director",
      "artifact_type": "routing_decision",
      "handoff_to": "",
      "summary": "Use provider read_session transcript.",
      "evidence_refs": ["provider.read_session"],
      "open_questions": [],
      "next_action": "implement",
      "complexity": "medium",
      "risk": "medium",
      "route": "core_relay",
      "required_roles": ["director", "implementer", "tester"],
      "acceptance_criteria": ["implemented", "tested"],
      "stop_conditions": [],
      "requires_user_approval": false
    }
    """

    async def read_session(native_session_id: str):
        provider.calls.append(("read_session", native_session_id))
        return {
            "thread": {"native_session_id": native_session_id},
            "turns": [{"role": "assistant", "text": completed_text, "native_turn_id": "turn-1"}],
        }

    provider.read_session = read_session

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert changed == 1
    assert ("sync_session", "native-1") in provider.calls
    assert ("read_session", "native-1") in provider.calls
    assert jobs["director"].status == "passed"
    assert detail.routing_decision is not None
    assert detail.routing_decision["summary"] == "Use provider read_session transcript."


def test_scan_stale_native_role_blocks_completed_invalid_output(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "architect"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    now = datetime(2026, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": '{"artifact_type":"architecture_plan","role"'},
            occurred_at=(now - timedelta(seconds=302)).isoformat(),
            agent_run_id=101,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"action": "turn_completed", "status": "completed"},
            occurred_at=(now - timedelta(seconds=301)).isoformat(),
            agent_run_id=101,
        )
    )

    changed = asyncio.run(service.scan_stale_native_roles(max_idle_seconds=300, now=now))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert changed == 1
    assert detail.task.status == "blocked"
    assert jobs["architect"].status == "blocked"
    assert jobs["architect"].error_message.startswith(
        "native provider completed without valid relay envelope:"
    )


def test_non_native_turn_completed_folds_text_deltas_into_role_completion(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    for delta in (
        '{"role_envelope": {"status": "passed", ',
        '"reason": "done", "role": "director", ',
        '"artifact_type": "final_summary", "handoff_to": "", ',
        '"summary": "Relay complete", "evidence_refs": ["x"], ',
        '"open_questions": [], "next_action": "complete task"}}',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={"delta": delta},
                occurred_at=now_iso(),
                agent_run_id=101,
            )
        )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"action": "turn_completed", "status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert jobs["director"].error_message == (
        "director must produce routing_decision before final_summary"
    )
    assert event_types[-1] == "role.status"


def test_codex_native_empty_turn_completed_waits_for_text(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="查询今日金价",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert detail.task.status == "running"
    assert jobs["director"].status == "streaming"
    assert "role.status" not in event_types


def test_agent_run_completed_folds_text_deltas_into_role_completion(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "full relay required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Use full relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "architect plan",
              "complexity": "complex",
              "risk": "medium",
              "route": "full_relay",
              "required_roles": ["director", "architect", "implementer"],
              "acceptance_criteria": ["roles pass"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(service.dispatch_role(task.id, "architect"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    for delta in (
        '{"status":"passed","reason":"architecture checked",',
        '"role":"architect","artifact_type":"architecture_plan",',
        '"handoff_to":"implementer","summary":"Architecture ready",',
        '"evidence_refs":["runtime_events"],"open_questions":[],',
        '"next_action":"implement"}',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.ANTIGRAVITY,
                actor="antigravity_cli_local",
                visibility=Visibility.USER,
                payload={"delta": delta},
                occurred_at=now_iso(),
                agent_run_id=101,
            )
        )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.ANTIGRAVITY,
            actor="antigravity_cli_local",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert jobs["architect"].status == "passed"
    assert jobs["implementer"].status == "streaming"
    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]
    assert "handoff.created" in event_types


def test_scan_active_native_runtime_events_projects_existing_completion(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "full relay required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Use full relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "architect plan",
              "complexity": "complex",
              "risk": "medium",
              "route": "full_relay",
              "required_roles": ["director", "architect", "implementer"],
              "acceptance_criteria": ["roles pass"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(service.dispatch_role(task.id, "architect"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    for delta in (
        '{"status":"passed","reason":"architecture checked",',
        '"role":"architect","artifact_type":"architecture_plan",',
        '"handoff_to":"implementer","summary":"Architecture ready",',
        '"evidence_refs":["runtime_events"],"open_questions":[],',
        '"next_action":"implement"}',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.ANTIGRAVITY,
                actor="antigravity_cli_local",
                visibility=Visibility.USER,
                payload={"delta": delta},
                occurred_at=now_iso(),
                agent_run_id=101,
            )
        )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.ANTIGRAVITY,
            actor="antigravity_cli_local",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    changed = asyncio.run(service.scan_active_native_runtime_events(runtime_store, limit=20))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert changed == 6
    assert jobs["architect"].status == "passed"
    assert jobs["implementer"].status == "streaming"
    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]


def test_agent_run_completed_without_text_blocks_streaming_role(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "architect"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.ANTIGRAVITY,
            actor="antigravity_cli_local",
            visibility=Visibility.USER,
            payload={"status": "completed"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["architect"].status == "blocked"
    assert jobs["architect"].error_message == ("native provider completed without assistant output")
    assert any(
        artifact.get("artifact_type") == "role_error"
        and artifact.get("summary") == "native provider completed without assistant output"
        for artifact in detail.artifacts
    )


def test_runtime_agent_run_failed_blocks_streaming_role(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    failed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type="agent.run.failed",
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"status": "failed", "error": "provider login required"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(failed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert jobs["director"].error_message == "provider login required"
    assert any(
        artifact.get("artifact_type") == "role_error"
        and artifact.get("summary") == "provider login required"
        for artifact in detail.artifacts
    )


def test_implementer_markdown_fenced_frontend_patch_envelope_retries_format(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "core relay required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "implementer",
              "summary": "需要开发工程师实现前端调整。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implementer implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "tester"],
              "acceptance_criteria": ["聊天输入框上方显示工作区"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
        )
    )
    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    implementer_agent_run_id = int(jobs["implementer"].agent_run_id or 0)
    runtime_event = RuntimeEvent(
        id=88,
        schema_version=1,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id=str(implementer_agent_run_id),
        correlation_id="corr-101",
        source=EventSource.CLAUDE,
        actor="claude",
        visibility=Visibility.USER,
        payload={
            "text": """
            ```json
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "frontend_patch",
              "handoff_to": "tester",
              "summary": "已添加工作区显示和选择入口。",
              "evidence_refs": ["wlcodex/live_stream/server.py"],
              "open_questions": [],
              "next_action": "tester verify"
            }
            ```
            """
        },
        occurred_at=now_iso(),
        agent_run_id=implementer_agent_run_id,
    )

    asyncio.run(service.handle_runtime_event(runtime_event))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    implementation = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "implementation_report"
    ]
    role_errors = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "implementer"
    ]
    assert jobs["implementer"].status == "streaming"
    assert jobs["tester"].status == "idle"
    assert implementation == []
    assert role_errors[-1]["retry_kind"] == "format"
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
    ]


def test_implementer_placeholder_artifact_type_normalizes_and_dispatches_auditor(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="修复聊天页输入后没有反馈的问题",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "core relay required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "implementer",
              "summary": "需要开发工程师修复后交给审核工程师。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implementer implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "auditor"],
              "acceptance_criteria": ["接续对话可见反馈"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
        )
    )
    service._store.update_task_status(task.id, "blocked")
    service._store.update_role_status(task.id, "implementer", "blocked")

    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "relay artifact type",
              "handoff_to": "",
              "summary": "已修复接续对话没有反馈的问题。",
              "evidence_refs": ["wlcodex/live_stream/server.py"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    implementation = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "implementation_report"
    ]
    assert jobs["implementer"].status == "passed"
    assert jobs["auditor"].status == "streaming"
    assert detail.task.status == "running"
    assert implementation[-1]["summary"] == "已修复接续对话没有反馈的问题。"
    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]


def test_codex_native_turn_completed_folds_text_deltas_into_routing_decision(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="查询今日天气",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    for delta in (
        '{"status":"passed","reason":"simple lookup","role":"director",',
        '"artifact_type":"routing_decision","handoff_to":"",',
        '"summary":"直接查询天气","evidence_refs":["wttr"],',
        '"open_questions":[],"next_action":"complete directly",',
        '"route":"director_only","risk":"low","complexity":"low",',
        '"required_roles":["director"],',
        '"acceptance_criteria":["给出天气结论"],',
        '"stop_conditions":[],"requires_user_approval":false}',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CODEX,
                actor="codex_native",
                visibility=Visibility.USER,
                payload={
                    "delta": delta,
                    "source_kind": "codex_native",
                    "provider": "codex",
                    "provider_engine": "app-server",
                },
                occurred_at=now_iso(),
                agent_run_id=101,
            )
        )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert detail.routing_decision is not None
    assert detail.routing_decision["route"] == "director_only"
    assert detail.task.status == "running"
    assert jobs["director"].status == "streaming"
    assert event_types[-2:] == ["role.streaming", "dispatch.verified"]


def test_codex_native_turn_completed_retries_invalid_core_relay_routing_decision(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="给聊天框上方增加工作区显示和选择入口",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "delta": json.dumps(
                    {
                        "status": "passed",
                        "reason": "需要实现一个小的 UI 改动",
                        "role": "director",
                        "artifact_type": "routing_decision",
                        "handoff_to": "core_relay",
                        "summary": "建议走 core_relay：实现聊天框上方的当前工作区显示与选择入口。",
                        "evidence_refs": [],
                        "open_questions": [],
                        "next_action": "派发给开发实现",
                        "complexity": "medium",
                        "risk": "medium",
                        "route": "core_relay",
                        "required_roles": ["director", "implementer", "reviewer"],
                        "acceptance_criteria": ["显示当前工作区", "支持切换工作区"],
                        "stop_conditions": ["需要用户确认交互细节时暂停"],
                        "requires_user_approval": False,
                    },
                    ensure_ascii=False,
                ),
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    service.project_runtime_event(completed)

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    role_errors = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "director"
    ]
    assert detail.routing_decision is None
    assert detail.task.status == "running"
    assert jobs["director"].status == "streaming"
    assert jobs["implementer"].status == "idle"
    assert role_errors[-1]["retry_kind"] == "format"
    assert all("recovered_as" not in artifact for artifact in role_errors)


def test_stale_scan_folds_current_turn_delta_consumed_before_completion(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="修复会话页顶栏滚动问题",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "needs implementation",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "implementer",
              "summary": "交给开发修复，再交给审核。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implementer implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "auditor"],
              "acceptance_criteria": ["顶栏滚动时保持可见"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "",
              "summary": "已修复顶栏滚动问题。",
              "evidence_refs": ["wlcodex/live_stream/static/relay_marvis.css:23"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
        )
    )
    service._store.update_role_metadata(
        task.id,
        "auditor",
        provider="codex",
        provider_engine="app-server",
        native_session_id="native-auditor",
        agent_run_id=301,
        turn_id="turn-audit",
        active_turn_id="turn-audit",
        turn_running=True,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "auditor", "streaming")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    for delta in (
        '{"artifact_type":"audit_report","evidence_refs":["css:23"],',
        '"handoff_to":"","next_action":"complete task",',
        '"open_questions":[],"reason":"审核通过",',
        '"role":"auditor","status":"passed",',
        '"summary":"审核通过，可以交付。"}',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="301",
                correlation_id="corr-301",
                source=EventSource.CODEX,
                actor="codex_native",
                visibility=Visibility.USER,
                payload={
                    "delta": delta,
                    "native_turn_id": "turn-audit",
                    "source_kind": "codex_native",
                    "provider": "codex",
                    "provider_engine": "app-server",
                },
                occurred_at=now_iso(),
                agent_run_id=301,
            )
        )
    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="301",
            correlation_id="corr-301",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "native_turn_id": "turn-audit",
                "turnId": "turn-audit",
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=(datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat(),
            agent_run_id=301,
        )
    )

    changed = asyncio.run(
        service._complete_stale_native_delta_if_ready(
            runtime_store,
            task_id=task.id,
            role="auditor",
            agent_run_id=301,
            after_id=int(completed.id),
            provider_name="codex",
            native_session_id="native-auditor",
            allow_read_session=False,
            current_turn_id="turn-audit",
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    audit_reports = [
        artifact for artifact in detail.artifacts if artifact.get("artifact_type") == "audit_report"
    ]
    assert changed is True
    assert jobs["auditor"].status == "passed"
    assert audit_reports[-1]["summary"] == "审核通过，可以交付。"


def test_late_valid_protocol_delta_recovers_same_round_role_error(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="量化 Marvis 接力 token 消耗。",
        workspace="/repo",
        provider="claude",
    )
    conversation = service._store._ledger.create_conversation(
        chat_id=1,
        user_id=1,
        title="Relay task",
        mode="relay",
        workspace_alias="/repo",
    )
    agent_run = service._store._ledger.create_agent_run(
        conversation.id,
        agent="claude",
        role="director",
        external_session_id="native-director",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="claude",
        provider_engine="sdk-test",
        native_session_id="native-director",
        agent_run_id=agent_run.id,
        turn_id="turn-director",
        active_turn_id="turn-director",
        turn_running=False,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "blocked")
    service._store.update_task_status(task.id, "blocked")
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "error": "invalid json: Expecting ',' delimiter",
            "output": '{"artifact_type":"routing_decisioncomplexitymedium"}',
            "retry_kind": "format",
            "round_id": 1,
        },
        summary="角色输出格式错误，已要求重新输出合法 JSON：invalid json",
    )
    late_delta = RuntimeEvent(
        id=77,
        schema_version=1,
        event_type=EventType.MODEL_TEXT_DELTA,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id=str(agent_run.id),
        correlation_id=f"corr-{agent_run.id}",
        source=EventSource.CLAUDE,
        actor="claude",
        visibility=Visibility.USER,
        payload={
            "delta": """
            {
              "status": "passed",
              "reason": "需要审计本地 token 统计来源后再量化。",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "auditor",
              "summary": "先审计证据，再计算接力模式相对单角色模式的 token 增量。",
              "evidence_refs": ["runtime_events"],
              "open_questions": [],
              "next_action": "派审核工程师读取本地 token 使用记录。",
              "complexity": "medium",
              "risk": "low",
              "route": "audit_first",
              "required_roles": ["director", "auditor"],
              "acceptance_criteria": ["报告 token 绝对增量", "报告百分比开销"],
              "stop_conditions": ["找不到 token 日志时说明不可直接量化"],
              "requires_user_approval": false
            }
            """,
            "native_turn_id": "turn-director",
            "source_kind": "claude",
        },
        occurred_at=now_iso(),
        agent_run_id=agent_run.id,
    )

    asyncio.run(service.handle_runtime_event(late_delta))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    routing = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "routing_decision"
    ]
    assert detail.task.status == "running"
    assert jobs["director"].status == "passed"
    assert jobs["auditor"].status == "streaming"
    assert routing[-1]["summary"] == "先审计证据，再计算接力模式相对单角色模式的 token 增量。"
    assert [call[0] for call in provider.calls] == ["start_session"]


def test_active_scan_completes_auditor_after_delta_cursor_passed_completion(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="修复会话页附件背景融合问题",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "needs implementation",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "implementer",
              "summary": "交给开发修复，再交给审核。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implementer implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "auditor"],
              "acceptance_criteria": ["附件面板背景融合"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "auditor",
              "summary": "已修复附件面板背景融合。",
              "evidence_refs": ["wlcodex/live_stream/static/relay_marvis.css:23"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
        )
    )
    conversation = service._store._ledger.create_conversation(
        chat_id=1,
        user_id=1,
        title="Relay task",
        mode="relay",
        workspace_alias="/repo",
    )
    agent_run = service._store._ledger.create_agent_run(
        conversation.id,
        agent="codex",
        role="auditor",
        external_session_id="native-auditor",
    )
    service._store.update_role_metadata(
        task.id,
        "auditor",
        provider="codex",
        provider_engine="app-server",
        native_session_id="native-auditor",
        agent_run_id=agent_run.id,
        turn_id="turn-audit",
        active_turn_id="turn-audit",
        turn_running=True,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "auditor", "streaming")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    for delta in (
        '{"artifact_type":"audit_report","evidence_refs":["css:23"],',
        '"handoff_to":"","next_action":"complete task",',
        '"open_questions":[],"reason":"审核通过",',
        '"role":"auditor","status":"passed",',
        '"summary":"审核通过，附件面板背景已和对话页融合。"}',
    ):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run.id),
                correlation_id=f"corr-{agent_run.id}",
                source=EventSource.CODEX,
                actor="codex_native",
                visibility=Visibility.USER,
                payload={
                    "delta": delta,
                    "native_turn_id": "turn-audit",
                    "source_kind": "codex_native",
                    "provider": "codex",
                    "provider_engine": "app-server",
                },
                occurred_at=now_iso(),
                agent_run_id=agent_run.id,
            )
        )

    projected_before_completion = asyncio.run(
        service.scan_active_native_runtime_events(runtime_store)
    )

    completed = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(agent_run.id),
            correlation_id=f"corr-{agent_run.id}",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "native_turn_id": "turn-audit",
                "turnId": "turn-audit",
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=now_iso(),
            agent_run_id=agent_run.id,
        )
    )

    projected_after_completion = asyncio.run(
        service.scan_active_native_runtime_events(runtime_store)
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    audit_reports = [
        artifact for artifact in detail.artifacts if artifact.get("artifact_type") == "audit_report"
    ]
    updated_run = service._store._ledger.get_agent_run(agent_run.id)
    assert projected_before_completion >= 2
    assert projected_after_completion >= 1
    assert int(completed.id) > 0
    assert jobs["auditor"].status == "passed"
    assert updated_run.status == "done"
    assert audit_reports[-1]["summary"] == "审核通过，附件面板背景已和对话页融合。"


def test_active_scan_reconciles_terminal_role_agent_run_without_duplicate_artifact(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="修复会话页附件背景融合问题",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "needs implementation",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "implementer",
              "summary": "交给开发修复，再交给审核。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implementer implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "auditor"],
              "acceptance_criteria": ["附件面板背景融合"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "auditor",
              "summary": "已修复附件面板背景融合。",
              "evidence_refs": ["wlcodex/live_stream/static/relay_marvis.css:23"],
              "open_questions": [],
              "next_action": "audit"
            }
            """,
        )
    )
    conversation = service._store._ledger.create_conversation(
        chat_id=1,
        user_id=1,
        title="Relay task",
        mode="relay",
        workspace_alias="/repo",
    )
    agent_run = service._store._ledger.create_agent_run(
        conversation.id,
        agent="codex",
        role="auditor",
        external_session_id="native-auditor",
    )
    service._store.update_role_metadata(
        task.id,
        "auditor",
        provider="codex",
        provider_engine="app-server",
        native_session_id="native-auditor",
        agent_run_id=agent_run.id,
        turn_id="turn-audit",
        active_turn_id="turn-audit",
        turn_running=False,
        dispatch_verified=True,
    )
    audit_report = """
    {
      "status": "passed",
      "reason": "审核通过",
      "role": "auditor",
      "artifact_type": "audit_report",
      "handoff_to": "",
      "summary": "审核通过，附件面板背景已和对话页融合。",
      "evidence_refs": ["css:23"],
      "open_questions": [],
      "next_action": "complete task"
    }
    """
    asyncio.run(service.handle_role_output(task.id, "auditor", audit_report))
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_MESSAGE_COMPLETED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(agent_run.id),
            correlation_id=f"corr-{agent_run.id}",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "message": audit_report,
                "native_turn_id": "turn-audit",
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=now_iso(),
            agent_run_id=agent_run.id,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(agent_run.id),
            correlation_id=f"corr-{agent_run.id}",
            source=EventSource.CODEX,
            actor="codex_native",
            visibility=Visibility.USER,
            payload={
                "action": "turn_completed",
                "status": "completed",
                "native_turn_id": "turn-audit",
                "turnId": "turn-audit",
                "source_kind": "codex_native",
                "provider": "codex",
                "provider_engine": "app-server",
            },
            occurred_at=now_iso(),
            agent_run_id=agent_run.id,
        )
    )
    detail_before = service.get_task(task.id)
    audit_count_before = sum(
        1 for artifact in detail_before.artifacts if artifact.get("artifact_type") == "audit_report"
    )

    changed = asyncio.run(service.scan_active_native_runtime_events(runtime_store))

    detail_after = service.get_task(task.id)
    audit_count_after = sum(
        1 for artifact in detail_after.artifacts if artifact.get("artifact_type") == "audit_report"
    )
    updated_run = service._store._ledger.get_agent_run(agent_run.id)
    assert changed >= 1
    assert audit_count_before == 1
    assert audit_count_after == audit_count_before
    assert updated_run.status == "done"


def test_director_final_summary_requires_prior_routing_decision(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "done",
              "role": "director",
              "artifact_type": "final_summary",
              "handoff_to": "",
              "summary": "Relay complete",
              "evidence_refs": ["summary"],
              "open_questions": [],
              "next_action": "complete"
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert jobs["director"].error_message == (
        "director must produce routing_decision before final_summary"
    )


def test_director_only_routing_decision_allows_director_final_summary(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="解释一下这个概念",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "low risk explanation",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "本任务判定无需派发，由总工程师直接完成。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "direct final summary",
              "complexity": "simple",
              "risk": "low",
              "route": "director_only",
              "required_roles": ["director"],
              "acceptance_criteria": ["给出清晰解释"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "answered",
              "role": "director",
              "artifact_type": "final_summary",
              "handoff_to": "",
              "summary": "解释完成",
              "evidence_refs": ["summary"],
              "open_questions": [],
              "next_action": "complete"
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    routing = next(
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "routing_decision"
    )
    assert detail.task.status == "completed"
    assert jobs["director"].status == "passed"
    assert jobs["architect"].idle_reason == "未纳入本轮路线"
    assert routing["route"] == "director_only"
    assert routing["complexity"] == "simple"
    assert routing["required_roles"] == ["director"]


def test_goal_mode_director_final_summary_requires_acceptance_evidence(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="完成这个目标",
        workspace="/repo",
        provider="claude",
        execution_mode="goal",
        execution_goal="完成这个目标并通过验收",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "goal routing",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "目标模式不能直接收口。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "direct final summary",
              "complexity": "simple",
              "risk": "low",
              "route": "director_only",
              "required_roles": ["director"],
              "acceptance_criteria": ["完成目标"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "premature",
              "role": "director",
              "artifact_type": "final_summary",
              "handoff_to": "",
              "summary": "目标完成",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "complete"
            }
            """,
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert "goal mode final_summary before acceptance evidence completed" in jobs[
        "director"
    ].error_message


def test_director_only_routing_decision_dispatches_director_final_summary(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="查询今日金价",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "low risk realtime lookup",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "总工程师直接查询并汇总。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "complete directly",
              "complexity": "low",
              "risk": "low",
              "route": "director_only",
              "required_roles": ["director"],
              "acceptance_criteria": ["返回价格、单位、币种、更新时间和来源"],
              "stop_conditions": ["无法访问可靠行情来源"],
              "requires_user_approval": false
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
    ]
    assert detail.task.status == "running"
    assert jobs["director"].status == "streaming"
    assert event_types[-2:] == ["role.streaming", "dispatch.verified"]


def test_director_only_is_rejected_when_user_requests_full_relay(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="请完整接力走五角色验收这个任务",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "looks simple",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "尝试直接完成",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "direct final summary",
              "complexity": "simple",
              "risk": "low",
              "route": "director_only",
              "required_roles": ["director"],
              "acceptance_criteria": ["done"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert "user requested full relay" in jobs["director"].error_message


def test_routing_decision_queues_first_required_role_and_marks_idle_reasons(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="实现一个跨模块 API 改造",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation needs staged relay",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "跨模块改造，进入核心接力。",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "architect plan",
              "complexity": "complex",
              "risk": "high",
              "route": "core_relay",
              "required_roles": ["director", "architect", "implementer", "tester"],
              "acceptance_criteria": ["实现完成", "测试通过"],
              "stop_conditions": ["发现无法确认的接口兼容风险"],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "running"
    assert jobs["architect"].status == "queued"
    assert jobs["implementer"].status == "idle"
    assert jobs["implementer"].idle_reason == "等待上一角色交接"
    assert jobs["auditor"].idle_reason == "等待上一角色交接"


def test_user_followup_steers_active_director_turn_when_supported(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = ActiveTurnProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(service.add_user_message(task.id, "Latest active-turn direction"))

    assert [call[0] for call in provider.calls] == [
        "start_session",
        "steer_session",
    ]
    assert provider.calls[1][1] == "native-active"
    assert provider.calls[1][2] == "turn-1"
    assert "Latest active-turn direction" in provider.calls[1][3]


def test_user_followup_dispatches_director_when_no_session_exists(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(service.add_user_message(task.id, "fresh instruction"))

    assert [call[0] for call in provider.calls] == ["start_session"]
    assert "role: director" in provider.calls[0][2]
    assert "fresh instruction" in provider.calls[0][2]


def test_director_dispatch_prompt_requires_initial_routing_decision(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="测试接力流程：把测试接力.md删了",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(service.dispatch_role(task.id, "director"))

    prompt = provider.calls[0][2]
    assert "Director first action must be a routing_decision" in prompt
    assert '"artifact_type": "routing_decision"' in prompt
    assert (
        '"route": "director_only|core_relay|full_relay|audit_first|waiting_user|blocked"' in prompt
    )
    assert "Do not inspect, edit, delete, test, commit, or deploy" in prompt


def test_director_cannot_emit_non_routing_artifact_before_decision(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="测试接力流程：把测试接力.md删了",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "waiting",
              "reason": "file path is ambiguous",
              "role": "director",
              "artifact_type": "final_summary",
              "handoff_to": "",
              "summary": "Need confirmation before deletion.",
              "evidence_refs": [],
              "open_questions": ["Which file should be deleted?"],
              "next_action": "Ask user to confirm the file path."
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["director"].status == "blocked"
    assert jobs["director"].error_message == (
        "director must produce routing_decision before final_summary"
    )


def test_user_followup_falls_back_to_dispatch_when_continue_fails(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = FailingContinueProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(service.add_user_message(task.id, "please adjust direction"))

    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
        "start_session",
    ]
    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["director"].status == "streaming"
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert "dispatch.fallback" in event_types
    assert event_types[-1] == "dispatch.verified"


def test_dispatch_role_blocks_when_provider_returns_unverified_start(tmp_path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = UnverifiedStartProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )

    asyncio.run(service.dispatch_role(task.id, "director"))

    detail = service.get_task(task.id)
    director = next(job for job in detail.role_jobs if job.role == "director")
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert detail.task.status == "blocked"
    assert director.status == "blocked"
    assert director.dispatch_verified is False
    assert "dispatch.fallback" in event_types
    assert "dispatch.verified" not in event_types


def test_native_runtime_completion_advances_without_sse_request(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation and testing required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "tester"],
              "acceptance_criteria": ["implemented", "tested"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(service.dispatch_role(task.id, "implementer"))

    asyncio.run(
        service.handle_runtime_event(
            RuntimeEvent(
                id=55,
                schema_version=1,
                event_type=EventType.MODEL_MESSAGE_COMPLETED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={
                    "text": """
                    {
                      "status": "passed",
                      "reason": "implemented",
                      "role": "implementer",
                      "artifact_type": "implementation_report",
                      "handoff_to": "tester",
                      "summary": "Implementation ready",
                      "evidence_refs": ["x"],
                      "open_questions": [],
                      "next_action": "test"
                    }
                    """
                },
                occurred_at=now_iso(),
                agent_run_id=101,
            )
        )
    )

    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["implementer"].status == "passed"
    assert jobs["tester"].status == "streaming"
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert "role.envelope" in event_types
    envelope_event = next(
        event
        for event in service.events_for_task(task.id)
        if event.event_type == "role.envelope" and event.role == "implementer"
    )
    assert envelope_event.payload["display_text"].startswith("结论：该角色已返回结构化结果")
    assert "handoff.created" in event_types
    assert "role.queued" in event_types
    assert "dispatch.verified" in event_types
    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]


def test_intermediate_message_completed_without_envelope_waits_for_final_output(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation and testing required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "tester"],
              "acceptance_criteria": ["implemented", "tested"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(service.dispatch_role(task.id, "implementer"))

    asyncio.run(
        service.handle_runtime_event(
            RuntimeEvent(
                id=54,
                schema_version=1,
                event_type=EventType.MODEL_MESSAGE_COMPLETED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={"text": "Now let me inspect the relay source files first."},
                occurred_at=now_iso(),
                agent_run_id=101,
            )
        )
    )

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    events = service.events_for_task(task.id)
    assert detail.task.status == "running"
    assert jobs["implementer"].status == "streaming"
    assert jobs["implementer"].error_message == ""
    assert not any(
        artifact.get("artifact_type") == "role_error"
        for artifact in detail.artifacts
        if artifact.get("role") == "implementer"
    )
    assert not any(
        event.event_type == "role.envelope" and event.role == "implementer" for event in events
    )
    assert [call[0] for call in provider.calls] == ["start_session"]


def test_runtime_completion_replay_after_service_restart_does_not_dispatch_again(
    tmp_path,
) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = FakeProvider()
    store = RelayStore(ledger)
    service = RelayService(
        store=store,
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "implementation and testing required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Core relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "implement",
              "complexity": "medium",
              "risk": "medium",
              "route": "core_relay",
              "required_roles": ["director", "implementer", "tester"],
              "acceptance_criteria": ["implemented", "tested"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(service.dispatch_role(task.id, "implementer"))
    runtime_event = RuntimeEvent(
        id=55,
        schema_version=1,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id="101",
        correlation_id="corr-101",
        source=EventSource.CLAUDE,
        actor="claude",
        visibility=Visibility.USER,
        payload={
            "text": """
            {
              "status": "passed",
              "reason": "implemented",
              "role": "implementer",
              "artifact_type": "implementation_report",
              "handoff_to": "tester",
              "summary": "Implementation ready",
              "evidence_refs": ["x"],
              "open_questions": [],
              "next_action": "test"
            }
            """
        },
        occurred_at=now_iso(),
        agent_run_id=101,
    )
    asyncio.run(service.handle_runtime_event(runtime_event))
    restarted = RelayService(
        store=store,
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
        events=service._events,
    )

    asyncio.run(restarted.handle_runtime_event(runtime_event))

    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]
    implementer_handoffs = [
        event
        for event in restarted.events_for_task(task.id)
        if event.event_type == "handoff.created" and event.role == "implementer"
    ]
    assert len(implementer_handoffs) == 1


def test_runtime_delta_is_projected_to_relay_event_bus(tmp_path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    service.project_runtime_event(
        RuntimeEvent(
            id=56,
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "hello from director"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    event = service.events_for_task(task.id)[-1]
    assert event.event_type == "role.output_delta"
    assert event.role == "director"
    assert event.payload["delta"] == "hello from director"


def test_project_runtime_event_marks_role_blocked_when_background_handler_fails(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "implementer"))

    class BrokenCompletionService(RelayService):
        async def handle_role_completion_event(self, *args: Any, **kwargs: Any):
            raise RuntimeError("completion projector failed")

    broken = BrokenCompletionService(
        store=service._store,
        registry=service._registry,
        default_provider="claude",
        events=service._events,
    )

    async def run_projector() -> None:
        broken.project_runtime_event(
            RuntimeEvent(
                id=57,
                schema_version=1,
                event_type=EventType.MODEL_MESSAGE_COMPLETED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="101",
                correlation_id="corr-101",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={
                    "text": """
                    {
                      "status": "passed",
                      "reason": "implemented",
                      "role": "implementer",
                      "artifact_type": "implementation_report",
                      "handoff_to": "tester",
                      "summary": "Implementation ready",
                      "evidence_refs": ["x"],
                      "open_questions": [],
                      "next_action": "test"
                    }
                    """
                },
                occurred_at=now_iso(),
                agent_run_id=101,
            )
        )
        await asyncio.sleep(0)

    asyncio.run(run_projector())

    detail = broken.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "blocked"
    assert jobs["implementer"].status == "blocked"
    assert broken.events_for_task(task.id)[-1].event_type == "role.status"
    assert broken.events_for_task(task.id)[-1].payload["status"] == "blocked"


def test_auditor_passed_returns_to_director_then_final_summary_completes_task(
    tmp_path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "full relay required",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Use full relay.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "architect plan",
              "complexity": "complex",
              "risk": "medium",
              "route": "full_relay",
              "required_roles": ["director", "architect", "implementer", "tester", "auditor"],
              "acceptance_criteria": ["all roles passed"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    for relay_role in ("architect", "implementer", "tester"):
        service._store.update_role_status(task.id, relay_role, "passed")

    asyncio.run(
        service.handle_role_output(
            task.id,
            "auditor",
            """
            {
              "status": "passed",
              "reason": "audit passed",
              "role": "auditor",
              "artifact_type": "audit_report",
              "handoff_to": "director",
              "summary": "Ready for final summary",
              "evidence_refs": ["audit"],
              "open_questions": [],
              "next_action": "summarize"
            }
            """,
            dispatch_next=False,
        )
    )

    assert {job.role: job.status for job in service.get_task(task.id).role_jobs}[
        "director"
    ] == "queued"

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "final summary accepted",
              "role": "director",
              "artifact_type": "final_summary",
              "handoff_to": "",
              "summary": "Relay task complete",
              "evidence_refs": ["summary"],
              "open_questions": [],
              "next_action": "complete task"
            }
            """,
        )
    )

    detail = service.get_task(task.id)
    assert detail.task.status == "completed"
    assert service.events_for_task(task.id)[-1].event_type == "task.completed"


def test_single_role_interrupt_stops_native_session_and_interrupts_task_when_no_roles_active(
    tmp_path,
) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(service.interrupt(task.id, role="director"))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "interrupted"
    assert jobs["director"].status == "interrupted"
    assert jobs["architect"].status == "idle"
    assert ("interrupt_session", "native-1", "") in provider.calls


def test_interrupt_completed_task_is_noop(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "done",
              "role": "director",
              "artifact_type": "routing_decision",
              "handoff_to": "",
              "summary": "Direct answer.",
              "evidence_refs": [],
              "open_questions": [],
              "next_action": "complete directly",
              "complexity": "low",
              "risk": "low",
              "route": "director_only",
              "required_roles": ["director"],
              "acceptance_criteria": ["answer"],
              "stop_conditions": [],
              "requires_user_approval": false
            }
            """,
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            """
            {
              "status": "passed",
              "reason": "final summary accepted",
              "role": "director",
              "artifact_type": "final_summary",
              "handoff_to": "",
              "summary": "Relay task complete",
              "evidence_refs": ["summary"],
              "open_questions": [],
              "next_action": "complete task"
            }
            """,
        )
    )

    asyncio.run(service.interrupt(task.id))

    detail = service.get_task(task.id)
    event_types = [event.event_type for event in service.events_for_task(task.id)]
    assert detail.task.status == "completed"
    assert event_types[-1] == "task.completed"
    assert "task.interrupted" not in event_types
    assert not any(call[0] == "interrupt_session" for call in provider.calls)


def test_interrupt_passed_role_is_noop(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_role_status(task.id, "director", "passed")

    asyncio.run(service.interrupt(task.id, role="director"))

    detail = service.get_task(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert jobs["director"].status == "passed"
    assert not any(call[0] == "interrupt_session" for call in provider.calls)
