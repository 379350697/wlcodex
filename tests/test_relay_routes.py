from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer
from wlcodex.native_agents.models import NativeAgentCapabilities
from wlcodex.native_agents.models import NativeAgentControlResult
from wlcodex.native_agents.provider import NativeAgentRegistry
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

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_start_session=True,
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


class SlowProvider(FakeProvider):
    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        await asyncio.sleep(30)
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="native-slow",
            agent_run_id=901,
            status="started",
        )


async def _read_response(host: str, port: int, request: str) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request.encode("utf-8"))
    await writer.drain()
    chunks: list[bytes] = []
    while True:
        chunk = await asyncio.wait_for(reader.read(65536), timeout=1.0)
        if not chunk:
            break
        chunks.append(chunk)
    writer.close()
    await writer.wait_closed()
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _read_until(
    host: str,
    port: int,
    request: str,
    needle: str,
    *,
    timeout: float = 1.0,
) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request.encode("utf-8"))
    await writer.drain()
    chunks: list[bytes] = []
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            if not chunk:
                break
            chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8", errors="replace")
            if needle in text:
                return text
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=0.1)
        except TimeoutError:
            pass


def _json_body(response: str) -> dict[str, Any]:
    return json.loads(response.split("\r\n\r\n", 1)[1])


def _relay_service(tmp_path: Path) -> RelayService:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    registry = NativeAgentRegistry([
        FakeCodexProvider(),
        FakeProvider(),
        FakeAntigravityProvider(),
    ])
    return RelayService(
        store=RelayStore(ledger),
        registry=registry,
        default_provider="claude",
    )


def _active_relay_service(tmp_path: Path) -> tuple[RelayService, ActiveTurnProvider]:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = ActiveTurnProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    return service, provider


def _slow_relay_service(tmp_path: Path) -> tuple[RelayService, SlowProvider]:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = SlowProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    return service, provider


async def _request_relay(
    tmp_path: Path,
    request: str,
    *,
    relay_service: RelayService | None = None,
    access_token: str | None = None,
    allow_unauthenticated_loopback: bool = True,
) -> str:
    service = relay_service or _relay_service(tmp_path)
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_registry=service._registry,
        relay_service=service,
        access_token=access_token,
        allow_unauthenticated_loopback=allow_unauthenticated_loopback,
    )
    await server.start()
    try:
        return await _read_response(server.host, server.port, request)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_create_and_get_relay_task_routes(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "title": "Build relay",
            "prompt": "Implement the task workspace",
            "workspace": "/repo",
            "provider": "claude",
            "execution_mode": "goal",
            "execution_goal": "ship relay lifecycle",
            "allow_subagents": "auto",
        }
    )
    service = _relay_service(tmp_path)
    response = await _request_relay(
        tmp_path,
        "POST /api/relay/tasks HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["task"]["title"] == "Build relay"
    assert payload["task"]["provider"] == "claude"

    task_id = payload["task"]["id"]
    created_detail = service.get_task(task_id)
    director = next(job for job in created_detail.role_jobs if job.role == "director")
    architect = next(job for job in created_detail.role_jobs if job.role == "architect")
    assert director.status == "streaming"
    assert director.native_session_id == "native-1"
    assert architect.status == "idle"
    round_execution = service._store.lifecycle.round_execution(task_id, 1)
    assert round_execution["execution_mode"] == "goal"
    assert round_execution["execution_goal"] == "ship relay lifecycle"
    assert round_execution["execution_strategy"]["allow_subagents"] == "auto"
    assert round_execution["execution_strategy"]["subagent_decision_json"]["provider"] == "claude"
    assert (
        round_execution["execution_strategy"]["subagent_decision_json"]["capability"]
        == "builtin_subagents"
    )
    assert "team_strategy" not in round_execution["execution_strategy"]

    get_response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task_id} HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in get_response
    detail = _json_body(get_response)
    assert detail["task"]["id"] == task_id
    assert [job["role"] for job in detail["role_jobs"]] == [
        "director",
        "architect",
        "implementer",
        "tester",
        "auditor",
    ]


@pytest.mark.asyncio
async def test_relay_inputs_route_queues_pending_input(tmp_path: Path) -> None:
    service, provider = _active_relay_service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build relay",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    body = json.dumps({"text": "queue this next"})

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/inputs HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["pending_input"]["status"] == "pending"
    assert payload["pending_input"]["queued_after_round_id"] == 1
    assert _json_body(
        await _request_relay(
            tmp_path,
            f"GET /api/relay/tasks/{task.id} HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
            relay_service=service,
        )
    )["pending_inputs"][0]["text"] == "queue this next"
    assert [call[0] for call in provider.calls] == ["start_session"]


@pytest.mark.asyncio
async def test_relay_pending_input_steer_route_guides_active_attempt(tmp_path: Path) -> None:
    service, provider = _active_relay_service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build relay",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    pending = service.queue_user_input(task.id, "guide now")

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/inputs/{pending.id}/steer HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 2\r\n"
        "Connection: close\r\n\r\n{}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["pending_input"]["status"] == "steered"
    assert payload["pending_input"]["steered_role"] == "director"
    assert payload["pending_input"]["guidance_artifact_id"]
    assert [call[0] for call in provider.calls] == ["start_session", "steer_session"]


@pytest.mark.asyncio
async def test_relay_inputs_route_followup_when_task_no_longer_running(tmp_path: Path) -> None:
    service, provider = _active_relay_service(tmp_path)
    task = service.create_task(
        title="Relay",
        prompt="Build relay",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_task_status(task.id, "completed")
    body = json.dumps({"text": "start the next round now"})

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/inputs HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["disposition"] == "followup"
    assert payload["followup"]["text"] == "start the next round now"
    assert service.get_task(task.id).current_round_id == 2
    assert service.get_task(task.id).task.status == "running"
    assert [call[0] for call in provider.calls] == ["start_session"]


@pytest.mark.asyncio
async def test_relay_round_control_approves_plan_in_current_round(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)
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
    detail = service.get_task(task.id)
    plan_artifact = next(
        artifact
        for artifact in reversed(detail.artifacts)
        if artifact["artifact_type"] == "architecture_plan"
    )
    body = json.dumps({"decision": "approve_plan", "artifact_id": plan_artifact["id"]})

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/rounds/{detail.current_round_id}/control HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    refreshed = service.get_task(task.id)
    assert refreshed.current_round_id == detail.current_round_id
    assert next(job for job in refreshed.role_jobs if job.role == "implementer").status in {
        "queued",
        "streaming",
    }


@pytest.mark.asyncio
async def test_relay_round_control_returns_before_provider_dispatch_finishes(
    tmp_path: Path,
) -> None:
    service, provider = _slow_relay_service(tmp_path)
    task = service.create_task(
        title="Waiting control",
        prompt="Need confirmation",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "waiting",
                "reason": "needs user input",
                "role": "director",
                "artifact_type": "routing_decision",
                "handoff_to": "",
                "summary": "Confirm before running.",
                "evidence_refs": [],
                "open_questions": ["Continue?"],
                "next_action": "wait for user",
                "complexity": "standard",
                "risk": "medium",
                "route": "waiting_user",
                "required_roles": ["director"],
                "acceptance_criteria": ["confirmed"],
                "stop_conditions": [],
                "requires_user_approval": True,
            }
        ),
        dispatch_next=False,
    )
    body = json.dumps({"decision": "continue"})

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/rounds/1/control HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["control"]["decision"] == "continue"
    assert payload["control"]["role"] == "director"
    assert len(provider.calls) == 1
    assert provider.calls[0][0] == "start_session"
    assert provider.calls[0][1] == "/repo"


@pytest.mark.asyncio
async def test_relay_config_routes_persist_role_provider_assignments(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)

    get_response = await _request_relay(
        tmp_path,
        "GET /api/relay/config HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in get_response
    config = _json_body(get_response)
    assert [role["role"] for role in config["roles"]] == [
        "director",
        "architect",
        "implementer",
        "tester",
        "auditor",
    ]
    assert {provider["provider"] for provider in config["providers"]} == {
        "codex",
        "claude",
        "antigravity",
    }
    assert config["assignments"]["director"] in {"codex", "claude"}

    body = json.dumps(
        {
            "assignments": {
                "director": "codex",
                "architect": "claude",
                "implementer": "antigravity",
                "tester": "claude",
                "auditor": "codex",
            }
        }
    )
    post_response = await _request_relay(
        tmp_path,
        "POST /api/relay/config HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in post_response
    updated = _json_body(post_response)
    assert updated["assignments"]["implementer"] == "antigravity"

    create_body = json.dumps(
        {
            "title": "Configured relay",
            "prompt": "Use role providers",
            "workspace": "/repo",
        }
    )
    create_response = await _request_relay(
        tmp_path,
        "POST /api/relay/tasks HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(create_body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{create_body}",
        relay_service=service,
    )
    payload = _json_body(create_response)
    detail = service.get_task(payload["task"]["id"])
    assert {job.role: job.provider for job in detail.role_jobs} == updated["assignments"]


@pytest.mark.asyncio
async def test_relay_config_route_rejects_unknown_provider(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)
    body = json.dumps({"assignments": {"director": "missing"}})

    response = await _request_relay(
        tmp_path,
        "POST /api/relay/config HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 400 Bad Request" in response


@pytest.mark.asyncio
async def test_relay_config_route_rejects_unknown_role(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)
    body = json.dumps({"assignments": {"investigator": "codex"}})

    response = await _request_relay(
        tmp_path,
        "POST /api/relay/config HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 400 Bad Request" in response
    assert "unknown relay role: investigator" in response


@pytest.mark.asyncio
async def test_create_relay_task_accepts_form_encoded_ui_submit(
    tmp_path: Path,
) -> None:
    body = (
        "title=Form+relay&prompt=Implement+from+form&workspace=%2Frepo"
        "&provider=claude"
    )
    service = _relay_service(tmp_path)

    response = await _request_relay(
        tmp_path,
        "POST /api/relay/tasks HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["task"]["title"] == "Form relay"
    assert payload["task"]["workspace"] == "/repo"
    assert payload["task"]["provider"] == "claude"


@pytest.mark.asyncio
async def test_create_relay_task_rejects_empty_workspace(tmp_path: Path) -> None:
    body = json.dumps({"title": "No workspace", "prompt": "Missing cwd"})

    response = await _request_relay(
        tmp_path,
        "POST /api/relay/tasks HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
    )

    assert "HTTP/1.1 400 Bad Request" in response
    assert _json_body(response) == {"error": "relay task workspace is required"}


@pytest.mark.asyncio
async def test_relay_message_route_accepts_image_and_text_file_attachments(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    provider = FakeProvider()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([provider]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Attachment follow-up",
        prompt="Initial",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    body = json.dumps(
        {
            "text": "继续看附件",
            "images": [
                {
                    "filename": "screen.png",
                    "mime_type": "image/png",
                    "url": "data:image/png;base64,aGVsbG8=",
                }
            ],
            "files": [
                {
                    "filename": "note.md",
                    "mime_type": "text/markdown",
                    "text": "# title\nbody",
                    "size": 12,
                }
            ],
        }
    )

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/message HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    assert provider.calls[-1][0] == "continue_session"
    assert provider.calls[-1][3]["images"][0]["filename"] == "screen.png"
    assert "note.md" in provider.calls[-1][2]
    assert "# title" in provider.calls[-1][2]
    payload = _json_body(response)
    followups = [
        artifact
        for artifact in payload["artifacts"]
        if artifact["artifact_type"] == "user_followup"
    ]
    assert followups[-1]["files"][0]["filename"] == "note.md"


@pytest.mark.asyncio
async def test_relay_run_aliases_match_task_routes(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Alias task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )

    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/runs/{task.id} HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["task"]["id"] == task.id


@pytest.mark.asyncio
async def test_relay_events_stream_includes_lane_metadata(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )

    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "Content-Type: text/event-stream; charset=utf-8" in response
    assert 'event: task.created' in response
    assert '"role": "director"' in response
    assert '"sequence": 2' in response


@pytest.mark.asyncio
async def test_relay_events_stream_replays_persisted_events_after_service_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([FakeProvider()]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Replay task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._events.emit(
        task.id,
        "routing.decision",
        role="director",
        payload={"route": "director_only"},
    )

    restarted_ledger = Ledger.open(db_path)
    restarted_ledger.migrate()
    restarted_service = RelayService(
        store=RelayStore(restarted_ledger),
        registry=NativeAgentRegistry([FakeProvider()]),
        default_provider="claude",
    )
    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events?after=1 HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=restarted_service,
    )

    assert "Content-Type: text/event-stream; charset=utf-8" in response
    assert "event: role.queued" in response
    assert "event: routing.decision" in response
    assert "event: task.created" not in response
    assert '"sequence": 3' in response
    assert '"route": "director_only"' in response


@pytest.mark.asyncio
async def test_relay_events_stream_rehydrates_persisted_runtime_delta_after_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([FakeProvider()]),
        default_provider="claude",
    )
    task = service.create_task(
        title="Runtime replay task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    runtime_store = RuntimeEventStore(ledger._conn)
    saved_event = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "durable delta"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )
    service.project_runtime_event(saved_event)

    restarted_ledger = Ledger.open(db_path)
    restarted_ledger.migrate()
    restarted_service = RelayService(
        store=RelayStore(restarted_ledger),
        registry=NativeAgentRegistry([FakeProvider()]),
        default_provider="claude",
    )
    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=restarted_service,
    )

    output_delta = response.split("event: role.output_delta", 1)[1].split("\n\n", 1)[0]
    assert '"runtime_event_id": 1' in output_delta
    assert '"delta": "durable delta"' in output_delta


@pytest.mark.asyncio
async def test_relay_events_stream_maps_native_runtime_delta_to_native_role_event(
    tmp_path: Path,
) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
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
            payload={"delta": "director says hi"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )

    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "event: role.native_event" in response
    assert "id: native-" not in response
    assert '"role": "director"' in response
    assert '"kind": "text_delta"' in response
    assert '"delta": "director says hi"' in response


@pytest.mark.asyncio
async def test_relay_events_snapshot_does_not_duplicate_projected_runtime_delta(
    tmp_path: Path,
) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    saved_event = runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"delta": "single visible delta"},
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )
    service.project_runtime_event(saved_event)

    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert response.count("event: role.output_delta") == 1
    assert response.count("event: role.native_event") == 1
    assert '"delta": "single visible delta"' in response


@pytest.mark.asyncio
async def test_relay_sse_snapshot_accepts_durable_valid_role_completion(
    tmp_path: Path,
) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
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
    await service.dispatch_role(task.id, "implementer")
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
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

    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "event: role.native_event" in response
    assert '"kind": "message_completed"' in response
    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["implementer"].status == "passed"
    assert jobs["tester"].status == "streaming"


@pytest.mark.asyncio
async def test_relay_events_include_dynamic_next_role_lane_after_handoff(
    tmp_path: Path,
) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
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
    await service.dispatch_role(task.id, "implementer")
    await service.handle_role_output(
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
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    service.project_runtime_event(
        runtime_store.append(
            RuntimeEvent(
                id=88,
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id="102",
                correlation_id="corr-102",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={"delta": "tester is verifying"},
                occurred_at=now_iso(),
                agent_run_id=102,
            )
        )
    )

    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "event: handoff.created" in response
    assert "event: role.output_delta" in response
    assert "event: role.native_event" in response
    assert '"role": "tester"' in response
    assert '"kind": "text_delta"' in response
    assert '"delta": "tester is verifying"' in response


@pytest.mark.asyncio
async def test_relay_live_events_do_not_drop_event_emitted_during_snapshot(
    tmp_path: Path,
) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    original_events_for_task = service.events_for_task

    def events_for_task(task_id: int, *, after: int = 0):
        events = original_events_for_task(task_id, after=after)
        service._events.emit(
            task_id,
            "role.output_delta",
            role="director",
            payload={"delta": "event emitted during snapshot"},
        )
        return events

    service.events_for_task = events_for_task  # type: ignore[method-assign]
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_registry=service._registry,
        relay_service=service,
    )
    await server.start()
    try:
        response = await _read_until(
            server.host,
            server.port,
            f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
            "Host: test\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: close\r\n\r\n",
            "event emitted during snapshot",
        )
    finally:
        await server.stop()

    assert "event: role.output_delta" in response
    assert "event emitted during snapshot" in response


@pytest.mark.asyncio
async def test_relay_live_events_subscribe_to_agent_run_created_after_connect(
    tmp_path: Path,
) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    runtime_store = RuntimeEventStore(service._store._ledger._conn)
    hub = WorkerLiveStreamHub(runtime_store)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=hub,
        native_registry=service._registry,
        relay_service=service,
    )
    await server.start()

    async def dispatch_and_publish() -> None:
        await asyncio.sleep(0.05)
        await service.dispatch_role(task.id, "director")
        detail = service.get_task(task.id)
        director_job = next(job for job in detail.role_jobs if job.role == "director")
        agent_run_id = int(director_job.agent_run_id or 0)
        deadline = asyncio.get_running_loop().time() + 0.5
        while (
            agent_run_id
            and hub.subscriber_count(agent_run_id=agent_run_id) == 0
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        saved_event = runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.MODEL_TEXT_DELTA,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=f"corr-{agent_run_id}",
                source=EventSource.CLAUDE,
                actor="claude",
                visibility=Visibility.USER,
                payload={"delta": "late worker delta after connect"},
                occurred_at=now_iso(),
                agent_run_id=agent_run_id,
            )
        )
        hub.publish(saved_event)

    publisher = asyncio.create_task(dispatch_and_publish())
    try:
        response = await _read_until(
            server.host,
            server.port,
            f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
            "Host: test\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: close\r\n\r\n",
            "late worker delta after connect",
            timeout=1.5,
        )
    finally:
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        await server.stop()

    assert "event: role.native_event" in response
    assert "id: native-" not in response
    assert '"role": "director"' in response
    assert '"delta": "late worker delta after connect"' in response


@pytest.mark.asyncio
async def test_relay_message_routes_to_director_and_interrupt_role(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Message task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    body = json.dumps({"text": "new instruction"})

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/message HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    jobs = {job["role"]: job for job in _json_body(response)["role_jobs"]}
    assert jobs["director"]["status"] == "streaming"
    assert jobs["architect"]["status"] == "idle"
    provider = service._registry.get("claude")
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
    ]

    interrupt_body = json.dumps({"role": "director"})
    interrupt_response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/interrupt HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(interrupt_body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{interrupt_body}",
        relay_service=service,
    )
    payload = _json_body(interrupt_response)
    assert payload["task"]["status"] == "interrupted"
    assert {job["role"]: job["status"] for job in payload["role_jobs"]}[
        "director"
    ] == "interrupted"


@pytest.mark.asyncio
async def test_relay_message_accepts_form_encoded_ui_submit(tmp_path: Path) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Message task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await service.dispatch_role(task.id, "director")
    body = "text=form+followup"

    response = await _request_relay(
        tmp_path,
        f"POST /api/relay/tasks/{task.id}/message HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        relay_service=service,
    )

    assert "HTTP/1.1 200 OK" in response
    provider = service._registry.get("claude")
    assert [call[0] for call in provider.calls] == [
        "start_session",
        "continue_session",
    ]
    assert "form followup" in provider.calls[1][2]


@pytest.mark.asyncio
async def test_relay_task_not_found_returns_404(tmp_path: Path) -> None:
    response = await _request_relay(
        tmp_path,
        "GET /api/relay/tasks/9999 HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 404 Not Found" in response
    assert _json_body(response) == {"error": "relay task not found"}
