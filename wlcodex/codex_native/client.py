"""Client for Codex native app-server JSON-RPC control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from wlcodex.codex_backend import (
    build_turn_start_params,
    build_turn_steer_params,
    parse_turn_response,
)
from wlcodex.codex_native.models import NativeCodexStatus
from wlcodex.jsonrpc import JsonRpcClient


class CodexNativeClient:
    def __init__(
        self,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
        close: Callable[[], Awaitable[None]],
        request_timeout_seconds: float = 60.0,
    ) -> None:
        self.rpc = JsonRpcClient(
            send_json=send_json,
            request_timeout_seconds=request_timeout_seconds,
        )
        self._close = close
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self.rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "wlcodex", "version": "1.0.0"},
                    "capabilities": None,
                },
            )
            await self.rpc.notify("initialized")
            self._initialized = True

    async def status(self) -> NativeCodexStatus:
        await self.initialize()
        return NativeCodexStatus(
            enabled=True,
            connected=True,
            remote_control_status="connected",
            server_name="local Codex app-server",
        )

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        result = await self.rpc.request(
            "thread/list",
            {
                "limit": limit,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": False,
                "useStateDbOnly": True,
            },
        )
        items = result.get("data")
        if not isinstance(items, list):
            items = result.get("threads")
        if not isinstance(items, list):
            items = result.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    async def read_session(
        self,
        native_thread_id: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        await self.initialize()
        return await self.rpc.request(
            "thread/read",
            {"threadId": native_thread_id, "includeTurns": include_turns},
        )

    async def attach_session(self, native_thread_id: str) -> dict[str, Any]:
        await self.initialize()
        return await self.rpc.request(
            "thread/resume",
            {"threadId": native_thread_id},
        )

    async def continue_session(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        await self.initialize()
        await self.rpc.request("thread/resume", {"threadId": native_thread_id})
        result = await self.rpc.request(
            "turn/start",
            build_turn_start_params(
                native_thread_id,
                prompt,
                model=model,
                images=images,
            ),
        )
        return parse_turn_response(result)

    async def steer_turn(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
        *,
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.initialize()
        await self.rpc.request(
            "turn/steer",
            build_turn_steer_params(
                native_thread_id,
                expected_turn_id,
                prompt,
                images=images,
            ),
        )

    async def interrupt_turn(self, native_thread_id: str, turn_id: str) -> None:
        await self.initialize()
        await self.rpc.request(
            "turn/interrupt",
            {"threadId": native_thread_id, "turnId": turn_id},
        )

    def register_notification_handler(
        self,
        method: str,
        handler: Callable[..., Awaitable[None]],
    ) -> None:
        self.rpc.on_notification(method, handler)

    def register_server_request_handler(
        self,
        method: str,
        handler: Callable[..., Awaitable[None]],
    ) -> None:
        self.rpc.on_server_request(method, handler)

    def resolve_request(self, request_id: str, result: dict[str, Any]) -> None:
        self.rpc.resolve_server_request(request_id, result)

    async def close(self) -> None:
        await self.rpc.close()
        await self._close()


class LazyNativeClient:
    """Defer starting the native Codex proxy until the first control request."""

    def __init__(
        self,
        factory: Callable[[], Awaitable[CodexNativeClient]],
    ) -> None:
        self._factory = factory
        self._client: CodexNativeClient | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._pending_notification_handlers: list[
            tuple[str, Callable[..., Awaitable[None]]]
        ] = []
        self._pending_server_request_handlers: list[
            tuple[str, Callable[..., Awaitable[None]]]
        ] = []

    async def _get(self) -> CodexNativeClient:
        if self._closed:
            raise RuntimeError("Codex native client is closed")
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._closed:
                raise RuntimeError("Codex native client is closed")
            if self._client is None:
                client = await self._factory()
                if self._closed:
                    await client.close()
                    raise RuntimeError("Codex native client is closed")
                for method, handler in self._pending_notification_handlers:
                    client.register_notification_handler(method, handler)
                for method, handler in self._pending_server_request_handlers:
                    client.register_server_request_handler(method, handler)
                self._client = client
        return self._client

    async def status(self) -> NativeCodexStatus:
        return await (await self._get()).status()

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return await (await self._get()).list_sessions(limit=limit)

    async def read_session(
        self,
        native_thread_id: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        return await (await self._get()).read_session(
            native_thread_id,
            include_turns=include_turns,
        )

    async def attach_session(self, native_thread_id: str) -> dict[str, Any]:
        return await (await self._get()).attach_session(native_thread_id)

    async def continue_session(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        return await (await self._get()).continue_session(
            native_thread_id,
            prompt,
            model=model,
            images=images,
        )

    async def steer_turn(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
        *,
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        await (await self._get()).steer_turn(
            native_thread_id,
            expected_turn_id,
            prompt,
            images=images,
        )

    async def interrupt_turn(self, native_thread_id: str, turn_id: str) -> None:
        await (await self._get()).interrupt_turn(native_thread_id, turn_id)

    def register_notification_handler(
        self,
        method: str,
        handler: Callable[..., Awaitable[None]],
    ) -> None:
        if self._client is not None:
            self._client.register_notification_handler(method, handler)
            return
        self._pending_notification_handlers.append((method, handler))

    def register_server_request_handler(
        self,
        method: str,
        handler: Callable[..., Awaitable[None]],
    ) -> None:
        if self._client is not None:
            self._client.register_server_request_handler(method, handler)
            return
        self._pending_server_request_handlers.append((method, handler))

    def resolve_request(self, request_id: str, result: dict[str, Any]) -> None:
        if self._client is None:
            raise RuntimeError("Codex native client has no active approval request")
        self._client.resolve_request(request_id, result)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            if self._client is not None:
                await self._client.close()
                self._client = None
