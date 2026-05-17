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

    def list_tasks(self, limit: int = 100, include_archived: bool = False):
        return self.list_active_tasks(limit=limit)


def test_build_health_snapshot_counts_active_tasks() -> None:
    snapshot = build_health_snapshot(Ledger(), Backend(True))

    assert snapshot.backend_healthy is True
    assert snapshot.backend_summary == "Backend healthy"
    assert snapshot.active_task_count == 2
    assert snapshot.running_count == 1
    assert snapshot.waiting_approval_count == 1
    assert snapshot.waiting_count == 0


def test_build_health_snapshot_detects_unhealthy_backend() -> None:
    snapshot = build_health_snapshot(Ledger(), Backend(False))

    assert snapshot.backend_healthy is False
    assert snapshot.backend_summary == "Backend unhealthy"


def test_build_health_snapshot_counts_all_statuses() -> None:
    class MixedLedger:
        def list_active_tasks(self, limit: int = 100):
            return [
                _task(1, TaskStatus.QUEUED),
                _task(2, TaskStatus.RUNNING),
                _task(3, TaskStatus.RUNNING),
                _task(4, TaskStatus.WAITING_APPROVAL),
                _task(5, TaskStatus.PAUSED),
                _task(6, TaskStatus.PAUSED),
                _task(7, TaskStatus.PAUSED),
            ]

        def list_tasks(self, limit: int = 100, include_archived: bool = False):
            return self.list_active_tasks(limit=limit)

    snapshot = build_health_snapshot(MixedLedger(), Backend(True))

    assert snapshot.active_task_count == 7
    assert snapshot.queued_count == 1
    assert snapshot.running_count == 2
    assert snapshot.waiting_approval_count == 1
    assert snapshot.paused_count == 3
    assert snapshot.waiting_count == 0


def test_build_health_snapshot_no_health_method() -> None:
    class NoHealthBackend:
        pass

    class EmptyLedger:
        def list_active_tasks(self, limit: int = 100):
            return []

        def list_tasks(self, limit: int = 100, include_archived: bool = False):
            return []

    snapshot = build_health_snapshot(EmptyLedger(), NoHealthBackend())

    assert snapshot.backend_healthy is True
    assert snapshot.backend_summary == "backend health unavailable"
    assert snapshot.active_task_count == 0
    assert snapshot.waiting_count == 0


def test_build_health_snapshot_callable_is_healthy() -> None:
    @dataclass
    class CallableHealth:
        def is_healthy(self) -> bool:
            return False

        def summary(self) -> str:
            return "down"

    class CallableBackend:
        def health(self) -> CallableHealth:
            return CallableHealth()

    class EmptyLedger:
        def list_active_tasks(self, limit: int = 100):
            return []

        def list_tasks(self, limit: int = 100, include_archived: bool = False):
            return []

    snapshot = build_health_snapshot(EmptyLedger(), CallableBackend())

    assert snapshot.backend_healthy is False
    assert snapshot.backend_summary == "down"
    assert snapshot.waiting_count == 0


def test_build_health_snapshot_counts_waiting_tasks() -> None:
    class WaitingLedger:
        def list_active_tasks(self, limit: int = 100):
            return [
                _task(1, TaskStatus.RUNNING),
            ]

        def list_tasks(self, limit: int = 100, include_archived: bool = False):
            return [
                _task(1, TaskStatus.RUNNING),
                _task(2, TaskStatus.WAITING_SLOT),
                _task(3, TaskStatus.WAITING_SLOT),
                _task(4, TaskStatus.WAITING_SLOT),
            ]

    snapshot = build_health_snapshot(WaitingLedger(), Backend(True))

    assert snapshot.active_task_count == 1
    assert snapshot.running_count == 1
    assert snapshot.waiting_count == 3
