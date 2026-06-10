from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
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


class FakeProvider:
    provider = "claude"
    provider_engine = "sdk-deepseek"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def status(self):
        self.calls.append(("status",))
        return NativeAgentStatus(
            provider="claude",
            provider_engine="sdk-deepseek",
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self):
        self.calls.append(("capabilities",))
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_continue_session=True,
        )

    async def list_sessions(self, limit: int = 50):
        self.calls.append(("list_sessions", limit))
        return [
            NativeAgentSession(
                id=1,
                provider="claude",
                provider_engine="sdk-deepseek",
                native_session_id="session-1",
                agent_run_id=2,
                conversation_id=3,
                title="Claude work",
                cwd="/repo",
                source_kind="claude_sdk_deepseek",
                status="running",
                last_turn_id="",
                activity_at="2026-06-01T00:00:00Z",
                created_at="2026-06-01T00:00:00Z",
                updated_at="2026-06-01T00:00:00Z",
            )
        ]

    async def list_models(self):
        self.calls.append(("list_models",))
        return [{"id": "deepseek-v4-pro"}]

    async def start_session(self, cwd: str, prompt: str, **kwargs):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="session-2",
            agent_run_id=4,
            status="started",
        )

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ):
        self.calls.append(("continue_session", native_session_id, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=5,
            turn_id="turn-2",
            active_turn_id="turn-2",
            turn_running=True,
            status="continued",
        )


class FakeNoSteerProvider(FakeProvider):
    provider = "antigravity"
    provider_engine = "cli-local"

    def capabilities(self):
        self.calls.append(("capabilities",))
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_continue_session=True,
            can_steer_active_turn=False,
            disabled_reasons={
                "can_steer_active_turn": (
                    "Antigravity CLI continuation starts a new prompt turn."
                )
            },
        )

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ):
        self.calls.append(("steer_session", native_session_id, expected_turn_id, prompt))
        raise NotImplementedError("Antigravity CLI provider does not support steering")


class FakeMissingSessionProvider(FakeProvider):
    async def read_session(self, native_session_id: str):
        self.calls.append(("read_session", native_session_id))
        raise KeyError(native_session_id)


class FakeWorkflowService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def preview_handoff(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("preview_handoff", kwargs))
        return {
            "workflow_run_id": "wf_1",
            "preview_id": "preview_1",
            "intent": "execute_plan",
            "target_provider": kwargs["target_provider"],
            "prompt": "handoff prompt",
            "warnings": [],
        }

    async def execute_handoff(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("execute_handoff", kwargs))
        return {
            "workflow_run_id": kwargs["workflow_run_id"],
            "step_id": "step_1",
            "target_provider": kwargs["target_provider"],
            "target_thread_id": "claude-thread-1",
            "target_url": "/workers/9/live?native_provider=claude&native_thread_id=claude-thread-1",
            "status": "running",
        }


def test_fake_provider_contract_shape() -> None:
    registry = NativeAgentRegistry([FakeProvider()])

    assert registry.get("claude").provider_engine == "sdk-deepseek"


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


async def _request_native_agent(
    tmp_path: Path,
    request: str,
    *,
    provider: FakeProvider | None = None,
    access_token: str | None = None,
    allow_unauthenticated_loopback: bool = True,
) -> tuple[str, FakeProvider]:
    fake_provider = provider or FakeProvider()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(_store(tmp_path)),
        native_registry=NativeAgentRegistry([fake_provider]),
        access_token=access_token,
        allow_unauthenticated_loopback=allow_unauthenticated_loopback,
    )
    await server.start()
    try:
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()
    return response, fake_provider


async def _request_native_agent_with_workflow(
    tmp_path: Path,
    request: str,
    *,
    access_token: str | None = None,
    allow_unauthenticated_loopback: bool = True,
) -> tuple[str, FakeProvider, FakeWorkflowService]:
    fake_provider = FakeProvider()
    workflow_service = FakeWorkflowService()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(_store(tmp_path)),
        native_registry=NativeAgentRegistry([fake_provider]),
        workflow_service=workflow_service,
        access_token=access_token,
        allow_unauthenticated_loopback=allow_unauthenticated_loopback,
    )
    await server.start()
    try:
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()
    return response, fake_provider, workflow_service


@pytest.mark.asyncio
async def test_native_agent_registry_root_shows_token_entry_for_native_index(
    tmp_path: Path,
) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )

    assert "HTTP/1.1 200 OK" in response
    assert "<title>WLCodex</title>" in response
    assert 'location.replace("/native")' in response
    assert provider.calls == []


@pytest.mark.asyncio
async def test_native_agent_status_route(tmp_path: Path) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "GET /api/native/claude/status HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["status_code"] == "ok"
    assert payload["provider_engine"] == "sdk-deepseek"
    assert provider.calls == [("status",)]


@pytest.mark.asyncio
async def test_native_agent_capabilities_route(tmp_path: Path) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "GET /api/native/claude/capabilities HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["can_list_sessions"] is True
    assert payload["can_continue_session"] is True
    assert provider.calls == [("capabilities",)]


@pytest.mark.asyncio
async def test_native_agent_steer_route_reports_disabled_capability(
    tmp_path: Path,
) -> None:
    provider = FakeNoSteerProvider()
    body = json.dumps(
        {"prompt": "steer this turn", "expected_turn_id": "turn-running"}
    )

    response, provider = await _request_native_agent(
        tmp_path,
        "POST /api/native/antigravity/sessions/session-1/steer HTTP/1.1\r\n"
        "Host: test\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        provider=provider,
    )

    assert "HTTP/1.1 409" in response
    assert _json_body(response) == {
        "error": "Antigravity CLI continuation starts a new prompt turn."
    }
    assert provider.calls == [("capabilities",)]


@pytest.mark.asyncio
async def test_native_agent_sessions_route(tmp_path: Path) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "GET /api/native/claude/sessions HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["sessions"][0]["native_session_id"] == "session-1"
    assert payload["sessions"][0]["provider_engine"] == "sdk-deepseek"
    assert provider.calls == [("list_sessions", 50)]


@pytest.mark.asyncio
async def test_native_agent_read_missing_session_returns_404(tmp_path: Path) -> None:
    provider = FakeMissingSessionProvider()
    response, provider = await _request_native_agent(
        tmp_path,
        "GET /api/native/claude/sessions/thread-test HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        provider=provider,
    )

    assert "HTTP/1.1 404 Not Found" in response
    assert _json_body(response) == {"error": "native session not found"}
    assert provider.calls == [("read_session", "thread-test")]


@pytest.mark.asyncio
async def test_native_agent_start_session_route(tmp_path: Path) -> None:
    body = '{"cwd": "/repo", "prompt": "fix it"}'
    response, provider = await _request_native_agent(
        tmp_path,
        "POST /api/native/claude/sessions/start HTTP/1.1\r\n"
        "Host: test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["native_session_id"] == "session-2"
    assert payload["provider_engine"] == "sdk-deepseek"
    assert provider.calls == [
        (
            "start_session",
            "/repo",
            "fix it",
            {
                "model": None,
                "effort": None,
                "service_tier": None,
                "images": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_native_antigravity_start_session_route_forwards_permission_flags(
    tmp_path: Path,
) -> None:
    body = json.dumps(
        {
            "cwd": "/repo",
            "prompt": "fix it",
            "permission_mode": "skip_permissions_sandbox",
        }
    )
    provider = FakeNoSteerProvider()
    provider.provider = "antigravity"
    provider.provider_engine = "cli-local"
    response, provider = await _request_native_agent(
        tmp_path,
        "POST /api/native/antigravity/sessions/start HTTP/1.1\r\n"
        "Host: test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        provider=provider,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["provider"] == "antigravity"
    assert payload["provider_engine"] == "cli-local"
    assert provider.calls == [
        (
            "start_session",
            "/repo",
            "fix it",
            {
                "model": None,
                "effort": None,
                "service_tier": None,
                "images": None,
                "dangerously_skip_permissions": True,
                "sandbox": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_native_antigravity_continue_session_route_forwards_permission_flags(
    tmp_path: Path,
) -> None:
    provider = FakeNoSteerProvider()
    provider.provider = "antigravity"
    provider.provider_engine = "cli-local"
    body = json.dumps({"prompt": "continue", "permission_mode": "sandbox"})

    response, provider = await _request_native_agent(
        tmp_path,
        "POST /api/native/antigravity/sessions/session-1/continue HTTP/1.1\r\n"
        "Host: test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
        provider=provider,
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["native_session_id"] == "session-1"
    assert provider.calls == [
        ("capabilities",),
        (
            "continue_session",
            "session-1",
            "continue",
            {
                "model": None,
                "effort": None,
                "service_tier": None,
                "images": None,
                "dangerously_skip_permissions": False,
                "sandbox": True,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_workflow_handoff_preview_route(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "source_provider": "antigravity",
            "source_thread_id": "thread-1",
            "source_turn_id": "turn-2",
            "target_provider": "claude",
            "cwd": "/repo",
            "intent": "execute_plan",
            "user_note": "按计划执行",
        }
    )

    response, provider, workflow_service = await _request_native_agent_with_workflow(
        tmp_path,
        "POST /api/native/workflows/handoffs/preview HTTP/1.1\r\n"
        "Host: test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["workflow_run_id"] == "wf_1"
    assert payload["preview_id"] == "preview_1"
    assert payload["prompt"] == "handoff prompt"
    assert provider.calls == []
    assert workflow_service.calls == [
        (
            "preview_handoff",
            {
                "source_provider": "antigravity",
                "source_thread_id": "thread-1",
                "source_turn_id": "turn-2",
                "target_provider": "claude",
                "cwd": "/repo",
                "intent": "execute_plan",
                "user_note": "按计划执行",
            },
        )
    ]


@pytest.mark.asyncio
async def test_workflow_handoff_execute_route(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "workflow_run_id": "wf_1",
            "preview_id": "preview_1",
            "target_provider": "claude",
            "cwd": "/repo",
            "prompt": "handoff prompt",
        }
    )

    response, _provider, workflow_service = await _request_native_agent_with_workflow(
        tmp_path,
        "POST /api/native/workflows/handoffs/execute HTTP/1.1\r\n"
        "Host: test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n"
        f"{body}",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["step_id"] == "step_1"
    assert payload["target_url"] == (
        "/workers/9/live?native_provider=claude&native_thread_id=claude-thread-1"
    )
    assert workflow_service.calls == [
        (
            "execute_handoff",
            {
                "workflow_run_id": "wf_1",
                "preview_id": "preview_1",
                "target_provider": "claude",
                "cwd": "/repo",
                "prompt": "handoff prompt",
            },
        )
    ]


@pytest.mark.asyncio
async def test_native_agent_login_ticket_path_uses_provider_name(
    tmp_path: Path,
) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "POST /api/native/claude/login-ticket HTTP/1.1\r\n"
        "Host: test\r\nAuthorization: Bearer secret\r\n"
        "Connection: close\r\n\r\n",
        access_token="secret",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["path"].startswith("/native/claude/login?ticket=")
    assert payload["expires_in"] == 300
    assert provider.calls == []


@pytest.mark.asyncio
async def test_native_agent_login_ticket_path_quotes_provider_name(
    tmp_path: Path,
) -> None:
    custom_provider = FakeProvider()
    custom_provider.provider = "claude/dev"
    response, provider = await _request_native_agent(
        tmp_path,
        "POST /api/native/claude%2Fdev/login-ticket HTTP/1.1\r\n"
        "Host: test\r\nAuthorization: Bearer secret\r\n"
        "Connection: close\r\n\r\n",
        provider=custom_provider,
        access_token="secret",
    )

    assert "HTTP/1.1 200 OK" in response
    payload = _json_body(response)
    assert payload["path"].startswith("/native/claude%2Fdev/login?ticket=")
    assert provider.calls == []


@pytest.mark.asyncio
async def test_unknown_native_agent_login_page_returns_404(tmp_path: Path) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "GET /native/missing/login?ticket=ticket-1 HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        access_token="secret",
    )

    assert "HTTP/1.1 404 Not Found" in response
    assert _json_body(response)["error"] == "unknown native provider"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_native_claude_page_uses_claude_api_base(tmp_path: Path) -> None:
    response, _provider = await _request_native_agent(
        tmp_path,
        "GET /native/claude HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROVIDER = "claude";' in response
    assert 'const PROVIDER_LABEL = "Claude";' in response
    assert 'const API_BASE = "/api/native/claude";' in response
    assert "<title>Claude</title>" in response
    assert "<h1>Claude</h1>" in response
    assert "/api/native/codex/sessions" not in response
    assert '\"value\": "acceptEdits"' in response
    assert '\"value\": "auto"' in response
    assert '\"value\": "plan"' in response
    assert '\"value\": "default"' in response
    assert '\"value\": "dontAsk"' in response
    assert '\"value\": "bypassPermissions"' in response


@pytest.mark.asyncio
async def test_native_antigravity_page_uses_antigravity_api_base(tmp_path: Path) -> None:
    provider = FakeNoSteerProvider()
    response, _provider = await _request_native_agent(
        tmp_path,
        "GET /native/antigravity HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        provider=provider,
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROVIDER = "antigravity";' in response
    assert 'const PROVIDER_LABEL = "Antigravity";' in response
    assert 'const API_BASE = "/api/native/antigravity";' in response
    assert '"value": "default"' in response
    assert '"value": "sandbox"' in response
    assert '"value": "skip_permissions"' in response
    assert '"value": "skip_permissions_sandbox"' in response


@pytest.mark.asyncio
async def test_native_root_lists_providers(tmp_path: Path) -> None:
    response, _provider = await _request_native_agent(
        tmp_path,
        "GET /native HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "/native/claude" in response
    assert "Claude" in response
    assert "sdk-deepseek" in response


@pytest.mark.asyncio
async def test_native_root_provider_links_preserve_query_token(
    tmp_path: Path,
) -> None:
    response, _provider = await _request_native_agent(
        tmp_path,
        "GET /native?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        access_token="secret",
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'href="/native/claude?token=secret"' in response
    assert 'href="/council?token=secret"' in response


@pytest.mark.asyncio
async def test_native_claude_token_entry_returns_to_claude(
    tmp_path: Path,
) -> None:
    response, _provider = await _request_native_agent(
        tmp_path,
        "GET /native/claude HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )

    assert "HTTP/1.1 401 Unauthorized" in response
    assert 'location.replace("/native/claude")' in response
    assert 'location.href = "/native/claude";' in response


@pytest.mark.asyncio
async def test_unknown_native_agent_page_returns_404(tmp_path: Path) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "GET /native/missing HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 404 Not Found" in response
    assert _json_body(response)["error"] == "unknown native provider"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_worker_live_page_uses_native_provider_query(tmp_path: Path) -> None:
    response, _provider = await _request_native_agent(
        tmp_path,
        "GET /workers/4/live?native_provider=claude&native_thread_id=session-2 HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROVIDER = "claude";' in response
    assert 'const PROVIDER_LABEL = "Claude";' in response
    assert 'const API_BASE = "/api/native/claude";' in response
    assert "<h1>Claude</h1>" in response
    assert "连接 Claude 会话" in response
    assert "输入消息开始 Claude 会话" in response
    assert "等待 Claude 转录" not in response
    assert 'placeholder="继续 Claude 会话"' in response
    assert "/api/native/codex/sessions/" not in response
    assert "连接官方 Codex 会话" not in response
    assert "等待官方 Codex 转录" not in response
    assert "继续官方 Codex 会话" not in response


@pytest.mark.asyncio
async def test_worker_live_page_falls_back_to_codex_for_unknown_provider(
    tmp_path: Path,
) -> None:
    response, _provider = await _request_native_agent(
        tmp_path,
        "GET /workers/4/live?native_provider=missing&native_thread_id=session-2 HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROVIDER = "codex";' in response
    assert 'const API_BASE = "/api/native/codex";' in response


@pytest.mark.asyncio
async def test_unknown_native_agent_provider_returns_404(tmp_path: Path) -> None:
    response, provider = await _request_native_agent(
        tmp_path,
        "GET /api/native/unknown/status HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 404 Not Found" in response
    assert _json_body(response)["error"] == "unknown native provider"
    assert provider.calls == []
