from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer, format_sse_event
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


def _store(tmp_path: Path) -> RuntimeEventStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return RuntimeEventStore(ledger._conn)


def _event(agent_run_id: int, payload: dict) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=EventType.MODEL_TEXT_DELTA,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id=str(agent_run_id),
        correlation_id=f"corr-{agent_run_id}",
        source=EventSource.CODEX,
        actor="codex",
        visibility=Visibility.USER,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=7,
        agent_run_id=agent_run_id,
    )


def _runtime_event(
    agent_run_id: int,
    event_type: str,
    payload: dict,
) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id=str(agent_run_id),
        correlation_id=f"corr-{agent_run_id}",
        source=EventSource.CODEX,
        actor="codex",
        visibility=Visibility.USER,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=7,
        agent_run_id=agent_run_id,
    )


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


@pytest.mark.asyncio
async def test_health_endpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /health HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert '"status": "ok"' in response


@pytest.mark.asyncio
async def test_static_css_endpoint_serves_package_asset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /static/native_index.css HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    headers, body = response.split("\r\n\r\n", 1)
    assert "HTTP/1.1 200 OK" in headers
    assert "Content-Type: text/css; charset=utf-8" in headers
    assert "Cache-Control: public, max-age=300, stale-while-revalidate=60" in headers
    assert ".provider-index" in body


@pytest.mark.asyncio
async def test_static_base_css_endpoint_serves_design_tokens(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /static/base.css HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    headers, body = response.split("\r\n\r\n", 1)
    assert "HTTP/1.1 200 OK" in headers
    assert "Content-Type: text/css; charset=utf-8" in headers
    assert "Cache-Control: public, max-age=300, stale-while-revalidate=60" in headers
    assert "--bg-root" in body
    assert "--color-link" in body
    assert "--ease-default" in body
    assert "focus-visible" in body
    assert "prefers-reduced-motion" in body


@pytest.mark.asyncio
async def test_snapshot_endpoint_returns_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.append(_event(42, {"delta": "hello"}))
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?after=0&limit=10 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    body = response.split("\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert payload["events"][0]["id"] == saved.id
    assert payload["events"][0]["kind"] == "text_delta"
    assert payload["events"][0]["payload"] == {"delta": "hello"}


@pytest.mark.asyncio
async def test_snapshot_endpoint_can_return_tail_with_previous_count(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    saved = [store.append(_event(42, {"delta": str(index)})) for index in range(5)]
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?tail=2 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    body = response.split("\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert [event["id"] for event in payload["events"]] == [
        saved[3].id,
        saved[4].id,
    ]
    assert payload["previous_event_count"] == 3


@pytest.mark.asyncio
async def test_snapshot_endpoint_is_readonly_and_does_not_schedule_native_transcript_sync(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    class FakeTranscriptMirror:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def sync_thread(self, native_thread_id: str) -> int:
            self.calls.append(native_thread_id)
            store.append(
                _runtime_event(
                    42,
                    EventType.USER_MESSAGE_RECEIVED,
                    {
                        "native_thread_id": native_thread_id,
                        "native_turn_id": "jsonl-turn",
                        "text": "jsonl latest",
                    },
                )
            )
            return 1

    mirror = FakeTranscriptMirror()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_transcript_mirror=mirror,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?tail=10&native_thread_id=thread-jsonl HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    body = response.split("\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert mirror.calls == []
    assert payload["events"] == []
    assert payload["native_sync_error"] == ""
    assert payload["native_sync_pending"] is False


@pytest.mark.asyncio
async def test_snapshot_endpoint_can_page_before_an_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = [store.append(_event(42, {"delta": str(index)})) for index in range(5)]
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            f"GET /api/workers/42/events?before={saved[3].id}&limit=2 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    body = response.split("\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert [event["id"] for event in payload["events"]] == [
        saved[1].id,
        saved[2].id,
    ]
    assert payload["previous_event_count"] == 1


@pytest.mark.asyncio
async def test_turn_summary_endpoint_returns_rule_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _runtime_event(
            42,
            EventType.USER_MESSAGE_RECEIVED,
            {"native_turn_id": "turn-fold", "text": "修复实时发送"},
        )
    )
    store.append(
        _runtime_event(
            42,
            EventType.AGENT_RUN_COMPLETED,
            {"native_turn_id": "turn-fold", "status": "completed"},
        )
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/turn-summary?native_turn_id=turn-fold HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    body = response.split("\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert payload["summary"]["native_turn_id"] == "turn-fold"
    assert payload["summary"]["title"] == "2 条消息：修复实时发送"
    assert payload["summary"]["source"] == "rules"


@pytest.mark.asyncio
async def test_live_page_endpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Codex" in response
    assert "/api/workers/42/stream" in response
    assert "turn-fold" in response
    assert "turn-fold-chevron" in response
    assert "turnFoldTitle(group)" in response
    assert "turn-fold-source" not in response
    # Session freshness must expose its real source instead of pretending that
    # an unavailable provider snapshot is live.  This is distinct from the
    # old turn-fold source badge, which intentionally remains absent.
    assert "nativePresentationSourceLabel(freshness.source)" in response
    assert "nativePresentationNoticeText" in response
    assert "hasLiveDisplayEvents" in response
    assert ".transcript-item.user { justify-self: end;" in response
    assert ".transcript-item.user .transcript-body" in response
    assert "function isMirroredTranscriptEvent(event)" in response
    assert "const mirroredTranscript = isMirroredTranscriptEvent(event);" in response
    assert "if (options.historical) return;" in response
    assert "} else if (mirroredTranscript) {" in response


def test_server_rejects_non_loopback_host(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="loopback"):
        WorkerLiveStreamServer(
            host="0.0.0.0",
            port=18731,
            hub=WorkerLiveStreamHub(store),
        )


def test_format_sse_event_uses_event_id_and_json_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.append(_event(42, {"delta": "hello"}))
    hub = WorkerLiveStreamHub(store)
    event = hub.snapshot(agent_run_id=42, after_id=0, limit=1)[0]

    raw = format_sse_event(event).decode("utf-8")

    assert raw.startswith(f"id: {saved.id}\n")
    assert "event: text_delta\n" in raw
    assert '"kind": "text_delta"' in raw
    assert raw.endswith("\n\n")
