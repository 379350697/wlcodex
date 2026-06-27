from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer, _relay_activity_label
from wlcodex.native_agents.models import NativeAgentCapabilities
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
)


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


def test_relay_activity_label_formats_iso_timestamp_for_mobile_cards() -> None:
    label = _relay_activity_label("2026-06-16T12:20:06.297146+00:00")

    assert label == "最近活动 06-16 20:20"
    assert "T12:20:06.297146+00:00" not in label

    microsecond_label = _relay_activity_label("2026-06-16T07:51:57.510982+00:00")
    assert microsecond_label == "最近活动 06-16 15:51"

    datetime_label = _relay_activity_label(
        datetime(2026, 6, 16, 7, 58, 48, tzinfo=timezone.utc)
    )
    assert datetime_label == "最近活动 06-16 15:58"

    fallback_label = _relay_activity_label("2026-06-16T07:51:57.510982123+00:00")
    assert fallback_label == "最近活动 06-16 15:51"
    assert "2026-06-16T" not in fallback_label


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
    providers = [FakeCodexProvider(), FakeProvider(), FakeAntigravityProvider()]
    service = relay_service or RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry(providers),
        default_provider="claude",
        role_skills={
            "architect": ("gitnexus-impact-analysis",),
            "implementer": ("test-driven-development",),
        },
        role_capabilities={
            "architect": ("read", "gitnexus"),
            "implementer": ("write", "tests"),
        },
    )
    runtime_store = RuntimeEventStore(ledger._conn)
    return WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_registry=NativeAgentRegistry(providers),
        relay_service=service,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    ), service, runtime_store


async def _request(tmp_path: Path, request: str, relay_service: RelayService | None = None):
    server, service, _runtime_store = _server(tmp_path, relay_service)
    await server.start()
    try:
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()
    return response, service


def _append_runtime_event(
    runtime_store: RuntimeEventStore,
    *,
    agent_run_id: int,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: str,
):
    return runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=event_type,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(agent_run_id),
            correlation_id=f"agent:{agent_run_id}",
            source=EventSource.CLAUDE,
            actor="claude_native",
            visibility=Visibility.USER,
            payload=payload,
            occurred_at=occurred_at,
            agent_run_id=agent_run_id,
        )
    )


def _native_message_keys(response: str) -> list[str]:
    return re.findall(r'data-native-key="([^"]+)"', response)


def _native_message_kinds(response: str) -> list[str]:
    return re.findall(
        r'<article class="relay-message"[^>]*data-native-kind="([^"]+)"',
        response,
    )


def _relay_view_panel_html(response: str, view: str) -> str:
    if view == "conversation":
        start_marker = '<section class="relay-view relay-conversation-panel"'
        end_markers = (
            '<section class="relay-view relay-board-panel"',
            '<section class="marvis-work-log"',
            "</main>",
        )
    elif view == "board":
        start_marker = '<section class="relay-view relay-board-panel"'
        end_markers = ("</main>",)
        if start_marker not in response:
            return _relay_work_log_html(response)
    else:
        raise AssertionError(f"unknown relay view: {view}")
    assert start_marker in response
    panel = response.split(start_marker, 1)[1]
    for end_marker in end_markers:
        if end_marker in panel:
            return panel.split(end_marker, 1)[0]
    raise AssertionError(f"missing relay panel end marker for {view}")


def _relay_work_log_html(response: str) -> str:
    start_marker = '<section class="marvis-work-log"'
    assert start_marker in response
    return response.split(start_marker, 1)[1].split("<script", 1)[0]


def _relay_message_bodies_html(panel_html: str) -> str:
    return "\n".join(
        re.findall(
            r'<div class="(?:relay-message-body|marvis-relay-agent-bubble|marvis-relay-user-bubble)" data-native-message-body>(.*?)</div>',
            panel_html,
            flags=re.DOTALL,
        )
    )


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
    assert "Marvis 接力" in response
    assert 'href="/native/workflows/relay?token=secret"' in response
    assert 'data-native-entry="marvis-relay"' in response
    assert '<span>工作流</span>' not in response


@pytest.mark.asyncio
async def test_workflow_directory_links_to_relay_council_and_dev_flow(tmp_path: Path) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native/workflows?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "Marvis 接力" in response
    assert 'href="/native/workflows/relay?token=secret"' in response
    assert "议会审核" in response
    assert 'href="/council?token=secret"' in response
    assert "Dev Flow" in response


@pytest.mark.asyncio
async def test_relay_task_list_is_workspace_not_session_list(tmp_path: Path) -> None:
    server, service, _runtime_store = _server(tmp_path)
    default_workspace = "/Users/wl/projects/wlcodex"
    task = service.create_task(
        title="Default workspace relay",
        prompt="Prompt",
        workspace=default_workspace,
        provider="claude",
    )
    other = service.create_task(
        title="Other workspace relay",
        prompt="Prompt",
        workspace="/other",
        provider="claude",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/workflows/relay?token=secret HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Marvis" in response
    assert 'data-marvis-relay-view="tasks"' in response
    assert '<meta name="color-scheme" content="light only">' in response
    assert '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260627-work-log">' in response
    assert 'class="marvis-relay-bottom-nav"' in response
    assert 'class="marvis-relay-avatar marvis-relay-avatar-marvis"' in response
    assert 'href="/native/workflows/relay/office?token=secret"' in response
    assert (
        'href="/native/workflows/relay/chat?token=secret&amp;workspace=/Users/wl/projects/wlcodex"'
        in response
    )
    assert 'href="/native/workflows/relay?token=secret&amp;workspace=/Users/wl/projects/wlcodex"' in response
    assert 'data-marvis-nav="tasks" aria-current="page"' in response
    assert 'class="marvis-relay-composer"' not in response
    assert "请输入任务" not in response
    assert "任务历史" in response
    assert "新接力任务" not in response
    assert "配置" not in response
    assert '<a class="relay-secondary" href="/native/workflows/relay/config' not in response
    assert 'href="/native/workflows/relay/config' not in response
    assert "新聊天" not in response
    assert "relay-create-panel" not in response
    assert "relay-create-modal" not in response
    assert 'aria-label="new relay task"' not in response
    assert 'data-close-new-task aria-label="关闭新接力任务"' not in response
    assert "wlcodex" in response
    assert "将快照以下五角色 provider 配置" not in response
    assert "发布大任务" not in response
    assert "暂无任务" not in response
    assert "relay-group" not in response
    assert 'data-filter="running"' in response
    assert 'data-filter="waiting_user"' in response
    assert 'data-filter="blocked"' in response
    assert 'data-filter="completed"' in response
    assert 'data-filter="interrupted"' in response
    assert "全部工作区" not in response
    assert 'data-workspace-value=""' not in response
    assert "wlcodex" in response
    assert "工作区（可选）" not in response
    assert '<option value="">不指定工作区</option>' not in response
    assert 'aria-label="relay role provider configuration"' not in response
    assert "Provider 应用于全部角色" not in response
    assert "默认角色配置" not in response
    assert 'aria-label="relay role provider summary"' not in response
    assert "角色 provider 快照" not in response
    assert "总工程师 ·" not in response
    assert "开发工程师 ·" not in response
    assert "gitnexus-impact-analysis" not in response
    assert "test-driven-development" not in response
    assert 'aria-label="relay task history"' in response
    assert "native session list" not in response.lower()
    assert "Default workspace relay" in response
    assert "Other workspace relay" not in response
    assert "relay-task-card" in response
    assert "marvis-relay-task-card" in response
    assert 'class="relay-status-badge"' in response
    assert 'class="relay-open relay-card-open"' in response
    assert "总工程师：" not in response
    assert "等待总工程师接收" not in response
    assert "打开任务" in response
    assert "open task" not in response
    assert f'/native/workflows/relay/tasks/{task.id}?token=secret' in response
    assert f'/native/workflows/relay/tasks/{other.id}?token=secret' not in response

    populated, _ = await _request(
        tmp_path,
        "GET /native/workflows/relay?token=secret&workspace=%2Frepo HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )
    assert 'href="/native/workflows/relay/chat?token=secret&amp;workspace=/repo"' in populated
    assert "Default workspace relay" not in populated
    assert "Other workspace relay" not in populated

    repo_task = service.create_task(
        title="Long relay title wraps without overlap",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    populated, _ = await _request(
        tmp_path,
        "GET /native/workflows/relay?token=secret&workspace=%2Frepo HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )
    assert "Long relay title wraps without overlap" in populated
    assert "Other workspace relay" not in populated
    assert "relay-task-card" in populated
    assert "marvis-relay-task-card" in populated
    assert 'class="relay-status-badge"' in populated
    assert 'class="relay-role-chips"' not in populated
    assert "总工程师 · 阻塞 · Codex" not in populated
    assert "架构工程师 · 未调度 · Antigravity" not in populated
    assert "开发工程师 · 未调度 · Claude" not in populated
    assert 'class="relay-open relay-card-open"' in populated
    assert "总工程师：" not in populated
    assert "等待总工程师接收" not in populated
    assert "打开任务" in populated
    assert "open task" not in populated
    assert f'/native/workflows/relay/tasks/{repo_task.id}?token=secret' in populated
    assert f'/native/workflows/relay/tasks/{other.id}?token=secret' not in populated

    service._store.save_artifact(
        repo_task.id,
        "director",
        "handoff_packet",
        {
            "relay_role": "director",
            "summary": "按完整五角色接力处理：先由架构工程师审查。",
        },
        summary="按完整五角色接力处理：先由架构工程师审查。",
    )
    service._store._ledger._conn.execute(
        "UPDATE team_runs SET updated_at = ? WHERE id = ?",
        ("2026-06-16T07:51:57.510982123+00:00", repo_task.id),
    )
    service._store._ledger._conn.commit()
    polished, _ = await _request(
        tmp_path,
        "GET /native/workflows/relay?token=secret&workspace=%2Frepo HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )
    assert 'class="marvis-relay-task-card-footer"' in polished
    assert polished.index('class="relay-status-badge"') < polished.index(
        'class="relay-open relay-card-open"'
    )
    assert "最近活动 06-16 15:51" in polished
    assert "2026-06-16T07:51:57.510982123+00:00" not in polished
    assert "最近接棒：" not in polished
    assert "按完整五角色接力处理" not in polished


@pytest.mark.asyncio
async def test_marvis_relay_chat_home_is_the_only_new_task_entry(
    tmp_path: Path,
) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native/workflows/relay/chat?token=secret&workspace=%2Frepo HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'data-marvis-relay-view="chat"' in response
    assert '<meta name="color-scheme" content="light only">' in response
    assert 'class="marvis-relay-topbar"' in response
    assert 'class="marvis-relay-avatar marvis-relay-avatar-marvis marvis-relay-hero-avatar"' in response
    assert "你好，今天想做什么？" in response
    assert "GitHub热门项目收集" not in response
    assert "曼谷旅行路线书网页" not in response
    assert "同花顺股票信息查询" not in response
    assert "个人信息文件归档" not in response
    assert 'class="marvis-relay-suggestion"' not in response
    assert 'class="marvis-relay-composer"' in response
    assert 'action="/api/relay/tasks?token=secret"' in response
    assert '<input name="title" autocomplete="off" placeholder="请在此输入任务">' in response
    assert '<input type="hidden" name="prompt" value="">' in response
    assert '<input type="hidden" name="workspace" value="/repo">' in response
    assert 'data-marvis-nav="chat" aria-current="page"' in response
    assert 'href="/native/workflows/relay?token=secret&amp;workspace=/repo"' in response
    assert "新接力任务" not in response
    assert "relay-create-modal" not in response


@pytest.mark.asyncio
async def test_marvis_relay_office_page_uses_screenshot_assets_and_persona_modal(
    tmp_path: Path,
) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native/workflows/relay/office?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'data-marvis-relay-view="office"' in response
    assert '<meta name="color-scheme" content="light only">' in response
    assert '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260627-work-log">' in response
    assert "Marvis办公室" in response
    assert 'href="/native/workflows/relay?token=secret"' in response
    assert "/static/marvis/office-scene-roles-5.png?v=20260627-red-director" in response
    assert response.count("data-marvis-office-role=") == 5
    assert "/static/marvis/office-worker-cutout-" not in response
    assert "/static/marvis/office-desk-empty-slot.png" not in response
    assert "/static/marvis/office-desk-empty-hd.png" not in response
    assert response.count("data-marvis-persona-open=") == 5
    assert '"architect":{"role":"architect","display_name":"架构工程师"' in response
    assert '"implementer":{"role":"implementer","display_name":"开发工程师"' in response
    assert '"director":{"role":"director","display_name":"总工程师","title":"总工程师","provider"' in response
    assert '"architect":{"role":"architect","display_name":"架构工程师","title":"架构工程师","provider"' in response
    assert '"implementer":{"role":"implementer","display_name":"开发工程师","title":"开发工程师","provider"' in response
    assert '"tester":{"role":"tester","display_name":"测试工程师","title":"测试工程师","provider"' in response
    assert '"auditor":{"role":"auditor","display_name":"审计工程师","title":"审计工程师","provider"' in response
    assert re.search(r'"director":\{[^}]*"display_name":"总工程师"[^}]*"avatar":"marvis"', response)
    assert re.search(r'"architect":\{[^}]*"display_name":"架构工程师"[^}]*"avatar":"computer-agent"', response)
    assert re.search(r'"implementer":\{[^}]*"display_name":"开发工程师"[^}]*"avatar":"search-agent"', response)
    assert re.search(r'"tester":\{[^}]*"display_name":"测试工程师"[^}]*"avatar":"app-agent"', response)
    assert re.search(r'"auditor":\{[^}]*"display_name":"审计工程师"[^}]*"avatar":"browser-agent"', response)
    assert '"avatar":"file-agent"' not in response
    assert "data-persona-name" in response
    assert "Team Leader" not in response
    assert "Computer Agent" not in response
    assert "File Agent" not in response
    assert "Browser Agent" not in response
    assert "Search Agent" not in response
    assert "设置大脑" in response
    assert "设置大模型" not in response
    assert "marvis-persona-model-panel" in response
    assert 'data-provider-option="codex"' in response
    assert 'data-provider-option="claude"' in response
    assert 'data-provider-option="antigravity"' in response
    assert "/api/relay/config${TOKEN_SUFFIX}" in response
    assert "今日消耗Token" in response
    assert "总消耗Token" in response
    assert "今日节省Token" not in response
    assert "marvis-token-beans" in response
    avatar_assets = [
        "persona-avatar-marvis.png",
        "persona-avatar-app-agent.png",
        "persona-avatar-computer-agent.png",
        "persona-avatar-search-agent.png",
        "persona-avatar-file-agent.png",
        "persona-avatar-browser-agent.png",
    ]
    css = Path("wlcodex/live_stream/static/relay_marvis.css").read_text()
    for asset in avatar_assets:
        assert Path("wlcodex/live_stream/static/marvis", asset).exists()
        assert f"/static/marvis/{asset}" in css


@pytest.mark.asyncio
async def test_marvis_relay_office_page_displays_today_token_usage(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Token stats relay",
        prompt="count tokens",
        workspace="/repo",
        provider="codex",
    )
    ledger = service._store._ledger
    ledger.record_usage_event(
        task_id=task.id,
        agent="codex",
        role="director",
        phase="dispatch",
        request_kind="turn",
        model="gpt-5",
        source="exact",
        input_tokens=1200,
        output_tokens=300,
        status="completed",
    )
    ledger.record_usage_event(
        task_id=task.id,
        agent="claude",
        role="architect",
        phase="implementation",
        request_kind="turn",
        model="claude",
        source="exact",
        total_tokens=700,
        status="completed",
    )
    older = ledger.record_usage_event(
        task_id=task.id,
        agent="codex",
        role="tester",
        phase="verification",
        request_kind="turn",
        model="gpt-5",
        source="exact",
        total_tokens=800,
        status="completed",
    )
    ledger._conn.execute(
        "UPDATE usage_events SET created_at = datetime('now', '-1 day') WHERE id = ?",
        (older.id,),
    )
    ledger._conn.commit()
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/workflows/relay/office?token=secret HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
        stats_response = await _read_response(
            server.host,
            server.port,
            "GET /api/relay/token-stats?token=secret HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert 'data-token-consumed="2200"' in response
    assert 'data-token-total="3000"' in response
    assert "2,200" in response
    assert "3,000" in response
    assert "data-token-local" not in response
    assert "data-token-saved" not in response
    assert "今日节省Token" not in response
    assert '"consumed_tokens": 2200' in stats_response
    assert '"total_consumed_tokens": 3000' in stats_response
    assert "local_tokens" not in stats_response
    assert "saved_tokens" not in stats_response


@pytest.mark.asyncio
async def test_marvis_relay_office_occupancy_follows_configured_roles(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    service.config = lambda: {  # type: ignore[method-assign]
        "configured_roles": [
            {"role": "architect", "display_name": "架构工程师"},
            {"role": "implementer", "display_name": "开发工程师"},
        ],
        "roles": [
            {"role": "director", "display_name": "总工程师"},
            {"role": "architect", "display_name": "架构工程师"},
            {"role": "implementer", "display_name": "开发工程师"},
            {"role": "tester", "display_name": "测试工程师"},
            {"role": "auditor", "display_name": "审计工程师"},
        ],
        "providers": [],
        "assignments": {"architect": "codex", "implementer": "claude"},
    }
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/workflows/relay/office?token=secret HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert response.count("data-marvis-office-role=") == 2
    assert 'data-marvis-office-role="architect"' in response
    assert 'data-marvis-office-role="implementer"' in response
    assert response.count("data-marvis-persona-open=") == 2
    assert "/static/marvis/office-scene-roles-2.png?v=20260627-red-director" in response
    assert "/static/marvis/office-worker-cutout-" not in response
    assert "/static/marvis/office-desk-empty-slot.png" not in response
    assert "/static/marvis/office-worker-cutout-3.png" not in response
    assert "/static/marvis/office-desk-empty-hd.png" not in response


@pytest.mark.asyncio
async def test_relay_config_has_dedicated_page(tmp_path: Path) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native/workflows/relay/config?token=secret&workspace=%2Frepo HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "<title>流式接力配置</title>" in response
    assert "<h1>流式接力</h1>" in response
    assert "<h1>流式接力配置</h1>" not in response
    assert "返回任务" not in response
    assert "角色配置" in response
    assert 'href="/native/workflows/relay?token=secret&amp;workspace=/repo"' in response
    assert "总工程师" in response
    assert "架构工程师" in response
    assert "开发工程师" in response
    assert "测试工程师" in response
    assert "审计工程师" in response
    assert "Codex" in response
    assert "Claude" in response
    assert "Antigravity" in response
    assert 'class="relay-provider-select"' in response
    assert "color-scheme: dark" in response
    assert ".relay-provider-select option" in response
    assert "当前：Claude" in response
    assert "gitnexus-impact-analysis" in response
    assert "test-driven-development" in response
    assert "保存配置" in response
    assert 'const RELAY_HISTORY_HREF = "/native/workflows/relay?token=secret&workspace=/repo";' in response
    assert "window.location.href = RELAY_HISTORY_HREF;" in response
    assert "任务历史" not in response
    assert "新接力任务" not in response


@pytest.mark.asyncio
async def test_relay_task_detail_renders_conversation_default_and_board_switch(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Detail task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="claude",
        model="sonnet",
        native_session_id="native-director-1",
        agent_run_id=101,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    service._store.update_role_metadata(
        task.id,
        "architect",
        provider="codex",
        model="gpt-5",
        native_session_id="native-architect-1",
        agent_run_id=102,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "architect", "streaming")
    _append_runtime_event(
        runtime_store,
        agent_run_id=101,
        event_type=EventType.USER_MESSAGE_RECEIVED,
        payload={
            "text": "让审计工程师确认一下",
            "native_thread_id": "native-director-1",
            "native_turn_id": "turn-director-1",
            "itemId": "director-user-1",
        },
        occurred_at="2026-06-14T12:00:01+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=101,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": "我会先确认风险。",
            "native_thread_id": "native-director-1",
            "native_turn_id": "turn-director-1",
            "itemId": "director-assistant-1",
        },
        occurred_at="2026-06-14T12:00:02+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=101,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "我会先确认风险。",
            "native_thread_id": "native-director-1",
            "native_turn_id": "turn-director-1",
            "itemId": "director-assistant-1",
        },
        occurred_at="2026-06-14T12:00:03+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=102,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "架构侧继续补齐影响面。",
            "native_thread_id": "native-architect-1",
            "native_turn_id": "turn-architect-1",
            "itemId": "architect-assistant-1",
        },
        occurred_at="2026-06-14T12:00:04+00:00",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "low risk explanation",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "",
            "summary": "本任务判定无需派发，由总工程师直接完成。",
            "complexity": "simple",
            "risk": "low",
            "route": "director_only",
            "required_roles": ["director"],
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "总工程师直接完成。",
            "acceptance_criteria": ["给出清晰说明"],
            "stop_conditions": [],
            "requires_user_approval": False,
        },
        summary="本任务判定无需派发，由总工程师直接完成。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "summary": "总工程师已接收，正在拆解任务。",
            "output": "总工程师已接收，正在拆解任务。",
            "open_questions": ["请确认验收标准"],
        },
        summary="总工程师已接收",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "error": "invalid json: Expecting value",
        },
        summary="invalid json: Expecting value",
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
    assert 'data-marvis-relay-view="conversation"' in response
    assert '<meta name="color-scheme" content="light only">' in response
    assert '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260627-work-log">' in response
    assert 'class="marvis-relay-topbar"' in response
    assert 'class="marvis-relay-bottom-nav"' in response
    assert 'href="/native/workflows/relay/office?token=secret"' in response
    assert 'data-marvis-open-log aria-label="工作日志"' in response
    assert 'class="marvis-work-log"' in response
    assert "工作日志" in response
    assert "产出物" in response
    assert 'class="marvis-relay-composer"' in response
    assert "请输入任务" in response
    assert 'data-relay-view="conversation"' in response
    assert 'class="marvis-relay-task-title"' not in response
    assert 'class="relay-view-switch"' not in response
    assert 'data-view-tab="conversation"' not in response
    assert 'data-view-tab="board"' not in response
    assert "会话流" not in response
    assert 'aria-label="任务状态"' not in response
    assert "你好，今天想做什么？" not in response
    assert "marvis-relay-hero-avatar" not in response
    assert "relay-conversation" in response
    assert 'data-native-conversation-timeline' in response
    assert 'class="relay-native-stack"' not in response
    assert 'data-native-session-stack' not in response
    assert 'class="relay-native-stream"' not in response
    assert 'class="relay-native-frame"' not in response
    assert "<iframe" not in response
    conversation_html = _relay_view_panel_html(response, "conversation")
    work_log_html = _relay_work_log_html(response)
    assert "让审计工程师确认一下" in conversation_html
    assert "我会先确认风险。" not in conversation_html
    assert "架构侧继续补齐影响面。" in conversation_html
    assert "结论：本任务判定无需派发，由总工程师直接完成。" in conversation_html
    assert 'data-native-role="director"' in response
    assert 'data-native-role="architect"' in response
    assert 'data-view-panel="conversation"' in response
    assert 'data-view-panel="board"' not in response
    assert "任务进度" not in response.split("<script", 1)[0]
    assert "调度决策" not in response.split("<script", 1)[0]
    assert "总工程师直接完成" in response
    assert "验收依据" in response
    assert "给出清晰说明" in response
    assert "下一步" in response
    assert "RelayBoard" not in response
    assert "current goal" not in response
    assert "latest user input" not in response
    assert "native_session_id:" not in response
    assert "provider/model:" not in response
    assert "open native session" not in response
    assert ">interrupt<" not in response
    assert ">send<" not in response
    for display_name in ["Marvis", "Computer Agent"]:
        assert display_name in response
    assert 'data-marvis-work-log-role="director"' in work_log_html
    assert 'data-marvis-work-log-role="architect"' in work_log_html
    assert "继续补充给总工程师" in response
    assert "发送补充" in response
    assert "中断任务" not in response.split("<script", 1)[0]
    assert "打开原生会话" not in response.split("<script", 1)[0]
    assert "native_thread_id=native-director-1" not in response
    assert "/sessions/native-director-1" not in response
    assert "执行问题：invalid json: Expecting value" in work_log_html
    assert "等待总工程师接收并形成决策摘要" not in response
    assert f"/api/relay/tasks/{task.id}/message" in response
    assert "relay-board-grid" not in response.split("<script", 1)[0]
    assert "relay-progress" not in response.split("<script", 1)[0]
    assert "relay-activity-log" not in response.split("<script", 1)[0]
    assert 'const EVENTS_SUFFIX = "?token=secret&after=3";' in response
    assert "function normalizeRelayPayload(raw)" in response
    assert "const payload = parseRelayEvent(event);" in response
    assert "function renderRelayNativeEvent" in response
    assert "const TERMINAL_ROLE_STATUSES = new Set" in response
    assert "TERMINAL_ROLE_STATUSES.has(currentStatus)" in response
    assert 'source.addEventListener("role.native_event"' in response
    assert 'document.querySelectorAll("[data-native-key]")' in response
    assert "nativeTranscriptNodes.set(node.dataset.nativeKey" in response
    assert "events${EVENTS_SUFFIX}" in response
    assert "appendConversationDelta(" not in response
    assert "activeConversationRole" not in response
    assert "appendConversationUser(" not in response
    for event_name in [
        "role.queued",
        "role.streaming",
        "dispatch.verified",
        "dispatch.fallback",
        "role.native_event",
        "role.output_delta",
        "role.envelope",
        "handoff.created",
        "role.status",
        "task.completed",
        "task.interrupted",
    ]:
        assert event_name in response


@pytest.mark.asyncio
async def test_relay_task_detail_hides_native_activity_from_conversation(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Readable detail task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="claude",
        model="sonnet",
        native_session_id="native-director-readable",
        agent_run_id=201,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    _append_runtime_event(
        runtime_store,
        agent_run_id=201,
        event_type=EventType.USER_MESSAGE_RECEIVED,
        payload={
            "text": "请确认要删除哪个文件",
            "native_turn_id": "turn-readable-1",
            "itemId": "readable-user-1",
        },
        occurred_at="2026-06-14T12:10:01+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=201,
        event_type=EventType.AGENT_RUN_ACTIVITY,
        payload={
            "status": "turn_started",
            "native_turn_id": "turn-readable-1",
        },
        occurred_at="2026-06-14T12:10:02+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=201,
        event_type=EventType.AGENT_RUN_ACTIVITY,
        payload={
            "status": "turn_completed",
            "native_turn_id": "turn-readable-1",
        },
        occurred_at="2026-06-14T12:10:03+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=201,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "需要你确认精确文件路径。",
            "native_turn_id": "turn-readable-1",
            "itemId": "readable-assistant-1",
        },
        occurred_at="2026-06-14T12:10:04+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=201,
        event_type=EventType.AGENT_RUN_COMPLETED,
        payload={
            "status": "completed",
            "native_turn_id": "turn-readable-1",
        },
        occurred_at="2026-06-14T12:10:05+00:00",
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

    conversation_html = _relay_view_panel_html(response, "conversation")
    assert "请确认要删除哪个文件" in conversation_html
    assert "需要你确认精确文件路径。" in conversation_html
    kinds = _native_message_kinds(response)
    assert "activity" not in kinds
    assert "completed" not in kinds
    assert "turn_started" not in conversation_html
    assert "turn_completed" not in conversation_html
    keys = _native_message_keys(response)
    assert keys == list(dict.fromkeys(keys))


@pytest.mark.asyncio
async def test_relay_work_log_hides_internal_native_activity_for_director_only(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="查今日金价",
        prompt="查今日金价",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-gold",
        agent_run_id=278,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "role": "director",
            "artifact_type": "routing_decision",
            "status": "passed",
            "route": "director_only",
            "summary": "选择director_only处理今日金价查询，直接查询实时来源并返回结果。",
            "handoff_to": "",
            "required_roles": ["director"],
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "总工程师直接完成。",
        },
        summary="选择director_only处理今日金价查询，直接查询实时来源并返回结果。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "role": "director",
            "artifact_type": "final_summary",
            "status": "passed",
            "summary": "截至查询时，今日国际现货黄金约为 4,088.60 美元/盎司。",
            "handoff_to": "",
            "evidence_refs": ["https://www.kitco.com/charts/gold"],
            "open_questions": [],
            "next_action": "",
        },
        summary="截至查询时，今日国际现货黄金约为 4,088.60 美元/盎司。",
    )
    for index, payload in enumerate(
        [
            {"action": "thread_started", "threadId": "thread-gold"},
            {"action": "thread_status_changed", "threadId": "thread-gold"},
            {
                "action": "turn_started",
                "threadId": "thread-gold",
                "turnId": "turn-gold",
            },
        ],
        start=1,
    ):
        _append_runtime_event(
            runtime_store,
            agent_run_id=278,
            event_type=EventType.AGENT_RUN_ACTIVITY,
            payload=payload,
            occurred_at=f"2026-06-27T08:25:0{index}+00:00",
        )
    _append_runtime_event(
        runtime_store,
        agent_run_id=278,
        event_type=EventType.MODEL_USAGE_UPDATED,
        payload={"total_tokens": 47215},
        occurred_at="2026-06-27T08:25:04+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=278,
        event_type=EventType.AGENT_RUN_COMPLETED,
        payload={"status": "completed", "native_turn_id": "turn-gold"},
        occurred_at="2026-06-27T08:25:05+00:00",
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

    work_log_html = _relay_work_log_html(response)
    assert "47K" in work_log_html
    assert "activity" not in work_log_html
    assert "thread_started" not in work_log_html
    assert "thread_status_changed" not in work_log_html
    assert "turn_started" not in work_log_html
    assert "completed" not in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_projects_marvis_chat_and_work_log_drawer(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Marvis interaction task",
        prompt="手机办公室看不到其他小马",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-marvis",
        agent_run_id=701,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="codex",
        model="gpt-5",
        native_session_id="native-implementer-marvis",
        agent_run_id=702,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "needs app window inspection",
            "role": "director",
            "artifact_type": "routing_decision",
            "summary": "问题收到了，先派 App Agent 查看窗口。",
            "route": "core_relay",
            "required_roles": ["director", "implementer"],
            "handoff_to": "implementer",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给 App Agent 查看窗口状态。",
        },
        summary="问题收到了，先派 App Agent 查看窗口。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "status": "passed",
            "reason": "window list checked",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "搞定，有请下一位。",
            "handoff_to": "tester",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交还 Marvis 汇总。",
        },
        summary="搞定，有请下一位。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "window scope checked",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "App Agent 那边反馈 Marvis 本身不在可操作应用范围内，后面要从办公室入口或应用权限方向继续排查。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "等待用户补充办公室页面信息。",
        },
        summary="App Agent 那边反馈 Marvis 本身不在可操作应用范围内，后面要从办公室入口或应用权限方向继续排查。",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=701,
        event_type=EventType.USER_MESSAGE_RECEIVED,
        payload={
            "text": "手机办公室看不到其他小马",
            "native_turn_id": "turn-marvis-1",
            "itemId": "marvis-user-1",
        },
        occurred_at="2026-06-14T12:10:01+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=702,
        event_type=EventType.TOOL_CALL_STARTED,
        payload={
            "tool_name": "list windows",
            "native_turn_id": "turn-marvis-2",
            "itemId": "marvis-tool-1",
        },
        occurred_at="2026-06-14T12:10:02+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=702,
        event_type=EventType.TOOL_CALL_COMPLETED,
        payload={
            "tool_name": "list windows",
            "native_turn_id": "turn-marvis-2",
            "itemId": "marvis-tool-1",
        },
        occurred_at="2026-06-14T12:10:03+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=701,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "已完成汇总。",
            "usage": {"total_tokens": 12416},
        },
        occurred_at="2026-06-14T12:10:04+00:00",
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

    visible_html = response.split("<script", 1)[0]
    conversation_html = _relay_view_panel_html(response, "conversation")
    work_log_html = _relay_work_log_html(response)

    assert 'data-marvis-open-log aria-label="工作日志"' in response
    assert "wanglin的Mac mini" in response
    assert 'data-marvis-nav="tasks"' in response
    assert 'data-marvis-nav="tasks"' not in response.split(
        'document.querySelectorAll("[data-marvis-open-log]"', 1
    )[-1]
    assert "手机办公室看不到其他小马" in conversation_html
    assert "Marvis" in conversation_html
    assert "dispatch task 已完成" in conversation_html
    assert "Marvis拍了拍 App Agent" in conversation_html
    assert "App Agent" in conversation_html
    assert "搞定，有请下一位。" in conversation_html
    assert "App Agent 那边反馈 Marvis 本身不在可操作应用范围内" in conversation_html
    assert "list windows" not in conversation_html
    assert "tool.call" not in conversation_html
    for forbidden in [
        "relay-board-grid",
        "role-lane",
        "查看结构化数据",
        "打开原生会话",
        "data-role-output",
        "data-view-panel=\"board\"",
        "任务进度",
        "调度决策",
        "你好，今天想做什么？",
        "marvis-relay-hero-avatar",
    ]:
        assert forbidden not in visible_html
    assert "工作日志" in work_log_html
    assert "产出物" in work_log_html
    assert "/static/marvis/office-desk-worker-" in work_log_html
    assert "/static/marvis/office-desk-empty-slot.png" in work_log_html
    assert 'data-marvis-work-log-role="director"' in work_log_html
    assert 'data-marvis-work-log-role="implementer"' in work_log_html
    assert "12K" in work_log_html
    assert "list windows 已完成" in work_log_html
    assert "marvis-work-log-tool-chip" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_humanizes_internal_user_prompt(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Internal prompt task",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-internal",
        agent_run_id=251,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    _append_runtime_event(
        runtime_store,
        agent_run_id=251,
        event_type=EventType.USER_MESSAGE_RECEIVED,
        payload={
            "text": (
                "task_id: 99\n"
                "role: director\n"
                "workspace: /repo\n"
                "goal: 删除测试接力文件\n"
                "latest_user_input: 删除之前测试接力流程生成的md文件\n"
                "handoff_summaries:\n"
                "constraints:\n"
                "- Return only one strict JSON object.\n"
                "expected_output_envelope:\n"
                '{"acceptance_criteria":["observable acceptance criterion"]}'
            ),
            "native_turn_id": "turn-internal-1",
            "itemId": "internal-user-1",
        },
        occurred_at="2026-06-14T12:15:01+00:00",
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

    assert "删除之前测试接力流程生成的md文件" in response
    assert "task_id: 99" not in response
    assert "Return only one strict JSON object" not in response
    assert "observable acceptance criterion" not in response
    assert "{&quot;acceptance_criteria" not in response


@pytest.mark.asyncio
async def test_relay_task_detail_humanizes_malformed_role_envelope(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Malformed envelope task",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-malformed",
        agent_run_id=301,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "blocked")
    service._store.update_task_status(task.id, "blocked")
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "error": "invalid json: Expecting ':' delimiter",
        },
        summary="invalid json: Expecting ':' delimiter",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=301,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": (
                '{"acceptance_criteria":["确认目标文件"],'
                '"artifact_type":"routing_decisioncomplexitylowevidence_refs":[]'
            ),
            "native_turn_id": "turn-malformed-1",
            "itemId": "malformed-assistant-1",
        },
        occurred_at="2026-06-14T12:20:01+00:00",
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

    conversation_html = _relay_view_panel_html(response, "conversation")
    board_html = _relay_view_panel_html(response, "board")
    assert 'data-native-kind="role_error"' not in conversation_html
    assert "总工程师输出格式异常，任务已阻塞。" not in conversation_html
    assert "invalid json: Expecting" not in conversation_html
    assert "接力暂停在总工程师，详情见工作日志。" in conversation_html
    assert "routing_decisioncomplexitylow" not in conversation_html
    assert '{"acceptance_criteria"' not in conversation_html
    assert "总工程师执行问题" in board_html
    assert "invalid json: Expecting" in board_html


@pytest.mark.asyncio
async def test_relay_task_detail_humanizes_valid_role_envelope(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Valid envelope task",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-valid",
        agent_run_id=401,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    _append_runtime_event(
        runtime_store,
        agent_run_id=401,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": json.dumps(
                {
                    "status": "waiting_user",
                    "role": "director",
                    "artifact_type": "routing_decision",
                    "summary": "需要先确认具体文件路径。",
                    "next_action": "请用户确认要删除的 md 文件。",
                    "open_questions": ["是否删除 /repo/测试接力.md？"],
                    "route": "waiting_user",
                    "risk": "medium",
                    "required_roles": ["director"],
                    "acceptance_criteria": ["确认精确路径后再执行"],
                    "requires_user_approval": True,
                },
                ensure_ascii=False,
            ),
            "native_turn_id": "turn-valid-1",
            "itemId": "valid-assistant-1",
        },
        occurred_at="2026-06-14T12:30:01+00:00",
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

    assert 'data-native-kind="role_envelope"' in response
    assert "结论：需要先确认具体文件路径。" in response
    assert "下一步：请用户确认要删除的 md 文件。" in response
    assert "待确认：是否删除 /repo/测试接力.md？" in response
    assert '"artifact_type": "routing_decision"' not in response
    assert '"required_roles": ["director"]' not in response


@pytest.mark.asyncio
async def test_relay_task_detail_uses_canonical_envelope_when_output_has_bad_prefix(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Bad prefix envelope task",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-prefix",
        agent_run_id=451,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    canonical = {
        "status": "passed",
        "reason": "completed",
        "role": "director",
        "artifact_type": "final_summary",
        "handoff_to": "",
        "summary": "完成闭环修复，最终展示只使用权威完成态。",
        "evidence_refs": ["completed transcript"],
        "next_action": "继续观察全新复杂接力任务。",
        "open_questions": [],
        "acceptance_criteria": ["会话流不显示污染前缀"],
    }
    polluted_output = "坏前缀：模型先说了一段废话\n" + json.dumps(
        canonical,
        ensure_ascii=False,
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            **canonical,
            "relay_role": "director",
            "output": polluted_output,
        },
        summary=canonical["summary"],
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=451,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": "坏前缀：模型先说了一段废话\n",
            "native_turn_id": "turn-prefix-1",
            "itemId": "prefix-assistant-1",
        },
        occurred_at="2026-06-14T12:35:01+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=451,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": polluted_output,
            "native_turn_id": "turn-prefix-1",
            "itemId": "prefix-assistant-1",
        },
        occurred_at="2026-06-14T12:35:02+00:00",
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

    visible_html = response.split("<script", 1)[0]
    assert 'data-conversation-role-final="director"' in visible_html
    assert 'data-role-canonical-json="director"' not in visible_html
    assert "结论：完成闭环修复，最终展示只使用权威完成态。" in visible_html
    assert "下一步：继续观察全新复杂接力任务。" in visible_html
    assert "验收依据：会话流不显示污染前缀" in visible_html
    assert "&quot;artifact_type&quot;: &quot;final_summary&quot;" not in visible_html
    assert "模型先说了一段废话" not in visible_html


@pytest.mark.asyncio
async def test_relay_conversation_role_replies_are_humanized_to_chinese(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="English role output task",
        prompt="请按五角色接力给出中文结果。",
        workspace="/repo",
        provider="codex",
    )
    role_payloads = {
        "director": (
            "routing_decision",
            "Route to full relay for read-only browser verification.",
            "Hand off to architect for planning.",
            "All role replies are readable in Chinese.",
        ),
        "architect": (
            "architecture_plan",
            "Formulated read-only browser re-testing architecture plan.",
            "Hand off to implementer for implementation notes.",
            "Architecture summary is available to the user.",
        ),
        "implementer": (
            "implementation_report",
            "Confirmed no code changes are required for this verification pass.",
            "Hand off to tester for validation.",
            "Implementation report is readable.",
        ),
        "tester": (
            "test_report",
            "Validated browser conversation timeline and board separation.",
            "Hand off to auditor for final review.",
            "Test evidence is understandable.",
        ),
        "auditor": (
            "audit_report",
            "Reviewed completed transcript authority and delta filtering risks.",
            "Return to director for final summary.",
            "Audit result is clear.",
        ),
    }
    for index, (role, (artifact_type, summary, next_action, acceptance)) in enumerate(
        role_payloads.items(),
        start=1,
    ):
        service._store.update_role_metadata(
            task.id,
            role,
            provider="codex",
            model="gpt-5",
            native_session_id=f"native-{role}-english",
            agent_run_id=500 + index,
            dispatch_verified=True,
        )
        service._store.update_role_status(task.id, role, "passed")
        service._store.save_artifact(
            task.id,
            role,
            artifact_type,
            {
                "status": "passed",
                "reason": "completed",
                "role": role,
                "relay_role": role,
                "artifact_type": artifact_type,
                "handoff_to": "",
                "summary": summary,
                "evidence_refs": ["browser verification"],
                "open_questions": [],
                "next_action": next_action,
                "acceptance_criteria": [acceptance],
            },
            summary=summary,
        )
    service._store.update_task_status(task.id, "completed")

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

    conversation_html = _relay_view_panel_html(response, "conversation")
    message_bodies = _relay_message_bodies_html(conversation_html)
    assert conversation_html.count('data-conversation-role-final="') == 5
    assert conversation_html.count('data-role-canonical-json="') == 0
    for english_phrase in (
        "Route to full relay",
        "Formulated read-only browser",
        "Confirmed no code changes",
        "Validated browser conversation",
        "Reviewed completed transcript",
        "Hand off to",
        "Return to director",
        "All role replies are readable in Chinese",
    ):
        assert english_phrase not in message_bodies
    assert "该角色已返回结构化结果，详情见结构化数据。" in message_bodies
    assert "下一步：下一步见结构化数据。" in message_bodies
    assert "验收依据：验收依据见结构化数据。" in message_bodies


@pytest.mark.asyncio
async def test_relay_conversation_hides_blocked_error_details_and_dedupes_user_prompt(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    prompt = (
        "请按完整五角色接力流程，只做只读验证：确认 relay 会话流只展示五角色 "
        "canonical 摘要和 JSON，任务状态只展示进度/状态。"
    )
    task = service.create_task(
        title="Blocked conversation semantics",
        prompt=prompt,
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-blocked-ui",
        agent_run_id=452,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "blocked")
    service._store.update_task_status(task.id, "blocked")
    canonical = {
        "status": "passed",
        "reason": "routed",
        "role": "director",
        "artifact_type": "routing_decision",
        "handoff_to": "architect",
        "summary": "路由到五角色完整接力，下一步交给架构工程师。",
        "route": "full_relay",
        "risk": "low",
        "required_roles": [
            "director",
            "architect",
            "implementer",
            "tester",
            "auditor",
        ],
        "evidence_refs": [],
        "next_action": "交给架构工程师制定验证步骤。",
        "open_questions": [],
        "acceptance_criteria": ["会话流不展示底层错误详情"],
    }
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {**canonical, "relay_role": "director"},
        summary=canonical["summary"],
    )
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "error": "invalid json: Expecting ',' delimiter",
        },
        summary="invalid json: Expecting ',' delimiter",
    )
    retry_prompt = (
        "你刚才作为总工程师输出的接力结果不是合法 role_envelope JSON。\n"
        "expected_output_envelope:\n"
        '{"artifact_type":"routing_decision","handoff_to":"architect"}'
    )
    for item_id, text, occurred_at in (
        ("blocked-user-1", prompt, "2026-06-14T13:00:01+00:00"),
        ("blocked-user-2", prompt, "2026-06-14T13:00:02+00:00"),
        ("blocked-user-retry", retry_prompt, "2026-06-14T13:00:03+00:00"),
    ):
        _append_runtime_event(
            runtime_store,
            agent_run_id=452,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            payload={
                "text": text,
                "native_turn_id": "turn-blocked-ui",
                "itemId": item_id,
            },
            occurred_at=occurred_at,
        )
    _append_runtime_event(
        runtime_store,
        agent_run_id=452,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": (
                "总工程师输出格式异常，任务已阻塞。\n"
                "错误：invalid json: Expecting ',' delimiter\n"
                "请补充确认后重新调度，原始结构化输出不在主会话展示。"
            ),
            "native_turn_id": "turn-blocked-ui",
            "itemId": "blocked-assistant-error",
        },
        occurred_at="2026-06-14T13:00:04+00:00",
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

    conversation_html = _relay_view_panel_html(response, "conversation")
    board_html = _relay_view_panel_html(response, "board")
    assert conversation_html.count(prompt) == 1
    assert "结论：路由到五角色完整接力，下一步交给架构工程师。" in conversation_html
    assert "接力暂停在总工程师，详情见工作日志。" in conversation_html
    assert "输出格式异常" not in conversation_html
    assert "任务已阻塞" not in conversation_html
    assert "invalid json" not in conversation_html
    assert "请补充确认后重新调度" not in conversation_html
    assert "expected_output_envelope" not in conversation_html
    assert "你刚才作为总工程师" not in conversation_html
    assert 'data-conversation-role-preview="director"' not in conversation_html
    assert "总工程师执行问题" in board_html
    assert "invalid json: Expecting" in board_html


@pytest.mark.asyncio
async def test_relay_task_detail_projects_running_delta_as_initial_preview(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Live preview task",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-live-preview",
        agent_run_id=501,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    _append_runtime_event(
        runtime_store,
        agent_run_id=501,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": "总工程师正在拆解任务，先确认影响面。",
            "native_turn_id": "turn-live-preview",
            "itemId": "assistant-live-preview",
        },
        occurred_at="2026-06-14T14:00:01+00:00",
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

    conversation_html = _relay_view_panel_html(response, "conversation")
    assert 'data-conversation-role-preview="director"' in conversation_html
    assert 'data-conversation-role-final="director"' not in conversation_html
    assert 'data-raw-preview="总工程师正在拆解任务，先确认影响面。"' in conversation_html
    assert 'data-preview-event-ids="1"' in conversation_html
    assert "总工程师正在拆解任务，先确认影响面。" in _relay_message_bodies_html(
        conversation_html
    )


@pytest.mark.asyncio
async def test_relay_task_detail_projects_structured_running_delta_as_counted_preview(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Structured live preview task",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-structured-preview",
        agent_run_id=502,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    delta = '{"artifact_type":"routing_decision","handoff_to":"architect"'
    _append_runtime_event(
        runtime_store,
        agent_run_id=502,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": delta,
            "native_turn_id": "turn-structured-preview",
            "itemId": "assistant-structured-preview",
        },
        occurred_at="2026-06-14T14:00:01+00:00",
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

    conversation_html = _relay_view_panel_html(response, "conversation")
    bodies_html = _relay_message_bodies_html(conversation_html)
    assert 'data-conversation-role-preview="director"' in conversation_html
    assert "正在接收结构化输出" in bodies_html
    assert f"已接收 {len(delta)} 字" in bodies_html


@pytest.mark.asyncio
async def test_relay_task_detail_streaming_delta_is_preview_not_final_output(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Preview task",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
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

    assert 'data-role-preview="' not in response
    assert 'data-native-kind="waiting"' in response
    assert "..." in response
    assert "function appendRolePreview" in response
    assert "function relayPreviewDisplayText" in response
    assert "function renderRoleEnvelope" in response
    assert "已接收 ${value.length} 字" in response
    assert "function clearAllRolePreviews" in response
    assert "const seenPreviewEventKeys = new Set" in response
    assert "function previewEventKey" in response
    assert "renderRelayNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);" in response
    assert 'appendRolePreview(payload.role, payload.delta || payload.text || "", payload.runtime_event_id);' in response
    delta_handler = response.split('source.addEventListener("role.output_delta"', 1)[1]
    delta_handler = delta_handler.split('source.addEventListener("routing.decision"', 1)[0]
    assert "appendRolePreview" in delta_handler
    assert "roleOutputs[payload.role]" not in delta_handler
    envelope_handler = response.split('source.addEventListener("role.envelope"', 1)[1]
    envelope_handler = envelope_handler.split('source.addEventListener("handoff.created"', 1)[0]
    assert "renderRoleEnvelope" in envelope_handler
    assert "clearRolePreview(role);" in response


@pytest.mark.asyncio
async def test_relay_task_detail_humanizes_internal_route_terms(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Gold price task",
        prompt="今日黄金多少钱？",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-gold",
        agent_run_id=402,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    _append_runtime_event(
        runtime_store,
        agent_run_id=402,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": json.dumps(
                {
                    "status": "passed",
                    "role": "director",
                    "artifact_type": "routing_decision",
                    "summary": "路由为director_only：直接查询并汇总今日金价。",
                    "next_action": "complete directly after routing by checking current market sources and returning the latest available gold price",
                    "route": "director_only",
                    "risk": "low",
                    "required_roles": ["director"],
                    "acceptance_criteria": ["不展示 director_only 给用户"],
                    "requires_user_approval": False,
                },
                ensure_ascii=False,
            ),
            "native_turn_id": "turn-gold-1",
            "itemId": "gold-assistant-1",
        },
        occurred_at="2026-06-14T12:40:01+00:00",
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

    visible_html = response.split("<script", 1)[0]
    assert 'data-native-kind="role_envelope"' in visible_html
    assert 'data-role-canonical-json="director"' not in visible_html
    assert "结论：由总工程师直接处理：直接查询并汇总今日金价。" in visible_html
    assert "下一步：由总工程师核验最新行情来源并给出结果" in visible_html
    assert "验收依据：不展示 总工程师直接处理 给用户" in visible_html
    assert "路由为director_only" not in visible_html
    assert (
        "complete directly after routing by checking current market sources"
        not in visible_html
    )


@pytest.mark.asyncio
async def test_relay_task_detail_hides_retry_prompt_and_malformed_protocol_output(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Complex relay task",
        prompt="请按完整五角色接力流程审查，不要修改任何文件。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-complex",
        agent_run_id=502,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_task_status(task.id, "running")
    retry_prompt = (
        "你刚才作为总工程师输出的接力结果不是合法 role_envelope JSON，服务端无法继续工作流。\n"
        "请只重新输出一个合法 JSON object。\n"
        'expected_output_envelope:\n{"route":"director_only|core_relay|full_relay",'
        '"next_action":"complete directly"}\n'
        "上一版无效输出如下，请保留语义、修正结构：\n"
        '{"artifact_type":"routing_decisioncomplexityhighroutefull_relay"}'
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=502,
        event_type=EventType.USER_MESSAGE_RECEIVED,
        payload={
            "text": retry_prompt,
            "native_turn_id": "turn-complex-retry",
            "itemId": "complex-user-retry",
        },
        occurred_at="2026-06-14T12:50:01+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=502,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": '{"artifact_type":"routing_decisioncomplexityhighroutefull_relaynext_actioncomplete directly"',
            "native_turn_id": "turn-complex-retry",
            "itemId": "complex-assistant-malformed",
        },
        occurred_at="2026-06-14T12:50:02+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=502,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": (
                "总工程师输出格式异常，任务已阻塞。\n"
                "错误：invalid json: Expecting ',' delimiter\n"
                "请补充确认后重新调度，原始结构化输出不在主会话展示。"
                '{"artifact_type":"routing_decisioncomplexityhighroutefull_relay"}'
            ),
            "native_turn_id": "turn-complex-retry-2",
            "itemId": "complex-assistant-leaked-error",
        },
        occurred_at="2026-06-14T12:50:03+00:00",
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

    conversation_html = _relay_view_panel_html(response, "conversation")
    assert "系统已要求当前角色重新输出合法结构化结果。" not in conversation_html
    assert "总工程师的结构化输出已由系统处理，原始协议内容不在主会话展示。" not in conversation_html
    assert "总工程师输出格式异常，任务已阻塞。" not in conversation_html
    assert "expected_output_envelope" not in conversation_html
    assert "director_only" not in conversation_html
    assert "full_relay" not in conversation_html
    assert "complete directly" not in conversation_html
    assert "routing_decisioncomplexityhigh" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_board_view_activates_status_cards(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Board detail task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            f"GET /native/workflows/relay/tasks/{task.id}?token=secret&view=board HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'data-relay-view="board"' in response
    assert 'data-view-tab="conversation"' not in response
    assert 'data-view-tab="board"' not in response
    assert 'class="relay-view-switch"' not in response
    assert "任务进度" not in response.split("<script", 1)[0]
    assert "五角色进度" not in response
    assert "交接摘要" not in response
    assert "待确认问题" not in response
    assert "原生会话" not in response.split("<script", 1)[0]
    assert 'data-marvis-open-log aria-label="工作日志"' in response
    assert 'class="marvis-work-log"' in response


@pytest.mark.asyncio
async def test_relay_task_detail_uses_role_error_as_director_summary(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Blocked detail task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_role_status(task.id, "director", "blocked")
    service._store.update_task_status(task.id, "blocked")
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "error": "invalid json: Expecting value",
        },
        summary="invalid json: Expecting value",
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

    assert "执行问题：invalid json: Expecting value" in response
    assert "等待总工程师接收并形成决策摘要" not in response
    assert "调度决策未生成" not in response
    assert "总工程师执行问题：invalid json: Expecting value" in response
    assert "等待总工程师接收任务并形成调度决策" not in response
