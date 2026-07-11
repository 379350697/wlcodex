"""Read-only Relay task-detail projection shared by HTML and SSE callers.

The projection deliberately contains only data already persisted by Relay and
the live-stream hub.  It is the boundary that keeps a page refresh from
silently reconciling, dispatching, or otherwise advancing a task lifecycle.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RelayTaskDetailReadModel:
    """Everything a task-detail surface needs to render its current truth."""

    detail: Any
    events: list[Any]
    token_stats: Any


async def build_relay_task_detail_read_model(
    service: Any,
    task_id: int,
) -> RelayTaskDetailReadModel:
    """Load a task-detail snapshot without invoking lifecycle reconciliation."""

    if service is None:
        raise KeyError(f"unknown relay task id: {task_id}")
    detail = service.get_task_readonly(task_id)
    if inspect.isawaitable(detail):
        detail = await detail
    events = service.events_for_task(task_id)
    if inspect.isawaitable(events):
        events = await events
    token_stats = service.task_token_stats(task_id)
    if inspect.isawaitable(token_stats):
        token_stats = await token_stats
    return RelayTaskDetailReadModel(
        detail=detail,
        events=list(events or []),
        token_stats=token_stats,
    )
