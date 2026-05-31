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
    path.parent.mkdir(parents=True)
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

