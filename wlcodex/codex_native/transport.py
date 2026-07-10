"""Transports for Codex native remote-control JSON-RPC."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
from typing import Any, Protocol


NativeMessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
_DEFAULT_CONTROL_SOCKET = Path("~/.codex/app-server-control/app-server-control.sock")
_MACOS_CHATGPT_APP_CODEX_BINARY = Path(
    "/Applications/ChatGPT.app/Contents/Resources/codex"
)
_MACOS_CODEX_APP_BINARY = Path("/Applications/Codex.app/Contents/Resources/codex")
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class CodexNativeTransport(Protocol):
    async def start(self, on_message: NativeMessageHandler) -> None: ...

    async def send_json(self, msg: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...


def _resolve_codex_binary(binary: str) -> str:
    value = str(binary or "").strip() or "codex"
    if "/" in value:
        return value
    resolved = shutil.which(value)
    if resolved:
        return resolved
    if value == "codex":
        for app_binary in (
            _MACOS_CHATGPT_APP_CODEX_BINARY,
            _MACOS_CODEX_APP_BINARY,
        ):
            if app_binary.exists():
                return str(app_binary)
    return value


class WebSocketJsonFramer:
    """Minimal WebSocket framing for the official Unix control socket."""

    @staticmethod
    def handshake_request(host: str = "localhost", path: str = "/") -> tuple[bytes, str]:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        return request, key

    @staticmethod
    def accept_for_key(key: str) -> str:
        digest = hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        return base64.b64encode(digest).decode("ascii")

    @classmethod
    async def read_handshake_response(cls, reader: Any, key: str) -> None:
        try:
            response = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as exc:
            raise RuntimeError("incomplete Codex daemon websocket handshake") from exc

        header_text = response.decode("latin1")
        lines = header_text.split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise RuntimeError("Codex daemon websocket handshake was not accepted")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if sep:
                headers[name.lower()] = value.strip()
        expected = cls.accept_for_key(key)
        if headers.get("sec-websocket-accept") != expected:
            raise RuntimeError("Codex daemon websocket accept key did not match")

    @staticmethod
    def encode_client_text(text: str) -> bytes:
        return WebSocketJsonFramer._encode_frame(
            _OP_TEXT,
            text.encode("utf-8"),
            masked=True,
        )

    @staticmethod
    def encode_client_close() -> bytes:
        return WebSocketJsonFramer._encode_frame(_OP_CLOSE, b"", masked=True)

    @staticmethod
    def encode_client_pong(payload: bytes) -> bytes:
        return WebSocketJsonFramer._encode_frame(_OP_PONG, payload, masked=True)

    @staticmethod
    def encode_server_text(text: str) -> bytes:
        return WebSocketJsonFramer._encode_frame(
            _OP_TEXT,
            text.encode("utf-8"),
            masked=False,
        )

    @staticmethod
    def _encode_frame(opcode: int, payload: bytes, *, masked: bool) -> bytes:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        mask_bit = 0x80 if masked else 0
        if length < 126:
            header.append(mask_bit | length)
        elif length <= 0xFFFF:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        if not masked:
            return bytes(header) + payload
        mask = os.urandom(4)
        masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return bytes(header) + mask + masked_payload

    @staticmethod
    async def read_frame(reader: Any) -> tuple[int, bytes, bool]:
        header = await reader.readexactly(2)
        first, second = header
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        mask = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload, fin


class CodexDaemonTransport:
    """Connect to the official Codex daemon Unix control socket."""

    def __init__(
        self,
        binary: str,
        sock_path: Path | None = None,
        *,
        fallback_app_server: CodexNativeTransport | None = None,
    ) -> None:
        self.binary = binary
        self.sock_path = (sock_path or _DEFAULT_CONTROL_SOCKET).expanduser()
        self._fallback_app_server = fallback_app_server
        self._active_fallback: CodexNativeTransport | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None

    async def start(
        self,
        on_message: NativeMessageHandler,
    ) -> None:
        if self._writer is not None:
            raise RuntimeError("Codex daemon transport is already started")
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(self.sock_path)
            )
        except FileNotFoundError:
            if self._fallback_app_server is None:
                raise
            await self._fallback_app_server.start(on_message)
            self._active_fallback = self._fallback_app_server
            return
        request, key = WebSocketJsonFramer.handshake_request()
        self._writer.write(request)
        await self._writer.drain()
        await WebSocketJsonFramer.read_handshake_response(self._reader, key)
        self._reader_task = asyncio.create_task(self._read_stdout(on_message))

    async def send_json(self, msg: dict[str, Any]) -> None:
        if self._active_fallback is not None:
            await self._active_fallback.send_json(msg)
            return
        if self._writer is None:
            raise RuntimeError("Codex daemon transport is not started")
        raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        self._writer.write(WebSocketJsonFramer.encode_client_text(raw))
        await self._writer.drain()

    async def close(self) -> None:
        if self._active_fallback is not None:
            fallback = self._active_fallback
            self._active_fallback = None
            await fallback.close()
            return
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
            writer = self._writer
            self._reader = None
            self._writer = None
            if writer is not None:
                try:
                    writer.write(WebSocketJsonFramer.encode_client_close())
                    await writer.drain()
                except Exception:
                    pass
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    def describe(self) -> dict[str, Any]:
        if self._active_fallback is not None:
            description = dict(self._active_fallback.describe())
            description["fallback_from"] = "daemon"
            description["sock_path"] = str(self.sock_path)
            return description
        return {
            "transport": "daemon",
            "source": "daemon",
            "binary": self.binary,
            "sock_path": str(self.sock_path),
            "framing": "unix-websocket-json",
        }

    async def _read_stdout(
        self,
        on_message: NativeMessageHandler,
    ) -> None:
        if self._reader is None or self._writer is None:
            return

        text_fragments: list[bytes] = []
        while True:
            try:
                opcode, payload, fin = await WebSocketJsonFramer.read_frame(
                    self._reader
                )
            except asyncio.IncompleteReadError:
                return
            if opcode == _OP_TEXT:
                text_fragments = [payload]
                if not fin:
                    continue
            elif opcode == _OP_CONTINUATION:
                if not text_fragments:
                    continue
                text_fragments.append(payload)
                if not fin:
                    continue
            elif opcode == _OP_PING:
                self._writer.write(WebSocketJsonFramer.encode_client_pong(payload))
                await self._writer.drain()
                continue
            elif opcode == _OP_CLOSE:
                return
            else:
                continue

            message = json.loads(b"".join(text_fragments).decode("utf-8"))
            text_fragments = []
            if isinstance(message, dict):
                await on_message(message)


class CodexProxyTransport(CodexDaemonTransport):
    """Compatibility alias for the official daemon proxy transport."""


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
        remote_control: bool = True,
        spawn_process: bool = True,
    ) -> None:
        self.binary = binary
        self.listen_endpoint = listen_endpoint
        self.connect_endpoint = connect_endpoint or listen_endpoint
        self.startup_timeout_seconds = startup_timeout_seconds
        self.analytics_default_enabled = analytics_default_enabled
        self.remote_control = remote_control
        self.spawn_process = spawn_process
        self._process: asyncio.subprocess.Process | None = None
        self._websocket: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._resolved_binary = ""

    async def start(
        self,
        on_message: NativeMessageHandler,
    ) -> None:
        if self._process is not None or self._websocket is not None:
            raise RuntimeError("Codex app-server transport is already started")

        if self.spawn_process:
            self._resolved_binary = _resolve_codex_binary(self.binary)
            args = [self._resolved_binary, "app-server"]
            if self.remote_control:
                args.append("--remote-control")
            args.extend(["--listen", self.listen_endpoint])
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

    def describe(self) -> dict[str, Any]:
        return {
            "transport": "app-server",
            "source": "app-server",
            "binary": self.binary,
            "resolved_binary": self._resolved_binary or _resolve_codex_binary(self.binary),
            "endpoint": self.connect_endpoint,
            "listen_endpoint": self.listen_endpoint,
            "framing": "websocket-json",
            "spawn_process": self.spawn_process,
            "remote_control": self.remote_control,
        }

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
        on_message: NativeMessageHandler,
    ) -> None:
        websocket = self._websocket
        if websocket is None:
            return
        async for raw in websocket:
            message = json.loads(raw)
            if isinstance(message, dict):
                await on_message(message)


def create_codex_native_transport(
    *,
    transport: str,
    binary: str,
    sock_path: Path | None,
    listen_endpoint: str,
    startup_timeout_seconds: float,
    remote_control: bool = True,
) -> CodexNativeTransport:
    if transport in {"daemon", "proxy"}:
        return CodexDaemonTransport(
            binary=binary,
            sock_path=sock_path,
            fallback_app_server=CodexAppServerWebSocketTransport(
                binary=binary,
                listen_endpoint=listen_endpoint,
                startup_timeout_seconds=startup_timeout_seconds,
                remote_control=remote_control,
            ),
        )
    if transport == "app-server":
        return CodexAppServerWebSocketTransport(
            binary=binary,
            listen_endpoint=listen_endpoint,
            startup_timeout_seconds=startup_timeout_seconds,
            remote_control=remote_control,
        )
    raise ValueError(f"unknown Codex native transport: {transport!r}")
