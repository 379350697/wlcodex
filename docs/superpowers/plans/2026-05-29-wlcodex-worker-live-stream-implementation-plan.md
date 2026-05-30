# WLCodex Worker Live Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a disabled-by-default local Worker Live Stream server that streams existing WLCodex runtime events for one `agent_run_id` to a browser using cursor-based SSE.

**Architecture:** Add a small `wlcodex.live_stream` package that sits beside the existing Telegram surfaces. It reads historical events from `RuntimeEventStore`, receives live events through the existing `RuntimeEventStore.add_projector` boundary, normalizes them to UI-safe worker stream frames, and exposes loopback HTTP/SSE endpoints when `[live_stream].enabled = true`.

**Tech Stack:** Python 3.12, stdlib `asyncio`, stdlib `json`, existing SQLite-backed `RuntimeEventStore`, existing WLCodex `RuntimeEvent` schema, pytest.

---

## Scope

Implement only the deterministic live-stream substrate.

Do not implement:

- full office overview;
- relay/cloud access;
- authentication;
- worker steering;
- approval resolution from web;
- Antigravity integration;
- rich diff viewer;
- model-generated summaries.

## File Structure

Create:

- `wlcodex/live_stream/__init__.py`  
  Package exports.

- `wlcodex/live_stream/models.py`  
  Pure dataclasses and event normalization helpers. No I/O.

- `wlcodex/live_stream/hub.py`  
  In-process snapshot and live fan-out for worker events.

- `wlcodex/live_stream/server.py`  
  Minimal loopback HTTP/SSE server.

- `tests/test_worker_live_stream_models.py`  
  Pure unit tests for event normalization.

- `tests/test_worker_live_stream_hub.py`  
  Unit tests for snapshot, cursor filtering, and live fan-out.

- `tests/test_worker_live_stream_server.py`  
  Async tests for HTTP health, snapshot JSON, SSE formatting, and unsafe bind rejection.

Modify:

- `wlcodex/config.py`  
  Add `[live_stream]` config section with disabled-by-default loopback settings.

- `wlcodex/main.py`  
  Wire the live stream hub and server into app startup only when enabled.

- `wlcodex/runtime_event_store.py`  
  Add cursor query by `agent_run_id`.

- `config/wlcodex.example.toml`  
  Document disabled-by-default live stream config.

- `README.md`  
  Add a short operator note for local Worker Live Stream.

## Task 1: Runtime Event Cursor Query

**Files:**

- Modify: `wlcodex/runtime_event_store.py`
- Test: `tests/test_runtime_event_store.py`

- [ ] **Step 1: Write failing tests for agent-run cursor queries**

Add these tests near the existing `list_by_agent_run` tests in `tests/test_runtime_event_store.py`:

```python
def test_list_by_agent_run_after_returns_events_after_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append(_make_event(agent_run_id=42, payload={"n": 1}))
    second = store.append(_make_event(agent_run_id=42, payload={"n": 2}))
    store.append(_make_event(agent_run_id=99, payload={"n": "other"}))
    third = store.append(_make_event(agent_run_id=42, payload={"n": 3}))

    events = store.list_by_agent_run_after(42, after_id=first.id, limit=20)

    assert [event.id for event in events] == [second.id, third.id]
    assert [event.payload["n"] for event in events] == [2, 3]


def test_list_by_agent_run_after_respects_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        store.append(_make_event(agent_run_id=42, payload={"n": index}))

    events = store.list_by_agent_run_after(42, after_id=0, limit=2)

    assert len(events) == 2
    assert [event.payload["n"] for event in events] == [0, 1]


def test_list_by_agent_run_after_rejects_non_positive_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)

    events = store.list_by_agent_run_after(42, after_id=0, limit=0)

    assert events == []
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_runtime_event_store.py::test_list_by_agent_run_after_returns_events_after_cursor tests/test_runtime_event_store.py::test_list_by_agent_run_after_respects_limit tests/test_runtime_event_store.py::test_list_by_agent_run_after_rejects_non_positive_limit -q
```

Expected: fail with `AttributeError: 'RuntimeEventStore' object has no attribute 'list_by_agent_run_after'`.

- [ ] **Step 3: Add cursor query implementation**

Add this method to `RuntimeEventStore` after `list_by_agent_run`:

```python
    def list_by_agent_run_after(
        self,
        agent_run_id: int,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[RuntimeEvent]:
        """Events for a specific agent run after a runtime event id.

        Ordered by id ascending so clients can use the last event id as a
        reconnect cursor.
        """
        if limit <= 0:
            return []
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE agent_run_id = ?
              AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (agent_run_id, after_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_runtime_event_store.py::test_list_by_agent_run_after_returns_events_after_cursor tests/test_runtime_event_store.py::test_list_by_agent_run_after_respects_limit tests/test_runtime_event_store.py::test_list_by_agent_run_after_rejects_non_positive_limit -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/runtime_event_store.py tests/test_runtime_event_store.py
git commit -m "feat: add runtime event cursor query"
```

## Task 2: Worker Stream Event Normalization

**Files:**

- Create: `wlcodex/live_stream/__init__.py`
- Create: `wlcodex/live_stream/models.py`
- Test: `tests/test_worker_live_stream_models.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_worker_live_stream_models.py`:

```python
from __future__ import annotations

from wlcodex.live_stream.models import stream_event_from_runtime
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


def _event(event_type: str, payload: dict, *, event_id: int = 10) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id="42",
        correlation_id="corr-live",
        source=EventSource.CODEX,
        actor="codex",
        visibility=Visibility.USER,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=7,
        agent_run_id=42,
        id=event_id,
    )


def test_model_text_delta_maps_to_text_delta_kind() -> None:
    runtime = _event(EventType.MODEL_TEXT_DELTA, {"delta": "hello"}, event_id=11)

    stream = stream_event_from_runtime(runtime)

    assert stream.id == 11
    assert stream.type == EventType.MODEL_TEXT_DELTA
    assert stream.kind == "text_delta"
    assert stream.agent_run_id == 42
    assert stream.conversation_id == 7
    assert stream.payload == {"delta": "hello"}


def test_command_output_maps_to_command_output_kind() -> None:
    runtime = _event(EventType.COMMAND_OUTPUT_DELTA, {"delta": "pytest"}, event_id=12)

    stream = stream_event_from_runtime(runtime)

    assert stream.kind == "command_output"
    assert stream.payload["delta"] == "pytest"


def test_to_json_dict_keeps_runtime_metadata() -> None:
    runtime = _event(EventType.APPROVAL_REQUESTED, {"summary": "needs approval"}, event_id=13)

    data = stream_event_from_runtime(runtime).to_json_dict()

    assert data["id"] == 13
    assert data["type"] == EventType.APPROVAL_REQUESTED
    assert data["kind"] == "approval_requested"
    assert data["source"] == EventSource.CODEX
    assert data["actor"] == "codex"
    assert data["visibility"] == Visibility.USER
    assert data["payload"] == {"summary": "needs approval"}
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_models.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'wlcodex.live_stream'`.

- [ ] **Step 3: Create live stream package and model code**

Create `wlcodex/live_stream/__init__.py`:

```python
"""Local worker live stream support."""

from wlcodex.live_stream.models import WorkerStreamEvent, stream_event_from_runtime

__all__ = ["WorkerStreamEvent", "stream_event_from_runtime"]
```

Create `wlcodex/live_stream/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wlcodex.runtime_events import EventType, RuntimeEvent


_KIND_BY_EVENT_TYPE = {
    EventType.AGENT_RUN_STARTED: "lifecycle",
    EventType.AGENT_RUN_ACTIVITY: "activity",
    EventType.MODEL_TEXT_DELTA: "text_delta",
    EventType.MODEL_REASONING_DELTA: "reasoning_delta",
    EventType.COMMAND_STARTED: "command_started",
    EventType.COMMAND_OUTPUT_DELTA: "command_output",
    EventType.COMMAND_COMPLETED: "command_completed",
    EventType.COMMAND_FAILED: "command_failed",
    EventType.FILE_CHANGED: "file_changed",
    EventType.DIFF_UPDATED: "diff_updated",
    EventType.APPROVAL_REQUESTED: "approval_requested",
    EventType.APPROVAL_RESOLVED: "approval_resolved",
    EventType.AGENT_RUN_COMPLETED: "completed",
    EventType.AGENT_RUN_FAILED: "failed",
}


@dataclass(frozen=True)
class WorkerStreamEvent:
    id: int
    type: str
    kind: str
    agent_run_id: int | None
    conversation_id: int | None
    occurred_at: str
    source: str
    actor: str
    visibility: str
    payload: dict[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "kind": self.kind,
            "agent_run_id": self.agent_run_id,
            "conversation_id": self.conversation_id,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "actor": self.actor,
            "visibility": self.visibility,
            "payload": self.payload,
        }


def stream_event_from_runtime(event: RuntimeEvent) -> WorkerStreamEvent:
    return WorkerStreamEvent(
        id=event.id,
        type=event.event_type,
        kind=_KIND_BY_EVENT_TYPE.get(event.event_type, "event"),
        agent_run_id=event.agent_run_id,
        conversation_id=event.conversation_id,
        occurred_at=event.occurred_at,
        source=str(event.source),
        actor=str(event.actor),
        visibility=str(event.visibility),
        payload=dict(event.payload),
    )
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_models.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/live_stream/__init__.py wlcodex/live_stream/models.py tests/test_worker_live_stream_models.py
git commit -m "feat: normalize worker live stream events"
```

## Task 3: Worker Live Stream Hub

**Files:**

- Create: `wlcodex/live_stream/hub.py`
- Modify: `wlcodex/live_stream/__init__.py`
- Test: `tests/test_worker_live_stream_hub.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_worker_live_stream_hub.py`:

```python
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


def _event(agent_run_id: int, event_type: str, payload: dict, *, event_id: int = 0) -> RuntimeEvent:
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
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_hub.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'wlcodex.live_stream.hub'`.

- [ ] **Step 3: Implement hub**

Create `wlcodex/live_stream/hub.py`:

```python
from __future__ import annotations

import asyncio
from collections import defaultdict

from wlcodex.live_stream.models import WorkerStreamEvent, stream_event_from_runtime
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import RuntimeEvent


class WorkerLiveStreamHub:
    """Snapshot and live fan-out for one worker/agent run."""

    def __init__(self, store: RuntimeEventStore) -> None:
        self._store = store
        self._subscribers: dict[int, set[asyncio.Queue[WorkerStreamEvent]]] = defaultdict(set)

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
```

Modify `wlcodex/live_stream/__init__.py`:

```python
"""Local worker live stream support."""

from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.models import WorkerStreamEvent, stream_event_from_runtime

__all__ = ["WorkerLiveStreamHub", "WorkerStreamEvent", "stream_event_from_runtime"]
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_hub.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/live_stream/__init__.py wlcodex/live_stream/hub.py tests/test_worker_live_stream_hub.py
git commit -m "feat: add worker live stream hub"
```

## Task 4: Config Section

**Files:**

- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config.py` near other config default tests:

```python
def _write_live_stream_config(tmp_path, *, live_stream_block: str = ""):
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "/tmp/wlcodex"

{live_stream_block}
""".strip(),
        encoding="utf-8",
    )
    return config_path


def test_live_stream_config_defaults_disabled(tmp_path):
    config_path = _write_live_stream_config(tmp_path)
    config = load_config(config_path)

    assert config.live_stream.enabled is False
    assert config.live_stream.host == "127.0.0.1"
    assert config.live_stream.port == 18731


def test_live_stream_config_can_be_enabled(tmp_path):
    config_path = _write_live_stream_config(
        tmp_path,
        live_stream_block="""
[live_stream]
enabled = true
host = "127.0.0.1"
port = 18732
""",
    )

    config = load_config(config_path)

    assert config.live_stream.enabled is True
    assert config.live_stream.host == "127.0.0.1"
    assert config.live_stream.port == 18732


def test_live_stream_config_rejects_non_loopback_host(tmp_path):
    config_path = _write_live_stream_config(
        tmp_path,
        live_stream_block="""
[live_stream]
enabled = true
host = "0.0.0.0"
port = 18731
""",
    )

    with pytest.raises(ConfigError, match="live_stream.host"):
        load_config(config_path)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py -q
```

Expected: fail because `AppConfig` has no `live_stream`.

- [ ] **Step 3: Add config dataclass and parser**

In `wlcodex/config.py`, add:

```python
@dataclass(frozen=True)
class LiveStreamConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 18731
```

Add field to `AppConfig`:

```python
    live_stream: LiveStreamConfig = LiveStreamConfig()
```

Inside `load_config`, add:

```python
    live_stream_raw = data.get("live_stream", {})
```

Then include in returned `AppConfig`:

```python
        live_stream=_live_stream_config(live_stream_raw),
```

Add helper:

```python
def _live_stream_config(data: dict[str, object]) -> LiveStreamConfig:
    host = str(data.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost"):
        raise ConfigError(
            f"live_stream.host must be loopback-only in this release, got: {host!r}"
        )
    port = int(data.get("port", 18731))
    if port <= 0 or port > 65535:
        raise ConfigError(f"live_stream.port must be 1-65535, got: {port}")
    return LiveStreamConfig(
        enabled=bool(data.get("enabled", False)),
        host=host,
        port=port,
    )
```

In `config/wlcodex.example.toml`, add near interaction/output config:

```toml
[live_stream]
# Local Worker Live Stream web/SSE server. Disabled by default.
# First release is loopback-only; use SSH/Tailscale forwarding for phone tests.
enabled = false
host = "127.0.0.1"
port = 18731
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py -q
```

Expected: all config tests pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/config.py config/wlcodex.example.toml tests/test_config.py
git commit -m "feat: add live stream config"
```

## Task 5: Local HTTP/SSE Server

**Files:**

- Create: `wlcodex/live_stream/server.py`
- Modify: `wlcodex/live_stream/__init__.py`
- Test: `tests/test_worker_live_stream_server.py`

- [ ] **Step 1: Write failing server tests**

Create `tests/test_worker_live_stream_server.py`:

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer, format_sse_event
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


def _event(agent_run_id: int, payload: dict) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=EventType.MODEL_TEXT_DELTA,
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
    )


async def _read_response(host: str, port: int, request: str) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request.encode("utf-8"))
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=1.0)
    writer.close()
    await writer.wait_closed()
    return data.decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_health_endpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /health HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert '"status": "ok"' in response


@pytest.mark.asyncio
async def test_snapshot_endpoint_returns_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.append(_event(42, {"delta": "hello"}))
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?after=0&limit=10 HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    body = response.split("\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert payload["events"][0]["id"] == saved.id
    assert payload["events"][0]["kind"] == "text_delta"
    assert payload["events"][0]["payload"] == {"delta": "hello"}


@pytest.mark.asyncio
async def test_live_page_endpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Worker Live Stream" in response
    assert "/api/workers/42/stream" in response


def test_server_rejects_non_loopback_host(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="loopback"):
        WorkerLiveStreamServer(
            host="0.0.0.0",
            port=18731,
            hub=WorkerLiveStreamHub(store),
        )


def test_format_sse_event_uses_event_id_and_json_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.append(_event(42, {"delta": "hello"}))
    hub = WorkerLiveStreamHub(store)
    event = hub.snapshot(agent_run_id=42, after_id=0, limit=1)[0]

    raw = format_sse_event(event).decode("utf-8")

    assert raw.startswith(f"id: {saved.id}\n")
    assert "event: text_delta\n" in raw
    assert '"kind": "text_delta"' in raw
    assert raw.endswith("\n\n")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_server.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'wlcodex.live_stream.server'`.

- [ ] **Step 3: Implement minimal HTTP server**

Create `wlcodex/live_stream/server.py`:

```python
from __future__ import annotations

import asyncio
import json
from html import escape
from urllib.parse import parse_qs, urlparse

from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.models import WorkerStreamEvent


class WorkerLiveStreamServer:
    def __init__(self, *, host: str, port: int, hub: WorkerLiveStreamHub) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError(f"Worker live stream server is loopback-only, got {host!r}")
        self.host = host
        self.port = port
        self._hub = hub
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.host,
            port=self.port,
        )
        socket = self._server.sockets[0]
        self.port = int(socket.getsockname()[1])

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return
            method, target, _version = request_line.decode("utf-8", errors="replace").strip().split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if ":" in decoded:
                    name, value = decoded.split(":", 1)
                    headers[name.lower()] = value.strip()
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            parsed = urlparse(target)
            if parsed.path == "/health":
                await self._send_json(writer, 200, {"status": "ok", "service": "worker-live-stream"})
                return
            agent_id = _agent_id_from_path(parsed.path, prefix="/api/workers/", suffix="/events")
            if agent_id is not None:
                query = parse_qs(parsed.query)
                after = _safe_int(query.get("after", ["0"])[0], default=0)
                limit = _safe_int(query.get("limit", ["500"])[0], default=500)
                events = self._hub.snapshot(agent_run_id=agent_id, after_id=after, limit=limit)
                await self._send_json(writer, 200, {"agent_run_id": agent_id, "events": [e.to_json_dict() for e in events]})
                return
            agent_id = _agent_id_from_path(parsed.path, prefix="/workers/", suffix="/live")
            if agent_id is not None:
                await self._send_html(writer, 200, _live_page(agent_id))
                return
            agent_id = _agent_id_from_path(parsed.path, prefix="/api/workers/", suffix="/stream")
            if agent_id is not None:
                query = parse_qs(parsed.query)
                after = _safe_int(
                    query.get("after", [headers.get("last-event-id", "0")])[0],
                    default=0,
                )
                await self._send_sse(writer, agent_id, after)
                return
            await self._send_json(writer, 404, {"error": "not found"})
        except Exception as exc:
            if not writer.is_closing():
                await self._send_json(writer, 500, {"error": type(exc).__name__})

    async def _send_json(self, writer: asyncio.StreamWriter, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await _send_response(writer, status, "application/json; charset=utf-8", body)

    async def _send_html(self, writer: asyncio.StreamWriter, status: int, body: str) -> None:
        await _send_response(writer, status, "text/html; charset=utf-8", body.encode("utf-8"))

    async def _send_sse(
        self,
        writer: asyncio.StreamWriter,
        agent_run_id: int,
        after_id: int,
    ) -> None:
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(header.encode("utf-8"))
        await writer.drain()
        latest = after_id
        for event in self._hub.snapshot(agent_run_id=agent_run_id, after_id=after_id, limit=500):
            latest = event.id
            await _write_sse(writer, event)
        queue = self._hub.subscribe(agent_run_id=agent_run_id)
        try:
            while not writer.is_closing():
                event = await queue.get()
                if event.id <= latest:
                    continue
                latest = event.id
                await _write_sse(writer, event)
        finally:
            self._hub.unsubscribe(agent_run_id=agent_run_id, queue=queue)


async def _send_response(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "Error")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    writer.write(header.encode("utf-8") + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _write_sse(writer: asyncio.StreamWriter, event: WorkerStreamEvent) -> None:
    writer.write(format_sse_event(event))
    await writer.drain()


def format_sse_event(event: WorkerStreamEvent) -> bytes:
    payload = json.dumps(event.to_json_dict(), ensure_ascii=False)
    return f"id: {event.id}\nevent: {event.kind}\ndata: {payload}\n\n".encode("utf-8")


def _agent_id_from_path(path: str, *, prefix: str, suffix: str) -> int | None:
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    raw = path[len(prefix): -len(suffix)]
    if not raw.isdigit():
        return None
    return int(raw)


def _safe_int(raw: str, *, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _live_page(agent_run_id: int) -> str:
    stream_path = f"/api/workers/{agent_run_id}/stream"
    safe_title = escape(f"Worker Live Stream #{agent_run_id}")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; background: #101114; color: #f4f4f5; }}
    header {{ position: sticky; top: 0; padding: 12px 16px; background: #191b20; border-bottom: 1px solid #30333a; }}
    main {{ padding: 12px; }}
    .event {{ white-space: pre-wrap; border-bottom: 1px solid #30333a; padding: 10px 4px; }}
    .meta {{ color: #a1a1aa; font-size: 12px; margin-bottom: 4px; }}
    .approval_requested {{ color: #facc15; }}
    .failed {{ color: #f87171; }}
    .completed {{ color: #86efac; }}
  </style>
</head>
<body>
  <header>
    <strong>Worker Live Stream</strong>
    <span id="state">connecting</span>
    <span id="cursor"></span>
  </header>
  <main id="events"></main>
  <script>
    const state = document.getElementById("state");
    const cursor = document.getElementById("cursor");
    const events = document.getElementById("events");
    const source = new EventSource("{stream_path}");
    source.onopen = () => {{ state.textContent = "connected"; }};
    source.onerror = () => {{ state.textContent = "reconnecting"; }};
    source.onmessage = (message) => render(JSON.parse(message.data));
    [
      "lifecycle",
      "activity",
      "text_delta",
      "reasoning_delta",
      "command_started",
      "command_output",
      "command_completed",
      "command_failed",
      "file_changed",
      "diff_updated",
      "approval_requested",
      "approval_resolved",
      "completed",
      "failed",
      "event"
    ].forEach(kind => {{
      source.addEventListener(kind, message => render(JSON.parse(message.data)));
    }});
    function render(event) {{
      cursor.textContent = " last event " + event.id;
      const row = document.createElement("div");
      row.className = "event " + event.kind;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = "#" + event.id + " " + event.kind + " " + event.type;
      const body = document.createElement("div");
      body.textContent = event.payload.delta || event.payload.summary || JSON.stringify(event.payload, null, 2);
      row.append(meta, body);
      events.append(row);
      window.scrollTo(0, document.body.scrollHeight);
    }}
  </script>
</body>
</html>"""
```

Modify `wlcodex/live_stream/__init__.py`:

```python
"""Local worker live stream support."""

from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.models import WorkerStreamEvent, stream_event_from_runtime
from wlcodex.live_stream.server import WorkerLiveStreamServer

__all__ = [
    "WorkerLiveStreamHub",
    "WorkerLiveStreamServer",
    "WorkerStreamEvent",
    "stream_event_from_runtime",
]
```

- [ ] **Step 4: Run server tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_server.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/live_stream/__init__.py wlcodex/live_stream/server.py tests/test_worker_live_stream_server.py
git commit -m "feat: serve local worker live stream"
```

## Task 6: Main Wiring

**Files:**

- Modify: `wlcodex/main.py`
- Test: `tests/test_main_composition.py`

- [ ] **Step 1: Write failing composition tests**

Add tests to `tests/test_main_composition.py`:

```python
def test_create_live_stream_components_disabled_returns_none(tmp_path):
    from types import SimpleNamespace
    from wlcodex.main import _create_live_stream_components
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    config = SimpleNamespace(
        live_stream=SimpleNamespace(enabled=False, host="127.0.0.1", port=18731)
    )

    assert _create_live_stream_components(config, store) is None


def test_create_live_stream_components_enabled_registers_projector(tmp_path):
    from types import SimpleNamespace
    from wlcodex.main import _create_live_stream_components
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    config = SimpleNamespace(
        live_stream=SimpleNamespace(enabled=True, host="127.0.0.1", port=0)
    )

    components = _create_live_stream_components(config, store)

    assert components is not None
    assert components.server.host == "127.0.0.1"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py::test_create_live_stream_components_disabled_returns_none tests/test_main_composition.py::test_create_live_stream_components_enabled_registers_projector -q
```

Expected: fail because `_create_live_stream_components` is missing.

- [ ] **Step 3: Add composition helper and lifecycle wiring**

In `wlcodex/main.py`, add near `_create_terminal_manager`:

```python
def _create_live_stream_components(config: object, runtime_store: object) -> object | None:
    """Create Worker Live Stream components when enabled."""
    live_stream = getattr(config, "live_stream", None)
    if live_stream is None or not getattr(live_stream, "enabled", False):
        return None

    from types import SimpleNamespace
    from wlcodex.live_stream import WorkerLiveStreamHub, WorkerLiveStreamServer

    hub = WorkerLiveStreamHub(runtime_store)
    runtime_store.add_projector(hub.publish)
    server = WorkerLiveStreamServer(
        host=str(getattr(live_stream, "host", "127.0.0.1")),
        port=int(getattr(live_stream, "port", 18731)),
        hub=hub,
    )
    return SimpleNamespace(hub=hub, server=server)
```

In `main()`, after `runtime_store.add_projector(runtime_projector.apply)`, add:

```python
    live_stream_components = _create_live_stream_components(config, runtime_store)
```

Inside `_run()`, before initializing Telegram, add:

```python
        live_stream_server = (
            live_stream_components.server
            if live_stream_components is not None
            else None
        )
        if live_stream_server is not None:
            await live_stream_server.start()
            logger.info(
                "Worker Live Stream listening at http://%s:%s",
                live_stream_server.host,
                live_stream_server.port,
            )
```

Inside `_run()` `finally`, before closing the event loop, add:

```python
            if live_stream_server is not None:
                await live_stream_server.stop()
```

Place `live_stream_server = None` before the `try:` if needed so the `finally`
scope is always defined.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py::test_create_live_stream_components_disabled_returns_none tests/test_main_composition.py::test_create_live_stream_components_enabled_registers_projector -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/main.py tests/test_main_composition.py
git commit -m "feat: wire worker live stream server"
```

## Task 7: Documentation And Focused Verification

**Files:**

- Modify: `README.md`
- Test: existing focused test set

- [ ] **Step 1: Update README**

Add a short section under the interaction or local setup area:

````markdown
## Worker Live Stream

WLCodex can expose a local browser stream for a single worker/agent run. This is
the first substrate for the future Virtual Engineering Office worker station.
It is disabled by default and binds to loopback only.

```toml
[live_stream]
enabled = true
host = "127.0.0.1"
port = 18731
```

After starting WLCodex, open:

```text
http://127.0.0.1:18731/workers/<agent_run_id>/live
```

The page streams existing redacted runtime events. It does not invoke another
model and does not add model token usage.
````

- [ ] **Step 2: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_models.py tests/test_worker_live_stream_hub.py tests/test_worker_live_stream_server.py tests/test_runtime_event_store.py::test_list_by_agent_run_after_returns_events_after_cursor tests/test_runtime_event_store.py::test_list_by_agent_run_after_respects_limit tests/test_runtime_event_store.py::test_list_by_agent_run_after_rejects_non_positive_limit tests/test_config.py tests/test_main_composition.py -q
```

Expected: all pass.

- [ ] **Step 3: Run import smoke**

Run:

```bash
.venv/bin/python -m pytest tests/test_imports.py -q
```

Expected: pass.

- [ ] **Step 4: Run lint**

Run:

```bash
.venv/bin/python -m ruff check wlcodex/live_stream wlcodex/config.py wlcodex/main.py tests/test_worker_live_stream_models.py tests/test_worker_live_stream_hub.py tests/test_worker_live_stream_server.py
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document worker live stream"
```

## Final Verification

After all tasks:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_models.py tests/test_worker_live_stream_hub.py tests/test_worker_live_stream_server.py tests/test_runtime_event_store.py tests/test_config.py tests/test_main_composition.py tests/test_imports.py -q
.venv/bin/python -m ruff check .
git status --short
```

Expected:

- focused pytest set passes;
- ruff passes;
- `git status --short` shows no uncommitted implementation changes except any intentionally untracked notes.

## Manual Smoke

1. Set `[live_stream].enabled = true` in a local config.
2. Start WLCodex with fake backend or real backend.
3. Create or identify an `agent_run_id` that has runtime events.
4. Open:

```text
http://127.0.0.1:18731/workers/<agent_run_id>/live
```

5. Start or continue work for that worker.
6. Confirm:

- historical events render after page load;
- new events append without page refresh;
- page reconnect does not duplicate all already-seen events when browser sends `Last-Event-ID`;
- no additional model call is made for the web page.

## Spec Coverage

This plan covers:

- runtime cursor query: Task 1;
- normalized stream event contract: Task 2;
- live fan-out: Task 3;
- disabled-by-default loopback config: Task 4;
- HTTP/SSE stream and minimal browser page: Task 5;
- WLCodex startup wiring: Task 6;
- README and verification: Task 7.

It intentionally leaves the office overview, relay, authentication, approvals
from web, and worker steering to later specs.
