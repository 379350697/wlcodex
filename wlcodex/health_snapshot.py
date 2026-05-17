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
    waiting_count: int = 0
    isolated_running_count: int = 0


def build_health_snapshot(ledger: Any, backend: Any) -> HealthSnapshot:
    backend_healthy, backend_summary = backend_health(backend)
    active_tasks = list(ledger.list_active_tasks(limit=100))
    all_tasks = list(ledger.list_tasks(limit=100))
    waiting_count = sum(
        1 for task in all_tasks if task.status == TaskStatus.WAITING_SLOT
    )
    isolated_running_count = sum(
        1 for task in all_tasks
        if task.worktree_path
        and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED)
    )
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
        waiting_count=waiting_count,
        isolated_running_count=isolated_running_count,
    )


def backend_health(backend: Any) -> tuple[bool, str]:
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
