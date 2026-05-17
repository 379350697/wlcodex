import asyncio
from pathlib import Path

import pytest

from wlcodex.codex_backend import BackendEvent
from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.event_bridge import EventBridge
from wlcodex.task_service import TaskService


class IdleBackend:
    async def events(self):
        while True:
            await asyncio.sleep(3600)
            yield None


class ApprovalSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def expire_stale_approvals(self, ledger, backend) -> int:
        self.calls += 1
        return 0


async def _send_telegram(chat_id: int, text: str, buttons=None) -> int:
    return 1


async def _edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
    return None


def _bridge(
    service: TaskService,
    backend: object,
    ledger: Ledger,
    send_telegram=_send_telegram,
    edit_telegram=_edit_telegram,
) -> EventBridge:
    return EventBridge(
        task_service=service,
        backend=backend,
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=edit_telegram,
        approval_service=ApprovalSpy(),
    )


@pytest.mark.asyncio
async def test_expiry_scan_runs_without_backend_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "wlcodex.event_bridge.EXPIRY_SCAN_INTERVAL_SECONDS", 0.01
    )
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    approval = ApprovalSpy()
    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=approval,
    )

    task = asyncio.create_task(bridge.run())
    try:
        for _ in range(50):
            if approval.calls:
                break
            await asyncio.sleep(0.01)
        assert approval.calls >= 1
    finally:
        task.cancel()
        await task


@pytest.mark.asyncio
async def test_approval_request_sends_approval_card_without_status_refresh(tmp_path: Path) -> None:
    """Approval cards should not also cause a noisy status-card edit."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run approval probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    ledger.set_status_message(task.id, 123, 456)
    sent: list[str] = []
    edited: list[str] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append(text)
        return 42

    async def edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
        edited.append(text)

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram, edit_telegram)
    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ))
    edited.clear()

    await bridge.process_event(BackendEvent(
        "approval_requested",
        {
            "threadId": "thread-1",
            "codexRequestId": "0",
            "kind": "file_change",
            "reason": "command failed; retry without sandbox?",
        },
    ))

    assert len(sent) == 1
    assert "审批" in sent[0]
    assert edited == []


@pytest.mark.asyncio
async def test_active_thread_status_does_not_refresh_status_card(tmp_path: Path) -> None:
    """Backend active-phase churn should stay out of Telegram status cards."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    ledger.set_status_message(task.id, 123, 456)
    edited: list[str] = []

    async def edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
        edited.append(text)

    bridge = _bridge(service, IdleBackend(), ledger, edit_telegram=edit_telegram)
    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ))
    edited.clear()

    await bridge.process_event(BackendEvent(
        "thread_status_changed",
        {"threadId": "thread-1", "status": {"type": "active"}},
    ))

    assert edited == []


@pytest.mark.asyncio
async def test_duplicate_status_card_payload_is_not_edited_twice(tmp_path: Path) -> None:
    """Rendering the same status card twice should produce at most one edit."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    ledger.set_status_message(task.id, 123, 456)
    edited: list[str] = []

    async def edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
        edited.append(text)

    bridge = _bridge(service, IdleBackend(), ledger, edit_telegram=edit_telegram)
    event = BackendEvent(
        "turn_started",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )

    await bridge.process_event(event)
    await bridge.process_event(event)

    assert len(edited) == 1


@pytest.mark.asyncio
async def test_terminal_status_still_refreshes_status_card(tmp_path: Path) -> None:
    """Skipping active churn must not hide the terminal task state."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    ledger.set_status_message(task.id, 123, 456)
    edited: list[str] = []

    async def edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
        edited.append(text)

    bridge = _bridge(service, IdleBackend(), ledger, edit_telegram=edit_telegram)
    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ))
    edited.clear()

    await bridge.process_event(BackendEvent(
        "thread_status_changed",
        {"threadId": "thread-1", "status": {"type": "active"}},
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-1", "status": "completed"},
    ))

    assert len(edited) == 1
    assert "已完成" in edited[0]


@pytest.mark.asyncio
async def test_approval_card_uses_reason_when_summary_missing(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    sent: list[str] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append(text)
        return 42

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
    )
    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ))
    await bridge.process_event(BackendEvent(
        "approval_requested",
        {
            "threadId": "thread-1",
            "codexRequestId": "0",
            "kind": "command",
            "reason": "Allow writing /home/wl/probe outside the workspace?",
            "command": "python3 probe.py",
        },
    ))

    assert task.id == 1
    assert sent
    assert "Allow writing /home/wl/probe outside the workspace?" in sent[-1]


@pytest.mark.asyncio
async def test_approval_buttons_are_chinese(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    service.start_task(
        "demo",
        "Run probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    sent_buttons: list[list[list[dict[str, str]]]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent_buttons.append(buttons)
        return 42

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
    )
    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ))
    await bridge.process_event(BackendEvent(
        "approval_requested",
        {
            "threadId": "thread-1",
            "codexRequestId": "0",
            "kind": "command",
            "reason": "Allow writing outside the workspace?",
            "command": "python3 probe.py",
        },
    ))

    labels = [button["text"] for row in sent_buttons[-1] for button in row]
    assert labels == ["批准一次", "本会话批准", "拒绝", "取消"]


class WatchdogSpy:
    def __init__(self) -> None:
        self.scans = 0

    def scan_once(self) -> int:
        self.scans += 1
        return 0


@pytest.mark.asyncio
async def test_watchdog_scan_runs_without_backend_events(tmp_path: Path, monkeypatch) -> None:
    """TaskWatchdog.scan_once() is called periodically even when no backend events arrive."""
    monkeypatch.setattr(
        "wlcodex.event_bridge.TASK_WATCHDOG_INTERVAL_SECONDS", 0.01
    )
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    wd = WatchdogSpy()
    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        task_watchdog=wd,
        watchdog_interval_seconds=0.01,
    )

    task = asyncio.create_task(bridge.run())
    try:
        for _ in range(50):
            if wd.scans >= 1:
                break
            await asyncio.sleep(0.01)
        assert wd.scans >= 1
    finally:
        task.cancel()
        await task


@pytest.mark.asyncio
async def test_agent_message_delta_forwards_to_interaction_renderer(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run probe",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    received = []

    class Interaction:
        async def handle(self, event):
            received.append(event)

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        interaction_renderer=Interaction(),
    )

    await bridge.process_event(
        BackendEvent(
            "agent_message_delta",
            {"threadId": "thread-1", "delta": "hello"},
        )
    )

    assert received
    assert received[0].event_type == "text_delta"
    assert received[0].chat_id == 123
    assert received[0].task_id == task.id
    assert received[0].text == "hello"
