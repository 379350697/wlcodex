"""Shared, read-only user-presentation contract.

The contract intentionally contains no persistence, transport, or lifecycle
logic.  Relay, Native, and compatibility surfaces can therefore describe an
already-known state without a page load or notification causing reconciliation
writes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


PRESENTATION_STATES = frozenset(
    {
        "running",
        "waiting_user",
        "waiting_approval",
        "blocked",
        "completed",
        "interrupted",
        "failed",
        "stale",
    }
)


def task_status_label(status: str) -> str:
    """Return the canonical user-facing label for a presentation state."""

    return {
        "queued": "排队中",
        "running": "进行中",
        "waiting_user": "等待你",
        "waiting_approval": "等待审批",
        "blocked": "已阻塞",
        "failed": "失败",
        "completed": "已完成",
        "interrupted": "已中断",
        "stale": "状态已陈旧",
    }.get(status, status or "未知")


def telegram_compatibility_presentation(
    *,
    legacy_compatible: bool,
    next_action: str,
    allowed_actions: Sequence[str],
) -> dict[str, Any]:
    """Build the common presentation contract for Telegram's legacy bridge.

    Telegram deliberately no longer owns a task/session lifecycle.  It must
    therefore never infer ``running`` or ``completed`` from an old
    conversation; the only truthful state is ``stale`` and the projection
    directs the user to the owning Native or Relay surface.  The helper is
    purely value based so a redirect, status hint, or callback response cannot
    write to the ledger or trigger lifecycle reconciliation.
    """

    if legacy_compatible:
        source = "telegram_legacy"
        reason = "Telegram 仅保留历史会话兼容，不是当前执行状态的真相来源。"
    else:
        source = "telegram_redirect"
        reason = "Telegram 不再创建新会话或维护旧主状态，请转到 Native 或 Relay。"
    action = str(next_action or "打开 Native 或 Relay").strip() or "打开 Native 或 Relay"
    return {
        "state": "stale",
        "freshness": {
            "source": source,
            "updated_at": "",
            "is_stale": True,
            "reason": reason,
        },
        "current_actor": {"role": "", "label": "", "status": ""},
        "blocking_reason": reason,
        "next_action": action,
        "allowed_actions": list(allowed_actions),
    }
