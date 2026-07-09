from __future__ import annotations

from typing import Any


ACTIVE_TURN_STATUSES = ("queued", "streaming", "waiting", "pending", "running")
COMPLETED_TURN_STATUSES = ("completed", "done", "succeeded", "success")
FAILED_TURN_STATUSES = (
    "failed",
    "error",
    "cancelled",
    "canceled",
    "interrupted",
    "aborted",
    "timed_out",
    "timeout",
    "orphaned",
)
INTERRUPTED_TURN_STATUSES = ("interrupted", "cancelled", "canceled", "aborted")
TIMEOUT_TURN_STATUSES = ("timed_out", "timeout")


def normalize_turn_status(status: object) -> str:
    return str(status or "").strip().lower()


def is_active_turn_status(status: object) -> bool:
    return normalize_turn_status(status) in ACTIVE_TURN_STATUSES


def is_completed_turn_status(status: object) -> bool:
    return normalize_turn_status(status) in COMPLETED_TURN_STATUSES


def is_failed_turn_status(status: object) -> bool:
    return normalize_turn_status(status) in FAILED_TURN_STATUSES


def turn_terminal_label(status: object) -> str:
    normalized = normalize_turn_status(status)
    if normalized in INTERRUPTED_TURN_STATUSES:
        return "已中断"
    if normalized in TIMEOUT_TURN_STATUSES:
        return "已超时"
    return "执行失败"


def turn_semantics_json() -> dict[str, Any]:
    return {
        "active": list(ACTIVE_TURN_STATUSES),
        "completed": list(COMPLETED_TURN_STATUSES),
        "failed": list(FAILED_TURN_STATUSES),
        "interrupted": list(INTERRUPTED_TURN_STATUSES),
        "timeout": list(TIMEOUT_TURN_STATUSES),
    }
