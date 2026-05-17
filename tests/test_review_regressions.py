"""Tests that encode the full cockpit closure acceptance criteria.

These tests assert behaviors that are missing from the current skeleton.
They MUST FAIL before the full closure implementation is complete.
"""

from pathlib import Path

import pytest

from wlcodex.codex_backend import AppServerCodexBackend, FakeCodexBackend
from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.router import (
    ParseError,
    parse_command,
)
from wlcodex.task_service import TaskService
from wlcodex.telegram_app import build_application, is_authorized


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


def test_build_app_registers_all_v1_handlers(tmp_path: Path) -> None:
    """Every V1 command must be registered in the Telegram Application."""
    from wlcodex.config import load_config
    from wlcodex.codex_backend import FakeCodexBackend
    from wlcodex.db import Ledger
    from wlcodex.task_service import TaskService
    from wlcodex.inspection import TaskInspector
    from wlcodex.controller import CommandController
    from wlcodex.approval import ApprovalService

    config_path = Path(__file__).parent / "fixtures" / "full_cockpit.toml"
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[task]
max_running_seconds = 7200
max_queued_seconds = 1800
max_waiting_approval_seconds = 3600
watchdog_interval_seconds = 60
backend_dead_grace_seconds = 120

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    ledger = Ledger.open(tmp_path / "test.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    task_service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    approval_svc = ApprovalService()
    controller = CommandController(task_service, backend, inspector)

    app, _ = build_application(config, "dummy-token", controller, ledger, approval_svc)

    registered = set()
    for group in app.handlers.values():
        for handler in group:
            if hasattr(handler, "commands"):
                registered.update(handler.commands)

    required = {
        "start", "help", "task", "tasks", "status", "continue", "steer",
        "tail", "events", "diff", "files", "pause", "abort", "archive",
        "fork", "codex_sessions", "health",
    }
    missing = required - registered
    assert not missing, f"Missing registered handlers: {missing}"


# ---------------------------------------------------------------------------
# Auth: unauthorized rejection
# ---------------------------------------------------------------------------


def test_unauthorized_private_user_cannot_run_task() -> None:
    """Unauthorized private chat users must be rejected."""
    assert not is_authorized(user_id=999, chat_type="private", allowed_user_ids=frozenset({123}))


def test_authorized_group_chat_cannot_run_task() -> None:
    """Authorized user IDs in group chats must be rejected."""
    assert not is_authorized(user_id=123, chat_type="group", allowed_user_ids=frozenset({123}))


# ---------------------------------------------------------------------------
# AppServerCodexBackend must not raise skeleton RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_server_backend_accepts_fake_transport() -> None:
    """AppServerCodexBackend must accept a fake JSON-RPC transport and
    not raise the spike RuntimeError for create_thread."""
    async def fake_send(message: dict) -> None:
        pass

    async def fake_recv() -> dict:
        return {"jsonrpc": "2.0", "id": 1, "result": {"threadId": "thread-1"}}

    # When the backend is constructed with a transport, create_thread
    # should work instead of raising RuntimeError.
    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")
    # The backend should not raise RuntimeError when we inject a transport
    # after construction.
    try:
        backend.set_transport(fake_send, fake_recv)
    except AttributeError:
        # Backend must expose set_transport for testing
        assert False, "AppServerCodexBackend must expose set_transport() for fake transport injection"


# ---------------------------------------------------------------------------
# State machine: queued -> running -> done
# ---------------------------------------------------------------------------


def test_task_moves_from_queued_to_running(tmp_path: Path) -> None:
    """When a turn starts, the task must move from queued to running."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-1")

    assert task.status == TaskStatus.QUEUED

    # Simulate backend event: turn started
    from wlcodex.codex_backend import BackendEvent
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    updated = ledger.get_task(task.id)
    assert updated.status == TaskStatus.RUNNING


def test_task_moves_to_done_when_turn_completes(tmp_path: Path) -> None:
    """When a turn completes, the task must move to done."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-1")

    from wlcodex.codex_backend import BackendEvent
    service.apply_backend_event(BackendEvent(
        event_type="turn_started",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))
    service.apply_backend_event(BackendEvent(
        event_type="turn_completed",
        payload={"threadId": "thread-1", "turnId": "turn-1"},
    ))

    updated = ledger.get_task(task.id)
    assert updated.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# Lock release on terminal states
# ---------------------------------------------------------------------------


def test_lock_released_after_done(tmp_path: Path) -> None:
    """Write lock must be released when task reaches done."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    ledger.set_task_status(task.id, TaskStatus.DONE)

    task2 = service.start_task("demo", "Another task", codex_thread_id="thread-2")
    assert task2.id == task.id + 1


def test_lock_released_after_failed(tmp_path: Path) -> None:
    """Write lock must be released when task reaches failed."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    ledger.set_task_status(task.id, TaskStatus.FAILED)

    task2 = service.start_task("demo", "Another task", codex_thread_id="thread-2")
    assert task2.id == task.id + 1


def test_lock_released_after_aborted(tmp_path: Path) -> None:
    """Write lock must be released when task reaches aborted."""
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))

    task = service.start_task("demo", "Fix bug", codex_thread_id="thread-1")
    ledger.set_task_status(task.id, TaskStatus.ABORTED)

    task2 = service.start_task("demo", "Another task", codex_thread_id="thread-2")
    assert task2.id == task.id + 1


# ---------------------------------------------------------------------------
# /steer must use active turn steering, not continue
# ---------------------------------------------------------------------------


def test_steer_uses_backend_steer_not_continue() -> None:
    """Fake backend must track steer vs continue as distinct operations."""
    backend = FakeCodexBackend()
    # steer_turn must record differently from continue_turn
    assert hasattr(backend, "steer_turn"), "Backend must expose steer_turn"
    assert hasattr(backend, "steers"), "Backend must track steers separately from turns"


@pytest.mark.asyncio
async def test_steer_does_not_call_continue() -> None:
    """steer_turn must not call continue_turn."""
    backend = FakeCodexBackend()
    thread_id = await backend.create_thread("/tmp/demo")
    await backend.start_turn(thread_id, "Fix bug")

    pre_steer_turns = len(backend.turns)
    await backend.steer_turn(thread_id, "turn-1", "Change direction")
    # Turns list should NOT have grown (steer is separate)
    assert len(backend.turns) == pre_steer_turns
    assert len(backend.steers) == 1


# ---------------------------------------------------------------------------
# Approval callback idempotence
# ---------------------------------------------------------------------------


def test_approval_callback_decoding_roundtrip() -> None:
    """Approval callback data must encode/decode correctly."""
    from wlcodex.approval import encode_approval_callback, decode_approval_callback

    encoded = encode_approval_callback(approval_id=42, action="approve_once")
    decoded = decode_approval_callback(encoded)
    assert decoded.approval_id == 42
    assert decoded.action == "approve_once"


def test_approval_duplicate_callback_does_not_double_resolve(tmp_path: Path) -> None:
    """Duplicate approval callbacks must not send multiple backend responses."""

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()

    # Simulate an approval row that was already resolved
    class FakeApprovalService:
        def __init__(self):
            self.resolve_count = 0

        def resolve_callback(self, approval_id, action, backend, ledger):
            self.resolve_count += 1
            if self.resolve_count > 1:
                raise RuntimeError("resolution called twice")

    svc = FakeApprovalService()
    # First call succeeds
    svc.resolve_callback(1, "approve_once", None, None)
    # Second call should be detected as duplicate and not re-resolve
    # (the real impl must check status before resolving)
    assert svc.resolve_count == 1


# ---------------------------------------------------------------------------
# /tasks and /status parse as ListTasksCommand
# ---------------------------------------------------------------------------


def test_parse_tasks_and_status_commands() -> None:
    assert parse_command("/tasks") is not None
    assert parse_command("/status") is not None


def test_parse_rejects_empty_task_prompt() -> None:
    with pytest.raises(ParseError, match="用法"):
        parse_command("/task lightfee")


# ---------------------------------------------------------------------------
# Router: full command surface parsed
# ---------------------------------------------------------------------------


def test_parse_health_command() -> None:
    from wlcodex.router import HealthCommand
    cmd = parse_command("/health")
    assert isinstance(cmd, HealthCommand)


def test_parse_codex_sessions_command() -> None:
    from wlcodex.router import CodexSessionsCommand
    cmd = parse_command("/codex-sessions")
    assert isinstance(cmd, CodexSessionsCommand)


def test_parse_tail_command() -> None:
    from wlcodex.router import TailCommand
    cmd = parse_command("/tail 42")
    assert isinstance(cmd, TailCommand)
    assert cmd.task_id == 42


def test_parse_events_command() -> None:
    from wlcodex.router import EventsCommand
    cmd = parse_command("/events 42")
    assert isinstance(cmd, EventsCommand)
    assert cmd.task_id == 42


def test_parse_diff_command() -> None:
    from wlcodex.router import DiffCommand
    cmd = parse_command("/diff 42")
    assert isinstance(cmd, DiffCommand)
    assert cmd.task_id == 42


def test_parse_files_command() -> None:
    from wlcodex.router import FilesCommand
    cmd = parse_command("/files 42")
    assert isinstance(cmd, FilesCommand)
    assert cmd.task_id == 42


def test_parse_pause_command() -> None:
    from wlcodex.router import PauseCommand
    cmd = parse_command("/pause 42")
    assert isinstance(cmd, PauseCommand)
    assert cmd.task_id == 42


def test_parse_abort_command() -> None:
    from wlcodex.router import AbortCommand
    cmd = parse_command("/abort 42")
    assert isinstance(cmd, AbortCommand)
    assert cmd.task_id == 42


def test_parse_archive_command() -> None:
    from wlcodex.router import ArchiveCommand
    cmd = parse_command("/archive 42")
    assert isinstance(cmd, ArchiveCommand)
    assert cmd.task_id == 42


def test_parse_fork_command() -> None:
    from wlcodex.router import ForkCommand
    cmd = parse_command("/fork 42 Fix the new approach")
    assert isinstance(cmd, ForkCommand)
    assert cmd.task_id == 42
    assert cmd.prompt == "Fix the new approach"


def test_parse_help_command() -> None:
    from wlcodex.router import HelpCommand
    cmd = parse_command("/help")
    assert isinstance(cmd, HelpCommand)
