"""Internal Workbench execution scheduling boundary.

The current database still uses task rows to guard workspace writes and to
bridge older Codex thread plumbing.  Workbench code should express that as a
run intent and receive an execution lease instead of presenting task semantics.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunIntent:
    conversation_id: int
    workspace_alias: str
    prompt: str
    telegram_chat_id: int | None
    purpose: str


@dataclass(frozen=True)
class ExecutionLease:
    hidden_task_id: int
    workspace_alias: str
    prompt: str
    purpose: str
    task: object


class ExecutionScheduler:
    """Reserve internal execution leases for Workbench runs."""

    def __init__(self, task_service: object, ledger: object) -> None:
        self._service = task_service
        self._ledger = ledger

    def reserve(self, intent: RunIntent) -> ExecutionLease:
        task = self._service.reserve_task(
            intent.workspace_alias,
            intent.prompt,
            telegram_chat_id=intent.telegram_chat_id,
        )
        self._ledger.set_conversation_active_task(intent.conversation_id, task.id)
        return ExecutionLease(
            hidden_task_id=task.id,
            workspace_alias=intent.workspace_alias,
            prompt=intent.prompt,
            purpose=intent.purpose,
            task=task,
        )
