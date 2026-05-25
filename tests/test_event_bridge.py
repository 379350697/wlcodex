import asyncio
from pathlib import Path

import pytest

from wlcodex.codex_backend import BackendEvent
from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.event_bridge import EventBridge, _parse_audit_report_payload
from wlcodex.task_service import TaskService

pytestmark = pytest.mark.slow


def test_parse_audit_report_accepts_json_verdict_shape():
    payload = _parse_audit_report_payload("""
Auditor notes before JSON.
{
  "audit_report": {
    "verdict": "BLOCK",
    "risk": "MEDIUM",
    "summary": "README change is correct, but unrelated dirty files exist.",
    "findings": [
      {
        "severity": "blocking",
        "title": "Unrelated dirty files",
        "detail": "Workspace contains platform changes"
      }
    ],
    "missing_evidence": ["task-scoped diff baseline"]
  }
}
""")

    assert payload["decision"] == "block"
    assert payload["risk_level"] == "medium"
    assert payload["summary"].startswith("README change is correct")
    assert "Unrelated dirty files" in payload["findings"][0]
    assert payload["missing_evidence"] == ["task-scoped diff baseline"]


def test_parse_audit_report_recovers_truncated_json_verdict():
    payload = _parse_audit_report_payload(
        'notes {"audit_report": {"verdict": "BLOCK", "risk": "MEDIUM", '
        '"summary": "README task is blocked by unrelated dirty files", '
        '"findings": [{"title": "truncated"'
    )

    assert payload["decision"] == "block"
    assert payload["risk_level"] == "medium"
    assert payload["summary"] == "README task is blocked by unrelated dirty files"


def test_parse_audit_report_accepts_conclusion_alias():
    payload = _parse_audit_report_payload("""
{
  "audit_report": {
    "conclusion": "needs_repair",
    "risk": "high",
    "summary": "验收发现实现证据不完整。",
    "issues": ["缺少测试输出"],
    "next_action": "send_back_to_claude"
  }
}
""")

    assert payload["decision"] == "block"
    assert payload["risk_level"] == "high"
    assert payload["findings"] == ["缺少测试输出"]
    assert payload["recommended_next_action"] == "send_back_to_claude"


def test_parse_audit_report_derives_test_refs_from_passed_checks():
    payload = _parse_audit_report_payload("""
{
  "audit_report": {
    "verdict": "PASS",
    "risk": "LOW",
    "summary": "验收通过。",
    "findings": [],
    "passed_checks": [
      {
        "check": "测试证据",
        "result": "PASS",
        "evidence": "team_artifact=17"
      }
    ]
  }
}
""")

    assert payload["decision"] == "pass"
    assert payload["test_evidence_refs"] == ["team_artifact=17"]


def test_parse_audit_report_accepts_task_scope_pass_with_warning_from_truncated_text():
    payload = _parse_audit_report_payload(
        '''
我会按第 5 轮审计处理。
{
  "audit_report": {
    "verdict": "PASS_TASK_SCOPE_WITH_WORKTREE_WARNING",
    "risk": "LOW for README task; CRITICAL for full current worktree",
    "summary": "README 任务范围审计通过：目标说明行存在，README 专属 diff 只有 1 行。",
    "passed_checks": [
      {
        "check": "README 专属 diff",
        "result": "PASS",
        "evidence": "git diff -- README.md",
        "detail": "仅新增一行说明。"
      },
      {
        "check": "测试",
        "result": "PASS_REPORTED_NOT_RERUN",
        "evidence": "编排包 command=pytest_q",
        "detail": "结构化证据报告：all passing。"
      }
    ],
    "verification_result": {
      "commands_run_by_auditor": [
        "git diff --check README.md",
        "rg -n target README.md"
      ],
      "gitnexus_run_by_aud"
'''
    )

    assert payload["decision"] == "pass"
    assert payload["recommended_next_action"] == "close"
    assert payload["test_evidence_refs"]
    assert "pytest_q" in " ".join(payload["test_evidence_refs"])


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
    codex_implementer_enabled: bool = False,
) -> EventBridge:
    return EventBridge(
        task_service=service,
        backend=backend,
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=edit_telegram,
        approval_service=ApprovalSpy(),
        runtime_event_store=runtime_event_store,
        codex_implementer_enabled=codex_implementer_enabled,
    )


def _button_actions(buttons: object) -> set[str]:
    actions: set[str] = set()
    for row in buttons or []:
        for button in row:
            callback_data = button.get("callback_data", "")
            parts = callback_data.split(":")
            if len(parts) >= 3:
                actions.add(parts[2])
    return actions


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
        "analysis",
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
    completed = [event for event in events if event.event_type == EventType.AGENT_RUN_COMPLETED][-1]
    assert completed.payload["role"] == "analysis"
    assert find_non_terminal_agent_runs(store) == []


@pytest.mark.asyncio
async def test_auto_analysis_completion_releases_final_plan_gate(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import AUTO_COLLECTING_CONTEXT, ROLE_AUTO_ANALYSIS

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Collect auto context",
        codex_thread_id="thread-auto",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "goal")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_ANALYSIS,
        hidden_task_id=task.id,
        external_session_id="thread-auto",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(
        service,
        IdleBackend(),
        ledger,
        send_telegram=send_telegram,
        codex_implementer_enabled=True,
    )

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-auto", "turnId": "turn-auto"},
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-auto", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_COLLECTING_CONTEXT
    labels = [
        button["text"]
        for _, _, buttons in sent
        for row in (buttons or [])
        for button in row
    ]
    assert "生成最终方案" in labels


@pytest.mark.asyncio
async def test_auto_context_supplement_completion_sends_digest_without_raw_stream(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        ROLE_AUTO_CONTEXT_SUPPLEMENT,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Supplement auto context",
        codex_thread_id="thread-auto-supplement",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "goal")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_CONTEXT_SUPPLEMENT,
        hidden_task_id=task.id,
        external_session_id="thread-auto-supplement",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []
    rendered = []

    class Interaction:
        async def handle(self, event):
            rendered.append(event)

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = EventBridge(
        task_service=service,
        backend=IdleBackend(),
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=_edit_telegram,
        approval_service=ApprovalSpy(),
        interaction_renderer=Interaction(),
    )

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-auto-supplement", "turnId": "turn-auto-supplement"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-auto-supplement",
            "turnId": "turn-auto-supplement",
            "delta": (
                "结论：当前有 1 个开放仓位。\n"
                "依据：新问题 1：ALTUSDT 平仓卡住，Binance reduce-only 被拒；"
                "老问题 1：local L2 stale/rebuild 仍在大量复现。\n"
                "风险：高。\n"
            ),
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-auto-supplement", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_COLLECTING_CONTEXT
    assert "ALTUSDT 平仓卡住" in updated.last_codex_analysis
    assert rendered == []
    assert sent
    text = sent[-1][1]
    assert "Codex 已更新分析" in text
    assert "关键摘要" in text
    assert "ALTUSDT 平仓卡住" in text
    assert "local L2 stale/rebuild" in text


@pytest.mark.asyncio
async def test_auto_final_plan_completion_shows_assembled_plan_before_claude_gate(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final plan",
        codex_thread_id="thread-final-plan",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-plan",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(
        service,
        IdleBackend(),
        ledger,
        send_telegram=send_telegram,
        codex_implementer_enabled=True,
    )

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-final-plan", "turnId": "turn-final-plan"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-plan",
            "turnId": "turn-final-plan",
            "delta": "最终方案：\n1. 修改入口校验。\n",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-plan",
            "turnId": "turn-final-plan",
            "delta": "2. 跑完整回归验收。\n",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-final-plan", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_DRAFT_READY
    assert "最终方案" in updated.last_codex_analysis
    assert "修改入口校验" in updated.last_codex_analysis
    assert "完整回归验收" in updated.last_codex_analysis
    text = sent[-1][1]
    assert "最终方案" in text
    assert "修改入口校验" in text
    assert "完整回归验收" in text
    labels = [
        button["text"]
        for row in (sent[-1][2] or [])
        for button in row
    ]
    assert "交给 DeepSeek 开发工程师" in labels
    assert "交给 GPT 开发工程师" in labels
    assert "auto_send_to_codex" in _button_actions(sent[-1][2])


@pytest.mark.asyncio
async def test_auto_final_plan_completion_marks_architect_job_done_but_team_run_running(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final plan",
        codex_thread_id="thread-final-plan-team",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-plan-team",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    architect_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="architect",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    stale_architect_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="architect",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=None,
    )
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-final-plan-team", "turnId": "turn-final-plan-team"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-plan-team",
            "turnId": "turn-final-plan-team",
            "delta": "最终方案：\n1. 修改入口校验。\n",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-final-plan-team", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    jobs_by_id = {job.id: job for job in ledger.list_team_agent_jobs(team_run.id)}
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_DRAFT_READY
    assert jobs_by_id[architect_job.id].status == "done"
    assert jobs_by_id[stale_architect_job.id].status == "running"
    assert updated_team is not None
    assert updated_team.status == "running"


@pytest.mark.asyncio
async def test_auto_final_plan_completion_records_diagnosis_artifact_for_bug_route(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate diagnosis",
        codex_thread_id="thread-final-diagnosis-team",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "Telegram 验收失败")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-diagnosis-team",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="Telegram 验收失败",
        route="staged_auto",
        risk_level="medium",
    )
    investigator_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="investigator",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    ledger.record_team_artifact(
        team_run_id=team_run.id,
        agent_job_id=investigator_job.id,
        artifact_type="routing_decision",
        summary="bug route",
        payload={
            "route_kind": "bug",
            "first_role": "investigator",
            "reason": "bug_signals",
            "matched_signals": ["失败"],
        },
    )

    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-final-diagnosis-team", "turnId": "turn-final-diagnosis-team"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-diagnosis-team",
            "turnId": "turn-final-diagnosis-team",
            "delta": "诊断：Telegram 验收失败，因为审计范围包含无关 dirty files。\n",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-final-diagnosis-team", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]
    artifacts = [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "diagnosis_report"
    ]

    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_DRAFT_READY
    assert updated_job.id == investigator_job.id
    assert updated_job.status == "done"
    assert len(artifacts) == 1
    assert artifacts[0].agent_job_id == investigator_job.id
    assert "root_cause" in artifacts[0].payload


@pytest.mark.asyncio
async def test_auto_final_plan_completion_closes_existing_architect_job_when_final_plan_uses_new_agent_run(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_ANALYSIS,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final plan",
        codex_thread_id="thread-final-plan-existing-architect",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    analysis_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_ANALYSIS,
        hidden_task_id=task.id,
        external_session_id="thread-analysis-existing-architect",
    )
    final_plan_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-plan-existing-architect",
    )
    ledger.update_agent_run_status(final_plan_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    architect_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="architect",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=analysis_run.id,
    )

    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-final-plan-existing-architect",
            "turnId": "turn-final-plan-existing-architect",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-plan-existing-architect",
            "turnId": "turn-final-plan-existing-architect",
            "delta": "最终方案：\n1. 修改入口校验。\n",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-final-plan-existing-architect", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]
    artifacts = [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "architecture_plan"
    ]

    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_DRAFT_READY
    assert updated_job.id == architect_job.id
    assert updated_job.status == "done"
    assert len(artifacts) == 1
    assert artifacts[0].agent_job_id == architect_job.id


@pytest.mark.asyncio
async def test_replayed_auto_final_plan_completion_does_not_duplicate_architecture_plan(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final plan",
        codex_thread_id="thread-final-plan-replay",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-plan-replay",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    architect_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="architect",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)
    completion = BackendEvent(
        "turn_completed",
        {"threadId": "thread-final-plan-replay", "status": "completed"},
    )

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-final-plan-replay", "turnId": "turn-final-plan-replay"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-plan-replay",
            "turnId": "turn-final-plan-replay",
            "delta": "最终方案：\n1. 修改入口校验。\n",
        },
    ))
    await bridge.process_event(completion)
    await bridge.process_event(completion)

    artifacts = [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "architecture_plan"
    ]
    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]

    assert updated_job.id == architect_job.id
    assert updated_job.status == "done"
    assert len(artifacts) == 1
    assert artifacts[0].agent_job_id == architect_job.id


@pytest.mark.asyncio
async def test_auto_implementation_completion_marks_linked_implementer_job_done_only(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_DONE,
        AUTO_CLAUDE_RUNNING,
        ROLE_AUTO_IMPLEMENTATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Implement final plan",
        codex_thread_id="thread-implementation-team",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-implementation-team",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    matching_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    unrelated_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=None,
    )

    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-implementation-team", "turnId": "turn-implementation-team"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-implementation-team",
            "turnId": "turn-implementation-team",
            "delta": "Implementation complete; tests passed.",
        },
    ))
    await bridge.process_event(BackendEvent(
        "item_completed",
        {
            "threadId": "thread-implementation-team",
            "item": {
                "type": "commandExecution",
                "status": "completed",
                "command": "pytest tests/test_workflow.py -q",
            },
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-implementation-team", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    jobs_by_id = {job.id: job for job in ledger.list_team_agent_jobs(team_run.id)}
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_CLAUDE_DONE
    assert jobs_by_id[matching_job.id].status == "done"
    assert jobs_by_id[unrelated_job.id].status == "running"
    assert updated_team is not None
    assert updated_team.status == "running"


@pytest.mark.asyncio
async def test_auto_implementation_completion_creates_tester_job_before_audit(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_DONE,
        AUTO_CLAUDE_RUNNING,
        ROLE_AUTO_IMPLEMENTATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Implement final plan",
        codex_thread_id="thread-implementation-tested",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-implementation-tested",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    implementer_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="claude_deepseek",
        status="running",
        agent_run_id=agent_run.id,
    )
    ledger.add_event(task.id, "diff_updated", {"diff": "diff --git a/app.py b/app.py\n+ok"})

    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-implementation-tested", "turnId": "turn-tested"},
    ))
    await bridge.process_event(BackendEvent(
        "item_completed",
        {
            "threadId": "thread-implementation-tested",
            "item": {
                "type": "commandExecution",
                "status": "completed",
                "command": "pytest tests/test_app.py -q",
            },
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-implementation-tested", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    jobs = ledger.list_team_agent_jobs(team_run.id)
    tester_jobs = [job for job in jobs if job.role == "tester"]
    test_reports = [
        artifact
        for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "test_report"
    ]

    assert updated.current_step == AUTO_CLAUDE_DONE
    assert len(tester_jobs) == 1
    assert tester_jobs[0].status == "done"
    assert tester_jobs[0].agent_run_id == agent_run.id
    assert tester_jobs[0].model_profile == implementer_job.model_profile
    assert test_reports[0].agent_job_id == tester_jobs[0].id
    assert test_reports[0].payload["passed"] == ["pytest tests/test_app.py -q"]
    assert ledger.list_team_agent_jobs(team_run.id)[0].id == implementer_job.id


@pytest.mark.asyncio
async def test_auto_implementation_completion_with_missing_tests_blocks_audit(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_RUNNING,
        AUTO_RETRY_READY,
        ROLE_AUTO_IMPLEMENTATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Implement final plan",
        codex_thread_id="thread-implementation-no-tests",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-implementation-no-tests",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-implementation-no-tests", "turnId": "turn-no-tests"},
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-implementation-no-tests", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    tester_job = [
        job for job in ledger.list_team_agent_jobs(team_run.id) if job.role == "tester"
    ][0]

    assert updated.current_step == AUTO_RETRY_READY
    assert "测试第 1/3 次未通过" in (updated.last_verification_result or "")
    assert tester_job.status == "failed"
    assert sent
    assert "测试未通过" in sent[-1][1]
    assert "审计工程师验收" not in str(sent[-1][2])


@pytest.mark.asyncio
async def test_auto_implementation_completion_stops_internal_test_loop_at_three(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_RUNNING,
        AUTO_RETRY_READY,
        ROLE_AUTO_IMPLEMENTATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Implement final plan",
        codex_thread_id="thread-implementation-third-test-fail",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-implementation-third-test-fail",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="tester",
        model_profile="codex_gpt",
        status="failed",
        agent_run_id=None,
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="tester",
        model_profile="codex_gpt",
        status="failed",
        agent_run_id=None,
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-implementation-third-test-fail",
            "turnId": "turn-third-test-fail",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-implementation-third-test-fail",
            "status": "completed",
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    tester_jobs = [
        job for job in ledger.list_team_agent_jobs(team_run.id) if job.role == "tester"
    ]

    assert updated.current_step == AUTO_RETRY_READY
    assert len(tester_jobs) == 3
    assert "测试连续 3 次未通过" in (updated.last_verification_result or "")


@pytest.mark.asyncio
async def test_auto_implementation_failure_marks_orchestration_and_linked_job_failed(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_RUNNING,
        ROLE_AUTO_IMPLEMENTATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Implement final plan",
        codex_thread_id="thread-implementation-failure",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-implementation-failure",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="fix the workflow",
        route="staged_auto",
        risk_level="medium",
    )
    matching_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    unrelated_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=None,
    )

    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-implementation-failure",
            "turnId": "turn-implementation-failure",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-implementation-failure",
            "status": "failed",
            "error": {"message": "backend implementation boom"},
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    updated_agent = ledger.get_agent_run(agent_run.id)
    jobs_by_id = {job.id: job for job in ledger.list_team_agent_jobs(team_run.id)}

    assert updated.status == "failed"
    assert updated.current_step == AUTO_CLAUDE_RUNNING
    assert "backend implementation boom" in updated.last_claude_summary
    assert updated_agent.status == "failed"
    assert jobs_by_id[matching_job.id].status == "failed"
    assert jobs_by_id[unrelated_job.id].status == "running"
    assert updated_team is not None
    assert updated_team.status == "failed"


@pytest.mark.asyncio
async def test_auto_final_plan_prioritizes_human_readable_plan_over_diagnose_json(
    tmp_path: Path, monkeypatch
) -> None:
    """When diagnose_json is present, draft_ready must still show the
    human-readable plan as primary content. Diagnose evidence is only a
    short supplement, never the main conclusion."""
    import json as _json

    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    # Fake diagnose JSON with alarming content that must NOT dominate
    fake_diagnose = _json.dumps(
        {
            "schema_version": "2.0.0",
            "conclusion": {
                "status": "unhealthy",
                "risk": "high",
                "summary": "service inactive, exchange unavailable",
            },
            "health": {
                "fingerprints": ["service_lightfee-live_inactive"],
            },
            "service_status": {
                "lightfee-live": {"active": "inactive", "n_restarts": 3},
            },
            "exchange_truth": {
                "available": False,
                "errors": ["exchange unavailable"],
                "confidence": "low",
            },
            "local_state": {
                "lifecycle": "running",
                "risk_mode": "fail_closed",
            },
            "state_consistency": {},
            "evidence_quality": {
                "overall": "missing",
                "confidence": "low",
                "missing_evidence": ["exchange data"],
            },
        }
    )
    monkeypatch.setattr(
        "wlcodex.event_bridge._try_collect_diagnose_json_sync",
        lambda bridge, auto_run: fake_diagnose,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final plan with diagnose present",
        codex_thread_id="thread-final-diagnose-override",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(
        conversation.id,
        "最终方案出来了，下一步你生成精准修复的给 Claude 看的提示词",
    )
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-diagnose-override",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)

    plan_text = (
        "结论：下面是可直接交给 Claude 的精准修复提示词。\n"
        "Claude 任务：修复 /auto 最终方案展示层，保留人话方案摘要。\n"
        "依据：当前 /auto 最终方案阶段优先展示 diagnose_json 的"
        "结构化摘要，将人话方案和 Claude handoff 提示词盖掉了。\n"
        "风险：中 — 用户看不到人话方案，难以决策。\n"
        "下一步：交给 Claude 执行。"
    )

    await bridge.process_event(
        BackendEvent(
            "turn_started",
            {
                "threadId": "thread-final-diagnose-override",
                "turnId": "turn-final-diagnose-override",
            },
        )
    )
    await bridge.process_event(
        BackendEvent(
            "agent_message_delta",
            {
                "threadId": "thread-final-diagnose-override",
                "turnId": "turn-final-diagnose-override",
                "delta": plan_text,
            },
        )
    )
    await bridge.process_event(
        BackendEvent(
            "turn_completed",
            {
                "threadId": "thread-final-diagnose-override",
                "status": "completed",
            },
        )
    )

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_DRAFT_READY

    text = sent[-1][1]

    # Human-readable plan must be the primary content
    assert "最终方案已生成" in text
    assert "DeepSeek 开发工程师" in text
    assert "精准修复提示词" in text
    assert "展示层" in text
    assert "人话方案" in text

    # Diagnose supplement must exist but be labeled as reference-only
    assert "诊断证据" in text
    assert "仅作证据参考" in text
    assert "不替代最终方案" in text

    # The plan content must appear BEFORE the diagnose supplement section
    plan_pos = text.find("人话方案")
    supplement_pos = text.find("诊断证据")
    assert plan_pos < supplement_pos, (
        "Plan must precede diagnose supplement, but "
        f"plan_pos={plan_pos} >= supplement_pos={supplement_pos}"
    )

    # The diagnose supplement must be short (not the full structured digest)
    supplement_section = text[supplement_pos:]
    assert len(supplement_section) < 200, (
        f"Supplement too long ({len(supplement_section)} chars), "
        "full diagnose digest must not replace plan"
    )

    # The alarming diagnose content must NOT appear in the primary
    # conclusion area (before the supplement section)
    pre_supplement = text[:supplement_pos]
    assert "service_lightfee-live_inactive" not in pre_supplement
    assert "exchange unavailable" not in pre_supplement

    # Buttons must include the Claude execution gate
    labels = [
        button["text"]
        for row in (sent[-1][2] or [])
        for button in row
    ]
    assert "交给 DeepSeek 开发工程师" in labels


@pytest.mark.asyncio
async def test_auto_final_plan_completion_sends_chinese_digest_not_raw_long_plan(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate final conclusion",
        codex_thread_id="thread-final-digest",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "summarize skill research")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-final-digest",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)
    long_plan = (
        "最终结论：Codex 和 Claude 都有技能体系，但 Telegram 驾驶舱应该做发送前精炼层。\n"
        "依据：Claude Code 支持 .claude/skills；Codex 支持 .agents/skills；Telegram 单条消息有限长。\n"
        "风险：如果继续直接展示原文，用户无法快速判断是否可以交给 Claude。\n"
        "下一步：新增中文摘要卡，全文保留在草稿里。\n\n"
        "以下是冗长背景：\n"
        + "\n".join(
            f"背景段落 {i}: 这里模拟模型输出的大量解释、操作步骤、mkdir -p ~/.claude/skills/summarize、SKILL.md 内容。"
            for i in range(120)
        )
    )

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-final-digest", "turnId": "turn-final-digest"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-final-digest",
            "turnId": "turn-final-digest",
            "delta": long_plan,
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-final-digest", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.current_step == AUTO_DRAFT_READY
    assert "背景段落 10" in updated.last_codex_analysis

    text = sent[-1][1]
    assert len(text) < 900
    assert "结论：" in text
    assert "依据：" in text
    assert "风险：" in text
    assert "下一步：" in text
    assert "背景段落 10" not in text
    assert "mkdir -p" not in text
    assert "请选择下一步" in text


@pytest.mark.asyncio
async def test_auto_claude_done_sends_chinese_digest_not_raw_long_summary(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_DONE,
        AUTO_DRAFT_READY,
        ROLE_AUTO_IMPLEMENTATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run Claude implementation",
        codex_thread_id="thread-claude-digest",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "implement digest")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_DRAFT_READY,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "claude",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-claude-digest",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)
    long_summary = (
        "结论：Claude 已实现 Telegram 驾驶舱摘要层。\n"
        "依据：新增摘要 helper；事件桥发送短卡片；相关测试已覆盖。\n"
        "风险：需要继续跑完整回归。\n"
        "下一步：让 Codex 验收。\n\n"
        + "\n".join(f"执行日志 {i}: 这里是冗长实现细节和终端输出。" for i in range(120))
    )

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-claude-digest", "turnId": "turn-claude-digest"},
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-claude-digest",
            "turnId": "turn-claude-digest",
            "delta": long_summary,
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-claude-digest", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.current_step == AUTO_CLAUDE_DONE

    text = sent[-1][1]
    assert len(text) < 900
    assert "结论：" in text
    assert "依据：" in text
    assert "风险：" in text
    assert "下一步：" in text
    assert "执行日志 20" not in text
    labels = [
        button["text"]
        for row in (sent[-1][2] or [])
        for button in row
    ]
    assert "审计工程师验收" in labels


@pytest.mark.asyncio
async def test_auto_codex_implementation_done_advances_to_claude_done(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_CLAUDE_DONE,
        AUTO_CLAUDE_RUNNING,
        ROLE_AUTO_IMPLEMENTATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run Codex implementation",
        codex_thread_id="thread-codex-implementation",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "implement with codex")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
        last_codex_analysis="方案：让 Codex 执行实现。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_IMPLEMENTATION,
        hidden_task_id=task.id,
        external_session_id="thread-codex-implementation",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-codex-implementation",
            "turnId": "turn-codex-implementation",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-codex-implementation",
            "turnId": "turn-codex-implementation",
            "delta": "结论：Codex 已完成实现。\n下一步：请验收。",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-codex-implementation", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_CLAUDE_DONE
    assert "Codex 已完成实现" in updated.last_claude_summary
    assert "审计工程师验收" in [
        button["text"]
        for row in (sent[-1][2] or [])
        for button in row
    ]
    assert "开发完成，测试通过" in sent[-1][1]
    assert "Claude 执行完成" not in sent[-1][1]


@pytest.mark.asyncio
async def test_auto_verification_failure_marks_orchestration_and_linked_job_failed(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_VERIFYING,
        ROLE_AUTO_VERIFICATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run Codex verification",
        codex_thread_id="thread-verification-failure",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "verify with codex")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_codex_analysis="方案：让 Codex 验收。",
        last_claude_summary="实现完成。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=task.id,
        external_session_id="thread-verification-failure",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="verify with codex",
        route="staged_auto",
        risk_level="medium",
    )
    matching_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="auditor",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    unrelated_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="auditor",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=None,
    )

    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-verification-failure",
            "turnId": "turn-verification-failure",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-verification-failure",
            "status": "failed",
            "error": {"message": "backend verification boom"},
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    updated_agent = ledger.get_agent_run(agent_run.id)
    jobs_by_id = {job.id: job for job in ledger.list_team_agent_jobs(team_run.id)}

    assert updated.status == "failed"
    assert updated.current_step == AUTO_VERIFYING
    assert "backend verification boom" in updated.last_verification_result
    assert updated_agent.status == "failed"
    assert jobs_by_id[matching_job.id].status == "failed"
    assert jobs_by_id[unrelated_job.id].status == "running"
    assert updated_team is not None
    assert updated_team.status == "failed"


@pytest.mark.asyncio
async def test_auto_verification_pass_without_test_evidence_blocks_team_completion(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_RETRY_READY,
        AUTO_VERIFYING,
        ROLE_AUTO_VERIFICATION,
    )
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
        "Run Codex verification",
        codex_thread_id="thread-verification-pass-team",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "verify with codex")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_codex_analysis="方案：让 Codex 验收。",
        last_claude_summary="实现完成。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=task.id,
        external_session_id="thread-verification-pass-team",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="verify with codex",
        route="staged_auto",
        risk_level="medium",
    )
    auditor_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="auditor",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    bridge = _bridge(
        service,
        IdleBackend(),
        ledger,
        runtime_event_store=store,
    )

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-verification-pass-team",
            "turnId": "turn-verification-pass-team",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-verification-pass-team",
            "turnId": "turn-verification-pass-team",
            "delta": "decision: pass\nsummary: Verification passed.",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-verification-pass-team",
            "status": "completed",
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    updated_job = ledger.list_team_agent_jobs(team_run.id)[0]
    event_types = [
        event.event_type
        for event in store.list_by_orchestration_run(orch_run.id, limit=100)
    ]

    assert updated.current_step == AUTO_RETRY_READY
    assert updated_team is not None
    assert updated_team.status == "running"
    assert updated_job.id == auditor_job.id
    assert updated_job.status == "done"
    assert EventType.TEAM_RUN_COMPLETED not in event_types
    assert EventType.TEAM_GATE_FAILED in event_types
    assert EventType.TEAM_AGENT_JOB_COMPLETED in event_types
    assert EventType.TEAM_ARTIFACT_RECORDED in event_types


@pytest.mark.asyncio
async def test_auto_verification_pass_without_team_run_preserves_legacy_completion(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COMPLETED,
        AUTO_VERIFYING,
        ROLE_AUTO_VERIFICATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run Codex verification",
        codex_thread_id="thread-verification-legacy-pass",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "legacy verify")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_codex_analysis="方案：legacy staged auto。",
        last_claude_summary="实现完成。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=task.id,
        external_session_id="thread-verification-legacy-pass",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-verification-legacy-pass",
            "turnId": "turn-verification-legacy-pass",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-verification-legacy-pass",
            "turnId": "turn-verification-legacy-pass",
            "delta": "decision: pass\nsummary: Legacy verification passed.",
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-verification-legacy-pass",
            "status": "completed",
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)

    assert updated.current_step == AUTO_COMPLETED
    assert updated.last_verification_result.startswith("decision: pass")


@pytest.mark.asyncio
async def test_auto_verification_pass_without_current_auditor_job_blocks_completion(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_RETRY_READY,
        AUTO_VERIFYING,
        ROLE_AUTO_VERIFICATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run Codex verification",
        codex_thread_id="thread-verification-no-auditor",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "verify with codex")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_codex_analysis="方案：让 Codex 验收。",
        last_claude_summary="实现完成。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=task.id,
        external_session_id="thread-verification-no-auditor",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="verify with codex",
        route="staged_auto",
        risk_level="medium",
    )
    ledger.record_team_artifact(
        team_run_id=team_run.id,
        agent_job_id=None,
        artifact_type="test_report",
        summary="Focused tests passed.",
        payload={
            "summary": "Focused tests passed.",
            "commands_run": [
                {
                    "command": "pytest tests/test_event_bridge.py -q",
                    "exit_status": 0,
                    "summary": "event bridge tests passed",
                }
            ],
            "passed": ["Focused tests"],
            "failed": ["None"],
            "coverage_of_acceptance_criteria": [
                {
                    "criterion": "Focused verification passes",
                    "status": "covered",
                    "evidence": "pytest tests/test_event_bridge.py -q",
                }
            ],
            "failure_evidence": ["None"],
        },
    )
    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-verification-no-auditor",
            "turnId": "turn-verification-no-auditor",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-verification-no-auditor",
            "turnId": "turn-verification-no-auditor",
            "delta": (
                "decision: pass\n"
                "summary: Verification passed.\n"
                "test_evidence_refs:\n"
                "- team_artifact=1"
            ),
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-verification-no-auditor",
            "status": "completed",
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)

    assert updated.current_step == AUTO_RETRY_READY
    assert updated_team is not None
    assert updated_team.status == "running"
    assert not [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "audit_report"
    ]


@pytest.mark.asyncio
async def test_auto_completed_message_includes_final_synthesis_from_team_artifacts(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COMPLETED,
        AUTO_VERIFYING,
        ROLE_AUTO_VERIFICATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run Codex verification",
        codex_thread_id="thread-verification-final-synthesis",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "verify with codex")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_codex_analysis="方案：让 Codex 验收。",
        last_claude_summary="实现完成。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=task.id,
        external_session_id="thread-verification-final-synthesis",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="verify with codex",
        route="staged_auto",
        risk_level="medium",
    )
    implementer_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="done",
        agent_run_id=None,
    )
    impl = ledger.record_team_artifact(
        team_run_id=team_run.id,
        agent_job_id=implementer_job.id,
        artifact_type="implementation_report",
        summary="Implementation complete.",
        payload={
            "summary": "Implementation complete.",
            "changed_files": ["tracked.txt"],
            "diff_summary": "tracked.txt changed.",
            "commands_run": [
                {
                    "command": "pytest tests/test_event_bridge.py -q",
                    "exit_status": 0,
                    "summary": "event bridge tests passed",
                }
            ],
            "tests_attempted": [
                {
                    "command": "pytest tests/test_event_bridge.py -q",
                    "exit_status": 0,
                    "summary": "event bridge tests passed",
                }
            ],
            "known_limitations": ["None known"],
        },
    )
    test_report = ledger.record_team_artifact(
        team_run_id=team_run.id,
        agent_job_id=implementer_job.id,
        artifact_type="test_report",
        summary="Focused tests passed.",
        payload={
            "summary": "Focused tests passed.",
            "commands_run": [
                {
                    "command": "pytest tests/test_event_bridge.py -q",
                    "exit_status": 0,
                    "summary": "event bridge tests passed",
                }
            ],
            "passed": ["pytest tests/test_event_bridge.py -q"],
            "failed": ["None"],
            "coverage_of_acceptance_criteria": [
                {
                    "criterion": "Focused verification passes",
                    "status": "covered",
                    "evidence": f"team_artifact={impl.id}",
                }
            ],
            "failure_evidence": ["None"],
        },
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="auditor",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    sent: list[str] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append(text)
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-verification-final-synthesis",
            "turnId": "turn-verification-final-synthesis",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-verification-final-synthesis",
            "turnId": "turn-verification-final-synthesis",
            "delta": (
                "decision: pass\n"
                "summary: Reviewed focused pytest evidence.\n"
                "findings:\n"
                "- No blocking findings.\n"
                "missing_evidence:\n"
                "- None\n"
                "test_evidence_refs:\n"
                f"- team_artifact={test_report.id}"
            ),
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-verification-final-synthesis",
            "status": "completed",
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)

    assert updated.current_step == AUTO_COMPLETED
    assert sent
    assert sent[-1] != "验收通过，任务完成。"
    assert "最终综合" in sent[-1]
    assert "tracked.txt" in sent[-1]
    assert "pytest tests/test_event_bridge.py -q" in sent[-1]
    assert "pass" in sent[-1]


@pytest.mark.asyncio
async def test_auto_verification_json_verdict_pass_completes_team_run(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COMPLETED,
        AUTO_VERIFYING,
        ROLE_AUTO_VERIFICATION,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Run Codex verification",
        codex_thread_id="thread-verification-json-pass",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "verify with codex")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_VERIFYING,
        last_codex_analysis="方案：让 Codex 验收。",
        last_claude_summary="实现完成。",
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_VERIFICATION,
        hidden_task_id=task.id,
        external_session_id="thread-verification-json-pass",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    team_run = ledger.create_team_run(
        conversation_id=conversation.id,
        orchestration_run_id=orch_run.id,
        goal="verify with codex",
        route="staged_auto",
        risk_level="medium",
    )
    implementer_job = ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="implementer",
        model_profile="codex_gpt",
        status="done",
        agent_run_id=None,
    )
    test_report = ledger.record_team_artifact(
        team_run_id=team_run.id,
        agent_job_id=implementer_job.id,
        artifact_type="test_report",
        summary="Focused tests passed.",
        payload={
            "summary": "Focused tests passed.",
            "commands_run": [
                {
                    "command": "pytest tests/test_event_bridge.py -q",
                    "exit_status": 0,
                    "summary": "event bridge tests passed",
                }
            ],
            "passed": ["pytest tests/test_event_bridge.py -q"],
            "failed": ["None"],
            "coverage_of_acceptance_criteria": [
                {
                    "criterion": "Focused verification passes",
                    "status": "covered",
                    "evidence": "pytest tests/test_event_bridge.py -q",
                }
            ],
            "failure_evidence": ["None"],
        },
    )
    ledger.create_team_agent_job(
        team_run_id=team_run.id,
        role="auditor",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=agent_run.id,
    )
    bridge = _bridge(service, IdleBackend(), ledger)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {
            "threadId": "thread-verification-json-pass",
            "turnId": "turn-verification-json-pass",
        },
    ))
    await bridge.process_event(BackendEvent(
        "agent_message_delta",
        {
            "threadId": "thread-verification-json-pass",
            "turnId": "turn-verification-json-pass",
            "delta": (
                '{\n'
                '  "audit_report": {\n'
                '    "verdict": "PASS",\n'
                '    "risk": "LOW",\n'
                '    "summary": "Reviewed focused pytest evidence.",\n'
                '    "findings": [],\n'
                '    "passed_checks": [\n'
                '      {"check": "测试证据", "result": "PASS", '
                f'"evidence": "team_artifact={test_report.id}"}}\n'
                '    ]\n'
                '  }\n'
                '}'
            ),
        },
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {
            "threadId": "thread-verification-json-pass",
            "status": "completed",
        },
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    updated_team = ledger.get_team_run(team_run.id)
    audit_report = [
        artifact for artifact in ledger.list_team_artifacts(team_run.id)
        if artifact.artifact_type == "audit_report"
    ][0]

    assert updated.current_step == AUTO_COMPLETED
    assert updated_team is not None
    assert updated_team.status == "completed"
    assert audit_report.payload["decision"] == "pass"
    assert audit_report.payload["test_evidence_refs"] == [
        f"team_artifact={test_report.id}"
    ]


@pytest.mark.asyncio
async def test_auto_final_plan_completion_without_body_hides_claude_gate(
    tmp_path: Path,
) -> None:
    from wlcodex.auto_workflow import (
        AUTO_COLLECTING_CONTEXT,
        AUTO_DRAFT_READY,
        ROLE_AUTO_FINAL_PLAN,
    )

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    task = service.start_task(
        "demo",
        "Generate empty final plan",
        codex_thread_id="thread-empty-plan",
        telegram_chat_id=123,
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    ledger.set_conversation_active_task(conversation.id, task.id)
    orch_run = ledger.create_orchestration_run(conversation.id, "fix the workflow")
    ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_COLLECTING_CONTEXT,
    )
    agent_run = ledger.create_agent_run(
        conversation.id,
        "codex",
        ROLE_AUTO_FINAL_PLAN,
        hidden_task_id=task.id,
        external_session_id="thread-empty-plan",
    )
    ledger.update_agent_run_status(agent_run.id, "running")
    sent: list[tuple[int, str, object]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text, buttons))
        return 1

    bridge = _bridge(service, IdleBackend(), ledger, send_telegram=send_telegram)

    await bridge.process_event(BackendEvent(
        "turn_started",
        {"threadId": "thread-empty-plan", "turnId": "turn-empty-plan"},
    ))
    await bridge.process_event(BackendEvent(
        "turn_completed",
        {"threadId": "thread-empty-plan", "status": "completed"},
    ))

    updated = ledger.get_orchestration_run(orch_run.id)
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_DRAFT_READY
    assert updated.last_codex_analysis == ""
    assert "没有收到方案正文" in sent[-1][1]
    labels = [
        button["text"]
        for row in (sent[-1][2] or [])
        for button in row
    ]
    assert "交给 DeepSeek 开发工程师" not in labels
    assert "继续补充" in labels
    assert "重写方案" not in labels


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
