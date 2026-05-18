from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from wlcodex.db import Ledger
from wlcodex.runtime_events import EventType
from wlcodex.status import render_task_card

if TYPE_CHECKING:
    from wlcodex.runtime_events import RuntimeEvent

logger = logging.getLogger(__name__)

SendTelegram = Callable[[int, str, list[list[dict[str, str]]] | None], Awaitable[int]]
EditTelegram = Callable[[int, int, str], Awaitable[None]]


async def notify_recovery_paused_tasks(
    *,
    ledger: Ledger,
    paused_ids: list[int],
    send_telegram: SendTelegram,
    edit_telegram: EditTelegram | None,
) -> int:
    sent = 0
    for task_id in paused_ids:
        try:
            task = ledger.get_task(task_id)
        except KeyError:
            continue
        if task.telegram_chat_id is None:
            continue

        text = (
            f"任务 #{task.id} 已因 WLCodex 重启暂停。\n"
            f"可用 /continue {task.id} <prompt> 继续，"
            f"或 /abort {task.id} 释放工作区。"
        )
        try:
            await send_telegram(task.telegram_chat_id, text, None)
            sent += 1
        except Exception:
            logger.exception("failed to send recovery notification for task #%d", task.id)

        if edit_telegram is not None and task.telegram_status_message_id is not None:
            try:
                await edit_telegram(
                    task.telegram_chat_id,
                    task.telegram_status_message_id,
                    render_task_card(task),
                )
            except Exception:
                logger.exception("failed to edit recovery status card for task #%d", task.id)
    return sent


def format_recovery_summary(
    *,
    recovery_events: list["RuntimeEvent"],
    paused_task_ids: list[int],
    orch_marked: int,
    agent_marked: int,
) -> str:
    """Build a human-readable summary of recovery actions from events.

    Reads the appended recovery events to describe what happened during
    startup recovery without requiring access to mutable projection state.
    """
    orphaned_ids: list[int] = []
    for ev in recovery_events:
        if ev.event_type == EventType.AGENT_RUN_ORPHANED:
            rid = ev.payload.get("agent_run_id", 0)
            if rid:
                orphaned_ids.append(int(rid))

    lines = ["WLCodex 启动恢复摘要：", ""]

    if paused_task_ids:
        lines.append(f"暂停的任务（{len(paused_task_ids)}）：")
        for tid in paused_task_ids:
            lines.append(f"  #{tid}")
    else:
        lines.append("暂停的任务：无")

    lines.append("")

    if orphaned_ids:
        lines.append(f"孤儿 Agent 运行（{len(orphaned_ids)}）：")
        for rid in orphaned_ids:
            lines.append(f"  Agent 运行 #{rid} — 标记为 orphaned")
    else:
        lines.append("孤儿 Agent 运行：无")

    lines.append("")
    lines.append(
        f"编排运行标记为失败：{orch_marked}  |  Agent 运行标记为失败：{agent_marked}"
    )

    total = len(paused_task_ids) + len(orphaned_ids) + orch_marked + agent_marked
    lines.append(f"总恢复操作：{total}")

    return "\n".join(lines)


async def notify_event_sourced_recovery(
    *,
    recovery_summary: str,
    chat_ids: set[int],
    send_telegram: SendTelegram,
) -> int:
    """Send recovery summary to affected Telegram chats.

    Returns the number of notifications sent.
    """
    sent = 0
    for chat_id in chat_ids:
        try:
            await send_telegram(chat_id, recovery_summary, None)
            sent += 1
        except Exception:
            logger.exception(
                "failed to send event-sourced recovery notification to chat %d",
                chat_id,
            )
    return sent
