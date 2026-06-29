import asyncio
import json
from pathlib import Path
from typing import Any

from wlcodex.db import Ledger
from wlcodex.native_agents.models import NativeAgentCapabilities, NativeAgentControlResult
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.lifecycle import RelayLifecycleStore
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

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.next_agent_run_id = 101

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_start_session=True,
            can_continue_session=True,
        )

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="native-director",
            agent_run_id=self.next_agent_run_id,
            status="started",
            turn_id="turn-director",
            active_turn_id="turn-director",
            turn_running=True,
        )

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs: Any):
        self.calls.append(("continue_session", native_session_id, prompt, kwargs))
        self.next_agent_run_id += 1
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=self.next_agent_run_id,
            status="continued",
            turn_id=f"turn-{self.next_agent_run_id}",
            active_turn_id=f"turn-{self.next_agent_run_id}",
            turn_running=True,
        )


def _service(tmp_path: Path) -> tuple[RelayService, FakeProvider]:
    ledger = Ledger.open(tmp_path / "relay.sqlite3")
    ledger.migrate()
    provider = FakeProvider()
    registry = NativeAgentRegistry([provider])
    store = RelayStore(ledger)
    return (
        RelayService(
            store=store,
            registry=registry,
            default_provider="claude",
        ),
        provider,
    )


def _round_rows(service: RelayService) -> list[dict[str, Any]]:
    rows = service._store._ledger._conn.execute(
        "SELECT * FROM relay_rounds ORDER BY team_run_id, round_id"
    ).fetchall()
    return [dict(row) for row in rows]


def _attempt_rows(service: RelayService) -> list[dict[str, Any]]:
    rows = service._store._ledger._conn.execute(
        """
        SELECT * FROM relay_role_attempts
        ORDER BY team_run_id, round_id, role, attempt_no
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _routing_delta(agent_run_id: int, turn_id: str, *, summary: str) -> RuntimeEvent:
    output = json.dumps(
        {
            "status": "passed",
            "reason": "需要先审计 durable runtime evidence。",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "auditor",
            "summary": summary,
            "evidence_refs": ["runtime_events"],
            "open_questions": [],
            "next_action": "派 auditor 审计当前证据。",
            "complexity": "medium",
            "risk": "low",
            "route": "audit_first",
            "required_roles": ["director", "auditor"],
            "acceptance_criteria": ["使用 durable runtime evidence"],
            "stop_conditions": ["没有证据时阻塞"],
            "requires_user_approval": False,
        },
        ensure_ascii=False,
    )
    return RuntimeEvent(
        schema_version=1,
        event_type=EventType.MODEL_TEXT_DELTA,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id=str(agent_run_id),
        correlation_id=f"corr-{agent_run_id}",
        source=EventSource.CLAUDE,
        actor="claude",
        visibility=Visibility.USER,
        payload={"delta": output, "native_turn_id": turn_id},
        occurred_at=now_iso(),
        agent_run_id=agent_run_id,
    )


def test_lifecycle_tables_are_created_by_migration(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "relay.sqlite3")
    ledger.migrate()

    tables = {
        str(row["name"])
        for row in ledger._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert "relay_rounds" in tables
    assert "relay_role_attempts" in tables


def test_create_task_creates_round_one_and_director_attempt(tmp_path: Path) -> None:
    service, _provider = _service(tmp_path)

    task = service.create_task(
        title="Lifecycle relay",
        prompt="Implement lifecycle",
        workspace="/repo",
        provider="claude",
    )

    detail = service.get_task(task.id)
    rounds = _round_rows(service)
    attempts = _attempt_rows(service)
    director = next(job for job in detail.role_jobs if job.role == "director")
    auditor = next(job for job in detail.role_jobs if job.role == "auditor")

    assert detail.current_round_id == 1
    assert detail.task.status == "running"
    assert rounds == [
        {
            **rounds[0],
            "team_run_id": task.id,
            "round_id": 1,
            "status": "running",
            "trigger_kind": "initial",
        }
    ]
    assert attempts[0]["team_run_id"] == task.id
    assert attempts[0]["round_id"] == 1
    assert attempts[0]["role"] == "director"
    assert attempts[0]["attempt_no"] == 1
    assert attempts[0]["status"] == "queued"
    assert director.status == "queued"
    assert auditor.status == "idle"


def test_followup_supersedes_previous_round_and_opens_clean_director_round(
    tmp_path: Path,
) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Lifecycle relay",
        prompt="Initial task",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    asyncio.run(service.add_user_message(task.id, "继续新的需求"))

    detail = service.get_task(task.id)
    rounds = _round_rows(service)
    attempts = _attempt_rows(service)
    active_attempts = [
        attempt
        for attempt in attempts
        if attempt["team_run_id"] == task.id and attempt["round_id"] == 2
    ]
    director = next(job for job in detail.role_jobs if job.role == "director")

    assert [(row["round_id"], row["status"]) for row in rounds] == [
        (1, "superseded"),
        (2, "running"),
    ]
    assert detail.current_round_id == 2
    assert detail.task.status == "running"
    assert active_attempts[0]["role"] == "director"
    assert active_attempts[0]["status"] in {"queued", "streaming"}
    assert director.status in {"queued", "streaming"}


def test_audit_first_routing_decision_projects_required_roles(tmp_path: Path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Lifecycle relay",
        prompt="审计 token 消耗",
        workspace="/repo",
        provider="claude",
    )
    output = json.dumps(
        {
            "status": "passed",
            "reason": "先审计证据。",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "auditor",
            "summary": "进入 audit_first。",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给 auditor。",
            "complexity": "medium",
            "risk": "low",
            "route": "audit_first",
            "required_roles": ["director", "auditor"],
            "acceptance_criteria": ["有真实证据"],
            "stop_conditions": [],
            "requires_user_approval": False,
        },
        ensure_ascii=False,
    )

    asyncio.run(service.handle_role_output(task.id, "director", output))

    detail = service.get_task(task.id)
    attempts = _attempt_rows(service)
    director_attempt = next(
        attempt
        for attempt in attempts
        if attempt["round_id"] == 1 and attempt["role"] == "director"
    )
    auditor_attempt = next(
        attempt
        for attempt in attempts
        if attempt["round_id"] == 1 and attempt["role"] == "auditor"
    )
    jobs = {job.role: job for job in detail.role_jobs}
    current_round = _round_rows(service)[0]

    assert current_round["status"] == "running"
    assert json.loads(current_round["required_roles_json"]) == ["director", "auditor"]
    assert current_round["route"] == "audit_first"
    assert director_attempt["status"] == "passed"
    assert auditor_attempt["status"] == "streaming"
    assert jobs["director"].status == "passed"
    assert jobs["auditor"].status == "streaming"
    assert [call[0] for call in provider.calls] == ["start_session"]


def test_idle_read_model_does_not_write_idle_attempt_status(tmp_path: Path) -> None:
    service, _provider = _service(tmp_path)
    task = service.create_task(
        title="Lifecycle relay",
        prompt="审计 token 消耗",
        workspace="/repo",
        provider="claude",
    )

    service._store.update_role_status(task.id, "auditor", "queued")
    service._store.update_role_status(task.id, "auditor", "idle")

    detail = service.get_task(task.id)
    attempts = [
        attempt
        for attempt in _attempt_rows(service)
        if attempt["round_id"] == 1 and attempt["role"] == "auditor"
    ]
    auditor = next(job for job in detail.role_jobs if job.role == "auditor")

    assert auditor.status == "idle"
    assert attempts[-1]["status"] == "superseded"
    assert "idle" not in {attempt["status"] for attempt in attempts}


def test_reconcile_recovers_late_valid_delta_for_current_round(tmp_path: Path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Lifecycle relay",
        prompt="审计 token 消耗",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    detail = service.get_task(task.id)
    director = next(job for job in detail.role_jobs if job.role == "director")
    service._store.update_role_status(task.id, "director", "blocked")
    service._store.update_task_status(task.id, "blocked")
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "error": "invalid json",
            "output": "{not-json",
            "retry_kind": "format",
            "round_id": detail.current_round_id,
        },
        summary="invalid json",
    )
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    completed = runtime_store.append(
        _routing_delta(
            int(director.agent_run_id or 0),
            director.active_turn_id or director.turn_id,
            summary="从 durable delta 恢复 routing decision。",
        )
    )

    changed = asyncio.run(service.ensure_task_lifecycle_current(task.id, runtime_store))

    refreshed = service.get_task(task.id)
    jobs = {job.role: job for job in refreshed.role_jobs}
    director_attempt = next(
        attempt
        for attempt in _attempt_rows(service)
        if attempt["round_id"] == 1 and attempt["role"] == "director"
    )
    routing = [
        artifact
        for artifact in refreshed.artifacts
        if artifact.get("artifact_type") == "routing_decision"
    ]

    assert changed is True
    assert refreshed.task.status == "running"
    assert jobs["director"].status == "passed"
    assert jobs["auditor"].status == "streaming"
    assert director_attempt["completion_event_id"] == completed.id
    assert routing[-1]["runtime_event_id"] == completed.id
    assert routing[-1]["summary"] == "从 durable delta 恢复 routing decision。"
    assert [call[0] for call in provider.calls] == ["start_session", "start_session"]


def test_reconcile_ignores_late_valid_delta_from_superseded_round(tmp_path: Path) -> None:
    service, provider = _service(tmp_path)
    task = service.create_task(
        title="Lifecycle relay",
        prompt="初始问题",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))
    first_detail = service.get_task(task.id)
    first_director = next(job for job in first_detail.role_jobs if job.role == "director")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    runtime_store.append(
        _routing_delta(
            int(first_director.agent_run_id or 0),
            first_director.active_turn_id or first_director.turn_id,
            summary="旧 round 的 late delta 不应复活。",
        )
    )

    asyncio.run(service.add_user_message(task.id, "新的接续问题"))
    changed = asyncio.run(service.ensure_task_lifecycle_current(task.id, runtime_store))

    refreshed = service.get_task(task.id)
    routing = [
        artifact
        for artifact in refreshed.artifacts
        if artifact.get("artifact_type") == "routing_decision"
    ]
    jobs = {job.role: job for job in refreshed.role_jobs}

    assert changed is False
    assert refreshed.current_round_id == 2
    assert routing == []
    assert jobs["director"].status == "streaming"
    assert jobs["auditor"].status == "idle"
    assert [call[0] for call in provider.calls] == ["start_session", "continue_session"]
