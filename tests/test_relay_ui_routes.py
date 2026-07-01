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
from wlcodex.live_stream.server import (
    WorkerLiveStreamServer,
    _marvis_relay_clean_artifact_summary,
    _marvis_relay_merge_work_log_entry,
    _marvis_relay_role_error_payloads_by_role,
    _marvis_relay_work_log_entry_from_event,
    _marvis_relay_work_log_entry_html,
    _relay_activity_label,
)
from wlcodex.live_stream.models import WorkerStreamEvent
from wlcodex.native_agents.models import NativeAgentCapabilities
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.models import HandoffPacket
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore
from wlcodex.relay.work_log_projection import (
    RawWorkLogEntry,
    WORK_LOG_PROJECTION_PROFILES,
    compress_work_log_entries,
)
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

    datetime_label = _relay_activity_label(datetime(2026, 6, 16, 7, 58, 48, tzinfo=timezone.utc))
    assert datetime_label == "最近活动 06-16 15:58"

    fallback_label = _relay_activity_label("2026-06-16T07:51:57.510982123+00:00")
    assert fallback_label == "最近活动 06-16 15:51"
    assert "2026-06-16T" not in fallback_label


def test_marvis_work_log_cleans_escaped_protocol_summary_fragment() -> None:
    raw = (
        'https://www.kitco.com/charts/gold\\",\\"handoff_to\\":\\"\\",'
        '\\"reason\\":\\"已查询实时贵金属报价源\\",\\"role\\":\\"director\\",'
        '\\"summary\\":\\"截至查询时，今日国际现货黄金约为 4,088.60 美元/盎司。\\"}'
    )

    assert _marvis_relay_clean_artifact_summary(raw) == (
        "截至查询时，今日国际现货黄金约为 4,088.60 美元/盎司。"
    )


def test_marvis_work_log_collapses_long_message_output() -> None:
    long_text = (
        "现在生成页面代码：\n"
        "```html\n"
        "<!doctype html>\n"
        "<html><body><pre>" + "x" * 900 + "</pre></body></html>\n"
        "```"
    )
    event = WorkerStreamEvent(
        id=1,
        type=EventType.MODEL_MESSAGE_COMPLETED,
        kind="message_completed",
        agent_run_id=101,
        conversation_id=None,
        occurred_at="2026-06-14T12:10:00+00:00",
        source="codex",
        actor="codex_native",
        visibility="user",
        payload={
            "text": long_text,
            "native_turn_id": "turn-long-message",
            "itemId": "assistant-long-message",
        },
    )

    entry = _marvis_relay_work_log_entry_from_event("director", event)
    assert entry is not None
    assert entry.text == ""
    assert entry.chip == "过程输出 已折叠"
    assert entry.output == long_text
    html = _marvis_relay_work_log_entry_html(entry)
    paragraph_html = html.split("<details", 1)[0]
    assert "过程输出 已折叠" in paragraph_html
    assert "输出较长，已折叠。" not in paragraph_html
    assert "<!doctype html>" not in paragraph_html
    assert "查看输出" in html
    assert "&lt;!doctype html&gt;" in html


def test_marvis_work_log_collapses_machine_transcript_output() -> None:
    machine_output = (
        "Task not found\n"
        "Found 9 files\n"
        "tests/test_relay_service.py\n"
        "tests/test_relay_ui_routes.py\n"
        "wlcodex/live_stream/server.py\n"
        "No files found\n"
        "4059:_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS: dict[str, tuple[tuple[str, str], ...]] = {\n"
        "4098:    labels = _MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS.get(str(role or '').strip(), ())"
    )
    event = WorkerStreamEvent(
        id=2,
        type=EventType.MODEL_MESSAGE_COMPLETED,
        kind="message_completed",
        agent_run_id=102,
        conversation_id=None,
        occurred_at="2026-06-14T12:11:00+00:00",
        source="codex",
        actor="codex_native",
        visibility="user",
        payload={
            "text": machine_output,
            "native_turn_id": "turn-machine-output",
            "itemId": "assistant-machine-output",
        },
    )

    entry = _marvis_relay_work_log_entry_from_event("implementer", event)
    assert entry is not None
    assert entry.text == ""
    assert entry.chip == "过程输出 已折叠"
    assert entry.output == machine_output
    html = _marvis_relay_work_log_entry_html(entry)
    visible_html = html.split("<details", 1)[0]
    assert "Found 9 files" not in visible_html
    assert "No files found" not in visible_html
    assert "4059:_MARVIS" not in visible_html
    assert "Task not found" not in visible_html
    assert "查看输出" in html
    assert "Found 9 files" in html


def test_marvis_work_log_keeps_natural_language_message_visible() -> None:
    text = "我先定位 Marvis 会话组件，再核对输入区和工作日志的投影规则。"
    event = WorkerStreamEvent(
        id=3,
        type=EventType.MODEL_MESSAGE_COMPLETED,
        kind="message_completed",
        agent_run_id=103,
        conversation_id=None,
        occurred_at="2026-06-14T12:12:00+00:00",
        source="codex",
        actor="codex_native",
        visibility="user",
        payload={
            "text": text,
            "native_turn_id": "turn-natural-language",
            "itemId": "assistant-natural-language",
        },
    )

    entry = _marvis_relay_work_log_entry_from_event("director", event)
    assert entry is not None
    assert entry.text == text
    assert entry.chip == ""
    assert entry.output == ""


def test_marvis_work_log_merges_command_output_chunks_under_single_chip() -> None:
    first = WorkerStreamEvent(
        id=4,
        type=EventType.COMMAND_OUTPUT_DELTA,
        kind="command_output",
        agent_run_id=104,
        conversation_id=None,
        occurred_at="2026-06-14T12:13:00+00:00",
        source="codex",
        actor="codex_native",
        visibility="user",
        payload={
            "command": "/bin/zsh -lc 'rg Marvis'",
            "output": "Found 9 files\nwlcodex/live_stream/server.py",
            "native_turn_id": "turn-rg-output",
            "itemId": "cmd-rg",
        },
    )
    second = WorkerStreamEvent(
        id=5,
        type=EventType.COMMAND_OUTPUT_DELTA,
        kind="command_output",
        agent_run_id=104,
        conversation_id=None,
        occurred_at="2026-06-14T12:13:01+00:00",
        source="codex",
        actor="codex_native",
        visibility="user",
        payload={
            "command": "/bin/zsh -lc 'rg Marvis'",
            "output": "4059:_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS = {",
            "native_turn_id": "turn-rg-output",
            "itemId": "cmd-rg",
        },
    )

    entry = _marvis_relay_work_log_entry_from_event("implementer", first)
    update = _marvis_relay_work_log_entry_from_event("implementer", second)
    assert entry is not None
    assert update is not None
    _marvis_relay_merge_work_log_entry(entry, update)

    assert entry.text == ""
    assert entry.chip == "rg 进行中"
    assert "Found 9 files" in entry.output
    assert "4059:_MARVIS" in entry.output
    html = _marvis_relay_work_log_entry_html(entry)
    visible_html = html.split("<details", 1)[0]
    assert "Found 9 files" not in visible_html
    assert "4059:_MARVIS" not in visible_html
    assert html.count("data-marvis-work-log-output") == 1


def test_marvis_work_log_projection_summarizes_tool_storm_and_hides_agent_dump() -> None:
    entries = [
        RawWorkLogEntry(
            kind="message",
            key="message:implementer:1",
            text="The file wlcodex/live_stream/static/relay_marvis.css has been updated successfully.",
        ),
        RawWorkLogEntry(
            kind="message",
            key="message:implementer:2",
            text="Let me summarize the visible relay UI state before handing off.",
        ),
        RawWorkLogEntry(kind="command", key="cmd:rg", chip="rg 已完成", output="No matches found"),
        RawWorkLogEntry(kind="command", key="cmd:sed", chip="sed 已完成", output="Found 9 files"),
        RawWorkLogEntry(
            kind="command", key="cmd:git", chip="git 已完成", output="位于分支 codex/relay"
        ),
        RawWorkLogEntry(kind="command", key="cmd:pytest", chip="pytest 已完成", output="2 passed"),
        RawWorkLogEntry(
            kind="artifact",
            key="artifact:implementer",
            chip="开发结果 已完成",
            text="已调整页面安全区、头像位置和附件区域背景融合。",
        ),
        RawWorkLogEntry(
            kind="error",
            key="error:director",
            chip="调用失败",
            text=(
                "总工程师执行问题：invalid json: Expecting ',' delimiter: line 1 column 42 "
                "(char 41)"
            ),
            failed=True,
        ),
    ]

    projected = compress_work_log_entries(entries, role="implementer", profile="marvis")

    assert WORK_LOG_PROJECTION_PROFILES["marvis"].tool_batch_threshold == 4
    visible = "\n".join(part for entry in projected for part in (entry.chip, entry.text) if part)
    output = "\n".join(entry.output for entry in projected if entry.output)
    assert "已调整页面安全区、头像位置和附件区域背景融合" in visible
    assert "工具调用 4 次" in visible
    assert "检索 1 次" in visible
    assert "测试 1 次" in visible
    assert "结构化结果不是合法 JSON" in visible
    assert "Let me summarize the visible relay UI state" in visible
    assert "updated successfully" not in visible
    assert "No matches found" not in visible
    assert "Found 9 files" not in visible
    assert "位于分支" not in visible
    assert "No matches found" in output


@pytest.mark.asyncio
async def test_relay_task_detail_work_log_collapses_machine_output_in_default_view(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Work log raw output task",
        prompt="修复 Marvis 工作日志默认视图。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="codex",
        model="gpt-5",
        native_session_id="native-implementer-work-log",
        agent_run_id=501,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "implementer", "passed")
    machine_output = (
        "Task not found\n"
        "Found 9 files\n"
        "tests/test_relay_service.py\n"
        "tests/test_relay_ui_routes.py\n"
        "wlcodex/live_stream/server.py\n"
        "No files found\n"
        "4059:_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS: dict[str, tuple[tuple[str, str], ...]] = {"
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=501,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": machine_output,
            "native_turn_id": "turn-machine-transcript",
            "itemId": "assistant-machine-transcript",
        },
        occurred_at="2026-06-14T12:20:00+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=501,
        event_type=EventType.COMMAND_OUTPUT_DELTA,
        payload={
            "command": "/bin/zsh -lc 'rg Marvis'",
            "output": "Found 9 files\nwlcodex/live_stream/server.py",
            "native_turn_id": "turn-rg",
            "itemId": "cmd-rg",
        },
        occurred_at="2026-06-14T12:20:01+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=501,
        event_type=EventType.COMMAND_OUTPUT_DELTA,
        payload={
            "command": "/bin/zsh -lc 'rg Marvis'",
            "output": "4059:_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS = {",
            "native_turn_id": "turn-rg",
            "itemId": "cmd-rg",
        },
        occurred_at="2026-06-14T12:20:02+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=501,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "我已定位到工作日志投影，把原始输出收进可展开详情。",
            "native_turn_id": "turn-natural",
            "itemId": "assistant-natural",
        },
        occurred_at="2026-06-14T12:20:03+00:00",
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
    default_html = re.sub(
        r'<details class="marvis-work-log-output"[^>]*>.*?</details>',
        "",
        work_log_html,
        flags=re.DOTALL,
    )
    assert "过程输出 已折叠" in default_html
    assert "rg 进行中" in default_html
    assert "我已定位到工作日志投影" in default_html
    assert "Found 9 files" not in default_html
    assert "No files found" not in default_html
    assert "4059:_MARVIS" not in default_html
    assert "Task not found" not in default_html
    assert "查看输出" in work_log_html
    assert "Found 9 files" in work_log_html
    assert "4059:_MARVIS" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_work_log_projects_marvis_summary_not_tool_dump(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Marvis work log summary task",
        prompt="修复 Marvis 头像遮挡和附件背景融合。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="codex",
        model="gpt-5",
        native_session_id="native-implementer-summary",
        agent_run_id=601,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "status": "passed",
            "reason": "completed",
            "role": "implementer",
            "relay_role": "implementer",
            "artifact_type": "implementation_report",
            "handoff_to": "auditor",
            "summary": "已调整页面安全区、头像位置和附件区域背景融合。",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
            "acceptance_criteria": [],
        },
        summary="已调整页面安全区、头像位置和附件区域背景融合。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "role": "director",
            "relay_role": "director",
            "artifact_type": "role_error",
            "error": "invalid json: Expecting ',' delimiter: line 1 column 42 (char 41)",
        },
        summary="invalid json",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=601,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": (
                "The file wlcodex/live_stream/static/relay_marvis.css has been updated successfully."
            ),
            "native_turn_id": "turn-agent-dump",
            "itemId": "assistant-agent-dump",
        },
        occurred_at="2026-06-14T12:21:00+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=601,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "Let me summarize the visible relay UI state before handing off.",
            "native_turn_id": "turn-natural-english",
            "itemId": "assistant-natural-english",
        },
        occurred_at="2026-06-14T12:21:01+00:00",
    )
    for index, (command, output) in enumerate(
        [
            ("/bin/zsh -lc 'rg Marvis'", "No matches found"),
            ("/bin/zsh -lc 'sed -n 1,80p wlcodex/live_stream/server.py'", "Found 9 files"),
            ("/bin/zsh -lc 'git status --short'", "位于分支 codex/relay\n尚未暂存"),
            ("/bin/zsh -lc '.venv/bin/pytest tests/test_relay_ui_routes.py -q'", "2 passed"),
        ],
        start=1,
    ):
        _append_runtime_event(
            runtime_store,
            agent_run_id=601,
            event_type=EventType.COMMAND_COMPLETED,
            payload={
                "command": command,
                "output": output,
                "native_turn_id": "turn-tool-storm",
                "itemId": f"cmd-tool-{index}",
            },
            occurred_at=f"2026-06-14T12:21:0{index}+00:00",
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
    default_html = re.sub(
        r'<details class="marvis-work-log-output"[^>]*>.*?</details>',
        "",
        work_log_html,
        flags=re.DOTALL,
    )
    assert "已调整页面安全区、头像位置和附件区域背景融合" in default_html
    assert "工具调用 4 次" in default_html
    assert "检索 1 次" in default_html
    assert "测试 1 次" in default_html
    assert "结构化结果不是合法 JSON" in default_html
    assert "Let me summarize the visible relay UI state" in default_html
    assert "updated successfully" not in default_html
    assert "rg 已完成" not in default_html
    assert "sed 已完成" not in default_html
    assert "git 已完成" not in default_html
    assert "No matches found" not in default_html
    assert "Found 9 files" not in default_html
    assert "位于分支" not in default_html
    assert "No matches found" in work_log_html


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
    return (
        WorkerLiveStreamServer(
            host="127.0.0.1",
            port=0,
            hub=WorkerLiveStreamHub(runtime_store),
            native_registry=NativeAgentRegistry(providers),
            relay_service=service,
            access_token="secret",
            allow_unauthenticated_loopback=False,
        ),
        service,
        runtime_store,
    )


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


def test_marvis_relay_source_does_not_keep_legacy_board_projection() -> None:
    server_source = Path("wlcodex/live_stream/server.py").read_text()
    css_source = Path("wlcodex/live_stream/static/relay_marvis.css").read_text()

    for legacy_symbol in [
        "_relay_role_progress_html",
        "_relay_routing_decision_html",
        "_relay_conversation_html_from_events",
        "_relay_initial_conversation_html",
        "_relay_role_panel_html",
        "_relay_native_conversation_html",
    ]:
        assert legacy_symbol not in server_source

    for legacy_dom in [
        "data-activity-log",
        "data-board-",
        "data-progress-role",
        "data-role-output",
        "data-role-preview",
        "data-routing-",
        "relay-board-grid",
        "relay-activity",
        "role-lane",
        "role-canonical-json",
        "relay-board h2",
        "查看结构化数据",
    ]:
        assert legacy_dom not in server_source
        assert legacy_dom not in css_source


@pytest.mark.asyncio
async def test_marvis_relay_composer_has_real_attachment_sheet(
    tmp_path: Path,
) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native/workflows/relay/chat?token=secret HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    assert "data-marvis-attachment-sheet" in response
    assert "data-marvis-attach-open" in response
    assert "data-marvis-image-input" in response
    assert "data-marvis-file-input" in response
    assert "添加到对话" in response
    assert "相册" in response
    assert "本地文件" in response
    assert "我的技能" in response
    assert "添加技能" in response
    assert "/static/marvis/attachment-icon-album-marvis.png" in response
    assert "/static/marvis/attachment-icon-local-file-marvis.png" in response
    assert "/static/marvis/attachment-icon-skills-marvis.png" in response
    assert "readRelayImageAttachment" in response
    assert "readRelayTextAttachment" in response
    css_response, _service = await _request(
        tmp_path,
        "GET /static/relay_marvis.css HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
    )
    assert "--marvis-s25-bottom-nav-item-gap: 1px" in css_response
    assert "--marvis-s25-attachment-sheet-height: 487px" in css_response
    assert "--marvis-s25-attachment-tile-height: 121px" in css_response
    assert "--marvis-s25-attachment-skill-icon: 82px" in css_response
    assert "background: rgba(0, 0, 0, .45)" in css_response
    assert ".marvis-relay-sheet-icon-native" in css_response
    assert ".marvis-relay-sheet-icon-album::before" not in css_response
    assert ".marvis-relay-sheet-icon-local-file::before" not in css_response
    assert ".marvis-relay-sheet-icon-skills::before" not in css_response
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css_response
    assert "gap: 20px" in css_response
    for icon_path in (
        "/static/marvis/attachment-icon-album-marvis.png",
        "/static/marvis/attachment-icon-local-file-marvis.png",
        "/static/marvis/attachment-icon-skills-marvis.png",
    ):
        icon_response, _service = await _request(
            tmp_path,
            f"GET {icon_path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
        assert "HTTP/1.1 200 OK" in icon_response
        assert "Content-Type: image/png" in icon_response


@pytest.mark.asyncio
async def test_relay_task_detail_projects_followup_turns_into_marvis_chat(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Follow-up relay task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_task_status(task.id, "completed")
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "text": "继续解释为什么没有显示",
            "target_role": "director",
            "context_packet_id": 41,
        },
        summary="继续解释为什么没有显示",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "followup_response",
        {
            "text": "问题在主会话投影层，现在已经接续到同一个 task。",
            "target_role": "user",
        },
        summary="问题在主会话投影层，现在已经接续到同一个 task。",
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
    work_log_html = _relay_work_log_html(response)
    assert "继续解释为什么没有显示" in conversation_html
    assert "问题在主会话投影层，现在已经接续到同一个 task。" in conversation_html
    assert 'class="marvis-relay-user-message"' in conversation_html
    assert 'data-native-kind="followup_response"' in conversation_html
    assert "继续解释为什么没有显示" not in work_log_html
    assert "function appendMarvisConversationUser" in response
    assert 'addRelayEventListener("user.followup"' in response
    assert 'addRelayEventListener("role.followup_response"' in response


@pytest.mark.asyncio
async def test_relay_task_detail_keeps_invalid_artifact_out_of_marvis_chat(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Invalid semantic follow-up",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_task_status(task.id, "waiting_user")
    service._store.update_role_status(task.id, "director", "waiting")
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "text": "继续按计划模式走",
            "target_role": "director",
            "round_id": 2,
        },
        summary="继续按计划模式走",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "followup_response",
        {
            "text": "这是 provider 原始正文，应该完整出现在主对话。",
            "target_role": "user",
            "status": "waiting",
            "round_id": 2,
            "semantic_invalid": True,
        },
        summary="这是 provider 原始正文，应该完整出现在主对话。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "role_artifact_invalid",
        {
            "relay_role": "director",
            "error": "missing required fields: status",
            "output": '{"summary":"正文里带了坏 JSON"}',
            "round_id": 2,
        },
        summary="结构化产物未采用，已保留原始回答。",
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
    work_log_html = _relay_work_log_html(response)
    assert "继续按计划模式走" in conversation_html
    assert "这是 provider 原始正文，应该完整出现在主对话。" in conversation_html
    assert 'data-native-kind="role_artifact_invalid"' not in conversation_html
    assert "结构化结果缺少必填字段" not in conversation_html
    assert "missing required fields: status" not in conversation_html
    assert "结构化产物未采用，自动流转暂停" in work_log_html
    assert "missing required fields: status" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_projects_running_followup_as_marvis_waiting(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Running follow-up relay task",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_task_status(task.id, "running")
    service._store.update_role_status(task.id, "director", "streaming")
    service._store.update_role_status(task.id, "implementer", "idle")
    service._store.update_role_status(task.id, "auditor", "idle")
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "text": "接力暂停在开发工程师，可以接着继续下去吗",
            "target_role": "director",
            "context_packet_id": 42,
        },
        summary="接力暂停在开发工程师，可以接着继续下去吗",
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
    assert "接力暂停在开发工程师，可以接着继续下去吗" in conversation_html
    assert 'class="marvis-relay-user-message"' in conversation_html
    assert 'data-native-kind="waiting"' in conversation_html
    assert 'data-marvis-followup-waiting="true"' in conversation_html
    assert "..." in conversation_html
    assert "接力暂停在开发工程师，详情见工作日志。" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_shows_plan_waiting_confirmation_card(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Plan control",
        prompt="Plan first",
        workspace="/repo",
        provider="claude",
        execution_mode="plan_first",
    )
    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "passed",
                "reason": "plan first",
                "role": "director",
                "artifact_type": "routing_decision",
                "handoff_to": "",
                "summary": "Plan before implementation.",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "plan",
                "complexity": "medium",
                "risk": "medium",
                "route": "core_relay",
                "required_roles": ["director", "architect", "implementer"],
                "acceptance_criteria": ["approved plan"],
                "stop_conditions": [],
                "requires_user_approval": False,
            }
        ),
        dispatch_next=False,
    )
    await service.handle_role_output(
        task.id,
        "architect",
        json.dumps(
            {
                "status": "waiting",
                "reason": "needs approval",
                "role": "architect",
                "artifact_type": "architecture_plan",
                "handoff_to": "",
                "summary": "Use Plan A.",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "approve plan",
            }
        ),
        dispatch_next=False,
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

    assert 'data-marvis-confirmation-card' in response
    assert 'data-marvis-confirmation-page' in response
    assert "Relay 澄清确认" in response
    assert "计划等待确认" in response
    assert "Use Plan A." in response
    assert "执行计划" in response
    assert "补充内容" in response
    assert "停止" in response
    assert 'class="marvis-relay-plan-control"' not in response
    assert "/rounds/${encodeURIComponent(roundId)}/control" in response


@pytest.mark.asyncio
async def test_relay_task_detail_shows_generic_waiting_confirmation_card_with_options(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Waiting confirmation",
        prompt="Need explicit confirmation",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "waiting",
                "reason": "needs user input",
                "role": "director",
                "artifact_type": "routing_decision",
                "handoff_to": "",
                "summary": "Need confirmation before work.",
                "evidence_refs": [],
                "open_questions": ["Confirm output path and style?"],
                "confirmation_options": [
                    {
                        "id": "minimal",
                        "label": "简约风格",
                        "summary": "更接近原生 Codex。",
                        "instruction": "采用简约、克制、手机原生风格。",
                    },
                    {
                        "id": "cyber",
                        "label": "赛博风格",
                        "summary": "更强视觉冲击。",
                        "instruction": "采用赛博风格，但仍保持可读。",
                    },
                ],
                "next_action": "wait for confirmation",
                "complexity": "standard",
                "risk": "medium",
                "route": "waiting_user",
                "required_roles": ["director"],
                "acceptance_criteria": ["user confirmed path"],
                "stop_conditions": [],
                "requires_user_approval": True,
            }
        ),
        dispatch_next=False,
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

    control_match = re.search(
        r'<section class="marvis-relay-confirmation-card"(?P<html>.*?)</section>',
        response,
        re.S,
    )
    assert control_match is not None
    control_html = control_match.group("html")
    assert "等待确认" in control_html
    assert "Relay 澄清确认" in control_html
    assert "计划等待确认" not in control_html
    assert "简约风格" in control_html
    assert "赛博风格" in control_html
    assert "选择执行" in response
    assert "补充内容" in response
    assert "停止" in control_html
    assert 'data-confirmation-option-id="minimal"' in response
    assert 'data-plan-decision="continue"' in response
    assert "data-waiting-input" in control_html
    assert "说明你的想法或修改要求" in response
    assert "selected_option_id" in response
    assert 'data-marvis-confirmation-page' in response
    assert 'class="marvis-relay-plan-control"' not in response
    assert "/rounds/${encodeURIComponent(roundId)}/control" in response
    assert "confirmation_source" in response
    assert "function renderMarvisWorkLogConfirmation" in response
    assert "renderMarvisWorkLogConfirmation(payload);" in response
    assert "Relay 澄清确认" in response


@pytest.mark.asyncio
async def test_relay_task_detail_shows_native_approval_confirmation_source(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Native approval",
        prompt="Run tests",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "director", "waiting")
    service._store.lifecycle.set_round_execution(
        task.id,
        1,
        execution_mode="plan_first",
        execution_goal="",
        execution_strategy={},
        waiting_reason="provider_approval",
    )
    service._store.lifecycle.set_round_confirmation(
        task.id,
        1,
        source="provider_native_approval",
        kind="command_approval",
        role="director",
        provider="codex",
        provider_request_id="req-native-1",
        runtime_event_id=44,
        native_session_id="codex-thread-1",
        agent_run_id=701,
        turn_id="turn-codex-1",
    )
    service._store.update_task_status(task.id, "waiting_user")

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

    assert '<section class="marvis-relay-confirmation-card"' in response
    assert "Codex 原生确认" in response
    assert "请求类型：command_approval" in response
    assert "等待原因：provider_approval" in response
    assert "请求 ID：req-native-1" in response
    assert "Relay 澄清确认" not in response.split(
        '<section class="marvis-relay-confirmation-card"',
        1,
    )[1].split("</section>", 1)[0]
    work_log_html = _relay_work_log_html(response)
    assert "Codex 原生确认" in work_log_html
    assert "请求类型：command_approval" in work_log_html
    assert "请求 ID：req-native-1" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_clears_waiting_confirmation_when_control_advances(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Waiting confirmation",
        prompt="Need explicit confirmation",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "waiting",
                "reason": "needs user input",
                "role": "director",
                "artifact_type": "routing_decision",
                "handoff_to": "",
                "summary": "Need confirmation before work.",
                "evidence_refs": [],
                "open_questions": ["Continue?"],
                "next_action": "wait for confirmation",
                "complexity": "standard",
                "risk": "medium",
                "route": "waiting_user",
                "required_roles": ["director"],
                "acceptance_criteria": ["user confirmed path"],
                "stop_conditions": [],
                "requires_user_approval": True,
            }
        ),
        dispatch_next=False,
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

    click_segment = response.split(
        'const decision = target.getAttribute("data-plan-decision");',
        1,
    )[1].split("} catch", 1)[0]
    assert click_segment.index("hidePlanControlSurface();") < click_segment.index(
        "const response = await fetch"
    )

    assert 'addRelayEventListener("round.control"' in response
    round_control_segment = response.split('addRelayEventListener("round.control"', 1)[
        1
    ].split("});", 1)[0]
    assert "hidePlanControlSurface();" in round_control_segment

    queued_segment = response.split('addRelayEventListener("role.queued"', 1)[1].split(
        "});",
        1,
    )[0]
    assert "hidePlanControlSurface();" in queued_segment


@pytest.mark.asyncio
async def test_relay_task_detail_does_not_revive_confirmation_card_after_blocked(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Blocked after confirmation",
        prompt="Need explicit confirmation",
        workspace="/repo",
        provider="claude",
    )
    await service.handle_role_output(
        task.id,
        "director",
        json.dumps(
            {
                "status": "waiting",
                "reason": "needs user input",
                "role": "director",
                "artifact_type": "routing_decision",
                "handoff_to": "",
                "summary": "Need confirmation before work.",
                "evidence_refs": [],
                "open_questions": ["Continue?"],
                "next_action": "wait for confirmation",
                "complexity": "standard",
                "risk": "medium",
                "route": "waiting_user",
                "required_roles": ["director"],
                "acceptance_criteria": ["user confirmed path"],
                "stop_conditions": [],
                "requires_user_approval": True,
            }
        ),
        dispatch_next=False,
    )
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {"error": "invalid json", "round_id": 1},
        summary="invalid json",
    )
    service._store.update_role_status(task.id, "director", "blocked")
    service._store.update_task_status(task.id, "blocked")

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

    assert '<section class="marvis-relay-confirmation-card"' not in response
    assert 'data-task-status-value="blocked"' in response


@pytest.mark.asyncio
async def test_relay_task_detail_projects_followup_attachments_into_user_bubble(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Follow-up with attachments",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="claude",
        provider_engine="sdk-test",
        native_session_id="native-director-followup",
        agent_run_id=201,
        turn_id="turn-followup",
        active_turn_id="turn-followup",
        turn_running=False,
        dispatch_verified=True,
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="201",
            correlation_id="corr-followup-201",
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=Visibility.USER,
            payload={"text": "看这个截图和日志", "native_turn_id": "turn-followup"},
            occurred_at="2026-06-14T12:00:00+00:00",
            agent_run_id=201,
        )
    )
    service._store.save_artifact(
        task.id,
        "director",
        "relay_board",
        {
            "latest_user_input": (
                "看这个截图和日志\n\n用户附带图片：\n"
                "- screen.png (image/png)\n\n用户附带文件：\n"
                "- trace.log (text/plain, 13 bytes)"
            ),
            "current_dispatch": "director",
            "next_step": "director review latest user input",
        },
        summary="User follow-up routed to director",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "text": "看这个截图和日志",
            "target_role": "director",
            "images": [
                {
                    "filename": "screen.png",
                    "mime_type": "image/png",
                    "url": "data:image/png;base64,aGVsbG8=",
                }
            ],
            "files": [
                {
                    "filename": "trace.log",
                    "mime_type": "text/plain",
                    "text": "line 1\nline 2",
                    "size": 13,
                }
            ],
        },
        summary="看这个截图和日志",
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
    assert conversation_html.count("看这个截图和日志") == 1
    assert 'data-native-kind="user_message"' in conversation_html
    assert 'data-native-key="user_followup:' in conversation_html
    assert "relay_board_followup:" not in conversation_html
    assert 'class="marvis-relay-message-images"' in conversation_html
    assert 'class="marvis-relay-message-image"' in conversation_html
    assert 'src="data:image/png;base64,aGVsbG8="' in conversation_html
    assert "screen.png" not in conversation_html
    assert "trace.log" in conversation_html
    assert 'class="marvis-relay-attachment-list"' in conversation_html
    assert "line 1" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_hides_pseudo_envelope_followup_response(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    assert service is not None
    task = service.create_task(
        prompt="确认 task28 是否能继续对话",
        workspace="/tmp/project-a",
        title="Follow-up display",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {"text": "接续验证：请用一句话说明 task28 现在可以继续对话。"},
        summary="接续验证：请用一句话说明 task28 现在可以继续对话。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "followup_response",
        {
            "text": (
                '{"artifact_type":"final_summary","reason接续验证一句话回复。'
                'roledirectorstatuspassedsummary task28 现在可以继续对话"}'
            ),
            "target_role": "user",
        },
        summary=(
            '{"artifact_type":"final_summary","reason接续验证一句话回复。'
            'roledirectorstatuspassedsummary task28 现在可以继续对话"}'
        ),
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
    assert "接续验证：请用一句话说明 task28 现在可以继续对话。" in conversation_html
    assert "summary task28 现在可以继续对话" not in conversation_html
    assert "总工程师的结构化输出已由系统处理，原始协议内容不在主会话展示。" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_hides_fused_json_followup_response(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    assert service is not None
    task = service.create_task(
        prompt="确认 task28 是否能继续对话",
        workspace="/tmp/project-a",
        title="Follow-up display",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {"text": "部署后接续验证：请只回复“接续已修复”。"},
        summary="部署后接续验证：请只回复“接续已修复”。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "followup_response",
        {
            "text": (
                '{"artifact_type":"final_summary","evidence_refs":[],'
                '"handoff_to":"","next_actionopen_questionsreason接续验证。'
                'roledirectorstatuspassedsummary已修复"}'
            ),
            "target_role": "user",
        },
        summary="已修复",
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
    assert "部署后接续验证" in conversation_html
    assert "summary已修复" not in conversation_html
    assert "总工程师的结构化输出已由系统处理，原始协议内容不在主会话展示。" not in conversation_html
    assert "next_actionopen_questionsreason" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_hides_prefixed_spaced_protocol_followup_response(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        prompt="确认接续是否还会泄漏结构化协议",
        workspace="/tmp/project-a",
        title="Spaced protocol follow-up display",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {"text": "请继续，只展示自然语言。"},
        summary="请继续，只展示自然语言。",
    )
    leaked_protocol = (
        '模型先说了一句前缀\n{"artifact_type" : "final_summary", '
        '"role" : "director", "status" : "passed", "summary" : "不该显示", '
        '"handoff_to" : "", "evidence_refs" : [], "open_questions" : [], '
        '"next_action" : "", "reason" : "协议内容"}'
    )
    service._store.save_artifact(
        task.id,
        "director",
        "followup_response",
        {
            "text": leaked_protocol,
            "target_role": "user",
        },
        summary=leaked_protocol,
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
    assert "请继续，只展示自然语言。" in conversation_html
    assert "模型先说了一句前缀" not in conversation_html
    assert "不该显示" not in conversation_html
    assert "artifact_type" not in conversation_html
    assert "handoff_to" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_projects_direct_final_summary_as_plain_closure(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        prompt="请只用两句话确认收到。",
        workspace="/tmp/project-a",
        title="Direct final summary",
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_task_status(task.id, "completed")
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "artifact_type": "final_summary",
            "role": "director",
            "status": "passed",
            "summary": "我已收到这个交互采样任务。对话区会保持简洁。",
            "reason": "按要求仅确认收到并保持简洁，无需工具或产出物。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="我已收到这个交互采样任务。对话区会保持简洁。",
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
    assert "我已收到这个交互采样任务。对话区会保持简洁。" in conversation_html
    assert "结论：" not in conversation_html
    assert "下一步：" not in conversation_html
    assert "详情见结构化数据" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_keeps_current_round_direct_final_after_old_role_process(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        prompt="第一轮完成后，第二轮直接收口。",
        workspace="/tmp/project-a",
        title="Current round direct final",
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.update_task_status(task.id, "completed")
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "artifact_type": "implementation_report",
            "role": "implementer",
            "round_id": 1,
            "status": "passed",
            "summary": "第一轮实现已经完成。",
            "handoff_to": "director",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回总工程师收尾。",
        },
        summary="第一轮实现已经完成。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "artifact_type": "final_summary",
            "role": "director",
            "round_id": 2,
            "status": "passed",
            "summary": "第二轮已直接完成，并保持对话区简洁。",
            "reason": "第二轮无需继续接力。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="第二轮已直接完成，并保持对话区简洁。",
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
    assert "第二轮已直接完成，并保持对话区简洁。" in conversation_html
    assert "第一轮实现已经完成。" not in conversation_html
    assert "结论：" not in conversation_html
    assert "详情见结构化数据" not in conversation_html


def test_relay_projected_rows_prunes_direct_final_summary_by_round() -> None:
    from wlcodex.live_stream.server import _relay_projected_conversation_rows

    rows = _relay_projected_conversation_rows(
        [],
        hub=None,
        artifacts=[
            {
                "id": 1,
                "artifact_type": "implementation_report",
                "role": "implementer",
                "relay_role": "implementer",
                "round_id": 1,
                "status": "passed",
                "reason": "第一轮实现完成。",
                "summary": "第一轮实现已经完成。",
                "handoff_to": "director",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "交回总工程师收尾。",
            },
            {
                "id": 2,
                "artifact_type": "final_summary",
                "role": "director",
                "relay_role": "director",
                "round_id": 2,
                "status": "passed",
                "reason": "第二轮无需继续接力。",
                "summary": "第二轮已直接完成，并保持对话区简洁。",
                "handoff_to": "",
                "evidence_refs": [],
                "open_questions": [],
                "next_action": "",
            },
        ],
    )

    assert any(
        row.get("key") == "final_summary_response:2"
        and row.get("body") == "第二轮已直接完成，并保持对话区简洁。"
        for row in rows
    )


@pytest.mark.asyncio
async def test_relay_task_detail_keeps_natural_reply_with_json_snippet_in_chat(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        prompt="解释这段 JSON。",
        workspace="/tmp/project-a",
        title="Natural JSON reply",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-json-snippet",
        agent_run_id=611,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    _append_runtime_event(
        runtime_store,
        agent_run_id=611,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": (
                '可以，正文里的 JSON 示例是 {"artifact_type":"note","summary":"只是示例"}，'
                "这里不是接力结构化产物。"
            ),
            "native_turn_id": "turn-json-snippet",
            "itemId": "assistant-json-snippet",
        },
        occurred_at="2026-06-14T11:45:01+00:00",
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
    assert "正文里的 JSON 示例" in conversation_html
    assert "artifact_type" in conversation_html
    assert "只是示例" in conversation_html
    assert "这里不是接力结构化产物。" in conversation_html
    assert 'data-native-kind="role_envelope"' not in conversation_html


@pytest.mark.asyncio
async def test_native_index_shows_relay_card_and_preserves_token(tmp_path: Path) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native?token=secret HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "Codex" in response
    assert "Claude" in response
    assert "Antigravity" in response
    assert "议会审核" in response
    assert "Marvis 接力" in response
    assert 'href="/native/workflows/relay?token=secret"' in response
    assert 'data-native-entry="marvis-relay"' in response
    assert "<span>工作流</span>" not in response


@pytest.mark.asyncio
async def test_workflow_directory_links_to_relay_council_and_dev_flow(tmp_path: Path) -> None:
    response, _service = await _request(
        tmp_path,
        "GET /native/workflows?token=secret HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
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
    assert (
        '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260629-confirmation-provenance">'
        in response
    )
    assert 'class="marvis-relay-bottom-nav"' in response
    assert 'class="marvis-relay-avatar marvis-relay-avatar-marvis"' in response
    assert 'href="/native/workflows/relay/office?token=secret"' in response
    assert (
        'href="/native/workflows/relay/chat?token=secret&amp;workspace=/Users/wl/projects/wlcodex"'
        in response
    )
    assert (
        'href="/native/workflows/relay?token=secret&amp;workspace=/Users/wl/projects/wlcodex"'
        in response
    )
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
    assert "relay-status-badge" in response
    assert 'class="relay-open relay-card-open"' in response
    assert "总工程师：" not in response
    assert "等待总工程师接收" not in response
    assert "打开任务" in response
    assert "open task" not in response
    assert f"/native/workflows/relay/tasks/{task.id}?token=secret" in response
    assert f"/native/workflows/relay/tasks/{other.id}?token=secret" not in response

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
    assert "relay-status-badge" in populated
    assert 'class="relay-role-chips"' not in populated
    assert "总工程师 · 阻塞 · Codex" not in populated
    assert "架构工程师 · 未调度 · Antigravity" not in populated
    assert "开发工程师 · 未调度 · Claude" not in populated
    assert 'class="relay-open relay-card-open"' in populated
    assert "总工程师：" not in populated
    assert "等待总工程师接收" not in populated
    assert "打开任务" in populated
    assert "open task" not in populated
    assert f"/native/workflows/relay/tasks/{repo_task.id}?token=secret" in populated
    assert f"/native/workflows/relay/tasks/{other.id}?token=secret" not in populated

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
        "UPDATE team_artifacts SET created_at = ? WHERE team_run_id = ?",
        ("2026-06-16T07:51:57.510982123+00:00", repo_task.id),
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
    assert polished.index("relay-status-badge") < polished.index(
        'class="relay-open relay-card-open"'
    )
    assert 'class="relay-card-topline"' not in polished
    assert 'class="relay-card-identity"' in polished
    assert 'class="relay-card-project-pill">repo</span>' in polished
    assert 'class="relay-card-activity">最近活动 06-16 15:51</span>' in polished
    assert polished.index('class="relay-card-avatar-row"') < polished.index(
        'class="relay-card-project-pill"'
    )
    assert polished.index('class="relay-card-identity"') < polished.index('class="relay-title"')
    assert polished.index("relay-status-badge") < polished.index('class="relay-title"')
    assert "当前阶段：" not in polished
    assert "/repo ·" not in polished
    assert "is-completed" in polished
    css_response, _ = await _request(
        tmp_path,
        "GET /static/relay_marvis.css HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        relay_service=service,
    )
    footer_start = css_response.index(".marvis-relay-task-card-footer")
    footer_block = css_response[footer_start : footer_start + 260]
    assert "padding-left" not in footer_block
    assert "最近活动 06-16 15:51" in polished
    assert "2026-06-16T07:51:57.510982123+00:00" not in polished
    assert "最近接棒：" not in polished
    assert "按完整五角色接力处理" not in polished


@pytest.mark.asyncio
async def test_relay_task_list_paginates_ten_tasks_per_page(tmp_path: Path) -> None:
    server, service, _runtime_store = _server(tmp_path)
    workspace = "/Users/wl/projects/wlcodex"
    tasks = [
        service.create_task(
            title=f"Paged relay task {index:02d}",
            prompt="Prompt",
            workspace=workspace,
            provider="claude",
        )
        for index in range(12)
    ]
    for index, task in enumerate(tasks):
        service._store._ledger._conn.execute(
            "UPDATE team_runs SET updated_at = ? WHERE id = ?",
            (f"2026-06-{index + 1:02d}T00:00:00+00:00", task.id),
        )
    service._store._ledger._conn.commit()

    await server.start()
    try:
        page_one = await _read_response(
            server.host,
            server.port,
            "GET /native/workflows/relay?token=secret&workspace=%2FUsers%2Fwl%2Fprojects%2Fwlcodex HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
        page_two = await _read_response(
            server.host,
            server.port,
            "GET /native/workflows/relay?token=secret&workspace=%2FUsers%2Fwl%2Fprojects%2Fwlcodex&page=2 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert page_one.count('class="relay-task-card marvis-relay-task-card"') == 10
    assert "Paged relay task 11" in page_one
    assert "Paged relay task 02" in page_one
    assert "Paged relay task 01" not in page_one
    assert "Paged relay task 00" not in page_one
    assert 'class="relay-pagination"' in page_one
    assert "第 1 / 2 页" in page_one
    assert (
        'href="/native/workflows/relay?token=secret&amp;workspace=/Users/wl/projects/wlcodex&amp;page=2"'
        in page_one
    )
    assert ".relay-page-link { color: #315e8d; background: #e8f1fb;" in page_one
    assert ".relay-page-disabled { color: #6f7782; background: #f0f2f5;" in page_one

    assert page_two.count('class="relay-task-card marvis-relay-task-card"') == 2
    assert "Paged relay task 01" in page_two
    assert "Paged relay task 00" in page_two
    assert "Paged relay task 02" not in page_two
    assert "第 2 / 2 页" in page_two
    assert (
        'href="/native/workflows/relay?token=secret&amp;workspace=/Users/wl/projects/wlcodex&amp;page=1"'
        in page_two
    )


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
    assert (
        'class="marvis-relay-avatar marvis-relay-avatar-marvis marvis-relay-hero-avatar"'
        in response
    )
    assert "你好，今天想做什么？" in response
    assert "GitHub热门项目收集" not in response
    assert "曼谷旅行路线书网页" not in response
    assert "同花顺股票信息查询" not in response
    assert "个人信息文件归档" not in response
    assert 'class="marvis-relay-suggestion"' not in response
    assert 'class="marvis-relay-composer"' in response
    assert 'action="/api/relay/tasks?token=secret"' in response
    assert 'class="marvis-relay-mode-strip"' in response
    assert 'name="execution_mode" value="simple" checked' in response
    assert 'name="execution_mode" value="plan_first"' in response
    assert 'name="execution_mode" value="goal"' in response
    assert 'name="execution_mode" value="auto"' in response
    assert 'name="execution_mode" value="team"' not in response
    assert 'type="hidden" name="allow_subagents" value="off"' in response
    assert 'type="checkbox" name="allow_subagents" value="auto" checked' in response
    assert "使用子代理" in response
    assert "子代理关闭" not in response
    assert 'select name="team_strategy"' not in response
    assert "团队策略" not in response
    assert '<input name="title" autocomplete="off" placeholder="请在此输入任务">' in response
    assert '<input type="hidden" name="prompt" value="">' in response
    assert '<input type="hidden" name="execution_goal" value="">' in response
    assert '<input type="hidden" name="workspace" value="/repo">' in response
    assert 'data-marvis-nav="chat" aria-current="page"' in response
    assert 'href="/native/workflows/relay?token=secret&amp;workspace=/repo"' in response
    assert "新接力任务" not in response
    assert "relay-create-modal" not in response


@pytest.mark.asyncio
async def test_relay_work_log_shows_subagent_dispatch_decision(tmp_path: Path) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Plan with helpers",
        prompt="Plan with helpers",
        workspace="/repo",
        provider="claude",
        execution_mode="plan_first",
        allow_subagents="auto",
    )
    service._store.update_role_metadata(
        task.id,
        "architect",
        provider="claude",
        provider_engine="sdk-test",
        native_session_id="native-architect-1",
        dispatch_verified=True,
        provider_mode={
            "execution_mode": "plan_first",
            "team_strategy": "none",
            "allow_subagents": "auto",
            "provider_mode": "claude_plan",
            "fallback": False,
            "subagent_decision_json": {
                "provider": "claude",
                "allowed": True,
                "capability": "builtin_subagents",
                "reason": "子代理由当前角色按任务需要自行判断；Relay 不暴露手工子代理用途。",
            },
        },
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
    assert "Claude plan" in work_log_html
    assert "子代理自动" in work_log_html
    assert "builtin_subagents" in work_log_html
    assert "provider_team_topology" not in work_log_html


@pytest.mark.asyncio
async def test_marvis_relay_skills_and_profile_show_construction_pages(
    tmp_path: Path,
) -> None:
    skills_response, _ = await _request(
        tmp_path,
        "GET /native/workflows/relay/skills?token=secret&workspace=%2Frepo HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )
    profile_response, _ = await _request(
        tmp_path,
        "GET /native/workflows/relay/profile?token=secret&workspace=%2Frepo HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )

    for response, active_nav in ((skills_response, "skills"), (profile_response, "profile")):
        assert "HTTP/1.1 200 OK" in response
        assert 'data-marvis-relay-view="construction"' in response
        assert "正在建设中" in response
        assert "/static/marvis/relay-under-construction.svg" in response
        assert f'data-marvis-nav="{active_nav}" aria-current="page"' in response
        assert 'href="/native/workflows/relay/skills?token=secret&amp;workspace=/repo"' in response
        assert 'href="/native/workflows/relay/profile?token=secret&amp;workspace=/repo"' in response

    asset_response, _ = await _request(
        tmp_path,
        "GET /static/marvis/relay-under-construction.svg HTTP/1.1\r\n"
        "Host: test\r\nConnection: close\r\n\r\n",
    )
    assert "HTTP/1.1 200 OK" in asset_response
    assert "Content-Type: image/svg+xml" in asset_response
    assert "<svg" in asset_response


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
    assert (
        '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260629-confirmation-provenance">'
        in response
    )
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
    assert (
        '"director":{"role":"director","display_name":"总工程师","title":"总工程师","provider"'
        in response
    )
    assert (
        '"architect":{"role":"architect","display_name":"架构工程师","title":"架构工程师","provider"'
        in response
    )
    assert (
        '"implementer":{"role":"implementer","display_name":"开发工程师","title":"开发工程师","provider"'
        in response
    )
    assert (
        '"tester":{"role":"tester","display_name":"测试工程师","title":"测试工程师","provider"'
        in response
    )
    assert (
        '"auditor":{"role":"auditor","display_name":"审核工程师","title":"审核工程师","provider"'
        in response
    )
    assert re.search(r'"director":\{[^}]*"display_name":"总工程师"[^}]*"avatar":"marvis"', response)
    assert re.search(
        r'"architect":\{[^}]*"display_name":"架构工程师"[^}]*"avatar":"architect"', response
    )
    assert re.search(
        r'"implementer":\{[^}]*"display_name":"开发工程师"[^}]*"avatar":"implementer"', response
    )
    assert re.search(r'"tester":\{[^}]*"display_name":"测试工程师"[^}]*"avatar":"tester"', response)
    assert re.search(
        r'"auditor":\{[^}]*"display_name":"审核工程师"[^}]*"avatar":"auditor"', response
    )
    assert "data-persona-name" in response
    assert "Team Leader" not in response
    for legacy_role_name in [
        " ".join(("Computer", "Agent")),
        " ".join(("File", "Agent")),
        " ".join(("Browser", "Agent")),
        " ".join(("Search", "Agent")),
    ]:
        assert legacy_role_name not in response
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
    assert "☕" in response
    assert "marvis-token-beans" not in response
    legacy_avatar_slugs = [
        "-".join(("app", "agent")),
        "-".join(("computer", "agent")),
        "-".join(("search", "agent")),
        "-".join(("file", "agent")),
        "-".join(("browser", "agent")),
    ]
    for legacy_avatar_slug in legacy_avatar_slugs:
        assert legacy_avatar_slug not in response
    avatar_assets = [
        "persona-avatar-marvis.png",
        "persona-avatar-architect.png",
        "persona-avatar-implementer.png",
        "persona-avatar-tester.png",
        "persona-avatar-auditor.png",
    ]
    css = Path("wlcodex/live_stream/static/relay_marvis.css").read_text()
    for legacy_avatar_slug in legacy_avatar_slugs:
        assert legacy_avatar_slug not in css
    for asset in avatar_assets:
        assert Path("wlcodex/live_stream/static/marvis", asset).exists()
        assert f"/static/marvis/{asset}" in css


def test_marvis_relay_task_topbar_is_fixed_above_scroll_content() -> None:
    css = Path("wlcodex/live_stream/static/relay_marvis.css").read_text()

    assert "body[data-marvis-relay-view] .marvis-relay-topbar {\n  position: fixed;" in css
    assert "--marvis-s25-title-size: 22px;" in css
    assert "--marvis-s25-top-icon-stroke: 2.4px;" in css
    assert "--marvis-s25-composer-bottom: 84px;" in css
    assert "--marvis-s25-bottom-nav-height: 78px;" in css
    assert "body[data-marvis-relay-view] .marvis-relay-menu.is-back::after {" in css
    assert ":placeholder-shown) .marvis-relay-submit:not(.is-stop)" not in css
    assert (
        "body[data-marvis-relay-view] .marvis-relay-agent-bubble {\n  grid-column: 1 / -1;" in css
    )
    assert "body[data-marvis-relay-view] .marvis-relay-composer-image-remove::before" in css
    assert "body[data-marvis-relay-view] .marvis-relay-composer-image-remove::after" in css
    assert "body[data-marvis-relay-view] .relay-message-body {\n  grid-column: 1 / -1;" in css
    assert (
        "body[data-marvis-relay-view] .marvis-relay-task-main {\n"
        "  padding: var(--marvis-s25-task-main-top) var(--marvis-s25-task-main-x) var(--marvis-s25-task-main-bottom);"
    ) in css


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
    assert 'type="button" class="marvis-office-token-card"' in response
    assert 'data-marvis-token-details-open="today"' in response
    assert 'data-marvis-token-details-open="total"' in response
    assert "data-marvis-token-details-modal" in response
    assert "Token明细" in response
    assert "Codex" in response
    assert "Claude" in response
    assert "今日 1,500" in response
    assert "总计 2,300" in response
    assert "今日 700" in response
    assert "2,200" in response
    assert "3,000" in response
    assert "data-token-local" not in response
    assert "data-token-saved" not in response
    assert "今日节省Token" not in response
    assert '"consumed_tokens": 2200' in stats_response
    assert '"total_consumed_tokens": 3000' in stats_response
    assert '"agent": "codex"' in stats_response
    assert '"today_tokens": 1500' in stats_response
    assert '"total_tokens": 2300' in stats_response
    assert '"agent": "claude"' in stats_response
    assert '"today_tokens": 700' in stats_response
    assert "local_tokens" not in stats_response
    assert "saved_tokens" not in stats_response


def test_marvis_relay_office_token_details_sheet_uses_fixed_modal_css() -> None:
    css = Path("wlcodex/live_stream/static/relay_marvis.css").read_text()
    selector_start = css.index("[data-marvis-token-details-modal]")
    selector_block = css[selector_start : selector_start + 360]
    assert "position: fixed" in selector_block
    assert "bottom: 0" in selector_block
    assert "z-index: 43" in selector_block
    assert "var(--marvis-s25-phone-width)" in selector_block
    assert "var(--marvis-relay-phone-width)" not in selector_block


@pytest.mark.asyncio
async def test_marvis_relay_office_token_usage_includes_runtime_usage_by_agent_run(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Runtime usage relay",
        prompt="count runtime tokens",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        provider_engine="app-server",
        model="gpt-5",
        native_session_id="native-director",
        agent_run_id=801,
        dispatch_verified=True,
    )
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="claude",
        provider_engine="sdk-deepseek",
        model="claude",
        native_session_id="native-implementer",
        agent_run_id=802,
        dispatch_verified=True,
    )
    now = datetime.now(timezone.utc).isoformat()
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_USAGE_UPDATED,
        payload={
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "provider": "codex",
            "native_turn_id": "turn-director",
        },
        occurred_at=now,
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_USAGE_UPDATED,
        payload={
            "input_tokens": 200,
            "output_tokens": 20,
            "total_tokens": 220,
            "provider": "codex",
            "native_turn_id": "turn-director",
        },
        occurred_at=now,
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=802,
        event_type=EventType.MODEL_USAGE_UPDATED,
        payload={
            "usage": {
                "input_tokens": 50,
                "cache_read_input_tokens": 400,
                "output_tokens": 30,
            },
            "provider": "claude",
            "native_turn_id": "turn-implementer",
        },
        occurred_at=now,
    )

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
        task_response = await _read_response(
            server.host,
            server.port,
            f"GET /native/workflows/relay/tasks/{task.id}?token=secret HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert 'data-token-consumed="700"' in response
    assert 'data-token-total="700"' in response
    assert "Codex" in response
    assert "Claude" in response
    assert "今日 220" in response
    assert "今日 480" in response
    assert "缓存 400" in response
    assert '"consumed_tokens": 700' in stats_response
    assert '"total_consumed_tokens": 700' in stats_response
    assert '"agent": "codex"' in stats_response
    assert '"today_tokens": 220' in stats_response
    assert '"agent": "claude"' in stats_response
    assert '"today_tokens": 480' in stats_response
    assert '"cached_input_tokens": 400' in stats_response
    assert 'data-token-total="700"' in task_response
    assert ">700 ☕<" in task_response


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
            {"role": "auditor", "display_name": "审核工程师"},
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
    assert "审核工程师" in response
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
    assert (
        'const RELAY_HISTORY_HREF = "/native/workflows/relay?token=secret&workspace=/repo";'
        in response
    )
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
            "text": "让审核工程师确认一下",
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
    assert (
        '<link rel="stylesheet" href="/static/relay_marvis.css?v=20260629-confirmation-provenance">'
        in response
    )
    assert 'class="marvis-relay-topbar"' in response
    assert (
        'class="marvis-relay-menu is-back" href="/native/workflows/relay?token=secret&amp;workspace=/repo" aria-label="返回上一级"'
        in response
    )
    assert 'class="marvis-relay-bottom-nav"' in response
    assert 'href="/native/workflows/relay/office?token=secret"' in response
    assert 'data-marvis-open-log aria-label="工作日志"' in response
    assert 'class="marvis-work-log"' in response
    assert "工作日志" in response
    assert "产出物" not in response
    assert 'class="marvis-relay-composer"' in response
    assert "has-image-attachments" in response
    assert "marvis-relay-composer-image-preview" in response
    assert "marvis-relay-message-image" in response
    assert "marvis-relay-composer-image-remove" in response
    assert "remove.textContent" not in response
    assert 'remove.textContent = "<svg' not in response
    assert "请在此输入任务" in response
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
    assert "data-native-conversation-timeline" in response
    assert 'class="relay-native-stack"' not in response
    assert "data-native-session-stack" not in response
    assert 'class="relay-native-stream"' not in response
    assert 'class="relay-native-frame"' not in response
    assert "<iframe" not in response
    conversation_html = _relay_view_panel_html(response, "conversation")
    work_log_html = _relay_work_log_html(response)
    assert "让审核工程师确认一下" in conversation_html
    assert "我会先确认风险。" in conversation_html
    assert "架构侧继续补齐影响面。" in conversation_html
    assert "结论：本任务判定无需派发，由总工程师直接完成。" not in conversation_html
    assert 'data-native-role="director"' in response
    assert 'data-native-role="architect"' in response
    assert 'data-view-panel="conversation"' in response
    assert 'data-view-panel="board"' not in response
    assert "任务进度" not in response.split("<script", 1)[0]
    assert "调度决策" not in response.split("<script", 1)[0]
    assert "总工程师直接完成" in work_log_html
    assert "验收依据" in work_log_html
    assert "给出清晰说明" in work_log_html
    assert "下一步" in work_log_html
    assert "RelayBoard" not in response
    assert "current goal" not in response
    assert "latest user input" not in response
    assert "native_session_id:" not in response
    assert "provider/model:" not in response
    assert "open native session" not in response
    assert ">interrupt<" not in response
    assert ">send<" not in response
    for display_name in ["Marvis", "架构工程师"]:
        assert display_name in response
    assert 'data-marvis-work-log-role="director"' in work_log_html
    assert 'data-marvis-work-log-role="architect"' in work_log_html
    assert "继续补充给总工程师" in response
    assert "发送补充" in response
    assert "中断任务" not in response.split("<script", 1)[0]
    assert "打开原生会话" not in response.split("<script", 1)[0]
    assert "native_thread_id=native-director-1" not in response
    assert "/sessions/native-director-1" not in response
    assert "结构化结果不是合法 JSON，系统无法直接收口。" in work_log_html
    assert "等待总工程师接收并形成决策摘要" not in response
    assert f"/api/relay/tasks/{task.id}/message" in response
    assert 'data-marvis-pending-inputs' in response
    assert "/inputs${TOKEN_SUFFIX}" in response
    assert "已排队，当前 round 结束后自动开始" in response
    assert "已引导当前，等待当前角色接收" in response
    assert 'isSteered ? " is-steered" : ""' in response
    assert 'hasError ? " is-error" : ""' in response
    assert 'const actions = isSteered ? ""' in response
    assert "appendMarvisConversationGuidance(payload);" in response
    assert 'const responseDisposition = String(payload.disposition || "pending");' in response
    assert 'addRelayEventListener("user.input_queued"' in response
    assert 'addRelayEventListener("user.input_consumed"' in response
    assert "data-marvis-followup-composer" in response
    assert 'followupComposer?.addEventListener("submit"' in response
    assert 'document.querySelector(".relay-composer")?.addEventListener("submit"' not in response
    assert "relay-board-grid" not in response.split("<script", 1)[0]
    assert "relay-progress" not in response.split("<script", 1)[0]
    assert "relay-activity-log" not in response.split("<script", 1)[0]
    assert 'const EVENTS_SUFFIX = "?token=secret&after=3";' in response
    assert "function normalizeRelayPayload(raw)" in response
    assert "const payload = parseRelayEvent(event);" in response
    assert "function renderRelayNativeEvent" in response
    assert "const TERMINAL_ROLE_STATUSES = new Set" in response
    assert "TERMINAL_ROLE_STATUSES.has(currentStatus)" in response
    assert "function clearMarvisConversationPausedRows()" in response
    assert "setRoleStatus(role, status, options = {})" in response
    assert "options.force" in response
    assert 'reason === "new_followup_turn"' in response
    assert "function relayTaskIsRunning()" in response
    assert "!relayTaskIsRunning()) return;" in response
    assert 'addRelayEventListener("role.native_event"' in response
    assert 'document.querySelectorAll("[data-native-key]")' in response
    assert "nativeTranscriptNodes.set(node.dataset.nativeKey" in response
    assert "events${relayEventsSuffix()}" in response
    assert "function connectRelayEventSource()" in response
    assert "function closeRelayEventSource()" in response
    assert "function scheduleRelayEventsReconnect()" in response
    assert 'window.addEventListener("pagehide", closeRelayEventSource);' in response
    assert "updateRelayEventsCursor(event);" in response
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
        payload={
            "input_tokens": 45907,
            "output_tokens": 1308,
            "cached_input_tokens": 5504,
            "reasoning_output_tokens": 1096,
            "total_tokens": 47215,
        },
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
    assert "final_summary" not in work_log_html
    assert "relay_role" not in work_log_html
    assert "evidence_refs" not in work_log_html
    assert "今日国际现货黄金约为" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_projects_marvis_chat_and_work_log_drawer(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    legacy_implementer_name = " ".join(("App", "Agent"))
    legacy_search_name = " ".join(("Search", "Agent"))
    legacy_browser_name = " ".join(("Browser", "Agent"))
    legacy_implementer_slug = "-".join(("app", "agent"))
    legacy_search_slug = "-".join(("search", "agent"))
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
            "summary": f"问题收到了，先派 {legacy_implementer_name} 查看窗口。",
            "route": "core_relay",
            "required_roles": ["director", "implementer"],
            "handoff_to": "implementer",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": f"交给 {legacy_implementer_name} 查看窗口状态。",
        },
        summary=f"问题收到了，先派 {legacy_implementer_name} 查看窗口。",
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
            "summary": f"{legacy_implementer_name} 那边反馈 Marvis 本身不在可操作应用范围内，后面要从办公室入口或应用权限方向继续排查。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "等待用户补充办公室页面信息。",
        },
        summary=f"{legacy_implementer_name} 那边反馈 Marvis 本身不在可操作应用范围内，后面要从办公室入口或应用权限方向继续排查。",
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
            "output": f"{legacy_implementer_name} used {legacy_implementer_slug}; {legacy_search_slug} idle; {legacy_browser_name} checked.",
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
    assert (
        'data-marvis-nav="tasks"'
        not in response.split('document.querySelectorAll("[data-marvis-open-log]"', 1)[-1]
    )
    assert "手机办公室看不到其他小马" in conversation_html
    assert "Marvis" in conversation_html
    assert "任务分配 已完成" in conversation_html
    assert "Marvis 拍了拍 开发工程师 说， 别等了，这就开始" in conversation_html
    assert "开发工程师" in conversation_html
    assert "搞定，有请下一位。" in conversation_html
    assert "开发工程师 那边反馈 Marvis 本身不在可操作应用范围内" not in conversation_html
    assert "已完成汇总。" in conversation_html
    for forbidden_role_name in [
        legacy_implementer_name,
        " ".join(("Computer", "Agent")),
        legacy_search_name,
        " ".join(("File", "Agent")),
        legacy_browser_name,
        legacy_implementer_slug,
        legacy_search_slug,
    ]:
        assert forbidden_role_name not in response
    assert "list windows" not in conversation_html
    assert "tool.call" not in conversation_html
    for forbidden in [
        "relay-board-grid",
        "role-lane",
        "查看结构化数据",
        "打开原生会话",
        "data-role-output",
        'data-view-panel="board"',
        "任务进度",
        "调度决策",
        "你好，今天想做什么？",
        "marvis-relay-hero-avatar",
    ]:
        assert forbidden not in visible_html
    assert "工作日志" in work_log_html
    assert "产出物" not in work_log_html
    assert "/static/marvis/office-desk-worker-" in work_log_html
    assert "/static/marvis/office-desk-empty-slot.png" in work_log_html
    assert 'data-marvis-work-log-role="director"' in work_log_html
    assert 'data-marvis-work-log-role="implementer"' in work_log_html
    assert "12K" in work_log_html
    assert "list windows 已完成" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_projects_saved_handoff_when_native_rows_are_earlier(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Marvis handoff task",
        prompt="修复聊天页输入没有反馈。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-handoff",
        agent_run_id=801,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="claude",
        model="sonnet",
        native_session_id="native-implementer-handoff",
        agent_run_id=802,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.update_role_metadata(
        task.id,
        "auditor",
        provider="codex",
        model="gpt-5",
        native_session_id="native-auditor-handoff",
        agent_run_id=803,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "auditor", "passed")
    service._store.save_handoff_packet(
        task.id,
        from_role="director",
        to_role="implementer",
        packet=HandoffPacket(
            from_role="director",
            to_role="implementer",
            summary="需要开发工程师修复输入反馈。",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="implement",
        ),
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="director",
        to_role="implementer",
        packet=HandoffPacket(
            from_role="director",
            to_role="implementer",
            summary="重复交接事件不应在主会话重复展示。",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="implement",
        ),
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="director",
        to_role="director",
        packet=HandoffPacket(
            from_role="director",
            to_role="director",
            summary="总工程师自交接不应展示。",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="continue",
        ),
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="auditor",
        to_role="implementer",
        packet=HandoffPacket(
            from_role="auditor",
            to_role="implementer",
            summary="审核回炉交接不应伪装成 Marvis 派发标语。",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="rework",
        ),
    )
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "needs implementation",
            "role": "director",
            "artifact_type": "routing_decision",
            "summary": "收到，交给开发工程师处理。",
            "route": "core_relay",
            "required_roles": ["director", "implementer", "auditor"],
            "handoff_to": "implementer",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "implement",
        },
        summary="收到，交给开发工程师处理。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "status": "passed",
            "reason": "implemented",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "修复完成，交给审核。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "audit",
        },
        summary="修复完成，交给审核。",
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="implementer",
        to_role="auditor",
        packet=HandoffPacket(
            from_role="implementer",
            to_role="auditor",
            summary="开发完成后交给审核工程师复核。",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="audit",
        ),
    )
    service._store.save_artifact(
        task.id,
        "auditor",
        "audit_report",
        {
            "relay_role": "auditor",
            "status": "passed",
            "reason": "审核通过。",
            "role": "auditor",
            "artifact_type": "audit_report",
            "summary": "审核通过。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回总工程师收尾。",
        },
        summary="审核通过。",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=802,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={"text": "修复完成，交给审核。"},
        occurred_at="2026-06-14T12:09:59+00:00",
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
    assert conversation_html.count("Marvis 拍了拍 开发工程师 说， 别等了，这就开始") == 1
    assert "开发工程师交给审核工程师复核" in conversation_html
    assert "审核工程师退回开发工程师继续处理" in conversation_html
    assert "Marvis 拍了拍 审核工程师 说， 别等了，这就开始" not in conversation_html
    assert "Marvis 拍了拍 Marvis 说， 别等了，这就开始" not in conversation_html
    assert conversation_html.index("Marvis 拍了拍 开发工程师") < conversation_html.index(
        "修复完成，交给审核。"
    )


@pytest.mark.asyncio
async def test_relay_task_detail_renders_auditor_return_to_director_handoff(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Auditor final handoff task",
        prompt="修复接力提示。",
        workspace="/repo",
        provider="codex",
    )
    for role in ("director", "implementer", "auditor"):
        service._store.update_role_metadata(
            task.id,
            role,
            provider="codex",
            model="gpt-5",
            native_session_id=f"native-{role}-final-handoff",
            agent_run_id=970 + len(role),
            dispatch_verified=True,
        )
        service._store.update_role_status(task.id, role, "passed")
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "需要开发和审核",
            "role": "director",
            "artifact_type": "routing_decision",
            "summary": "交给开发工程师处理。",
            "route": "core_relay",
            "required_roles": ["director", "implementer", "auditor"],
            "handoff_to": "implementer",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "开发处理",
        },
        summary="交给开发工程师处理。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "status": "passed",
            "reason": "已修复提示。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "已修复提示。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="已修复提示。",
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="implementer",
        to_role="auditor",
        packet=HandoffPacket(
            from_role="implementer",
            to_role="auditor",
            summary="开发完成后交给审核工程师复核。",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="audit",
        ),
    )
    service._store.save_artifact(
        task.id,
        "auditor",
        "audit_report",
        {
            "relay_role": "auditor",
            "status": "passed",
            "reason": "审核通过。",
            "role": "auditor",
            "artifact_type": "audit_report",
            "summary": "审核通过。",
            "handoff_to": "director",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回总工程师收尾。",
        },
        summary="审核通过。",
    )
    service._store.save_handoff_packet(
        task.id,
        from_role="auditor",
        to_role="director",
        packet=HandoffPacket(
            from_role="auditor",
            to_role="director",
            summary="审核通过，交回总工程师收尾。",
            confirmed_facts=[],
            open_questions=[],
            evidence_refs=[],
            next_action="summarize",
        ),
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "completed",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "已修复接力提示丢失问题。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="已修复接力提示丢失问题。",
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
    assert "开发工程师交给审核工程师复核" in conversation_html
    assert "审核工程师交回Marvis收尾" in conversation_html
    assert "已修复接力提示丢失问题。" not in conversation_html


@pytest.mark.asyncio
async def test_relay_task_detail_does_not_show_stale_round_final_summary_as_assignment_waiting(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Stale waiting final summary task",
        prompt="第一轮问题。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "director", "interrupted")
    service._store.update_task_status(task.id, "interrupted")
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "round_id": 1,
            "status": "waiting",
            "reason": "旧轮等待用户验收",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "旧轮等待用户验收。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "等待用户确认",
        },
        summary="旧轮等待用户验收。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "round_id": 2,
            "text": "第二轮继续修复。",
            "target_role": "director",
            "context_packet_id": 1,
        },
        summary="第二轮继续修复。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "followup_response",
        {
            "round_id": 2,
            "text": "第二轮已经处理完成。",
            "target_role": "user",
            "status": "passed",
        },
        summary="第二轮已经处理完成。",
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
    assert "旧轮等待用户验收。" not in conversation_html
    assert "第二轮已经处理完成。" in conversation_html
    assert "任务分配 等待" not in conversation_html
    assert "任务分配 已中断" not in conversation_html
    assert 'const CURRENT_ROUND_ID = "2";' in response
    assert "let activeRelayRoundId = CURRENT_ROUND_ID;" in response
    assert "function isCurrentRoundEvent(payload)" in response
    assert "function activateRelayRound(payload)" in response
    assert "if (!isCurrentRoundEvent(payload)) return;" in response
    native_handler = response.split('addRelayEventListener("role.native_event"', 1)[1]
    native_handler = native_handler.split('addRelayEventListener("role.output_delta"', 1)[0]
    assert "renderMarvisWorkLogNativeEvent" in native_handler
    assert "if (!isCurrentRoundEvent(payload)) return;" in native_handler
    assert native_handler.index("renderMarvisWorkLogNativeEvent") < native_handler.index(
        "if (!isCurrentRoundEvent(payload)) return;"
    )
    followup_handler = response.split(
        'addRelayEventListener("role.followup_response"',
        1,
    )[1]
    followup_handler = followup_handler.split(
        'addRelayEventListener("routing.decision"',
        1,
    )[0]
    assert "if (!isCurrentRoundEvent(payload)) return;" in followup_handler
    routing_handler = response.split('addRelayEventListener("routing.decision"', 1)[1]
    routing_handler = routing_handler.split('addRelayEventListener("role.envelope"', 1)[0]
    assert "if (!isCurrentRoundEvent(payload)) return;" in routing_handler
    assert "renderRoleEnvelope" not in routing_handler
    assert "artifact_type: payload.artifact_type || \"routing_decision\"" not in routing_handler
    status_handler = response.split('addRelayEventListener("role.status"', 1)[1]
    status_handler = status_handler.split('addRelayEventListener("task.completed"', 1)[0]
    assert "if (!isCurrentRoundEvent(payload)) return;" in status_handler
    completed_handler = response.split('addRelayEventListener("task.completed"', 1)[1]
    completed_handler = completed_handler.split('addRelayEventListener("task.interrupted"', 1)[0]
    assert "if (!isCurrentRoundEvent(payload)) return;" in completed_handler


@pytest.mark.asyncio
async def test_relay_task_detail_limits_handoff_prompts_to_current_round(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Task30 projection task",
        prompt="第一轮修复。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "director", "interrupted")
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.update_role_status(task.id, "auditor", "passed")
    service._store.update_task_status(task.id, "interrupted")
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "round_id": 2,
            "text": "历史轮：先修 A。",
            "target_role": "director",
            "context_packet_id": 1,
        },
        summary="历史轮：先修 A。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "round_id": 2,
            "status": "waiting",
            "reason": "交给开发工程师处理。",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "历史轮交给开发工程师。",
            "handoff_to": "implementer",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给开发工程师。",
        },
        summary="历史轮交给开发工程师。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "handoff_packet",
        {
            "round_id": 2,
            "from_role": "director",
            "to_role": "implementer",
            "handoff_to": "implementer",
            "summary": "历史轮交给开发工程师。",
            "next_action": "开发处理。",
        },
        summary="历史轮交给开发工程师。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "round_id": 2,
            "status": "passed",
            "reason": "历史轮已修复 A。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "历史轮已修复 A。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="历史轮已修复 A。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "handoff_packet",
        {
            "round_id": 2,
            "from_role": "implementer",
            "to_role": "auditor",
            "handoff_to": "auditor",
            "summary": "历史轮交给审核工程师复核。",
            "next_action": "审核。",
        },
        summary="历史轮交给审核工程师复核。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "round_id": 3,
            "text": "当前轮：继续修 B。",
            "target_role": "director",
            "context_packet_id": 2,
        },
        summary="当前轮：继续修 B。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "round_id": 3,
            "status": "waiting",
            "reason": "当前轮交给开发工程师处理。",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "当前轮交给开发工程师。",
            "handoff_to": "implementer",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给开发工程师。",
        },
        summary="当前轮交给开发工程师。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "handoff_packet",
        {
            "round_id": 3,
            "from_role": "director",
            "to_role": "implementer",
            "handoff_to": "implementer",
            "summary": "当前轮交给开发工程师。",
            "next_action": "开发处理。",
        },
        summary="当前轮交给开发工程师。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "round_id": 3,
            "status": "passed",
            "reason": "当前轮已修复 B。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "当前轮已修复 B。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="当前轮已修复 B。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "handoff_packet",
        {
            "round_id": 3,
            "from_role": "implementer",
            "to_role": "auditor",
            "handoff_to": "auditor",
            "summary": "当前轮交给审核工程师复核。",
            "next_action": "审核。",
        },
        summary="当前轮交给审核工程师复核。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "round_id": 3,
            "status": "waiting",
            "reason": "当前轮等待最终收口。",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "当前轮等待最终收口。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="当前轮等待最终收口。",
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
    assert "历史轮：先修 A。" in conversation_html
    assert "历史轮已修复 A。" not in conversation_html
    assert "当前轮：继续修 B。" in conversation_html
    assert "当前轮已修复 B。" in conversation_html
    assert "| 已中断" not in conversation_html
    assert "| 等待" not in conversation_html
    assert conversation_html.count("任务分配 已完成") == 1
    assert "接力暂停在总工程师" not in conversation_html
    assert conversation_html.count("Marvis 拍了拍 开发工程师 说， 别等了，这就开始") == 1
    assert conversation_html.count("开发工程师交给审核工程师复核") == 1


@pytest.mark.asyncio
async def test_relay_task_detail_treats_terminal_waiting_summary_as_completed(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Task30 lifecycle closure task",
        prompt="修复接力生命周期状态。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "director", "interrupted")
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.update_role_status(task.id, "auditor", "passed")
    service._store.update_task_status(task.id, "interrupted")
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "round_id": 2,
            "text": "当前轮：修复接力收口。",
            "target_role": "director",
            "context_packet_id": 1,
        },
        summary="当前轮：修复接力收口。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "round_id": 2,
            "status": "passed",
            "reason": "修复了接力状态错误显示。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "修复了接力状态错误显示。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="修复了接力状态错误显示。",
    )
    service._store.save_artifact(
        task.id,
        "auditor",
        "audit_report",
        {
            "relay_role": "auditor",
            "round_id": 2,
            "status": "passed",
            "reason": "审核通过，接力状态已经正确收口。",
            "role": "auditor",
            "artifact_type": "audit_report",
            "summary": "审核通过，接力状态已经正确收口。",
            "handoff_to": "director",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回Marvis收尾。",
        },
        summary="审核通过，接力状态已经正确收口。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "round_id": 2,
            "status": "waiting",
            "reason": "已完成接力生命周期统一收口。",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "已完成接力生命周期统一收口。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="已完成接力生命周期统一收口。",
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
    assert "已完成接力生命周期统一收口。" not in conversation_html
    assert "修复了接力状态错误显示。" in conversation_html
    assert "审核通过，接力状态已经正确收口。" in conversation_html
    assert "| 等待" not in conversation_html
    assert "执行反馈 已完成" in conversation_html
    assert "审核反馈 已完成" in conversation_html
    assert "接力暂停在总工程师" not in conversation_html


@pytest.mark.asyncio
async def test_relay_work_log_hides_superseded_role_errors_after_round_success(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Task30 work log lifecycle task",
        prompt="修复工作日志错误收口。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "director", "interrupted")
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.update_role_status(task.id, "auditor", "passed")
    service._store.update_task_status(task.id, "interrupted")
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "role": "director",
            "round_id": 2,
            "artifact_type": "role_error",
            "error": "结构化结果不是合法 JSON，系统无法直接收口。",
        },
        summary="结构化结果不是合法 JSON，系统无法直接收口。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "role_error",
        {
            "relay_role": "implementer",
            "role": "implementer",
            "round_id": 2,
            "artifact_type": "role_error",
            "error": "结构化结果缺少必填字段：status, reason, role。",
        },
        summary="结构化结果缺少必填字段：status, reason, role。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "round_id": 2,
            "status": "passed",
            "reason": "已修复工作日志默认展示失败的问题。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "已修复工作日志默认展示失败的问题。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="已修复工作日志默认展示失败的问题。",
    )
    service._store.save_artifact(
        task.id,
        "auditor",
        "audit_report",
        {
            "relay_role": "auditor",
            "round_id": 2,
            "status": "passed",
            "reason": "审核通过，确认错误收口不再污染默认工作日志。",
            "role": "auditor",
            "artifact_type": "audit_report",
            "summary": "审核通过，确认错误收口不再污染默认工作日志。",
            "handoff_to": "director",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回Marvis收尾。",
        },
        summary="审核通过，确认错误收口不再污染默认工作日志。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "round_id": 2,
            "status": "waiting",
            "reason": "已完成工作日志生命周期收口。",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "已完成工作日志生命周期收口。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="已完成工作日志生命周期收口。",
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
    assert "已完成工作日志生命周期收口。" in work_log_html
    assert "已修复工作日志默认展示失败的问题。" in work_log_html
    assert "结构化结果不是合法 JSON，系统无法直接收口。" not in work_log_html
    assert "结构化结果缺少必填字段：status, reason, role。" not in work_log_html
    assert 'data-marvis-work-log-entry="error"' not in work_log_html


@pytest.mark.asyncio
async def test_relay_work_log_hides_same_round_stale_job_error_after_success(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Same round stale job error",
        prompt="修复同一回合失败后成功的状态收口。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "implementer", "interrupted")
    service._store.save_artifact(
        task.id,
        "implementer",
        "role_error",
        {
            "relay_role": "implementer",
            "role": "implementer",
            "round_id": 1,
            "artifact_type": "role_error",
            "error": "结构化结果缺少必填字段：status, reason, role。",
        },
        summary="结构化结果缺少必填字段：status, reason, role。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "round_id": 1,
            "status": "passed",
            "reason": "已重新输出合法实现报告并完成修复。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "已重新输出合法实现报告并完成修复。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="已重新输出合法实现报告并完成修复。",
    )
    service._store.save_artifact(
        task.id,
        "auditor",
        "audit_report",
        {
            "relay_role": "auditor",
            "round_id": 1,
            "status": "passed",
            "reason": "审核通过，确认同一回合旧错误不再污染当前生命周期。",
            "role": "auditor",
            "artifact_type": "audit_report",
            "summary": "审核通过，确认同一回合旧错误不再污染当前生命周期。",
            "handoff_to": "director",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回Marvis收尾。",
        },
        summary="审核通过，确认同一回合旧错误不再污染当前生命周期。",
    )

    detail = service._store.get_task_detail(task.id)
    implementer_job = next(job for job in detail.role_jobs if job.role == "implementer")
    assert implementer_job.error_message == ""

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
    assert "已重新输出合法实现报告并完成修复。" in work_log_html
    assert "结构化结果缺少必填字段：status, reason, role。" not in work_log_html
    assert 'data-marvis-work-log-entry="error"' not in work_log_html


@pytest.mark.asyncio
async def test_relay_work_log_hides_previous_round_error_after_later_role_success(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Cross round stale error",
        prompt="修复上一轮失败但下一轮成功后的日志收口。",
        workspace="/repo",
        provider="codex",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "relay_role": "director",
            "artifact_type": "user_followup",
            "summary": "第一轮接续触发开发。",
        },
        summary="第一轮接续触发开发。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "role_error",
        {
            "relay_role": "implementer",
            "role": "implementer",
            "artifact_type": "role_error",
            "error": "结构化结果缺少必填字段：status, reason, role。",
        },
        summary="结构化结果缺少必填字段：status, reason, role。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "user_followup",
        {
            "relay_role": "director",
            "artifact_type": "user_followup",
            "summary": "第二轮继续完成同一个开发问题。",
        },
        summary="第二轮继续完成同一个开发问题。",
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "status": "passed",
            "reason": "第二轮已经完成同一个开发问题。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "第二轮已经完成同一个开发问题。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="第二轮已经完成同一个开发问题。",
    )
    service._store.save_artifact(
        task.id,
        "auditor",
        "audit_report",
        {
            "relay_role": "auditor",
            "status": "passed",
            "reason": "审核通过，确认跨回合旧错误不再污染默认工作日志。",
            "role": "auditor",
            "artifact_type": "audit_report",
            "summary": "审核通过，确认跨回合旧错误不再污染默认工作日志。",
            "handoff_to": "director",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回Marvis收尾。",
        },
        summary="审核通过，确认跨回合旧错误不再污染默认工作日志。",
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
    assert "第二轮已经完成同一个开发问题。" in work_log_html
    assert "结构化结果缺少必填字段：status, reason, role。" not in work_log_html
    assert 'data-marvis-work-log-entry="error"' not in work_log_html


def test_relay_role_error_projection_is_resolved_by_later_round_success() -> None:
    errors = _marvis_relay_role_error_payloads_by_role(
        [
            {
                "relay_role": "implementer",
                "role": "implementer",
                "round_id": 5,
                "artifact_type": "role_error",
                "error": "结构化结果缺少必填字段：status, reason, role。",
            },
            {
                "relay_role": "director",
                "role": "director",
                "round_id": 6,
                "artifact_type": "final_summary",
                "status": "waiting",
                "summary": "重新派发开发工程师处理同一问题。",
                "handoff_to": "implementer",
            },
            {
                "relay_role": "implementer",
                "role": "implementer",
                "round_id": 6,
                "artifact_type": "implementation_report",
                "status": "passed",
                "summary": "开发工程师已经在下一轮修复同一问题。",
                "handoff_to": "auditor",
            },
        ]
    )

    assert "implementer" not in errors


@pytest.mark.asyncio
async def test_relay_task_detail_keeps_generic_final_summary_out_of_chat(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Concrete final closure task",
        prompt="修复交互不一致 bug。",
        workspace="/repo",
        provider="codex",
    )
    for role in ("director", "implementer", "auditor"):
        service._store.update_role_metadata(
            task.id,
            role,
            provider="codex",
            model="gpt-5",
            native_session_id=f"native-{role}-closure",
            agent_run_id=900 + len(role),
            dispatch_verified=True,
        )
        service._store.update_role_status(task.id, role, "passed")
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "relay_role": "implementer",
            "status": "passed",
            "reason": "修复了交互不一致 bug，圆形控件改成椭圆形。",
            "role": "implementer",
            "artifact_type": "implementation_report",
            "summary": "修复了交互不一致 bug，圆形控件改成椭圆形。",
            "handoff_to": "auditor",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交给审核工程师复核。",
        },
        summary="修复了交互不一致 bug，圆形控件改成椭圆形。",
    )
    service._store.save_artifact(
        task.id,
        "auditor",
        "audit_report",
        {
            "relay_role": "auditor",
            "status": "passed",
            "reason": "审核通过，交互展示与预期一致。",
            "role": "auditor",
            "artifact_type": "audit_report",
            "summary": "审核通过，交互展示与预期一致。",
            "handoff_to": "director",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "交回总工程师收尾。",
        },
        summary="审核通过，交互展示与预期一致。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "completed",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "已完成任务",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="已完成任务",
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
    work_log_html = _relay_work_log_html(response)
    assert "结论：已完成任务" not in conversation_html
    assert "该角色已返回结构化结果" not in conversation_html
    assert "修复了交互不一致 bug，圆形控件改成椭圆形" in conversation_html
    assert "审核通过，交互展示与预期一致" in conversation_html
    assert "交给审核工程师复核" not in conversation_html
    assert "交回总工程师收尾" not in conversation_html
    assert "修复了交互不一致 bug，圆形控件改成椭圆形" in work_log_html
    assert "审核通过，交互展示与预期一致" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_keeps_mixed_language_final_summary_out_of_chat(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Mixed final closure task",
        prompt="头像被挡住，英文角色名也要改掉。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-mixed-final",
        agent_run_id=951,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "completed",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": (
                "已完成两个问题的处理：1. Marvis 的头像和问候语整体下移，并兼容顶部 safe-area，"
                "避免被顶部遮挡；2. 执行过程中显示的 Marvis/File/Search/Computer Agent 和 "
                "dispatch task 已替换为更自然的中文角色与动作展示。审核已通过。"
            ),
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="已完成两个问题的处理。",
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
    work_log_html = _relay_work_log_html(response)
    assert "头像和问候语整体下移" not in conversation_html
    assert "兼容顶部 安全区" not in conversation_html
    assert "英文角色名" not in conversation_html
    assert "任务分配" not in conversation_html
    assert "该角色已返回结构化结果" not in conversation_html
    assert "safe-area" not in conversation_html
    assert "dispatch task" not in conversation_html
    assert "File/Search/Computer Agent" not in conversation_html
    assert "头像和问候语整体下移" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_keeps_natural_final_summary_with_technical_terms(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Technical final closure task",
        prompt="解释 safe-area 和 dispatch task。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_task_status(task.id, "completed")
    service._store.save_artifact(
        task.id,
        "director",
        "final_summary",
        {
            "relay_role": "director",
            "status": "passed",
            "reason": "用户询问的是普通技术术语。",
            "role": "director",
            "artifact_type": "final_summary",
            "summary": "safe-area 是移动端为系统状态栏预留的安全区域；dispatch task 是任务分发日志里的动作名。",
            "handoff_to": "",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "",
        },
        summary="safe-area 是移动端为系统状态栏预留的安全区域；dispatch task 是任务分发日志里的动作名。",
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
    assert "安全区 是移动端为系统状态栏预留的安全区域" in conversation_html
    assert "任务分配 是任务分发日志里的动作名" in conversation_html
    assert "详情见结构化数据" not in conversation_html


@pytest.mark.asyncio
async def test_relay_work_log_projects_native_events_in_call_order(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    legacy_search_name = " ".join(("Search", "Agent"))
    task = service.create_task(
        title="Marvis full native log task",
        prompt="整理 Steam 游戏并生成网页",
        workspace="/repo",
        provider="codex",
    )
    for role, agent_run_id in [
        ("director", 801),
        ("tester", 802),
        ("director", 801),
        ("implementer", 803),
        ("director", 801),
    ]:
        service._store.update_role_metadata(
            task.id,
            role,
            provider="codex",
            model="gpt-5",
            native_session_id=f"native-{role}-{agent_run_id}",
            agent_run_id=agent_run_id,
            dispatch_verified=True,
        )
        service._store.update_role_status(task.id, role, "passed")

    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "role": "director",
            "artifact_type": "routing_decision",
            "status": "passed",
            "route": "core_relay",
            "summary": "先获取 Mac 配置，再全盘检索 Steam 热门 macOS 游戏。",
            "handoff_to": "tester",
            "required_roles": ["director", "tester", "implementer"],
            "open_questions": [],
            "next_action": f"派 {legacy_search_name} 检索。",
        },
        summary="先获取 Mac 配置，再全盘检索 Steam 热门 macOS 游戏。",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.COMMAND_STARTED,
        payload={
            "command": "shell executor",
            "native_turn_id": "turn-director-config",
            "itemId": "cmd-config",
        },
        occurred_at="2026-06-14T12:10:01+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.COMMAND_COMPLETED,
        payload={
            "command": "shell executor",
            "native_turn_id": "turn-director-config",
            "itemId": "cmd-config",
            "output": "Apple M4 / 32GB / 10核GPU / macOS 26.5",
        },
        occurred_at="2026-06-14T12:10:02+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": f"配置到手：Apple M4 / 32GB / 10核GPU / macOS 26.5。现在派 {legacy_search_name} 去做 Steam 全量检索。",
            "native_turn_id": "turn-director-config",
            "itemId": "assistant-config",
        },
        occurred_at="2026-06-14T12:10:03+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=802,
        event_type=EventType.TOOL_CALL_COMPLETED,
        payload={
            "tool_name": "search apps",
            "native_turn_id": "turn-search",
            "itemId": "tool-search",
        },
        occurred_at="2026-06-14T12:10:04+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=802,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "搞定，有请下一位",
            "native_turn_id": "turn-search",
            "itemId": "assistant-search",
        },
        occurred_at="2026-06-14T12:10:05+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": 'https://www.kitco.com/charts/gold\\",\\"handoff_to\\":\\"\\",',
            "native_turn_id": "turn-director-gold",
            "itemId": "assistant-gold",
        },
        occurred_at="2026-06-14T12:10:05.100000+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": '\\"summary\\":\\"截至查询时，今日国际现货黄金约为 4,088.60 美元/盎司。\\"}',
            "native_turn_id": "turn-director-gold",
            "itemId": "assistant-gold",
        },
        occurred_at="2026-06-14T12:10:05.200000+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.COMMAND_FAILED,
        payload={
            "command": "python executor",
            "native_turn_id": "turn-director-html",
            "itemId": "cmd-python",
            "output": "Invalid control character at line 18 column 27",
        },
        occurred_at="2026-06-14T12:10:06+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "JSON 中有非标准格式，让我修复提取逻辑。",
            "native_turn_id": "turn-director-html",
            "itemId": "assistant-json-fix",
        },
        occurred_at="2026-06-14T12:10:07+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=803,
        event_type=EventType.COMMAND_COMPLETED,
        payload={
            "command": "shell executor",
            "native_turn_id": "turn-browser",
            "itemId": "cmd-browser",
            "output": "页面已打开，截图保存。",
        },
        occurred_at="2026-06-14T12:10:08+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=803,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "搞定，有请下一位",
            "native_turn_id": "turn-browser",
            "itemId": "assistant-browser",
        },
        occurred_at="2026-06-14T12:10:09+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": "页面渲染正常，全部验证通过。",
            "native_turn_id": "turn-director-final",
            "itemId": "assistant-final",
        },
        occurred_at="2026-06-14T12:10:10+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.MODEL_USAGE_UPDATED,
        payload={
            "input_tokens": 28000,
            "output_tokens": 1000,
            "total_tokens": 29000,
        },
        occurred_at="2026-06-14T12:10:11+00:00",
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=801,
        event_type=EventType.AGENT_RUN_ACTIVITY,
        payload={
            "action": "turn_started",
            "native_turn_id": "turn-noise",
        },
        occurred_at="2026-06-14T12:10:12+00:00",
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

    assert "python executor" not in conversation_html
    assert "Invalid control character" not in conversation_html
    assert "29K" in work_log_html
    assert "shell executor 已完成" in work_log_html
    assert "search apps 已完成" in work_log_html
    assert "python executor 调用失败" in work_log_html
    assert "Invalid control character at line 18 column 27" in work_log_html
    assert "配置到手：Apple M4" in work_log_html
    assert "今日国际现货黄金约为 4,088.60 美元/盎司" in work_log_html
    assert "kitco.com/charts/gold" not in work_log_html
    assert "handoff_to" not in work_log_html
    assert "JSON 中有非标准格式" in work_log_html
    assert "turn_started" not in work_log_html
    assert "data-marvis-work-log-segment" in work_log_html
    assert "data-marvis-work-log-entry" in work_log_html
    assert "data-marvis-work-log-output" in work_log_html

    segment_roles = re.findall(r'data-marvis-work-log-segment="([^"]+)"', work_log_html)
    assert segment_roles[:5] == [
        "director",
        "tester",
        "director",
        "implementer",
        "director",
    ]
    assert work_log_html.count('data-marvis-work-log-segment="director"') >= 3
    assert "function renderMarvisWorkLogNativeEvent" in response
    assert "function ensureMarvisWorkLogSegment" in response
    assert "function compactMarvisWorkLogSegment" in response
    assert "dataset.marvisWorkLogToolCount" in response
    assert "dataset.marvisWorkLogToolCounts" in response
    assert "previousCount + toolNodes.length" in response
    assert "existingOutput && newOutput" in response
    assert "updateMarvisWorkLogTokenTotal" in response
    assert (
        "renderMarvisWorkLogNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);"
        in response
    )
    assert "marvis-work-log-shell" not in visible_html


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
    assert "结构化结果不是合法 JSON，系统无法直接收口。" in board_html


@pytest.mark.asyncio
async def test_relay_task_detail_keeps_valid_role_envelope_out_of_marvis_chat(
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
                    "reason": "需要用户先确认目标文件，避免误删。",
                    "role": "director",
                    "artifact_type": "routing_decision",
                    "handoff_to": "",
                    "summary": "需要先确认具体文件路径。",
                    "evidence_refs": ["用户请求未提供精确路径"],
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

    conversation_html = _relay_view_panel_html(response, "conversation")
    assert 'data-native-kind="role_envelope"' not in conversation_html
    assert "结论：需要先确认具体文件路径。" not in conversation_html
    assert "下一步：请用户确认要删除的 md 文件。" not in conversation_html
    assert "待确认：是否删除 /repo/测试接力.md？" not in conversation_html
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
    conversation_html = _relay_view_panel_html(response, "conversation")
    work_log_html = _relay_work_log_html(response)
    assert 'data-conversation-role-final="director"' not in conversation_html
    assert 'data-role-canonical-json="director"' not in visible_html
    assert "结论：完成闭环修复，最终展示只使用权威完成态。" not in conversation_html
    assert "下一步：继续观察全新复杂接力任务。" not in conversation_html
    assert "验收依据：会话流不显示污染前缀" not in conversation_html
    assert "完成闭环修复，最终展示只使用权威完成态。" in work_log_html
    assert "&quot;artifact_type&quot;: &quot;final_summary&quot;" not in visible_html
    assert "模型先说了一段废话" not in visible_html


@pytest.mark.asyncio
async def test_relay_conversation_keeps_role_artifacts_in_work_log_not_chat(
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
    work_log_html = _relay_work_log_html(response)
    message_bodies = _relay_message_bodies_html(conversation_html)
    assert conversation_html.count('data-conversation-role-final="') == 0
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
    assert "该角色已返回结构化结果，详情见结构化数据。" not in message_bodies
    assert "下一步：下一步见结构化数据。" not in message_bodies
    assert "验收依据：验收依据见结构化数据。" not in message_bodies
    assert "结构化结果" in work_log_html


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
    assert "结论：路由到五角色完整接力，下一步交给架构工程师。" not in conversation_html
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
async def test_relay_conversation_hides_same_round_role_error_after_success(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Recovered director envelope",
        prompt="量化 Marvis 接力 token 消耗。",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_status(task.id, "director", "passed")
    service._store.update_role_status(task.id, "auditor", "streaming")
    service._store.update_task_status(task.id, "running")
    service._store.save_artifact(
        task.id,
        "director",
        "role_error",
        {
            "relay_role": "director",
            "role": "director",
            "round_id": 1,
            "artifact_type": "role_error",
            "error": "invalid json: Expecting ',' delimiter",
            "retry_kind": "format",
        },
        summary="结构化结果不是合法 JSON，系统无法直接收口。",
    )
    service._store.save_artifact(
        task.id,
        "director",
        "routing_decision",
        {
            "relay_role": "director",
            "round_id": 1,
            "status": "passed",
            "reason": "需要先审计本地 token 来源。",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "auditor",
            "summary": "先审计证据，再计算接力模式相对单角色模式的 token 增量。",
            "route": "audit_first",
            "risk": "low",
            "required_roles": ["director", "auditor"],
            "evidence_refs": ["runtime_events"],
            "next_action": "派审核工程师读取本地 token 使用记录。",
            "open_questions": [],
            "acceptance_criteria": ["报告 token 绝对增量", "报告百分比开销"],
            "stop_conditions": ["找不到 token 日志时说明不可直接量化"],
            "requires_user_approval": False,
        },
        summary="先审计证据，再计算接力模式相对单角色模式的 token 增量。",
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
    work_log_html = _relay_work_log_html(response)
    assert "结论：先审计证据，再计算接力模式相对单角色模式的 token 增量。" not in conversation_html
    assert "接力暂停在总工程师" not in conversation_html
    assert "结构化结果不是合法 JSON" not in conversation_html
    assert "invalid json" not in conversation_html
    assert "先审计证据，再计算接力模式相对单角色模式的 token 增量。" in work_log_html


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
    assert 'data-conversation-role-stream="director"' in conversation_html
    assert 'data-conversation-role-final="director"' not in conversation_html
    assert "data-raw-preview" not in conversation_html
    assert 'data-stream-event-ids="1"' in conversation_html
    bodies_html = _relay_message_bodies_html(conversation_html)
    assert "总工程师正在处理任务，完成后展示结果。" not in bodies_html
    assert "总工程师正在拆解任务，先确认影响面。" in bodies_html


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
    assert 'data-conversation-role-stream="director"' not in conversation_html
    assert "总工程师正在处理任务，完成后展示结果。" not in bodies_html
    assert delta not in bodies_html
    assert "正在接收结构化输出" not in bodies_html
    assert f"已接收 {len(delta)} 字" not in bodies_html


@pytest.mark.asyncio
async def test_relay_task_detail_hides_fragmented_structured_running_delta(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Fragmented structured live preview task",
        prompt="查询今日铜价",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-fragment-preview",
        agent_run_id=503,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "streaming")
    delta = (
        'final_summary","confirmation_options":[],"evidence_refs":'
        '["https://www.marketwatch.com/investing/future/hg00",'
        '"https://www.lme.com/en/metals/non-ferrous/lme-copper"],'
        '"handoff_to":""'
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=503,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": delta,
            "native_turn_id": "turn-fragment-preview",
            "itemId": "assistant-fragment-preview",
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
    work_log_html = _relay_work_log_html(response)
    assert 'data-conversation-role-stream="director"' not in conversation_html
    assert "final_summary" not in conversation_html
    assert "confirmation_options" not in conversation_html
    assert "evidence_refs" not in conversation_html
    assert "handoff_to" not in conversation_html
    assert "final_summary" in work_log_html
    assert "evidence_refs" in work_log_html


@pytest.mark.asyncio
async def test_relay_task_detail_hides_fragmented_structured_completed_message(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Fragmented structured completed task",
        prompt="查询今日铜价",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "director",
        provider="codex",
        model="gpt-5",
        native_session_id="native-director-fragment-completed",
        agent_run_id=504,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "director", "passed")
    completed = (
        'final_summary","confirmation_options":[],"evidence_refs":'
        '["https://www.marketwatch.com/investing/future/hg00",'
        '"https://hq.smm.cn/copper"],"handoff_to":"","status":"passed"'
    )
    _append_runtime_event(
        runtime_store,
        agent_run_id=504,
        event_type=EventType.MODEL_MESSAGE_COMPLETED,
        payload={
            "text": completed,
            "native_turn_id": "turn-fragment-completed",
            "itemId": "assistant-fragment-completed",
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
    work_log_html = _relay_work_log_html(response)
    assert "final_summary" not in conversation_html
    assert "confirmation_options" not in conversation_html
    assert "evidence_refs" not in conversation_html
    assert "handoff_to" not in conversation_html
    assert "final_summary" in work_log_html
    assert "evidence_refs" in work_log_html


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


@pytest.mark.asyncio
async def test_marvis_relay_conversation_hides_native_task_delta_preview(
    tmp_path: Path,
) -> None:
    server, service, runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Native task chatter",
        prompt="给聊天框上方增加工作区选择",
        workspace="/repo",
        provider="codex",
    )
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="claude",
        model="sdk-deepseek",
        native_session_id="native-implementer-chatter",
        agent_run_id=917,
        dispatch_verified=True,
    )
    service._store.update_role_status(task.id, "implementer", "streaming")
    _append_runtime_event(
        runtime_store,
        agent_run_id=917,
        event_type=EventType.MODEL_TEXT_DELTA,
        payload={
            "delta": (
                "Task #1 created successfully: Explore project structure and chat UI"
                "<tool_use_error>Directory does not exist: /repo/src.</tool_use_error>"
                "total 144"
            ),
            "native_turn_id": "turn-chatter",
            "itemId": "assistant-chatter",
        },
        occurred_at="2026-06-14T12:35:01+00:00",
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
    work_log_html = _relay_work_log_html(response)
    assert "Task #1 created successfully" not in conversation_html
    assert "tool_use_error" not in conversation_html
    assert "total 144" not in conversation_html
    assert "开发工程师正在处理任务，完成后展示结果。" not in conversation_html
    assert "Task #1 created successfully" in work_log_html
    assert "function appendRolePreview" not in response
    assert "function relayPreviewDisplayText" not in response
    assert "function appendRoleStreamDelta" in response
    assert "function renderRoleEnvelope" not in response
    assert "已接收 ${value.length} 字" not in response
    assert "正在处理任务，完成后展示结果" not in response
    assert "function clearAllRolePreviews" in response
    assert "function appendMarvisConversationHandoff" in response
    assert "const seenStreamEventKeys = new Set" in response
    assert "function streamEventKey" in response
    assert (
        "renderRelayNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);"
        in response
    )
    assert "payload.runtime_event_id," in response
    assert "\n        payload\n" in response
    delta_handler = response.split('addRelayEventListener("role.output_delta"', 1)[1]
    delta_handler = delta_handler.split('addRelayEventListener("routing.decision"', 1)[0]
    assert "appendRoleStreamDelta" in delta_handler
    assert "roleOutputs[payload.role]" not in delta_handler
    routing_handler = response.split('addRelayEventListener("routing.decision"', 1)[1]
    routing_handler = routing_handler.split('addRelayEventListener("role.envelope"', 1)[0]
    assert "renderRoleEnvelope" not in routing_handler
    assert "appendMarvisConversationAssistant" not in routing_handler
    assert "data-routing-" not in routing_handler
    assert "data-board-" not in routing_handler
    envelope_handler = response.split('addRelayEventListener("role.envelope"', 1)[1]
    envelope_handler = envelope_handler.split('addRelayEventListener("handoff.created"', 1)[0]
    assert "renderRoleEnvelope" not in envelope_handler
    assert 'artifactType === "final_summary"' in envelope_handler
    assert "appendMarvisConversationAssistant(" in envelope_handler
    assert "payload.display_text" not in envelope_handler
    assert "envelope.display_text" not in envelope_handler
    handoff_handler = response.split('addRelayEventListener("handoff.created"', 1)[1]
    handoff_handler = handoff_handler.split('addRelayEventListener("role.status"', 1)[0]
    assert (
        "appendMarvisConversationHandoff(toRole, handoffKey, fromRole, roundId);" in handoff_handler
    )
    assert "data-native-from-role" in response
    assert "data-native-to-role" in response
    assert "clearRolePreview(role);" in response


@pytest.mark.asyncio
async def test_marvis_relay_stream_delta_buffers_and_removes_protocol_fragments(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Live protocol fragment cleanup",
        prompt="查询今日铜价",
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

    assert "const roleStreamBuffers = new Map" in response
    assert "const hiddenProtocolStreamKeys = new Set" in response
    assert "removeRoleStreamNode(role);" in response
    assert "marvisConversationTextIsProtocolNoise(buffered)" in response
    assert "appendMarvisConversationWaiting(activeRelayRoundId);" in response
    assert "payload.stream_key" in response
    assert "const directPayload = nativeEvent && typeof nativeEvent === \"object\" ? nativeEvent : {};" in response
    assert "roleStreamBufferKey(role, stableEventId)" in response
    assert "function createMarvisRoleStreamMessage" in response
    assert "function marvisConversationStreamAction" in response
    assert 'node.className = "marvis-relay-agent-step";' in response
    assert 'node.dataset.nativeKind = "text_delta";' in response
    assert 'createNativeMessage(role, "text_delta", labelForRole(role)' not in response
    delta_handler = response.split('addRelayEventListener("role.output_delta"', 1)[1]
    delta_handler = delta_handler.split('addRelayEventListener("role.followup_response"', 1)[0]
    assert "payload.runtime_event_id," in delta_handler
    assert "\n        payload\n" in delta_handler


@pytest.mark.asyncio
async def test_marvis_relay_live_final_summary_appends_natural_response(
    tmp_path: Path,
) -> None:
    server, service, _runtime_store = _server(tmp_path)
    task = service.create_task(
        title="Live direct final summary",
        prompt="查今天上海天气",
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

    envelope_handler = response.split('addRelayEventListener("role.envelope"', 1)[1]
    envelope_handler = envelope_handler.split('addRelayEventListener("handoff.created"', 1)[0]
    assert 'artifactType === "final_summary"' in envelope_handler
    assert "appendMarvisConversationAssistant(" in envelope_handler
    assert "payload.display_text" not in envelope_handler


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
    conversation_html = _relay_view_panel_html(response, "conversation")
    assert 'data-native-kind="role_envelope"' not in conversation_html
    assert 'data-role-canonical-json="director"' not in visible_html
    assert "结论：由总工程师直接处理：直接查询并汇总今日金价。" not in conversation_html
    assert "下一步：由总工程师核验最新行情来源并给出结果" not in conversation_html
    assert "验收依据：不展示 总工程师直接处理 给用户" not in conversation_html
    assert "路由为director_only" not in visible_html
    assert "complete directly after routing by checking current market sources" not in visible_html


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

    assert "结构化结果不是合法 JSON，系统无法直接收口。" in response
    assert "等待总工程师接收并形成决策摘要" not in response
    assert "调度决策未生成" not in response
    assert "总工程师执行问题：结构化结果不是合法 JSON，系统无法直接收口。" in response
    assert "等待总工程师接收任务并形成调度决策" not in response
