"""Waiting task decision callback — separate protocol from approval callbacks.

Never injects status/log/queue text into Codex prompts.
"""

from __future__ import annotations

from dataclasses import dataclass

CALLBACK_SEPARATOR = ":"
WAITING_PREFIX = "waiting"
WORKTREE_DONE_PREFIX = "worktree_done"

# Phase-1 actions (on waiting_slot task card)
KEEP = "keep"
SHOW_BLOCKER = "show_blocker"
ABORT_BLOCKER_START_NEXT = "abort_blocker_start_next"
ABORT_BLOCKER_CONFIRM = "abort_blocker_confirm"  # second-step confirmation
CONTINUE_BLOCKER = "continue_blocker"  # show /continue hint for the blocker
FORCE_PARALLEL_REQUEST = "force_parallel_request"
FORCE_PARALLEL_CONFIRM = "force_parallel_confirm"
WORKTREE_ISOLATED = "worktree_isolated"

# Phase-2 actions (post-completion worktree decisions)
WORKTREE_DIFF = "diff"
WORKTREE_MERGE = "merge"
WORKTREE_DISCARD = "discard"
WORKTREE_KEEP = "keep"


@dataclass(frozen=True)
class WaitingCallback:
    task_id: int
    action: str


def encode_waiting_callback(task_id: int, action: str) -> str:
    return f"{WAITING_PREFIX}{CALLBACK_SEPARATOR}{task_id}{CALLBACK_SEPARATOR}{action}"


def decode_waiting_callback(data: str) -> WaitingCallback | None:
    try:
        parts = data.split(CALLBACK_SEPARATOR)
        if len(parts) != 3 or parts[0] != WAITING_PREFIX:
            return None
        return WaitingCallback(task_id=int(parts[1]), action=parts[2])
    except (ValueError, TypeError):
        return None


def encode_worktree_done_callback(task_id: int, action: str) -> str:
    return f"{WORKTREE_DONE_PREFIX}{CALLBACK_SEPARATOR}{task_id}{CALLBACK_SEPARATOR}{action}"


def decode_worktree_done_callback(data: str) -> WaitingCallback | None:
    try:
        parts = data.split(CALLBACK_SEPARATOR)
        if len(parts) != 3 or parts[0] != WORKTREE_DONE_PREFIX:
            return None
        return WaitingCallback(task_id=int(parts[1]), action=parts[2])
    except (ValueError, TypeError):
        return None
