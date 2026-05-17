from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from wlcodex.health_snapshot import backend_health
from wlcodex.models import TaskStatus


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
        healthy, summary = backend_health(self._backend)
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

