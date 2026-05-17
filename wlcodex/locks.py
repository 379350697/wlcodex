from __future__ import annotations

from wlcodex.models import Task, TaskStatus


ACTIVE_WRITE_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.PAUSED,
}


def active_write_task(
    tasks: list[Task],
    workspace_alias: str,
    exclude_task_id: int | None = None,
) -> Task | None:
    for task in tasks:
        if exclude_task_id is not None and task.id == exclude_task_id:
            continue
        if task.workspace_alias != workspace_alias:
            continue
        if task.status not in ACTIVE_WRITE_STATUSES:
            continue
        # Worktree-isolated tasks don't block the original workspace.
        if task.worktree_path:
            continue
        return task
    return None
