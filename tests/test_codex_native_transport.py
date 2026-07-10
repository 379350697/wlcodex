from __future__ import annotations

import asyncio
import json

import pytest

from wlcodex.codex_native import transport as transport_module
from wlcodex.codex_native.transport import (
    CodexAppServerWebSocketTransport,
    CodexDaemonTransport,
    WebSocketJsonFramer,
    create_codex_native_transport,
)


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> None:
        pass


class _FakeUnixWriter:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        *,
        server_messages: list[dict[str, object]] | None = None,
    ) -> None:
        self.reader = reader
        self.server_messages = server_messages or []
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        if data.startswith(b"GET "):
            headers = data.decode("ascii").split("\r\n")
            key = ""
            for header in headers:
                name, sep, value = header.partition(":")
                if sep and name.lower() == "sec-websocket-key":
                    key = value.strip()
                    break
            accept = WebSocketJsonFramer.accept_for_key(key)
            self.reader.feed_data(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "connection: Upgrade\r\n"
                    "upgrade: websocket\r\n"
                    f"sec-websocket-accept: {accept}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            for message in self.server_messages:
                self.reader.feed_data(
                    WebSocketJsonFramer.encode_server_text(
                        json.dumps(message, separators=(",", ":"))
                    )
                )

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        pass


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_websocket_framer_reads_fragmented_and_multiple_frames() -> None:
    first = WebSocketJsonFramer.encode_server_text(
        json.dumps({"jsonrpc": "2.0", "id": 1}, separators=(",", ":"))
    )
    second = WebSocketJsonFramer.encode_server_text(
        json.dumps({"jsonrpc": "2.0", "id": 2}, separators=(",", ":"))
    )
    reader = asyncio.StreamReader()

    pending = asyncio.create_task(WebSocketJsonFramer.read_frame(reader))
    reader.feed_data(first[:7])
    await asyncio.sleep(0)
    assert pending.done() is False

    reader.feed_data(first[7:] + second)
    reader.feed_eof()

    opcode, payload, fin = await pending
    assert opcode == 1
    assert fin is True
    assert json.loads(payload.decode("utf-8")) == {"jsonrpc": "2.0", "id": 1}
    opcode, payload, fin = await WebSocketJsonFramer.read_frame(reader)
    assert opcode == 1
    assert fin is True
    assert json.loads(payload.decode("utf-8")) == {"jsonrpc": "2.0", "id": 2}


@pytest.mark.asyncio
async def test_websocket_framer_masks_client_text_frames() -> None:
    raw = WebSocketJsonFramer.encode_client_text(
        json.dumps({"jsonrpc": "2.0", "method": "ping"}, separators=(",", ":"))
    )
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()

    assert raw[1] & 0x80
    opcode, payload, fin = await WebSocketJsonFramer.read_frame(reader)
    assert opcode == 1
    assert fin is True
    assert json.loads(payload.decode("utf-8")) == {
        "jsonrpc": "2.0",
        "method": "ping",
    }


@pytest.mark.asyncio
async def test_daemon_transport_uses_official_control_socket_websocket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    reader = asyncio.StreamReader()
    writer = _FakeUnixWriter(
        reader,
        server_messages=[{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}],
    )
    opened_path = ""

    async def fake_open_unix_connection(path: str):
        nonlocal opened_path
        opened_path = path
        return reader, writer

    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )
    received: list[dict[str, object]] = []

    async def on_message(message: dict[str, object]) -> None:
        received.append(message)

    sock_path = tmp_path / "app-server-control.sock"
    transport = CodexDaemonTransport("codex", sock_path=sock_path)
    await transport.start(on_message)
    await transport.send_json({"jsonrpc": "2.0", "method": "ping"})
    await asyncio.sleep(0)
    await transport.close()

    assert opened_path == str(sock_path)
    assert received == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}]
    raw = writer.writes[-2]
    reader_for_client_frame = asyncio.StreamReader()
    reader_for_client_frame.feed_data(raw)
    reader_for_client_frame.feed_eof()
    opcode, payload, fin = await WebSocketJsonFramer.read_frame(reader_for_client_frame)
    assert opcode == 1
    assert fin is True
    assert json.loads(payload.decode("utf-8")) == {
        "jsonrpc": "2.0",
        "method": "ping",
    }
    assert transport.describe()["source"] == "daemon"
    assert transport.describe()["framing"] == "unix-websocket-json"
    assert writer.closed is True


def test_transport_factory_defaults_to_daemon_and_keeps_legacy_explicit(tmp_path) -> None:
    daemon = create_codex_native_transport(
        transport="daemon",
        binary="codex",
        sock_path=tmp_path / "app-server-control.sock",
        listen_endpoint="ws://127.0.0.1:18742",
        startup_timeout_seconds=3.0,
    )
    legacy = create_codex_native_transport(
        transport="app-server",
        binary="codex",
        sock_path=None,
        listen_endpoint="ws://127.0.0.1:18742",
        startup_timeout_seconds=3.0,
    )
    proxy_alias = create_codex_native_transport(
        transport="proxy",
        binary="codex",
        sock_path=tmp_path / "app-server-control.sock",
        listen_endpoint="ws://127.0.0.1:18742",
        startup_timeout_seconds=3.0,
    )

    assert isinstance(daemon, CodexDaemonTransport)
    assert daemon.describe()["source"] == "daemon"
    assert isinstance(proxy_alias, CodexDaemonTransport)
    assert isinstance(legacy, CodexAppServerWebSocketTransport)
    assert legacy.describe()["source"] == "app-server"
    assert legacy.describe()["endpoint"] == "ws://127.0.0.1:18742"


@pytest.mark.asyncio
async def test_daemon_factory_falls_back_to_app_server_when_socket_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    spawned_args: tuple[object, ...] | None = None
    fake_websocket = _FakeWebSocket()

    async def fake_open_unix_connection(_path: str):
        raise FileNotFoundError("missing control socket")

    async def fake_create_subprocess_exec(*args, **_kwargs):
        nonlocal spawned_args
        spawned_args = args
        return _FakeProcess()

    async def fake_connect(endpoint: str, max_size: object = None) -> _FakeWebSocket:
        assert endpoint == "ws://127.0.0.1:18742"
        assert max_size is None
        return fake_websocket

    import websockets

    monkeypatch.setattr(transport_module.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(
        asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)

    transport = create_codex_native_transport(
        transport="daemon",
        binary="codex",
        sock_path=tmp_path / "missing.sock",
        listen_endpoint="ws://127.0.0.1:18742",
        startup_timeout_seconds=3.0,
    )
    await transport.start(lambda _message: None)
    await transport.send_json({"jsonrpc": "2.0", "method": "ping"})
    await transport.close()

    assert spawned_args == (
        "codex",
        "app-server",
        "--remote-control",
        "--listen",
        "ws://127.0.0.1:18742",
        "--analytics-default-enabled",
    )
    assert json.loads(fake_websocket.sent[0]) == {
        "jsonrpc": "2.0",
        "method": "ping",
    }


@pytest.mark.asyncio
async def test_app_server_websocket_transport_spawns_remote_control_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned_args: tuple[object, ...] | None = None
    spawned_kwargs: dict[str, object] | None = None

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal spawned_args, spawned_kwargs
        spawned_args = args
        spawned_kwargs = kwargs
        return _FakeProcess()

    async def fake_connect(endpoint: str, max_size: object = None) -> _FakeWebSocket:
        assert endpoint == "ws://127.0.0.1:18742"
        assert max_size is None
        return _FakeWebSocket()

    import websockets

    monkeypatch.setattr(transport_module.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)

    transport = CodexAppServerWebSocketTransport("codex")
    await transport.start(lambda _message: None)
    await transport.close()

    assert spawned_args == (
        "codex",
        "app-server",
        "--remote-control",
        "--listen",
        "ws://127.0.0.1:18742",
        "--analytics-default-enabled",
    )
    assert spawned_kwargs is not None
    assert spawned_kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert spawned_kwargs["stdout"] is asyncio.subprocess.DEVNULL
    assert spawned_kwargs["stderr"] is asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_app_server_websocket_transport_resolves_macos_app_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    spawned_args: tuple[object, ...] | None = None
    macos_binary = tmp_path / "Codex.app" / "Contents" / "Resources" / "codex"
    macos_binary.parent.mkdir(parents=True)
    macos_binary.write_text("#!/bin/sh\n", encoding="utf-8")

    async def fake_create_subprocess_exec(*args, **_kwargs):
        nonlocal spawned_args
        spawned_args = args
        return _FakeProcess()

    async def fake_connect(endpoint: str, max_size: object = None) -> _FakeWebSocket:
        assert endpoint == "ws://127.0.0.1:18742"
        assert max_size is None
        return _FakeWebSocket()

    import websockets

    monkeypatch.setattr(transport_module.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(
        transport_module,
        "_MACOS_CHATGPT_APP_CODEX_BINARY",
        tmp_path / "missing-ChatGPT.app" / "codex",
    )
    monkeypatch.setattr(transport_module, "_MACOS_CODEX_APP_BINARY", macos_binary)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)

    transport = CodexAppServerWebSocketTransport("codex")
    await transport.start(lambda _message: None)
    await transport.close()

    assert spawned_args is not None
    assert spawned_args[0] == str(macos_binary)


@pytest.mark.asyncio
async def test_app_server_websocket_transport_prefers_chatgpt_app_binary_when_codex_is_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    spawned_args: tuple[object, ...] | None = None
    chatgpt_binary = tmp_path / "ChatGPT.app" / "Contents" / "Resources" / "codex"
    chatgpt_binary.parent.mkdir(parents=True)
    chatgpt_binary.write_text("#!/bin/sh\n", encoding="utf-8")

    async def fake_create_subprocess_exec(*args, **_kwargs):
        nonlocal spawned_args
        spawned_args = args
        return _FakeProcess()

    async def fake_connect(endpoint: str, max_size: object = None) -> _FakeWebSocket:
        assert endpoint == "ws://127.0.0.1:18742"
        assert max_size is None
        return _FakeWebSocket()

    import websockets

    monkeypatch.setattr(transport_module.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(
        transport_module,
        "_MACOS_CHATGPT_APP_CODEX_BINARY",
        chatgpt_binary,
    )
    monkeypatch.setattr(
        transport_module,
        "_MACOS_CODEX_APP_BINARY",
        tmp_path / "missing-Codex.app" / "codex",
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)

    transport = CodexAppServerWebSocketTransport("codex")
    await transport.start(lambda _message: None)
    await transport.close()

    assert spawned_args is not None
    assert spawned_args[0] == str(chatgpt_binary)


@pytest.mark.asyncio
async def test_app_server_websocket_transport_can_disable_remote_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned_args: tuple[object, ...] | None = None

    async def fake_create_subprocess_exec(*args, **_kwargs):
        nonlocal spawned_args
        spawned_args = args
        return _FakeProcess()

    async def fake_connect(endpoint: str, max_size: object = None) -> _FakeWebSocket:
        assert endpoint == "ws://127.0.0.1:18742"
        assert max_size is None
        return _FakeWebSocket()

    import websockets

    monkeypatch.setattr(transport_module.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(websockets, "connect", fake_connect)

    transport = CodexAppServerWebSocketTransport("codex", remote_control=False)
    await transport.start(lambda _message: None)
    await transport.close()

    assert spawned_args == (
        "codex",
        "app-server",
        "--listen",
        "ws://127.0.0.1:18742",
        "--analytics-default-enabled",
    )


@pytest.mark.asyncio
async def test_app_server_websocket_transport_can_connect_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = False
    sent: list[str] = []

    async def forbidden_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("connect-only transport must not spawn app-server")

    class FakeWebSocket:
        async def send(self, raw: str) -> None:
            sent.append(raw)

        async def close(self) -> None:
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def fake_connect(endpoint: str, max_size: object = None) -> FakeWebSocket:
        assert endpoint == "ws://127.0.0.1:18742"
        assert max_size is None
        return FakeWebSocket()

    import websockets

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    monkeypatch.setattr(websockets, "connect", fake_connect)

    transport = CodexAppServerWebSocketTransport(
        "codex",
        spawn_process=False,
    )
    await transport.start(lambda _message: None)
    await transport.send_json({"jsonrpc": "2.0", "method": "ping"})
    await transport.close()

    assert spawned is False
    assert json.loads(sent[0]) == {"jsonrpc": "2.0", "method": "ping"}
