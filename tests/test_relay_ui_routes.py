from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer
from wlcodex.native_agents.models import NativeAgentCapabilities
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore
from wlcodex.runtime_event_store import RuntimeEventStore


class FakeProvider:
    provider = "claude"
    provider_engine = "sdk-test"

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(can_start_session=True)

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        raise AssertionError("UI route tests should not start providers")


class FakeCodexProvider(FakeProvider):
    provider = "codex"
    provider_engine = "app-server"


class FakeAntigravityProvider(FakeProvider):
    provider = "antigravity"
    provider_engine = "cli-local"


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


def _server(tmp_path: Path, relay_service: RelayService | None = None):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    service = relay_service or RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([FakeProvider()]),
        default_provider="claude",
    )
    providers = [FakeCodexProvider(), FakeProvider(), FakeAntigravityProvider()]
    return WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(RuntimeEventStore(ledger._conn)),
        native_registry=NativeAgentRegistry(providers),
        relay_service=service,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    ), service


async def _request(tmp_path: Path, request: str, relay_service: RelayService | None = None):
    server, service = _server(tmp_path, relay_service)
    await server.start()
    try:
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()
    return response, service


@pytest.mark.asyncio
async def test_native_index_shows_relay_card_and_preserves_token(tmp_path: Path) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "Codex" in response
    assert "Claude" in response
    assert "Antigravity" in response
    assert "议会审核" in response
    assert "流式接力" in response
    assert 'href="/native/workflows/relay?token=secret"' in response
    assert '<span>工作流</span>' not in response


@pytest.mark.asyncio
async def test_workflow_directory_links_to_relay_council_and_dev_flow(tmp_path: Path) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native/workflows?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "流式接力模式" in response
    assert 'href="/native/workflows/relay?token=secret"' in response
    assert "议会审核" in response
    assert 'href="/council?token=secret"' in response
    assert "Dev Flow" in response


@pytest.mark.asyncio
async def test_relay_task_list_is_workspace_not_session_list(tmp_path: Path) -> None:
    response, service = await _request(
        tmp_path,
        "GET /native/workflows/relay?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "流式接力" in response
    assert "任务历史" in response
    assert "新接力任务" in response
    assert "新聊天" not in response
    assert '<section class="relay-create-panel" id="new-task-panel" hidden>' in response
    assert "发布大任务" not in response
    assert "还没有接力任务" in response
    assert "暂无任务" not in response
    assert "relay-group" not in response
    assert 'data-filter="running"' in response
    assert 'data-filter="waiting_user"' in response
    assert 'data-filter="blocked"' in response
    assert 'data-filter="completed"' in response
    assert 'data-filter="interrupted"' in response
    assert 'list="relay-workspaces"' in response
    assert '<datalist id="relay-workspaces">' in response
    assert 'aria-label="relay task history"' in response
    assert "native session list" not in response.lower()

    task = service.create_task(
        title="Long relay title wraps without overlap",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    populated, _ = await _request(
        tmp_path,
        "GET /native/workflows/relay?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )
    assert "Long relay title wraps without overlap" in populated
    assert '<option value="/repo"></option>' in populated
    assert 'class="relay-task-card"' in populated
    assert 'class="relay-status-badge"' in populated
    assert "总工程师" in populated
    assert "架构工程师" in populated
    assert "开发工程师" in populated
    assert "测试工程师" in populated
    assert "审计工程师" in populated
    assert "等待总工程师接收" in populated
    assert "打开任务" in populated
    assert "open task" not in populated
    assert f'/native/workflows/relay/tasks/{task.id}?token=secret' in populated


@pytest.mark.asyncio
async def test_relay_task_detail_renders_five_role_lanes_and_idle_roles(
    tmp_path: Path,
) -> None:
    server, service = _server(tmp_path)
    task = service.create_task(
        title="Detail task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            f"GET /native/workflows/relay/tasks/{task.id}?token=secret HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "RelayBoard" in response
    for display_name in ["总工程师", "架构工程师", "开发工程师", "测试工程师", "审计工程师"]:
        assert display_name in response
    assert 'data-role="director"' in response
    assert 'data-role="architect"' in response
    assert "idle" in response
    assert f"/api/relay/tasks/{task.id}/message" in response
    assert "relay-board-grid" in response
