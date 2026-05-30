"""Stdio transport for Codex native app-server proxy."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
import subprocess
from typing import Any


class CodexProxyTransport:
    def __init__(self, binary: str, sock_path: Path | None = None) -> None:
        self.binary = binary
        self.sock_path = sock_path
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None

    async def start(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._process is not None:
            raise RuntimeError("Codex proxy transport is already started")
        args = [self.binary, "app-server", "proxy"]
        if self.sock_path is not None:
            args.extend(["--sock", str(self.sock_path)])

        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(on_message))

    async def send_json(self, msg: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Codex proxy transport is not started")
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def close(self) -> None:
        try:
            if self._reader_task is not None:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        finally:
            self._reader_task = None
            if self._process is not None:
                process = self._process
                self._process = None
                if process.stdin is not None:
                    process.stdin.close()
                    await process.stdin.wait_closed()
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()

    async def _read_stdout(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._process is None or self._process.stdout is None:
            return

        while True:
            line = await self._process.stdout.readline()
            if not line:
                return
            message = json.loads(line.decode("utf-8"))
            if isinstance(message, dict):
                await on_message(message)


class CodexAppServerWebSocketTransport:
    """Start a local Codex app-server and speak JSON-RPC over WebSocket."""

    def __init__(
        self,
        binary: str,
        *,
        listen_endpoint: str = "ws://127.0.0.1:18742",
        connect_endpoint: str | None = None,
        startup_timeout_seconds: float = 10.0,
        analytics_default_enabled: bool = True,
        spawn_process: bool = True,
    ) -> None:
        self.binary = binary
        self.listen_endpoint = listen_endpoint
        self.connect_endpoint = connect_endpoint or listen_endpoint
        self.startup_timeout_seconds = startup_timeout_seconds
        self.analytics_default_enabled = analytics_default_enabled
        self.spawn_process = spawn_process
        self._process: asyncio.subprocess.Process | None = None
        self._websocket: Any = None
        self._reader_task: asyncio.Task[None] | None = None

    async def start(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._process is not None or self._websocket is not None:
            raise RuntimeError("Codex app-server transport is already started")

        if self.spawn_process:
            args = [self.binary, "app-server", "--listen", self.listen_endpoint]
            if self.analytics_default_enabled:
                args.append("--analytics-default-enabled")
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self._websocket = await self._connect_with_retry()
        self._reader_task = asyncio.create_task(self._read_websocket(on_message))

    async def send_json(self, msg: dict[str, Any]) -> None:
        if self._websocket is None:
            raise RuntimeError("Codex app-server transport is not started")
        await self._websocket.send(
            json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        )

    async def close(self) -> None:
        try:
            if self._reader_task is not None:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        finally:
            self._reader_task = None
            websocket = self._websocket
            self._websocket = None
            if websocket is not None:
                await websocket.close()
            if self._process is not None:
                process = self._process
                self._process = None
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()

    async def _connect_with_retry(self) -> Any:
        import websockets

        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
        last_error: Exception | None = None
        while True:
            if self._process is not None and self._process.returncode is not None:
                raise RuntimeError(
                    f"Codex app-server exited with code {self._process.returncode}"
                )
            try:
                return await websockets.connect(self.connect_endpoint, max_size=None)
            except Exception as exc:
                last_error = exc
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        f"Timed out connecting to Codex app-server at "
                        f"{self.connect_endpoint}: {last_error}"
                    ) from last_error
                await asyncio.sleep(0.1)

    async def _read_websocket(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        async for raw in websocket:
            message = json.loads(raw)
            if isinstance(message, dict):
                await on_message(message)
