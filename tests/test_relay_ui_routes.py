from __future__ import annotations

import asyncio
import json
import re
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
        end_marker = '<section class="relay-view relay-board-panel"'
    elif view == "board":
        start_marker = '<section class="relay-view relay-board-panel"'
        end_marker = "</main>"
    else:
        raise AssertionError(f"unknown relay view: {view}")
    assert start_marker in response
    assert end_marker in response
    return response.split(start_marker, 1)[1].split(end_marker, 1)[0]


def _relay_message_bodies_html(panel_html: str) -> str:
    return "\n".join(
        re.findall(
            r'<div class="relay-message-body" data-native-message-body>(.*?)</div>',
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
    assert '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260627-office">' in response
    assert 'class="marvis-relay-bottom-nav"' in response
    assert 'class="marvis-relay-composer"' in response
    assert 'class="marvis-relay-avatar marvis-relay-avatar-marvis"' in response
    assert 'href="/native/workflows/relay/office?token=secret"' in response
    assert "请输入任务" in response
    assert "任务历史" in response
    assert response.count('data-open-new-task>新接力任务</button>') == 1
    assert "配置" in response
    assert '<a class="relay-secondary" href="/native/workflows/relay/config' not in response
    assert (
        '<a class="relay-open" href="/native/workflows/relay/config?token=secret&amp;'
        'workspace=/Users/wl/projects/wlcodex">配置</a>'
    ) in response
    assert 'href="/native/workflows/relay/config?token=secret&amp;workspace=/Users/wl/projects/wlcodex"' in response
    assert "新聊天" not in response
    assert "relay-create-panel" not in response
    assert '<section class="relay-create-modal" id="new-task-modal" hidden role="dialog"' in response
    assert 'aria-label="new relay task"' in response
    assert 'data-close-new-task aria-label="关闭新接力任务"' in response
    assert '<h2>新接力任务</h2>' in response
    assert "当前工作区" in response
    assert "将快照以下五角色 provider 配置" in response
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
    assert f'<input type="hidden" name="workspace" value="{default_workspace}">' in response
    assert 'aria-label="relay role provider configuration"' not in response
    assert "Provider 应用于全部角色" not in response
    assert "默认角色配置" in response
    assert "总工程师 ·" in response
    assert "开发工程师 ·" in response
    assert "gitnexus-impact-analysis" not in response
    assert "test-driven-development" not in response
    assert 'aria-label="relay task history"' in response
    assert "native session list" not in response.lower()
    assert "Default workspace relay" in response
    assert "Other workspace relay" not in response
    assert "relay-task-card" in response
    assert "marvis-relay-task-card" in response
    assert 'class="relay-status-badge"' in response
    assert "等待总工程师接收" in response
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
    assert '<input type="hidden" name="workspace" value="/repo">' in populated
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
    assert "总工程师" in populated
    assert "架构工程师" in populated
    assert "开发工程师" in populated
    assert "测试工程师" in populated
    assert "审计工程师" in populated
    assert "等待总工程师接收" in populated
    assert "打开任务" in populated
    assert "open task" not in populated
    assert f'/native/workflows/relay/tasks/{repo_task.id}?token=secret' in populated
    assert f'/native/workflows/relay/tasks/{other.id}?token=secret' not in populated


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
    assert '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260627-office">' in response
    assert "Marvis办公室" in response
    assert 'href="/native/workflows/relay?token=secret"' in response
    assert "/static/marvis/office-desk-marvis.jpg" in response
    assert response.count("/static/marvis/office-desk-empty.jpg") == 5
    assert "data-marvis-persona-open" in response
    assert "/static/marvis/persona-modal-marvis.jpg" in response
    assert "Marvis（马维斯）" in response
    assert "今日消耗Token" in response
    assert "今日节省Token" in response


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
    assert '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260627-office">' in response
    assert 'class="marvis-relay-topbar"' in response
    assert 'class="marvis-relay-bottom-nav"' in response
    assert 'href="/native/workflows/relay/office?token=secret"' in response
    assert 'data-marvis-open-log aria-label="工作日志"' not in response
    assert 'class="marvis-work-log"' in response
    assert "工作日志" in response
    assert "产出物" in response
    assert 'class="marvis-relay-avatar marvis-relay-avatar-director"' in response
    assert 'class="marvis-relay-composer"' in response
    assert "请输入任务" in response
    assert response.count('data-role-output="director"') == 1
    assert response.count('<ol class="relay-activity-log" data-activity-log>') == 1
    assert response.count("data-routing-summary>") == 1
    assert response.count("<p data-board-next-step>") == 1
    assert 'data-marvis-snapshot-role-output="director"' in response
    assert "data-marvis-snapshot-activity-log" in response
    assert "data-marvis-snapshot-routing-summary" in response
    assert "data-marvis-snapshot-board-next-step" in response
    assert 'data-relay-view="conversation"' in response
    assert 'data-view-tab="conversation" data-view-active="true"' in response
    assert 'data-view-tab="board" data-view-active="false"' in response
    assert "会话流" in response
    assert "任务状态" in response
    assert (
        f'href="/native/workflows/relay/tasks/{task.id}?token=secret&amp;view=conversation"'
        in response
    )
    assert (
        f'href="/native/workflows/relay/tasks/{task.id}?token=secret&amp;view=board"'
        in response
    )
    assert 'class="relay-conversation"' in response
    assert 'data-native-conversation-timeline' in response
    assert 'class="relay-native-stack"' not in response
    assert 'data-native-session-stack' not in response
    assert 'class="relay-native-stream"' not in response
    assert 'class="relay-native-frame"' not in response
    assert "<iframe" not in response
    conversation_html = _relay_view_panel_html(response, "conversation")
    assert "让审计工程师确认一下" in conversation_html
    assert "我会先确认风险。" not in conversation_html
    assert "架构侧继续补齐影响面。" not in conversation_html
    assert "结论：本任务判定无需派发，由总工程师直接完成。" in conversation_html
    assert 'data-native-role="director"' in response
    assert 'data-native-role="architect"' in response
    assert 'data-view-panel="conversation"' in response
    assert 'data-view-panel="board"' in response
    assert "任务进度" in response
    assert "调度决策" in response
    assert "任务难度" in response
    assert "风险等级" in response
    assert "执行路径" in response
    assert "simple" in response
    assert "低" in response
    assert "总工程师直接完成" in response
    assert "验收依据" in response
    assert "给出清晰说明" in response
    assert "验收面板" in response
    assert "推进日志" in response
    assert "当前阶段" in response
    assert "当前负责角色" in response
    assert "下一步" in response
    assert "最近用户补充" in response
    assert "总工程师决策" in response
    assert "RelayBoard" not in response
    assert "current goal" not in response
    assert "latest user input" not in response
    assert "native_session_id:" not in response
    assert "provider/model:" not in response
    assert "open native session" not in response
    assert ">interrupt<" not in response
    assert ">send<" not in response
    for display_name in ["总工程师", "架构工程师", "开发工程师", "测试工程师", "审计工程师"]:
        assert display_name in response
    assert 'data-role="director"' in response
    assert 'data-role="architect"' in response
    assert "未纳入本轮路线" in response
    assert "继续补充给总工程师" in response
    assert "中断任务" in response
    assert "发送补充" in response
    assert "打开原生会话" in response
    assert "native_thread_id=native-director-1" in response
    assert "/sessions/native-director-1" not in response
    assert "执行问题：invalid json: Expecting value" in response
    assert "等待总工程师接收并形成决策摘要" not in response
    assert f"/api/relay/tasks/{task.id}/message" in response
    assert "relay-board-grid" in response
    assert "relay-progress" in response
    assert "relay-progress-status" in response
    assert "relay-activity-log" in response
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
    assert "需要你确认精确文件路径。" not in conversation_html
    kinds = _native_message_kinds(response)
    assert "activity" not in kinds
    assert "completed" not in kinds
    assert "turn_started" not in response
    assert "turn_completed" not in response
    keys = _native_message_keys(response)
    assert keys == list(dict.fromkeys(keys))


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
    assert "接力暂停在总工程师，详情见任务状态。" in conversation_html
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
    assert 'data-role-canonical-json="director"' in visible_html
    assert "结论：完成闭环修复，最终展示只使用权威完成态。" in visible_html
    assert "下一步：继续观察全新复杂接力任务。" in visible_html
    assert "验收依据：会话流不显示污染前缀" in visible_html
    assert "&quot;artifact_type&quot;: &quot;final_summary&quot;" in visible_html
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
    assert conversation_html.count('data-role-canonical-json="') == 5
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
    assert "接力暂停在总工程师，详情见任务状态。" in conversation_html
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

    assert 'data-role-preview="' in response
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
    message_body_html = visible_html.split('<details class="role-canonical-json">', 1)[0]
    assert 'data-native-kind="role_envelope"' in visible_html
    assert 'data-role-canonical-json="director"' in visible_html
    assert "结论：由总工程师直接处理：直接查询并汇总今日金价。" in visible_html
    assert "下一步：由总工程师核验最新行情来源并给出结果" in visible_html
    assert "验收依据：不展示 总工程师直接处理 给用户" in visible_html
    assert "路由为director_only" not in message_body_html
    assert (
        "complete directly after routing by checking current market sources"
        not in message_body_html
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
    assert 'data-view-tab="conversation" data-view-active="false"' in response
    assert 'data-view-tab="board" data-view-active="true"' in response
    assert "任务状态" in response
    assert "任务进度" in response
    assert "五角色进度" in response
    assert "交接摘要" in response
    assert "待确认问题" in response
    assert "原生会话" in response
    assert (
        f'href="/native/workflows/relay/tasks/{task.id}?token=secret&amp;view=conversation"'
        in response
    )
    assert (
        f'href="/native/workflows/relay/tasks/{task.id}?token=secret&amp;view=board"'
        in response
    )


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
    assert "调度决策未生成" in response
    assert "总工程师输出协议错误：invalid json: Expecting value" in response
    assert "等待总工程师接收任务并形成调度决策" not in response
