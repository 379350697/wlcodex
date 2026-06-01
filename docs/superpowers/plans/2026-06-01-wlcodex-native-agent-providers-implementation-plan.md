# WLCodex Native Agent Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the Codex-only native web control surface into a provider-based native-agent surface for Codex, Claude, and Antigravity while enforcing one active Claude engine at a time.

**Architecture:** Add a generic `native_agents` package with provider contracts, normalized models, a provider registry, and a generic session store. Adapt the existing Codex native controller through the registry first, then add `claude` as one provider with mutually exclusive `cli-local` and `sdk-deepseek` engines, and add Antigravity through an SDK provider. Keep `/native/codex` and `/api/native/codex/...` working as compatibility routes while moving new code to `/native/{provider}` and `/api/native/{provider}/...`.

**Tech Stack:** Python 3.12, asyncio, SQLite, pytest, existing WLCodex `RuntimeEventStore`, existing Codex app-server bridge, Claude Code CLI `stream-json`, Claude Agent SDK through DeepSeek's Anthropic-compatible endpoint, optional Antigravity SDK dependency.

---

## Scope Guard

This plan implements the provider architecture and local web/runtime plumbing.

Do not:

- create separate top-level providers or pages named `claude-cli` or `claude-deepseek`;
- run both Claude engines in one WLCodex process;
- depend on the system `agy` CLI for Antigravity runtime behavior;
- scrape Claude Code or Antigravity GUI apps;
- commit API keys or local login state;
- replace existing Telegram or workbench execution behavior.

Execution starts from the current dirty workspace. Before editing an already-modified file, inspect the current contents and preserve user changes. Stage and commit only files changed by the executing worker for each task.

## File Structure

Create:

- `wlcodex/native_agents/__init__.py`  
  Package exports for provider models and registry.

- `wlcodex/native_agents/models.py`  
  Provider-agnostic status, capabilities, session, control result, and request dataclasses.

- `wlcodex/native_agents/provider.py`  
  `NativeAgentProvider` protocol and registry.

- `wlcodex/native_agents/session_store.py`  
  Generic SQLite-backed `native_agent_sessions` store.

- `wlcodex/native_agents/codex_provider.py`  
  Adapter that wraps the existing `CodexNativeController`.

- `wlcodex/native_agents/claude_cli_provider.py`  
  Claude provider engine backed by the local Claude Code CLI and existing stream-json parser.

- `wlcodex/native_agents/claude_sdk_deepseek_provider.py`  
  Claude provider engine backed by Claude Agent SDK against DeepSeek's Anthropic-compatible endpoint.

- `wlcodex/native_agents/antigravity_provider.py`  
  Antigravity SDK provider with clean missing-SDK and missing-auth status.

- `tests/test_native_agent_models.py`
- `tests/test_native_agent_session_store.py`
- `tests/test_native_agent_registry.py`
- `tests/test_native_agent_config.py`
- `tests/test_native_agent_codex_provider.py`
- `tests/test_native_agent_claude_cli_provider.py`
- `tests/test_native_agent_claude_sdk_deepseek_provider.py`
- `tests/test_native_agent_antigravity_provider.py`
- `tests/test_worker_live_stream_native_agent_routes.py`

Modify:

- `wlcodex/db.py`  
  Add `native_agent_sessions` table and indexes.

- `wlcodex/config.py`  
  Add `[native_agents]` config while preserving existing `[codex_native]` compatibility.

- `wlcodex/runtime_events.py`  
  Add `EventSource.ANTIGRAVITY`.

- `wlcodex/claude_binary.py`  
  Resolve `~/.local/bin/claude` in auto mode.

- `wlcodex/claude_backend.py`  
  Treat empty CLI model as "do not pass `--model`".

- `wlcodex/live_stream/server.py`  
  Dispatch generic native-agent routes and render provider-aware native pages.

- `wlcodex/main.py`  
  Build a provider registry and pass it to the live stream server.

- `pyproject.toml`  
  Add optional extras for SDK providers without making normal installs fail.

- `config/wlcodex.example.toml`  
  Document provider config and Claude engine selection.

---

### Task 1: Native-Agent Models And Provider Registry

**Files:**
- Create: `wlcodex/native_agents/__init__.py`
- Create: `wlcodex/native_agents/models.py`
- Create: `wlcodex/native_agents/provider.py`
- Test: `tests/test_native_agent_models.py`
- Test: `tests/test_native_agent_registry.py`

- [ ] **Step 1: Write model tests**

Create `tests/test_native_agent_models.py`:

```python
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)


def test_status_serializes_provider_and_engine() -> None:
    status = NativeAgentStatus(
        provider="claude",
        provider_engine="sdk-deepseek",
        enabled=True,
        connected=False,
        status_code="missing_api_key",
        message="DEEPSEEK_API_KEY is not set.",
    )

    assert status.to_json_dict() == {
        "provider": "claude",
        "provider_engine": "sdk-deepseek",
        "enabled": True,
        "connected": False,
        "status_code": "missing_api_key",
        "message": "DEEPSEEK_API_KEY is not set.",
        "metadata": {},
    }


def test_capabilities_disable_controls_with_reasons() -> None:
    caps = NativeAgentCapabilities(
        can_list_sessions=True,
        can_continue_session=True,
        disabled_reasons={"can_steer_active_turn": "Claude SDK cannot steer an active turn."},
    )

    payload = caps.to_json_dict()

    assert payload["can_list_sessions"] is True
    assert payload["can_continue_session"] is True
    assert payload["can_steer_active_turn"] is False
    assert payload["disabled_reasons"]["can_steer_active_turn"] == (
        "Claude SDK cannot steer an active turn."
    )


def test_session_serializes_native_thread_id_for_existing_ui() -> None:
    session = NativeAgentSession(
        id=1,
        provider="antigravity",
        provider_engine="sdk",
        native_session_id="ag-1",
        agent_run_id=44,
        conversation_id=9,
        title="Fix UI",
        cwd="/Users/wl/projects/wlcodex",
        source_kind="antigravity_sdk",
        status="running",
        last_turn_id="turn-1",
        activity_at="2026-06-01T00:00:00Z",
        created_at="2026-06-01T00:00:00Z",
        updated_at="2026-06-01T00:01:00Z",
    )

    payload = session.to_json_dict()

    assert payload["provider"] == "antigravity"
    assert payload["provider_engine"] == "sdk"
    assert payload["native_session_id"] == "ag-1"
    assert payload["native_thread_id"] == "ag-1"


def test_control_result_preserves_existing_thread_field() -> None:
    result = NativeAgentControlResult(
        provider="claude",
        provider_engine="cli-local",
        native_session_id="claude-session-1",
        agent_run_id=45,
        status="started",
    )

    payload = result.to_json_dict()

    assert payload["native_session_id"] == "claude-session-1"
    assert payload["native_thread_id"] == "claude-session-1"
    assert payload["status"] == "started"
```

- [ ] **Step 2: Write registry tests**

Create `tests/test_native_agent_registry.py`:

```python
import pytest

from wlcodex.native_agents.models import NativeAgentCapabilities, NativeAgentStatus
from wlcodex.native_agents.provider import NativeAgentRegistry


class FakeProvider:
    provider = "codex"
    provider_engine = "app-server"

    async def status(self):
        return NativeAgentStatus(
            provider="codex",
            provider_engine="app-server",
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self):
        return NativeAgentCapabilities(can_list_sessions=True)


def test_registry_returns_provider_by_name() -> None:
    registry = NativeAgentRegistry([FakeProvider()])

    assert registry.get("codex").provider_engine == "app-server"


def test_registry_rejects_duplicate_provider_names() -> None:
    with pytest.raises(ValueError, match="duplicate native provider: codex"):
        NativeAgentRegistry([FakeProvider(), FakeProvider()])


def test_registry_rejects_claude_engine_as_provider_name() -> None:
    class BadProvider(FakeProvider):
        provider = "claude-cli"

    with pytest.raises(ValueError, match="Claude engines must not be providers"):
        NativeAgentRegistry([BadProvider()])


def test_registry_lists_provider_summaries() -> None:
    registry = NativeAgentRegistry([FakeProvider()])

    assert registry.list_provider_summaries() == [
        {"provider": "codex", "provider_engine": "app-server"}
    ]
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_models.py tests/test_native_agent_registry.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'wlcodex.native_agents'`.

- [ ] **Step 4: Add models**

Create `wlcodex/native_agents/__init__.py`:

```python
"""Provider-based native agent control surface."""
```

Create `wlcodex/native_agents/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NativeAgentStatus:
    provider: str
    provider_engine: str
    enabled: bool
    connected: bool
    status_code: str
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_engine": self.provider_engine,
            "enabled": self.enabled,
            "connected": self.connected,
            "status_code": self.status_code,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class NativeAgentCapabilities:
    can_list_sessions: bool = False
    can_list_models: bool = False
    can_start_session: bool = False
    can_resume_session: bool = False
    can_read_history: bool = False
    can_stream_events: bool = False
    can_continue_session: bool = False
    can_steer_active_turn: bool = False
    can_interrupt: bool = False
    can_resolve_approval: bool = False
    can_apply_file_edits: bool = False
    can_run_shell_commands: bool = False
    disabled_reasons: dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "can_list_sessions": self.can_list_sessions,
            "can_list_models": self.can_list_models,
            "can_start_session": self.can_start_session,
            "can_resume_session": self.can_resume_session,
            "can_read_history": self.can_read_history,
            "can_stream_events": self.can_stream_events,
            "can_continue_session": self.can_continue_session,
            "can_steer_active_turn": self.can_steer_active_turn,
            "can_interrupt": self.can_interrupt,
            "can_resolve_approval": self.can_resolve_approval,
            "can_apply_file_edits": self.can_apply_file_edits,
            "can_run_shell_commands": self.can_run_shell_commands,
            "disabled_reasons": self.disabled_reasons,
        }


@dataclass(frozen=True)
class NativeAgentSession:
    id: int
    provider: str
    provider_engine: str
    native_session_id: str
    agent_run_id: int
    conversation_id: int
    title: str
    cwd: str
    source_kind: str
    status: str
    last_turn_id: str
    activity_at: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "provider_engine": self.provider_engine,
            "native_session_id": self.native_session_id,
            "native_thread_id": self.native_session_id,
            "agent_run_id": self.agent_run_id,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "cwd": self.cwd,
            "source_kind": self.source_kind,
            "status": self.status,
            "last_turn_id": self.last_turn_id,
            "activity_at": self.activity_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class NativeAgentControlResult:
    provider: str
    provider_engine: str
    native_session_id: str
    agent_run_id: int
    turn_id: str = ""
    active_turn_id: str = ""
    turn_running: bool = False
    status: str = "ok"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_engine": self.provider_engine,
            "native_session_id": self.native_session_id,
            "native_thread_id": self.native_session_id,
            "agent_run_id": self.agent_run_id,
            "turn_id": self.turn_id,
            "active_turn_id": self.active_turn_id,
            "turn_running": self.turn_running,
            "status": self.status,
        }
```

- [ ] **Step 5: Add provider protocol and registry**

Create `wlcodex/native_agents/provider.py`:

```python
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)


class NativeAgentProvider(Protocol):
    provider: str
    provider_engine: str

    async def status(self) -> NativeAgentStatus: ...

    def capabilities(self) -> NativeAgentCapabilities: ...

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]: ...

    async def list_models(self) -> list[dict[str, Any]]: ...

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult: ...

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult: ...

    async def read_session(self, native_session_id: str) -> dict[str, Any]: ...

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult: ...

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult: ...

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult: ...

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult: ...

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult: ...

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> NativeAgentControlResult: ...


class NativeAgentRegistry:
    def __init__(self, providers: Iterable[NativeAgentProvider]) -> None:
        self._providers: dict[str, NativeAgentProvider] = {}
        for provider in providers:
            name = provider.provider.strip()
            if name in {"claude-cli", "claude-deepseek"}:
                raise ValueError("Claude engines must not be providers")
            if name in self._providers:
                raise ValueError(f"duplicate native provider: {name}")
            self._providers[name] = provider

    def get(self, provider: str) -> NativeAgentProvider:
        try:
            return self._providers[provider]
        except KeyError:
            raise KeyError(f"unknown native provider: {provider}") from None

    def maybe_get(self, provider: str) -> NativeAgentProvider | None:
        return self._providers.get(provider)

    def list_provider_summaries(self) -> list[dict[str, str]]:
        return [
            {
                "provider": provider.provider,
                "provider_engine": provider.provider_engine,
            }
            for provider in self._providers.values()
        ]
```

- [ ] **Step 6: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_models.py tests/test_native_agent_registry.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add wlcodex/native_agents tests/test_native_agent_models.py tests/test_native_agent_registry.py
git commit -m "feat: add native agent provider contracts"
```

---

### Task 2: Generic Native-Agent Session Store

**Files:**
- Create: `wlcodex/native_agents/session_store.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_native_agent_session_store.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write session-store tests**

Create `tests/test_native_agent_session_store.py`:

```python
from pathlib import Path

from wlcodex.db import Ledger
from wlcodex.native_agents.session_store import NativeAgentSessionStore


def _store(tmp_path: Path) -> NativeAgentSessionStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return NativeAgentSessionStore(ledger)


def test_get_or_create_session_is_unique_by_provider_engine_and_session(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = store.get_or_create_session(
        provider="claude",
        provider_engine="cli-local",
        native_session_id="session-1",
        title="Local",
        cwd="/repo",
        source_kind="claude_cli_local",
        status="running",
    )
    second = store.get_or_create_session(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id="session-1",
        title="SDK",
        cwd="/repo",
        source_kind="claude_sdk_deepseek",
        status="running",
    )

    assert first.id != second.id
    assert first.agent_run_id != second.agent_run_id
    assert first.provider_engine == "cli-local"
    assert second.provider_engine == "sdk-deepseek"


def test_get_or_create_session_reuses_existing_row(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        title="old",
        cwd="/repo",
        source_kind="codex_native",
        status="queued",
    )
    second = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        title="new",
        cwd="/repo",
        source_kind="codex_native",
        status="running",
        last_turn_id="turn-1",
    )

    assert second.id == first.id
    assert second.agent_run_id == first.agent_run_id
    assert second.title == "new"
    assert second.status == "running"
    assert second.last_turn_id == "turn-1"


def test_list_recent_filters_by_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    codex = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        title="Codex",
        cwd="/repo",
        source_kind="codex_native",
        status="done",
    )
    claude = store.get_or_create_session(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id="session-1",
        title="Claude",
        cwd="/repo",
        source_kind="claude_sdk_deepseek",
        status="running",
    )

    assert [session.id for session in store.list_recent(provider="claude")] == [claude.id]
    assert [session.id for session in store.list_recent(provider="codex")] == [codex.id]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_session_store.py -q
```

Expected: fail because `NativeAgentSessionStore` and `native_agent_sessions` do not exist.

- [ ] **Step 3: Add database table**

Modify `wlcodex/db.py` near the existing `native_codex_sessions` migration:

```python
            CREATE TABLE IF NOT EXISTS native_agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_engine TEXT NOT NULL,
                native_session_id TEXT NOT NULL,
                agent_run_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL DEFAULT 'unknown',
                last_turn_id TEXT NOT NULL DEFAULT '',
                activity_at TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id),
                FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_native_agent_sessions_identity
                ON native_agent_sessions(provider, provider_engine, native_session_id);
            CREATE INDEX IF NOT EXISTS idx_native_agent_sessions_provider_updated
                ON native_agent_sessions(provider, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_native_agent_sessions_agent_run
                ON native_agent_sessions(agent_run_id);
```

Add this migration-safety assertion to `tests/test_db.py`:

```python
def test_migrate_creates_native_agent_sessions(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    row = ledger._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'native_agent_sessions'"
    ).fetchone()

    assert row is not None
```

- [ ] **Step 4: Add session store implementation**

Create `wlcodex/native_agents/session_store.py`:

```python
from __future__ import annotations

import json
import sqlite3
from typing import Any

from wlcodex.db import Ledger, _now
from wlcodex.native_agents.models import NativeAgentSession

_NATIVE_CHAT_ID = 0
_NATIVE_USER_ID = 0
_NATIVE_WORKSPACE_ALIAS = "wlcodex"


class NativeAgentSessionStore:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._conn = ledger._conn

    def get_or_create_native_conversation(self, provider: str) -> int:
        mode = f"{provider}_native"
        row = self._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE chat_id = ? AND user_id = ? AND mode = ?
              AND workspace_alias = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (_NATIVE_CHAT_ID, _NATIVE_USER_ID, mode, _NATIVE_WORKSPACE_ALIAS),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        conversation = self._ledger.create_conversation(
            chat_id=_NATIVE_CHAT_ID,
            user_id=_NATIVE_USER_ID,
            title=f"{provider.title()} Native",
            mode=mode,
            workspace_alias=_NATIVE_WORKSPACE_ALIAS,
        )
        return conversation.id

    def get_by_native_session_id(
        self,
        *,
        provider: str,
        provider_engine: str,
        native_session_id: str,
    ) -> NativeAgentSession | None:
        row = self._conn.execute(
            """
            SELECT * FROM native_agent_sessions
            WHERE provider = ? AND provider_engine = ? AND native_session_id = ?
            """,
            (provider, provider_engine, native_session_id),
        ).fetchone()
        return _session(row) if row is not None else None

    def get_or_create_session(
        self,
        *,
        provider: str,
        provider_engine: str,
        native_session_id: str,
        title: str = "",
        cwd: str = "",
        source_kind: str = "unknown",
        status: str = "unknown",
        last_turn_id: str = "",
        activity_at: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> NativeAgentSession:
        existing = self.get_by_native_session_id(
            provider=provider,
            provider_engine=provider_engine,
            native_session_id=native_session_id,
        )
        if existing is not None:
            return self.update_session(
                session_id=existing.id,
                title=title or existing.title,
                cwd=cwd or existing.cwd,
                source_kind=source_kind or existing.source_kind,
                status=status or existing.status,
                last_turn_id=last_turn_id or existing.last_turn_id,
                activity_at=activity_at or existing.activity_at,
                metadata=metadata if metadata is not None else existing.metadata,
            )

        conversation_id = self.get_or_create_native_conversation(provider)
        agent_run = self._ledger.create_agent_run(
            conversation_id,
            agent=provider,
            role=f"{provider}_native",
            external_session_id=native_session_id,
            prompt_packet_summary=f"{provider} native session",
        )
        self._ledger.update_agent_run_status(
            agent_run.id,
            _agent_run_status(status),
            external_session_id=native_session_id,
        )
        now = _now()
        self._conn.execute(
            """
            INSERT INTO native_agent_sessions (
                provider, provider_engine, native_session_id, agent_run_id,
                conversation_id, title, cwd, source_kind, status, last_turn_id,
                activity_at, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                provider_engine,
                native_session_id,
                agent_run.id,
                conversation_id,
                title,
                cwd,
                source_kind,
                status,
                last_turn_id,
                activity_at or now,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        self._conn.commit()
        created = self.get_by_native_session_id(
            provider=provider,
            provider_engine=provider_engine,
            native_session_id=native_session_id,
        )
        if created is None:
            raise KeyError(f"native agent session was not created: {native_session_id}")
        return created

    def update_session(
        self,
        session_id: int,
        *,
        title: str | None = None,
        cwd: str | None = None,
        source_kind: str | None = None,
        status: str | None = None,
        last_turn_id: str | None = None,
        activity_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NativeAgentSession:
        existing = self._lookup_session(session_id)
        if status is not None:
            self._ledger.update_agent_run_status(
                existing.agent_run_id,
                _agent_run_status(status),
                external_session_id=existing.native_session_id,
            )
        self._conn.execute(
            """
            UPDATE native_agent_sessions
            SET title = ?, cwd = ?, source_kind = ?, status = ?,
                last_turn_id = ?, activity_at = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title if title is not None else existing.title,
                cwd if cwd is not None else existing.cwd,
                source_kind if source_kind is not None else existing.source_kind,
                status if status is not None else existing.status,
                last_turn_id if last_turn_id is not None else existing.last_turn_id,
                activity_at if activity_at is not None else existing.activity_at,
                json.dumps(
                    metadata if metadata is not None else existing.metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                _now(),
                existing.id,
            ),
        )
        self._conn.commit()
        return self._lookup_session(existing.id)

    def list_recent(self, *, provider: str, limit: int = 50) -> list[NativeAgentSession]:
        rows = self._conn.execute(
            """
            SELECT * FROM native_agent_sessions
            WHERE provider = ?
            ORDER BY
                CASE
                    WHEN activity_at IS NOT NULL AND activity_at != '' THEN activity_at
                    ELSE updated_at
                END DESC,
                id DESC
            LIMIT ?
            """,
            (provider, limit),
        ).fetchall()
        return [_session(row) for row in rows]

    def _lookup_session(self, session_id: int) -> NativeAgentSession:
        row = self._conn.execute(
            "SELECT * FROM native_agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown native agent session id: {session_id}")
        return _session(row)


def _agent_run_status(status: str) -> str:
    if status in ("running", "active"):
        return "running"
    if status in ("done", "completed"):
        return "done"
    if status in ("failed", "error"):
        return "failed"
    return "queued"


def _session(row: sqlite3.Row) -> NativeAgentSession:
    return NativeAgentSession(
        id=int(row["id"]),
        provider=str(row["provider"]),
        provider_engine=str(row["provider_engine"]),
        native_session_id=str(row["native_session_id"]),
        agent_run_id=int(row["agent_run_id"]),
        conversation_id=int(row["conversation_id"]),
        title=str(row["title"]),
        cwd=str(row["cwd"]),
        source_kind=str(row["source_kind"]),
        status=str(row["status"]),
        last_turn_id=str(row["last_turn_id"]),
        activity_at=str(row["activity_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        metadata=json.loads(str(row["metadata_json"] or "{}")),
    )
```

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_session_store.py tests/test_db.py::test_migrate_creates_native_agent_sessions -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/db.py wlcodex/native_agents/session_store.py tests/test_native_agent_session_store.py tests/test_db.py
git commit -m "feat: add native agent session store"
```

---

### Task 3: Native-Agent Configuration And Claude Engine Validation

**Files:**
- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_native_agent_config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write config tests**

Create `tests/test_native_agent_config.py`:

```python
from pathlib import Path

import pytest

from wlcodex.config import ConfigError, load_config


BASE = """
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [1]

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "{sqlite_path}"
task_log_dir = "{task_log_dir}"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "{workspace}"
allow_write = true
"""


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "wlcodex.toml"
    path.write_text(
        BASE.format(
            sqlite_path=tmp_path / "db.sqlite3",
            task_log_dir=tmp_path / "logs",
            workspace=tmp_path,
        )
        + extra,
        encoding="utf-8",
    )
    return path


def test_native_agents_default_to_codex_only_compatibility(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    assert config.native_agents.enabled is False
    assert config.native_agents.default_provider == "codex"
    assert config.native_agents.codex.enabled is False
    assert config.native_agents.claude.enabled is False
    assert config.native_agents.claude.engine == "cli-local"


def test_native_agents_parse_claude_sdk_deepseek(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            """
[native_agents]
enabled = true
default_provider = "claude"

[native_agents.claude]
enabled = true
engine = "sdk-deepseek"

[native_agents.claude.sdk_deepseek]
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"
""",
        )
    )

    assert config.native_agents.enabled is True
    assert config.native_agents.default_provider == "claude"
    assert config.native_agents.claude.enabled is True
    assert config.native_agents.claude.engine == "sdk-deepseek"
    assert config.native_agents.claude.sdk_deepseek.model == "deepseek-v4-pro"


def test_native_agents_reject_claude_engine_as_provider(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="default_provider must be codex, claude, or antigravity"):
        load_config(
            _write_config(
                tmp_path,
                """
[native_agents]
enabled = true
default_provider = "claude-deepseek"
""",
            )
        )


def test_native_agents_reject_legacy_dual_claude_enabled_flags(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="claude engine must be selected by native_agents.claude.engine"):
        load_config(
            _write_config(
                tmp_path,
                """
[native_agents.claude]
enabled = true
engine = "cli-local"

[native_agents.claude.cli_local]
enabled = true

[native_agents.claude.sdk_deepseek]
enabled = true
""",
            )
        )
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_config.py -q
```

Expected: fail because `AppConfig` has no `native_agents`.

- [ ] **Step 3: Add config dataclasses**

Modify `wlcodex/config.py` near the existing native config dataclasses:

```python
@dataclass(frozen=True)
class NativeAgentsCodexConfig:
    enabled: bool = False


@dataclass(frozen=True)
class NativeAgentsClaudeCliLocalConfig:
    binary: str = "auto"
    model: str = ""
    permission_mode: str = "acceptEdits"


@dataclass(frozen=True)
class NativeAgentsClaudeSdkDeepSeekConfig:
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com/anthropic"
    model: str = "deepseek-v4-pro"


@dataclass(frozen=True)
class NativeAgentsClaudeConfig:
    enabled: bool = False
    engine: str = "cli-local"
    cli_local: NativeAgentsClaudeCliLocalConfig = NativeAgentsClaudeCliLocalConfig()
    sdk_deepseek: NativeAgentsClaudeSdkDeepSeekConfig = (
        NativeAgentsClaudeSdkDeepSeekConfig()
    )


@dataclass(frozen=True)
class NativeAgentsAntigravityConfig:
    enabled: bool = False
    engine: str = "sdk"


@dataclass(frozen=True)
class NativeAgentsConfig:
    enabled: bool = False
    default_provider: str = "codex"
    codex: NativeAgentsCodexConfig = NativeAgentsCodexConfig()
    claude: NativeAgentsClaudeConfig = NativeAgentsClaudeConfig()
    antigravity: NativeAgentsAntigravityConfig = NativeAgentsAntigravityConfig()
```

Add `native_agents: NativeAgentsConfig = NativeAgentsConfig()` to `AppConfig`.

- [ ] **Step 4: Add parser**

Add this parser to `wlcodex/config.py`:

```python
def _native_agents_config(data: dict[str, object]) -> NativeAgentsConfig:
    default_provider = str(data.get("default_provider", "codex"))
    if default_provider not in {"codex", "claude", "antigravity"}:
        raise ConfigError("native_agents.default_provider must be codex, claude, or antigravity")

    codex_raw = dict(data.get("codex", {}) or {})
    claude_raw = dict(data.get("claude", {}) or {})
    antigravity_raw = dict(data.get("antigravity", {}) or {})
    cli_raw = dict(claude_raw.get("cli_local", {}) or {})
    sdk_raw = dict(claude_raw.get("sdk_deepseek", {}) or {})

    if "enabled" in cli_raw or "enabled" in sdk_raw:
        raise ConfigError(
            "claude engine must be selected by native_agents.claude.engine, "
            "not by per-engine enabled flags"
        )

    engine = str(claude_raw.get("engine", "cli-local"))
    if engine not in {"cli-local", "sdk-deepseek"}:
        raise ConfigError("native_agents.claude.engine must be cli-local or sdk-deepseek")

    antigravity_engine = str(antigravity_raw.get("engine", "sdk"))
    if antigravity_engine != "sdk":
        raise ConfigError("native_agents.antigravity.engine must be sdk")

    try:
        cli_permission_mode = normalize_claude_permission_mode(
            str(cli_raw.get("permission_mode", "acceptEdits"))
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return NativeAgentsConfig(
        enabled=bool(data.get("enabled", False)),
        default_provider=default_provider,
        codex=NativeAgentsCodexConfig(
            enabled=bool(codex_raw.get("enabled", False)),
        ),
        claude=NativeAgentsClaudeConfig(
            enabled=bool(claude_raw.get("enabled", False)),
            engine=engine,
            cli_local=NativeAgentsClaudeCliLocalConfig(
                binary=str(cli_raw.get("binary", "auto")),
                model=str(cli_raw.get("model", "")),
                permission_mode=cli_permission_mode,
            ),
            sdk_deepseek=NativeAgentsClaudeSdkDeepSeekConfig(
                api_key_env=str(sdk_raw.get("api_key_env", "DEEPSEEK_API_KEY")),
                base_url=str(
                    sdk_raw.get("base_url", "https://api.deepseek.com/anthropic")
                ),
                model=str(sdk_raw.get("model", "deepseek-v4-pro")),
            ),
        ),
        antigravity=NativeAgentsAntigravityConfig(
            enabled=bool(antigravity_raw.get("enabled", False)),
            engine=antigravity_engine,
        ),
    )
```

In `load_config`, read:

```python
    native_agents_raw = data.get("native_agents", {})
```

and pass:

```python
        native_agents=_native_agents_config(native_agents_raw),
```

- [ ] **Step 5: Preserve old `[codex_native]` compatibility**

Add this compatibility rule after parsing `codex_native_raw` and `native_agents_raw`:

```python
    native_agents_config = _native_agents_config(native_agents_raw)
    codex_native_config = _codex_native_config(codex_native_raw)
    if codex_native_config.enabled and not native_agents_config.enabled:
        native_agents_config = NativeAgentsConfig(
            enabled=True,
            default_provider="codex",
            codex=NativeAgentsCodexConfig(enabled=True),
            claude=native_agents_config.claude,
            antigravity=native_agents_config.antigravity,
        )
```

Use `native_agents=native_agents_config` and `codex_native=codex_native_config` in the `AppConfig` constructor.

- [ ] **Step 6: Update example config**

Add this to `config/wlcodex.example.toml`:

```toml
[native_agents]
enabled = false
default_provider = "codex"

[native_agents.codex]
enabled = false

[native_agents.claude]
enabled = false
engine = "cli-local"

[native_agents.claude.cli_local]
binary = "auto"
model = ""
permission_mode = "acceptEdits"

[native_agents.claude.sdk_deepseek]
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"

[native_agents.antigravity]
enabled = false
engine = "sdk"
```

- [ ] **Step 7: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_config.py tests/test_config.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add wlcodex/config.py config/wlcodex.example.toml tests/test_native_agent_config.py tests/test_config.py
git commit -m "feat: add native agent configuration"
```

---

### Task 4: Codex Provider Adapter And Generic Registry Composition

**Files:**
- Create: `wlcodex/native_agents/codex_provider.py`
- Modify: `wlcodex/main.py`
- Test: `tests/test_native_agent_codex_provider.py`
- Test: `tests/test_main_composition.py`

- [ ] **Step 1: Write Codex adapter tests**

Create `tests/test_native_agent_codex_provider.py`:

```python
from dataclasses import dataclass

import pytest

from wlcodex.codex_native.models import (
    NativeCodexControlResult,
    NativeCodexSession,
    NativeCodexStatus,
)
from wlcodex.native_agents.codex_provider import CodexAppServerProvider


@dataclass
class FakeCodexController:
    async def status(self):
        return NativeCodexStatus(
            enabled=True,
            connected=True,
            remote_control_status="enabled",
            server_name="Codex",
        )

    async def list_sessions(self, limit: int = 50):
        return [
            NativeCodexSession(
                id=1,
                native_thread_id="thread-1",
                agent_run_id=2,
                conversation_id=3,
                title="Codex work",
                cwd="/repo",
                source_kind="codex_native",
                status="running",
                last_turn_id="turn-1",
                activity_at="2026-06-01T00:00:00Z",
                created_at="2026-06-01T00:00:00Z",
                updated_at="2026-06-01T00:01:00Z",
            )
        ]

    async def list_models(self):
        return [{"id": "gpt-5.1-codex"}]

    async def start_session(self, cwd: str, prompt: str, **kwargs):
        return NativeCodexControlResult(
            native_thread_id="thread-2",
            agent_run_id=4,
            status="started",
        )


@pytest.mark.asyncio
async def test_codex_provider_normalizes_status_and_sessions() -> None:
    provider = CodexAppServerProvider(FakeCodexController())

    status = await provider.status()
    sessions = await provider.list_sessions()

    assert provider.provider == "codex"
    assert provider.provider_engine == "app-server"
    assert status.provider == "codex"
    assert status.status_code == "enabled"
    assert sessions[0].provider == "codex"
    assert sessions[0].native_session_id == "thread-1"


@pytest.mark.asyncio
async def test_codex_provider_wraps_control_result() -> None:
    provider = CodexAppServerProvider(FakeCodexController())

    result = await provider.start_session("/repo", "fix it")

    assert result.provider == "codex"
    assert result.provider_engine == "app-server"
    assert result.native_session_id == "thread-2"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_codex_provider.py -q
```

Expected: fail because `codex_provider.py` does not exist.

- [ ] **Step 3: Implement Codex provider**

Create `wlcodex/native_agents/codex_provider.py`:

```python
from __future__ import annotations

from typing import Any

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)


class CodexAppServerProvider:
    provider = "codex"
    provider_engine = "app-server"

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    async def status(self) -> NativeAgentStatus:
        status = await self._controller.status()
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=bool(getattr(status, "enabled", True)),
            connected=bool(getattr(status, "connected", False)),
            status_code=str(getattr(status, "remote_control_status", "unknown")),
            message=str(getattr(status, "error", "") or ""),
            metadata={
                "server_name": str(getattr(status, "server_name", "") or ""),
                "installation_id": str(getattr(status, "installation_id", "") or ""),
                "environment_id": getattr(status, "environment_id", None),
            },
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_list_models=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_steer_active_turn=True,
            can_interrupt=True,
            can_resolve_approval=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        sessions = await self._controller.list_sessions(limit)
        return [_session_from_codex(session) for session in sessions]

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._controller.list_models()

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any) -> NativeAgentControlResult:
        result = await self._controller.start_session(cwd, prompt, **kwargs)
        return _result_from_codex(result)

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        result = await self._controller.create_session(cwd, **kwargs)
        return _result_from_codex(result)

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        return await self._controller.read_session(native_session_id)

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        return _result_from_codex(await self._controller.attach_session(native_session_id))

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        return _result_from_codex(await self._controller.sync_session(native_session_id))

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        return _result_from_codex(
            await self._controller.continue_session(native_session_id, prompt, **kwargs)
        )

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        return _result_from_codex(
            await self._controller.steer_session(
                native_session_id,
                expected_turn_id,
                prompt,
                **kwargs,
            )
        )

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult:
        return _result_from_codex(
            await self._controller.interrupt_session(native_session_id, turn_id)
        )

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> NativeAgentControlResult:
        return _result_from_codex(await self._controller.resolve_approval(request_id, body))


def _session_from_codex(session: Any) -> NativeAgentSession:
    return NativeAgentSession(
        id=int(session.id),
        provider="codex",
        provider_engine="app-server",
        native_session_id=str(session.native_thread_id),
        agent_run_id=int(session.agent_run_id),
        conversation_id=int(session.conversation_id),
        title=str(session.title),
        cwd=str(session.cwd),
        source_kind=str(session.source_kind),
        status=str(session.status),
        last_turn_id=str(session.last_turn_id),
        activity_at=str(getattr(session, "activity_at", "") or ""),
        created_at=str(session.created_at),
        updated_at=str(session.updated_at),
    )


def _result_from_codex(result: Any) -> NativeAgentControlResult:
    return NativeAgentControlResult(
        provider="codex",
        provider_engine="app-server",
        native_session_id=str(result.native_thread_id),
        agent_run_id=int(result.agent_run_id),
        turn_id=str(getattr(result, "turn_id", "") or ""),
        active_turn_id=str(getattr(result, "active_turn_id", "") or ""),
        turn_running=bool(getattr(result, "turn_running", False)),
        status=str(getattr(result, "status", "ok")),
    )
```

- [ ] **Step 4: Compose registry in main**

Modify `wlcodex/main.py` inside the live stream setup:

```python
    native_registry = None
    native_providers = []
```

After `native_controller = CodexNativeController(...)`, add:

```python
            from wlcodex.native_agents.codex_provider import CodexAppServerProvider

            native_providers.append(CodexAppServerProvider(native_controller))
```

After optional provider construction, add:

```python
    if native_providers:
        from wlcodex.native_agents.provider import NativeAgentRegistry

        native_registry = NativeAgentRegistry(native_providers)
```

Pass `native_registry=native_registry` to `WorkerLiveStreamServer` in Task 5.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_codex_provider.py tests/test_main_composition.py -q
```

Expected: all tests pass or unrelated existing dirty-worktree assertions remain unchanged. If `tests/test_main_composition.py` fails due to constructor signature updates, apply Task 5 before re-running it.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/native_agents/codex_provider.py wlcodex/main.py tests/test_native_agent_codex_provider.py tests/test_main_composition.py
git commit -m "feat: adapt codex native control as provider"
```

---

### Task 5: Generic Native-Agent Routes With Codex Compatibility

**Files:**
- Modify: `wlcodex/live_stream/server.py`
- Test: `tests/test_worker_live_stream_native_agent_routes.py`
- Test: `tests/test_worker_live_stream_native_routes.py`

- [ ] **Step 1: Write generic route tests**

Create `tests/test_worker_live_stream_native_agent_routes.py` with the same test harness style used in `tests/test_worker_live_stream_native_routes.py`:

```python
import json
from types import SimpleNamespace

import pytest

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.provider import NativeAgentRegistry


class FakeProvider:
    provider = "claude"
    provider_engine = "sdk-deepseek"

    async def status(self):
        return NativeAgentStatus(
            provider="claude",
            provider_engine="sdk-deepseek",
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self):
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_continue_session=True,
        )

    async def list_sessions(self, limit: int = 50):
        return [
            NativeAgentSession(
                id=1,
                provider="claude",
                provider_engine="sdk-deepseek",
                native_session_id="session-1",
                agent_run_id=2,
                conversation_id=3,
                title="Claude work",
                cwd="/repo",
                source_kind="claude_sdk_deepseek",
                status="running",
                last_turn_id="",
                activity_at="2026-06-01T00:00:00Z",
                created_at="2026-06-01T00:00:00Z",
                updated_at="2026-06-01T00:00:00Z",
            )
        ]

    async def list_models(self):
        return [{"id": "deepseek-v4-pro"}]

    async def start_session(self, cwd: str, prompt: str, **kwargs):
        return NativeAgentControlResult(
            provider="claude",
            provider_engine="sdk-deepseek",
            native_session_id="session-2",
            agent_run_id=4,
            status="started",
        )


def test_fake_provider_contract_shape() -> None:
    registry = NativeAgentRegistry([FakeProvider()])

    assert registry.get("claude").provider_engine == "sdk-deepseek"
```

Add HTTP tests by copying the `_request` helper from `test_worker_live_stream_native_routes.py` and creating a `WorkerLiveStreamServer(..., native_registry=NativeAgentRegistry([FakeProvider()]))`. The assertions:

```python
assert "HTTP/1.1 200 OK" in response
payload = json.loads(response.split("\r\n\r\n", 1)[1])
assert payload["status_code"] == "ok"
assert payload["provider_engine"] == "sdk-deepseek"
```

Target paths:

```text
GET /api/native/claude/status
GET /api/native/claude/capabilities
GET /api/native/claude/sessions
POST /api/native/claude/sessions/start
GET /api/native/unknown/status
```

Expected unknown provider response:

```python
assert "HTTP/1.1 404 Not Found" in response
assert json.loads(body)["error"] == "unknown native provider"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_agent_routes.py -q
```

Expected: fail because server has no `native_registry` and only recognizes `/api/native/codex`.

- [ ] **Step 3: Extend server constructor**

Modify `WorkerLiveStreamServer.__init__` in `wlcodex/live_stream/server.py`:

```python
        native_registry: object | None = None,
```

Store:

```python
        self._native_registry = native_registry
```

Keep `native_controller` for compatibility during this task.

- [ ] **Step 4: Route generic native API paths**

In the request dispatcher, replace the hardcoded check:

```python
            if parsed.path.startswith("/api/native/codex"):
                await self._handle_native_route(...)
```

with:

```python
            if parsed.path.startswith("/api/native/"):
                await self._handle_native_agent_route(
                    reader,
                    writer,
                    method,
                    parsed.path,
                    headers,
                    query,
                )
                return
```

Rename the existing `_handle_native_route` body to `_handle_native_agent_route` and make it parse:

```python
        parts = [part for part in path.split("/") if part]
        provider_name = parts[2] if len(parts) >= 3 else ""
        provider = self._native_provider(provider_name)
```

Add helper:

```python
    def _native_provider(self, provider_name: str):
        if self._native_registry is not None:
            provider = self._native_registry.maybe_get(provider_name)
            if provider is not None:
                return provider
        if provider_name == "codex" and self._native_controller is not None:
            from wlcodex.native_agents.codex_provider import CodexAppServerProvider

            return CodexAppServerProvider(self._native_controller)
        return None
```

When provider is missing, return:

```python
await self._send_json(writer, 404, {"error": "unknown native provider"})
```

- [ ] **Step 5: Make route base provider-aware**

Inside `_handle_native_agent_route`, compute:

```python
        base = f"/api/native/{provider_name}"
```

Then call `provider.status()`, `provider.capabilities()`, `provider.list_sessions()`, and the provider control methods instead of `self._native_controller`.

Add `/capabilities`:

```python
        if path == f"{base}/capabilities":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            await self._send_json(writer, 200, provider.capabilities().to_json_dict())
            return
```

- [ ] **Step 6: Preserve login-ticket compatibility**

For `/api/native/{provider}/login-ticket`, return a provider-aware path:

```python
                    "path": f"/native/{provider_name}/login?ticket={quote(ticket, safe='')}",
```

Existing `/api/native/codex/login-ticket` must continue returning `/native/codex/login?...`.

- [ ] **Step 7: Run route tests and old Codex route tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_agent_routes.py tests/test_worker_live_stream_native_routes.py -q
```

Expected: all tests pass. The old Codex tests prove compatibility.

- [ ] **Step 8: Commit**

```bash
git add wlcodex/live_stream/server.py tests/test_worker_live_stream_native_agent_routes.py tests/test_worker_live_stream_native_routes.py
git commit -m "feat: add generic native agent routes"
```

---

### Task 6: Provider-Aware Native Web UI

**Files:**
- Modify: `wlcodex/live_stream/server.py`
- Test: `tests/test_worker_live_stream_native_agent_routes.py`
- Test: `tests/test_worker_live_stream_native_routes.py`

- [ ] **Step 1: Add page-rendering tests**

Add tests to `tests/test_worker_live_stream_native_agent_routes.py`:

```python
def test_native_claude_page_uses_claude_api_base() -> None:
    response = _request(
        server_with_fake_registry(),
        "GET /native/claude HTTP/1.1\r\nHost: localhost\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROVIDER = "claude";' in response
    assert 'const API_BASE = "/api/native/claude";' in response
    assert "/api/native/codex/sessions" not in response


def test_native_root_lists_providers() -> None:
    response = _request(
        server_with_fake_registry(),
        "GET /native HTTP/1.1\r\nHost: localhost\r\n\r\n",
    )

    assert "HTTP/1.1 200 OK" in response
    assert "/native/claude" in response
    assert "Claude" in response
```

Keep the existing test in `tests/test_worker_live_stream_native_routes.py` that asserts `/native/codex` still contains Codex API paths, but update it to accept `API_BASE` construction:

```python
assert 'const API_BASE = "/api/native/codex";' in response
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_agent_routes.py::test_native_claude_page_uses_claude_api_base tests/test_worker_live_stream_native_agent_routes.py::test_native_root_lists_providers -q
```

Expected: fail because `/native/claude` and `/native` are not rendered.

- [ ] **Step 3: Add generic native page routing**

In the top-level request dispatcher:

```python
            if parsed.path == "/native":
                await self._send_native_provider_index(writer, headers, query)
                return

            if parsed.path.startswith("/native/"):
                provider_name = parsed.path.split("/", 3)[2]
                if provider_name and provider_name != "codex":
                    await self._send_native_page(writer, provider_name, headers, query)
                    return
```

Keep existing `/native/codex` route behavior, but internally call the same `_send_native_page(writer, "codex", ...)`.

- [ ] **Step 4: Convert hardcoded JS endpoints to `API_BASE`**

In the native page HTML script, add:

```javascript
const PROVIDER = "__PROVIDER__";
const API_BASE = `/api/native/${PROVIDER}`;
```

Replace hardcoded calls:

```javascript
api("/api/native/codex/status")
api("/api/native/codex/sessions")
api("/api/native/codex/sessions/start")
api("/api/native/codex/models")
```

with:

```javascript
api(`${API_BASE}/status`)
api(`${API_BASE}/sessions`)
api(`${API_BASE}/sessions/start`)
api(`${API_BASE}/models`)
```

Replace template endpoints:

```javascript
`/api/native/codex/sessions/${encodeURIComponent(nativeThreadId)}/${action}`
`/api/native/codex/approvals/${encodeURIComponent(requestId)}/resolve`
```

with:

```javascript
`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/${action}`
`${API_BASE}/approvals/${encodeURIComponent(requestId)}/resolve`
```

- [ ] **Step 5: Render provider index**

Add `_send_native_provider_index`:

```python
    async def _send_native_provider_index(
        self,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        if not self._is_authorized(
            writer,
            headers,
            query,
            require_token=self._native_registry is not None or self._native_controller is not None,
        ):
            await self._send_redirect(writer, "/native/codex")
            return
        providers = []
        if self._native_registry is not None:
            providers = self._native_registry.list_provider_summaries()
        elif self._native_controller is not None:
            providers = [{"provider": "codex", "provider_engine": "app-server"}]
        body = _native_provider_index_html(providers)
        await self._send_html(writer, body)
```

Add helper HTML with links only:

```python
def _native_provider_index_html(providers: list[dict[str, str]]) -> str:
    links = "\n".join(
        f'<a class="provider" href="/native/{escape(provider["provider"])}">'
        f'{escape(provider["provider"].title())}'
        f'<span>{escape(provider["provider_engine"])}</span></a>'
        for provider in providers
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Native Agents</title></head>
<body><main>{links}</main></body></html>"""
```

Use `html.escape` imported as `escape`.

- [ ] **Step 6: Run UI route tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_live_stream_native_agent_routes.py tests/test_worker_live_stream_native_routes.py -q
```

Expected: all tests pass and old `/native/codex` still works.

- [ ] **Step 7: Commit**

```bash
git add wlcodex/live_stream/server.py tests/test_worker_live_stream_native_agent_routes.py tests/test_worker_live_stream_native_routes.py
git commit -m "feat: render provider-aware native web UI"
```

---

### Task 7: Claude CLI Local Provider

**Files:**
- Modify: `wlcodex/claude_binary.py`
- Modify: `wlcodex/claude_backend.py`
- Create: `wlcodex/native_agents/claude_cli_provider.py`
- Test: `tests/test_claude_binary.py`
- Test: `tests/test_claude_backend.py`
- Test: `tests/test_native_agent_claude_cli_provider.py`

- [ ] **Step 1: Add binary and model-argument tests**

Add to `tests/test_claude_binary.py`:

```python
def test_resolve_claude_binary_checks_local_bin_in_auto_mode(tmp_path: Path) -> None:
    home = tmp_path
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    binary = local_bin / "claude"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    result = resolve_claude_binary("auto", env={"PATH": ""}, home=home)

    assert result.binary == str(binary)
    assert result.source == "local-bin"
```

Add to `tests/test_claude_backend.py`:

```python
def test_prompt_args_omits_model_when_model_is_empty() -> None:
    backend = ClaudeBackend(ClaudeConfig(enabled=True, binary="/bin/echo", model=""))

    args = backend._prompt_args("hello", stream_json=True)

    assert "--model" not in args
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_binary.py::test_resolve_claude_binary_checks_local_bin_in_auto_mode tests/test_claude_backend.py::test_prompt_args_omits_model_when_model_is_empty -q
```

Expected: local-bin test fails; model test may already pass after current code inspection, but keep it as protection.

- [ ] **Step 3: Resolve `~/.local/bin/claude`**

Modify `resolve_claude_binary` in `wlcodex/claude_binary.py` after the PATH lookup:

```python
    attempted.append("~/.local/bin/claude")
    local_bin = home_path / ".local" / "bin" / "claude"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return ClaudeBinaryResolution(
            str(local_bin),
            "local-bin",
            attempted=tuple(attempted),
        )
```

- [ ] **Step 4: Ensure empty model means no CLI model override**

In `wlcodex/claude_backend.py`, keep this guard in both `_prompt_args` and `send_terminal_input`:

```python
        if self._config.model and capabilities.model:
            args.extend(["--model", normalize_claude_model_name(self._config.model)])
```

Do not replace it with a default model for native-agent CLI use.

- [ ] **Step 5: Write Claude CLI provider tests**

Create `tests/test_native_agent_claude_cli_provider.py`:

```python
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.claude_cli_provider import ClaudeCliLocalProvider
from wlcodex.native_agents.session_store import NativeAgentSessionStore


class FakeClaudeEngine:
    enabled = True

    async def send_streaming(self, request):
        self.request = request
        yield type("Event", (), {"delta": "hello", "event_type": "text"})()


def _provider(tmp_path: Path) -> ClaudeCliLocalProvider:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    return ClaudeCliLocalProvider(
        engine=FakeClaudeEngine(),
        session_store=NativeAgentSessionStore(ledger),
        default_cwd=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_claude_cli_provider_starts_session_and_records_agent_run(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    result = await provider.start_session(str(tmp_path), "say hi")

    assert result.provider == "claude"
    assert result.provider_engine == "cli-local"
    assert result.native_session_id
    assert result.agent_run_id > 0


@pytest.mark.asyncio
async def test_claude_cli_provider_capabilities_disable_active_turn_steering(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    caps = provider.capabilities()

    assert caps.can_start_session is True
    assert caps.can_continue_session is True
    assert caps.can_steer_active_turn is False
    assert "can_steer_active_turn" in caps.disabled_reasons
```

- [ ] **Step 6: Implement Claude CLI provider**

Create `wlcodex/native_agents/claude_cli_provider.py`:

```python
from __future__ import annotations

from typing import Any
from uuid import uuid4

from wlcodex.agent_backend import AgentRequest
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore


class ClaudeCliLocalProvider:
    provider = "claude"
    provider_engine = "cli-local"

    def __init__(
        self,
        *,
        engine: Any,
        session_store: NativeAgentSessionStore,
        default_cwd: str = "",
    ) -> None:
        self._engine = engine
        self._session_store = session_store
        self._default_cwd = default_cwd

    async def status(self) -> NativeAgentStatus:
        enabled = bool(getattr(self._engine, "enabled", False))
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=enabled,
            connected=enabled,
            status_code="ok" if enabled else "disabled",
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_interrupt=False,
            can_resolve_approval=False,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": "Claude Code CLI continuation starts a new prompt turn.",
                "can_interrupt": "Claude CLI provider does not hold a long-lived process handle yet.",
                "can_resolve_approval": "Claude CLI permissions are controlled by Claude Code permission mode.",
            },
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        return self._session_store.list_recent(provider=self.provider, limit=limit)

    async def list_models(self) -> list[dict[str, Any]]:
        return []

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any) -> NativeAgentControlResult:
        native_session_id = str(uuid4())
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=prompt.strip()[:80],
            cwd=cwd or self._default_cwd,
            source_kind="claude_cli_local",
            status="running",
        )
        async for _event in self._engine.send_streaming(
            AgentRequest(
                prompt=prompt,
                workspace_path=cwd or self._default_cwd,
                extra={"resume_session_id": ""},
            )
        ):
            pass
        session = self._session_store.update_session(session.id, status="done")
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=session.agent_run_id,
            status="started",
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=str(uuid4()),
            title="Claude CLI session",
            cwd=cwd or self._default_cwd,
            source_kind="claude_cli_local",
            status="created",
        )
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="created",
        )

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        return {"thread": session.to_json_dict(), "turns": []}

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="attached",
        )

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        return await self.attach_session(native_session_id)

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        async for _event in self._engine.send_streaming(
            AgentRequest(
                prompt=prompt,
                workspace_path=session.cwd,
                extra={"resume_session_id": native_session_id},
            )
        ):
            pass
        session = self._session_store.update_session(session.id, status="done")
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="continued",
        )

    async def steer_session(self, native_session_id: str, expected_turn_id: str, prompt: str, **kwargs: Any):
        raise NotImplementedError("Claude CLI provider does not support active-turn steering")

    async def interrupt_session(self, native_session_id: str, turn_id: str = ""):
        raise NotImplementedError("Claude CLI provider does not support interrupt yet")

    async def resolve_approval(self, request_id: str, body: dict[str, Any]):
        raise KeyError(request_id)
```

- [ ] **Step 7: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_binary.py tests/test_claude_backend.py tests/test_native_agent_claude_cli_provider.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add wlcodex/claude_binary.py wlcodex/claude_backend.py wlcodex/native_agents/claude_cli_provider.py tests/test_claude_binary.py tests/test_claude_backend.py tests/test_native_agent_claude_cli_provider.py
git commit -m "feat: add claude cli native provider"
```

---

### Task 8: Claude SDK DeepSeek Provider

**Files:**
- Create: `wlcodex/native_agents/claude_sdk_deepseek_provider.py`
- Modify: `pyproject.toml`
- Test: `tests/test_native_agent_claude_sdk_deepseek_provider.py`

- [ ] **Step 1: Add optional dependency extra**

Modify `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
  "pytest>=8,<10",
  "pytest-asyncio>=0.24,<2",
  "ruff>=0.8,<1",
]
claude-sdk = [
  "claude-agent-sdk>=0.1",
]
```

Keep existing `dev` entries exactly as they are and add only the new extra.

- [ ] **Step 2: Write SDK provider tests**

Create `tests/test_native_agent_claude_sdk_deepseek_provider.py`:

```python
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.claude_sdk_deepseek_provider import (
    ClaudeSdkDeepSeekConfig,
    ClaudeSdkDeepSeekProvider,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore


class FakeSdkRunner:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, *, prompt: str, cwd: str, session_id: str, config: ClaudeSdkDeepSeekConfig):
        self.calls.append((prompt, cwd, session_id, config.base_url, config.model))
        yield {"type": "assistant", "text": "done"}


def _provider(tmp_path: Path, *, env: dict[str, str] | None = None):
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    runner = FakeSdkRunner()
    provider = ClaudeSdkDeepSeekProvider(
        config=ClaudeSdkDeepSeekConfig(api_key_env="DEEPSEEK_API_KEY"),
        session_store=NativeAgentSessionStore(ledger),
        runner=runner,
        env=env or {"DEEPSEEK_API_KEY": "sk-test"},
    )
    return provider, runner


@pytest.mark.asyncio
async def test_status_reports_missing_api_key(tmp_path: Path) -> None:
    provider, _runner = _provider(tmp_path, env={})

    status = await provider.status()

    assert status.connected is False
    assert status.status_code == "missing_api_key"


@pytest.mark.asyncio
async def test_start_session_uses_deepseek_anthropic_endpoint(tmp_path: Path) -> None:
    provider, runner = _provider(tmp_path)

    result = await provider.start_session(str(tmp_path), "fix tests")

    assert result.provider == "claude"
    assert result.provider_engine == "sdk-deepseek"
    assert runner.calls[0][3] == "https://api.deepseek.com/anthropic"
    assert runner.calls[0][4] == "deepseek-v4-pro"


def test_capabilities_do_not_expose_second_claude_provider(tmp_path: Path) -> None:
    provider, _runner = _provider(tmp_path)

    assert provider.provider == "claude"
    assert provider.provider_engine == "sdk-deepseek"
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_claude_sdk_deepseek_provider.py -q
```

Expected: fail because the provider module does not exist.

- [ ] **Step 4: Implement SDK provider with injectable runner**

Create `wlcodex/native_agents/claude_sdk_deepseek_provider.py`:

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore


@dataclass(frozen=True)
class ClaudeSdkDeepSeekConfig:
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com/anthropic"
    model: str = "deepseek-v4-pro"


class ClaudeAgentSdkRunner:
    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
    ):
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as exc:
            raise RuntimeError("claude-agent-sdk is not installed") from exc

        old_api_key = os.environ.get("ANTHROPIC_API_KEY")
        old_base_url = os.environ.get("ANTHROPIC_BASE_URL")
        os.environ["ANTHROPIC_API_KEY"] = os.environ[config.api_key_env]
        os.environ["ANTHROPIC_BASE_URL"] = config.base_url
        try:
            options = ClaudeAgentOptions(
                cwd=cwd,
                model=config.model,
            )
            async for message in query(prompt=prompt, options=options):
                yield message
        finally:
            if old_api_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old_api_key
            if old_base_url is None:
                os.environ.pop("ANTHROPIC_BASE_URL", None)
            else:
                os.environ["ANTHROPIC_BASE_URL"] = old_base_url


class ClaudeSdkDeepSeekProvider:
    provider = "claude"
    provider_engine = "sdk-deepseek"

    def __init__(
        self,
        *,
        config: ClaudeSdkDeepSeekConfig,
        session_store: NativeAgentSessionStore,
        runner: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._session_store = session_store
        self._runner = runner or ClaudeAgentSdkRunner()
        self._env = env if env is not None else os.environ

    async def status(self) -> NativeAgentStatus:
        if not self._env.get(self._config.api_key_env):
            return NativeAgentStatus(
                provider=self.provider,
                provider_engine=self.provider_engine,
                enabled=True,
                connected=False,
                status_code="missing_api_key",
                message=f"{self._config.api_key_env} is not set.",
            )
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
            metadata={"base_url": self._config.base_url, "model": self._config.model},
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": "Claude Agent SDK sessions continue by prompt, not same-turn steering.",
                "can_interrupt": "SDK cancellation is not exposed through the first native-agent slice.",
                "can_resolve_approval": "SDK human-in-loop approval mapping is not enabled in this slice.",
            },
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        return self._session_store.list_recent(provider=self.provider, limit=limit)

    async def list_models(self) -> list[dict[str, Any]]:
        return [{"id": self._config.model}]

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any) -> NativeAgentControlResult:
        native_session_id = str(uuid4())
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=prompt.strip()[:80],
            cwd=cwd,
            source_kind="claude_sdk_deepseek",
            status="running",
            metadata={"model": self._config.model, "base_url": self._config.base_url},
        )
        async for _event in self._runner.run(
            prompt=prompt,
            cwd=cwd,
            session_id=native_session_id,
            config=self._config,
        ):
            pass
        session = self._session_store.update_session(session.id, status="done")
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=session.agent_run_id,
            status="started",
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=str(uuid4()),
            title="Claude DeepSeek SDK session",
            cwd=cwd,
            source_kind="claude_sdk_deepseek",
            status="created",
            metadata={"model": self._config.model, "base_url": self._config.base_url},
        )
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="created",
        )

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        return {"thread": session.to_json_dict(), "turns": []}

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="attached",
        )

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        return await self.attach_session(native_session_id)

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs: Any):
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        async for _event in self._runner.run(
            prompt=prompt,
            cwd=session.cwd,
            session_id=session.native_session_id,
            config=self._config,
        ):
            pass
        session = self._session_store.update_session(session.id, status="done")
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="continued",
        )
```

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_claude_sdk_deepseek_provider.py -q
```

Expected: all tests pass without requiring a real DeepSeek key.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml wlcodex/native_agents/claude_sdk_deepseek_provider.py tests/test_native_agent_claude_sdk_deepseek_provider.py
git commit -m "feat: add claude deepseek sdk provider"
```

---

### Task 9: Antigravity SDK Provider

**Files:**
- Create: `wlcodex/native_agents/antigravity_provider.py`
- Modify: `pyproject.toml`
- Modify: `wlcodex/runtime_events.py`
- Test: `tests/test_native_agent_antigravity_provider.py`

- [ ] **Step 1: Add optional dependency extra and event source**

Modify `pyproject.toml`:

```toml
antigravity-sdk = [
  "google-antigravity>=0.1",
]
```

Modify `EventSource` in `wlcodex/runtime_events.py`:

```python
    ANTIGRAVITY = "antigravity"
```

- [ ] **Step 2: Write Antigravity provider tests**

Create `tests/test_native_agent_antigravity_provider.py`:

```python
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.antigravity_provider import AntigravitySdkProvider
from wlcodex.native_agents.session_store import NativeAgentSessionStore


class FakeAntigravityRunner:
    available = True

    async def run(self, *, prompt: str, cwd: str, session_id: str):
        self.call = (prompt, cwd, session_id)
        yield {"type": "assistant", "text": "done"}


def _provider(tmp_path: Path, runner=None) -> AntigravitySdkProvider:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    return AntigravitySdkProvider(
        session_store=NativeAgentSessionStore(ledger),
        runner=runner or FakeAntigravityRunner(),
    )


@pytest.mark.asyncio
async def test_status_reports_sdk_not_installed(tmp_path: Path) -> None:
    class MissingRunner:
        available = False
        error = "No module named google.antigravity"

    provider = _provider(tmp_path, runner=MissingRunner())

    status = await provider.status()

    assert status.connected is False
    assert status.status_code == "sdk_not_installed"


@pytest.mark.asyncio
async def test_start_session_uses_sdk_runner(tmp_path: Path) -> None:
    runner = FakeAntigravityRunner()
    provider = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "fix it")

    assert result.provider == "antigravity"
    assert result.provider_engine == "sdk"
    assert runner.call[0] == "fix it"
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_antigravity_provider.py -q
```

Expected: fail because the provider module does not exist.

- [ ] **Step 4: Implement Antigravity provider with injectable runner**

Create `wlcodex/native_agents/antigravity_provider.py`:

```python
from __future__ import annotations

from typing import Any
from uuid import uuid4

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore


class AntigravitySdkRunner:
    def __init__(self) -> None:
        try:
            import google.antigravity  # type: ignore[import-not-found]
        except ImportError as exc:
            self.available = False
            self.error = str(exc)
            self._sdk = None
        else:
            self.available = True
            self.error = ""
            self._sdk = google.antigravity

    async def run(self, *, prompt: str, cwd: str, session_id: str):
        if not self.available:
            raise RuntimeError(self.error)
        raise RuntimeError(
            "Antigravity SDK runner must be wired to the installed SDK session API"
        )


class AntigravitySdkProvider:
    provider = "antigravity"
    provider_engine = "sdk"

    def __init__(
        self,
        *,
        session_store: NativeAgentSessionStore,
        runner: Any | None = None,
    ) -> None:
        self._session_store = session_store
        self._runner = runner or AntigravitySdkRunner()

    async def status(self) -> NativeAgentStatus:
        if not bool(getattr(self._runner, "available", False)):
            return NativeAgentStatus(
                provider=self.provider,
                provider_engine=self.provider_engine,
                enabled=True,
                connected=False,
                status_code="sdk_not_installed",
                message=str(getattr(self._runner, "error", "")),
            )
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": "Antigravity SDK steering is not exposed in this slice.",
                "can_interrupt": "Antigravity SDK cancellation is not exposed in this slice.",
                "can_resolve_approval": "Antigravity SDK approval mapping is not enabled in this slice.",
            },
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        return self._session_store.list_recent(provider=self.provider, limit=limit)

    async def list_models(self) -> list[dict[str, Any]]:
        return []

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any) -> NativeAgentControlResult:
        native_session_id = str(uuid4())
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=prompt.strip()[:80],
            cwd=cwd,
            source_kind="antigravity_sdk",
            status="running",
        )
        async for _event in self._runner.run(
            prompt=prompt,
            cwd=cwd,
            session_id=native_session_id,
        ):
            pass
        session = self._session_store.update_session(session.id, status="done")
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=session.agent_run_id,
            status="started",
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=str(uuid4()),
            title="Antigravity SDK session",
            cwd=cwd,
            source_kind="antigravity_sdk",
            status="created",
        )
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="created",
        )

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        return {"thread": session.to_json_dict(), "turns": []}

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="attached",
        )

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        return await self.attach_session(native_session_id)

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs: Any):
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        async for _event in self._runner.run(
            prompt=prompt,
            cwd=session.cwd,
            session_id=session.native_session_id,
        ):
            pass
        session = self._session_store.update_session(session.id, status="done")
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=session.native_session_id,
            agent_run_id=session.agent_run_id,
            status="continued",
        )
```

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_antigravity_provider.py -q
```

Expected: all tests pass without requiring a real Antigravity SDK installation.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml wlcodex/runtime_events.py wlcodex/native_agents/antigravity_provider.py tests/test_native_agent_antigravity_provider.py
git commit -m "feat: add antigravity sdk native provider"
```

---

### Task 10: Compose Enabled Providers

**Files:**
- Modify: `wlcodex/main.py`
- Test: `tests/test_main_composition.py`

- [ ] **Step 1: Add composition tests**

Add to `tests/test_main_composition.py`:

```python
def test_native_agent_registry_uses_single_claude_engine(tmp_path: Path, monkeypatch) -> None:
    config = _minimal_config(tmp_path)
    type(config).native_agents = SimpleNamespace(
        enabled=True,
        codex=SimpleNamespace(enabled=False),
        claude=SimpleNamespace(
            enabled=True,
            engine="sdk-deepseek",
            cli_local=SimpleNamespace(binary="auto", model="", permission_mode="acceptEdits"),
            sdk_deepseek=SimpleNamespace(
                api_key_env="DEEPSEEK_API_KEY",
                base_url="https://api.deepseek.com/anthropic",
                model="deepseek-v4-pro",
            ),
        ),
        antigravity=SimpleNamespace(enabled=False),
    )

    components = configure_live_stream(config, ledger=Ledger.open(tmp_path / "db.sqlite3"))

    assert components.native_registry.get("claude").provider_engine == "sdk-deepseek"
```

Use existing helpers in the file for `_minimal_config` and `configure_live_stream`. If the names differ, adapt only the test harness names, not the behavior being asserted.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py::test_native_agent_registry_uses_single_claude_engine -q
```

Expected: fail because `configure_live_stream` does not compose Claude providers yet.

- [ ] **Step 3: Compose Codex, Claude, and Antigravity from config**

In `wlcodex/main.py`, after creating `native_session_store = NativeAgentSessionStore(ledger)`, add:

```python
            if config.native_agents.claude.enabled:
                if config.native_agents.claude.engine == "cli-local":
                    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
                    from wlcodex.claude_binary import resolve_claude_binary
                    from wlcodex.native_agents.claude_cli_provider import (
                        ClaudeCliLocalProvider,
                    )

                    binary_resolution = resolve_claude_binary(
                        config.native_agents.claude.cli_local.binary
                    )
                    claude_engine = ClaudeBackend(
                        ClaudeConfig(
                            enabled=bool(binary_resolution.binary),
                            binary=binary_resolution.binary,
                            permission_mode=config.native_agents.claude.cli_local.permission_mode,
                            model=config.native_agents.claude.cli_local.model,
                            binary_resolution_error=binary_resolution.warning,
                        )
                    )
                    native_providers.append(
                        ClaudeCliLocalProvider(
                            engine=claude_engine,
                            session_store=native_agent_session_store,
                            default_cwd=str(config.workspace_by_alias(config.conversation.default_workspace).path),
                        )
                    )
                elif config.native_agents.claude.engine == "sdk-deepseek":
                    from wlcodex.native_agents.claude_sdk_deepseek_provider import (
                        ClaudeSdkDeepSeekConfig,
                        ClaudeSdkDeepSeekProvider,
                    )

                    native_providers.append(
                        ClaudeSdkDeepSeekProvider(
                            config=ClaudeSdkDeepSeekConfig(
                                api_key_env=config.native_agents.claude.sdk_deepseek.api_key_env,
                                base_url=config.native_agents.claude.sdk_deepseek.base_url,
                                model=config.native_agents.claude.sdk_deepseek.model,
                            ),
                            session_store=native_agent_session_store,
                        )
                    )

            if config.native_agents.antigravity.enabled:
                from wlcodex.native_agents.antigravity_provider import AntigravitySdkProvider

                native_providers.append(
                    AntigravitySdkProvider(session_store=native_agent_session_store)
                )
```

Keep Codex composition from Task 4.

- [ ] **Step 4: Return registry from live-stream composition**

In the `SimpleNamespace` returned by `configure_live_stream`, add:

```python
        native_registry=native_registry,
```

- [ ] **Step 5: Run composition tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/main.py tests/test_main_composition.py
git commit -m "feat: compose native agent providers"
```

---

### Task 11: Event Provenance And Provider Sample Normalization

**Files:**
- Modify: `wlcodex/runtime_events.py`
- Modify: `wlcodex/codex_native/projector.py`
- Test: `tests/test_codex_native_projector.py`
- Test: `tests/test_native_agent_claude_cli_provider.py`
- Test: `tests/test_native_agent_antigravity_provider.py`

- [ ] **Step 1: Add provenance tests**

Add to `tests/test_codex_native_projector.py`:

```python
def test_codex_native_projector_marks_provider_provenance(tmp_path: Path) -> None:
    session_store, runtime_store = _store(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)

    events = projector.project_notification(
        "item/agentMessage/delta",
        {"threadId": "thread-1", "turnId": "turn-1", "delta": "hi"},
    )

    assert events[0].source == EventSource.CODEX
    assert events[0].payload["source_kind"] == "codex_native"
    assert events[0].payload["provider"] == "codex"
    assert events[0].payload["provider_engine"] == "app-server"
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_codex_native_projector.py::test_codex_native_projector_marks_provider_provenance -q
```

Expected: fail because provider fields are not in payload yet.

- [ ] **Step 3: Add Codex provenance**

Modify `_append_backend_event` in `wlcodex/codex_native/projector.py`:

```python
            native_payload = {
                **event.payload,
                "native_thread_id": native_thread_id,
                "native_turn_id": native_turn_id,
                "source_kind": _SOURCE_KIND,
                "provider": "codex",
                "provider_engine": "app-server",
            }
```

- [ ] **Step 4: Ensure `EventSource.ANTIGRAVITY` exists**

If Task 9 did not already add it, add this to `EventSource` in `wlcodex/runtime_events.py`:

```python
    ANTIGRAVITY = "antigravity"
```

- [ ] **Step 5: Run provenance tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_codex_native_projector.py tests/test_native_agent_claude_cli_provider.py tests/test_native_agent_antigravity_provider.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/runtime_events.py wlcodex/codex_native/projector.py tests/test_codex_native_projector.py tests/test_native_agent_claude_cli_provider.py tests/test_native_agent_antigravity_provider.py
git commit -m "feat: tag native agent event provenance"
```

---

### Task 12: End-To-End Verification And Operator Notes

**Files:**
- Modify: `config/wlcodex.example.toml`
- Test: no new test file unless a regression appears during verification.

- [ ] **Step 1: Run focused native-agent tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_native_agent_models.py tests/test_native_agent_registry.py tests/test_native_agent_session_store.py tests/test_native_agent_config.py tests/test_native_agent_codex_provider.py tests/test_native_agent_claude_cli_provider.py tests/test_native_agent_claude_sdk_deepseek_provider.py tests/test_native_agent_antigravity_provider.py tests/test_worker_live_stream_native_agent_routes.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run legacy native Codex tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_codex_native_controller.py tests/test_codex_native_projector.py tests/test_codex_native_session_store.py tests/test_worker_live_stream_native_routes.py -q
```

Expected: all tests pass. These tests prove `/native/codex` compatibility.

- [ ] **Step 3: Run config and composition tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_main_composition.py tests/test_claude_binary.py tests/test_claude_backend.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Run default test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all non-slow, non-integration, non-live tests pass.

- [ ] **Step 5: Optional live smoke for Claude SDK DeepSeek**

Only run this when `DEEPSEEK_API_KEY` is available and the user approves network access:

```bash
DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" .venv/bin/python -m pytest tests/test_native_agent_claude_sdk_deepseek_provider.py -m live -q
```

Expected: provider can create a minimal session through `https://api.deepseek.com/anthropic` with `deepseek-v4-pro`.

- [ ] **Step 6: Optional live smoke for Antigravity SDK**

Only run this when the Antigravity SDK package and auth are available:

```bash
.venv/bin/python -m pytest tests/test_native_agent_antigravity_provider.py -m live -q
```

Expected: provider status is `ok` and a minimal SDK session emits at least one assistant event.

- [ ] **Step 7: Commit verification docs or config notes**

If Task 12 changes only `config/wlcodex.example.toml`, commit:

```bash
git add config/wlcodex.example.toml
git commit -m "docs: document native agent provider config"
```

If no files changed in Task 12, do not create an empty commit.

---

## Self-Review Checklist

- Spec coverage: Tasks 1-2 cover provider contract and session storage; Tasks 3 and 10 cover config and Claude mutual exclusion; Tasks 4-6 cover Codex compatibility and generic routes/UI; Tasks 7-8 cover the two Claude engines under one `claude` provider; Task 9 covers Antigravity SDK; Task 11 covers event provenance; Task 12 covers verification.
- Placeholder scan: The plan contains no task with an unspecified implementation step. Optional live smokes are explicitly gated by credentials and network access.
- Type consistency: The same names are used throughout: `NativeAgentProvider`, `NativeAgentRegistry`, `NativeAgentSessionStore`, `provider`, `provider_engine`, `native_session_id`, `cli-local`, `sdk-deepseek`, and `sdk`.
- Business invariant: `claude` is always one provider. `cli-local` and `sdk-deepseek` are provider engines and must not appear as route-level providers.

## References

- Design spec: `docs/superpowers/specs/2026-06-01-wlcodex-native-agent-providers-design.md`
- Claude Code headless CLI: `https://code.claude.com/docs/en/headless`
- Claude Agent SDK Python: `https://code.claude.com/docs/en/agent-sdk/python`
- DeepSeek Anthropic-compatible API: `https://api-docs.deepseek.com/guides/anthropic_api`
- Antigravity SDK overview: `https://antigravity.google/docs/sdk-overview`
