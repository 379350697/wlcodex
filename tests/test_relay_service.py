import asyncio
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
              "next_action": "wait for user"
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
    assert [
        event.event_type
        for event in restarted.events_for_task(task.id)
        if event.event_type == "handoff.created"
    ] == ["handoff.created"]


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
                payload={"text": "{}"},
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
