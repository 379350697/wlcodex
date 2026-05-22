from datetime import datetime, timezone
from dataclasses import replace

from wlcodex.models import Task, TaskStatus
from wlcodex.legacy_task_status import render_task_card, render_task_list


def _task(task_id: int, status: TaskStatus, title: str) -> Task:
    now = datetime(2026, 5, 16, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        workspace_alias="demo",
        workspace_path="/tmp/demo",
        title=title,
        status=status,
        codex_thread_id=f"thread-{task_id}",
        active_turn_id=None,
        parent_task_id=None,
        telegram_chat_id=None,
        telegram_status_message_id=None,
        created_at=now,
        updated_at=now,
        last_summary="short summary",
        last_phase="running tests",
        last_error="",
        changed_file_count=0,
        pending_approval_count=0,
        token_input=0,
        token_output=0,
    )


def test_render_task_card_is_compact() -> None:
    text = render_task_card(_task(42, TaskStatus.RUNNING, "Fix health timeout"))

    assert "任务 #42" in text
    assert "运行中" in text
    assert "running tests" in text
    assert "short summary" in text
    assert len(text) < 600


def test_render_task_card_hides_internal_thread_and_turn_ids() -> None:
    task = replace(
        _task(42, TaskStatus.RUNNING, "Fix health timeout"),
        codex_thread_id="thread-secret",
        active_turn_id="turn-secret",
    )

    text = render_task_card(task)

    assert "thread-secret" not in text
    assert "turn-secret" not in text
    assert "线程" not in text
    assert "turn" not in text.lower()


def test_render_task_list_limits_noise() -> None:
    text = render_task_list([_task(42, TaskStatus.RUNNING, "Fix health timeout")])

    assert "#42" in text
    assert "Fix health timeout" in text
    assert "thread-42" not in text


def test_render_task_card_shows_worktree_info() -> None:
    task = _task(1, TaskStatus.RUNNING, "Worktree task")
    task = Task(
        id=task.id,
        workspace_alias=task.workspace_alias,
        workspace_path=task.workspace_path,
        title=task.title,
        status=task.status,
        codex_thread_id=task.codex_thread_id,
        active_turn_id=task.active_turn_id,
        parent_task_id=task.parent_task_id,
        telegram_chat_id=task.telegram_chat_id,
        telegram_status_message_id=task.telegram_status_message_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_summary=task.last_summary,
        last_phase=task.last_phase,
        last_error=task.last_error,
        changed_file_count=0,
        pending_approval_count=0,
        token_input=0,
        token_output=0,
        worktree_path="/tmp/wt/task-1",
        worktree_branch="wlcodex/task-1-test",
    )

    text = render_task_card(task)
    assert "隔离 worktree：/tmp/wt/task-1" in text
    assert "Worktree 分支：wlcodex/task-1-test" in text


def test_render_task_card_shows_force_parallel_warning() -> None:
    task = _task(1, TaskStatus.RUNNING, "Force task")
    task = Task(
        id=task.id,
        workspace_alias=task.workspace_alias,
        workspace_path=task.workspace_path,
        title=task.title,
        status=task.status,
        codex_thread_id=task.codex_thread_id,
        active_turn_id=task.active_turn_id,
        parent_task_id=task.parent_task_id,
        telegram_chat_id=task.telegram_chat_id,
        telegram_status_message_id=task.telegram_status_message_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_summary=task.last_summary,
        last_phase=task.last_phase,
        last_error=task.last_error,
        changed_file_count=0,
        pending_approval_count=0,
        token_input=0,
        token_output=0,
        is_force_parallel=True,
    )

    text = render_task_card(task)
    assert "同目录强制并行" in text


def test_render_task_list_shows_worktree_marker() -> None:
    task = _task(1, TaskStatus.RUNNING, "Worktree task")
    task = Task(
        id=task.id,
        workspace_alias=task.workspace_alias,
        workspace_path=task.workspace_path,
        title=task.title,
        status=task.status,
        codex_thread_id=task.codex_thread_id,
        active_turn_id=task.active_turn_id,
        parent_task_id=task.parent_task_id,
        telegram_chat_id=task.telegram_chat_id,
        telegram_status_message_id=task.telegram_status_message_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_summary=task.last_summary,
        last_phase=task.last_phase,
        last_error=task.last_error,
        changed_file_count=0,
        pending_approval_count=0,
        token_input=0,
        token_output=0,
        worktree_path="/tmp/wt/task-1",
        worktree_branch="wlcodex/task-1-test",
    )

    text = render_task_list([task])
    assert "WT:wlcodex/task-1-test" in text


def test_render_task_list_shows_force_parallel_marker() -> None:
    task = _task(1, TaskStatus.RUNNING, "Force task")
    task = Task(
        id=task.id,
        workspace_alias=task.workspace_alias,
        workspace_path=task.workspace_path,
        title=task.title,
        status=task.status,
        codex_thread_id=task.codex_thread_id,
        active_turn_id=task.active_turn_id,
        parent_task_id=task.parent_task_id,
        telegram_chat_id=task.telegram_chat_id,
        telegram_status_message_id=task.telegram_status_message_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_summary=task.last_summary,
        last_phase=task.last_phase,
        last_error=task.last_error,
        changed_file_count=0,
        pending_approval_count=0,
        token_input=0,
        token_output=0,
        is_force_parallel=True,
    )

    text = render_task_list([task])
    assert "⚠️并行" in text


def test_render_task_card_waiting_slot_shows_blocker_and_position() -> None:
    task = _task(2, TaskStatus.WAITING_SLOT, "Waiting task")
    text = render_task_card(task, blocker_id=1, blocker_status="运行中", queue_position=1)
    assert "阻塞者：#1（运行中）" in text
    assert "队列位置：第 1 位" in text


def test_render_conversation_help_is_compact_for_natural_profile() -> None:
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="natural")

    assert "直接发消息" in text
    assert "/task" not in text
    assert len(text.splitlines()) <= 14


# ═══════════════════════════════════════════════════════════════
# BLOCKER A: /status must NOT leak internal IDs
# ═══════════════════════════════════════════════════════════════


def test_format_status_display_leaks_internal_ids():
    """format_status_display exposes banned terms — PROVES the problem.

    This test guards against accidentally re-introducing the diagnostic
    formatter into the normal /status path.  If format_status_display
    stops leaking these terms, the /status path was fixed — verify
    that was intentional.
    """
    from wlcodex.runtime_diagnostics import (
        RuntimeAgentSummary,
        RuntimeStatus,
        format_status_display,
    )

    status = RuntimeStatus(
        conversation_id=42,
        active_agent="claude",
        active_agent_run_id=15,
        phase="implementation",
        status="running",
        last_event_type="agent.run.started",
        last_event_id=1234,
        total_events=456,
        agents=[
            RuntimeAgentSummary(
                agent_run_id=12, agent="codex", status="completed",
            ),
            RuntimeAgentSummary(
                agent_run_id=15, agent="claude", status="running",
            ),
        ],
    )

    output = format_status_display(status)
    assert "#15" in output or "运行 #" in output, (
        "format_status_display must leak agent_run_id. If this assertion "
        "fails, the diagnostic formatter was cleaned — verify intentional."
    )
    assert "#1234" in output or "事件总数" in output, (
        "format_status_display must leak event_id/event count."
    )


def test_status_command_must_not_use_format_status_display():
    """/status handler with runtime_store routes through render_conversation_status.

    Even when runtime_store is available, StatusCommand (/status, a
    primary menu entry) must NOT call format_status_display.
    The diagnostic dump is reserved for explicit /trace.
    """
    from unittest.mock import MagicMock, patch
    from types import SimpleNamespace
    from wlcodex.controller import CommandController

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(
            id=42, chat_id=7001, user_id=100,
            title="test", mode="chief_engineer",
            workspace_alias="wlcodex",
            conversation_summary="testing user flow",
            active_codex_task_id=None,
            active_claude_run_id=None,
            current_model="claude-sonnet-4-6",
        )
    )
    ledger.list_agent_runs = MagicMock(return_value=[])
    ledger.list_orchestration_runs = MagicMock(return_value=[])

    store = MagicMock()
    store.list_by_conversation = MagicMock(return_value=[])

    ctrl = CommandController.__new__(CommandController)
    ctrl._ledger = ledger
    ctrl._service = MagicMock()
    ctrl._backend = MagicMock()
    ctrl._orchestration_runner = MagicMock()
    ctrl._store = store
    ctrl._claude = None
    ctrl._default_workspace = "wlcodex"
    ctrl._default_mode = "chief_engineer"
    ctrl._background_tasks = set()
    ctrl._emit_event = MagicMock()
    ctrl._new_correlation_id = MagicMock(return_value="cid-1")
    ctrl._interaction_renderer = None
    ctrl._inspector = MagicMock()

    with patch(
        "wlcodex.runtime_diagnostics.format_status_display"
    ) as diag_fmt:
        diag_fmt.return_value = "诊断 #15"
        import asyncio
        response = asyncio.run(
            ctrl.handle("/status", {"chat_id": 7001, "user_id": 100})
        )

    assert not diag_fmt.called, (
        "After fix: StatusCommand MUST NOT call format_status_display. "
        "The clean formatter (render_conversation_status) is used instead. "
        "Diagnostic dump is reserved for /trace."
    )
    assert "#" not in response.text, (
        f"/status output must not contain internal IDs. Got: {response.text[:200]}"
    )


def test_render_workbench_history_marks_active_and_archived() -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from wlcodex.status import render_workbench_history

    now = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    sessions = [
        SimpleNamespace(
            id=2,
            title="Current",
            mode="chief_engineer",
            workspace_alias="wlcodex",
            archived_at=None,
            updated_at=now,
        ),
        SimpleNamespace(
            id=1,
            title="Old",
            mode="codex_direct",
            workspace_alias="lightfee",
            archived_at=now,
            updated_at=now,
        ),
    ]

    text = render_workbench_history(sessions)

    assert "工作台历史" in text
    assert "Current" in text and "当前" in text
    assert "Old" in text
    assert "#2" not in text
    assert "#1" not in text
    assert "lightfee" not in text
    assert "总工程师" not in text


def test_render_workspace_list_marks_active_workspace() -> None:
    from wlcodex.config import WorkspaceConfig
    from wlcodex.status import render_workspace_list
    from pathlib import Path

    workspaces = [
        WorkspaceConfig("wlcodex", Path("/repo/wlcodex"), True),
        WorkspaceConfig("lightfee", Path("/repo/LightFee"), True),
    ]

    text = render_workspace_list(workspaces, active_alias="lightfee")

    assert "选择工作区" in text
    assert "当前工作区：lightfee" in text
    assert "wlcodex" not in text
    assert "/repo" not in text


def test_render_workspace_list_without_active_workspace_stays_compact() -> None:
    from wlcodex.config import WorkspaceConfig
    from wlcodex.status import render_workspace_list
    from pathlib import Path

    workspaces = [
        WorkspaceConfig("wlcodex", Path("/repo/wlcodex"), True),
        WorkspaceConfig("lightfee", Path("/repo/LightFee"), True),
    ]

    text = render_workspace_list(workspaces)

    assert text == "选择工作区"
    assert "wlcodex" not in text
    assert "lightfee" not in text


# ═══════════════════════════════════════════════════════════════
# Surface mode in /status
# ═══════════════════════════════════════════════════════════════


def test_render_conversation_status_shows_surface_mode_product() -> None:
    from types import SimpleNamespace
    from wlcodex.status import render_conversation_status

    session = SimpleNamespace(
        id=1, title="测试对话", mode="chief_engineer",
        workspace_alias="wlcodex", conversation_summary="测试",
    )
    text = render_conversation_status(session, surface_mode="product")

    assert "当前视图：驾驶舱" in text
    assert "模式：总工程师" in text


def test_render_conversation_status_shows_surface_mode_terminal() -> None:
    from types import SimpleNamespace
    from wlcodex.status import render_conversation_status

    session = SimpleNamespace(
        id=1, title="测试对话", mode="chief_engineer",
        workspace_alias="wlcodex", conversation_summary="测试",
    )
    text = render_conversation_status(session, surface_mode="terminal")

    assert "当前视图：现场" in text
    assert "模式：总工程师" in text


def test_render_conversation_status_defaults_to_product() -> None:
    from types import SimpleNamespace
    from wlcodex.status import render_conversation_status

    session = SimpleNamespace(
        id=1, title="测试对话", mode="codex_direct",
        workspace_alias="wlcodex", conversation_summary="",
    )
    text = render_conversation_status(session)

    assert "当前视图：驾驶舱" in text
    assert "模式：Codex 直聊" in text


def test_render_conversation_status_shows_terminal_agent() -> None:
    from types import SimpleNamespace
    from wlcodex.status import render_conversation_status

    session = SimpleNamespace(
        id=1, title="测试对话", mode="chief_engineer",
        workspace_alias="wlcodex", conversation_summary="测试",
    )
    text = render_conversation_status(
        session, surface_mode="terminal", terminal_agent="claude")

    assert "当前视图：现场" in text
    assert "现场 Agent：Claude" in text


def test_render_conversation_status_unknown_surface_mode_falls_back() -> None:
    from types import SimpleNamespace
    from wlcodex.status import render_conversation_status

    session = SimpleNamespace(
        id=1, title="测试对话", mode="claude_direct",
        workspace_alias="wlcodex", conversation_summary="",
    )
    text = render_conversation_status(session, surface_mode="unknown")

    assert "当前视图：驾驶舱" in text
    assert "模式：Claude 直聊" in text
