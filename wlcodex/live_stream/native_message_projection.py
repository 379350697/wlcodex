"""Stable Native timeline/message serialization for JSON and SSE readers."""

from __future__ import annotations

import json
from typing import Any

from wlcodex.native_timeline import NativeTimelineEvent, NativeTimelineItem
from wlcodex.native_turn_semantics import ACTIVE_TURN_STATUSES


def format_native_timeline_sse_event(event: NativeTimelineEvent) -> bytes:
    payload = json.dumps(native_timeline_display_event(event), ensure_ascii=False)
    return f"id: {event.sequence}\nevent: {event.kind}\ndata: {payload}\n\n".encode("utf-8")


def format_native_message_sse_event(
    item: NativeTimelineItem,
    *,
    replay: bool = False,
    update_cursor: int | None = None,
) -> bytes:
    public_cursor = int(update_cursor or item.cursor or item.id)
    payload = json.dumps(
        {
            "item": native_conversation_item_json(item),
            "event": native_conversation_item_display_event(item),
            "cursor": public_cursor,
            "item_cursor": int(item.id),
            "update_cursor": public_cursor,
            "replay": replay,
        },
        ensure_ascii=False,
    )
    return (
        f"id: {public_cursor}\n"
        f"event: {native_message_sse_event_name(item, replay=replay)}\n"
        f"data: {payload}\n\n"
    ).encode("utf-8")


def native_message_sse_event_name(item: NativeTimelineItem, *, replay: bool = False) -> str:
    if replay:
        return "message_added"
    if item.kind == "message" and item.status == "completed":
        return "message_completed"
    if item.kind == "message":
        return "message_updated"
    return "message_added"


def native_conversation_item_json(item: NativeTimelineItem) -> dict[str, Any]:
    data = item.to_json_dict()
    data["sequence_cursor"] = item.cursor
    data["cursor"] = int(item.id)
    payload = dict(data.get("payload") or {})
    payload.pop("delta", None)
    payload["text"] = item.text
    data["payload"] = payload
    return data


def native_conversation_item_display_event(item: NativeTimelineItem) -> dict[str, Any]:
    payload = dict(item.payload)
    payload["text"] = item.text
    payload.setdefault("itemId", item.item_key)
    payload.setdefault("item_id", item.item_key)
    payload.setdefault("native_turn_id", item.turn_key)
    payload["status"] = item.status
    payload["role"] = item.role
    payload["message_snapshot"] = True
    kind = item.kind
    if item.kind == "message":
        kind = "message_completed" if item.status == "completed" else "text_delta"
    return {
        "id": int(item.id),
        "sequence": item.cursor,
        "type": kind,
        "source_type": "native.conversation.item",
        "kind": kind,
        "role": item.role,
        "visible": True,
        "provider": item.provider,
        "native_thread_id": item.native_thread_id,
        "occurred_at": item.updated_at,
        "payload": payload,
    }


def native_messages_run_state(items: list[NativeTimelineItem]) -> dict[str, Any]:
    active_items = [
        item
        for item in items
        if str(item.status or "").strip().lower() in ACTIVE_TURN_STATUSES
        and item.kind != "user_message"
    ]
    if not active_items:
        return {"active": False, "status": "idle", "active_turn_id": ""}
    latest = active_items[-1]
    return {
        "active": True,
        "status": str(latest.status or "streaming"),
        "active_turn_id": latest.turn_key,
        "item_id": latest.id,
    }


def native_timeline_display_event(event: NativeTimelineEvent) -> dict[str, Any]:
    if hasattr(event, "to_display_json_dict"):
        return event.to_display_json_dict()
    data = event.to_json_dict()
    data.setdefault("source_type", data.get("type"))
    data["type"] = data.get("kind", data.get("type"))
    return data


def is_visible_native_timeline_event(event: NativeTimelineEvent) -> bool:
    return bool(native_timeline_display_event(event).get("visible"))


def native_timeline_display_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    source_type_counts: dict[str, int] = {}
    hidden_by_reason: dict[str, int] = {}
    latest_sequence = 0
    latest_visible_sequence = 0
    visible_event_count = 0
    for event in events:
        sequence = _safe_int(event.get("sequence", event.get("id", 0)), default=0)
        latest_sequence = max(latest_sequence, sequence)
        source_type = str(event.get("source_type") or event.get("type") or "")
        if source_type:
            source_type_counts[source_type] = source_type_counts.get(source_type, 0) + 1
        if event.get("visible") is False:
            reason = str(event.get("hidden_reason") or "not_visible")
            hidden_by_reason[reason] = hidden_by_reason.get(reason, 0) + 1
            continue
        visible_event_count += 1
        latest_visible_sequence = max(latest_visible_sequence, sequence)
    return {
        "visible_event_count": visible_event_count,
        "hidden_event_count_by_reason": hidden_by_reason,
        "latest_sequence": latest_sequence,
        "latest_visible_sequence": latest_visible_sequence,
        "source_type_counts": source_type_counts,
    }


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
