from __future__ import annotations

import asyncio
import json

import pytest

from wlcodex.codex_native.transport import (
    CodexAppServerWebSocketTransport,
    CodexProxyTransport,
)


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class _BadStdout:
    async def readline(self) -> bytes:
        return b"{not-json\n"


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _BadStdout()
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


@pytest.mark.asyncio
async def test_proxy_transport_close_cleans_process_after_reader_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    transport = CodexProxyTransport("codex")
    await transport.start(lambda _message: None)
    await asyncio.sleep(0)

    await transport.close()

    assert process.stdin.closed is True
    assert process.terminated is True


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
