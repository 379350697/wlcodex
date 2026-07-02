from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.live_stream import server as live_server
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.runtime_event_store import RuntimeEventStore


class FakeCouncilProvider:
    provider = "claude"
    provider_engine = "sdk-deepseek"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def status(self) -> NativeAgentStatus:
        self.calls.append(("status",))
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self) -> NativeAgentCapabilities:
        self.calls.append(("capabilities",))
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_read_history=True,
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        self.calls.append(("list_sessions", limit))
        return []

    async def list_models(self) -> list[dict[str, str]]:
        self.calls.append(("list_models",))
        return [{"id": "deepseek-v4"}]

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self.calls.append(("start_session", cwd, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"council-{len(self.calls)}",
            agent_run_id=10 + len(self.calls),
            status="started",
        )

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        self.calls.append(("read_session", native_session_id))
        return {"turns": []}


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


async def _request_council(
    tmp_path: Path,
    request: str,
    *,
    provider: FakeCouncilProvider | None = None,
) -> tuple[str, FakeCouncilProvider]:
    fake_provider = provider or FakeCouncilProvider()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(_store(tmp_path)),
        native_registry=NativeAgentRegistry([fake_provider]),
    )
    await server.start()
    try:
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()
    return response, fake_provider


@pytest.mark.asyncio
async def test_council_review_page_is_available_from_web(tmp_path: Path) -> None:
    response, provider = await _request_council(
        tmp_path,
        "GET /council HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "<title>议会审核</title>" in response
    assert 'href="/council/seats"' in response
    assert 'const DEFAULT_CONFIG_URL = "/api/council/config/default";' in response
    assert 'const PROJECTS_URL = "/api/council/projects";' in response
    assert 'const POLL_INTERVAL_MS = 1200;' in response
    assert "Review Packet" in response
    assert '<select id="cwd">' in response
    assert '<input id="cwd"' not in response
    assert "打开原生会话" in response
    assert "function setRunBusy" in response
    assert "function boardStatusLabel" in response
    assert "function seatStatusLabel" in response
    assert "当前议会还在审核中" in response
    assert "议会审核中..." in response
    assert "部分席位已启动，等待输出" in response
    assert "已启动，等待输出" in response
    assert "运行状态：" not in response
    assert provider.calls == []


@pytest.mark.asyncio
async def test_council_seat_config_page_lists_five_roles(tmp_path: Path) -> None:
    response, provider = await _request_council(
        tmp_path,
        "GET /council/seats HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "<title>议会席位配置</title>" in response
    assert "唱反调" in response
    assert "第一性原理" in response
    assert "扩展思路" in response
    assert "局外人" in response
    assert "执行者" in response
    assert 'href="/council"' in response
    assert provider.calls == []


@pytest.mark.asyncio
async def test_native_root_links_to_council_pages(tmp_path: Path) -> None:
    response, _provider = await _request_council(
        tmp_path,
        "GET /native HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "/council" in response
    assert "议会审核" in response
    assert "/council/seats" not in response
    assert "席位配置" not in response


@pytest.mark.asyncio
async def test_council_projects_api_lists_projects_root_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    (projects_root / "wlcodex").mkdir()
    (projects_root / "LightFeeV2").mkdir()
    (projects_root / ".hidden").mkdir()
    (projects_root / "README.md").write_text("not a project")
    monkeypatch.setattr(
        live_server,
        "_COUNCIL_PROJECTS_ROOT",
        projects_root,
        raising=False,
    )

    response, provider = await _request_council(
        tmp_path,
        "GET /api/council/projects HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload == {
        "root": str(projects_root),
        "projects": [
            {"name": "LightFeeV2", "cwd": str(projects_root / "LightFeeV2")},
            {"name": "wlcodex", "cwd": str(projects_root / "wlcodex")},
        ],
    }
    assert provider.calls == []


@pytest.mark.asyncio
async def test_council_projects_api_prefers_configured_workspace_catalog(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        workspace_catalog=(
            WorkspaceConfig("wlcodex", tmp_path / "wlcodex", True),
            WorkspaceConfig("lightfee", tmp_path / "LightFeeV2", True),
        ),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/council/projects HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "root": "",
        "projects": [
            {
                "alias": "wlcodex",
                "name": "wlcodex",
                "cwd": str(tmp_path / "wlcodex"),
                "allow_write": True,
            },
            {
                "alias": "lightfee",
                "name": "lightfee",
                "cwd": str(tmp_path / "LightFeeV2"),
                "allow_write": True,
            },
        ],
    }


@pytest.mark.asyncio
async def test_default_council_config_api_returns_five_assignments(tmp_path: Path) -> None:
    response, provider = await _request_council(
        tmp_path,
        "GET /api/council/config/default HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["mode"] == "council"
    assert [seat["role"] for seat in payload["seat_definitions"]] == [
        "唱反调",
        "第一性原理",
        "扩展思路",
        "局外人",
        "执行者",
    ]
    assert len(payload["assignments"]) == 5
    assert {assignment["provider"] for assignment in payload["assignments"]} == {
        "claude"
    }
    assert {assignment["model"] for assignment in payload["assignments"]} == {
        "deepseek-v4"
    }
    assert payload["providers"][0]["provider"] == "claude"
    assert payload["models"]["claude"] == [{"id": "deepseek-v4"}]
    assert provider.calls == [("list_models",)]


@pytest.mark.asyncio
async def test_council_run_api_starts_native_review_session(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "title": "Web Council",
            "proposal": "Review a web-submitted proposal.",
            "cwd": "/repo",
            "config": {
                "mode": "council",
                "assignments": [
                    {
                        "seat_id": "contrarian",
                        "provider": "claude",
                        "model": "deepseek-v4",
                        "enabled": True,
                    }
                ],
                "required_seat_ids": ["contrarian"],
            },
        }
    )
    response, provider = await _request_council(
        tmp_path,
        "POST /api/council/runs HTTP/1.1\r\n"
        "Host: test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["status"] == "partial"
    assert payload["packet_fingerprint"]
    assert payload["seats"][0]["seat_id"] == "contrarian"
    assert payload["results"][0]["status"] == "started"
    assert payload["results"][0]["summary"].startswith("Native review session started:")
    assert provider.calls[0] == ("status",)
    assert provider.calls[1][0] == "start_session"
    assert provider.calls[1][1] == "/repo"
    assert "Review a web-submitted proposal." in provider.calls[1][2]
    assert provider.calls[1][3]["model"] == "deepseek-v4"
    assert provider.calls[2][0] == "read_session"


@pytest.mark.asyncio
async def test_async_council_run_api_can_poll_each_seat_state(tmp_path: Path) -> None:
    fake_provider = FakeCouncilProvider()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(_store(tmp_path)),
        native_registry=NativeAgentRegistry([fake_provider]),
    )
    body = json.dumps(
        {
            "async": True,
            "title": "Web Council",
            "proposal": "Review a web-submitted proposal.",
            "cwd": "/repo",
            "config": {
                "mode": "council",
                "assignments": [
                    {
                        "seat_id": "contrarian",
                        "provider": "claude",
                        "model": "deepseek-v4",
                        "enabled": True,
                    }
                ],
                "required_seat_ids": ["contrarian"],
            },
        }
    )
    await server.start()
    try:
        start_response = await _read_response(
            server.host,
            server.port,
            "POST /api/council/runs HTTP/1.1\r\n"
            "Host: test\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
        assert "HTTP/1.1 200 OK" in start_response
        started_payload = _json_body(start_response)
        assert started_payload["run_id"]
        assert started_payload["mode"] == "async"
        assert started_payload["results"][0]["seat_id"] == "contrarian"
        assert started_payload["results"][0]["status"] in {"queued", "running", "started"}

        polled_payload: dict[str, Any] | None = None
        for _attempt in range(20):
            poll_response = await _read_response(
                server.host,
                server.port,
                f"GET /api/council/runs/{started_payload['run_id']} HTTP/1.1\r\n"
                "Host: test\r\nConnection: close\r\n\r\n",
            )
            assert "HTTP/1.1 200 OK" in poll_response
            polled_payload = _json_body(poll_response)
            if polled_payload["results"][0]["native_session_id"]:
                break
            await asyncio.sleep(0.01)
        assert polled_payload is not None
        result = polled_payload["results"][0]
        assert polled_payload["run_id"] == started_payload["run_id"]
        assert polled_payload["status"] == "partial"
        assert result["status"] == "started"
        assert result["native_session_id"].startswith("council-")
        assert result["provider_engine"] == "sdk-deepseek"
        assert result["native_session_path"] == (
            f"/native/claude?native_thread_id={result['native_session_id']}"
        )
        assert result["summary"].startswith("Native review session started:")
    finally:
        await server.stop()
