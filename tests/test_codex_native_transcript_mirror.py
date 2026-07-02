from __future__ import annotations

import json
import os
from pathlib import Path

from wlcodex.codex_native.projector import NativeCodexEventProjector
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


def _logical_events(events: list) -> list:
    return [event for event in events if event.event_type != EventType.PROVIDER_RAW_FRAME]


def test_transcript_mirror_indexes_recent_desktop_sessions(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019efc99-e43b-7e83-95a7-88194da07f75"
    root = tmp_path / "sessions"
    path = _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-06-25T02:26:44.423Z",
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "cwd": "/Users/wl/projects/LiangH",
                    "timestamp": "2026-06-25T02:26:44.423Z",
                    "originator": "Codex Desktop",
                    "source": "codex_desktop",
                    "thread_source": "user",
                },
            },
            {
                "timestamp": "2026-06-25T02:26:45.000Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "cwd": "/Users/wl/projects/LiangH",
                },
            },
            {
                "timestamp": "2026-06-25T02:26:46.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "现在策略库有多少个策略了\n",
                },
            },
        ],
    )
    path.touch()
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
    )

    assert mirror.index_recent_sessions(limit=20) == 1

    [session] = session_store.list_recent(limit=10)
    assert session.native_thread_id == thread_id
    assert session.title == "现在策略库有多少个策略了"
    assert session.cwd == "/Users/wl/projects/LiangH"
    assert session.source_kind == "codex_jsonl"
    assert session.status == "idle"
    assert session.metadata["originator"] == "Codex Desktop"
    assert session.metadata["thread_source"] == "user"
    assert session.metadata["rollout_path"].endswith(f"{thread_id}.jsonl")


def test_transcript_mirror_selects_latest_turn_threads_by_file_activity(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    root = tmp_path / "sessions"
    thread_ids = [
        "019efc99-e43b-7e83-95a7-88194da07f750",
        "019efc99-e43b-7e83-95a7-88194da07f751",
        "019efc99-e43b-7e83-95a7-88194da07f752",
        "019efc99-e43b-7e83-95a7-88194da07f753",
    ]
    paths = []
    for index, thread_id in enumerate(thread_ids):
        path = _write_session_jsonl(
            root,
            thread_id,
            [
                {
                    "timestamp": "2026-06-25T02:26:44.423Z",
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "cwd": "/Users/wl/projects/wlcodex",
                        "thread_source": "user",
                    },
                },
                {
                    "timestamp": "2026-06-25T02:26:45.000Z",
                    "type": "turn_context",
                    "payload": {"turn_id": f"turn-{index}"},
                },
            ],
        )
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        paths.append(path)
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
    )

    assert mirror.recent_turn_thread_ids(limit=2) == [thread_ids[3], thread_ids[2]]
    assert mirror._path_cache == {
        thread_ids[3]: paths[3],
        thread_ids[2]: paths[2],
    }


def test_transcript_mirror_signatures_change_when_jsonl_changes(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019efc99-e43b-7e83-95a7-88194da07f75"
    root = tmp_path / "sessions"
    path = _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-06-25T02:26:44.423Z",
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "cwd": "/Users/wl/projects/LiangH",
                    "thread_source": "user",
                },
            },
        ],
    )
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
    )

    initial_session_signature = mirror.session_index_signature(limit=20)
    initial_thread_signature = mirror.thread_file_signature(thread_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-06-25T02:26:46.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "现在策略库有多少个策略了\n",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    assert mirror.session_index_signature(limit=20) != initial_session_signature
    assert mirror.thread_file_signature(thread_id) != initial_thread_signature


def test_transcript_mirror_skips_desktop_subagent_sessions(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019efded-972d-7ca3-84de-ea4ed72eecc1"
    root = tmp_path / "sessions"
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-06-25T08:37:46.000Z",
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "cwd": "/Users/wl/projects/LiangH",
                    "originator": "Codex Desktop",
                    "source": {"subagent": {"other": "guardian"}},
                    "thread_source": "subagent",
                },
            },
            {
                "timestamp": "2026-06-25T08:37:47.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "The following is the Codex agent history...",
                },
            },
        ],
    )
    mirror = CodexSessionTranscriptMirror(
        root=root,
        session_store=session_store,
        runtime_store=runtime_store,
    )

    assert mirror.index_recent_sessions(limit=20) == 0
    assert session_store.list_recent(limit=10) == []


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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))
    assert [event.event_type for event in events] == [
        EventType.USER_MESSAGE_RECEIVED,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
    ]
    assert events[0].payload["text"] == "你看下这个会话"
    assert events[2].payload["delta"] == "我已经看到根因"
    assert events[0].payload["native_turn_id"] == events[2].payload["native_turn_id"]


def test_transcript_mirror_imports_task_complete_last_agent_message(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019e924a-7709-78d1-963e-418444ea667e"
    root = tmp_path / "sessions"
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-06-04T11:00:14.300Z",
                "type": "turn_context",
                "payload": {"turn_id": "official-turn-1"},
            },
            {
                "timestamp": "2026-06-04T11:00:14.310Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "评估这个文档",
                },
            },
            {
                "timestamp": "2026-06-04T11:07:51.614Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "official-turn-1",
                    "last_agent_message": (
                        "结论：这份文档**方向大体正确**。\n\n"
                        "**主要正确点**\n"
                        "- `controller.py` 单类过大\n"
                    ),
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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))

    assert projected_count == 2
    assert [event.event_type for event in events] == [
        EventType.USER_MESSAGE_RECEIVED,
        EventType.PROVIDER_DISPLAY_COMPLETED,
        EventType.MODEL_MESSAGE_COMPLETED,
    ]
    assert events[2].payload["text"].startswith("结论：这份文档")
    assert events[2].payload["native_turn_id"] == "official-turn-1"
    assert str(events[2].payload["itemId"]).startswith("jsonl-assistant-final:")


def test_transcript_mirror_imports_official_plan_response_item_as_plan_activity(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    thread_id = "019f0010-0000-4f00-8f00-333333333333"
    root = tmp_path / "sessions"
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-06-04T11:30:00.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "official-turn-plan"},
            },
            {
                "timestamp": "2026-06-04T11:30:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "plan",
                    "id": "official-plan-item",
                    "text": "# WLCodex Plan\n\n## Summary\nRender the plan card.",
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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))
    assert first_count == 1
    assert second_count == 0
    assert [event.event_type for event in events] == [EventType.AGENT_RUN_ACTIVITY]
    assert events[0].payload["action"] == "plan_updated"
    assert events[0].payload["plan"].startswith("# WLCodex Plan")
    assert events[0].payload["itemId"] == "official-plan-item"
    assert events[0].payload["native_turn_id"] == "official-turn-plan"


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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))
    assert [event.payload["native_turn_id"] for event in events] == [
        "official-turn-1",
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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))
    assert len(events) == 3


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
    before = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))
    assert len(before) == 3
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

    after = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))
    assert len(after) == 3
    assert [event.payload["native_turn_id"] for event in after] == [
        "official-turn-1",
        "official-turn-1",
        "official-turn-1",
    ]
    assert [event.payload["turnId"] for event in after] == [
        "official-turn-1",
        "official-turn-1",
        "official-turn-1",
    ]
    assert session_store.get_by_thread_id(thread_id).last_turn_id == "official-turn-1"


def test_transcript_mirror_skips_user_message_when_local_echo_exists(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    thread_id = "019f000d-0000-4f00-8f00-111111111111"
    root = tmp_path / "sessions"
    projector.project_user_message(
        native_thread_id=thread_id,
        native_turn_id="official-turn-1",
        text="评估这份文档",
    )
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-06-04T12:00:00.000Z",
                "type": "turn_context",
                "payload": {"turn_id": "official-turn-1"},
            },
            {
                "timestamp": "2026-06-04T12:00:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "评估这份文档",
                },
            },
            {
                "timestamp": "2026-06-04T12:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "我来评估",
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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))

    assert projected_count == 1
    assert len(events) == 3
    assert events[0].event_type == EventType.USER_MESSAGE_RECEIVED
    assert str(events[0].payload["itemId"]).startswith("local-user-")
    assert events[1].event_type == EventType.PROVIDER_DISPLAY_DELTA
    assert events[2].event_type == EventType.MODEL_TEXT_DELTA


def test_transcript_mirror_skips_user_message_with_jsonl_turn_fallback(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    thread_id = "019f000d-0000-4f00-8f00-222222222222"
    root = tmp_path / "sessions"
    projector.project_user_message(
        native_thread_id=thread_id,
        native_turn_id="official-turn-1",
        text="继续执行",
    )
    _write_session_jsonl(
        root,
        thread_id,
        [
            {
                "timestamp": "2026-06-04T13:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "继续执行",
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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))

    assert projected_count == 0
    assert len(events) == 1
    assert events[0].event_type == EventType.USER_MESSAGE_RECEIVED
    assert str(events[0].payload["itemId"]).startswith("local-user-")


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
    events = _logical_events(runtime_store.list_by_agent_run(session.agent_run_id, limit=20))
    assert [event.payload["native_turn_id"] for event in events] == [
        "official-turn-1",
        "official-turn-1",
        "official-turn-1",
    ]
