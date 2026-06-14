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
    provider = FakeProvider()
    registry = NativeAgentRegistry([provider])
    return RelayService(
        store=RelayStore(ledger),
        registry=registry,
        default_provider="claude",
    )


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
async def test_relay_events_stream_maps_native_runtime_delta_to_role_lane(
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

    assert "event: role.output_delta" in response
    assert '"role": "director"' in response
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
    assert '"delta": "single visible delta"' in response


@pytest.mark.asyncio
async def test_relay_sse_snapshot_does_not_advance_role_completion(
    tmp_path: Path,
) -> None:
    service = _relay_service(tmp_path)
    task = service.create_task(
        title="Events task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
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

    assert "event: role.message_completed" in response
    jobs = {job.role: job for job in service.get_task(task.id).role_jobs}
    assert jobs["implementer"].status == "streaming"
    assert jobs["tester"].status == "idle"


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
    service.project_runtime_event(
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

    response = await _request_relay(
        tmp_path,
        f"GET /api/relay/tasks/{task.id}/events HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )

    assert "event: handoff.created" in response
    assert "event: role.output_delta" in response
    assert '"role": "tester"' in response
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
    assert payload["task"]["status"] == "running"
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
