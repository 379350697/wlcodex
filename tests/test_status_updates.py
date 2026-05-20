"""Status update and rendering tests."""

from datetime import datetime, timezone

from wlcodex.models import Task, TaskStatus
from wlcodex.legacy_task_status import (
    render_task_card,
    render_task_list,
)
from wlcodex.status import (
    render_help,
    render_health_card,
    render_approval_card,
)
from wlcodex.app_server_process import BackendHealth


def _task(task_id: int, status: TaskStatus, title: str, **kwargs) -> Task:
    now = datetime(2026, 5, 16, tzinfo=timezone.utc)
    defaults = dict(
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
        last_summary="",
        last_phase="",
        last_error="",
        changed_file_count=0,
        pending_approval_count=0,
        token_input=0,
        token_output=0,
    )
    defaults.update(kwargs)
    return Task(**defaults)


def test_task_card_shows_status_and_phase() -> None:
    task = _task(42, TaskStatus.RUNNING, "Fix health timeout",
                 last_phase="executing tests", last_summary="Running pytest")
    text = render_task_card(task)
    assert "任务 #42" in text
    assert "运行中" in text
    assert "executing tests" in text
    assert "Running pytest" in text
    assert len(text) < 800


def test_task_card_shows_approvals() -> None:
    task = _task(42, TaskStatus.WAITING_APPROVAL, "Dangerous op",
                 pending_approval_count=2)
    text = render_task_card(task)
    assert "待审批：2" in text


def test_task_card_shows_tokens() -> None:
    task = _task(42, TaskStatus.DONE, "Done task",
                 token_input=1500, token_output=800)
    text = render_task_card(task)
    assert "1500" in text
    assert "800" in text


def test_task_list_with_various_states() -> None:
    tasks = [
        _task(1, TaskStatus.RUNNING, "Active task"),
        _task(2, TaskStatus.DONE, "Finished task"),
        _task(3, TaskStatus.FAILED, "Failed task"),
    ]
    text = render_task_list(tasks)
    assert "#1" in text
    assert "#2" in text
    assert "#3" in text


def test_task_list_empty() -> None:
    text = render_task_list([])
    assert "legacy diagnostic task" in text
    assert "创建新任务" not in text


def test_help_is_not_empty() -> None:
    text = render_help()
    assert len(text) > 100
    assert "新工作台" in text
    assert "/task" not in text
    assert "/continue" not in text
    assert "/steer" not in text


def test_health_card_shows_status() -> None:
    healthy = BackendHealth(process_alive=True, websocket_connected=True)
    card = render_health_card(healthy)
    assert "后端健康" in card


def test_approval_card_compact() -> None:
    card = render_approval_card(42, 1, "command", "Run rm -rf /")
    assert "审批 #1" in card
    assert "任务 #" not in card
    assert "命令" in card
    assert "请使用下面按钮" in card
    assert len(card) < 500
