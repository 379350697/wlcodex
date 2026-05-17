from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from wlcodex.db import Ledger
from wlcodex.status import render_task_card

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
