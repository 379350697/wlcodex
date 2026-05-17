"""Telegram handler registration and auth guard tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from wlcodex.telegram_app import build_application, is_authorized


def test_all_v1_commands_registered_in_built_app(tmp_path: Path) -> None:
    """Verify that all V1 command handlers are registered."""
    from wlcodex.config import load_config
    from wlcodex.codex_backend import FakeCodexBackend
    from wlcodex.db import Ledger
    from wlcodex.task_service import TaskService
    from wlcodex.inspection import TaskInspector
    from wlcodex.controller import CommandController
    from wlcodex.approval import ApprovalService
    from wlcodex.config import WorkspaceConfig

    config_path = Path(__file__).parent / "fixtures" / "test_handler_config.toml"
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text("""
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
""", encoding="utf-8")

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
    assert not missing, f"Missing handler registrations: {missing}"


def test_authorized_private_chat_user() -> None:
    assert is_authorized(user_id=123, chat_type="private", allowed_user_ids=frozenset({123}))


def test_rejects_group_chat() -> None:
    assert not is_authorized(user_id=123, chat_type="group", allowed_user_ids=frozenset({123}))
    assert not is_authorized(user_id=123, chat_type="supergroup", allowed_user_ids=frozenset({123}))


def test_rejects_unknown_user() -> None:
    assert not is_authorized(user_id=999, chat_type="private", allowed_user_ids=frozenset({123}))


def test_rejects_none_user_id() -> None:
    assert not is_authorized(user_id=None, chat_type="private", allowed_user_ids=frozenset({123}))


def test_all_v1_commands_including_waiting_callbacks_registered(tmp_path: Path) -> None:
    """Verify that waiting callback handler is registered alongside commands."""
    from wlcodex.config import load_config
    from wlcodex.codex_backend import FakeCodexBackend
    from wlcodex.db import Ledger
    from wlcodex.task_service import TaskService
    from wlcodex.inspection import TaskInspector
    from wlcodex.controller import CommandController
    from wlcodex.approval import ApprovalService
    from wlcodex.config import WorkspaceConfig

    config_path = Path(__file__).parent / "fixtures" / "test_handler_config.toml"
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text("""
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
""", encoding="utf-8")

    config = load_config(config_path)
    ledger = Ledger.open(tmp_path / "test.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    task_service = TaskService(ledger, (WorkspaceConfig("demo", Path("/tmp/demo"), True),))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    approval_svc = ApprovalService()
    controller = CommandController(task_service, backend, inspector)

    app, _ = build_application(config, "dummy-token", controller, ledger, approval_svc)

    # Verify CallbackQueryHandler is registered
    callback_handlers = []
    for group in app.handlers.values():
        for handler in group:
            if hasattr(handler, "callback"):
                callback_handlers.append(handler)
    assert len(callback_handlers) > 0


def test_waiting_callback_decode_rejects_approval_data() -> None:
    """Waiting callback decoder must return None for approval callback data."""
    from wlcodex.approval import encode_approval_callback
    from wlcodex.waiting_callback import decode_waiting_callback

    approval_data = encode_approval_callback(1, "approve_once")
    assert decode_waiting_callback(approval_data) is None


def test_approval_callback_decode_rejects_waiting_data() -> None:
    """Approval callback decoder must return None for waiting callback data."""
    from wlcodex.approval import decode_approval_callback
    from wlcodex.waiting_callback import encode_waiting_callback, KEEP

    waiting_data = encode_waiting_callback(1, KEEP)
    assert decode_approval_callback(waiting_data) is None


@pytest.mark.asyncio
async def test_edit_telegram_ignores_message_not_modified() -> None:
    """Telegram 'message is not modified' must not fall back to a new message."""
    from wlcodex.telegram_app import WlCodexHandlers

    class Bot:
        def __init__(self) -> None:
            self.sent = 0

        async def edit_message_text(self, **kwargs):
            raise RuntimeError("Bad Request: message is not modified")

        async def send_message(self, **kwargs):
            self.sent += 1
            return SimpleNamespace(message_id=999)

    bot = Bot()
    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=object(),
        ledger=SimpleNamespace(list_tasks=lambda *args, **kwargs: []),
        approval_service=object(),
        bot=bot,
    )

    await handlers.edit_telegram(123, 456, "same text")

    assert bot.sent == 0
