"""Pure Relay SSE projections shared by the stream transport and test surfaces.

This module deliberately contains no server or persistence dependency.  Reading a
Relay stream must be able to compact and annotate events without acquiring a
database write path or importing the HTTP server.
"""

from __future__ import annotations

import asyncio
from typing import Any

from wlcodex.live_stream.models import WorkerStreamEvent
from wlcodex.relay.marvis_interaction import project_relay_event_to_marvis_typed_event


def offer_json_queue(
    queue: asyncio.Queue[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    """Offer the newest state to a bounded queue, discarding only stale state."""
    try:
        queue.put_nowait(payload)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        pass


def relay_active_worker_jobs(detail: Any | None) -> list[Any]:
    if detail is None:
        return []
    active_statuses = {"queued", "streaming", "waiting"}
    jobs: list[Any] = []
    for job in getattr(detail, "role_jobs", []) or []:
        if getattr(job, "agent_run_id", None) is None:
            continue
        status = str(getattr(job, "status", "") or "").strip().lower()
        if status in active_statuses or bool(getattr(job, "turn_running", False)):
            jobs.append(job)
    return jobs


def compact_relay_sse_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type != "role.native_event":
        return with_marvis_typed_event(event_type, payload)
    compacted = dict(payload)
    nested = compacted.get("payload")
    if isinstance(nested, dict) and (
        "native_event" in nested or "payload" in nested or "runtime_event_id" in nested
    ):
        compacted["payload"] = compact_relay_native_event_payload(nested)
        return compacted
    return compact_relay_native_event_payload(compacted)


def with_marvis_typed_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    marvis_event = project_relay_event_to_marvis_typed_event(event_type, payload)
    if marvis_event is None:
        return payload
    compacted = dict(payload)
    compacted.setdefault("marvis_event", marvis_event)
    nested = compacted.get("payload")
    if isinstance(nested, dict):
        nested_payload = dict(nested)
        nested_payload.setdefault("marvis_event", marvis_event)
        compacted["payload"] = nested_payload
    return compacted


def compact_relay_native_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    native_event = payload.get("native_event")
    native_payload = native_event.get("payload") if isinstance(native_event, dict) else None
    raw_payload = payload.get("payload")
    source_payload = raw_payload if isinstance(raw_payload, dict) else {}
    if isinstance(native_payload, dict):
        source_payload = {**native_payload, **source_payload}
    for key in (
        "role",
        "agent_run_id",
        "runtime_event_id",
        "round_id",
        "kind",
        "itemId",
        "item_id",
        "stream_key",
        "native_message_id",
        "message_id",
        "native_turn_id",
        "turnId",
        "turn_id",
        "active_turn_id",
    ):
        value = payload.get(key)
        if value in (None, "") and isinstance(native_event, dict):
            if key == "runtime_event_id":
                value = native_event.get("id")
            else:
                value = native_event.get(key)
        if value in (None, ""):
            value = source_payload.get(key)
        if value not in (None, ""):
            compacted[key] = value
    kind = str(compacted.get("kind") or "").strip()
    text = relay_native_display_text(source_payload)
    if text:
        if kind in {"text_delta", "reasoning_delta", "command_output"}:
            compacted["delta"] = text
        else:
            compacted["text"] = text
    for key in (
        "status",
        "title",
        "command",
        "exit_code",
        "approval_id",
        "request_id",
        "codexRequestId",
        "provider",
    ):
        value = source_payload.get(key)
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            compacted.setdefault(key, value)
    return compacted


def relay_native_display_text(payload: dict[str, Any]) -> str:
    for key in ("delta", "text", "summary", "content", "message", "output", "chunk", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def relay_worker_payload(
    task_id: int,
    role: str,
    worker_event: WorkerStreamEvent,
) -> tuple[str, dict[str, Any]]:
    return "role.native_event", {
        "event_type": "role.native_event",
        "task_id": task_id,
        "role": role,
        "runtime_event_id": worker_event.id,
        "agent_run_id": worker_event.agent_run_id,
        "kind": worker_event.kind,
        "payload": dict(worker_event.payload),
        "native_event": worker_event.to_json_dict(),
    }
