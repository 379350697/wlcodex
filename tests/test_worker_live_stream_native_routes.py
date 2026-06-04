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
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import RuntimeEvent


@dataclass(frozen=True)
class FakeNativeSession:
    native_thread_id: str
    agent_run_id: int
    activity_at: str = "2026-05-31T12:39:00+00:00"
    updated_at: str = "2026-05-31T13:00:00+00:00"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
            "activity_at": self.activity_at,
            "updated_at": self.updated_at,
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

    async def list_models(self) -> list[dict[str, Any]]:
        self.calls.append(("list_models",))
        return [
            {
                "id": "gpt-5.5",
                "model": "gpt-5.5",
                "displayName": "GPT-5.5",
                "description": "Most capable",
                "hidden": False,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "Deep"},
                ],
                "defaultReasoningEffort": "medium",
                "serviceTiers": [
                    {"id": "auto", "name": "Auto", "description": "Default"},
                    {"id": "fast", "name": "Fast", "description": "Lower latency"},
                ],
                "defaultServiceTier": "auto",
                "isDefault": True,
            }
        ]

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
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> FakeControlResult:
        if model is None and effort is None and service_tier is None and images is None:
            self.calls.append(("continue_session", native_thread_id, prompt))
        else:
            self.calls.append(
                (
                    "continue_session",
                    native_thread_id,
                    prompt,
                    model,
                    effort,
                    service_tier,
                    images,
                )
            )
        return FakeControlResult(native_thread_id, 42, "turn-2")

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> FakeControlResult:
        self.calls.append(
            ("start_session", cwd, prompt, model, effort, service_tier, images)
        )
        return FakeControlResult("thread-new", 43, "turn-new")

    async def create_session(
        self,
        cwd: str,
        *,
        model: str | None = None,
        service_tier: str | None = None,
    ) -> FakeControlResult:
        self.calls.append(("create_session", cwd, model, service_tier))
        return FakeControlResult("thread-empty", 44, "", status="created")

    async def steer_session(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> FakeControlResult:
        if model is None and effort is None and service_tier is None and images is None:
            self.calls.append(("steer_session", native_thread_id, expected_turn_id, prompt))
        else:
            self.calls.append(
                (
                    "steer_session",
                    native_thread_id,
                    expected_turn_id,
                    prompt,
                    model,
                    effort,
                    service_tier,
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


class FakeAntigravityProvider:
    provider = "antigravity"
    provider_engine = "cli-local"


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
        allow_unauthenticated_loopback=False,
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
async def test_native_routes_allow_public_loopback_when_token_is_disabled(
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

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "sessions": [
                {
                    "native_thread_id": "thread-1",
                    "agent_run_id": 42,
                    "activity_at": "2026-05-31T12:39:00+00:00",
                    "updated_at": "2026-05-31T13:00:00+00:00",
                }
            ]
        }
    assert controller.calls == [("list_sessions",)]


@pytest.mark.asyncio
async def test_native_public_root_and_page_open_without_token(tmp_path: Path) -> None:
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
        root_response = await _read_response(
            server.host,
            server.port,
            "GET / HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        page_response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 303 See Other" in root_response
    assert "Location: /native/codex" in root_response
    assert "访问令牌" not in root_response
    assert "HTTP/1.1 200 OK" in page_response
    assert "<title>Codex</title>" in page_response


@pytest.mark.asyncio
async def test_native_public_root_and_page_open_on_loopback_testing_with_token(
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
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        root_response = await _read_response(
            server.host,
            server.port,
            "GET / HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        page_response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 303 See Other" in root_response
    assert "Location: /native/codex" in root_response
    assert "访问令牌" not in root_response
    assert "HTTP/1.1 200 OK" in page_response
    assert "<title>Codex</title>" in page_response
    assert "访问令牌" not in page_response


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
        allow_unauthenticated_loopback=False,
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
                    "activity_at": "2026-05-31T12:39:00+00:00",
                    "updated_at": "2026-05-31T13:00:00+00:00",
                }
            ]
        }
    assert controller.calls == [("list_sessions",)]


@pytest.mark.asyncio
async def test_native_models_route_returns_official_catalog(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/models HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert body["models"][0]["model"] == "gpt-5.5"
    assert body["models"][0]["supportedReasoningEfforts"][1] == {
        "reasoningEffort": "high",
        "description": "Deep",
    }
    assert body["models"][0]["serviceTiers"][1]["id"] == "fast"
    assert controller.calls == [("list_models",)]


@pytest.mark.asyncio
async def test_native_start_route_creates_project_thread_with_model_settings(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "cwd": "/Users/wl/projects/wlcodex",
            "prompt": "start in this project",
            "model": "gpt-5.5",
            "effort": "high",
            "service_tier": "fast",
            "images": [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
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
    assert _json_body(response)["native_thread_id"] == "thread-new"
    assert controller.calls == [
        (
            "start_session",
            "/Users/wl/projects/wlcodex",
            "start in this project",
            "gpt-5.5",
            "high",
            "fast",
            [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        )
    ]


@pytest.mark.asyncio
async def test_native_start_route_creates_empty_project_thread_without_prompt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps({"cwd": "/Users/wl/projects/wlcodex"})
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
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
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
    body_json = _json_body(response)
    assert body_json["native_thread_id"] == "thread-empty"
    assert body_json["status"] == "created"
    assert controller.calls == [
        ("create_session", "/Users/wl/projects/wlcodex", None, None)
    ]


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
async def test_native_continue_accepts_chunked_json_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    first = '{"prompt":"keep '
    second = 'going"}'
    request = (
        "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
        "Host: test\r\n"
        "Authorization: Bearer secret\r\n"
        "Content-Type: application/json\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n\r\n"
        f"{len(first.encode('utf-8')):x}\r\n"
        f"{first}\r\n"
        f"{len(second.encode('utf-8')):x}\r\n"
        f"{second}\r\n"
        "0\r\n\r\n"
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
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["turn_id"] == "turn-2"
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
            None,
            None,
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
            None,
            None,
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
            effort: str | None = None,
            service_tier: str | None = None,
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
    assert 'localStorage.setItem("wlcodexToken", token)' in response
    assert 'const PROVIDER = "codex";' in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "api(`${API_BASE}/sessions`)" in response
    assert "device-chip" in response
    assert "await api(`/api/native/codex/sessions/${encodeURIComponent(selected.native_thread_id)}`).catch" not in response


@pytest.mark.asyncio
async def test_native_root_and_unauthorized_page_show_token_entry(
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
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        root_response = await _read_response(
            server.host,
            server.port,
            "GET / HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        native_response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in root_response
    assert "<title>WLCodex</title>" in root_response
    assert "localStorage.getItem(\"wlcodexToken\")" in root_response
    assert 'location.replace("/native/codex")' in root_response
    assert "HTTP/1.1 401 Unauthorized" in native_response
    assert "Content-Type: text/html; charset=utf-8" in native_response
    assert "访问令牌" in native_response
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_one_time_login_ticket_sets_cookie_once(tmp_path: Path) -> None:
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
        ticket_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/login-ticket HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
        ticket_body = _json_body(ticket_response)
        login_path = ticket_body["path"]
        first_open = await _read_response(
            server.host,
            server.port,
            f"GET {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        second_open = await _read_response(
            server.host,
            server.port,
            f"GET {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        first_login = await _read_response(
            server.host,
            server.port,
            f"POST {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
        second_login = await _read_response(
            server.host,
            server.port,
            f"POST {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
        cookie_login = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Cookie: wlcodex_token=secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in ticket_response
    assert ticket_body["expires_in"] > 0
    assert login_path.startswith("/native/codex/login?ticket=")
    assert "secret" not in login_path
    assert "HTTP/1.1 200 OK" in first_open
    assert "进入 Codex" in first_open
    assert "HTTP/1.1 200 OK" in second_open
    assert "HTTP/1.1 303 See Other" in first_login
    assert "Location: /native/codex" in first_login
    assert "Set-Cookie: wlcodex_token=secret;" in first_login
    assert "HTTP/1.1 401 Unauthorized" in second_login
    assert "HTTP/1.1 200 OK" in cookie_login
    assert "<title>Codex</title>" in cookie_login


@pytest.mark.asyncio
async def test_native_codex_page_uses_project_context_for_new_chat(
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
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "let selectedProjectCwd = \"\";" in response
    assert "function selectProject(cwd)" in response
    assert "selectedProjectCwd === cwd ? \" active\" : \"\"" in response
    assert "let selectedProjectRendered = false;" in response
    assert "projectsEl.appendChild(projectNewChat);" in response
    assert "selectedProjectRendered = true;" in response
    assert "sessionProjectKey(session) === selectedProjectCwd" in response
    assert 'id="projectNewChat"' in response
    assert "function renderProjectAction()" in response
    assert "async function openProjectNewChat()" in response
    assert "async function startNewChat(prompt)" in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "api(`${API_BASE}/sessions/start`" in response
    assert "document.getElementById(\"chat\").onclick = () => selectProject(\"\");" in response
    assert "document.querySelector(\".controls\")" in response
    assert "const SESSION_PREVIEW_LIMIT = 10;" in response
    assert "renderSessionList(filtered.slice(0, SESSION_PREVIEW_LIMIT)" in response
    assert 'details.className = "more-sessions";' in response
    assert "更多聊天" in response
    assert ".label { display: block;" in response
    assert ".recent-title { display: -webkit-box;" in response
    assert "-webkit-line-clamp: 2;" in response
    assert 'class="label recent-title"' in response
    assert "relativeTime(sessionActivityAt(session))" in response
    assert "Date.parse(sessionActivityAt(right))" in response
    assert "return session.activity_at || session.updated_at || \"\";" in response


@pytest.mark.asyncio
async def test_native_codex_page_filters_session_workspace_projects_to_projects_root(
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
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROJECTS_URL = "/api/council/projects";' in response
    assert 'let projectRoot = "";' in response
    assert "let projectCatalog = [];" in response
    assert "async function loadProjects()" in response
    assert 'projectRoot = String(data.root || "");' in response
    assert "projectCatalog = Array.isArray(data.projects) ? data.projects : [];" in response
    assert "addProjectOption(project.cwd, project.name);" in response
    assert "for (const project of projectCatalog)" in response
    assert "for (const session of sessions)" in response
    assert "if (!isKnownProjectWorkspace(session.cwd)) continue;" in response
    assert "function isKnownProjectWorkspace(cwd)" in response
    assert "projectCatalog.some(project => String(project.cwd || \"\") === value)" in response
    assert "const normalizedRoot = projectRoot.endsWith(\"/\") ? projectRoot : projectRoot + \"/\";" in response
    assert "return parts.length === 1;" in response
    assert "await loadProjects();" in response
    assert "if (seen.size >= 4) break;" not in response


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
        allow_unauthenticated_loopback=False,
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
    assert 'const PROVIDER = "codex";' in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/${action}`" in response
    assert "`${API_BASE}/approvals/${encodeURIComponent(requestId)}/resolve`" in response
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
    assert 'id="modelSettingsButton"' in response
    assert 'id="modelPopover"' in response
    assert 'id="modelSelector"' in response
    assert 'id="imageInput"' in response
    assert 'id="attachmentButton"' in response
    assert 'id="attachmentStrip"' in response
    assert 'class="interruption-choice" id="interruptionChoice" hidden' in response
    assert "function submitPrompt" in response
    assert "continueButton.onclick = () => submitPrompt();" in response
    assert 'throw new Error(`${PROVIDER_LABEL} 会话未连接`);' in response
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
    assert "function renderLocalUserEcho" in response
    assert "renderTranscriptImages(node.body, payload.images || [])" in response
    assert "node.append(document.createTextNode(String(text)))" in response
    assert "openInterruptionChoice()" in response
    assert 'submitPrompt("steer")' in response
    assert 'submitPrompt("continue")' in response
    assert ".bubble" not in response
    assert "message assistant" not in response


@pytest.mark.asyncio
async def test_worker_live_page_hides_success_lifecycle_events_from_transcript(
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
    assert "function shouldRenderStatusEvent(event)" in response
    assert 'if (event.kind === "completed") return false;' in response
    assert (
        'if (event.kind === "lifecycle" && !isFailedStatus(payload.status)) '
        "return false;"
    ) in response
    assert (
        'if (event.kind === "lifecycle" && status === "running") '
        'return `${PROVIDER_LABEL} 正在回复`;'
    ) in response


@pytest.mark.asyncio
async def test_worker_live_page_uses_official_model_catalog_settings(
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
    assert 'id="reasoningSelector"' in response
    assert 'id="serviceTierSelector"' in response
    assert "模型" in response
    assert "速度" in response
    assert "推理" in response
    assert "async function loadModelCatalog" in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "api(`${API_BASE}/models`)" in response
    assert "function updateSettingSummary" in response
    assert 'id="serviceTierOptions"' in response
    assert 'id="reasoningOptions"' in response
    assert "pointer-events: none" in response
    assert "function renderSettingOptions" in response
    assert "function fillServiceTierSelector" in response
    assert "function serviceTierLabel" in response
    assert "function reasoningEffortLabel" in response
    assert 'if (key === "high") return "高";' in response
    assert 'if (["xhigh", "extra_high"].includes(key)) return "极高";' in response
    assert 'if (["max", "maximum"].includes(key)) return "最大";' in response
    assert "function preferredServiceTierDefault" in response
    assert 'normalOption.value = "";' in response
    assert 'renderSettingOptions(serviceTierOptions, serviceTierSelector, updateSettingSummary, {includeEmpty: true});' in response
    assert "const MODEL_SETTINGS_STORAGE_KEY" in response
    assert "function saveModelSettingsIfChanged" in response
    assert "if (willClose) saveModelSettingsIfChanged();" in response
    assert "function markModelSettingsDirty" in response
    assert "localStorage.setItem(MODEL_SETTINGS_STORAGE_KEY" in response
    assert "button.dataset.value = option.value;" in response
    assert "function syncSettingOptionsSelection" in response
    assert "syncSettingOptionsSelection(container, select);" in response
    assert "syncSettingOptionsSelection(reasoningOptions, reasoningSelector);" in response
    assert "syncSettingOptionsSelection(serviceTierOptions, serviceTierSelector);" in response
    assert "body.model = savedModelSettings.model;" in response
    assert "body.effort = savedModelSettings.effort;" in response
    assert "body.service_tier = savedModelSettings.service_tier;" in response
    assert "modelSettingsButton.disabled = false;" in response
    assert "function syncSettingOptionsDisabled" in response
    assert "reasoningSelector.disabled = sendingPrompt || nativeTurnRunning" in response
    assert "serviceTierSelector.disabled = sendingPrompt || nativeTurnRunning" in response


@pytest.mark.asyncio
async def test_worker_live_page_uses_provider_scoped_model_catalog_for_antigravity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([FakeAntigravityProvider()]),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_provider=antigravity&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROVIDER = "antigravity";' in response
    assert 'const API_BASE = "/api/native/antigravity";' in response
    assert "api(`${API_BASE}/models`)" in response
    assert "body.model = savedModelSettings.model;" in response


@pytest.mark.asyncio
async def test_worker_live_page_shows_approval_resolution_state(
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
    assert 'className = "approval-state"' in response
    assert ".approval-action.approve" in response
    assert ".approval-action.danger" in response
    assert ".approval-action.selected" in response
    assert ".approval-action.muted" in response
    assert "button.dataset.action = action;" in response
    assert "button.className = `approval-action ${tone}`;" in response
    assert "card.dataset.selectedAction = action;" in response
    assert "setApprovalButtons(card, action, state);" in response
    assert "function setApprovalButtons" in response
    assert "button.classList.toggle(\"selected\", selected);" in response
    assert "button.classList.toggle(\"muted\", state !== \"idle\" && !selected);" in response
    assert "function approvalResolvedAction" in response
    assert "const action = approvalResolvedAction(event, card);" in response
    assert 'setApprovalState(card, action, "resolved");' in response
    assert 'setApprovalState(card, "approve_once", "resolved");' not in response
    assert "function approvalStateText" in response
    assert "if (action === \"approve_once\") return state === \"pending\" ? \"批准一次处理中\" : \"已批准一次\";" in response
    assert "if (action === \"approve_session\") return state === \"pending\" ? \"本会话批准处理中\" : \"本会话已批准\";" in response
    assert "setApprovalState(card, action, \"pending\")" in response
    assert "setApprovalState(card, action, \"resolved\")" in response
    assert "setApprovalState(card, action, \"failed\"" in response
    assert "button.onclick = () => resolveApproval(payload.codexRequestId, action, card)" in response


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
    assert "hasLiveDisplayEvents" in response
    assert "model.usage.updated" in response
    assert "function loadRecentEvents" in response
    assert "function loadOlderEvents" in response
    assert "function pollEvents" in response
    assert "setInterval(pollEvents, 1000)" in response
    assert "function eventsPath(params, options = {})" in response
    assert 'if (nativeThreadId) search.set("native_thread_id", nativeThreadId);' in response
    assert "eventsPath(`after=${latestEventId}&limit=100`)" in response
    assert 'source.onerror = () => { setConnectionState("reconnecting"); pollEvents(); };' in response
    assert "function isInternalEvent(event)" in response
    assert "if (isInternalEvent(event)) return;" in response
    assert 'historyFold.textContent = previousEventCount > 0 ? "加载更早的消息" : "更早的消息";' in response
    assert "`${previousEventCount} 条以前的消息`" not in response
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
        "if (!options.historical && !mirroredTranscript && payload.native_turn_id) "
        "nativeTurnId = payload.native_turn_id;" in response
    )
    assert "const mirroredTranscript = isMirroredTranscriptEvent(event);" in response
    assert "if (options.historical || mirroredTranscript) return;" in response
    assert "body.expected_turn_id = activeTurnId;" in response
    assert "activeTurnId = result.active_turn_id || \"\";" in response


@pytest.mark.asyncio
async def test_worker_live_page_clears_running_composer_state_on_terminal_turn_events(
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
    assert "function isTerminalTurnEvent(event)" in response
    assert "isTerminalTurnEvent(event)" in response
    assert '["completed", "done", "succeeded", "success"]' in response
    assert '["failed", "error", "cancelled", "canceled", "interrupted", "aborted"]' in response
    assert "nativeTurnRunning = false;" in response
    assert 'continueButton.textContent = mode === "interrupt" ? "■" : "↑";' in response
    assert "(!nativeTurnRunning && !composerHasDraft())" in response


@pytest.mark.asyncio
async def test_worker_live_page_keeps_latest_turn_open_and_collapses_prior_turns(
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
    assert "options.latest)" not in response
    assert "const latestTurnId = latestFoldGroupTurnId(groups);" in response
    assert "renderFoldGroup(group, {latestTurnId});" in response
    assert "function latestFoldGroupTurnId(groups)" in response
    assert "turnFoldTitle(group)" in response
    assert "function foldMessageCount(group)" in response
    assert "if (isInternalEvent(event)) continue;" in response
    assert "function dedupeDisplayEvents(sourceEvents)" in response
    assert "const groups = foldGroups(dedupeDisplayEvents(loadedEvents));" in response
    assert "title.textContent = turnFoldTitle(group);" in response
    assert "nativeTurnId !== latestTurnId" in response
    assert "completed || nativeTurnId !== latestTurnId" not in response
    assert "group.length > 1" not in response


@pytest.mark.asyncio
async def test_worker_live_page_only_keeps_pending_approvals_expanded(
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
    assert "function hasPendingApproval(group)" in response
    assert "const pendingApproval = hasPendingApproval(group);" in response
    assert "!pendingApproval" in response
    assert "const hasApproval = group.some(event => event.kind === \"approval_requested\");" not in response


@pytest.mark.asyncio
async def test_worker_live_page_fold_keeps_native_transcript_previews(
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
    assert "function renderFoldPreview(head, group)" in response
    assert "foldTranscriptPreviewText(group, \"user_message\")" in response
    assert "foldTranscriptPreviewText(group, \"text_delta\")" in response
    assert "appendFoldPreviewLine(preview, \"user\", userText);" in response
    assert "appendFoldPreviewLine(preview, \"assistant\", assistantText);" in response
    assert ".turn-fold[open] .turn-fold-preview { display: none; }" in response
    assert "/turn-summary" not in response
    assert "summarize" not in response


@pytest.mark.asyncio
async def test_worker_live_page_groups_turn_events_before_collapsing(
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
    assert "const groupByKey = new Map();" in response
    assert "groupByKey.get(key).push(event);" in response
    assert "return Array.from(groupByKey.values()).sort" in response
