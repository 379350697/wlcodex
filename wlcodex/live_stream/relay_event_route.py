"""Read-only Relay task-event route and SSE handoff."""

from __future__ import annotations

from typing import Any


async def handle_relay_task_events_route(
    *,
    suffix: str,
    task_id: int,
    method: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    writer: Any,
    service: Any,
    hub: Any,
    safe_int: Any,
    send_json: Any,
    send_sse: Any,
) -> bool:
    if suffix != "/events":
        return False
    if method != "GET":
        await send_json(writer, 405, {"error": "method not allowed"})
        return True
    after = safe_int(query.get("after", ["0"])[0], default=0)
    live = "text/event-stream" in headers.get("accept", "").lower()
    relay_event_queue = service.subscribe_events(task_id) if live else None
    try:
        events = service.events_for_task(task_id, after=after)
        detail = service.get_task_readonly(task_id)
    except Exception:
        if relay_event_queue is not None:
            service.unsubscribe_events(task_id, relay_event_queue)
        raise
    await send_sse(
        writer,
        events,
        detail=detail,
        hub=hub,
        relay_service=service,
        relay_event_queue=relay_event_queue,
        live=live,
    )
    return True
