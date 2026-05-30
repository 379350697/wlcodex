from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from wlcodex.codex_native.client import CodexNativeClient, LazyNativeClient
from wlcodex.codex_native.models import NativeCodexStatus


class FakeTransport:
    def __init__(
        self,
        response_for: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self.client: CodexNativeClient | None = None
        self.closed = False
        self._response_for = response_for

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)
        if "id" not in msg:
            return
        response = self._response_for(msg)
        if response is None:
            return
        assert self.client is not None
        await self.client.rpc.receive_message({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": response,
        })

    async def close(self) -> None:
        self.closed = True


def _method_names(sent: list[dict[str, Any]]) -> list[str]:
    return [msg["method"] for msg in sent]


@pytest.mark.asyncio
async def test_native_client_uses_official_app_server_protocol() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                assert msg["params"] == {
                    "clientInfo": {"name": "wlcodex", "version": "1.0.0"},
                    "capabilities": None,
                }
                return {"serverInfo": {"name": "Codex"}}
            case "thread/list":
                assert msg["params"] == {
                    "limit": 50,
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                    "archived": False,
                    "useStateDbOnly": True,
                }
                return {
                    "data": [
                        {
                            "id": "thread-1",
                            "name": "Task 1",
                            "preview": "first prompt",
                            "status": {"type": "notLoaded"},
                        }
                    ]
                }
            case "thread/read":
                assert msg["params"] == {"threadId": "thread-1", "includeTurns": True}
                return {
                    "thread": {
                        "id": "thread-1",
                        "name": "Task 1",
                        "turns": [{"id": "turn-1", "items": []}],
                    },
                }
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    status = await client.status()
    sessions = await client.list_sessions()
    detail = await client.read_session("thread-1")

    assert _method_names(transport.sent) == [
        "initialize",
        "initialized",
        "thread/list",
        "thread/read",
    ]
    assert status.connected is True
    assert status.remote_control_status == "connected"
    assert sessions[0]["id"] == "thread-1"
    assert detail["thread"]["turns"][0]["id"] == "turn-1"


@pytest.mark.asyncio
async def test_native_client_status_does_not_call_removed_remote_status_read() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                return {}
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    status = await client.status()

    assert status.connected is True
    assert status.remote_control_status == "connected"
    assert _method_names(transport.sent) == ["initialize", "initialized"]


@pytest.mark.asyncio
async def test_native_client_continue_steer_and_interrupt() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                return {}
            case "thread/resume":
                assert msg["params"] == {"threadId": "thread-1"}
                return {"threadId": "thread-1"}
            case "turn/start":
                assert msg["params"]["threadId"] == "thread-1"
                assert msg["params"]["input"] == [
                    {"type": "text", "text": "continue", "text_elements": []}
                ]
                return {"turn": {"id": "turn-2"}}
            case "turn/steer":
                assert msg["params"] == {
                    "threadId": "thread-1",
                    "expectedTurnId": "turn-2",
                    "input": [{"type": "text", "text": "steer", "text_elements": []}],
                }
                return {}
            case "turn/interrupt":
                assert msg["params"] == {"threadId": "thread-1", "turnId": "turn-2"}
                return {}
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    turn_id = await client.continue_session("thread-1", "continue")
    await client.steer_turn("thread-1", "turn-2", "steer")
    await client.interrupt_turn("thread-1", "turn-2")

    assert turn_id == "turn-2"
    assert _method_names(transport.sent) == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
    ]


@pytest.mark.asyncio
async def test_native_client_continue_sends_model_and_images_without_collaboration_mode() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                return {}
            case "thread/resume":
                return {"threadId": "thread-1"}
            case "turn/start":
                assert msg["params"] == {
                    "threadId": "thread-1",
                    "input": [
                        {"type": "text", "text": "continue", "text_elements": []},
                        {"type": "image", "url": "data:image/png;base64,abc"},
                    ],
                    "model": "gpt-5.5",
                }
                return {"turn": {"id": "turn-2"}}
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    turn_id = await client.continue_session(
        "thread-1",
        "continue",
        model="gpt-5.5",
        images=[{"url": "data:image/png;base64,abc"}],
    )

    assert turn_id == "turn-2"
    assert _method_names(transport.sent) == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/start",
    ]


@pytest.mark.asyncio
async def test_native_client_attach_session_resumes_thread_without_starting_turn() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                return {}
            case "thread/resume":
                assert msg["params"] == {"threadId": "thread-1"}
                return {
                    "thread": {
                        "id": "thread-1",
                        "name": "Live task",
                        "turns": [
                            {
                                "id": "turn-1",
                                "status": "completed",
                                "items": [],
                            }
                        ],
                    }
                }
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    detail = await client.attach_session("thread-1")

    assert detail["thread"]["id"] == "thread-1"
    assert _method_names(transport.sent) == [
        "initialize",
        "initialized",
        "thread/resume",
    ]


@pytest.mark.asyncio
async def test_native_client_initialize_is_concurrency_safe() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def response_later(client: CodexNativeClient, msg: dict[str, Any]) -> None:
        started.set()
        await release.wait()
        await client.rpc.receive_message({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {},
        })

    class SlowInitializeTransport:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.client: CodexNativeClient | None = None

        async def send_json(self, msg: dict[str, Any]) -> None:
            self.sent.append(msg)
            if "id" not in msg:
                return
            assert self.client is not None
            if msg["method"] == "initialize":
                asyncio.create_task(response_later(self.client, msg))
                return
            await self.client.rpc.receive_message({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {"connected": True, "status": "ready"},
            })

        async def close(self) -> None:
            pass

    transport = SlowInitializeTransport()
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    first = asyncio.create_task(client.status())
    await started.wait()
    second = asyncio.create_task(client.list_sessions())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert _method_names(transport.sent).count("initialize") == 1


@pytest.mark.asyncio
async def test_lazy_native_client_defers_factory_and_replays_handlers() -> None:
    class ReadyClient:
        def __init__(self) -> None:
            self.notification_handlers: list[tuple[str, Any]] = []
            self.server_request_handlers: list[tuple[str, Any]] = []
            self.calls: list[tuple[Any, ...]] = []
            self.resolved: list[tuple[str, dict[str, Any]]] = []

        async def status(self) -> NativeCodexStatus:
            self.calls.append(("status",))
            return NativeCodexStatus(
                enabled=True,
                connected=True,
                remote_control_status="ready",
            )

        async def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
            self.calls.append(("list_sessions", limit))
            return [{"id": "thread-1"}]

        def register_notification_handler(self, method: str, handler: Any) -> None:
            self.notification_handlers.append((method, handler))

        def register_server_request_handler(self, method: str, handler: Any) -> None:
            self.server_request_handlers.append((method, handler))

        def resolve_request(self, request_id: str, result: dict[str, Any]) -> None:
            self.resolved.append((request_id, result))

    created: list[ReadyClient] = []

    async def factory() -> ReadyClient:
        client = ReadyClient()
        created.append(client)
        return client

    async def handler(*_args: Any) -> None:
        return None

    lazy = LazyNativeClient(factory)
    lazy.register_notification_handler("turn/started", handler)
    lazy.register_server_request_handler(
        "item/commandExecution/requestApproval",
        handler,
    )

    assert created == []

    status = await lazy.status()
    sessions = await lazy.list_sessions(limit=2)
    lazy.resolve_request("req-1", {"decision": "approved"})

    assert status.remote_control_status == "ready"
    assert sessions == [{"id": "thread-1"}]
    assert len(created) == 1
    assert created[0].notification_handlers == [("turn/started", handler)]
    assert created[0].server_request_handlers == [
        ("item/commandExecution/requestApproval", handler)
    ]
    assert created[0].calls == [("status",), ("list_sessions", 2)]
    assert created[0].resolved == [("req-1", {"decision": "approved"})]


@pytest.mark.asyncio
async def test_lazy_native_client_close_waits_for_inflight_factory() -> None:
    class ClosableClient:
        def __init__(self) -> None:
            self.closed = False

        async def status(self) -> NativeCodexStatus:
            return NativeCodexStatus(
                enabled=True,
                connected=True,
                remote_control_status="ready",
            )

        async def close(self) -> None:
            self.closed = True

    created: list[ClosableClient] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> ClosableClient:
        started.set()
        await release.wait()
        client = ClosableClient()
        created.append(client)
        return client

    lazy = LazyNativeClient(factory)
    status_task = asyncio.create_task(lazy.status())
    await started.wait()
    close_task = asyncio.create_task(lazy.close())
    await asyncio.sleep(0)
    release.set()

    await close_task

    assert len(created) == 1
    assert created[0].closed is True
    with pytest.raises(RuntimeError, match="closed"):
        await status_task
