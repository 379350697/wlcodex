import asyncio
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
from wlcodex.relay.service import RelayService
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


class FakeCodexProvider(FakeProvider):
    provider = "codex"
    provider_engine = "app-server"


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
        self.calls.append(
            ("steer_session", native_session_id, expected_turn_id, prompt, kwargs)
        )
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
    implementer = next(job for job in service.get_task(task.id).role_jobs if job.role == "implementer")
    assert implementer.provider == "antigravity"
    assert implementer.native_session_id == "native-1"


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

    result = asyncio.run(
        service.handle_role_output(task.id, "implementer", '{"status": "passed"}')
    )

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


def test_malformed_director_routing_recovers_explicit_full_relay(tmp_path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt=(
            "请按完整五角色接力流程审查，不要修改任何文件，不要提交，不要部署。"
            "总工程师、架构工程师、开发工程师、测试工程师、审计工程师都要参与。"
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
    assert result.ok is False
    assert detail.task.status == "running"
    assert detail.routing_decision is not None
    assert detail.routing_decision["route"] == "full_relay"
    assert detail.routing_decision["required_roles"] == [
        "director",
        "architect",
        "implementer",
        "tester",
        "auditor",
    ]
    assert jobs["architect"].status == "streaming"
    assert provider.calls[-1][0] == "start_session"
    assert "role: architect" in provider.calls[-1][2]


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
        "handoff_to 审计工程师 is not in routing_decision.required_roles"
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
    assert jobs["auditor"].status == "idle"
    assert jobs["director"].status == "streaming"
    assert provider.calls[-1][0] == "start_session"
    assert "role: director" in provider.calls[-1][2]


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

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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

    changed = asyncio.run(
        service.scan_active_native_runtime_events(runtime_store, limit=20)
    )

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
    assert jobs["architect"].error_message == (
        "native provider completed without assistant output"
    )
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
    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]
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
    assert jobs["auditor"].idle_reason == "未纳入本轮路线"


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
    assert '"route": "director_only|core_relay|full_relay|audit_first|waiting_user|blocked"' in prompt
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
        event.event_type == "role.envelope" and event.role == "implementer"
        for event in events
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


def test_single_role_interrupt_stops_native_session_without_interrupting_whole_task(
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
    assert detail.task.status == "running"
    assert jobs["director"].status == "interrupted"
    assert jobs["architect"].status == "idle"
    assert ("interrupt_session", "native-1", "") in provider.calls
