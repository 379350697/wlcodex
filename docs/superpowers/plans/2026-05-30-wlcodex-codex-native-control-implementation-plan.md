# WLCodex Codex Native Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `Codex干活的` native-control station that lets a phone browser list, open, continue, steer, approve, and interrupt official Codex IDE sessions through the official app-server protocol.

**Architecture:** Keep WLCodex-owned task execution separate from official Codex native sessions. Add a dedicated native Codex client over app-server proxy/JSON-RPC, map official `threadId` values into WLCodex `agent_run_id` values through a `native_codex_sessions` table, project official Codex notifications into the existing `runtime_events` stream, and expose authenticated phone-safe HTTP controls from the existing Worker Live Stream server.

**Tech Stack:** Python asyncio, SQLite, existing `JsonRpcClient`, official Codex app-server JSON-RPC methods, existing `RuntimeEventStore`, existing Worker Live Stream HTTP/SSE server, pytest.

---

## Scope Guard

This plan implements only the native Codex control bridge. It does not build the full Virtual Engineering Office UI, Antigravity support, WebRTC voice, or a pixel-perfect copy of the official Codex mobile app.

The bridge is a control surface for the user's Mac. Any non-loopback use must be authenticated before it can call native Codex methods.

## File Structure

- Create: `wlcodex/codex_native/__init__.py`
  - Package export surface.
- Create: `wlcodex/codex_native/models.py`
  - Dataclasses for native status, sessions, turns, and control errors.
- Create: `wlcodex/codex_native/transport.py`
  - Async stdio transport for `codex app-server proxy`.
- Create: `wlcodex/codex_native/client.py`
  - Protocol client methods for native status/session/control operations.
- Create: `wlcodex/codex_native/session_store.py`
  - SQLite mapping between official `threadId` and WLCodex `agent_run_id`.
- Create: `wlcodex/codex_native/projector.py`
  - Notification-to-runtime-event projection for native Codex sessions.
- Create: `wlcodex/codex_native/controller.py`
  - Orchestrates client, session store, event projector, and control methods.
- Modify: `wlcodex/db.py`
  - Add `native_codex_sessions` migration.
- Modify: `wlcodex/config.py`
  - Add native Codex and live-stream auth configuration.
- Modify: `wlcodex/live_stream/server.py`
  - Add auth, POST parsing, native Codex routes, and `Codex干活的` page.
- Modify: `wlcodex/main.py`
  - Compose the native controller when enabled.
- Modify: `config/wlcodex.example.toml`
  - Document native control and auth settings.
- Test: `tests/test_codex_native_session_store.py`
- Test: `tests/test_codex_native_client.py`
- Test: `tests/test_codex_native_projector.py`
- Test: `tests/test_codex_native_controller.py`
- Test: `tests/test_worker_live_stream_native_routes.py`
- Test: `tests/test_config.py`
- Test: `tests/test_main_composition.py`

---

### Task 1: Add Native Codex Models And Session Store

**Files:**
- Create: `wlcodex/codex_native/__init__.py`
- Create: `wlcodex/codex_native/models.py`
- Create: `wlcodex/codex_native/session_store.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_codex_native_session_store.py`

- [ ] **Step 1: Write failing session-store tests**

Create `tests/test_codex_native_session_store.py`:

```python
from pathlib import Path

from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.db import Ledger


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return ledger


def test_native_session_creates_agent_run_once(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = NativeCodexSessionStore(ledger)

    first = store.get_or_create_session(
        native_thread_id="thread-native-1",
        title="检查部署后报错",
        cwd="/Users/wl/projects/wlcodex",
        source_kind="vscode",
        status="running",
    )
    second = store.get_or_create_session(
        native_thread_id="thread-native-1",
        title="检查部署后报错",
        cwd="/Users/wl/projects/wlcodex",
        source_kind="vscode",
        status="running",
    )

    assert second.id == first.id
    assert second.agent_run_id == first.agent_run_id
    agent_run = ledger.get_agent_run(first.agent_run_id)
    assert agent_run.agent == "codex"
    assert agent_run.role == "codex_native"
    assert agent_run.external_session_id == "thread-native-1"


def test_native_session_updates_last_turn_and_status(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = NativeCodexSessionStore(ledger)
    session = store.get_or_create_session(
        native_thread_id="thread-native-2",
        title="拉取项目到根目录",
        cwd="/Users/wl/projects/wlcodex",
        source_kind="cli",
        status="queued",
    )

    updated = store.update_session(
        native_thread_id="thread-native-2",
        status="running",
        last_turn_id="turn-9",
        title="拉取项目到根目录",
    )

    assert updated.id == session.id
    assert updated.status == "running"
    assert updated.last_turn_id == "turn-9"


def test_list_recent_sessions_returns_newest_first(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = NativeCodexSessionStore(ledger)
    first = store.get_or_create_session(
        native_thread_id="thread-a",
        title="A",
        cwd="/repo",
        source_kind="cli",
        status="done",
    )
    second = store.get_or_create_session(
        native_thread_id="thread-b",
        title="B",
        cwd="/repo",
        source_kind="vscode",
        status="running",
    )

    sessions = store.list_recent(limit=5)

    assert [session.id for session in sessions] == [second.id, first.id]
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_session_store.py -q
```

Expected: FAIL because `wlcodex.codex_native` and `native_codex_sessions` do not exist.

- [ ] **Step 3: Add the native package and models**

Create `wlcodex/codex_native/__init__.py`:

```python
"""Official Codex native session control bridge."""
```

Create `wlcodex/codex_native/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativeCodexStatus:
    enabled: bool
    connected: bool
    remote_control_status: str
    server_name: str = ""
    installation_id: str = ""
    environment_id: str | None = None
    error: str = ""


@dataclass(frozen=True)
class NativeCodexSession:
    id: int
    native_thread_id: str
    agent_run_id: int
    conversation_id: int
    title: str
    cwd: str
    source_kind: str
    status: str
    last_turn_id: str
    created_at: str
    updated_at: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "cwd": self.cwd,
            "source_kind": self.source_kind,
            "status": self.status,
            "last_turn_id": self.last_turn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class NativeCodexControlResult:
    native_thread_id: str
    agent_run_id: int
    turn_id: str = ""
    status: str = "ok"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
            "turn_id": self.turn_id,
            "status": self.status,
        }


class NativeCodexError(RuntimeError):
    pass
```

- [ ] **Step 4: Add the SQLite table migration**

Modify `wlcodex/db.py` inside the main `CREATE TABLE` script, immediately after `agent_runs`:

```python
            CREATE TABLE IF NOT EXISTS native_codex_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                native_thread_id TEXT NOT NULL,
                agent_run_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL DEFAULT 'unknown',
                last_turn_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id),
                FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_native_codex_sessions_thread_id
                ON native_codex_sessions(native_thread_id);
            CREATE INDEX IF NOT EXISTS idx_native_codex_sessions_agent_run
                ON native_codex_sessions(agent_run_id);
            CREATE INDEX IF NOT EXISTS idx_native_codex_sessions_updated
                ON native_codex_sessions(updated_at DESC);
```

- [ ] **Step 5: Implement the session store**

Create `wlcodex/codex_native/session_store.py`:

```python
from __future__ import annotations

import sqlite3

from wlcodex.codex_native.models import NativeCodexSession
from wlcodex.db import Ledger, _now


_NATIVE_CHAT_ID = 0
_NATIVE_USER_ID = 0
_NATIVE_TITLE = "Codex Native"
_NATIVE_MODE = "codex_native"
_NATIVE_WORKSPACE_ALIAS = "wlcodex"


class NativeCodexSessionStore:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._conn = ledger._conn

    def get_or_create_native_conversation(self) -> int:
        row = self._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE chat_id = ? AND user_id = ? AND mode = ?
            ORDER BY id ASC LIMIT 1
            """,
            (_NATIVE_CHAT_ID, _NATIVE_USER_ID, _NATIVE_MODE),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        conversation = self._ledger.create_conversation(
            chat_id=_NATIVE_CHAT_ID,
            user_id=_NATIVE_USER_ID,
            title=_NATIVE_TITLE,
            mode=_NATIVE_MODE,
            workspace_alias=_NATIVE_WORKSPACE_ALIAS,
        )
        return conversation.id

    def get_by_thread_id(self, native_thread_id: str) -> NativeCodexSession | None:
        row = self._conn.execute(
            "SELECT * FROM native_codex_sessions WHERE native_thread_id = ?",
            (native_thread_id,),
        ).fetchone()
        return _session(row) if row is not None else None

    def get_or_create_session(
        self,
        *,
        native_thread_id: str,
        title: str = "",
        cwd: str = "",
        source_kind: str = "unknown",
        status: str = "unknown",
        last_turn_id: str = "",
    ) -> NativeCodexSession:
        existing = self.get_by_thread_id(native_thread_id)
        if existing is not None:
            return self.update_session(
                native_thread_id=native_thread_id,
                title=title or existing.title,
                cwd=cwd or existing.cwd,
                source_kind=source_kind or existing.source_kind,
                status=status or existing.status,
                last_turn_id=last_turn_id or existing.last_turn_id,
            )
        conversation_id = self.get_or_create_native_conversation()
        agent_run = self._ledger.create_agent_run(
            conversation_id,
            agent="codex",
            role="codex_native",
            external_session_id=native_thread_id,
            prompt_packet_summary="Official Codex IDE session",
        )
        self._ledger.update_agent_run_status(
            agent_run.id,
            "running" if status in ("running", "active") else "queued",
            external_session_id=native_thread_id,
        )
        now = _now()
        self._conn.execute(
            """
            INSERT INTO native_codex_sessions (
                native_thread_id, agent_run_id, conversation_id, title, cwd,
                source_kind, status, last_turn_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                native_thread_id,
                agent_run.id,
                conversation_id,
                title,
                cwd,
                source_kind,
                status,
                last_turn_id,
                now,
                now,
            ),
        )
        self._conn.commit()
        created = self.get_by_thread_id(native_thread_id)
        if created is None:
            raise KeyError(f"native session was not created: {native_thread_id}")
        return created

    def update_session(
        self,
        *,
        native_thread_id: str,
        title: str | None = None,
        cwd: str | None = None,
        source_kind: str | None = None,
        status: str | None = None,
        last_turn_id: str | None = None,
    ) -> NativeCodexSession:
        existing = self.get_by_thread_id(native_thread_id)
        if existing is None:
            raise KeyError(f"unknown native Codex thread: {native_thread_id}")
        self._conn.execute(
            """
            UPDATE native_codex_sessions
            SET title = ?, cwd = ?, source_kind = ?, status = ?,
                last_turn_id = ?, updated_at = ?
            WHERE native_thread_id = ?
            """,
            (
                title if title is not None else existing.title,
                cwd if cwd is not None else existing.cwd,
                source_kind if source_kind is not None else existing.source_kind,
                status if status is not None else existing.status,
                last_turn_id if last_turn_id is not None else existing.last_turn_id,
                _now(),
                native_thread_id,
            ),
        )
        self._conn.commit()
        updated = self.get_by_thread_id(native_thread_id)
        if updated is None:
            raise KeyError(f"unknown native Codex thread after update: {native_thread_id}")
        return updated

    def list_recent(self, *, limit: int = 50) -> list[NativeCodexSession]:
        rows = self._conn.execute(
            """
            SELECT * FROM native_codex_sessions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_session(row) for row in rows]


def _session(row: sqlite3.Row) -> NativeCodexSession:
    return NativeCodexSession(
        id=int(row["id"]),
        native_thread_id=str(row["native_thread_id"]),
        agent_run_id=int(row["agent_run_id"]),
        conversation_id=int(row["conversation_id"]),
        title=str(row["title"]),
        cwd=str(row["cwd"]),
        source_kind=str(row["source_kind"]),
        status=str(row["status"]),
        last_turn_id=str(row["last_turn_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
```

- [ ] **Step 6: Run the session-store tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_session_store.py -q
```

Expected: PASS.

---

### Task 2: Add Native App-Server Transport And Client

**Files:**
- Create: `wlcodex/codex_native/transport.py`
- Create: `wlcodex/codex_native/client.py`
- Test: `tests/test_codex_native_client.py`

- [ ] **Step 1: Write failing native-client tests**

Create `tests/test_codex_native_client.py`:

```python
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from wlcodex.codex_native.client import CodexNativeClient


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.client = None

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)
        if "id" not in msg:
            return
        method = msg["method"]
        result: dict[str, Any]
        if method == "initialize":
            result = {"serverInfo": {"name": "fake-codex"}}
        elif method == "remoteControl/status/read":
            result = {
                "status": "connected",
                "serverName": "wanglindeMac-mini.local",
                "installationId": "install-1",
                "environmentId": "env-1",
            }
        elif method == "thread/list":
            result = {
                "threads": [
                    {
                        "id": "thread-1",
                        "title": "检查部署后报错",
                        "cwd": "/Users/wl/projects/wlcodex",
                        "sourceKind": "vscode",
                        "status": "running",
                    }
                ],
                "nextCursor": None,
            }
        elif method == "thread/read":
            result = {
                "thread": {
                    "id": msg["params"]["threadId"],
                    "title": "检查部署后报错",
                    "cwd": "/Users/wl/projects/wlcodex",
                    "sourceKind": "vscode",
                    "turns": [{"id": "turn-1", "status": "completed"}],
                }
            }
        elif method == "thread/resume":
            result = {"thread": {"id": msg["params"]["threadId"]}}
        elif method == "turn/start":
            result = {"turnId": "turn-2"}
        elif method == "turn/steer":
            result = {}
        elif method == "turn/interrupt":
            result = {}
        else:
            raise AssertionError(f"unexpected method: {method}")
        await self.client.receive_message({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": result,
        })

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_native_client_lists_and_reads_sessions() -> None:
    transport = FakeTransport()
    client = CodexNativeClient(send_json=transport.send_json, close=transport.close)
    transport.client = client.rpc

    status = await client.status()
    sessions = await client.list_sessions(limit=10)
    detail = await client.read_session("thread-1")

    assert status.remote_control_status == "connected"
    assert sessions[0]["id"] == "thread-1"
    assert detail["thread"]["turns"][0]["id"] == "turn-1"
    assert [msg["method"] for msg in transport.sent[:2]] == [
        "initialize",
        "remoteControl/status/read",
    ]


@pytest.mark.asyncio
async def test_native_client_continue_steer_and_interrupt() -> None:
    transport = FakeTransport()
    client = CodexNativeClient(send_json=transport.send_json, close=transport.close)
    transport.client = client.rpc

    turn_id = await client.continue_session("thread-1", "继续检查")
    await client.steer_turn("thread-1", "turn-2", "先不要改代码")
    await client.interrupt_turn("thread-1", "turn-2")

    methods = [msg["method"] for msg in transport.sent]
    assert turn_id == "turn-2"
    assert "thread/resume" in methods
    assert "turn/start" in methods
    assert "turn/steer" in methods
    assert "turn/interrupt" in methods
```

- [ ] **Step 2: Run the native-client tests and verify they fail**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_client.py -q
```

Expected: FAIL because `CodexNativeClient` does not exist.

- [ ] **Step 3: Implement stdio transport**

Create `wlcodex/codex_native/transport.py`:

```python
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


class CodexProxyTransport:
    def __init__(
        self,
        *,
        binary: str,
        sock_path: Path | None = None,
    ) -> None:
        self.binary = binary
        self.sock_path = sock_path
        self._process: asyncio.subprocess.Process | None = None
        self._recv_task: asyncio.Task[None] | None = None

    async def start(self, on_message: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        command = [self.binary, "app-server", "proxy"]
        if self.sock_path is not None:
            command.extend(["--sock", str(self.sock_path)])
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._recv_task = asyncio.create_task(self._recv_loop(on_message))

    async def send_json(self, msg: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Codex proxy transport is not started")
        encoded = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()

    async def _recv_loop(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._process is None or self._process.stdout is None:
            return
        while True:
            line = await self._process.stdout.readline()
            if not line:
                return
            await on_message(json.loads(line.decode("utf-8")))

    async def close(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        if self._process is not None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            self._process = None
```

- [ ] **Step 4: Implement the native client**

Create `wlcodex/codex_native/client.py`:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from wlcodex.codex_backend import (
    build_turn_start_params,
    build_turn_steer_params,
    parse_turn_response,
)
from wlcodex.codex_native.models import NativeCodexStatus
from wlcodex.jsonrpc import JsonRpcClient


class CodexNativeClient:
    def __init__(
        self,
        *,
        send_json: Callable[[dict[str, Any]], Awaitable[None]],
        close: Callable[[], Awaitable[None]],
        request_timeout_seconds: float = 60.0,
    ) -> None:
        self.rpc = JsonRpcClient(
            send_json=send_json,
            request_timeout_seconds=request_timeout_seconds,
        )
        self._close = close
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.rpc.request("initialize", {
            "clientInfo": {"name": "wlcodex-native", "version": "1.0.0"},
        })
        await self.rpc.notify("initialized", {})
        self._initialized = True

    async def status(self) -> NativeCodexStatus:
        await self.initialize()
        result = await self.rpc.request("remoteControl/status/read", {})
        return NativeCodexStatus(
            enabled=True,
            connected=str(result.get("status", "")) == "connected",
            remote_control_status=str(result.get("status", "")),
            server_name=str(result.get("serverName", "")),
            installation_id=str(result.get("installationId", "")),
            environment_id=result.get("environmentId")
            if isinstance(result.get("environmentId"), str)
            else None,
        )

    async def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        result = await self.rpc.request("thread/list", {
            "archived": False,
            "limit": limit,
            "sortDirection": "desc",
            "sortKey": "updated_at",
            "sourceKinds": [],
            "useStateDbOnly": False,
        })
        threads = result.get("threads", result.get("items", []))
        return list(threads) if isinstance(threads, list) else []

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        await self.initialize()
        return await self.rpc.request("thread/read", {
            "threadId": native_thread_id,
            "includeTurns": True,
        })

    async def continue_session(self, native_thread_id: str, prompt: str) -> str:
        await self.initialize()
        await self.rpc.request("thread/resume", {"threadId": native_thread_id})
        result = await self.rpc.request(
            "turn/start",
            build_turn_start_params(native_thread_id, prompt),
        )
        return parse_turn_response(result)

    async def steer_turn(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
    ) -> None:
        await self.initialize()
        await self.rpc.request(
            "turn/steer",
            build_turn_steer_params(native_thread_id, expected_turn_id, prompt),
        )

    async def interrupt_turn(self, native_thread_id: str, turn_id: str) -> None:
        await self.initialize()
        await self.rpc.request("turn/interrupt", {
            "threadId": native_thread_id,
            "turnId": turn_id,
        })

    def register_notification_handler(
        self,
        method: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.rpc.on_notification(method, handler)

    def register_server_request_handler(
        self,
        method: str,
        handler: Callable[[dict[str, Any], str], Awaitable[None]],
    ) -> None:
        self.rpc.on_server_request(method, handler)

    def resolve_request(self, request_id: str, result: dict[str, Any]) -> None:
        self.rpc.resolve_server_request(request_id, result)

    async def close(self) -> None:
        await self.rpc.close()
        await self._close()
```

- [ ] **Step 5: Run native-client tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_client.py -q
```

Expected: PASS.

---

### Task 3: Project Native Codex Events Into Runtime Events

**Files:**
- Create: `wlcodex/codex_native/projector.py`
- Test: `tests/test_codex_native_projector.py`

- [ ] **Step 1: Write failing projector tests**

Create `tests/test_codex_native_projector.py`:

```python
from pathlib import Path

from wlcodex.codex_native.projector import NativeCodexEventProjector
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.db import Ledger
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


def _deps(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = NativeCodexSessionStore(ledger)
    event_store = RuntimeEventStore(ledger._conn)
    return ledger, store, event_store


def test_projector_creates_session_and_text_delta(tmp_path: Path) -> None:
    _ledger, session_store, event_store = _deps(tmp_path)
    projector = NativeCodexEventProjector(session_store, event_store)

    stored = projector.project_notification(
        "item/agentMessage/delta",
        {
            "threadId": "thread-native-1",
            "turnId": "turn-1",
            "delta": "正在检查服务状态",
            "item": {"id": "item-1"},
        },
    )

    session = session_store.get_by_thread_id("thread-native-1")
    events = event_store.list_by_agent_run(session.agent_run_id)
    assert stored[0].event_type == EventType.MODEL_TEXT_DELTA
    assert events[0].payload["native_thread_id"] == "thread-native-1"
    assert events[0].payload["delta"] == "正在检查服务状态"
    assert events[0].actor == "codex_native"


def test_projector_updates_last_turn_from_turn_started(tmp_path: Path) -> None:
    _ledger, session_store, event_store = _deps(tmp_path)
    projector = NativeCodexEventProjector(session_store, event_store)

    projector.project_notification(
        "turn/started",
        {"threadId": "thread-native-2", "turnId": "turn-9"},
    )

    session = session_store.get_by_thread_id("thread-native-2")
    events = event_store.list_by_agent_run(session.agent_run_id)
    assert session.last_turn_id == "turn-9"
    assert events[0].event_type == EventType.AGENT_RUN_ACTIVITY
```

- [ ] **Step 2: Run projector tests and verify they fail**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_projector.py -q
```

Expected: FAIL because `NativeCodexEventProjector` does not exist.

- [ ] **Step 3: Implement native event projection**

Create `wlcodex/codex_native/projector.py`:

```python
from __future__ import annotations

from dataclasses import replace
from typing import Any

from wlcodex.codex_backend import BackendEvent
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.codex_runtime_source import CodexRuntimeSource
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import RuntimeEvent


_BACKEND_EVENT_BY_METHOD = {
    "thread/started": "thread_started",
    "thread/status/changed": "thread_status_changed",
    "thread/tokenUsage/updated": "token_usage_updated",
    "turn/started": "turn_started",
    "turn/completed": "turn_completed",
    "turn/diff/updated": "diff_updated",
    "turn/plan/updated": "plan_updated",
    "item/started": "item_started",
    "item/completed": "item_completed",
    "item/agentMessage/delta": "agent_message_delta",
    "item/commandExecution/outputDelta": "command_output_delta",
    "item/fileChange/outputDelta": "file_change_delta",
}


class NativeCodexEventProjector:
    def __init__(
        self,
        session_store: NativeCodexSessionStore,
        runtime_store: RuntimeEventStore,
    ) -> None:
        self._sessions = session_store
        self._runtime = runtime_store

    def project_notification(
        self,
        method: str,
        payload: dict[str, Any],
    ) -> list[RuntimeEvent]:
        native_thread_id = _thread_id(payload)
        if not native_thread_id:
            return []
        native_turn_id = _turn_id(payload)
        session = self._sessions.get_or_create_session(
            native_thread_id=native_thread_id,
            title=_title(payload),
            cwd=_cwd(payload),
            source_kind=_source_kind(payload),
            status=_status(method, payload),
            last_turn_id=native_turn_id,
        )
        if native_turn_id:
            session = self._sessions.update_session(
                native_thread_id=native_thread_id,
                status=_status(method, payload),
                last_turn_id=native_turn_id,
            )
        backend_type = _BACKEND_EVENT_BY_METHOD.get(method)
        if backend_type is None:
            return []
        source = CodexRuntimeSource(
            correlation_id=f"codex-native:{native_thread_id}",
            agent_run_id=session.agent_run_id,
            conversation_id=session.conversation_id,
        )
        events = source.map_event(BackendEvent(backend_type, dict(payload)))
        stored: list[RuntimeEvent] = []
        for event in events:
            patched = replace(
                event,
                actor="codex_native",
                payload={
                    **event.payload,
                    "native_thread_id": native_thread_id,
                    "native_turn_id": native_turn_id,
                    "source_kind": "codex_native",
                },
            )
            stored.append(self._runtime.append(patched))
        return stored


def _thread_id(payload: dict[str, Any]) -> str:
    thread_id = payload.get("threadId")
    if isinstance(thread_id, str):
        return thread_id
    thread = payload.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str):
        return str(thread["id"])
    return ""


def _turn_id(payload: dict[str, Any]) -> str:
    turn_id = payload.get("turnId")
    if isinstance(turn_id, str):
        return turn_id
    turn = payload.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return str(turn["id"])
    return ""


def _title(payload: dict[str, Any]) -> str:
    thread = payload.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("title"), str):
        return str(thread["title"])
    return str(payload.get("title", ""))


def _cwd(payload: dict[str, Any]) -> str:
    thread = payload.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("cwd"), str):
        return str(thread["cwd"])
    return str(payload.get("cwd", ""))


def _source_kind(payload: dict[str, Any]) -> str:
    thread = payload.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("sourceKind"), str):
        return str(thread["sourceKind"])
    return str(payload.get("sourceKind", "unknown"))


def _status(method: str, payload: dict[str, Any]) -> str:
    if method == "turn/started":
        return "running"
    if method == "turn/completed":
        return "done"
    status = payload.get("status")
    return str(status) if isinstance(status, str) else "unknown"
```

- [ ] **Step 4: Run projector tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_projector.py -q
```

Expected: PASS.

---

### Task 4: Add Native Controller For Session And Control Operations

**Files:**
- Create: `wlcodex/codex_native/controller.py`
- Test: `tests/test_codex_native_controller.py`

- [ ] **Step 1: Write failing controller tests**

Create `tests/test_codex_native_controller.py`:

```python
from pathlib import Path
from typing import Any

import pytest

from wlcodex.codex_native.controller import CodexNativeController
from wlcodex.codex_native.models import NativeCodexStatus
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.db import Ledger
from wlcodex.runtime_event_store import RuntimeEventStore


class FakeNativeClient:
    def __init__(self) -> None:
        self.continues: list[tuple[str, str]] = []
        self.steers: list[tuple[str, str, str]] = []
        self.interrupts: list[tuple[str, str]] = []

    async def status(self) -> NativeCodexStatus:
        return NativeCodexStatus(
            enabled=True,
            connected=True,
            remote_control_status="connected",
            server_name="mac",
            installation_id="install",
        )

    async def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [{
            "id": "thread-1",
            "title": "检查部署后报错",
            "cwd": "/Users/wl/projects/wlcodex",
            "sourceKind": "vscode",
            "status": "running",
        }]

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        return {"thread": {"id": native_thread_id, "turns": []}}

    async def continue_session(self, native_thread_id: str, prompt: str) -> str:
        self.continues.append((native_thread_id, prompt))
        return "turn-2"

    async def steer_turn(self, native_thread_id: str, expected_turn_id: str, prompt: str) -> None:
        self.steers.append((native_thread_id, expected_turn_id, prompt))

    async def interrupt_turn(self, native_thread_id: str, turn_id: str) -> None:
        self.interrupts.append((native_thread_id, turn_id))


def _controller(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    session_store = NativeCodexSessionStore(ledger)
    runtime_store = RuntimeEventStore(ledger._conn)
    client = FakeNativeClient()
    controller = CodexNativeController(
        client=client,
        session_store=session_store,
        runtime_store=runtime_store,
    )
    return controller, client, session_store


@pytest.mark.asyncio
async def test_controller_lists_and_maps_sessions(tmp_path: Path) -> None:
    controller, _client, session_store = _controller(tmp_path)

    sessions = await controller.list_sessions(limit=5)

    assert sessions[0].native_thread_id == "thread-1"
    assert sessions[0].agent_run_id > 0
    assert session_store.get_by_thread_id("thread-1") is not None


@pytest.mark.asyncio
async def test_controller_continue_steer_interrupt(tmp_path: Path) -> None:
    controller, client, _session_store = _controller(tmp_path)
    await controller.list_sessions(limit=5)

    continued = await controller.continue_session("thread-1", "继续检查")
    steered = await controller.steer_session("thread-1", "turn-2", "先不要改代码")
    interrupted = await controller.interrupt_session("thread-1", "turn-2")

    assert continued.turn_id == "turn-2"
    assert steered.status == "ok"
    assert interrupted.status == "ok"
    assert client.continues == [("thread-1", "继续检查")]
    assert client.steers == [("thread-1", "turn-2", "先不要改代码")]
    assert client.interrupts == [("thread-1", "turn-2")]
```

- [ ] **Step 2: Run controller tests and verify they fail**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_controller.py -q
```

Expected: FAIL because `CodexNativeController` does not exist.

- [ ] **Step 3: Implement controller**

Create `wlcodex/codex_native/controller.py`:

```python
from __future__ import annotations

from typing import Any

from wlcodex.codex_native.models import (
    NativeCodexControlResult,
    NativeCodexSession,
    NativeCodexStatus,
)
from wlcodex.codex_native.projector import NativeCodexEventProjector
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore


class CodexNativeController:
    def __init__(
        self,
        *,
        client: object,
        session_store: NativeCodexSessionStore,
        runtime_store: RuntimeEventStore,
    ) -> None:
        self._client = client
        self._sessions = session_store
        self._projector = NativeCodexEventProjector(session_store, runtime_store)
        self._register_handlers()

    async def status(self) -> NativeCodexStatus:
        return await self._client.status()

    async def list_sessions(self, *, limit: int = 50) -> list[NativeCodexSession]:
        raw_sessions = await self._client.list_sessions(limit=limit)
        mapped: list[NativeCodexSession] = []
        for raw in raw_sessions:
            thread_id = str(raw.get("id") or raw.get("threadId") or "")
            if not thread_id:
                continue
            mapped.append(self._sessions.get_or_create_session(
                native_thread_id=thread_id,
                title=str(raw.get("title", "")),
                cwd=str(raw.get("cwd", "")),
                source_kind=str(raw.get("sourceKind", raw.get("source_kind", "unknown"))),
                status=str(raw.get("status", "unknown")),
            ))
        return mapped

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        detail = await self._client.read_session(native_thread_id)
        thread = detail.get("thread")
        if isinstance(thread, dict):
            self._sessions.get_or_create_session(
                native_thread_id=native_thread_id,
                title=str(thread.get("title", "")),
                cwd=str(thread.get("cwd", "")),
                source_kind=str(thread.get("sourceKind", "unknown")),
                status=str(thread.get("status", "unknown")),
            )
        return detail

    async def continue_session(
        self,
        native_thread_id: str,
        prompt: str,
    ) -> NativeCodexControlResult:
        session = self._sessions.get_by_thread_id(native_thread_id)
        if session is None:
            session = self._sessions.get_or_create_session(native_thread_id=native_thread_id)
        turn_id = await self._client.continue_session(native_thread_id, prompt)
        self._sessions.update_session(
            native_thread_id=native_thread_id,
            status="running",
            last_turn_id=turn_id,
        )
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_id,
        )

    async def steer_session(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
    ) -> NativeCodexControlResult:
        session = self._sessions.get_by_thread_id(native_thread_id)
        if session is None:
            session = self._sessions.get_or_create_session(native_thread_id=native_thread_id)
        await self._client.steer_turn(native_thread_id, expected_turn_id, prompt)
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=expected_turn_id,
        )

    async def interrupt_session(
        self,
        native_thread_id: str,
        turn_id: str,
    ) -> NativeCodexControlResult:
        session = self._sessions.get_by_thread_id(native_thread_id)
        if session is None:
            session = self._sessions.get_or_create_session(native_thread_id=native_thread_id)
        await self._client.interrupt_turn(native_thread_id, turn_id)
        self._sessions.update_session(
            native_thread_id=native_thread_id,
            status="interrupted",
            last_turn_id=turn_id,
        )
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_id,
        )

    def _register_handlers(self) -> None:
        register = getattr(self._client, "register_notification_handler", None)
        if register is None:
            return
        for method in (
            "thread/started",
            "thread/status/changed",
            "thread/tokenUsage/updated",
            "turn/started",
            "turn/completed",
            "turn/diff/updated",
            "turn/plan/updated",
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
        ):
            register(method, self._make_notification_handler(method))

    def _make_notification_handler(self, method: str):
        async def _handler(payload: dict[str, Any]) -> None:
            self._projector.project_notification(method, payload)
        return _handler
```

- [ ] **Step 4: Run controller tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_controller.py -q
```

Expected: PASS.

---

### Task 5: Add Authenticated Native HTTP Routes

**Files:**
- Modify: `wlcodex/config.py`
- Modify: `wlcodex/live_stream/server.py`
- Test: `tests/test_worker_live_stream_native_routes.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing route and auth tests**

Create `tests/test_worker_live_stream_native_routes.py`:

```python
import asyncio
import json
from typing import Any

import pytest

from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.db import Ledger


class FakeNativeController:
    async def status(self):
        from wlcodex.codex_native.models import NativeCodexStatus
        return NativeCodexStatus(
            enabled=True,
            connected=True,
            remote_control_status="connected",
            server_name="mac",
            installation_id="install",
        )

    async def list_sessions(self, *, limit: int = 50):
        from wlcodex.codex_native.models import NativeCodexSession
        return [NativeCodexSession(
            id=1,
            native_thread_id="thread-1",
            agent_run_id=7,
            conversation_id=1,
            title="检查部署后报错",
            cwd="/Users/wl/projects/wlcodex",
            source_kind="vscode",
            status="running",
            last_turn_id="turn-1",
            created_at="2026-05-30T00:00:00+00:00",
            updated_at="2026-05-30T00:00:00+00:00",
        )]

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        return {"thread": {"id": native_thread_id, "turns": []}}

    async def continue_session(self, native_thread_id: str, prompt: str):
        from wlcodex.codex_native.models import NativeCodexControlResult
        return NativeCodexControlResult(native_thread_id, 7, "turn-2")


async def _read_response(host: str, port: int, request: str) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request.encode("utf-8"))
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.decode("utf-8", errors="replace").partition("\r\n\r\n")
    return head, body


@pytest.mark.asyncio
async def test_native_routes_require_auth(tmp_path):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    hub = WorkerLiveStreamHub(RuntimeEventStore(ledger._conn))
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=hub,
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        head, body = await _read_response(
            "127.0.0.1",
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\nHost: test\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "401 Unauthorized" in head


@pytest.mark.asyncio
async def test_native_sessions_route_returns_mapped_sessions(tmp_path):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    hub = WorkerLiveStreamHub(RuntimeEventStore(ledger._conn))
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=hub,
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        head, body = await _read_response(
            "127.0.0.1",
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "\r\n",
        )
    finally:
        await server.stop()

    payload = json.loads(body)
    assert "200 OK" in head
    assert payload["sessions"][0]["native_thread_id"] == "thread-1"
    assert payload["sessions"][0]["agent_run_id"] == 7
```

Append config tests to `tests/test_config.py`:

```python
def test_live_stream_auth_token_env_can_be_set(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, """
[live_stream]
enabled = true
host = "127.0.0.1"
port = 18731
access_token_env = "WLCODEX_TEST_TOKEN"

[codex_native]
enabled = true
transport = "proxy"
""")

    config = load_config(config_path)

    assert config.live_stream.access_token_env == "WLCODEX_TEST_TOKEN"
    assert config.codex_native.enabled is True
    assert config.codex_native.transport == "proxy"
```

- [ ] **Step 2: Run route/config tests and verify they fail**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_worker_live_stream_native_routes.py tests/test_config.py::test_live_stream_auth_token_env_can_be_set -q
```

Expected: FAIL because config fields and native routes do not exist.

- [ ] **Step 3: Add config fields**

Modify `wlcodex/config.py`:

```python
@dataclass(frozen=True)
class LiveStreamConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 18731
    access_token_env: str = "WLCODEX_LIVE_STREAM_TOKEN"
    allow_unauthenticated_loopback: bool = True


@dataclass(frozen=True)
class CodexNativeConfig:
    enabled: bool = False
    transport: str = "proxy"
    sock_path: Path | None = None
```

Add `codex_native: CodexNativeConfig = CodexNativeConfig()` to `AppConfig`.

Inside `load_config`, read:

```python
    live_stream_raw = data.get("live_stream", {})
    codex_native_raw = data.get("codex_native", {})
```

Return:

```python
        live_stream=LiveStreamConfig(
            enabled=bool(live_stream_raw.get("enabled", False)),
            host=str(live_stream_raw.get("host", "127.0.0.1")),
            port=int(live_stream_raw.get("port", 18731)),
            access_token_env=str(
                live_stream_raw.get("access_token_env", "WLCODEX_LIVE_STREAM_TOKEN")
            ),
            allow_unauthenticated_loopback=bool(
                live_stream_raw.get("allow_unauthenticated_loopback", True)
            ),
        ),
        codex_native=CodexNativeConfig(
            enabled=bool(codex_native_raw.get("enabled", False)),
            transport=str(codex_native_raw.get("transport", "proxy")),
            sock_path=_optional_path(codex_native_raw.get("sock_path")),
        ),
```

Validate:

```python
    codex_native_transport = str(codex_native_raw.get("transport", "proxy"))
    if codex_native_transport not in ("proxy",):
        raise ConfigError(
            "codex_native.transport must be 'proxy' for the first native-control release"
        )
```

- [ ] **Step 4: Add auth and native routes to the server**

Modify `WorkerLiveStreamServer.__init__` in `wlcodex/live_stream/server.py`:

```python
    def __init__(
        self,
        *,
        host: str,
        port: int,
        hub: WorkerLiveStreamHub,
        native_controller: object | None = None,
        access_token: str | None = None,
        allow_unauthenticated_loopback: bool = True,
    ) -> None:
        ...
        self._native_controller = native_controller
        self._access_token = access_token or ""
        self._allow_unauthenticated_loopback = allow_unauthenticated_loopback
```

Add helpers:

```python
import hmac


def _authorized(
    headers: dict[str, str],
    *,
    access_token: str,
    host: str,
    allow_unauthenticated_loopback: bool,
) -> bool:
    if not access_token and allow_unauthenticated_loopback and host in ("127.0.0.1", "localhost"):
        return True
    auth = headers.get("authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    return hmac.compare_digest(auth[len(prefix):], access_token)
```

Parse request bodies for POST:

```python
content_length = _safe_int(headers.get("content-length", "0"), default=0)
body_bytes = await reader.readexactly(content_length) if content_length > 0 else b""
body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
```

Before native routes, enforce:

```python
if parsed.path.startswith("/native/codex") or parsed.path.startswith("/api/native/codex"):
    if not _authorized(
        headers,
        access_token=self._access_token,
        host=self.host,
        allow_unauthenticated_loopback=self._allow_unauthenticated_loopback,
    ):
        await self._send_json(writer, 401, {"error": "unauthorized"})
        return
```

Add route behavior:

```python
if parsed.path == "/api/native/codex/status":
    status = await self._native_controller.status()
    await self._send_json(writer, 200, status.__dict__)
    return

if parsed.path == "/api/native/codex/sessions":
    sessions = await self._native_controller.list_sessions(limit=50)
    await self._send_json(
        writer,
        200,
        {"sessions": [session.to_json_dict() for session in sessions]},
    )
    return
```

For control POST routes, parse the native thread id from the path and call:

```python
result = await self._native_controller.continue_session(native_thread_id, str(body_json["prompt"]))
await self._send_json(writer, 200, result.to_json_dict())
```

- [ ] **Step 5: Run route/config tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_worker_live_stream_native_routes.py tests/test_config.py::test_live_stream_auth_token_env_can_be_set -q
```

Expected: PASS.

---

### Task 6: Add `Codex干活的` Phone Page

**Files:**
- Modify: `wlcodex/live_stream/server.py`
- Test: `tests/test_worker_live_stream_native_routes.py`

- [ ] **Step 1: Add failing page test**

Append to `tests/test_worker_live_stream_native_routes.py`:

```python
@pytest.mark.asyncio
async def test_native_codex_page_contains_worker_and_session_selector(tmp_path):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    hub = WorkerLiveStreamHub(RuntimeEventStore(ledger._conn))
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=hub,
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        head, body = await _read_response(
            "127.0.0.1",
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "\r\n",
        )
    finally:
        await server.stop()

    assert "200 OK" in head
    assert "Codex干活的" in body
    assert "/api/native/codex/sessions" in body
    assert "来源：官方 Codex IDE" in body
```

- [ ] **Step 2: Run the page test and verify it fails**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_worker_live_stream_native_routes.py::test_native_codex_page_contains_worker_and_session_selector -q
```

Expected: FAIL because `/native/codex` does not render the page.

- [ ] **Step 3: Add the native page renderer**

Add `_native_codex_page()` to `wlcodex/live_stream/server.py`:

```python
def _native_codex_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex干活的</title>
  <style>
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0b0d10; color: #f4f7fb; }
    header { padding: 18px 16px 10px; border-bottom: 1px solid #20252d; }
    h1 { margin: 0; font-size: 24px; font-weight: 700; }
    .source { margin-top: 6px; color: #9fb0c3; font-size: 14px; }
    main { padding: 14px 12px 72px; }
    .session { display: block; width: 100%; text-align: left; padding: 14px; margin: 10px 0; border: 1px solid #242b35; border-radius: 8px; background: #141820; color: #f4f7fb; }
    .title { font-size: 16px; font-weight: 650; }
    .meta { margin-top: 6px; color: #9fb0c3; font-size: 12px; word-break: break-all; }
    .controls { position: fixed; left: 0; right: 0; bottom: 0; display: flex; gap: 8px; padding: 10px; background: #0b0d10; border-top: 1px solid #20252d; }
    input { flex: 1; min-width: 0; border-radius: 8px; border: 1px solid #2f3845; background: #11161d; color: #f4f7fb; padding: 12px; font-size: 15px; }
    button { border-radius: 8px; border: 0; padding: 12px 14px; background: #f4f7fb; color: #0b0d10; font-weight: 700; }
  </style>
</head>
<body>
  <header>
    <h1>Codex干活的</h1>
    <div class="source">来源：官方 Codex IDE</div>
  </header>
  <main id="sessions"></main>
  <script>
    const token = new URLSearchParams(location.search).get("token") || "";
    const headers = token ? {"Authorization": "Bearer " + token} : {};
    async function loadSessions() {
      const res = await fetch("/api/native/codex/sessions", {headers});
      const data = await res.json();
      const root = document.getElementById("sessions");
      root.innerHTML = "";
      for (const session of data.sessions || []) {
        const btn = document.createElement("button");
        btn.className = "session";
        btn.innerHTML = `<div class="title">${escapeHtml(session.title || session.native_thread_id)}</div>
          <div class="meta">${escapeHtml(session.cwd || "")}</div>
          <div class="meta">status=${escapeHtml(session.status)} · source=${escapeHtml(session.source_kind)} · agent_run=${session.agent_run_id}</div>`;
        btn.onclick = () => location.href = `/workers/${session.agent_run_id}/live${token ? "?token=" + encodeURIComponent(token) : ""}`;
        root.appendChild(btn);
      }
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    loadSessions();
  </script>
</body>
</html>"""
```

Route:

```python
if parsed.path == "/native/codex":
    await self._send_html(writer, 200, _native_codex_page())
    return
```

- [ ] **Step 4: Run native route tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_worker_live_stream_native_routes.py -q
```

Expected: PASS.

---

### Task 7: Compose Native Control In Main

**Files:**
- Modify: `wlcodex/main.py`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_main_composition.py`

- [ ] **Step 1: Add failing composition test**

Append to `tests/test_main_composition.py`:

```python
def test_create_live_stream_components_wires_native_controller_when_enabled(tmp_path: Path) -> None:
    from wlcodex.config import (
        AppConfig,
        ApprovalConfig,
        BackendConfig,
        CodexConfig,
        CodexNativeConfig,
        DisplayConfig,
        LiveStreamConfig,
        StorageConfig,
        TaskConfig,
        TelegramConfig,
        WorkspaceConfig,
    )
    from wlcodex.db import Ledger
    from wlcodex.main import _create_live_stream_components
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    runtime_store = RuntimeEventStore(ledger._conn)
    config = AppConfig(
        telegram=TelegramConfig("BOT_TOKEN", frozenset({1})),
        codex=CodexConfig("codex", "127.0.0.1", 17431, "on-request", "workspace-write"),
        storage=StorageConfig(tmp_path / "db.sqlite3", tmp_path / "logs", tmp_path / "worktrees"),
        display=DisplayConfig(2, 40, 3500),
        backend=BackendConfig(15, 60, 300, 3600, 3600, 20000),
        approval=ApprovalConfig(3600, True),
        task=TaskConfig(7200, 1800, 3600, 60, 120),
        workspaces=(WorkspaceConfig("wlcodex", tmp_path, True),),
        live_stream=LiveStreamConfig(enabled=True, host="127.0.0.1", port=0),
        codex_native=CodexNativeConfig(enabled=True, transport="proxy"),
    )

    components = _create_live_stream_components(config, runtime_store, ledger)

    assert components is not None
    assert components.server._native_controller is not None
```

- [ ] **Step 2: Run the composition test and verify it fails**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_main_composition.py::test_create_live_stream_components_wires_native_controller_when_enabled -q
```

Expected: FAIL because `_create_live_stream_components` does not accept/build native control.

- [ ] **Step 3: Add composition wiring**

Modify `_create_live_stream_components` in `wlcodex/main.py` so it accepts the `Ledger`:

```python
def _create_live_stream_components(
    config: AppConfig,
    runtime_store: RuntimeEventStore,
    ledger: Ledger | None = None,
) -> LiveStreamComponents | None:
```

Build the native controller only when enabled and `ledger` is present:

```python
native_controller = None
if config.codex_native.enabled and ledger is not None:
    from wlcodex.codex_native.client import CodexNativeClient
    from wlcodex.codex_native.controller import CodexNativeController
    from wlcodex.codex_native.session_store import NativeCodexSessionStore
    from wlcodex.codex_native.transport import CodexProxyTransport

    transport = CodexProxyTransport(
        binary=config.codex.binary,
        sock_path=config.codex_native.sock_path,
    )

    async def _start_native_client() -> CodexNativeClient:
        client_holder: dict[str, CodexNativeClient] = {}
        async def _on_message(msg: dict) -> None:
            await client_holder["client"].rpc.receive_message(msg)
        await transport.start(_on_message)
        client = CodexNativeClient(send_json=transport.send_json, close=transport.close)
        client_holder["client"] = client
        return client

    native_client = LazyNativeClient(_start_native_client)
    native_controller = CodexNativeController(
        client=native_client,
        session_store=NativeCodexSessionStore(ledger),
        runtime_store=runtime_store,
    )
```

Add `LazyNativeClient` inside `wlcodex/codex_native/client.py` so synchronous
main composition can defer starting the proxy process until the first native
request:

```python
from collections.abc import Awaitable, Callable


class LazyNativeClient:
    def __init__(self, factory: Callable[[], Awaitable[CodexNativeClient]]) -> None:
        self._factory = factory
        self._client: CodexNativeClient | None = None
        self._pending_notification_handlers: list[tuple[str, object]] = []
        self._pending_server_request_handlers: list[tuple[str, object]] = []

    async def _get(self) -> CodexNativeClient:
        if self._client is None:
            self._client = await self._factory()
            for method, handler in self._pending_notification_handlers:
                self._client.register_notification_handler(method, handler)
            for method, handler in self._pending_server_request_handlers:
                self._client.register_server_request_handler(method, handler)
        return self._client

    async def status(self) -> NativeCodexStatus:
        return await (await self._get()).status()

    async def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return await (await self._get()).list_sessions(limit=limit)

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        return await (await self._get()).read_session(native_thread_id)

    async def continue_session(self, native_thread_id: str, prompt: str) -> str:
        return await (await self._get()).continue_session(native_thread_id, prompt)

    async def steer_turn(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
    ) -> None:
        await (await self._get()).steer_turn(native_thread_id, expected_turn_id, prompt)

    async def interrupt_turn(self, native_thread_id: str, turn_id: str) -> None:
        await (await self._get()).interrupt_turn(native_thread_id, turn_id)

    def register_notification_handler(self, method: str, handler) -> None:
        if self._client is not None:
            self._client.register_notification_handler(method, handler)
            return
        self._pending_notification_handlers.append((method, handler))

    def register_server_request_handler(self, method: str, handler) -> None:
        if self._client is not None:
            self._client.register_server_request_handler(method, handler)
            return
        self._pending_server_request_handlers.append((method, handler))
```

- [ ] **Step 4: Pass auth and native controller into server**

In `wlcodex/main.py`, read the token:

```python
access_token = os.environ.get(config.live_stream.access_token_env, "")
```

Pass:

```python
server = WorkerLiveStreamServer(
    host=config.live_stream.host,
    port=config.live_stream.port,
    hub=hub,
    native_controller=native_controller,
    access_token=access_token,
    allow_unauthenticated_loopback=config.live_stream.allow_unauthenticated_loopback,
)
```

- [ ] **Step 5: Update example config**

Append to `config/wlcodex.example.toml`:

```toml
[live_stream]
enabled = false
host = "127.0.0.1"
port = 18731
access_token_env = "WLCODEX_LIVE_STREAM_TOKEN"
allow_unauthenticated_loopback = true

[codex_native]
enabled = false
transport = "proxy"
# sock_path = "/Users/wl/.codex/app-server-control/app-server-control.sock"
```

- [ ] **Step 6: Run composition tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_main_composition.py::test_create_live_stream_components_wires_native_controller_when_enabled tests/test_config.py -q
```

Expected: PASS for the targeted tests. If an existing config assertion fails
because `LiveStreamConfig` or `AppConfig` now has additional constructor
fields, update that test fixture to pass the new default values and rerun this
same command.

---

### Task 8: Verification And Local Manual Test

**Files:**
- Existing tests only.

- [ ] **Step 1: Run focused native test suite**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_codex_native_session_store.py tests/test_codex_native_client.py tests/test_codex_native_projector.py tests/test_codex_native_controller.py tests/test_worker_live_stream_native_routes.py -q
```

Expected: PASS.

- [ ] **Step 2: Run existing live-stream tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_worker_live_stream_models.py tests/test_worker_live_stream_hub.py tests/test_worker_live_stream_server.py -q
```

Expected: PASS.

- [ ] **Step 3: Run config and composition tests**

Run:

```bash
UV_CACHE_DIR=/private/tmp/uv-cache uv run --extra dev pytest tests/test_config.py tests/test_main_composition.py -q
```

Expected: PASS. Any failure caused by constructor or config changes must be
fixed in this change before proceeding.

- [ ] **Step 4: Manual loopback test**

Start WLCodex with:

```bash
export WLCODEX_LIVE_STREAM_TOKEN="local-test-token"
UV_CACHE_DIR=/private/tmp/uv-cache uv run wlcodex --config config/wlcodex.example.toml
```

Open:

```text
http://127.0.0.1:18731/native/codex?token=local-test-token
```

Expected:

- page title shows `Codex干活的`;
- source label shows `来源：官方 Codex IDE`;
- session list loads from official Codex app-server proxy when the official daemon is available;
- missing daemon returns a JSON/UI error instead of a server crash.

- [ ] **Step 5: Authenticated tunnel test**

With the local server running and token set, start tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:18731
```

Open the tunnel URL with:

```text
/native/codex?token=local-test-token
```

Expected:

- page loads only with the token;
- `/api/native/codex/sessions` returns `401` without `Authorization: Bearer local-test-token`;
- with auth, sessions load and clicking a session opens `/workers/{agent_run_id}/live`.

---

## Self-Review Checklist

- Spec coverage:
  - Native session list/read/continue/steer/interrupt are covered by Tasks 2, 4, and 5.
  - Native event projection is covered by Task 3.
  - `Codex干活的` UI is covered by Task 6.
  - Authentication is covered by Task 5 and Task 8.
  - Composition and config are covered by Task 7.

- Completion-marker scan:
  - The plan contains no unresolved marker text and no unspecified
    "add tests" steps.

- Type consistency:
  - `NativeCodexSession.agent_run_id` is the stream key used by `/workers/{agent_run_id}/live`.
  - `native_thread_id` is the official Codex thread id used by native control routes.
  - `last_turn_id` is stored separately for active-turn steering and interrupt actions.
