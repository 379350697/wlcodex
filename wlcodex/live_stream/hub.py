from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from wlcodex.live_stream.models import WorkerStreamEvent, stream_event_from_runtime
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import RuntimeEvent


@dataclass(frozen=True)
class WorkerEventSnapshot:
    events: list[WorkerStreamEvent]
    previous_event_count: int = 0


class WorkerLiveStreamHub:
    """Snapshot and live fan-out for one worker/agent run."""

    def __init__(self, store: RuntimeEventStore) -> None:
        self._store = store
        self._subscribers: dict[int, set[asyncio.Queue[WorkerStreamEvent]]] = (
            defaultdict(set)
        )

    def snapshot(
        self,
        *,
        agent_run_id: int,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[WorkerStreamEvent]:
        events = self._store.list_by_agent_run_after(
            agent_run_id,
            after_id=after_id,
            limit=limit,
        )
        return [stream_event_from_runtime(event) for event in events]

    def snapshot_tail(
        self,
        *,
        agent_run_id: int,
        limit: int = 100,
    ) -> WorkerEventSnapshot:
        events = [
            stream_event_from_runtime(event)
            for event in self._store.list_by_agent_run_tail(
                agent_run_id,
                limit=limit,
            )
        ]
        first_id = events[0].id if events else 0
        previous_event_count = (
            self._store.count_by_agent_run_before(agent_run_id, before_id=first_id)
            if first_id
            else 0
        )
        return WorkerEventSnapshot(
            events=events,
            previous_event_count=previous_event_count,
        )

    def snapshot_before(
        self,
        *,
        agent_run_id: int,
        before_id: int,
        limit: int = 100,
    ) -> WorkerEventSnapshot:
        events = [
            stream_event_from_runtime(event)
            for event in self._store.list_by_agent_run_before(
                agent_run_id,
                before_id=before_id,
                limit=limit,
            )
        ]
        first_id = events[0].id if events else before_id
        return WorkerEventSnapshot(
            events=events,
            previous_event_count=self._store.count_by_agent_run_before(
                agent_run_id,
                before_id=first_id,
            ),
        )

    def subscribe(self, *, agent_run_id: int) -> asyncio.Queue[WorkerStreamEvent]:
        queue: asyncio.Queue[WorkerStreamEvent] = asyncio.Queue(maxsize=200)
        self._subscribers[agent_run_id].add(queue)
        return queue

    def unsubscribe(
        self,
        *,
        agent_run_id: int,
        queue: asyncio.Queue[WorkerStreamEvent],
    ) -> None:
        queues = self._subscribers.get(agent_run_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(agent_run_id, None)

    def publish(self, event: RuntimeEvent) -> None:
        agent_run_id = event.agent_run_id
        if agent_run_id is None:
            return
        stream_event = stream_event_from_runtime(event)
        for queue in list(self._subscribers.get(agent_run_id, ())):
            self._offer(queue, stream_event)

    def subscriber_count(self, *, agent_run_id: int) -> int:
        return len(self._subscribers.get(agent_run_id, ()))

    @staticmethod
    def _offer(
        queue: asyncio.Queue[WorkerStreamEvent],
        event: WorkerStreamEvent,
    ) -> None:
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
