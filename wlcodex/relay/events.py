from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RelayEvent:
    task_id: int
    event_type: str
    sequence: int
    role: str = ""
    job_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RelayEventBus:
    def __init__(self) -> None:
        self._events_by_task: dict[int, list[RelayEvent]] = {}
        self._subscribers: dict[int, set[asyncio.Queue[RelayEvent]]] = {}
        self._projectors: list[Callable[[RelayEvent], None]] = []

    def emit(
        self,
        task_id: int,
        event_type: str,
        *,
        role: str = "",
        job_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RelayEvent:
        events = self._events_by_task.setdefault(task_id, [])
        event = RelayEvent(
            task_id=task_id,
            event_type=event_type,
            sequence=len(events) + 1,
            role=role,
            job_id=job_id,
            payload=dict(payload or {}),
        )
        events.append(event)
        for projector in list(self._projectors):
            try:
                projector(event)
            except Exception:
                pass
        for queue in list(self._subscribers.get(task_id, ())):
            self._offer(queue, event)
        return event

    def list_events(self, task_id: int, *, after: int = 0) -> list[RelayEvent]:
        return [
            event
            for event in self._events_by_task.get(task_id, [])
            if event.sequence > after
        ]

    def subscribe(self, task_id: int) -> asyncio.Queue[RelayEvent]:
        queue: asyncio.Queue[RelayEvent] = asyncio.Queue(maxsize=200)
        self._subscribers.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe(self, task_id: int, queue: asyncio.Queue[RelayEvent]) -> None:
        queues = self._subscribers.get(task_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(task_id, None)

    def add_projector(self, projector: Callable[[RelayEvent], None]) -> None:
        if projector not in self._projectors:
            self._projectors.append(projector)

    def remove_projector(self, projector: Callable[[RelayEvent], None]) -> None:
        try:
            self._projectors.remove(projector)
        except ValueError:
            pass

    @staticmethod
    def _offer(queue: asyncio.Queue[RelayEvent], event: RelayEvent) -> None:
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
