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
    runtime_event_store=None,
) -> EventBridge:
    return EventBridge(
        task_service=service,
        backend=backend,
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=edit_telegram,
        approval_service=ApprovalSpy(),
        runtime_event_store=runtime_event_store,
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
async def test_backend_events_do_not_edit_legacy_task_status_cards(tmp_path: Path) -> None:
    """Backend events no longer refresh legacy task cards in Telegram."""
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

    assert edited == []


@pytest.mark.asyncio
async def test_terminal_status_does_not_refresh_legacy_task_status_card(tmp_path: Path) -> None:
    """Terminal task state is tracked in runtime events, not old task cards."""
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

    assert edited == []


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


@pytest.mark.asyncio
async def test_orchestration_managed_delta_not_forwarded_to_interaction_renderer(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Chief engineer task",
        codex_thread_id="thread-1",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="新对话",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    ledger.create_orchestration_run(conversation.id, "Chief engineer task")
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
            {"threadId": "thread-1", "delta": "large internal codex packet"},
        )
    )

    assert received == []


@pytest.mark.asyncio
async def test_agent_message_delta_skipped_when_renderer_is_none(tmp_path: Path) -> None:
    """When interaction_renderer is None (streaming disabled / legacy profile),
    agent_message_delta must NOT cause errors or try to forward."""
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

    # No interaction_renderer passed — should be a no-op
    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        interaction_renderer=None,
    )

    # Must not crash
    await bridge.process_event(
        BackendEvent(
            "agent_message_delta",
            {"threadId": "thread-1", "delta": "hello"},
        )
    )


@pytest.mark.asyncio
async def test_terminal_event_skipped_when_renderer_is_none(tmp_path: Path) -> None:
    """When interaction_renderer is None, terminal events must be skipped."""
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

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        interaction_renderer=None,
    )

    # Must not crash — terminal forward is a no-op when renderer is None
    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-1", "status": "completed"},
    ))


# ===================================================================
# Runtime event bridge contract tests (Lane E)
# ===================================================================


@pytest.mark.asyncio
async def test_runtime_progress_event_flows_to_renderer(tmp_path: Path) -> None:
    """runtime_progress events flow through the interaction renderer."""
    from wlcodex.interaction.events import InteractionEvent
    from wlcodex.interaction.runtime_renderer import RuntimeRunState

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    received: list[InteractionEvent] = []

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

    # Simulate runtime_progress event coming through the bridge
    state = RuntimeRunState(phase="running_implementation", active_agent="claude")
    await bridge._interaction_renderer.handle(
        InteractionEvent(
            event_type="runtime_progress",
            chat_id=123,
            conversation_id=1,
            metadata={"runtime_state": state},
        )
    )

    assert len(received) == 1
    assert received[0].event_type == "runtime_progress"
    assert received[0].metadata["runtime_state"] is state


@pytest.mark.asyncio
async def test_runtime_progress_noop_when_renderer_is_none(tmp_path: Path) -> None:
    """runtime_progress must not crash when interaction_renderer is None."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        interaction_renderer=None,
    )

    assert bridge._interaction_renderer is None
    # No crash expected


@pytest.mark.asyncio
async def test_runtime_events_dont_interfere_with_approval_cards(tmp_path: Path) -> None:
    """Runtime progress and approval events can coexist without collision."""
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
    sent: list[str] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append(text)
        return len(sent)

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
    )

    # Process approval (existing flow)
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
            "reason": "Allow?",
        },
    ))

    # Approval was sent
    assert any("审批" in s for s in sent)

    # Runtime events don't break subsequent processing
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-1", "status": "completed"},
    ))


@pytest.mark.asyncio
async def test_event_bridge_maps_codex_backend_events_to_runtime_events(
    tmp_path: Path,
) -> None:
    """Codex direct/EventBridge events must enter the runtime event ledger."""
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run runtime probe",
        codex_thread_id="thread-runtime",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="runtime",
        mode="codex_direct",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        "analysis",
        hidden_task_id=task.id,
    )
    ledger.update_agent_run_status(agent_run.id, "running")

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        runtime_event_store=store,
    )

    await bridge.process_event(BackendEvent(
        "token_usage_updated",
        {
            "threadId": "thread-runtime",
            "turnId": "turn-runtime",
            "tokenUsage": {
                "last": {"inputTokens": 7, "outputTokens": 3},
            },
        },
    ))

    events = store.list_by_agent_run(agent_run.id)
    assert [event.event_type for event in events] == [EventType.MODEL_USAGE_UPDATED]
    assert events[0].correlation_id == f"codex-task-{task.id}"
    assert events[0].conversation_id == conversation.id
    assert events[0].task_id == task.id
    assert events[0].payload["input_tokens"] == 7


@pytest.mark.asyncio
async def test_direct_codex_turn_completion_marks_agent_run_done(
    tmp_path: Path,
) -> None:
    from wlcodex.runtime_diagnostics import find_non_terminal_agent_runs
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run direct probe",
        codex_thread_id="thread-direct",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="direct",
        mode="codex_direct",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        "implementation",
        hidden_task_id=task.id,
        external_session_id="thread-direct",
    )
    ledger.update_agent_run_status(agent_run.id, "running")

    bridge = _bridge(service, IdleBackend(), ledger, runtime_event_store=store)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-direct", "turnId": "turn-direct"},
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-direct", "status": "completed"},
    ))

    assert ledger.get_task(task.id).status.value == "done"
    assert ledger.get_agent_run(agent_run.id).status == "done"
    events = store.list_by_agent_run(agent_run.id)
    event_types = [event.event_type for event in events]
    assert EventType.AGENT_RUN_COMPLETED in event_types
    assert find_non_terminal_agent_runs(store) == []


@pytest.mark.asyncio
async def test_event_bridge_maps_approval_resolved_without_thread_id(
    tmp_path: Path,
) -> None:
    """Backend approval_resolved events only carry codexRequestId and still need runtime context."""
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run approval probe",
        codex_thread_id="thread-approval",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="approval",
        mode="codex_direct",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        "analysis",
        hidden_task_id=task.id,
    )
    ledger.update_agent_run_status(agent_run.id, "running")

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=_send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        runtime_event_store=store,
    )

    await bridge.process_event(BackendEvent(
        "approval_requested",
        {
            "threadId": "thread-approval",
            "codexRequestId": "approval-1",
            "kind": "command",
            "summary": "run tests",
        },
    ))
    await bridge.process_event(BackendEvent(
        "approval_resolved",
        {
            "codexRequestId": "approval-1",
            "response": {"decision": "accept"},
        },
    ))

    event_types = [event.event_type for event in store.list_by_agent_run(agent_run.id)]
    assert EventType.APPROVAL_REQUESTED in event_types
    assert EventType.APPROVAL_RESOLVED in event_types
