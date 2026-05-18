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


# ---------------------------------------------------------------------------
# Network resilience: send_telegram / edit_telegram
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_telegram_returns_send_failed_on_timeout() -> None:
    """send_telegram must return SEND_FAILED on TimedOut, not crash."""
    from telegram.error import TimedOut
    from wlcodex.telegram_app import WlCodexHandlers, SEND_FAILED

    class Bot:
        async def send_message(self, **kwargs):
            raise TimedOut("Connection timed out")

    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=object(),
        ledger=SimpleNamespace(),
        approval_service=object(),
        bot=Bot(),
    )
    result = await handlers.send_telegram(123, "test message")
    assert result == SEND_FAILED


@pytest.mark.asyncio
async def test_send_telegram_returns_send_failed_on_network_error() -> None:
    """send_telegram must return SEND_FAILED on NetworkError."""
    from telegram.error import NetworkError
    from wlcodex.telegram_app import WlCodexHandlers, SEND_FAILED

    class Bot:
        async def send_message(self, **kwargs):
            raise NetworkError("Network unavailable")

    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=object(),
        ledger=SimpleNamespace(),
        approval_service=object(),
        bot=Bot(),
    )
    result = await handlers.send_telegram(123, "test")
    assert result == SEND_FAILED


@pytest.mark.asyncio
async def test_send_telegram_reraises_non_network_telegram_error() -> None:
    """send_telegram must re-raise non-network TelegramError (e.g. 403 Forbidden)."""
    from telegram.error import Forbidden
    from wlcodex.telegram_app import WlCodexHandlers

    class Bot:
        async def send_message(self, **kwargs):
            raise Forbidden("Bot was blocked by the user")

    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=object(),
        ledger=SimpleNamespace(),
        approval_service=object(),
        bot=Bot(),
    )
    with pytest.raises(Forbidden):
        await handlers.send_telegram(123, "test")


@pytest.mark.asyncio
async def test_edit_telegram_survives_network_timeout() -> None:
    """edit_telegram must log and return on network timeout, not crash."""
    from telegram.error import TimedOut
    from wlcodex.telegram_app import WlCodexHandlers

    class Bot:
        def __init__(self) -> None:
            self.sent = 0

        async def edit_message_text(self, **kwargs):
            raise TimedOut("Edit timed out")

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
    # Must not raise
    await handlers.edit_telegram(123, 456, "test edit")
    # Network timeout should NOT fall back to sending a new message
    assert bot.sent == 0


# ---------------------------------------------------------------------------
# ACK-before-orchestration: auto_cmd sends ACK first
# ---------------------------------------------------------------------------


class _FakeControllerForAutoAck:
    """Controller that records call order for ACK-first testing."""

    def __init__(self) -> None:
        self.handle_calls: list[str] = []

    async def handle(self, text: str, ctx) -> object:
        self.handle_calls.append(text)
        from wlcodex.controller import ControllerResponse
        return ControllerResponse("编排完成结果")


class _FakeUpdateForAutoAck:
    effective_user = SimpleNamespace(id=123)
    effective_chat = SimpleNamespace(id=456, type="private")
    effective_message = SimpleNamespace(text="/auto fix bug")
    update_id = 1


@pytest.mark.asyncio
async def test_auto_cmd_sends_ack_before_controller_call() -> None:
    """auto_cmd must send an ACK message BEFORE calling controller.handle()."""
    from wlcodex.telegram_app import WlCodexHandlers, SEND_FAILED

    send_order: list[str] = []
    sent_messages: list[str] = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            send_order.append("send:" + text[:20])
            sent_messages.append(text)
            return SimpleNamespace(message_id=len(sent_messages))

        async def send_chat_action(self, chat_id, action, **kwargs):
            pass

        async def edit_message_text(self, chat_id, message_id, text, **kwargs):
            send_order.append("edit:" + text[:20])

    bot = Bot()
    controller = _FakeControllerForAutoAck()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=controller,
        ledger=SimpleNamespace(
            list_tasks=lambda *a, **kw: [],
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=bot,
    )

    update = _FakeUpdateForAutoAck()
    await handlers.auto_cmd(update, SimpleNamespace())

    # The first send must be the ACK
    assert len(sent_messages) >= 1
    assert "稍候" in sent_messages[0] or "分析" in sent_messages[0]
    # Controller must have been called (after ACK send)
    assert len(controller.handle_calls) == 1


@pytest.mark.asyncio
async def test_auto_cmd_survives_ack_send_failure() -> None:
    """auto_cmd must still run orchestration even when ACK send fails."""
    from telegram.error import TimedOut
    from wlcodex.telegram_app import WlCodexHandlers, SEND_FAILED

    controller = _FakeControllerForAutoAck()

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            raise TimedOut("ACK send timed out")

        async def send_chat_action(self, chat_id, action, **kwargs):
            pass

        async def edit_message_text(self, chat_id, message_id, text, **kwargs):
            pass

    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=controller,
        ledger=SimpleNamespace(
            list_tasks=lambda *a, **kw: [],
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=Bot(),
    )

    update = _FakeUpdateForAutoAck()
    # Must not raise — handler survives ACK failure
    await handlers.auto_cmd(update, SimpleNamespace())
    # Controller must still have been called
    assert len(controller.handle_calls) == 1


@pytest.mark.asyncio
async def test_claude_cmd_survives_streaming_renderer_failure() -> None:
    """claude_cmd must not crash when streaming renderer fails with network error."""
    from telegram.error import TimedOut
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    sent_count = 0

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            nonlocal sent_count
            sent_count += 1
            return SimpleNamespace(message_id=sent_count)

        async def send_chat_action(self, chat_id, action, **kwargs):
            pass

        async def edit_message_text(self, chat_id, message_id, text, **kwargs):
            raise TimedOut("Edit timed out during streaming")

    class FakeCtrlWithButtons:
        def __init__(self) -> None:
            self.handle_calls: list[str] = []

        async def handle(self, text: str, ctx) -> ControllerResponse:
            self.handle_calls.append(text)
            return ControllerResponse(
                "Claude 已完成。",
                buttons=[[{"text": "查看 diff", "callback_data": "conv:1:diff"}]],
            )

    controller = FakeCtrlWithButtons()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=controller,
        ledger=SimpleNamespace(
            list_tasks=lambda *a, **kw: [],
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=Bot(),
    )

    update = _FakeUpdateForAutoAck()
    update.effective_message = SimpleNamespace(text="/claude fix auth")
    # Must not raise
    await handlers.claude_cmd(update, SimpleNamespace())
    # Controller must have been called
    assert len(controller.handle_calls) == 1


@pytest.mark.asyncio
async def test_conversation_text_natural_profile_uses_typing_without_ack() -> None:
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.actions: list[tuple[int, str]] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def send_chat_action(self, **kwargs):
            self.actions.append((kwargs["chat_id"], str(kwargs["action"])))

    class Controller:
        async def handle_conversation_text(self, text, ctx):
            return ControllerResponse("自然回复")

    message = SimpleNamespace(text="帮我看下 bug")
    update = SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=message,
    )
    config = SimpleNamespace(
        telegram=SimpleNamespace(allowed_user_ids=frozenset({123}), private_chat_only=True),
        interaction=SimpleNamespace(profile="natural", streaming_enabled=True, edit_min_interval_seconds=0.0),
    )
    bot = Bot()
    handlers = WlCodexHandlers(
        config=config,
        controller=Controller(),
        ledger=SimpleNamespace(
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=bot,
    )

    await handlers.conversation_text(update, SimpleNamespace())

    assert "正在处理你的消息，请稍候..." not in bot.sent
    assert bot.sent == ["自然回复"]


@pytest.mark.asyncio
async def test_conversation_text_natural_profile_respects_already_rendered() -> None:
    """When controller returns already_rendered=True, handler must NOT send duplicate."""
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def send_chat_action(self, **kwargs):
            pass

    class Controller:
        async def handle_conversation_text(self, text, ctx):
            return ControllerResponse("不应该发送", already_rendered=True)

    message = SimpleNamespace(text="帮我看下 bug")
    update = SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=message,
    )
    config = SimpleNamespace(
        telegram=SimpleNamespace(allowed_user_ids=frozenset({123}), private_chat_only=True),
        interaction=SimpleNamespace(profile="natural", streaming_enabled=True, edit_min_interval_seconds=0.0),
    )
    bot = Bot()
    handlers = WlCodexHandlers(
        config=config,
        controller=Controller(),
        ledger=SimpleNamespace(
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=bot,
    )

    await handlers.conversation_text(update, SimpleNamespace())

    # Must NOT send the response text since already_rendered=True
    assert bot.sent == []


@pytest.mark.asyncio
async def test_claude_cmd_respects_already_rendered() -> None:
    """claude_cmd must skip sending when controller response has already_rendered=True."""
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def send_chat_action(self, **kwargs):
            pass

    class FakeCtrl:
        def __init__(self) -> None:
            self.handle_calls: list[str] = []

        async def handle(self, text: str, ctx) -> ControllerResponse:
            self.handle_calls.append(text)
            return ControllerResponse("已完成", already_rendered=True)

    controller = FakeCtrl()
    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=controller,
        ledger=SimpleNamespace(
            list_tasks=lambda *a, **kw: [],
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=Bot(),
    )

    update = SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=SimpleNamespace(text="/claude fix bug"),
    )
    await handlers.claude_cmd(update, SimpleNamespace())

    # Controller must have been called
    assert len(controller.handle_calls) == 1
    # But no message sent since already_rendered=True


@pytest.mark.asyncio
async def test_settings_callback_routes_claude_permission() -> None:
    """Claude permission inline buttons must route through the settings callback."""
    from wlcodex.controller import ControllerResponse
    from wlcodex.telegram_app import WlCodexHandlers

    class Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def handle(self, text, ctx):
            self.calls.append((text, ctx))
            return ControllerResponse(
                "当前模式：允许编辑",
                buttons=[[
                    {
                        "text": "允许编辑",
                        "callback_data": "settings:claude_permission:acceptEdits",
                    }
                ]],
            )

    class Query:
        def __init__(self) -> None:
            self.data = "settings:claude_permission:acceptEdits"
            self.answers: list[str] = []
            self.message = SimpleNamespace(message_id=10, text="settings")

        async def answer(self, text):
            self.answers.append(text)

    class Bot:
        def __init__(self) -> None:
            self.edits: list[tuple[int, int, str, object]] = []

        async def edit_message_text(
            self, *, chat_id, message_id, text, reply_markup=None
        ):
            self.edits.append((chat_id, message_id, text, reply_markup))

    controller = Controller()
    bot = Bot()
    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=controller,
        ledger=SimpleNamespace(record_telegram_update=lambda *a, **kw: None),
        approval_service=None,
        bot=bot,
    )
    query = Query()
    update = SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=None,
        callback_query=query,
    )

    await handlers.callback_router(update, SimpleNamespace())

    assert controller.calls == [("/claude_mode acceptEdits", {"chat_id": 456, "user_id": 123})]
    assert query.answers == ["已切换"]
    assert bot.edits
    assert bot.edits[0][:3] == (456, 10, "当前模式：允许编辑")


@pytest.mark.asyncio
async def test_codex_cmd_respects_already_rendered() -> None:
    """codex_cmd must skip sending when controller response has already_rendered=True."""
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def send_chat_action(self, **kwargs):
            pass

    class FakeCtrl:
        def __init__(self) -> None:
            self.handle_calls: list[str] = []

        async def handle(self, text: str, ctx) -> ControllerResponse:
            self.handle_calls.append(text)
            return ControllerResponse("", already_rendered=True)

    controller = FakeCtrl()
    handlers = WlCodexHandlers(
        config=SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=frozenset({123}))),
        controller=controller,
        ledger=SimpleNamespace(
            list_tasks=lambda *a, **kw: [],
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=Bot(),
    )

    update = SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=SimpleNamespace(text="/codex analyze"),
    )
    await handlers.codex_cmd(update, SimpleNamespace())

    assert len(controller.handle_calls) == 1


@pytest.mark.asyncio
async def test_auto_cmd_natural_profile_no_ack() -> None:
    """auto_cmd in natural profile must not send textual ACK, uses streaming."""
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def send_chat_action(self, **kwargs):
            pass

    class FakeCtrl:
        def __init__(self) -> None:
            self.handle_calls: list[str] = []

        async def handle(self, text: str, ctx) -> ControllerResponse:
            self.handle_calls.append(text)
            return ControllerResponse("done", already_rendered=True)

    controller = FakeCtrl()
    config = SimpleNamespace(
        telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
        interaction=SimpleNamespace(profile="natural", streaming_enabled=True, edit_min_interval_seconds=0.0),
    )
    handlers = WlCodexHandlers(
        config=config,
        controller=controller,
        ledger=SimpleNamespace(
            list_tasks=lambda *a, **kw: [],
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=Bot(),
    )

    update = SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=SimpleNamespace(text="/auto fix bug"),
    )
    await handlers.auto_cmd(update, SimpleNamespace())

    assert len(controller.handle_calls) == 1


@pytest.mark.asyncio
async def test_legacy_profile_conversation_text_uses_ack() -> None:
    """Legacy profile must still send ACK before processing."""
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.controller import ControllerResponse

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(self.sent))

        async def send_chat_action(self, **kwargs):
            pass

        async def edit_message_text(self, **kwargs):
            pass

    class Controller:
        async def handle_conversation_text(self, text, ctx):
            return ControllerResponse("处理完成")

    config = SimpleNamespace(
        telegram=SimpleNamespace(allowed_user_ids=frozenset({123}), private_chat_only=True),
        interaction=SimpleNamespace(profile="legacy", streaming_enabled=False, edit_min_interval_seconds=1.0),
    )
    bot = Bot()
    handlers = WlCodexHandlers(
        config=config,
        controller=Controller(),
        ledger=SimpleNamespace(
            record_telegram_update=lambda *a, **kw: None,
        ),
        approval_service=object(),
        bot=bot,
    )

    message = SimpleNamespace(text="帮我看下 bug")
    update = SimpleNamespace(
        update_id=1,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=message,
    )
    await handlers.conversation_text(update, SimpleNamespace())

    # Legacy must send the ACK
    assert any("稍候" in s or "处理" in s for s in bot.sent)


# ---------------------------------------------------------------------------
# Claude streaming error handling (Issue 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cmd_routes_to_runtime_runner_without_direct_streaming() -> None:
    """The /claude entrypoint must not stream Claude output directly."""
    from wlcodex.controller import CommandController

    received_events: list[object] = []

    class FakeRenderer:
        async def handle(self, event):
            received_events.append(event)

    class ErrorClaude:
        enabled = True
        direct_stream_called = False

        async def send_streaming(self, request):
            self.direct_stream_called = True
            raise AssertionError("/claude must not call Claude directly")

    class FakeRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start_chief_engineer(self, **kwargs):
            self.calls.append(kwargs)

    class FakeTaskService:
        def reserve_task(self, *a, **kw):
            return SimpleNamespace(id=99, changed_file_count=0)

        def fail_task(self, task_id, error):
            pass

        def get_workspace(self, alias):
            return SimpleNamespace(path="/tmp/test")

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(
                id=1, title="test", mode="claude_direct",
                workspace_alias="demo", conversation_summary="",
                active_codex_task_id=None, active_claude_run_id=None,
            )

        def create_conversation(self, **kw):
            return SimpleNamespace(id=1, title="test")

        def create_agent_run(self, **kw):
            return SimpleNamespace(id=10)

        def create_orchestration_run(self, **kw):
            return SimpleNamespace(id=20)

        def set_conversation_active_task(self, *a):
            pass

        def set_task_status(self, *a):
            pass

        def set_conversation_active_claude_run(self, *a):
            pass

        def update_agent_run_status(self, *a, **kw):
            pass

        def update_conversation_summary(self, *a):
            pass

    claude = ErrorClaude()
    runner = FakeRunner()
    controller = CommandController(
        task_service=FakeTaskService(),
        backend=SimpleNamespace(),
        inspector=SimpleNamespace(),
        ledger=FakeLedger(),
        claude_backend=claude,
        interaction_renderer=FakeRenderer(),
        orchestration_runner=runner,
    )

    response = await controller.handle("/claude fix bug", {"chat_id": 456, "user_id": 123})
    assert response.already_rendered

    assert runner.calls
    assert runner.calls[0]["task_id"] == 99
    assert runner.calls[0]["orchestration_run_id"] == 20
    assert runner.calls[0]["codex_analysis_run_id"] == 10
    assert not claude.direct_stream_called
    event_types = [e.event_type for e in received_events]
    assert "run_started" in event_types


@pytest.mark.asyncio
async def test_claude_cmd_starts_runtime_runner_without_renderer() -> None:
    """/claude must still use the runtime runner when Telegram rendering is static."""
    from wlcodex.controller import CommandController

    class MixedClaude:
        enabled = True
        direct_stream_called = False

        async def send_streaming(self, request):
            self.direct_stream_called = True
            raise AssertionError("/claude must not call Claude directly")

    class FakeTaskService:
        def get_workspace(self, alias):
            return SimpleNamespace(path="/tmp/test")

        def reserve_task(self, *a, **kw):
            return SimpleNamespace(id=99, changed_file_count=0)

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(
                id=1, title="test", mode="claude_direct",
                workspace_alias="demo", conversation_summary="",
                active_codex_task_id=None, active_claude_run_id=None,
            )

        def create_conversation(self, **kw):
            return SimpleNamespace(id=1, title="test")

        def create_agent_run(self, **kw):
            return SimpleNamespace(id=10)

        def create_orchestration_run(self, **kw):
            return SimpleNamespace(id=20)

        def update_agent_run_status(self, *a, **kw):
            pass

        def set_conversation_active_task(self, *a):
            pass

    class FakeRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start_chief_engineer(self, **kwargs):
            self.calls.append(kwargs)

    claude = MixedClaude()
    runner = FakeRunner()
    controller = CommandController(
        task_service=FakeTaskService(),
        backend=SimpleNamespace(),
        inspector=SimpleNamespace(),
        ledger=FakeLedger(),
        claude_backend=claude,
        orchestration_runner=runner,
    )

    response = await controller.handle("/claude fix bug", {"chat_id": 456, "user_id": 123})
    assert not response.already_rendered
    assert "已开始" in response.text
    assert runner.calls
    assert not claude.direct_stream_called
