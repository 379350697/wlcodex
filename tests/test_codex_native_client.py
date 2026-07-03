from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from wlcodex.codex_native.client import CodexNativeClient, LazyNativeClient
from wlcodex.codex_native.models import NativeCodexStatus
from wlcodex.jsonrpc import JsonRpcTimeout


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
                    "capabilities": {"experimentalApi": True},
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
async def test_native_client_continue_sends_model_images_and_collaboration_mode() -> None:
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
                    "collaborationMode": {
                        "mode": "plan",
                        "settings": {"model": "gpt-5.5"},
                    },
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
        collaboration_mode={"mode": "plan"},
    )

    assert turn_id == "turn-2"
    assert _method_names(transport.sent) == [
        "initialize",
        "initialized",
        "thread/resume",
        "turn/start",
    ]


@pytest.mark.asyncio
async def test_native_client_sends_codex_permission_overrides() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                return {}
            case "thread/start":
                assert msg["params"] == {
                    "cwd": "/repo",
                    "model": "gpt-5.5",
                    "serviceTier": "fast",
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "auto_review",
                    "sandbox": "workspace-write",
                }
                return {"thread": {"id": "thread-1"}}
            case "thread/resume":
                assert msg["params"] == {"threadId": "thread-1"}
                return {"threadId": "thread-1"}
            case "turn/start":
                assert msg["params"] == {
                    "threadId": "thread-1",
                    "input": [
                        {"type": "text", "text": "continue", "text_elements": []}
                    ],
                    "effort": "high",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "auto_review",
                    "sandboxPolicy": {"type": "dangerFullAccess"},
                }
                return {"turn": {"id": "turn-2"}}
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    detail = await client.start_thread(
        "/repo",
        model="gpt-5.5",
        service_tier="fast",
        approval_policy="on-request",
        approvals_reviewer="auto_review",
        sandbox="workspace-write",
    )
    turn_id = await client.continue_session(
        "thread-1",
        "continue",
        effort="high",
        approval_policy="never",
        approvals_reviewer="auto_review",
        sandbox_policy={"type": "dangerFullAccess"},
    )

    assert detail["thread"]["id"] == "thread-1"
    assert turn_id == "turn-2"
    assert _method_names(transport.sent) == [
        "initialize",
        "initialized",
        "thread/start",
        "thread/resume",
        "turn/start",
    ]


@pytest.mark.asyncio
async def test_native_client_lists_official_model_catalog() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                return {}
            case "model/list":
                assert msg["params"] == {"limit": 100, "includeHidden": False}
                return {
                    "data": [
                        {
                            "id": "gpt-5.5",
                            "model": "gpt-5.5",
                            "displayName": "GPT-5.5",
                            "hidden": False,
                        }
                    ],
                    "nextCursor": None,
                }
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    models = await client.list_models()

    assert models == [
        {
            "id": "gpt-5.5",
            "model": "gpt-5.5",
            "displayName": "GPT-5.5",
            "hidden": False,
        }
    ]
    assert _method_names(transport.sent) == ["initialize", "initialized", "model/list"]


@pytest.mark.asyncio
async def test_native_client_starts_thread_and_turn_with_real_model_settings() -> None:
    def response_for(msg: dict[str, Any]) -> dict[str, Any] | None:
        match msg["method"]:
            case "initialize":
                return {}
            case "thread/start":
                assert msg["params"] == {
                    "cwd": "/workspace/two",
                    "model": "gpt-5.5",
                    "serviceTier": "fast",
                }
                return {
                    "thread": {
                        "id": "thread-new",
                        "cwd": "/workspace/two",
                        "status": "idle",
                    }
                }
            case "turn/start":
                assert msg["params"] == {
                    "threadId": "thread-new",
                    "input": [
                        {"type": "text", "text": "start work", "text_elements": []},
                        {"type": "image", "url": "data:image/png;base64,abc"},
                    ],
                    "effort": "high",
                    "model": "gpt-5.5",
                    "serviceTier": "fast",
                }
                return {"turn": {"id": "turn-new"}}
        raise AssertionError(f"unexpected method: {msg['method']}")

    transport = FakeTransport(response_for)
    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
    )
    transport.client = client

    detail = await client.start_thread(
        "/workspace/two",
        model="gpt-5.5",
        service_tier="fast",
    )
    turn_id = await client.start_turn(
        "thread-new",
        "start work",
        model="gpt-5.5",
        effort="high",
        service_tier="fast",
        images=[{"url": "data:image/png;base64,abc"}],
    )

    assert detail["thread"]["id"] == "thread-new"
    assert turn_id == "turn-new"
    assert _method_names(transport.sent) == [
        "initialize",
        "initialized",
        "thread/start",
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
async def test_lazy_native_client_retries_once_after_initialize_timeout() -> None:
    class TimeoutThenReadyClient:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail
            self.closed = False

        async def status(self) -> NativeCodexStatus:
            if self.fail:
                raise JsonRpcTimeout("Request initialize (id=1) timed out after 60.0s")
            return NativeCodexStatus(
                enabled=True,
                connected=True,
                remote_control_status="ready",
            )

        async def close(self) -> None:
            self.closed = True

    created: list[TimeoutThenReadyClient] = []

    async def factory() -> TimeoutThenReadyClient:
        client = TimeoutThenReadyClient(fail=not created)
        created.append(client)
        return client

    lazy = LazyNativeClient(factory)

    status = await lazy.status()

    assert status.remote_control_status == "ready"
    assert len(created) == 2
    assert created[0].closed is True
    assert created[1].closed is False


@pytest.mark.asyncio
async def test_lazy_native_client_replays_handlers_registered_after_start_on_reconnect() -> None:
    class ReconnectClient:
        def __init__(self) -> None:
            self.fail = False
            self.closed = False
            self.notification_handlers: list[tuple[str, Any]] = []
            self.server_request_handlers: list[tuple[str, Any]] = []

        async def status(self) -> NativeCodexStatus:
            if self.fail:
                raise JsonRpcTimeout("Request initialize (id=1) timed out after 60.0s")
            return NativeCodexStatus(
                enabled=True,
                connected=True,
                remote_control_status="ready",
            )

        def register_notification_handler(self, method: str, handler: Any) -> None:
            self.notification_handlers.append((method, handler))

        def register_server_request_handler(self, method: str, handler: Any) -> None:
            self.server_request_handlers.append((method, handler))

        async def close(self) -> None:
            self.closed = True

    created: list[ReconnectClient] = []

    async def factory() -> ReconnectClient:
        client = ReconnectClient()
        created.append(client)
        return client

    async def handler(*_args: Any) -> None:
        return None

    lazy = LazyNativeClient(factory)
    await lazy.status()
    lazy.register_notification_handler("turn/started", handler)
    lazy.register_server_request_handler(
        "item/commandExecution/requestApproval",
        handler,
    )

    created[0].fail = True
    status = await lazy.status()

    assert status.remote_control_status == "ready"
    assert len(created) == 2
    assert created[0].closed is True
    assert created[1].notification_handlers == [("turn/started", handler)]
    assert created[1].server_request_handlers == [
        ("item/commandExecution/requestApproval", handler)
    ]


@pytest.mark.asyncio
async def test_lazy_native_client_does_not_retry_non_initialize_timeout() -> None:
    class TurnTimeoutClient:
        def __init__(self) -> None:
            self.closed = False

        async def start_turn(
            self,
            native_thread_id: str,
            prompt: str,
            **_kwargs: Any,
        ) -> str:
            assert native_thread_id == "thread-1"
            assert prompt == "continue"
            raise JsonRpcTimeout("Request turn/start (id=2) timed out after 60.0s")

        async def close(self) -> None:
            self.closed = True

    created: list[TurnTimeoutClient] = []

    async def factory() -> TurnTimeoutClient:
        client = TurnTimeoutClient()
        created.append(client)
        return client

    lazy = LazyNativeClient(factory)

    with pytest.raises(JsonRpcTimeout, match="turn/start"):
        await lazy.start_turn("thread-1", "continue")

    assert len(created) == 1
    assert created[0].closed is True


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
