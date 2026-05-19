"""Tests for dual-surface Telegram command parsing and handlers.

Covers: /mode, /product, /terminal, /terminal <agent>, /terminal <subcommand>.
"""

import pytest

from wlcodex.router import (
    ModeSwitchCommand,
    TerminalSubCommand,
    parse_command,
    ParseError,
)


# --- Parser tests (no Telegram dependency) ---


def test_product_command_parses_mode_switch():
    command = parse_command("/product")

    assert command.kind == "mode_switch"
    assert command.mode == "product"


def test_terminal_command_parses_mode_switch():
    command = parse_command("/terminal")

    assert command.kind == "mode_switch"
    assert command.mode == "terminal"
    assert command.agent == ""


def test_terminal_claude_parses_mode_switch_with_agent():
    command = parse_command("/terminal claude")

    assert command.kind == "mode_switch"
    assert command.mode == "terminal"
    assert command.agent == "claude"


def test_terminal_codex_parses_mode_switch_with_agent():
    command = parse_command("/terminal codex")

    assert command.kind == "mode_switch"
    assert command.mode == "terminal"
    assert command.agent == "codex"


def test_terminal_tail_parses_subcommand():
    command = parse_command("/terminal tail")

    assert command.kind == "terminal_subcommand"
    assert command.subcommand == "tail"


def test_terminal_detach_parses_subcommand():
    command = parse_command("/terminal detach")

    assert command.kind == "terminal_subcommand"
    assert command.subcommand == "detach"


def test_terminal_product_switches_to_product():
    command = parse_command("/terminal product")

    assert command.kind == "mode_switch"
    assert command.mode == "product"


def test_mode_command_queries_current_mode():
    command = parse_command("/mode")

    assert command.kind == "mode_switch"
    assert command.mode == ""


def test_unknown_subcommand_errors():
    with pytest.raises(ParseError):
        parse_command("/terminal unknown")


def test_parser_is_deterministic():
    """Repeated parsing of same input yields same result."""
    a = parse_command("/terminal claude")
    b = parse_command("/terminal claude")

    assert a == b
    assert a.kind == b.kind
    assert a.mode == b.mode
    assert a.agent == b.agent


# --- Handler behavior tests ---


@pytest.mark.asyncio
async def test_product_handler_sends_confirmation():
    """Handler for /product records a mode switch event and confirms."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers

    captured_events = []
    sent_messages = []

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

        def _conn(self):
            return None

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
    )

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/product"),
        update_id=1,
    )

    await handlers.product_cmd(update, None)

    assert len(sent_messages) == 1
    assert sent_messages[0][1] == "已切到 product 模式。"
    assert len(store.events) == 2
    assert store.events[1].event_type == "conversation.mode.switched"


@pytest.mark.asyncio
async def test_terminal_handler_sends_confirmation():
    """Handler for /terminal claude records a mode switch with agent."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/terminal claude"),
        update_id=2,
    )

    await handlers.terminal_cmd(update, None)

    assert len(sent_messages) == 1
    assert "已切到 terminal 模式" in sent_messages[0][1]
    assert "claude" in sent_messages[0][1]
    assert len(store.events) == 2
    assert store.events[1].event_type == "conversation.mode.switched"
    assert store.events[1].payload["active_agent"] == "claude"


@pytest.mark.asyncio
async def test_mode_command_does_not_create_conversation():
    """Mode switches must not create a new conversation."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/product"),
        update_id=3,
    )

    await handlers.product_cmd(update, None)

    mode_switch_event = store.events[-1]
    assert mode_switch_event.event_type == "conversation.mode.switched"
    assert mode_switch_event.payload["conversation_id"] == 42
    # Must NOT be a conversation.started event
    event_types = [e.event_type for e in store.events]
    assert "conversation.started" not in event_types


@pytest.mark.asyncio
async def test_terminal_subcommand_handler_rejects_without_session():
    """Terminal subcommands (tail, detach) should be handled gracefully."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/terminal tail"),
        update_id=4,
    )

    await handlers.terminal_cmd(update, None)

    assert len(sent_messages) == 1
    # Should indicate something about tail without crashing
    assert len(sent_messages[0][1]) > 0
