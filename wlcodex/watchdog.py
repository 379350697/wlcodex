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
                    now=now,
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
        now: datetime,
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
        last_runtime = self._last_runtime_event_for_task(task_id)
        agent_run_id = (
            int(last_runtime["agent_run_id"])
            if last_runtime is not None and last_runtime["agent_run_id"] is not None
            else None
        )
        conversation_id = (
            int(last_runtime["conversation_id"])
            if last_runtime is not None and last_runtime["conversation_id"] is not None
            else self._conversation_id
        )
        last_event_id = int(last_runtime["id"]) if last_runtime is not None else 0
        last_event_type = (
            str(last_runtime["event_type"]) if last_runtime is not None else ""
        )
        elapsed_idle = age_seconds
        if last_runtime is not None:
            try:
                last_dt = datetime.fromisoformat(str(last_runtime["occurred_at"]))
                elapsed_idle = int((now - last_dt).total_seconds())
            except (TypeError, ValueError):
                elapsed_idle = age_seconds
        elapsed_hard = self._elapsed_hard_seconds(agent_run_id, fallback=age_seconds, now=now)
        healthy, _summary = backend_health(self._backend)
        subprocess_status = "healthy" if healthy else "unhealthy"

        event = RuntimeEvent(
            schema_version=1,
            event_type=event_type,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(agent_run_id or task_id),
            correlation_id=correlation_id,
            source=EventSource.WATCHDOG,
            actor="watchdog",
            visibility=Visibility.OPERATOR,
            payload={
                "task_id": task_id,
                "agent_run_id": agent_run_id,
                "last_event_id": last_event_id,
                "last_event_type": last_event_type,
                "elapsed_hard_seconds": elapsed_hard,
                "elapsed_idle_seconds": max(0, elapsed_idle),
                "subprocess_status": subprocess_status,
                "timeout_type": timeout_type,
                "threshold_seconds": threshold_seconds,
                "age_seconds": age_seconds,
                "reason": f"task timed out after {age_seconds}s (limit {threshold_seconds}s)",
            },
            occurred_at=now_iso(),
            task_id=task_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
        )
        stored_timeout = self._runtime_store.append(event)
        if agent_run_id is not None:
            self._runtime_store.append(RuntimeEvent(
                schema_version=1,
                event_type=EventType.AGENT_RUN_TIMED_OUT,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=correlation_id,
                source=EventSource.WATCHDOG,
                actor="watchdog",
                visibility=Visibility.OPERATOR,
                payload={
                    "agent_run_id": agent_run_id,
                    "reason": f"{timeout_type}_timeout",
                    "threshold_seconds": threshold_seconds,
                    "last_event_id": last_event_id,
                    "last_event_type": last_event_type,
                    "elapsed_hard_seconds": elapsed_hard,
                    "elapsed_idle_seconds": max(0, elapsed_idle),
                    "subprocess_status": subprocess_status,
                },
                occurred_at=now_iso(),
                task_id=task_id,
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
                causation_id=stored_timeout.id,
            ))

    def _last_runtime_event_for_task(self, task_id: int) -> Any:
        if self._runtime_store is None:
            return None
        return self._runtime_store._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()

    def _elapsed_hard_seconds(
        self,
        agent_run_id: int | None,
        *,
        fallback: int,
        now: datetime,
    ) -> int:
        if self._runtime_store is None or agent_run_id is None:
            return fallback
        row = self._runtime_store._conn.execute(
            """
            SELECT occurred_at FROM runtime_events
            WHERE agent_run_id = ?
              AND event_type = 'agent.run.started'
            ORDER BY id ASC
            LIMIT 1
            """,
            (agent_run_id,),
        ).fetchone()
        if row is None:
            return fallback
        try:
            started = datetime.fromisoformat(str(row["occurred_at"]))
            return max(0, int((now - started).total_seconds()))
        except (TypeError, ValueError):
            return fallback

    def _threshold_for(self, status: TaskStatus) -> int | None:
        if status == TaskStatus.RUNNING:
            return self._config.max_running_seconds
        if status == TaskStatus.QUEUED:
            return self._config.max_queued_seconds
        if status == TaskStatus.WAITING_APPROVAL:
            return None
        return None
