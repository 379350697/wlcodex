# WLCodex Telegram Remote Codex Cockpit Implementation Plan

> Superseded for product implementation: follow the 2026-05-20 Remote
> Workbench repair plans instead. Task-led `/task`, `/continue`, `/steer`,
> queue, blocker, task id, session id, and thread id user flows below are
> legacy diagnostics only.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a lightweight personal Telegram cockpit that controls local Linux Codex CLI tasks while preserving task isolation, low token usage, and explicit history resume.

**Architecture:** Python daemon with Telegram handlers, a deterministic command router, SQLite task ledger, workspace lock manager, status renderer, and a replaceable Codex backend. Codex app-server is the preferred backend, but all protocol details stay behind a narrow interface so the system can survive CLI protocol changes.

**Tech Stack:** Python 3.12+, `python-telegram-bot`, `websockets`, stdlib `sqlite3`, `pytest`, `ruff`, `systemd`.

---

## File Structure

- Create: `pyproject.toml` - package metadata, runtime dependencies, dev tools.
- Create: `README.md` - operator setup and safety notes.
- Create: `config/wlcodex.example.toml` - safe example config.
- Create: `wlcodex/__init__.py` - package marker and version.
- Create: `wlcodex/config.py` - TOML config loading and validation.
- Create: `wlcodex/models.py` - dataclasses and enums shared across components.
- Create: `wlcodex/db.py` - SQLite schema, migrations, task/event repositories.
- Create: `wlcodex/router.py` - Telegram command parsing into typed actions.
- Create: `wlcodex/status.py` - compact status card rendering.
- Create: `wlcodex/locks.py` - workspace write lock policy.
- Create: `wlcodex/codex_backend.py` - backend protocol, fake backend, app-server backend shell.
- Create: `wlcodex/task_service.py` - task lifecycle and invariants.
- Create: `wlcodex/telegram_app.py` - Telegram handlers and callback wiring.
- Create: `wlcodex/main.py` - daemon entrypoint.
- Create: `deploy/systemd/wlcodex.service.example` - service template.
- Create: `tests/test_config.py` - config behavior.
- Create: `tests/test_router.py` - command parsing.
- Create: `tests/test_status.py` - low-noise rendering.
- Create: `tests/test_db.py` - persistence and recovery.
- Create: `tests/test_task_service.py` - task isolation, resume, and locks.

## Task 1: Bootstrap Python Package

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `wlcodex/__init__.py`
- Create: `tests/test_imports.py`

- [ ] **Step 1: Create package metadata**

Write `pyproject.toml`:

```toml
[project]
name = "wlcodex"
version = "0.1.0"
description = "Personal Telegram cockpit for local Codex CLI on Linux"
requires-python = ">=3.12"
dependencies = [
  "python-telegram-bot>=21,<23",
  "websockets>=12,<16",
]

[project.optional-dependencies]
dev = [
  "pytest>=8,<10",
  "ruff>=0.8,<1",
]

[project.scripts]
wlcodex = "wlcodex.main:main"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create minimal README**

Write `README.md`:

```markdown
# WLCodex

WLCodex is a personal Telegram cockpit for controlling local Linux Codex CLI tasks.

V1 safety rules:

- private Telegram chat only
- allowlisted Telegram user IDs only
- new tasks use fresh Codex threads by default
- history resumes only by explicit task selection
- Telegram status cards are never fed back into Codex context
- SQLite is a local ledger, not automatic model memory
- one active write task per workspace
```

- [ ] **Step 3: Create package marker**

Write `wlcodex/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 4: Write import smoke test**

Write `tests/test_imports.py`:

```python
def test_package_imports() -> None:
    import wlcodex

    assert wlcodex.__version__ == "0.1.0"
```

- [ ] **Step 5: Run smoke test**

Run:

```bash
python -m pytest tests/test_imports.py -q
```

Expected:

```text
1 passed
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml README.md wlcodex/__init__.py tests/test_imports.py
git commit -m "chore: bootstrap wlcodex package"
```

## Task 2: Config Loading

**Files:**
- Create: `config/wlcodex.example.toml`
- Create: `wlcodex/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write example config**

Write `config/wlcodex.example.toml`:

```toml
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123456789]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "/media/wl/新加卷/codex/wlcodex"
allow_write = true
```

- [ ] **Step 2: Write failing config tests**

Write `tests/test_config.py`:

```python
from pathlib import Path

import pytest

from wlcodex.config import ConfigError, load_config


def test_load_config_reads_workspace_and_token_env(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.telegram.bot_token_env == "WLCODEX_TELEGRAM_BOT_TOKEN"
    assert config.telegram.allowed_user_ids == frozenset({123})
    assert config.workspace_by_alias("demo").path == Path("/tmp/demo")


def test_load_config_rejects_duplicate_workspace_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo1"
allow_write = true

[[workspaces]]
alias = "demo"
path = "/tmp/demo2"
allow_write = true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate workspace alias"):
        load_config(config_path)
```

- [ ] **Step 3: Implement config loader**

Write `wlcodex/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token_env: str
    allowed_user_ids: frozenset[int]
    private_chat_only: bool


@dataclass(frozen=True)
class CodexConfig:
    binary: str
    app_server_host: str
    app_server_port: int
    approval_policy: str
    sandbox: str


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: Path
    task_log_dir: Path


@dataclass(frozen=True)
class DisplayConfig:
    status_update_min_interval_seconds: int
    tail_lines: int
    diff_max_chars: int


@dataclass(frozen=True)
class WorkspaceConfig:
    alias: str
    path: Path
    allow_write: bool


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    codex: CodexConfig
    storage: StorageConfig
    display: DisplayConfig
    workspaces: tuple[WorkspaceConfig, ...]

    def workspace_by_alias(self, alias: str) -> WorkspaceConfig:
        for workspace in self.workspaces:
            if workspace.alias == alias:
                return workspace
        raise ConfigError(f"unknown workspace alias: {alias}")


def load_config(path: Path) -> AppConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    workspaces = tuple(_workspace(item) for item in data.get("workspaces", []))
    aliases = [workspace.alias for workspace in workspaces]
    if len(aliases) != len(set(aliases)):
        raise ConfigError("duplicate workspace alias")
    if not workspaces:
        raise ConfigError("at least one workspace is required")

    telegram = data["telegram"]
    codex = data["codex"]
    storage = data["storage"]
    display = data["display"]

    return AppConfig(
        telegram=TelegramConfig(
            bot_token_env=str(telegram["bot_token_env"]),
            allowed_user_ids=frozenset(int(value) for value in telegram["allowed_user_ids"]),
            private_chat_only=bool(telegram.get("private_chat_only", True)),
        ),
        codex=CodexConfig(
            binary=str(codex.get("binary", "codex")),
            app_server_host=str(codex.get("app_server_host", "127.0.0.1")),
            app_server_port=int(codex.get("app_server_port", 17431)),
            approval_policy=str(codex.get("approval_policy", "on-request")),
            sandbox=str(codex.get("sandbox", "workspace-write")),
        ),
        storage=StorageConfig(
            sqlite_path=Path(storage["sqlite_path"]),
            task_log_dir=Path(storage["task_log_dir"]),
        ),
        display=DisplayConfig(
            status_update_min_interval_seconds=int(
                display.get("status_update_min_interval_seconds", 2)
            ),
            tail_lines=int(display.get("tail_lines", 40)),
            diff_max_chars=int(display.get("diff_max_chars", 3500)),
        ),
        workspaces=workspaces,
    )


def _workspace(data: dict[str, object]) -> WorkspaceConfig:
    alias = str(data["alias"]).strip()
    if not alias:
        raise ConfigError("workspace alias cannot be empty")
    return WorkspaceConfig(
        alias=alias,
        path=Path(str(data["path"])),
        allow_write=bool(data.get("allow_write", True)),
    )
```

- [ ] **Step 4: Run config tests**

Run:

```bash
python -m pytest tests/test_config.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit**

```bash
git add config/wlcodex.example.toml wlcodex/config.py tests/test_config.py
git commit -m "feat: add typed config loading"
```

## Task 3: Models And SQLite Ledger

**Files:**
- Create: `wlcodex/models.py`
- Create: `wlcodex/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write ledger tests**

Write `tests/test_db.py`:

```python
from pathlib import Path

from wlcodex.db import Ledger
from wlcodex.models import TaskStatus


def test_ledger_creates_and_lists_tasks(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    task = ledger.create_task(
        workspace_alias="demo",
        workspace_path="/tmp/demo",
        title="Fix a bug",
        codex_thread_id="thread-1",
        parent_task_id=None,
    )
    ledger.add_event(task.id, "task_created", {"title": "Fix a bug"})

    tasks = ledger.list_tasks(limit=10)

    assert tasks[0].id == task.id
    assert tasks[0].status == TaskStatus.QUEUED
    assert ledger.list_events(task.id)[0].event_type == "task_created"


def test_ledger_updates_status_and_summary(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Run tests", "thread-2", None)

    ledger.update_task_status(task.id, TaskStatus.RUNNING, phase="running tests", summary="pytest")
    loaded = ledger.get_task(task.id)

    assert loaded.status == TaskStatus.RUNNING
    assert loaded.last_phase == "running tests"
    assert loaded.last_summary == "pytest"
```

- [ ] **Step 2: Implement models**

Write `wlcodex/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Task:
    id: int
    workspace_alias: str
    workspace_path: str
    title: str
    status: TaskStatus
    codex_thread_id: str | None
    parent_task_id: int | None
    created_at: datetime
    updated_at: datetime
    last_summary: str
    last_phase: str
    last_error: str


@dataclass(frozen=True)
class TaskEvent:
    id: int
    task_id: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
```

- [ ] **Step 3: Implement SQLite ledger**

Write `wlcodex/db.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from wlcodex.models import Task, TaskEvent, TaskStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Ledger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path) -> "Ledger":
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path))

    def migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_alias TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                codex_thread_id TEXT,
                parent_task_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_summary TEXT NOT NULL DEFAULT '',
                last_phase TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_workspace_status
                ON tasks(workspace_alias, status);

            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_task_events_task_id_id
                ON task_events(task_id, id);
            """
        )
        self._conn.commit()

    def create_task(
        self,
        workspace_alias: str,
        workspace_path: str,
        title: str,
        codex_thread_id: str | None,
        parent_task_id: int | None,
    ) -> Task:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO tasks (
                workspace_alias, workspace_path, title, status, codex_thread_id,
                parent_task_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_alias,
                workspace_path,
                title,
                TaskStatus.QUEUED.value,
                codex_thread_id,
                parent_task_id,
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_task(int(cur.lastrowid))

    def get_task(self, task_id: int) -> Task:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task id: {task_id}")
        return _task(row)

    def list_tasks(self, limit: int = 20) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_task(row) for row in rows]

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        phase: str = "",
        summary: str = "",
        error: str = "",
    ) -> None:
        self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, last_phase = ?, last_summary = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status.value, phase, summary, error, _now(), task_id),
        )
        self._conn.commit()

    def add_event(self, task_id: int, event_type: str, payload: dict[str, Any]) -> TaskEvent:
        cur = self._conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, event_type, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self._conn.commit()
        return self.list_events(task_id)[-1]

    def list_events(self, task_id: int, limit: int = 50) -> list[TaskEvent]:
        rows = self._conn.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        return [_event(row) for row in rows]


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=int(row["id"]),
        workspace_alias=str(row["workspace_alias"]),
        workspace_path=str(row["workspace_path"]),
        title=str(row["title"]),
        status=TaskStatus(str(row["status"])),
        codex_thread_id=row["codex_thread_id"],
        parent_task_id=row["parent_task_id"],
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
        last_summary=str(row["last_summary"]),
        last_phase=str(row["last_phase"]),
        last_error=str(row["last_error"]),
    )


def _event(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        event_type=str(row["event_type"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=_dt(str(row["created_at"])),
    )
```

- [ ] **Step 4: Run ledger tests**

Run:

```bash
python -m pytest tests/test_db.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/models.py wlcodex/db.py tests/test_db.py
git commit -m "feat: add sqlite task ledger"
```

## Task 4: Command Router

**Files:**
- Create: `wlcodex/router.py`
- Create: `tests/test_router.py`

- [ ] **Step 1: Write router tests**

Write `tests/test_router.py`:

```python
import pytest

from wlcodex.router import (
    ContinueCommand,
    ParseError,
    ShowTaskCommand,
    StartTaskCommand,
    parse_command,
)


def test_parse_new_task_command() -> None:
    command = parse_command("/task lightfee Fix health checks")

    assert command == StartTaskCommand(workspace_alias="lightfee", prompt="Fix health checks")


def test_parse_show_task_command() -> None:
    command = parse_command("/task 42")

    assert command == ShowTaskCommand(task_id=42)


def test_parse_continue_command() -> None:
    command = parse_command("/continue 42 Use conservative fix")

    assert command == ContinueCommand(task_id=42, prompt="Use conservative fix")


def test_parse_rejects_empty_task_prompt() -> None:
    with pytest.raises(ParseError, match="usage"):
        parse_command("/task lightfee")
```

- [ ] **Step 2: Implement router**

Write `wlcodex/router.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class StartTaskCommand:
    workspace_alias: str
    prompt: str


@dataclass(frozen=True)
class ShowTaskCommand:
    task_id: int


@dataclass(frozen=True)
class ContinueCommand:
    task_id: int
    prompt: str


@dataclass(frozen=True)
class SteerCommand:
    task_id: int
    prompt: str


@dataclass(frozen=True)
class ListTasksCommand:
    limit: int = 20


ParsedCommand = (
    StartTaskCommand | ShowTaskCommand | ContinueCommand | SteerCommand | ListTasksCommand
)


def parse_command(text: str) -> ParsedCommand:
    stripped = text.strip()
    if stripped == "/tasks" or stripped == "/status":
        return ListTasksCommand()
    if stripped.startswith("/task "):
        return _parse_task(stripped)
    if stripped.startswith("/continue "):
        return _parse_task_prompt(stripped, "/continue", ContinueCommand)
    if stripped.startswith("/steer "):
        return _parse_task_prompt(stripped, "/steer", SteerCommand)
    raise ParseError("unknown command")


def _parse_task(text: str) -> StartTaskCommand | ShowTaskCommand:
    parts = text.split(maxsplit=2)
    if len(parts) == 2 and parts[1].isdigit():
        return ShowTaskCommand(task_id=int(parts[1]))
    if len(parts) < 3:
        raise ParseError("usage: /task <workspace> <prompt> or /task <task_id>")
    workspace_alias = parts[1].strip()
    prompt = parts[2].strip()
    if not workspace_alias or not prompt:
        raise ParseError("usage: /task <workspace> <prompt>")
    return StartTaskCommand(workspace_alias=workspace_alias, prompt=prompt)


def _parse_task_prompt(text: str, verb: str, cls: type[ContinueCommand] | type[SteerCommand]):
    parts = text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].strip():
        raise ParseError(f"usage: {verb} <task_id> <prompt>")
    return cls(task_id=int(parts[1]), prompt=parts[2].strip())
```

- [ ] **Step 3: Run router tests**

Run:

```bash
python -m pytest tests/test_router.py -q
```

Expected:

```text
4 passed
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/router.py tests/test_router.py
git commit -m "feat: parse telegram commands"
```

## Task 5: Status Renderer

**Files:**
- Create: `wlcodex/status.py`
- Create: `tests/test_status.py`

- [ ] **Step 1: Write status tests**

Write `tests/test_status.py`:

```python
from datetime import datetime, timezone

from wlcodex.models import Task, TaskStatus
from wlcodex.status import render_task_card, render_task_list


def _task(task_id: int, status: TaskStatus, title: str) -> Task:
    now = datetime(2026, 5, 16, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        workspace_alias="demo",
        workspace_path="/tmp/demo",
        title=title,
        status=status,
        codex_thread_id=f"thread-{task_id}",
        parent_task_id=None,
        created_at=now,
        updated_at=now,
        last_summary="short summary",
        last_phase="running tests",
        last_error="",
    )


def test_render_task_card_is_compact() -> None:
    text = render_task_card(_task(42, TaskStatus.RUNNING, "Fix health timeout"))

    assert "Task #42" in text
    assert "running" in text
    assert "running tests" in text
    assert "short summary" in text
    assert len(text) < 600


def test_render_task_list_limits_noise() -> None:
    text = render_task_list([_task(42, TaskStatus.RUNNING, "Fix health timeout")])

    assert "#42" in text
    assert "Fix health timeout" in text
    assert "thread-42" not in text
```

- [ ] **Step 2: Implement renderer**

Write `wlcodex/status.py`:

```python
from __future__ import annotations

from collections.abc import Sequence

from wlcodex.models import Task


def render_task_card(task: Task) -> str:
    lines = [
        f"Task #{task.id} - {task.status.value}",
        f"Workspace: {task.workspace_alias}",
        f"Title: {_trim(task.title, 120)}",
    ]
    if task.last_phase:
        lines.append(f"Phase: {_trim(task.last_phase, 120)}")
    if task.last_summary:
        lines.append(f"Summary: {_trim(task.last_summary, 240)}")
    if task.last_error:
        lines.append(f"Error: {_trim(task.last_error, 240)}")
    return "\n".join(lines)


def render_task_list(tasks: Sequence[Task]) -> str:
    if not tasks:
        return "No tasks yet."
    lines = ["Tasks:"]
    for task in tasks:
        lines.append(f"#{task.id} {task.workspace_alias} {task.status.value}  {_trim(task.title, 80)}")
    return "\n".join(lines)


def _trim(value: str, max_chars: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"
```

- [ ] **Step 3: Run status tests**

Run:

```bash
python -m pytest tests/test_status.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/status.py tests/test_status.py
git commit -m "feat: render compact task status"
```

## Task 6: Workspace Locks And Task Service

**Files:**
- Create: `wlcodex/locks.py`
- Create: `wlcodex/task_service.py`
- Create: `tests/test_task_service.py`

- [ ] **Step 1: Write task service tests**

Write `tests/test_task_service.py`:

```python
from pathlib import Path

import pytest

from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.task_service import TaskService, WorkspaceBusy


def test_start_task_creates_fresh_thread_by_default(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger=ledger,
        workspaces=(WorkspaceConfig("demo", Path("/tmp/demo"), True),),
    )

    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-new")

    assert task.codex_thread_id == "thread-new"
    assert task.status == TaskStatus.QUEUED
    assert ledger.list_events(task.id)[0].event_type == "task_created"


def test_continue_task_requires_existing_task(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    with pytest.raises(KeyError):
        service.record_user_continue(99, "continue")


def test_workspace_write_lock_rejects_second_running_task(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    first = service.start_task("demo", "First", "thread-1")
    ledger.update_task_status(first.id, TaskStatus.RUNNING)

    with pytest.raises(WorkspaceBusy):
        service.ensure_workspace_available("demo")
```

- [ ] **Step 2: Implement lock helpers**

Write `wlcodex/locks.py`:

```python
from __future__ import annotations

from wlcodex.models import Task, TaskStatus


ACTIVE_WRITE_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.PAUSED,
}


def active_write_task(tasks: list[Task], workspace_alias: str) -> Task | None:
    for task in tasks:
        if task.workspace_alias == workspace_alias and task.status in ACTIVE_WRITE_STATUSES:
            return task
    return None
```

- [ ] **Step 3: Implement task service**

Write `wlcodex/task_service.py`:

```python
from __future__ import annotations

from collections.abc import Iterable

from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.locks import active_write_task
from wlcodex.models import Task


class WorkspaceBusy(RuntimeError):
    pass


class TaskService:
    def __init__(self, ledger: Ledger, workspaces: Iterable[WorkspaceConfig]) -> None:
        self._ledger = ledger
        self._workspaces = {workspace.alias: workspace for workspace in workspaces}

    def start_task(self, workspace_alias: str, prompt: str, codex_thread_id: str | None) -> Task:
        workspace = self._workspace(workspace_alias)
        self.ensure_workspace_available(workspace_alias)
        task = self._ledger.create_task(
            workspace_alias=workspace.alias,
            workspace_path=str(workspace.path),
            title=_title(prompt),
            codex_thread_id=codex_thread_id,
            parent_task_id=None,
        )
        self._ledger.add_event(
            task.id,
            "task_created",
            {"prompt": prompt, "context_policy": "fresh_thread_by_default"},
        )
        return task

    def record_user_continue(self, task_id: int, prompt: str) -> Task:
        task = self._ledger.get_task(task_id)
        self._ledger.add_event(
            task.id,
            "user_continue",
            {"prompt": prompt, "context_policy": "explicit_resume_only"},
        )
        return task

    def record_user_steer(self, task_id: int, prompt: str) -> Task:
        task = self._ledger.get_task(task_id)
        self._ledger.add_event(task.id, "user_steer", {"prompt": prompt})
        return task

    def ensure_workspace_available(self, workspace_alias: str) -> None:
        current = active_write_task(self._ledger.list_tasks(limit=100), workspace_alias)
        if current is not None:
            raise WorkspaceBusy(f"workspace {workspace_alias} is busy with task #{current.id}")

    def _workspace(self, workspace_alias: str) -> WorkspaceConfig:
        try:
            return self._workspaces[workspace_alias]
        except KeyError:
            raise KeyError(f"unknown workspace alias: {workspace_alias}") from None


def _title(prompt: str) -> str:
    one_line = " ".join(prompt.split())
    return one_line[:80]
```

- [ ] **Step 4: Run task service tests**

Run:

```bash
python -m pytest tests/test_task_service.py -q
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/locks.py wlcodex/task_service.py tests/test_task_service.py
git commit -m "feat: enforce task isolation and workspace locks"
```

## Task 7: Codex Backend Interface

**Files:**
- Create: `wlcodex/codex_backend.py`
- Create: `tests/test_codex_backend.py`

- [ ] **Step 1: Write fake backend tests**

Write `tests/test_codex_backend.py`:

```python
import pytest

from wlcodex.codex_backend import FakeCodexBackend


@pytest.mark.asyncio
async def test_fake_backend_creates_unique_threads() -> None:
    backend = FakeCodexBackend()

    first = await backend.create_thread("/tmp/demo")
    second = await backend.create_thread("/tmp/demo")

    assert first != second


@pytest.mark.asyncio
async def test_fake_backend_records_turns_without_status_noise() -> None:
    backend = FakeCodexBackend()
    thread_id = await backend.create_thread("/tmp/demo")

    await backend.start_turn(thread_id, "Fix bug")

    assert backend.turns == [(thread_id, "Fix bug")]
```

- [ ] **Step 2: Implement backend protocol and fake backend**

Write `wlcodex/codex_backend.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import uuid


@dataclass(frozen=True)
class BackendEvent:
    event_type: str
    payload: dict[str, object]


class CodexBackend(Protocol):
    async def create_thread(self, workspace_path: str) -> str:
        raise NotImplementedError

    async def start_turn(self, thread_id: str, prompt: str) -> None:
        raise NotImplementedError

    async def continue_turn(self, thread_id: str, prompt: str) -> None:
        raise NotImplementedError


class FakeCodexBackend:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    async def create_thread(self, workspace_path: str) -> str:
        return f"fake-{uuid.uuid4()}"

    async def start_turn(self, thread_id: str, prompt: str) -> None:
        self.turns.append((thread_id, prompt))

    async def continue_turn(self, thread_id: str, prompt: str) -> None:
        self.turns.append((thread_id, prompt))
```

- [ ] **Step 3: Add app-server implementation shell**

Append this class to `wlcodex/codex_backend.py`:

```python
class AppServerCodexBackend:
    """Codex app-server adapter.

    V1 implementation starts with a protocol spike against `codex app-server
    generate-json-schema` and `codex app-server --listen ws://127.0.0.1:<port>`.
    The rest of WLCodex must call only the CodexBackend protocol above.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    async def create_thread(self, workspace_path: str) -> str:
        raise RuntimeError("app-server backend protocol spike is not implemented yet")

    async def start_turn(self, thread_id: str, prompt: str) -> None:
        raise RuntimeError("app-server backend protocol spike is not implemented yet")

    async def continue_turn(self, thread_id: str, prompt: str) -> None:
        raise RuntimeError("app-server backend protocol spike is not implemented yet")
```

- [ ] **Step 4: Run backend tests**

Run:

```bash
python -m pytest tests/test_codex_backend.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/codex_backend.py tests/test_codex_backend.py
git commit -m "feat: define codex backend boundary"
```

## Task 8: Telegram Adapter Skeleton

**Files:**
- Create: `wlcodex/telegram_app.py`
- Create: `wlcodex/main.py`
- Create: `tests/test_telegram_auth.py`

- [ ] **Step 1: Write authorization test**

Write `tests/test_telegram_auth.py`:

```python
from wlcodex.telegram_app import is_authorized


def test_authorized_private_chat_user() -> None:
    assert is_authorized(user_id=123, chat_type="private", allowed_user_ids=frozenset({123}))


def test_rejects_group_chat() -> None:
    assert not is_authorized(user_id=123, chat_type="group", allowed_user_ids=frozenset({123}))


def test_rejects_unknown_user() -> None:
    assert not is_authorized(user_id=999, chat_type="private", allowed_user_ids=frozenset({123}))
```

- [ ] **Step 2: Implement Telegram authorization and app factory**

Write `wlcodex/telegram_app.py`:

```python
from __future__ import annotations

from telegram.ext import Application, CommandHandler

from wlcodex.config import AppConfig


def is_authorized(user_id: int | None, chat_type: str, allowed_user_ids: frozenset[int]) -> bool:
    if user_id is None:
        return False
    if chat_type != "private":
        return False
    return user_id in allowed_user_ids


async def start_command(update, context) -> None:
    await update.effective_message.reply_text(
        "WLCodex is online. Use /task <workspace> <prompt> or /tasks."
    )


def build_application(config: AppConfig, token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start_command))
    return application
```

- [ ] **Step 3: Implement main entrypoint**

Write `wlcodex/main.py`:

```python
from __future__ import annotations

import argparse
import os
from pathlib import Path

from wlcodex.config import load_config
from wlcodex.telegram_app import build_application


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/wlcodex.toml")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    token = os.environ.get(config.telegram.bot_token_env)
    if not token:
        raise SystemExit(f"missing Telegram token env: {config.telegram.bot_token_env}")

    app = build_application(config, token)
    app.run_polling()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run authorization tests**

Run:

```bash
python -m pytest tests/test_telegram_auth.py -q
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/telegram_app.py wlcodex/main.py tests/test_telegram_auth.py
git commit -m "feat: add telegram daemon skeleton"
```

## Task 9: Wire Commands To Task Service With Fake Backend

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Modify: `wlcodex/main.py`
- Create: `tests/test_command_flow.py`

- [ ] **Step 1: Write command flow test**

Write `tests/test_command_flow.py`:

```python
from pathlib import Path

import pytest

from wlcodex.config import WorkspaceConfig
from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.db import Ledger
from wlcodex.task_service import TaskService
from wlcodex.telegram_app import handle_text_command


@pytest.mark.asyncio
async def test_task_command_creates_task_and_starts_backend(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    response = await handle_text_command("/task demo Fix bug", service, backend)

    tasks = ledger.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Fix bug"
    assert backend.turns == [(tasks[0].codex_thread_id, "Fix bug")]
    assert "Task #1" in response
```

- [ ] **Step 2: Implement command flow helper**

Append to `wlcodex/telegram_app.py`:

```python
from wlcodex.codex_backend import CodexBackend
from wlcodex.router import (
    ContinueCommand,
    ListTasksCommand,
    ShowTaskCommand,
    StartTaskCommand,
    SteerCommand,
    parse_command,
)
from wlcodex.status import render_task_card, render_task_list
from wlcodex.task_service import TaskService


async def handle_text_command(text: str, service: TaskService, backend: CodexBackend) -> str:
    command = parse_command(text)
    if isinstance(command, ListTasksCommand):
        return render_task_list(service._ledger.list_tasks())
    if isinstance(command, ShowTaskCommand):
        return render_task_card(service._ledger.get_task(command.task_id))
    if isinstance(command, StartTaskCommand):
        workspace = service._workspace(command.workspace_alias)
        thread_id = await backend.create_thread(str(workspace.path))
        task = service.start_task(command.workspace_alias, command.prompt, thread_id)
        await backend.start_turn(thread_id, command.prompt)
        return render_task_card(task)
    if isinstance(command, ContinueCommand):
        task = service.record_user_continue(command.task_id, command.prompt)
        if task.codex_thread_id is None:
            raise RuntimeError(f"task #{task.id} has no codex thread")
        await backend.continue_turn(task.codex_thread_id, command.prompt)
        return render_task_card(task)
    if isinstance(command, SteerCommand):
        task = service.record_user_steer(command.task_id, command.prompt)
        if task.codex_thread_id is None:
            raise RuntimeError(f"task #{task.id} has no codex thread")
        await backend.continue_turn(task.codex_thread_id, command.prompt)
        return render_task_card(task)
    raise RuntimeError("unhandled command")
```

- [ ] **Step 3: Run command flow test**

Run:

```bash
python -m pytest tests/test_command_flow.py -q
```

Expected:

```text
1 passed
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/telegram_app.py wlcodex/main.py tests/test_command_flow.py
git commit -m "feat: wire telegram commands to task service"
```

## Task 10: Deployment Skeleton

**Files:**
- Create: `deploy/systemd/wlcodex.service.example`
- Modify: `README.md`

- [ ] **Step 1: Write systemd service template**

Write `deploy/systemd/wlcodex.service.example`:

```ini
[Unit]
Description=WLCodex Telegram remote Codex cockpit
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/media/wl/新加卷/codex/wlcodex
Environment=WLCODEX_TELEGRAM_BOT_TOKEN=replace-with-systemd-drop-in
ExecStart=/media/wl/新加卷/codex/wlcodex/.venv/bin/wlcodex --config config/wlcodex.toml
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Add README setup section**

Append to `README.md`:

```markdown
## Local setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
cp config/wlcodex.example.toml config/wlcodex.toml
export WLCODEX_TELEGRAM_BOT_TOKEN="your-token"
wlcodex --config config/wlcodex.toml
```

## systemd

Copy `deploy/systemd/wlcodex.service.example` to a user service, then set the real bot token through a private systemd drop-in instead of committing it to disk.

The app should run with Codex app-server bound to loopback only.
```

- [ ] **Step 3: Run all tests**

Run:

```bash
python -m pytest -q
```

Expected:

```text
all tests pass
```

- [ ] **Step 4: Commit**

```bash
git add deploy/systemd/wlcodex.service.example README.md
git commit -m "docs: add local deployment notes"
```

## Task 11: Codex App-Server Protocol Spike

**Files:**
- Create: `docs/protocol/codex-app-server-spike.md`

- [ ] **Step 1: Generate protocol schema locally**

Run:

```bash
mkdir -p docs/protocol runtime/protocol
codex app-server generate-json-schema --out runtime/protocol
```

Expected:

```text
runtime/protocol contains generated JSON Schema files from Codex CLI
```

- [ ] **Step 2: Record the local command result**

Write `docs/protocol/codex-app-server-spike.md`:

```markdown
# Codex App-Server Protocol Spike

## Purpose

This document records the exact app-server messages WLCodex uses so the rest of the app remains insulated from protocol changes.

## Local Codex CLI

- Command: `codex --version`
- App-server help command: `codex app-server --help`
- Schema command: `codex app-server generate-json-schema --out runtime/protocol`

## Required V1 Operations

- create or start a thread for a workspace
- start a turn with a user prompt
- continue an existing thread with a user prompt
- receive streamed events for status display
- receive approval requests
- resolve approval requests

## Validation Command

```bash
codex app-server generate-json-schema --out runtime/protocol
```

## Schema Files

- Record each generated file path here after running the command.

## V1 Message Mapping

Create a table with these columns after reading the generated schema:

| WLCodex operation | Codex app-server method/event | Request fields | Response fields | Notes |
| --- | --- | --- | --- | --- |
| create thread | discovered from schema | discovered from schema | discovered from schema | required before backend implementation |
| start turn | discovered from schema | discovered from schema | discovered from schema | required before backend implementation |
| continue turn | discovered from schema | discovered from schema | discovered from schema | required before backend implementation |
| stream status event | discovered from schema | discovered from schema | discovered from schema | required before Telegram live updates |
| receive approval request | discovered from schema | discovered from schema | discovered from schema | required before remote approvals |
| resolve approval | discovered from schema | discovered from schema | discovered from schema | required before remote approvals |
```

- [ ] **Step 3: Decide backend readiness**

Read the generated schema and update the `## V1 Message Mapping` table with exact method and event names. If all required operations are present, mark the app-server backend as ready for implementation in the spike document:

```markdown
## Backend Decision

Decision: app-server backend is ready for V1 implementation.
Reason: the generated schema exposes thread creation, turn start, streamed events, approval request events, and approval resolution.
```

If any required operation is missing, mark the backend as blocked and keep `FakeCodexBackend` as the only runnable backend:

```markdown
## Backend Decision

Decision: app-server backend is blocked for V1 implementation.
Reason: the generated schema does not expose <specific missing operation>.
Fallback: build a separate PTY backend spike before enabling live Codex execution.
```

- [ ] **Step 4: Write backend implementation addendum**

Create a follow-up plan only after Step 3 records exact app-server method names:

`docs/superpowers/plans/2026-05-16-wlcodex-codex-app-server-backend-implementation-plan.md`

The addendum must include:

- exact WebSocket connection command
- exact request/response message names
- exact event names for approval requests
- exact approval resolution message
- an integration test gated by `WLCODEX_RUN_CODEX_INTEGRATION=1`
- a harmless prompt for the integration test:

```text
Reply with exactly: wlcodex integration ok
```

- [ ] **Step 5: Run unit tests after the spike document is written**

Run:

```bash
python -m pytest -q
```

Expected:

```text
all currently implemented unit tests pass
```

- [ ] **Step 6: Commit**

```bash
git add docs/protocol/codex-app-server-spike.md runtime/protocol
git commit -m "docs: record codex app-server protocol spike"
```

## Self-Review

- Spec coverage: V1 covers Telegram private control, task isolation, explicit resume, low-token status display, local ledger, workspace lock, and replaceable Codex backend.
- Placeholder scan: no implementation step asks for a vague future fill-in. The app-server spike is an explicit decision gate because the protocol is experimental and must be generated locally before implementation details are trustworthy.
- Type consistency: task ids are integers, Codex thread ids are strings, workspace aliases are strings, task status values come from `TaskStatus`.
- Risk note: the app-server backend is the main technical uncertainty. The plan isolates that risk in Task 11 after local business rules and tests are already stable.
