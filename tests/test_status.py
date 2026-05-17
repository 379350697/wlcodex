from datetime import datetime, timezone

from wlcodex.models import Task, TaskStatus
from wlcodex.status import render_task_card, render_task_list


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
    assert len(text.splitlines()) <= 8
