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
