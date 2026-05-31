from __future__ import annotations

import json
from pathlib import Path

from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.codex_native.transcript_mirror import CodexSessionTranscriptMirror
from wlcodex.db import Ledger
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


def _stores(
    tmp_path: Path,
) -> tuple[NativeCodexSessionStore, RuntimeEventStore]:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return NativeCodexSessionStore(ledger), RuntimeEventStore(ledger._conn)


def _write_session_jsonl(root: Path, thread_id: str, rows: list[dict]) -> Path:
    path = root / "2026" / "05" / "31" / f"rollout-test-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_transcript_mirror_imports_official_jsonl_tail_without_duplicates(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019e730a-4745-76a3-94f3-488dc169249e"
    root = tmp_path / "sessions"
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-05-31T01:00:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "duplicate source"}],
                },
            },
            {
                "timestamp": "2026-05-31T01:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "你看下这个会话",
                },
            },
            {
                "timestamp": "2026-05-31T01:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": "large tool output should not become transcript text",
                },
            },
            {
                "timestamp": "2026-05-31T01:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "我已经看到根因",
                },
            },
        ],
    )
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
        tail_lines=20,
    )

    first_count = mirror.sync_thread(thread_id)
    second_count = mirror.sync_thread(thread_id)

    session = session_store.get_by_thread_id(thread_id)
    assert session is not None
    assert first_count == 2
    assert second_count == 0
    events = runtime_store.list_by_agent_run(session.agent_run_id, limit=20)
    assert [event.event_type for event in events] == [
        EventType.USER_MESSAGE_RECEIVED,
        EventType.MODEL_TEXT_DELTA,
    ]
    assert events[0].payload["text"] == "你看下这个会话"
    assert events[1].payload["delta"] == "我已经看到根因"
    assert events[0].payload["native_turn_id"] == events[1].payload["native_turn_id"]


def test_transcript_mirror_uses_official_turn_context_id(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019e7bae-f168-7881-82e6-515d49a51fec"
    root = tmp_path / "sessions"
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-05-31T01:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "official-turn-1",
                },
            },
            {
                "timestamp": "2026-05-31T01:00:00.001Z",
                "type": "turn_context",
                "payload": {"turn_id": "official-turn-1"},
            },
            {
                "timestamp": "2026-05-31T01:00:00.002Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "继续",
                },
            },
            {
                "timestamp": "2026-05-31T01:00:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "继续处理",
                },
            },
        ],
    )
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
        tail_lines=20,
    )

    projected_count = mirror.sync_thread(thread_id)

    session = session_store.get_by_thread_id(thread_id)
    assert session is not None
    assert projected_count == 2
    events = runtime_store.list_by_agent_run(session.agent_run_id, limit=20)
    assert [event.payload["native_turn_id"] for event in events] == [
        "official-turn-1",
        "official-turn-1",
    ]
    assert session_store.get_by_thread_id(thread_id).last_turn_id == "official-turn-1"


def test_transcript_mirror_deduplicates_existing_items_after_restart(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019e7bae-f168-7881-82e6-515d49a51fec"
    root = tmp_path / "sessions"
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-05-31T01:00:00.001Z",
                "type": "turn_context",
                "payload": {"turn_id": "official-turn-1"},
            },
            {
                "timestamp": "2026-05-31T01:00:00.002Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "继续",
                },
            },
            {
                "timestamp": "2026-05-31T01:00:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "继续处理",
                },
            },
        ],
    )
    first_mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
        tail_lines=20,
    )
    restarted_mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
        tail_lines=20,
    )

    assert first_mirror.sync_thread(thread_id) == 2
    assert restarted_mirror.sync_thread(thread_id) == 0

    session = session_store.get_by_thread_id(thread_id)
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id, limit=20)
    assert len(events) == 2


def test_transcript_mirror_corrects_existing_item_turn_ids(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019e7bae-f168-7881-82e6-515d49a51fec"
    root = tmp_path / "sessions"
    rows = [
        {
            "timestamp": "2026-05-31T01:00:00.002Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "继续",
            },
        },
        {
            "timestamp": "2026-05-31T01:00:01.000Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "继续处理",
            },
        },
    ]
    path = _write_session_jsonl(root, thread_id, rows)
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
        tail_lines=20,
    )
    assert mirror.sync_thread(thread_id) == 2

    session = session_store.get_by_thread_id(thread_id)
    assert session is not None
    before = runtime_store.list_by_agent_run(session.agent_run_id, limit=20)
    assert len(before) == 2
    assert all(
        str(event.payload["native_turn_id"]).startswith("jsonl-turn:")
        for event in before
    )

    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-05-31T01:00:00.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "official-turn-1"},
            },
            *rows,
        ],
    )
    path.touch()
    restarted_mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
        tail_lines=20,
    )

    assert restarted_mirror.sync_thread(thread_id) == 0

    after = runtime_store.list_by_agent_run(session.agent_run_id, limit=20)
    assert len(after) == 2
    assert [event.payload["native_turn_id"] for event in after] == [
        "official-turn-1",
        "official-turn-1",
    ]
    assert [event.payload["turnId"] for event in after] == [
        "official-turn-1",
        "official-turn-1",
    ]
    assert session_store.get_by_thread_id(thread_id).last_turn_id == "official-turn-1"


def test_transcript_mirror_default_tail_handles_tool_heavy_jsonl(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019e7bae-f168-7881-82e6-515d49a51fec"
    root = tmp_path / "sessions"
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-05-31T01:00:00.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "official-turn-1"},
            },
            {
                "timestamp": "2026-05-31T01:00:00.002Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "继续",
                },
            },
            {
                "timestamp": "2026-05-31T01:00:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "继续处理",
                },
            },
            *[
                {
                    "timestamp": f"2026-05-31T01:01:{index % 60:02d}.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": f"tool output {index}",
                    },
                }
                for index in range(600)
            ],
        ],
    )
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
    )

    assert mirror.sync_thread(thread_id) == 2

    session = session_store.get_by_thread_id(thread_id)
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id, limit=20)
    assert [event.payload["native_turn_id"] for event in events] == [
        "official-turn-1",
        "official-turn-1",
    ]
