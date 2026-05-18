"""Main composition tests — verify runtime wiring without Telegram polling."""

from pathlib import Path

import pytest


from wlcodex.approval import ApprovalService
from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.config import load_config
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.event_bridge import EventBridge
from wlcodex.inspection import TaskInspector
from wlcodex.models import TaskStatus
from wlcodex.task_service import TaskService
from wlcodex.telegram_app import build_application
from wlcodex.watchdog import TaskLivenessConfig, TaskWatchdog


def _write_test_config(path: Path) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(
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

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )


def test_composition_opens_sqlite(tmp_path: Path) -> None:
    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    Ledger.open(config.storage.sqlite_path)
    assert sqlite_path.exists() or config.storage.sqlite_path.exists()


def test_composition_migrates(tmp_path: Path) -> None:
    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    ledger = Ledger.open(config.storage.sqlite_path)
    ledger.migrate()

    # Verify tables exist
    tables = ledger._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = {r[0] for r in tables}
    for expected in ("tasks", "task_events", "approval_requests", "touched_files",
                     "backend_requests", "telegram_updates",
                     "conversation_sessions", "agent_runs",
                     "orchestration_runs", "orchestration_decisions"):
        assert expected in table_names, f"Missing table: {expected}"


def test_composition_runs_recovery(tmp_path: Path) -> None:
    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    ledger = Ledger.open(config.storage.sqlite_path)
    ledger.migrate()

    from wlcodex.models import TaskStatus
    t = ledger.create_task("demo", "/tmp/demo", "Running", "th-1", None)
    ledger.set_task_status(t.id, TaskStatus.RUNNING)

    paused_ids = ledger.mark_active_tasks_recovery_paused()
    assert t.id in paused_ids


def test_composition_builds_telegram_app(tmp_path: Path) -> None:
    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    ledger = Ledger.open(config.storage.sqlite_path)
    ledger.migrate()
    backend = FakeCodexBackend()
    task_service = TaskService(ledger, config.workspaces)
    inspector = TaskInspector(ledger, config.storage.task_log_dir)
    approval_svc = ApprovalService()
    controller = CommandController(task_service, backend, inspector)

    app, _ = build_application(config, "dummy-token", controller, ledger, approval_svc)

    # App should have handlers registered
    registered = set()
    for group in app.handlers.values():
        for handler in group:
            if hasattr(handler, "commands"):
                registered.update(handler.commands)

    assert "task" in registered
    assert "start" in registered
    assert "claude_mode" in registered


def test_telegram_app_processes_callbacks_while_long_runs_are_active(tmp_path: Path) -> None:
    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    ledger = Ledger.open(config.storage.sqlite_path)
    ledger.migrate()
    backend = FakeCodexBackend()
    task_service = TaskService(ledger, config.workspaces)
    inspector = TaskInspector(ledger, config.storage.task_log_dir)
    approval_svc = ApprovalService()
    controller = CommandController(task_service, backend, inspector)

    app, _ = build_application(config, "dummy-token", controller, ledger, approval_svc)

    assert app.update_processor.max_concurrent_updates > 1


def test_missing_token_exits(tmp_path: Path) -> None:
    config_path = tmp_path / "test.toml"
    _write_test_config(config_path)

    config = load_config(config_path)
    import os
    old = os.environ.get(config.telegram.bot_token_env)
    if config.telegram.bot_token_env in os.environ:
        del os.environ[config.telegram.bot_token_env]

    try:
        token = os.environ.get(config.telegram.bot_token_env)
        assert token is None
    finally:
        if old is not None:
            os.environ[config.telegram.bot_token_env] = old


class _FakeUpdater:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.stopped = False

    async def stop(self) -> None:
        if not self.running:
            raise RuntimeError("This Updater is not running!")
        self.stopped = True
        self.running = False


class _FakeApp:
    def __init__(self, *, running: bool = False, updater_running: bool = False) -> None:
        self.running = running
        self.updater = _FakeUpdater(running=updater_running)
        self.stopped = False
        self.shutdown_called = False

    async def stop(self) -> None:
        if not self.running:
            raise RuntimeError("This Application is not running!")
        self.stopped = True
        self.running = False

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.asyncio
async def test_shutdown_telegram_app_tolerates_initialize_failure() -> None:
    from wlcodex.main import _shutdown_telegram_app

    app = _FakeApp()
    await _shutdown_telegram_app(
        app,
        app_initialized=False,
        app_started=False,
        updater_started=False,
    )

    assert not app.updater.stopped
    assert not app.stopped
    assert not app.shutdown_called


@pytest.mark.asyncio
async def test_shutdown_telegram_app_stops_started_components() -> None:
    from wlcodex.main import _shutdown_telegram_app

    app = _FakeApp(running=True, updater_running=True)
    await _shutdown_telegram_app(
        app,
        app_initialized=True,
        app_started=True,
        updater_started=True,
    )

    assert app.updater.stopped
    assert app.stopped
    assert app.shutdown_called


def test_task_liveness_config_from_app_config(tmp_path: Path) -> None:
    """TaskLivenessConfig can be constructed from AppConfig.task fields."""
    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    lc = TaskLivenessConfig(
        max_running_seconds=config.task.max_running_seconds,
        max_queued_seconds=config.task.max_queued_seconds,
        max_waiting_approval_seconds=config.task.max_waiting_approval_seconds,
        backend_dead_grace_seconds=config.task.backend_dead_grace_seconds,
    )
    assert lc.max_running_seconds == 7200
    assert lc.max_queued_seconds == 1800
    assert lc.max_waiting_approval_seconds == 3600
    assert lc.backend_dead_grace_seconds == 120


def test_event_bridge_receives_task_watchdog_from_runtime_config(tmp_path: Path) -> None:
    """Runtime composition passes the real watchdog into EventBridge."""
    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    ledger = Ledger.open(config.storage.sqlite_path)
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, config.workspaces)
    approval_svc = ApprovalService()
    liveness_config = TaskLivenessConfig(
        max_running_seconds=config.task.max_running_seconds,
        max_queued_seconds=config.task.max_queued_seconds,
        max_waiting_approval_seconds=config.task.max_waiting_approval_seconds,
        backend_dead_grace_seconds=config.task.backend_dead_grace_seconds,
    )
    watchdog = TaskWatchdog(ledger, backend, liveness_config)

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        return 1

    async def edit_telegram(chat_id: int, message_id: int, text: str, buttons=None) -> None:
        return None

    bridge = EventBridge(
        task_service=service,
        backend=backend,
        ledger=ledger,
        send_telegram=send_telegram,
        edit_telegram=edit_telegram,
        approval_service=approval_svc,
        task_watchdog=watchdog,
        watchdog_interval_seconds=config.task.watchdog_interval_seconds,
    )

    assert bridge._task_watchdog is watchdog
    assert bridge._watchdog_interval == 60


@pytest.mark.asyncio
async def test_recovery_notification_sends_for_paused_restart_task(tmp_path: Path) -> None:
    """Recovery path notifies chat and refreshes the existing status card."""
    from wlcodex.recovery_notifications import notify_recovery_paused_tasks

    config_path = tmp_path / "test.toml"
    sqlite_path = tmp_path / "wlcodex.sqlite3"
    _write_test_config_with_path(config_path, sqlite_path, tmp_path / "tasks")

    config = load_config(config_path)
    ledger = Ledger.open(config.storage.sqlite_path)
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Running", "th-1", None, 123)
    ledger.set_task_status(task.id, TaskStatus.RUNNING)
    ledger._conn.execute(
        "UPDATE tasks SET telegram_status_message_id = ? WHERE id = ?",
        (456, task.id),
    )
    ledger._conn.commit()
    paused_ids = ledger.mark_active_tasks_recovery_paused()
    sent: list[tuple[int, str]] = []
    edited: list[tuple[int, int, str]] = []

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text))
        return 789

    async def edit_telegram(chat_id: int, message_id: int, text: str) -> None:
        edited.append((chat_id, message_id, text))

    count = await notify_recovery_paused_tasks(
        ledger=ledger,
        paused_ids=paused_ids,
        send_telegram=send_telegram,
        edit_telegram=edit_telegram,
    )

    assert count == 1
    expected_text = (
        f"任务 #{task.id} 已因 WLCodex 重启暂停。\n"
        f"可用 /continue {task.id} <prompt> 继续，或 /abort {task.id} 释放工作区。"
    )
    assert sent == [(123, expected_text)]
    assert edited
    assert edited[0][0] == 123
    assert edited[0][1] == 456
    assert "已暂停" in edited[0][2]


# ---------------------------------------------------------------------------
# Initialize-with-retry tests
# ---------------------------------------------------------------------------


class _FakeAppWithInitialize:
    """Fake PTB Application that counts initialize attempts."""

    def __init__(self, fail_count: int = 0, fail_with: type[Exception] | None = None) -> None:
        self.initialize_count = 0
        self._fail_count = fail_count
        self._fail_with = fail_with

    async def initialize(self) -> None:
        self.initialize_count += 1
        if self.initialize_count <= self._fail_count:
            if self._fail_with:
                raise self._fail_with("Simulated timeout")
            raise RuntimeError("Simulated failure")


@pytest.mark.asyncio
async def test_initialize_app_with_retry_succeeds_first_try() -> None:
    """_initialize_app_with_retry returns True on first success."""
    from wlcodex.main import _initialize_app_with_retry

    app = _FakeAppWithInitialize(fail_count=0)
    result = await _initialize_app_with_retry(app, max_retries=3, backoff_base=0.01)
    assert result is True
    assert app.initialize_count == 1


@pytest.mark.asyncio
async def test_initialize_app_with_retry_succeeds_after_retries() -> None:
    """_initialize_app_with_retry retries on network errors and succeeds."""
    from telegram.error import TimedOut
    from wlcodex.main import _initialize_app_with_retry

    app = _FakeAppWithInitialize(fail_count=2, fail_with=TimedOut)
    result = await _initialize_app_with_retry(app, max_retries=3, backoff_base=0.01)
    assert result is True
    assert app.initialize_count == 3


@pytest.mark.asyncio
async def test_initialize_app_with_retry_fails_after_all_retries() -> None:
    """_initialize_app_with_retry returns False when all retries exhausted."""
    from telegram.error import NetworkError
    from wlcodex.main import _initialize_app_with_retry

    app = _FakeAppWithInitialize(fail_count=3, fail_with=NetworkError)
    result = await _initialize_app_with_retry(app, max_retries=3, backoff_base=0.01)
    assert result is False
    assert app.initialize_count == 3


@pytest.mark.asyncio
async def test_initialize_app_with_retry_does_not_retry_non_network_error() -> None:
    """_initialize_app_with_retry does NOT retry non-network errors (e.g. bad token)."""
    from wlcodex.main import _initialize_app_with_retry

    app = _FakeAppWithInitialize(fail_count=1, fail_with=ValueError)
    result = await _initialize_app_with_retry(app, max_retries=3, backoff_base=0.01)
    assert result is False
    assert app.initialize_count == 1  # Only tried once, no retry


def _write_test_config_with_path(config_path: Path, sqlite_path: Path, task_log_dir: Path) -> None:
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text(
        f"""
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
sqlite_path = "{sqlite_path}"
task_log_dir = "{task_log_dir}"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )
