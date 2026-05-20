# WLCodex Task Liveness And Safe Queue Implementation Plan

> Superseded for product implementation: follow the 2026-05-20 Remote
> Workbench repair plans instead. Queue/lock behavior below is internal
> execution infrastructure; task ids, blockers, and queue positions must not
> appear in normal Workbench user paths.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix paused-task self-locking, add task liveness cleanup, and notify Telegram about recovery-paused tasks while preserving the one-active-write-task-per-workspace invariant.

**Architecture:** Keep workspace write locking as the safety boundary, add self-exclusion only for operations on the current task, move task liveness checks into a focused watchdog module, and wire the watchdog into the event bridge after independent modules land. The plan is split into a parallel first wave with disjoint write sets, followed by a short sequential integration wave.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `pytest`, `pytest-asyncio`, `python-telegram-bot`, Codex app-server health surface, local TOML config.

---

## Parallelization Contract

Workers are not alone in the codebase. Each worker must edit only the files assigned to that task, must not revert unrelated changes, and must adapt to changes made by other workers. The first wave is safe for parallel subagents because each task owns a disjoint write set.

### Wave 1 - Parallel

- Task 1 owns lock semantics: `wlcodex/locks.py`, `wlcodex/task_service.py`, `tests/test_task_service.py`.
- Task 2 owns task config: `wlcodex/config.py`, `config/wlcodex.example.toml`, `tests/test_config.py`, `tests/fixtures/full_cockpit.toml`.
- Task 3 owns ledger liveness helpers: `wlcodex/db.py`, `tests/test_db.py`.
- Task 4 owns watchdog module: create `wlcodex/watchdog.py`, create `tests/test_watchdog.py`.
- Task 5 owns recovery notification module: create `wlcodex/recovery_notifications.py`, create `tests/test_recovery_notifications.py`.
- Task 6 owns health snapshot module: create `wlcodex/health_snapshot.py`, create `tests/test_health_snapshot.py`.

### Wave 2 - Sequential Integration

- Task 7 wires watchdog into `EventBridge`: `wlcodex/event_bridge.py`, `tests/test_event_bridge.py`.
- Task 8 wires recovery notifications into `main`: `wlcodex/main.py`, `tests/test_main_composition.py`.
- Task 9 performs final regression and docs check: `tests/test_drift_repairs.py`, `README.md`.

Do not run Tasks 7-9 until Wave 1 is merged. Tasks 7-9 intentionally touch integration files that would create conflicts if edited in parallel.

## File Structure

- Modify: `wlcodex/locks.py` - active write lock helper with current-task exclusion.
- Modify: `wlcodex/task_service.py` - paused continue/abort semantics.
- Modify: `wlcodex/config.py` - `[task]` config dataclass and defaults.
- Modify: `wlcodex/db.py` - active task and task-liveness ledger helpers.
- Create: `wlcodex/watchdog.py` - liveness watchdog independent of backend events.
- Create: `wlcodex/recovery_notifications.py` - startup recovery Telegram notice helper.
- Create: `wlcodex/health_snapshot.py` - small read-only task/backend health snapshot.
- Modify: `wlcodex/event_bridge.py` - periodic watchdog loop.
- Modify: `wlcodex/main.py` - compose config, watchdog, and recovery notifications.
- Modify tests in the paths listed per task.

## Task 1: Lock Self-Exclusion And Paused Abort

**Files:**
- Modify: `wlcodex/locks.py`
- Modify: `wlcodex/task_service.py`
- Modify: `tests/test_task_service.py`

- [ ] **Step 1: Add failing task-service tests**

Add these tests to `tests/test_task_service.py`:

```python
def test_paused_task_can_continue_without_self_lock(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    continued = service.continue_task(task.id, "resume safely")

    assert continued.status == TaskStatus.QUEUED
    events = service._ledger.list_events(task.id)
    assert events[-1].event_type == "user_continue"


def test_paused_task_can_abort(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    aborted = service.abort_task(task.id)

    assert aborted.status == TaskStatus.ABORTED
    events = service._ledger.list_events(task.id)
    assert events[-1].event_type == "user_aborted"


def test_different_paused_task_still_blocks_new_write(service: TaskService) -> None:
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    service._ledger.set_task_status(task.id, TaskStatus.PAUSED)

    with pytest.raises(WorkspaceBusy, match="task #1"):
        service.start_task("demo", "Second", codex_thread_id="thread-2")
```

- [ ] **Step 2: Run the task-service tests and verify the first two fail**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_task_service.py -q
```

Expected before implementation: at least `test_paused_task_can_continue_without_self_lock` and `test_paused_task_can_abort` fail.

- [ ] **Step 3: Implement lock self-exclusion**

Change `wlcodex/locks.py` so the helper accepts an optional excluded task id:

```python
def active_write_task(
    tasks: list[Task],
    workspace_alias: str,
    exclude_task_id: int | None = None,
) -> Task | None:
    for task in tasks:
        if exclude_task_id is not None and task.id == exclude_task_id:
            continue
        if task.workspace_alias == workspace_alias and task.status in ACTIVE_WRITE_STATUSES:
            return task
    return None
```

- [ ] **Step 4: Thread the exclusion through `TaskService`**

Update `ensure_workspace_available()`:

```python
def ensure_workspace_available(
    self, workspace_alias: str, exclude_task_id: int | None = None
) -> None:
    current = active_write_task(
        self._ledger.list_tasks(limit=100),
        workspace_alias,
        exclude_task_id=exclude_task_id,
    )
    if current is not None:
        raise WorkspaceBusy(
            f"workspace {workspace_alias} is busy with task #{current.id}"
        )
```

Then update `continue_task()` to call:

```python
self.ensure_workspace_available(
    task.workspace_alias,
    exclude_task_id=task_id,
)
```

Do not add this exclusion to new task creation.

- [ ] **Step 5: Allow paused abort**

Change the abort guard in `abort_task()`:

```python
if task.status not in (
    TaskStatus.RUNNING,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.QUEUED,
    TaskStatus.PAUSED,
):
    raise RuntimeError(f"Cannot abort task #{task_id} in status {task.status.value}")
```

Also clear the active turn before transitioning:

```python
self._ledger.clear_active_turn(task_id)
self._transition(task_id, TaskStatus.ABORTED)
```

- [ ] **Step 6: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_task_service.py -q
```

Expected: all tests in `tests/test_task_service.py` pass.

## Task 2: Task Liveness Configuration

**Files:**
- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.example.toml`
- Modify: `tests/test_config.py`
- Modify: `tests/fixtures/full_cockpit.toml`

- [ ] **Step 1: Add config tests**

Add to `tests/test_config.py`:

```python
def test_task_config_defaults_when_section_missing(tmp_path: Path) -> None:
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
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.task.max_running_seconds == 7200
    assert config.task.max_queued_seconds == 1800
    assert config.task.max_waiting_approval_seconds == 3600
    assert config.task.watchdog_interval_seconds == 60
    assert config.task.backend_dead_grace_seconds == 120


def test_task_config_reads_overrides(tmp_path: Path) -> None:
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

[task]
max_running_seconds = 30
max_queued_seconds = 20
max_waiting_approval_seconds = 10
watchdog_interval_seconds = 5
backend_dead_grace_seconds = 7

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.task.max_running_seconds == 30
    assert config.task.max_queued_seconds == 20
    assert config.task.max_waiting_approval_seconds == 10
    assert config.task.watchdog_interval_seconds == 5
    assert config.task.backend_dead_grace_seconds == 7
```

- [ ] **Step 2: Run config tests and verify they fail**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_config.py -q
```

Expected before implementation: failures mention missing `config.task`.

- [ ] **Step 3: Add `TaskConfig`**

In `wlcodex/config.py`, add:

```python
@dataclass(frozen=True)
class TaskConfig:
    max_running_seconds: int
    max_queued_seconds: int
    max_waiting_approval_seconds: int
    watchdog_interval_seconds: int
    backend_dead_grace_seconds: int
```

Add `task: TaskConfig` to `AppConfig`.

Inside `load_config()`, add:

```python
task_raw = data.get("task", {})
```

and populate:

```python
task=TaskConfig(
    max_running_seconds=int(task_raw.get("max_running_seconds", 7200)),
    max_queued_seconds=int(task_raw.get("max_queued_seconds", 1800)),
    max_waiting_approval_seconds=int(
        task_raw.get("max_waiting_approval_seconds", 3600)
    ),
    watchdog_interval_seconds=int(task_raw.get("watchdog_interval_seconds", 60)),
    backend_dead_grace_seconds=int(task_raw.get("backend_dead_grace_seconds", 120)),
),
```

- [ ] **Step 4: Update example config and fixture**

Add this section to `config/wlcodex.example.toml` and `tests/fixtures/full_cockpit.toml`:

```toml
[task]
max_running_seconds = 7200
max_queued_seconds = 1800
max_waiting_approval_seconds = 3600
watchdog_interval_seconds = 60
backend_dead_grace_seconds = 120
```

- [ ] **Step 5: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_config.py tests/test_main_composition.py -q
```

Expected: config and composition tests pass.

## Task 3: Ledger Liveness Helpers

**Files:**
- Modify: `wlcodex/db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Add ledger tests**

Add to `tests/test_db.py`:

```python
def test_list_active_tasks_returns_write_lock_statuses(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    queued = ledger.create_task("demo", "/tmp/demo", "Queued", "q", None)
    running = ledger.create_task("demo", "/tmp/demo", "Running", "r", None)
    ledger.set_task_status(running.id, TaskStatus.RUNNING)
    waiting = ledger.create_task("demo", "/tmp/demo", "Waiting", "w", None)
    ledger.set_task_status(waiting.id, TaskStatus.WAITING_APPROVAL)
    paused = ledger.create_task("demo", "/tmp/demo", "Paused", "p", None)
    ledger.set_task_status(paused.id, TaskStatus.PAUSED)
    done = ledger.create_task("demo", "/tmp/demo", "Done", "d", None)
    ledger.set_task_status(done.id, TaskStatus.DONE)

    ids = {task.id for task in ledger.list_active_tasks()}

    assert ids == {queued.id, running.id, waiting.id, paused.id}


def test_mark_task_timeout_records_failure_event(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Running", "thread-1", None)
    ledger.set_task_status(task.id, TaskStatus.RUNNING)

    updated = ledger.mark_task_timeout(
        task.id,
        status=TaskStatus.RUNNING,
        age_seconds=8000,
        threshold_seconds=7200,
    )

    assert updated.status == TaskStatus.FAILED
    assert "timeout" in updated.last_error
    events = ledger.list_events(task.id)
    assert events[-1].event_type == "task_timeout"
    assert events[-1].payload["age_seconds"] == 8000


def test_mark_backend_dead_records_failure_event(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Running", "thread-1", None)
    ledger.set_task_status(task.id, TaskStatus.RUNNING)

    updated = ledger.mark_backend_dead(task.id, "Backend unhealthy: process dead")

    assert updated.status == TaskStatus.FAILED
    assert "Backend unhealthy" in updated.last_error
    events = ledger.list_events(task.id)
    assert events[-1].event_type == "backend_dead"
```

- [ ] **Step 2: Run DB tests and verify failures**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_db.py -q
```

Expected before implementation: failures mention missing helper methods.

- [ ] **Step 3: Implement `list_active_tasks()`**

In `wlcodex/db.py`, add:

```python
def list_active_tasks(self, limit: int = 100) -> list[Task]:
    rows = self._conn.execute(
        """
        SELECT * FROM tasks
        WHERE status IN (?, ?, ?, ?)
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
            TaskStatus.WAITING_APPROVAL.value,
            TaskStatus.PAUSED.value,
            limit,
        ),
    ).fetchall()
    return [_task(row) for row in rows]
```

- [ ] **Step 4: Implement timeout and backend-dead helpers**

Add:

```python
def mark_task_timeout(
    self,
    task_id: int,
    *,
    status: TaskStatus,
    age_seconds: int,
    threshold_seconds: int,
) -> Task:
    error = (
        f"task timed out in {status.value} after "
        f"{age_seconds}s (limit {threshold_seconds}s)"
    )
    self.set_task_status(
        task_id,
        TaskStatus.FAILED,
        phase="timeout",
        error=error[:240],
    )
    self.add_event(
        task_id,
        "task_timeout",
        {
            "status": status.value,
            "age_seconds": age_seconds,
            "threshold_seconds": threshold_seconds,
        },
    )
    self.clear_active_turn(task_id)
    return self.get_task(task_id)


def mark_backend_dead(self, task_id: int, summary: str) -> Task:
    error = f"backend dead: {summary}"
    self.set_task_status(
        task_id,
        TaskStatus.FAILED,
        phase="backend_dead",
        error=error[:240],
    )
    self.add_event(task_id, "backend_dead", {"summary": summary[:500]})
    self.clear_active_turn(task_id)
    return self.get_task(task_id)
```

- [ ] **Step 5: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_db.py tests/test_recovery.py -q
```

Expected: DB and recovery tests pass.

## Task 4: Watchdog Module

**Files:**
- Create: `wlcodex/watchdog.py`
- Create: `tests/test_watchdog.py`

- [ ] **Step 1: Create watchdog tests**

Create `tests/test_watchdog.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from wlcodex.models import Task, TaskStatus
from wlcodex.watchdog import TaskLivenessConfig, TaskWatchdog


def _task(task_id: int, status: TaskStatus, updated_age_seconds: int) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        workspace_alias="demo",
        workspace_path="/tmp/demo",
        title="Task",
        status=status,
        codex_thread_id="thread-1",
        active_turn_id="turn-1",
        parent_task_id=None,
        telegram_chat_id=123,
        telegram_status_message_id=None,
        created_at=now - timedelta(seconds=updated_age_seconds),
        updated_at=now - timedelta(seconds=updated_age_seconds),
        last_summary="",
        last_phase="",
        last_error="",
    )


class LedgerSpy:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks
        self.timeouts: list[tuple[int, TaskStatus, int, int]] = []
        self.backend_dead: list[tuple[int, str]] = []

    def list_active_tasks(self, limit: int = 100) -> list[Task]:
        return self.tasks

    def mark_task_timeout(
        self,
        task_id: int,
        *,
        status: TaskStatus,
        age_seconds: int,
        threshold_seconds: int,
    ) -> Task:
        self.timeouts.append((task_id, status, age_seconds, threshold_seconds))
        return self.tasks[0]

    def mark_backend_dead(self, task_id: int, summary: str) -> Task:
        self.backend_dead.append((task_id, summary))
        return self.tasks[0]


@dataclass
class Health:
    is_healthy: bool
    text: str = "health"

    def summary(self) -> str:
        return self.text


class Backend:
    def __init__(self, health: Health) -> None:
        self._health = health

    def health(self) -> Health:
        return self._health


def test_watchdog_marks_stale_running_task_timeout() -> None:
    ledger = LedgerSpy([_task(1, TaskStatus.RUNNING, 8000)])
    watchdog = TaskWatchdog(
        ledger=ledger,
        backend=Backend(Health(True)),
        config=TaskLivenessConfig(
            max_running_seconds=7200,
            max_queued_seconds=1800,
            max_waiting_approval_seconds=3600,
            backend_dead_grace_seconds=120,
        ),
    )

    watchdog.scan_once()

    assert ledger.timeouts[0][0] == 1
    assert ledger.timeouts[0][1] == TaskStatus.RUNNING
    assert ledger.timeouts[0][3] == 7200


def test_watchdog_waits_for_backend_dead_grace() -> None:
    ledger = LedgerSpy([_task(1, TaskStatus.RUNNING, 30)])
    watchdog = TaskWatchdog(
        ledger=ledger,
        backend=Backend(Health(False, "process dead")),
        config=TaskLivenessConfig(
            max_running_seconds=7200,
            max_queued_seconds=1800,
            max_waiting_approval_seconds=3600,
            backend_dead_grace_seconds=120,
        ),
    )

    watchdog.scan_once(now=datetime.now(timezone.utc))

    assert ledger.backend_dead == []


def test_watchdog_marks_backend_dead_after_grace() -> None:
    ledger = LedgerSpy([_task(1, TaskStatus.RUNNING, 30)])
    start = datetime.now(timezone.utc)
    watchdog = TaskWatchdog(
        ledger=ledger,
        backend=Backend(Health(False, "process dead")),
        config=TaskLivenessConfig(
            max_running_seconds=7200,
            max_queued_seconds=1800,
            max_waiting_approval_seconds=3600,
            backend_dead_grace_seconds=120,
        ),
    )

    watchdog.scan_once(now=start)
    watchdog.scan_once(now=start + timedelta(seconds=121))

    assert ledger.backend_dead == [(1, "process dead")]
```

- [ ] **Step 2: Implement the module**

Create `wlcodex/watchdog.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from wlcodex.models import Task, TaskStatus


@dataclass(frozen=True)
class TaskLivenessConfig:
    max_running_seconds: int
    max_queued_seconds: int
    max_waiting_approval_seconds: int
    backend_dead_grace_seconds: int


class TaskWatchdog:
    def __init__(self, ledger: Any, backend: Any, config: TaskLivenessConfig) -> None:
        self._ledger = ledger
        self._backend = backend
        self._config = config
        self._backend_unhealthy_since: datetime | None = None

    def scan_once(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        changed = 0
        changed += self._mark_stale_tasks(current)
        changed += self._mark_backend_dead_if_sustained(current)
        return changed

    def _mark_stale_tasks(self, now: datetime) -> int:
        changed = 0
        for task in self._ledger.list_active_tasks(limit=100):
            threshold = self._threshold_for(task.status)
            if threshold is None:
                continue
            age = int((now - task.updated_at).total_seconds())
            if age > threshold:
                self._ledger.mark_task_timeout(
                    task.id,
                    status=task.status,
                    age_seconds=age,
                    threshold_seconds=threshold,
                )
                changed += 1
        return changed

    def _mark_backend_dead_if_sustained(self, now: datetime) -> int:
        healthy, summary = self._backend_health()
        if healthy:
            self._backend_unhealthy_since = None
            return 0

        if self._backend_unhealthy_since is None:
            self._backend_unhealthy_since = now
            return 0

        age = int((now - self._backend_unhealthy_since).total_seconds())
        if age <= self._config.backend_dead_grace_seconds:
            return 0

        changed = 0
        for task in self._ledger.list_active_tasks(limit=100):
            if task.codex_thread_id:
                self._ledger.mark_backend_dead(task.id, summary)
                changed += 1
        return changed

    def _threshold_for(self, status: TaskStatus) -> int | None:
        if status == TaskStatus.RUNNING:
            return self._config.max_running_seconds
        if status == TaskStatus.QUEUED:
            return self._config.max_queued_seconds
        if status == TaskStatus.WAITING_APPROVAL:
            return self._config.max_waiting_approval_seconds
        return None

    def _backend_health(self) -> tuple[bool, str]:
        if not hasattr(self._backend, "health"):
            return True, "no health method"
        health = self._backend.health()
        summary = str(health)
        if hasattr(health, "summary"):
            summary = str(health.summary())
        is_healthy = getattr(health, "is_healthy", True)
        if callable(is_healthy):
            is_healthy = is_healthy()
        return bool(is_healthy), summary
```

- [ ] **Step 3: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_watchdog.py -q
```

Expected: all watchdog tests pass.

## Task 5: Recovery Notification Module

**Files:**
- Create: `wlcodex/recovery_notifications.py`
- Create: `tests/test_recovery_notifications.py`

- [ ] **Step 1: Add tests**

Create `tests/test_recovery_notifications.py`:

```python
from __future__ import annotations

import pytest

from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.recovery_notifications import notify_recovery_paused_tasks


@pytest.mark.asyncio
async def test_notify_recovery_paused_tasks_sends_only_tasks_with_chat(tmp_path):
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    with_chat = ledger.create_task("demo", "/tmp/demo", "Paused", "thread-1", None, 123)
    ledger.set_task_status(with_chat.id, TaskStatus.PAUSED)
    no_chat = ledger.create_task("demo", "/tmp/demo", "No chat", "thread-2", None, None)
    ledger.set_task_status(no_chat.id, TaskStatus.PAUSED)
    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text))
        return 99

    count = await notify_recovery_paused_tasks(
        ledger=ledger,
        paused_ids=[with_chat.id, no_chat.id],
        send_telegram=send,
        edit_telegram=None,
    )

    assert count == 1
    assert sent[0][0] == 123
    assert f"任务 #{with_chat.id}" in sent[0][1]
    assert "/continue" in sent[0][1]
    assert "/abort" in sent[0][1]


@pytest.mark.asyncio
async def test_notify_recovery_paused_tasks_edits_status_card(tmp_path):
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Paused", "thread-1", None, 123)
    ledger.set_task_status(task.id, TaskStatus.PAUSED)
    ledger.set_status_message(task.id, 123, 456)
    edits: list[tuple[int, int, str]] = []

    async def send(chat_id: int, text: str, buttons=None) -> int:
        return 99

    async def edit(chat_id: int, message_id: int, text: str) -> None:
        edits.append((chat_id, message_id, text))

    await notify_recovery_paused_tasks(
        ledger=ledger,
        paused_ids=[task.id],
        send_telegram=send,
        edit_telegram=edit,
    )

    assert edits[0][0] == 123
    assert edits[0][1] == 456
    assert "已暂停" in edits[0][2]
```

- [ ] **Step 2: Implement module**

Create `wlcodex/recovery_notifications.py`:

```python
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from wlcodex.db import Ledger
from wlcodex.status import render_task_card

logger = logging.getLogger(__name__)

SendTelegram = Callable[[int, str, list[list[dict[str, str]]] | None], Awaitable[int]]
EditTelegram = Callable[[int, int, str], Awaitable[None]]


async def notify_recovery_paused_tasks(
    *,
    ledger: Ledger,
    paused_ids: list[int],
    send_telegram: SendTelegram,
    edit_telegram: EditTelegram | None,
) -> int:
    sent = 0
    for task_id in paused_ids:
        try:
            task = ledger.get_task(task_id)
        except KeyError:
            continue
        if task.telegram_chat_id is None:
            continue

        text = (
            f"任务 #{task.id} 已因 WLCodex 重启暂停。\n"
            f"可用 /continue {task.id} <prompt> 继续，"
            f"或 /abort {task.id} 释放工作区。"
        )
        try:
            await send_telegram(task.telegram_chat_id, text, None)
            sent += 1
        except Exception:
            logger.exception("failed to send recovery notification for task #%d", task.id)

        if edit_telegram is not None and task.telegram_status_message_id is not None:
            try:
                await edit_telegram(
                    task.telegram_chat_id,
                    task.telegram_status_message_id,
                    render_task_card(task),
                )
            except Exception:
                logger.exception("failed to edit recovery status card for task #%d", task.id)
    return sent
```

- [ ] **Step 3: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_recovery_notifications.py -q
```

Expected: all recovery notification tests pass.

## Task 6: Health Snapshot Module

**Files:**
- Create: `wlcodex/health_snapshot.py`
- Create: `tests/test_health_snapshot.py`

- [ ] **Step 1: Add tests**

Create `tests/test_health_snapshot.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from wlcodex.health_snapshot import build_health_snapshot
from wlcodex.models import Task, TaskStatus


def _task(task_id: int, status: TaskStatus) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id,
        workspace_alias="demo",
        workspace_path="/tmp/demo",
        title="Task",
        status=status,
        codex_thread_id="thread-1",
        active_turn_id=None,
        parent_task_id=None,
        telegram_chat_id=None,
        telegram_status_message_id=None,
        created_at=now,
        updated_at=now,
        last_summary="",
        last_phase="",
        last_error="",
    )


@dataclass
class Health:
    is_healthy: bool

    def summary(self) -> str:
        return "Backend healthy" if self.is_healthy else "Backend unhealthy"


class Backend:
    def __init__(self, healthy: bool) -> None:
        self._healthy = healthy

    def health(self) -> Health:
        return Health(self._healthy)


class Ledger:
    def list_active_tasks(self, limit: int = 100):
        return [
            _task(1, TaskStatus.RUNNING),
            _task(2, TaskStatus.WAITING_APPROVAL),
        ]


def test_build_health_snapshot_counts_active_tasks() -> None:
    snapshot = build_health_snapshot(Ledger(), Backend(True))

    assert snapshot.backend_healthy is True
    assert snapshot.backend_summary == "Backend healthy"
    assert snapshot.active_task_count == 2
    assert snapshot.running_count == 1
    assert snapshot.waiting_approval_count == 1
```

- [ ] **Step 2: Implement module**

Create `wlcodex/health_snapshot.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wlcodex.models import TaskStatus


@dataclass(frozen=True)
class HealthSnapshot:
    backend_healthy: bool
    backend_summary: str
    active_task_count: int
    queued_count: int
    running_count: int
    waiting_approval_count: int
    paused_count: int


def build_health_snapshot(ledger: Any, backend: Any) -> HealthSnapshot:
    backend_healthy, backend_summary = _backend_health(backend)
    active_tasks = list(ledger.list_active_tasks(limit=100))
    return HealthSnapshot(
        backend_healthy=backend_healthy,
        backend_summary=backend_summary,
        active_task_count=len(active_tasks),
        queued_count=sum(1 for task in active_tasks if task.status == TaskStatus.QUEUED),
        running_count=sum(1 for task in active_tasks if task.status == TaskStatus.RUNNING),
        waiting_approval_count=sum(
            1 for task in active_tasks if task.status == TaskStatus.WAITING_APPROVAL
        ),
        paused_count=sum(1 for task in active_tasks if task.status == TaskStatus.PAUSED),
    )


def _backend_health(backend: Any) -> tuple[bool, str]:
    if not hasattr(backend, "health"):
        return True, "backend health unavailable"
    health = backend.health()
    summary = str(health)
    if hasattr(health, "summary"):
        summary = str(health.summary())
    is_healthy = getattr(health, "is_healthy", True)
    if callable(is_healthy):
        is_healthy = is_healthy()
    return bool(is_healthy), summary
```

- [ ] **Step 3: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_health_snapshot.py -q
```

Expected: health snapshot tests pass.

## Task 7: EventBridge Watchdog Integration

**Files:**
- Modify: `wlcodex/event_bridge.py`
- Modify: `tests/test_event_bridge.py`

**Dependency:** Run after Tasks 2, 3, and 4 are merged.

- [ ] **Step 1: Add event bridge watchdog test**

Add to `tests/test_event_bridge.py`:

```python
class WatchdogSpy:
    def __init__(self) -> None:
        self.calls = 0

    def scan_once(self) -> int:
        self.calls += 1
        return 0


@pytest.mark.asyncio
async def test_task_watchdog_runs_without_backend_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("wlcodex.event_bridge.TASK_WATCHDOG_INTERVAL_SECONDS", 0.01)
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    watchdog = WatchdogSpy()
    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        task_watchdog=watchdog,
    )

    task = asyncio.create_task(bridge.run())
    try:
        for _ in range(50):
            if watchdog.calls:
                break
            await asyncio.sleep(0.01)
        assert watchdog.calls >= 1
    finally:
        task.cancel()
        await task
```

- [ ] **Step 2: Implement optional watchdog loop**

In `wlcodex/event_bridge.py`, add a module constant:

```python
TASK_WATCHDOG_INTERVAL_SECONDS = 60
```

Add an optional constructor parameter:

```python
task_watchdog: object | None = None,
```

Store it:

```python
self._task_watchdog = task_watchdog
```

In `run()`, create the task only when configured:

```python
watchdog_task = None
if self._task_watchdog is not None:
    watchdog_task = asyncio.create_task(
        self._task_watchdog_loop(), name="task-liveness-watchdog"
    )
```

Cancel it in `finally` just like `expiry_task`.

Add:

```python
async def _task_watchdog_loop(self) -> None:
    while True:
        await asyncio.sleep(TASK_WATCHDOG_INTERVAL_SECONDS)
        try:
            self._task_watchdog.scan_once()
        except Exception:
            logger.exception("Task liveness watchdog scan failed")
```

- [ ] **Step 3: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_event_bridge.py tests/test_watchdog.py -q
```

Expected: event bridge and watchdog tests pass.

## Task 8: Main Composition And Recovery Notifications

**Files:**
- Modify: `wlcodex/main.py`
- Modify: `tests/test_main_composition.py`

**Dependency:** Run after Tasks 2, 4, 5, and 7 are merged.

- [ ] **Step 1: Add composition tests**

Add `import pytest` near the top of `tests/test_main_composition.py`, then add tests that construct the modules directly:

```python
def test_task_watchdog_config_can_be_built_from_app_config(tmp_path: Path) -> None:
    from wlcodex.config import load_config
    from wlcodex.watchdog import TaskLivenessConfig

    config = load_config(Path("tests/fixtures/full_cockpit.toml"))
    watchdog_config = TaskLivenessConfig(
        max_running_seconds=config.task.max_running_seconds,
        max_queued_seconds=config.task.max_queued_seconds,
        max_waiting_approval_seconds=config.task.max_waiting_approval_seconds,
        backend_dead_grace_seconds=config.task.backend_dead_grace_seconds,
    )

    assert watchdog_config.max_running_seconds == 7200
```

Add an async test for the notification helper through direct import:

```python
@pytest.mark.asyncio
async def test_recovery_notification_helper_is_importable() -> None:
    from wlcodex.recovery_notifications import notify_recovery_paused_tasks

    assert callable(notify_recovery_paused_tasks)
```

- [ ] **Step 2: Wire watchdog config in `main.py`**

Import:

```python
from wlcodex.watchdog import TaskLivenessConfig, TaskWatchdog
from wlcodex.recovery_notifications import notify_recovery_paused_tasks
```

After `approval_service`, build:

```python
task_watchdog = TaskWatchdog(
    ledger=ledger,
    backend=backend,
    config=TaskLivenessConfig(
        max_running_seconds=config.task.max_running_seconds,
        max_queued_seconds=config.task.max_queued_seconds,
        max_waiting_approval_seconds=config.task.max_waiting_approval_seconds,
        backend_dead_grace_seconds=config.task.backend_dead_grace_seconds,
    ),
)
```

Pass it to `EventBridge`:

```python
task_watchdog=task_watchdog,
```

- [ ] **Step 3: Override watchdog interval from config**

In `main.py`, before constructing `EventBridge`, set:

```python
import wlcodex.event_bridge as event_bridge_module

event_bridge_module.TASK_WATCHDOG_INTERVAL_SECONDS = (
    config.task.watchdog_interval_seconds
)
```

This keeps constructor signatures smaller and matches the existing test style for interval monkeypatching.

- [ ] **Step 4: Send recovery notifications after Telegram starts**

Inside `_run()`, after `await app.updater.start_polling()`, add:

```python
if paused_ids:
    await notify_recovery_paused_tasks(
        ledger=ledger,
        paused_ids=paused_ids,
        send_telegram=handlers.send_telegram,
        edit_telegram=handlers.edit_telegram,
    )
```

This ensures the bot is initialized before trying to send messages.

- [ ] **Step 5: Verify**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_main_composition.py tests/test_recovery_notifications.py tests/test_event_bridge.py -q
```

Expected: composition, recovery notification, and event bridge tests pass.

## Task 9: Final Regression And Documentation Check

**Files:**
- Modify: `tests/test_drift_repairs.py`
- Modify: `README.md`

**Dependency:** Run after Tasks 1-8 are merged.

- [ ] **Step 1: Add end-to-end drift repair tests**

Add to `tests/test_drift_repairs.py`:

```python
def test_paused_continue_regression_releases_self_lock(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    ledger.set_task_status(task.id, TaskStatus.PAUSED)

    continued = service.continue_task(task.id, "continue")

    assert continued.status == TaskStatus.QUEUED


def test_paused_abort_regression_releases_workspace(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task("demo", "First", codex_thread_id="thread-1")
    ledger.set_task_status(task.id, TaskStatus.PAUSED)

    service.abort_task(task.id)
    next_task = service.start_task("demo", "Second", codex_thread_id="thread-2")

    assert next_task.id == task.id + 1
```

- [ ] **Step 2: Update README safety/runtime section**

Add a short section near "V1 safety rules":

```markdown
## Task liveness

WLCodex keeps one active write task per workspace. Paused tasks still hold
that write slot until they are continued, aborted, archived after terminal
state, or marked failed by the watchdog. The task watchdog can fail stale
queued, running, or waiting-approval tasks and records `task_timeout` or
`backend_dead` events in SQLite before releasing the workspace.
```

- [ ] **Step 3: Run targeted tests**

Run:

```bash
rtk .venv/bin/python -m pytest tests/test_task_service.py tests/test_db.py tests/test_watchdog.py tests/test_event_bridge.py tests/test_recovery_notifications.py tests/test_main_composition.py tests/test_drift_repairs.py -q
```

Expected: all targeted tests pass.

- [ ] **Step 4: Run full local verification**

Run:

```bash
rtk .venv/bin/python -m pytest -q
rtk .venv/bin/python -m ruff check .
```

Expected: tests and lint pass. If live Telegram or real app-server tests are gated by environment variables, leave them gated and record that they were not run.

## Self-Review Checklist

- [ ] Wave 1 tasks have disjoint write sets.
- [ ] Wave 2 tasks are sequential and do not run in parallel.
- [ ] Paused continue excludes only the current task and does not weaken new task locking.
- [ ] Paused abort releases the workspace and records an event.
- [ ] Watchdog scans run without backend events.
- [ ] Backend unhealthy state requires grace before failing tasks.
- [ ] Recovery notices are sent only to known Telegram chats.
- [ ] No status, watchdog, recovery, or ledger text is injected into Codex prompts.
- [ ] Full local verification commands have been run or explicitly recorded as blocked.
