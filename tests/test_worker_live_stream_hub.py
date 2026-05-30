from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.live_stream.hub import WorkerLiveStreamHub
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


def _event(
    agent_run_id: int, event_type: str, payload: dict, *, event_id: int = 0
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
        id=event_id,
    )


def test_snapshot_returns_cursor_filtered_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append(_event(42, EventType.MODEL_TEXT_DELTA, {"delta": "a"}))
    second = store.append(_event(42, EventType.MODEL_TEXT_DELTA, {"delta": "b"}))
    store.append(_event(99, EventType.MODEL_TEXT_DELTA, {"delta": "other"}))
    hub = WorkerLiveStreamHub(store)

    events = hub.snapshot(agent_run_id=42, after_id=first.id, limit=100)

    assert [event.id for event in events] == [second.id]
    assert events[0].payload == {"delta": "b"}


def test_tail_snapshot_returns_recent_events_and_previous_count(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = [
        store.append(_event(42, EventType.MODEL_TEXT_DELTA, {"delta": str(index)}))
        for index in range(5)
    ]
    store.append(_event(99, EventType.MODEL_TEXT_DELTA, {"delta": "other"}))
    hub = WorkerLiveStreamHub(store)

    snapshot = hub.snapshot_tail(agent_run_id=42, limit=2)

    assert [event.id for event in snapshot.events] == [saved[3].id, saved[4].id]
    assert snapshot.previous_event_count == 3


def test_before_snapshot_returns_previous_page_and_remaining_count(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    saved = [
        store.append(_event(42, EventType.MODEL_TEXT_DELTA, {"delta": str(index)}))
        for index in range(5)
    ]
    hub = WorkerLiveStreamHub(store)

    snapshot = hub.snapshot_before(agent_run_id=42, before_id=saved[3].id, limit=2)

    assert [event.id for event in snapshot.events] == [saved[1].id, saved[2].id]
    assert snapshot.previous_event_count == 1


@pytest.mark.asyncio
async def test_subscriber_receives_live_event_for_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hub = WorkerLiveStreamHub(store)
    queue = hub.subscribe(agent_run_id=42)

    stored = store.append(_event(42, EventType.MODEL_TEXT_DELTA, {"delta": "live"}))
    hub.publish(stored)

    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.id == stored.id
    assert received.kind == "text_delta"
    assert received.payload == {"delta": "live"}


@pytest.mark.asyncio
async def test_subscriber_ignores_other_agent_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hub = WorkerLiveStreamHub(store)
    queue = hub.subscribe(agent_run_id=42)

    stored = store.append(_event(99, EventType.MODEL_TEXT_DELTA, {"delta": "other"}))
    hub.publish(stored)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.05)


def test_unsubscribe_removes_queue(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hub = WorkerLiveStreamHub(store)
    queue = hub.subscribe(agent_run_id=42)

    hub.unsubscribe(agent_run_id=42, queue=queue)

    assert hub.subscriber_count(agent_run_id=42) == 0
