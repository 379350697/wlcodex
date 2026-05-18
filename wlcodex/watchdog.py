from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from wlcodex.health_snapshot import backend_health
from wlcodex.models import TaskStatus

if TYPE_CHECKING:
    from wlcodex.runtime_event_store import RuntimeEventStore


@dataclass(frozen=True)
class TaskLivenessConfig:
    max_running_seconds: int
    max_queued_seconds: int
    max_waiting_approval_seconds: int
    backend_dead_grace_seconds: int


class TaskWatchdog:
    def __init__(
        self,
        ledger: Any,
        backend: Any,
        config: TaskLivenessConfig,
        *,
        runtime_store: "RuntimeEventStore | None" = None,
        conversation_id: int | None = None,
    ) -> None:
        self._ledger = ledger
        self._backend = backend
        self._config = config
        self._runtime_store = runtime_store
        self._conversation_id = conversation_id
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
                self._emit_timeout_event(
                    task_id=task.id,
                    timeout_type="hard",
                    threshold_seconds=threshold,
                    age_seconds=age,
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

    def _emit_timeout_event(
        self,
        *,
        task_id: int,
        timeout_type: str,
        threshold_seconds: int,
        age_seconds: int,
    ) -> None:
        """Emit a watchdog timeout event to the runtime store when available."""
        if self._runtime_store is None:
            return

        from wlcodex.runtime_events import (
            AggregateType,
            EventSource,
            EventType,
            RuntimeEvent,
            Visibility,
            now_iso,
        )

        event_type = (
            EventType.WATCHDOG_HARD_TIMEOUT
            if timeout_type == "hard"
            else EventType.WATCHDOG_IDLE_TIMEOUT
        )
        correlation_id = f"watchdog-{task_id}-{now_iso()}"

        event = RuntimeEvent(
            schema_version=1,
            event_type=event_type,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(task_id),
            correlation_id=correlation_id,
            source=EventSource.WATCHDOG,
            actor="watchdog",
            visibility=Visibility.OPERATOR,
            payload={
                "task_id": task_id,
                "timeout_type": timeout_type,
                "threshold_seconds": threshold_seconds,
                "age_seconds": age_seconds,
                "reason": f"task timed out after {age_seconds}s (limit {threshold_seconds}s)",
            },
            occurred_at=now_iso(),
            task_id=task_id,
            conversation_id=self._conversation_id,
        )
        self._runtime_store.append(event)

    def _threshold_for(self, status: TaskStatus) -> int | None:
        if status == TaskStatus.RUNNING:
            return self._config.max_running_seconds
        if status == TaskStatus.QUEUED:
            return self._config.max_queued_seconds
        if status == TaskStatus.WAITING_APPROVAL:
            return self._config.max_waiting_approval_seconds
        return None

