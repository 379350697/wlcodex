"""Transport-agnostic JSON-RPC 2.0 client for Codex app-server communication."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


class JsonRpcTimeout(RuntimeError):
    """Raised when a JSON-RPC request does not receive a response in time."""


@dataclass
class JsonRpcClient:
    """JSON-RPC 2.0 client.

    Accepts async send_json callable for transport-agnostic operation.
    Tests can inject a fake transport without WebSocket.

    Server requests (methods with an id) are held open — their response
    is deferred until resolve_server_request() is called.  This lets
    approval flows wait for a Telegram button click before replying.
    """

    send_json: Callable[[dict[str, Any]], Coroutine[None, None, None]]
    request_timeout_seconds: float = 60.0
    _next_id: int = field(default=0, init=False)
    _pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict, init=False)
    _notification_handlers: dict[str, list[Callable[..., Coroutine[None, None, None]]]] = field(
        default_factory=dict, init=False
    )
    _server_request_handlers: dict[
        str, Callable[..., Coroutine[None, None, None]]
    ] = field(default_factory=dict, init=False)
    _held_requests: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict, init=False
    )
    _held_request_raw_ids: dict[str, Any] = field(default_factory=dict, init=False)

    def _issue_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        rid = self._issue_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.Future()
        self._pending[rid] = future

        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        }
        await self.send_json(msg)

        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout_seconds)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise JsonRpcTimeout(
                f"Request {method} (id={rid}) timed out after "
                f"{self.request_timeout_seconds}s"
            ) from None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await self.send_json(msg)

    def on_notification(
        self, method: str, handler: Callable[..., Coroutine[None, None, None]]
    ) -> None:
        self._notification_handlers.setdefault(method, []).append(handler)

    def on_server_request(
        self,
        method: str,
        handler: Callable[..., Coroutine[None, None, None]],
    ) -> None:
        """Register a handler for a server-request method.

        The handler receives (params: dict, request_id: str) and should
        arrange for `resolve_server_request(request_id, result)` to be
        called later.  It must NOT return a value — the response is
        sent only when resolve_server_request() is called.
        """
        self._server_request_handlers[method] = handler

    def resolve_server_request(self, request_id: str, result: dict[str, Any]) -> None:
        """Complete a held server request with a result."""
        future = self._held_requests.pop(request_id, None)
        raw_id = self._held_request_raw_ids.pop(request_id, request_id)
        if future is not None and not future.done():
            setattr(future, "_wlcodex_jsonrpc_raw_id", raw_id)
            future.set_result(result)

    def reject_server_request(self, request_id: str, code: int, message: str) -> None:
        """Reject a held server request with an error."""
        future = self._held_requests.pop(request_id, None)
        raw_id = self._held_request_raw_ids.pop(request_id, request_id)
        if future is not None and not future.done():
            setattr(future, "_wlcodex_jsonrpc_raw_id", raw_id)
            future.set_exception(JsonRpcError(code=code, message=message))

    async def receive_message(self, message: dict[str, Any]) -> None:
        """Deliver an incoming JSON-RPC message."""
        if "id" in message and "method" not in message:
            # Response to a request
            rid = message["id"]
            future = self._pending.pop(rid, None)
            if future is None:
                return
            if "error" in message:
                err = message["error"]
                future.set_exception(
                    JsonRpcError(
                        code=int(err.get("code", -1)),
                        message=str(err.get("message", "unknown")),
                        data=err.get("data"),
                    )
                )
            else:
                future.set_result(message.get("result", {}))
        elif "id" in message and "method" in message:
            # Server request — hold the response until resolved externally.
            # The response sender runs in a background task so the receive
            # loop is never blocked waiting for a Telegram button click.
            method = str(message["method"])
            raw_rid = message["id"]
            rid = str(raw_rid)
            handler = self._server_request_handlers.get(method)
            if handler is None:
                await self.send_json({
                    "jsonrpc": "2.0",
                    "id": raw_rid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
                return

            future: asyncio.Future[dict[str, Any]] = asyncio.Future()
            self._held_requests[rid] = future
            self._held_request_raw_ids[rid] = raw_rid

            try:
                await handler(message.get("params", {}), rid)
            except Exception:
                if not future.done():
                    future.set_exception(
                        JsonRpcError(code=-32000, message="Approval handler error")
                    )

            asyncio.create_task(self._send_held_response(rid, future))
            return
        else:
            # Notification (no id)
            method = str(message.get("method", ""))
            handlers = self._notification_handlers.get(method, [])
            params = message.get("params", {})
            for handler in handlers:
                try:
                    await handler(params)
                except Exception:
                    pass

    async def _send_held_response(
        self, rid: str, future: asyncio.Future[dict[str, Any]]
    ) -> None:
        """Wait for the held future and send the JSON-RPC response.

        Runs as a background task so the receive loop is never blocked.
        """
        try:
            result = await future
            raw_id = getattr(future, "_wlcodex_jsonrpc_raw_id", rid)
            await self.send_json({"jsonrpc": "2.0", "id": raw_id, "result": result})
        except JsonRpcError as exc:
            raw_id = getattr(future, "_wlcodex_jsonrpc_raw_id", rid)
            await self.send_json({
                "jsonrpc": "2.0",
                "id": raw_id,
                "error": {"code": exc.code, "message": exc.rpc_message},
            })
        except asyncio.CancelledError:
            pass
        finally:
            self._held_requests.pop(rid, None)
            self._held_request_raw_ids.pop(rid, None)

    async def close(self) -> None:
        for rid, future in self._pending.items():
            if not future.done():
                future.cancel()
        self._pending.clear()
        for rid, future in self._held_requests.items():
            if not future.done():
                future.cancel()
        self._held_requests.clear()
        self._held_request_raw_ids.clear()
        self._notification_handlers.clear()
        self._server_request_handlers.clear()
