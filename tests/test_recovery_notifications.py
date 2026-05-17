from __future__ import annotations

import pytest

from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.recovery_notifications import notify_recovery_paused_tasks


@pytest.mark.asyncio
async def test_notify_recovery_paused_tasks_sends_only_tasks_with_chat(tmp_path):
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    with_chat = ledger.create_task("demo", "/tmp/demo", "Paused", "thread-1", None, 123)
    ledger.set_task_status(with_chat.id, TaskStatus.PAUSED)
    no_chat = ledger.create_task("demo", "/tmp/demo", "No chat", "thread-2", None, None)
    ledger.set_task_status(no_chat.id, TaskStatus.PAUSED)
    sent: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text))
        return 99

    count = await notify_recovery_paused_tasks(
        ledger=ledger,
        paused_ids=[with_chat.id, no_chat.id],
        send_telegram=send,
        edit_telegram=None,
    )

    assert count == 1
    assert sent[0][0] == 123
    assert f"任务 #{with_chat.id}" in sent[0][1]
    assert "/continue" in sent[0][1]
    assert "/abort" in sent[0][1]


@pytest.mark.asyncio
async def test_notify_recovery_paused_tasks_edits_status_card(tmp_path):
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Paused", "thread-1", None, 123)
    ledger.set_task_status(task.id, TaskStatus.PAUSED)
    ledger.set_status_message(task.id, 123, 456)
    edits: list[tuple[int, int, str]] = []

    async def send(chat_id: int, text: str, buttons=None) -> int:
        return 99

    async def edit(chat_id: int, message_id: int, text: str) -> None:
        edits.append((chat_id, message_id, text))

    await notify_recovery_paused_tasks(
        ledger=ledger,
        paused_ids=[task.id],
        send_telegram=send,
        edit_telegram=edit,
    )

    assert edits[0][0] == 123
    assert edits[0][1] == 456
    assert "已暂停" in edits[0][2]
