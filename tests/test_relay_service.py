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
from wlcodex.relay.service import RelayService, _plain_followup_visible_text
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
    implementer = next(job for job in service.get_task(task.id).role_jobs if job.role == "implementer")
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


def test_followup_malformed_routing_envelope_recovers_instead_of_plain_response(
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
        'routecore_relayrequired_rolesdirectorimplementerauditor'
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
    followup_responses = [
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "followup_response"
    ]
    assert followup_responses == []
    assert detail.task.status == "running"
    assert detail.routing_decision is not None
    assert detail.routing_decision["route"] == "core_relay"
    assert detail.routing_decision["required_roles"] == [
        "director",
        "implementer",
        "auditor",
    ]
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"
    assert any(call[0] == "start_session" for call in provider.calls)
    assert any(
        artifact.get("artifact_type") == "role_error"
        and artifact.get("relay_role") == "director"
        and artifact.get("recovered_as") == "routing_decision"
        for artifact in detail.artifacts
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
            "turns": [
                {"role": "assistant", "text": completed_text, "native_turn_id": "turn-1"}
            ],
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


def test_followup_malformed_read_session_protocol_recovers_not_plain_response(
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
        'routecore_relayrequired_rolesdirectorimplementerauditor'
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
    assert followup_responses == []
    assert detail.task.status == "running"
    assert detail.routing_decision is not None
    assert detail.routing_decision["route"] == "core_relay"
    assert detail.routing_decision["required_roles"] == [
        "director",
        "implementer",
        "auditor",
    ]
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"


def test_plain_followup_visible_text_extracts_fused_protocol_summary() -> None:
    text = (
        '{"artifact_type":"final_summary","evidence_refs":[],"handoff_to":"",'
        '"next_actionopen_questionsreason接续验证。'
        'roledirectorstatuspassedsummary已修复"}'
    )

    assert _plain_followup_visible_text(text) == "已修复"


def test_plain_followup_visible_text_prefers_readable_text_over_fragmented_summary() -> None:
    text = (
        "我会先修输入框对齐，再替换底部导航图标。"
        "CSS 已经收口，旧伪元素已删除，编译确认通过。"
        '{"artifact_type":"final_summary","evidence_refs":["python -m py_compile"],'
        '"handoff_to":"","next_actionopen_questions":[],"reason完成交互本地'
        'roledirectorstatuspassedsummary区“请输入”椭圆麦克风 风格为空可更新 缓存版本"}'
    )

    assert _plain_followup_visible_text(text) == "CSS 已经收口，旧伪元素已删除，编译确认通过。"


def test_plain_followup_visible_text_uses_result_tail_before_protocol_payload() -> None:
    text = (
        "我会先修输入框对齐。"
        "中间过程文字很多，没有完整断句"
        "CSS 已经收口形mar-relay-nav-icon-chatclock/tool/person已删编译确认确实提交双数据"
        '{"artifact_type":"final_summary","evidence_refs":[],'
        '"reason完成交互本地roledirectorstatuspassedsummary区“请输入”椭圆麦克风"}'
    )

    assert _plain_followup_visible_text(text).startswith("CSS 已经收口")


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

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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
            "turns": [
                {"role": "assistant", "text": completed_text, "native_turn_id": "turn-1"}
            ],
        }

    provider.read_session = read_session

    changed = asyncio.run(
        service.scan_stale_native_roles(max_idle_seconds=300, now=now)
    )

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


def test_implementer_frontend_patch_envelope_normalizes_to_implementation_report(
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
    assert jobs["implementer"].status == "passed"
    assert jobs["tester"].status == "streaming"
    assert implementation
    assert implementation[-1]["summary"] == "已添加工作区显示和选择入口。"
    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]


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


def test_codex_native_turn_completed_recovers_core_relay_routing_decision(
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
    assert detail.routing_decision is not None
    assert detail.routing_decision["route"] == "core_relay"
    assert detail.routing_decision["required_roles"] == [
        "director",
        "implementer",
        "auditor",
    ]
    assert jobs["director"].status == "passed"
    assert jobs["implementer"].status == "streaming"
    assert any(
        artifact.get("artifact_type") == "role_error"
        and "已按明确语义恢复路由" in str(artifact.get("summary") or "")
        for artifact in detail.artifacts
    )


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
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "audit_report"
    ]
    assert changed is True
    assert jobs["auditor"].status == "passed"
    assert audit_reports[-1]["summary"] == "审核通过，可以交付。"


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
