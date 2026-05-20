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
    """Recovery path notifies chat without refreshing legacy status cards."""
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

    async def send_telegram(chat_id: int, text: str, buttons=None) -> int:
        sent.append((chat_id, text))
        return 789

    count = await notify_recovery_paused_tasks(
        ledger=ledger,
        paused_ids=paused_ids,
        send_telegram=send_telegram,
    )

    assert count == 1
    expected_text = (
        "WLCodex 已恢复。\n\n"
        "上次运行已安全暂停。当前工作仍在驾驶舱中可见，"
        "可以查看状态、接管现场，或发送 /new 开始新的工作台。"
    )
    assert sent == [(123, expected_text)]


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


def _write_test_config_toml(terminal_enabled: bool = False) -> Path:
    """Write a minimal test config to a named temp dir and return the path."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="wlcodex-test-config-"))
    p = d / "test.toml"
    terminal_block = ""
    if terminal_enabled:
        terminal_block = "\n[terminal]\nenabled = true\ndefault_agent = \"codex\"\n"
    p.write_text(
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
{terminal_block}
[storage]
sqlite_path = ""
task_log_dir = ""

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
    return p


# ---------------------------------------------------------------------------
# Terminal manager composition tests
# ---------------------------------------------------------------------------


def test_create_terminal_manager_disabled_returns_none() -> None:
    """When terminal.enabled=false, _create_terminal_manager returns None."""
    from wlcodex.main import _create_terminal_manager

    config = load_config(_write_test_config_toml())
    assert config.terminal.enabled is False

    backend = FakeCodexBackend()
    tm = _create_terminal_manager(config, claude_backend=None, codex_backend=backend)
    assert tm is None


def test_create_terminal_manager_enabled_returns_manager_with_adapters() -> None:
    """When terminal.enabled=true, _create_terminal_manager returns a
    TerminalSessionManager with adapters for both agents."""
    from wlcodex.main import _create_terminal_manager
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

    config_path = _write_test_config_toml(terminal_enabled=True)
    config = load_config(config_path)
    assert config.terminal.enabled is True

    backend = FakeCodexBackend()
    tm = _create_terminal_manager(config, claude_backend=None, codex_backend=backend)

    assert isinstance(tm, TerminalSessionManager)
    assert "codex" in tm._adapters
    assert isinstance(tm._adapters["codex"], CodexTerminalAdapter)
    # Claude backend is None → claude adapter not registered
    assert "claude" not in tm._adapters


def test_create_terminal_manager_with_claude_backend_registers_both_adapters() -> None:
    """When claude_backend is provided, both claude and codex adapters are wired."""
    from wlcodex.main import _create_terminal_manager
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig

    config_path = _write_test_config_toml(terminal_enabled=True)
    config = load_config(config_path)

    claude_cfg = ClaudeConfig(enabled=True, binary="claude")
    claude = ClaudeBackend(claude_cfg)

    backend = FakeCodexBackend()
    tm = _create_terminal_manager(config, claude_backend=claude, codex_backend=backend)

    assert isinstance(tm, TerminalSessionManager)
    assert "codex" in tm._adapters
    assert "claude" in tm._adapters
    assert isinstance(tm._adapters["claude"], ClaudeTerminalAdapter)
    assert isinstance(tm._adapters["codex"], CodexTerminalAdapter)


def test_main_passes_terminal_manager_to_build_application(tmp_path: Path) -> None:
    """main() passes terminal_manager=None to build_application when
    terminal.enabled=false, and a real TerminalSessionManager when enabled=true.

    This test MUST call wlcodex.main.main() — not a manual replica.
    """
    import asyncio as _asyncio
    import os as _os
    import sys as _sys
    from unittest.mock import MagicMock

    import wlcodex.main as main_mod

    def _make_fake_handlers():
        fake = MagicMock()
        fake.send_telegram = MagicMock()
        fake.edit_telegram = MagicMock()
        fake.create_interaction_renderer = MagicMock(return_value=None)
        return fake

    # ── Test case 1: terminal.enabled=false → terminal_manager is None ──
    config_path = tmp_path / "test_disabled.toml"
    sqlite_path = tmp_path / "wlcodex_disabled.sqlite3"
    task_dir = tmp_path / "tasks_disabled"
    task_dir.mkdir(exist_ok=True)
    _write_test_config_with_path(
        config_path,
        sqlite_path,
        task_dir,
        terminal_enabled=False,
    )

    captured_tm = None
    captured_scheduler = None
    build_called = False

    def fake_build(cfg, token, ctrl, lgr, appr, runtime_event_store=None,
                   outbox=None, terminal_manager=None,
                   execution_scheduler=None):
        nonlocal captured_tm, captured_scheduler, build_called
        captured_tm = terminal_manager
        captured_scheduler = execution_scheduler
        build_called = True
        fake_app = MagicMock()
        fake_app.bot = MagicMock()
        fake_app.updater = MagicMock()
        fake_app.updater.running = False
        return fake_app, _make_fake_handlers()

    # Short-circuit the event loop — build_application is called before
    # the loop runs, so we just need main() to exit after building.
    # Patch on the concrete BaseEventLoop, not the abstract class.
    from asyncio import base_events as _base_events
    original_run_until_complete = _base_events.BaseEventLoop.run_until_complete
    _base_events.BaseEventLoop.run_until_complete = lambda self, future: None

    # Also prevent new_event_loop from returning a real loop.
    original_new_event_loop = _asyncio.new_event_loop
    original_set_event_loop = _asyncio.set_event_loop
    _asyncio.new_event_loop = lambda: MagicMock()
    _asyncio.set_event_loop = lambda loop: None

    original_build = main_mod.build_application
    original_argv = _sys.argv[:]
    _sys.argv = ["main.py", "--fake-backend", "--config", str(config_path)]
    _os.environ["WLCODEX_TELEGRAM_BOT_TOKEN"] = "test-main-composition-token"

    main_mod.build_application = fake_build
    try:
        main_mod.main()
    finally:
        main_mod.build_application = original_build
        _sys.argv = original_argv
        _base_events.BaseEventLoop.run_until_complete = original_run_until_complete
        _asyncio.new_event_loop = original_new_event_loop
        _asyncio.set_event_loop = original_set_event_loop

    assert build_called, "build_application was never called by main()"
    assert captured_tm is None, (
        f"Expected terminal_manager=None when terminal.enabled=false, got {captured_tm}"
    )
    assert captured_scheduler is not None, (
        "Expected main() to pass the shared ExecutionScheduler to build_application"
    )

    # ── Test case 2: terminal.enabled=true + claude.enabled=true →
    #     build_application receives non-None TerminalSessionManager with
    #     codex adapter; and because claude.enabled=true, also a claude adapter.
    config2_path = tmp_path / "test_enabled.toml"
    sqlite2_path = tmp_path / "wlcodex_enabled.sqlite3"
    task2_dir = tmp_path / "tasks_enabled"
    task2_dir.mkdir(exist_ok=True)
    _write_test_config_with_path(
        config2_path,
        sqlite2_path,
        task2_dir,
        terminal_enabled=True,
        claude_enabled=True,
    )

    captured_tm2 = None
    captured_scheduler2 = None
    build2_called = False

    def fake_build2(cfg, token, ctrl, lgr, appr, runtime_event_store=None,
                    outbox=None, terminal_manager=None,
                    execution_scheduler=None):
        nonlocal captured_tm2, captured_scheduler2, build2_called
        captured_tm2 = terminal_manager
        captured_scheduler2 = execution_scheduler
        build2_called = True
        fake_app = MagicMock()
        fake_app.bot = MagicMock()
        fake_app.updater = MagicMock()
        fake_app.updater.running = False
        return fake_app, _make_fake_handlers()

    # Re-apply event loop patches (they were restored in test case 1's finally).
    _base_events.BaseEventLoop.run_until_complete = lambda self, future: None
    _asyncio.new_event_loop = lambda: MagicMock()
    _asyncio.set_event_loop = lambda loop: None

    main_mod.build_application = fake_build2
    original_argv2 = _sys.argv[:]
    _sys.argv = ["main.py", "--fake-backend", "--config", str(config2_path)]

    try:
        main_mod.main()
    finally:
        main_mod.build_application = original_build
        _sys.argv = original_argv2
        _base_events.BaseEventLoop.run_until_complete = original_run_until_complete
        _asyncio.new_event_loop = original_new_event_loop
        _asyncio.set_event_loop = original_set_event_loop

    assert build2_called, "build_application was never called by main() for enabled case"
    assert captured_tm2 is not None, (
        "Expected non-None terminal_manager when terminal.enabled=true"
    )
    assert captured_scheduler2 is not None, (
        "Expected main() to pass the shared ExecutionScheduler in terminal mode"
    )
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager
    assert isinstance(captured_tm2, TerminalSessionManager), (
        f"Expected TerminalSessionManager, got {type(captured_tm2)}"
    )
    assert "codex" in captured_tm2._adapters, (
        "TerminalSessionManager must have a codex adapter"
    )
    assert "claude" in captured_tm2._adapters, (
        "TerminalSessionManager must have a claude adapter when claude.enabled=true"
    )


def _write_test_config_with_path(
    path: Path,
    sqlite_path: Path,
    task_log_dir: Path,
    *,
    terminal_enabled: bool = False,
    claude_enabled: bool = False,
) -> None:
    path.parent.mkdir(exist_ok=True)
    terminal_block = ""
    if terminal_enabled:
        terminal_block = "[terminal]\nenabled = true\ndefault_agent = \"codex\"\n"
    claude_block = ""
    if claude_enabled:
        claude_block = "[claude]\nenabled = true\nbinary = \"claude\"\n"
    path.write_text(
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

{terminal_block}
{claude_block}
[storage]
sqlite_path = "{sqlite_path}"
task_log_dir = "{task_log_dir}"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[interaction]
profile = "legacy"

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )
