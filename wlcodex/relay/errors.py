"""Relay domain exceptions shared by route and service layers."""

from __future__ import annotations

from typing import Any


class ActiveRelayTasksDecisionRequired(ValueError):
    """A workspace has live Relay work and no new-task policy was selected."""

    def __init__(self, tasks: list[Any]) -> None:
        super().__init__("active Relay tasks require an explicit new-task decision")
        self.tasks = list(tasks)
