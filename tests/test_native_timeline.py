from __future__ import annotations

import sqlite3

from wlcodex.native_timeline import NativeTimelineStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


def _runtime_event(
    event_type: str,
    payload: dict,
    *,
    event_id: int | None = None,
    agent_run_id: int = 42,
) -> RuntimeEvent:
    return RuntimeEvent(
        id=event_id,
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id=str(agent_run_id),
        correlation_id=f"corr-{agent_run_id}",
        source=EventSource.CODEX,
        actor="codex_native",
        visibility=Visibility.USER,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=7,
        agent_run_id=agent_run_id,
    )


def test_native_timeline_sequences_and_replays_after_restart() -> None:
    conn = sqlite3.connect(":memory:")
    store = NativeTimelineStore(conn)
    store.project_runtime_event(
        _runtime_event(
            EventType.USER_MESSAGE_RECEIVED,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "user-1",
                "text": "请分析这个问题",
                "provider": "codex",
            },
            event_id=101,
        )
    )
    store.project_runtime_event(
        _runtime_event(
            EventType.PROVIDER_DISPLAY_DELTA,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "delta": "结论",
                "provider": "codex",
            },
            event_id=102,
        )
    )
    store.project_runtime_event(
        _runtime_event(
            EventType.PROVIDER_DISPLAY_COMPLETED,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "text": "结论完整",
                "provider": "codex",
            },
            event_id=103,
        )
    )

    restarted = NativeTimelineStore(conn)
    events = restarted.list_events("codex", "thread-1", after=1)

    assert [event.sequence for event in events] == [2, 3]
    assert [event.kind for event in events] == ["text_delta", "message_completed"]
    assert events[-1].payload["text"] == "结论完整"
    items = restarted.list_items("codex", "thread-1", limit=20)
    assert [(item.role, item.text, item.status) for item in items] == [
        ("user", "请分析这个问题", "completed"),
        ("assistant", "结论完整", "completed"),
    ]
    snapshot = restarted.list_item_events("codex", "thread-1", limit=20)
    assert [event.sequence for event in snapshot] == [1, 3]
    assert [event.kind for event in snapshot] == ["user_message", "message_completed"]
    assert snapshot[-1].payload["text"] == "结论完整"


def test_native_timeline_item_snapshot_before_uses_item_latest_sequence() -> None:
    store = NativeTimelineStore(sqlite3.connect(":memory:"))
    store.project_runtime_event(
        _runtime_event(
            EventType.USER_MESSAGE_RECEIVED,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-0",
                "itemId": "user-0",
                "text": "更早消息",
                "provider": "codex",
            },
            event_id=111,
        )
    )
    store.project_runtime_event(
        _runtime_event(
            EventType.PROVIDER_DISPLAY_DELTA,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "delta": "片段一",
                "provider": "codex",
            },
            event_id=112,
        )
    )
    store.project_runtime_event(
        _runtime_event(
            EventType.PROVIDER_DISPLAY_DELTA,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "delta": "片段二",
                "provider": "codex",
            },
            event_id=113,
        )
    )

    recent = store.list_item_events("codex", "thread-1", limit=1)
    older = store.list_item_events("codex", "thread-1", before=recent[0].sequence, limit=20)
    live_events = store.list_events("codex", "thread-1", after=1, limit=20)

    assert [(event.sequence, event.kind, event.payload["text"]) for event in recent] == [
        (3, "text_delta", "片段一片段二")
    ]
    assert [(event.sequence, event.kind, event.payload.get("delta")) for event in live_events] == [
        (2, "text_delta", "片段一"),
        (3, "text_delta", "片段二"),
    ]
    assert all("text" not in event.payload for event in live_events)
    assert [(event.sequence, event.kind, event.payload["text"]) for event in older] == [
        (1, "user_message", "更早消息")
    ]


def test_native_timeline_item_snapshot_excludes_internal_command_items() -> None:
    store = NativeTimelineStore(sqlite3.connect(":memory:"))
    store.project_runtime_event(
        _runtime_event(
            EventType.USER_MESSAGE_RECEIVED,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "user-1",
                "text": "显示我",
                "provider": "codex",
            },
            event_id=121,
        )
    )
    for index in range(25):
        store.project_runtime_event(
            _runtime_event(
                EventType.COMMAND_STARTED,
                {
                    "native_thread_id": "thread-1",
                    "native_turn_id": f"turn-cmd-{index}",
                    "command_id": f"cmd-{index}",
                    "command": f"pytest shard {index}",
                    "provider": "codex",
                },
                event_id=122 + index,
            )
        )

    snapshot = store.list_item_events("codex", "thread-1", limit=5)
    events = store.list_events("codex", "thread-1", after=1, limit=3)

    assert [(event.kind, event.payload["text"]) for event in snapshot] == [
        ("user_message", "显示我")
    ]
    assert [event.kind for event in events] == [
        "command_started",
        "command_started",
        "command_started",
    ]


def test_native_timeline_merges_local_echo_with_official_user_message() -> None:
    store = NativeTimelineStore(sqlite3.connect(":memory:"))

    local = store.record_local_user_message(
        provider="codex",
        native_thread_id="thread-1",
        text="同一个任务",
        images=[{"filename": "photo.jpg"}],
    )
    official = store.project_runtime_event(
        _runtime_event(
            EventType.USER_MESSAGE_RECEIVED,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-9",
                "itemId": "item-9",
                "text": "同一个任务",
                "provider": "codex",
            },
            event_id=201,
        )
    )

    items = store.list_items("codex", "thread-1", limit=20)
    assert len(items) == 1
    assert items[0].turn_key == "turn-9"
    assert items[0].item_key == "item-9"
    assert [local[0].sequence, official[0].sequence] == [1, 2]


def test_native_timeline_ignores_raw_frames_and_preserves_visible_delta_text() -> None:
    store = NativeTimelineStore(sqlite3.connect(":memory:"))

    assert (
        store.project_runtime_event(
            _runtime_event(
                EventType.PROVIDER_RAW_FRAME,
                {
                    "native_thread_id": "thread-1",
                    "native_turn_id": "turn-1",
                    "raw_preview": "large raw frame",
                    "provider": "codex",
                },
                event_id=301,
            )
        )
        == []
    )
    events = store.project_runtime_event(
        _runtime_event(
            EventType.MODEL_TEXT_DELTA,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "delta": "x" * 400,
                "provider": "codex",
            },
            event_id=302,
        )
    )

    assert len(events) == 1
    assert events[0].runtime_event_id == 302
    row = store._conn.execute(
        "SELECT payload_json FROM native_timeline_events WHERE sequence = 1"
    ).fetchone()
    json_payload = row[0]
    assert json_payload
    assert "raw_preview" not in json_payload
    assert "x" * 400 in json_payload


def test_native_timeline_does_not_downgrade_completed_message_with_late_delta() -> None:
    store = NativeTimelineStore(sqlite3.connect(":memory:"))

    store.project_runtime_event(
        _runtime_event(
            EventType.PROVIDER_DISPLAY_COMPLETED,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "text": "最终答案",
                "provider": "codex",
            },
            event_id=401,
        )
    )
    late_delta = store.project_runtime_event(
        _runtime_event(
            EventType.PROVIDER_DISPLAY_DELTA,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "delta": "过期片段",
                "provider": "codex",
            },
            event_id=402,
        )
    )

    assert late_delta == []
    events = store.list_events("codex", "thread-1")
    items = store.list_items("codex", "thread-1")
    assert [event.kind for event in events] == ["message_completed"]
    assert [(item.text, item.status) for item in items] == [("最终答案", "completed")]


def test_native_timeline_projects_plan_and_approval_items() -> None:
    store = NativeTimelineStore(sqlite3.connect(":memory:"))

    plan = store.project_runtime_event(
        _runtime_event(
            EventType.AGENT_RUN_ACTIVITY,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "action": "plan_updated",
                "plan": ["写 RED 测试", "实现 read-model"],
                "provider": "codex",
            },
            event_id=501,
        )
    )
    approval = store.project_runtime_event(
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "request_id": "approval-1",
                "summary": "需要运行 pytest",
                "provider": "codex",
            },
            event_id=502,
        )
    )

    assert [event.kind for event in plan + approval] == [
        "activity",
        "approval_requested",
    ]
    items = store.list_items("codex", "thread-1")
    assert [(item.kind, item.status, item.text) for item in items] == [
        ("activity", "completed", "写 RED 测试\n实现 read-model"),
        ("approval_requested", "pending", "需要运行 pytest"),
    ]
