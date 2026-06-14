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
    server, service = _server(tmp_path)
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
    assert "流式接力" in response
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
    assert 'class="relay-task-card"' in response
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
    assert f'/native/workflows/relay/tasks/{repo_task.id}?token=secret' in populated
    assert f'/native/workflows/relay/tasks/{other.id}?token=secret' not in populated


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
    server, service = _server(tmp_path)
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
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "low risk explanation",
            "role": "director",
            "summary": "本任务判定无需派发，由总工程师直接完成。",
            "complexity": "simple",
            "risk": "low",
            "route": "director_only",
            "required_roles": ["director"],
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
    assert 'data-conversation-timeline' in response
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
    assert "总工程师已接收，正在拆解任务。" in response
    assert "请确认验收标准" in response
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
    assert "events${EVENTS_SUFFIX}" in response
    assert "appendConversationDelta(payload.role" in response
    assert "activeConversationRole !== role" in response
    assert "appendConversationUser(String(data.text" in response
    for event_name in [
        "role.queued",
        "role.streaming",
        "dispatch.verified",
        "dispatch.fallback",
        "role.output_delta",
        "role.envelope",
        "handoff.created",
        "role.status",
        "task.completed",
        "task.interrupted",
    ]:
        assert event_name in response


@pytest.mark.asyncio
async def test_relay_task_detail_board_view_activates_status_cards(
    tmp_path: Path,
) -> None:
    server, service = _server(tmp_path)
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
    server, service = _server(tmp_path)
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
