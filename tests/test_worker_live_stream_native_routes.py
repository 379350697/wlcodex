from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.jsonrpc import JsonRpcError
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import RuntimeEvent


@dataclass(frozen=True)
class FakeNativeSession:
    native_thread_id: str
    agent_run_id: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
        }


@dataclass(frozen=True)
class FakeControlResult:
    native_thread_id: str
    agent_run_id: int
    turn_id: str
    status: str = "ok"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
            "turn_id": self.turn_id,
            "status": self.status,
        }


class FakeNativeController:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.sessions = [FakeNativeSession("thread-1", 42)]

    async def status(self) -> dict[str, Any]:
        self.calls.append(("status",))
        return {"enabled": True, "connected": True, "remote_control_status": "ready"}

    async def list_sessions(self) -> list[FakeNativeSession]:
        self.calls.append(("list_sessions",))
        return self.sessions

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        self.calls.append(("read_session", native_thread_id))
        return {"thread": {"id": native_thread_id, "turns": []}, "agent_run_id": 42}

    async def attach_session(self, native_thread_id: str) -> dict[str, Any]:
        self.calls.append(("attach_session", native_thread_id))
        return FakeControlResult(native_thread_id, 42, "turn-1", status="attached")

    async def sync_session(self, native_thread_id: str) -> FakeControlResult:
        self.calls.append(("sync_session", native_thread_id))
        return FakeControlResult(native_thread_id, 42, "turn-1", status="synced")

    async def continue_session(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> FakeControlResult:
        if model is None and images is None:
            self.calls.append(("continue_session", native_thread_id, prompt))
        else:
            self.calls.append(
                ("continue_session", native_thread_id, prompt, model, images)
            )
        return FakeControlResult(native_thread_id, 42, "turn-2")

    async def steer_session(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
        *,
        model: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> FakeControlResult:
        if model is None and images is None:
            self.calls.append(("steer_session", native_thread_id, expected_turn_id, prompt))
        else:
            self.calls.append(
                (
                    "steer_session",
                    native_thread_id,
                    expected_turn_id,
                    prompt,
                    model,
                    images,
                )
            )
        return FakeControlResult(native_thread_id, 42, expected_turn_id)

    async def interrupt_session(
        self,
        native_thread_id: str,
        turn_id: str,
    ) -> FakeControlResult:
        self.calls.append(("interrupt_session", native_thread_id, turn_id))
        return FakeControlResult(native_thread_id, 42, turn_id, status="interrupted")

    async def resolve_approval(
        self,
        codex_request_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("resolve_approval", codex_request_id, response))
        return {"codex_request_id": codex_request_id, "status": "resolved"}


def _store(tmp_path: Path) -> RuntimeEventStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return RuntimeEventStore(ledger._conn)


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


def _json_body(response: str) -> dict[str, Any]:
    return json.loads(response.split("\r\n\r\n", 1)[1])


def _append_worker_event(
    store: RuntimeEventStore,
    agent_run_id: int,
    *,
    event_type: str = "model.text.delta",
    native_thread_id: str | None = None,
    native_turn_id: str | None = None,
    delta: str = "hello",
) -> None:
    payload: dict[str, Any] = {"delta": delta}
    if native_thread_id is not None:
        payload["native_thread_id"] = native_thread_id
    if native_turn_id is not None:
        payload["native_turn_id"] = native_turn_id
    store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=event_type,
            aggregate_type="agent_run",
            aggregate_id=str(agent_run_id),
            correlation_id=f"agent:{agent_run_id}",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload=payload,
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=agent_run_id,
        )
    )


@pytest.mark.asyncio
async def test_native_sessions_requires_authorization_when_token_is_configured(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 401" in response
    assert _json_body(response) == {"error": "unauthorized"}
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_routes_require_token_even_on_loopback_when_controller_exists(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 401 Unauthorized" in response
    assert _json_body(response) == {"error": "unauthorized"}
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_sessions_returns_json_with_bearer_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "sessions": [
            {
                "native_thread_id": "thread-1",
                "agent_run_id": 42,
            }
        ]
    }
    assert controller.calls == [("list_sessions",)]


@pytest.mark.asyncio
async def test_native_continue_posts_json_body_and_returns_control_result(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps({"prompt": "keep going"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "native_thread_id": "thread-1",
        "agent_run_id": 42,
        "turn_id": "turn-2",
        "status": "ok",
    }
    assert controller.calls == [("continue_session", "thread-1", "keep going")]


@pytest.mark.asyncio
async def test_native_continue_accepts_model_and_image_attachments(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "prompt": "describe this",
            "model": "gpt-5.5",
            "images": [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["turn_id"] == "turn-2"
    assert controller.calls == [
        (
            "continue_session",
            "thread-1",
            "describe this",
            "gpt-5.5",
            [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        )
    ]


@pytest.mark.asyncio
async def test_native_status_and_read_routes_return_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        status_response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/status HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        read_response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions/thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in status_response
    assert _json_body(status_response)["remote_control_status"] == "ready"
    assert "HTTP/1.1 200 OK" in read_response
    assert _json_body(read_response)["thread"]["id"] == "thread-1"
    assert controller.calls == [("status",), ("read_session", "thread-1")]


@pytest.mark.asyncio
async def test_native_attach_route_resumes_session_without_prompt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/attach HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "native_thread_id": "thread-1",
        "agent_run_id": 42,
        "turn_id": "turn-1",
        "status": "attached",
    }
    assert controller.calls == [("attach_session", "thread-1")]


@pytest.mark.asyncio
async def test_native_sync_route_projects_server_side_and_returns_compact_result(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/sync HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "native_thread_id": "thread-1",
        "agent_run_id": 42,
        "turn_id": "turn-1",
        "status": "synced",
    }
    assert controller.calls == [("sync_session", "thread-1")]


@pytest.mark.asyncio
async def test_native_steer_interrupt_and_approval_routes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    steer_body = json.dumps({"expected_turn_id": "turn-2", "prompt": "adjust"})
    interrupt_body = json.dumps({"turn_id": "turn-2"})
    approval_body = json.dumps({"action": "approve_once"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        steer_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/steer HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(steer_body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{steer_body}",
        )
        interrupt_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/interrupt HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(interrupt_body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{interrupt_body}",
        )
        approval_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/approvals/req-1/resolve HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(approval_body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{approval_body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in steer_response
    assert _json_body(steer_response)["turn_id"] == "turn-2"
    assert "HTTP/1.1 200 OK" in interrupt_response
    assert _json_body(interrupt_response)["status"] == "interrupted"
    assert "HTTP/1.1 200 OK" in approval_response
    assert _json_body(approval_response) == {
        "codex_request_id": "req-1",
        "status": "resolved",
    }
    assert controller.calls == [
        ("steer_session", "thread-1", "turn-2", "adjust"),
        ("interrupt_session", "thread-1", "turn-2"),
        ("resolve_approval", "req-1", {"action": "approve_once"}),
    ]


@pytest.mark.asyncio
async def test_native_steer_accepts_image_attachments(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "expected_turn_id": "turn-2",
            "prompt": "adjust",
            "model": "gpt-5.5",
            "images": [{"url": "data:image/jpeg;base64,abc"}],
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/steer HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert controller.calls == [
        (
            "steer_session",
            "thread-1",
            "turn-2",
            "adjust",
            "gpt-5.5",
            [{"url": "data:image/jpeg;base64,abc"}],
        )
    ]


@pytest.mark.asyncio
async def test_native_approval_route_returns_404_for_unknown_request(
    tmp_path: Path,
) -> None:
    class UnknownApprovalController(FakeNativeController):
        async def resolve_approval(
            self,
            codex_request_id: str,
            response: dict[str, Any],
        ) -> dict[str, Any]:
            raise KeyError(codex_request_id)

    store = _store(tmp_path)
    body = json.dumps({"action": "approve_once"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=UnknownApprovalController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/approvals/missing/resolve HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 404 Not Found" in response
    assert _json_body(response) == {"error": "approval request not found"}


@pytest.mark.asyncio
async def test_native_routes_return_rpc_error_message_instead_of_exception_class(
    tmp_path: Path,
) -> None:
    class RpcFailingController(FakeNativeController):
        async def continue_session(
            self,
            native_thread_id: str,
            prompt: str,
            *,
            model: str | None = None,
            images: list[dict[str, Any]] | None = None,
        ) -> FakeControlResult:
            raise JsonRpcError(-32000, "turn is not accepting input")

    store = _store(tmp_path)
    body = json.dumps({"prompt": "hello"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=RpcFailingController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 409" in response
    assert _json_body(response) == {
        "error": "turn is not accepting input",
        "code": -32000,
    }


@pytest.mark.asyncio
async def test_native_post_rejects_oversized_json_body_before_controller_call(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = "{}"
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 9000000\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 413 Payload Too Large" in response
    assert _json_body(response) == {"error": "request body too large"}
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_codex_page_contains_worker_and_session_selector(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "<title>Codex</title>" in response
    assert "Codex" in response
    assert "/api/native/codex/sessions" in response
    assert "device-chip" in response
    assert "await api(`/api/native/codex/sessions/${encodeURIComponent(selected.native_thread_id)}`).catch" not in response


@pytest.mark.asyncio
async def test_native_routes_return_503_when_controller_is_unavailable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=None,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 503 Service Unavailable" in response
    assert _json_body(response) == {"error": "native controller unavailable"}


@pytest.mark.asyncio
async def test_worker_stream_routes_require_auth_when_token_is_configured(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?after=0 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 401 Unauthorized" in response
    assert _json_body(response) == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_worker_events_does_not_sync_native_thread_before_returning_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _append_worker_event(store, agent_run_id=42)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?after=0&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["native_sync_error"] == ""
    assert controller.calls == []


@pytest.mark.asyncio
async def test_worker_events_tail_filters_to_current_native_turn(
    tmp_path: Path,
) -> None:
    class CurrentTurnController(FakeNativeController):
        async def sync_session(self, native_thread_id: str) -> FakeControlResult:
            self.calls.append(("sync_session", native_thread_id))
            return FakeControlResult(native_thread_id, 42, "turn-current")

    store = _store(tmp_path)
    _append_worker_event(
        store,
        agent_run_id=42,
        native_thread_id="thread-1",
        native_turn_id="turn-old",
        delta="old turn leaked late",
    )
    _append_worker_event(
        store,
        agent_run_id=42,
        native_thread_id="thread-1",
        native_turn_id="turn-current",
        delta="current turn",
    )
    controller = CurrentTurnController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
            response = await _read_response(
                server.host,
                server.port,
                "GET /api/workers/42/events?tail=80&native_thread_id=thread-1"
                "&native_turn_id=turn-current HTTP/1.1\r\n"
                "Host: test\r\nAuthorization: Bearer secret\r\n"
                "Connection: close\r\n\r\n",
            )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert [event["payload"]["delta"] for event in body["events"]] == ["current turn"]
    assert body["previous_event_count"] == 1
    assert controller.calls == []


@pytest.mark.asyncio
async def test_worker_live_page_accepts_query_token_and_contains_native_controls(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'const streamPathBase = "/api/workers/42/stream";' in response
    assert "/api/native/codex/sessions/" in response
    assert "/api/native/codex/approvals/" in response
    assert "attachNative" in response
    assert "syncNative" not in response
    assert "native-mobile-shell" in response
    assert "renderAssistant" in response
    assert "renderCommand" in response


@pytest.mark.asyncio
async def test_worker_live_page_uses_native_codex_run_interaction_model(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "codex-transcript" in response
    assert "codex-status-flow" in response
    assert "codex-tool-call" in response
    assert "codex-input-dock" in response
    assert "function renderTranscript" in response
    assert "function renderStatusEvent" in response
    assert "function renderStatus(kind, text)" in response
    assert "function renderToolCall" in response
    assert 'id="modelSelector"' in response
    assert 'id="imageInput"' in response
    assert 'id="attachmentButton"' in response
    assert 'id="attachmentStrip"' in response
    assert "function submitPrompt" in response
    assert "continueButton.onclick = () => submitPrompt();" in response
    assert 'throw new Error("官方 Codex 会话未连接");' in response
    assert "await pollEvents();" in response
    assert "function primaryComposerAction" in response
    assert "function applyNativeTurnState" in response
    assert 'id="composerActivityDot"' in response
    assert "function setComposerActivity" in response
    assert "continueButton.textContent = mode === \"interrupt\" ? \"■\" : \"↑\";" in response
    assert 'const requiresTurn = mode === "interrupt" || mode === "steer";' in response
    assert "requiresTurn && !activeTurnId" in response
    assert 'class="dock-actions" hidden' in response
    assert "function readImageAttachment" in response
    assert "function renderAttachments" in response
    assert ".bubble" not in response
    assert "message assistant" not in response


@pytest.mark.asyncio
async def test_worker_live_page_loads_recent_tail_and_folds_history(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "RECENT_EVENT_LIMIT" in response
    assert 'eventsPath("tail=" + RECENT_EVENT_LIMIT, {currentTurn: true})' in response
    assert 'if (!loadedEvents.length && nativeTurnId)' in response
    assert "function loadRecentEvents" in response
    assert "function loadOlderEvents" in response
    assert "function pollEvents" in response
    assert "setInterval(pollEvents, 1000)" in response
    assert "function eventsPath(params, options = {})" in response
    assert 'if (nativeThreadId) search.set("native_thread_id", nativeThreadId);' in response
    assert "eventsPath(`after=${latestEventId}&limit=100`)" in response
    assert 'source.onerror = () => { setConnectionState("reconnecting"); pollEvents(); };' in response
    assert "以前的消息" in response
    assert "previous_event_count" in response
    assert "new EventSource(streamPath)" not in response
    assert "new EventSource(streamPathWithCursor" in response


@pytest.mark.asyncio
async def test_worker_live_page_does_not_bind_historical_turns_as_current_control(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "render(event, {scroll: false, historical: true});" in response
    assert "applyNativeTurnState(event, options);" in response
    assert "function applyNativeTurnState(event, options = {})" in response
    assert "let activeTurnId = \"\";" in response
    assert (
        "if (!options.historical && payload.native_turn_id) "
        "nativeTurnId = payload.native_turn_id;" in response
    )
    assert "if (options.historical) return;" in response
    assert "body.expected_turn_id = activeTurnId;" in response
    assert "activeTurnId = result.active_turn_id || \"\";" in response
