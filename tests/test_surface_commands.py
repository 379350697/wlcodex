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


def test_terminal_pause_parses_subcommand():
    command = parse_command("/terminal pause")

    assert command.kind == "terminal_subcommand"
    assert command.subcommand == "pause"


def test_terminal_agent_claude_parses_mode_switch():
    command = parse_command("/terminal agent claude")

    assert command.kind == "mode_switch"
    assert command.mode == "terminal"
    assert command.agent == "claude"


def test_terminal_agent_codex_parses_mode_switch():
    command = parse_command("/terminal agent codex")

    assert command.kind == "mode_switch"
    assert command.mode == "terminal"
    assert command.agent == "codex"


def test_terminal_agent_unknown_errors():
    with pytest.raises(ParseError):
        parse_command("/terminal agent unknown")


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
    assert sent_messages[0][1] == "已回到驾驶舱。现场仍在运行，我会继续用摘要跟进。"
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
            terminal=SimpleNamespace(enabled=True),
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
    assert "当前没有可接管的现场" in sent_messages[0][1]
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


# --- terminal.enabled guard tests ---


@pytest.mark.asyncio
async def test_terminal_cmd_blocked_when_disabled():
    """When terminal.enabled=false, /terminal must reject the switch."""
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
            terminal=SimpleNamespace(enabled=False),
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
        update_id=10,
    )

    await handlers.terminal_cmd(update, None)

    assert len(sent_messages) == 1
    assert "现场接管当前不可用" in sent_messages[0][1]
    assert "terminal.enabled" not in sent_messages[0][1]
    # No mode switch event should have been recorded
    mode_switch_events = [e for e in store.events
                         if getattr(e, "event_type", "") == "conversation.mode.switched"]
    assert len(mode_switch_events) == 0


@pytest.mark.asyncio
async def test_terminal_cmd_allowed_when_enabled():
    """When terminal.enabled=true, /terminal must proceed with the switch."""
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
            terminal=SimpleNamespace(enabled=True),
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
        update_id=11,
    )

    await handlers.terminal_cmd(update, None)

    assert len(sent_messages) == 1
    assert "当前没有可接管的现场" in sent_messages[0][1]
    # Mode switch event must be recorded
    assert len(store.events) == 2
    assert store.events[1].event_type == "conversation.mode.switched"


# --- conversation_text terminal routing test ---


@pytest.mark.asyncio
async def test_conversation_text_in_terminal_mode_does_not_call_product_controller():
    """When active mode is terminal, conversation_text must route to terminal
    input and must NOT call the product controller."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers

    controller_called = []

    class FakeController:
        async def handle_conversation_text(self, text, ctx):
            controller_called.append(text)

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeConn:
        """Pretends the last mode.switched event says 'terminal'."""
        def execute(self, sql, params):
            return self

        def fetchone(self):
            return {"payload": '{"to_mode": "terminal"}'}

    class FakeRuntimeStoreWithConn(FakeRuntimeStore):
        def __init__(self):
            super().__init__()
            self._conn = FakeConn()

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    store = FakeRuntimeStoreWithConn()
    ledger = FakeLedger()
    controller = FakeController()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True),
        ),
        controller=controller,
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
        effective_message=SimpleNamespace(text="some terminal input"),
        update_id=20,
    )

    await handlers.conversation_text(update, None)

    # Must NOT call product controller
    assert len(controller_called) == 0
    # Must send an actionable Onsite start-card hint.
    assert len(sent_messages) == 1
    assert "当前没有可接管的现场" in sent_messages[0][1]


@pytest.mark.asyncio
async def test_conversation_text_in_product_mode_calls_controller():
    """When active mode is product (or unset), conversation_text must call
    the product controller normally."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers

    controller_called = []

    class FakeController:
        async def handle_conversation_text(self, text, ctx):
            controller_called.append(text)
            return SimpleNamespace(already_rendered=False, text="response", buttons=None)

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeConn:
        """Pretends the last mode.switched event says 'product'."""
        def execute(self, sql, params):
            return self

        def fetchone(self):
            return {"payload": '{"to_mode": "product"}'}

    class FakeRuntimeStoreWithConn(FakeRuntimeStore):
        def __init__(self):
            super().__init__()
            self._conn = FakeConn()

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    store = FakeRuntimeStoreWithConn()
    ledger = FakeLedger()
    controller = FakeController()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
        ),
        controller=controller,
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
        effective_message=SimpleNamespace(text="hello product"),
        update_id=21,
    )

    await handlers.conversation_text(update, None)

    # Must call product controller
    assert len(controller_called) == 1
    assert controller_called[0] == "hello product"


# --- /terminal product bypass when terminal.enabled=false ---


@pytest.mark.asyncio
async def test_terminal_product_bypasses_disabled_check():
    """When terminal.enabled=false, /terminal product must still switch to product."""
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
            terminal=SimpleNamespace(enabled=False),
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
        effective_message=SimpleNamespace(text="/terminal product"),
        update_id=99,
    )

    await handlers.terminal_cmd(update, None)

    # Must have sent mode switch confirmation for product
    assert len(sent_messages) == 1
    assert "驾驶舱" in sent_messages[0][1]
    # Must NOT be the "terminal mode not enabled" message
    assert "尚未启用" not in sent_messages[0][1]
    # Mode switch event must be recorded
    mode_switches = [e for e in store.events
                     if getattr(e, "event_type", "") == "conversation.mode.switched"]
    assert len(mode_switches) == 1
    assert mode_switches[0].payload["to_mode"] == "product"


# --- Terminal input with active terminal session ---


@pytest.mark.asyncio
async def test_terminal_input_with_active_session_prompts_busy_choices_not_raw_send():
    """Terminal mode plain text should offer the same busy choices as product."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.models import TerminalSessionRef

    controller_called = []

    class FakeController:
        async def handle_terminal_workspace_busy(
            self, active, original_text, agent_label
        ):
            return SimpleNamespace(
                text=(
                    "当前工作区正在执行，新的话不会丢。\n\n"
                    "你刚发的新话可以这样处理："
                ),
                buttons=[
                    [
                        {
                            "text": f"发给当前 {agent_label}",
                            "callback_data": f"busy_append:{active.id}",
                        }
                    ],
                    [
                        {
                            "text": "打断并执行这句",
                            "callback_data": f"busy_interrupt:{active.id}",
                        }
                    ],
                    [
                        {
                            "text": "排队稍后",
                            "callback_data": f"busy_queue:{active.id}",
                        }
                    ],
                    [
                        {
                            "text": "新开隔离现场",
                            "callback_data": f"busy_new_session:{active.id}",
                        }
                    ],
                ],
            )

        async def handle_conversation_text(self, text, ctx):
            controller_called.append(text)

    class FakeAdapter:
        def __init__(self):
            self.inputs = []

        async def send_input(self, session_ref, text):
            self.inputs.append((session_ref.external_session_id, text))

    class FakeTerminalManager:
        def __init__(self, adapter):
            self._adapter = adapter
            self._sessions: dict[int, list[TerminalSessionRef]] = {}

        def attach(self, conversation_id, agent, strategy, external_session_id):
            ref = TerminalSessionRef(
                conversation_id=conversation_id,
                agent=agent,
                strategy=strategy,
                external_session_id=external_session_id,
                status="attached",
            )
            self._sessions.setdefault(conversation_id, []).append(ref)
            return ref

        async def send_input(self, session_ref, text):
            await self._adapter.send_input(session_ref, text)

        def active_for_conversation(self, conversation_id):
            sessions = self._sessions.get(conversation_id, [])
            for s in reversed(sessions):
                if s.status == "attached":
                    return s
            return None

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeConn:
        def execute(self, sql, params):
            return self

        def fetchone(self):
            return {"payload": '{"to_mode": "terminal"}'}

    class FakeRuntimeStoreWithConn(FakeRuntimeStore):
        def __init__(self):
            super().__init__()
            self._conn = FakeConn()

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    adapter = FakeAdapter()
    manager = FakeTerminalManager(adapter)
    manager.attach(
        conversation_id=42,
        agent="codex",
        strategy="stream_json",
        external_session_id="codex_thread_1",
    )

    store = FakeRuntimeStoreWithConn()
    ledger = FakeLedger()
    controller = FakeController()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True),
        ),
        controller=controller,
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=manager,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text, buttons))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="pytest -q"),
        update_id=200,
    )

    await handlers.conversation_text(update, None)

    # Must NOT call product controller
    assert len(controller_called) == 0, (
        f"product controller was called {controller_called} times"
    )
    # Must not silently feed raw input; user gets explicit control choices.
    assert adapter.inputs == []
    assert len(sent_messages) == 1
    _, text, buttons = sent_messages[0]
    assert "当前工作区正在执行" in text
    labels = {button["text"] for row in buttons for button in row}
    assert "发给当前 Codex" in labels
    assert "打断并执行这句" in labels
    assert "排队稍后" in labels
    assert "新开隔离现场" in labels
    # terminal.session.input.sent event must not be recorded until user chooses.
    input_events = [e for e in store.events
                    if getattr(e, "event_type", "") == "terminal.session.input.sent"]
    assert input_events == []


@pytest.mark.asyncio
async def test_terminal_input_without_active_session_sends_hint():
    """When terminal mode has NO active session, sends actionable hint.
    Still must NOT call the product controller."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers

    controller_called = []

    class FakeController:
        async def handle_conversation_text(self, text, ctx):
            controller_called.append(text)

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeConn:
        def execute(self, sql, params):
            return self

        def fetchone(self):
            return {"payload": '{"to_mode": "terminal"}'}

    class FakeRuntimeStoreWithConn(FakeRuntimeStore):
        def __init__(self):
            super().__init__()
            self._conn = FakeConn()

    class FakeLedger:
        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            pass

    # No terminal_manager at all — simulates the state before manager wiring.
    store = FakeRuntimeStoreWithConn()
    ledger = FakeLedger()
    controller = FakeController()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True),
        ),
        controller=controller,
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=None,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="some input"),
        update_id=201,
    )

    await handlers.conversation_text(update, None)

    # Must NOT call product controller
    assert len(controller_called) == 0
    # Must send a hint about no active session
    assert len(sent_messages) == 1
    assert "当前没有可接管的现场" in sent_messages[0][1]


# --- `/terminal claude|codex` attach behavior with terminal_manager ---


@pytest.mark.asyncio
async def test_terminal_claude_cmd_attaches_when_external_session_exists():
    """When terminal_manager has an adapter and an agent_run has an
    external_session_id, /terminal claude must attach to it and record
    a terminal.session.attached event."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager
    from wlcodex.surfaces.terminal.models import TerminalSessionRef

    class FakeAdapter:
        def __init__(self):
            self.refs: list[TerminalSessionRef] = []

        async def send_input(self, ref, text):
            pass

    adapter = FakeAdapter()
    terminal_manager = TerminalSessionManager(adapters={"claude": adapter})

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def __init__(self):
            self._updates: list[dict] = []

        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            self._updates.append(kwargs)

        def list_agent_runs(self, conversation_id, limit=50):
            return [
                SimpleNamespace(
                    agent="claude",
                    external_session_id="claude_sess_abc",
                    status="done",
                ),
            ]

        def list_recent_agent_runs(self, conversation_id, limit=50):
            return self.list_agent_runs(conversation_id, limit=limit)

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=terminal_manager,
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
        update_id=200,
    )

    await handlers.terminal_cmd(update, None)

    # Must send confirmation that session was attached
    assert len(sent_messages) == 1
    assert "接管现场" in sent_messages[0][1]
    assert "claude" in sent_messages[0][1]

    # Must record mode switch AND terminal.session.attached
    event_types = [getattr(e, "event_type", "") for e in store.events]
    assert "conversation.mode.switched" in event_types
    assert "terminal.session.attached" in event_types

    # Verify attached event payload
    attached_events = [e for e in store.events
                       if getattr(e, "event_type", "") == "terminal.session.attached"]
    assert len(attached_events) == 1
    ae = attached_events[0]
    assert ae.visibility == "operator"
    assert ae.payload["agent"] == "claude"
    assert ae.payload["external_session_id"] == "claude_sess_abc"
    assert ae.payload["status"] == "attached"

    # Verify terminal manager has the session
    ref = terminal_manager.active_for_conversation(42)
    assert ref is not None
    assert ref.agent == "claude"
    assert ref.external_session_id == "claude_sess_abc"


@pytest.mark.asyncio
async def test_terminal_claude_cmd_no_external_session_reports_unavailable():
    """When no agent_run has an external_session_id, /terminal claude must
    NOT create a misleading fake active session. It reports the agent is
    not available and subsequent text in terminal mode sends a hint."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager

    class FakeAdapter:
        async def send_input(self, ref, text):
            pass

    adapter = FakeAdapter()
    terminal_manager = TerminalSessionManager(adapters={"claude": adapter})

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeConn:
        def execute(self, sql, params):
            return self

        def fetchone(self):
            return {"payload": '{"to_mode": "terminal"}'}

    class FakeRuntimeStoreWithConn(FakeRuntimeStore):
        def __init__(self):
            super().__init__()
            self._conn = FakeConn()

    class FakeLedger:
        def __init__(self):
            self._updates: list[dict] = []

        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            self._updates.append(kwargs)

        def list_agent_runs(self, conversation_id, limit=50):
            return []  # No agent runs at all

        def list_recent_agent_runs(self, conversation_id, limit=50):
            return []

    store = FakeRuntimeStoreWithConn()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=terminal_manager,
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
        update_id=300,
    )

    await handlers.terminal_cmd(update, None)

    # Must send a message indicating no session is available
    assert len(sent_messages) == 1
    assert "当前没有可接管的现场" in sent_messages[0][1]

    # Must NOT have created a fake active session in the manager
    ref = terminal_manager.active_for_conversation(42)
    assert ref is None, (
        f"Terminal manager should have no active session, got {ref}"
    )

    # Must still record mode switch
    mode_switches = [e for e in store.events
                     if getattr(e, "event_type", "") == "conversation.mode.switched"]
    assert len(mode_switches) == 1

    # Must NOT record terminal.session.attached
    attached = [e for e in store.events
                if getattr(e, "event_type", "") == "terminal.session.attached"]
    assert len(attached) == 0

    # Subsequent plain text in terminal mode must still send a hint
    sent_messages.clear()
    text_update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="some terminal input"),
        update_id=301,
    )
    await handlers.conversation_text(text_update, None)

    assert len(sent_messages) == 1
    assert "当前没有可接管的现场" in sent_messages[0][1]


@pytest.mark.asyncio
async def test_terminal_product_does_not_attach_session():
    """/terminal product must only switch mode, never attach a terminal session."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager

    class FakeAdapter:
        async def send_input(self, ref, text):
            pass

    adapter = FakeAdapter()
    terminal_manager = TerminalSessionManager(adapters={"claude": adapter})

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def __init__(self):
            self._updates: list[dict] = []

        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            self._updates.append(kwargs)

        def list_agent_runs(self, conversation_id, limit=50):
            return [
                SimpleNamespace(
                    agent="claude",
                    external_session_id="claude_sess_abc",
                    status="done",
                ),
            ]

        def list_recent_agent_runs(self, conversation_id, limit=50):
            return self.list_agent_runs(conversation_id, limit=limit)

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=False),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=terminal_manager,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/terminal product"),
        update_id=400,
    )

    await handlers.terminal_cmd(update, None)

    # Must confirm product mode switch
    assert len(sent_messages) == 1
    assert "驾驶舱" in sent_messages[0][1]

    # Must NOT record terminal.session.attached
    attached = [e for e in store.events
                if getattr(e, "event_type", "") == "terminal.session.attached"]
    assert len(attached) == 0

    # Terminal manager must have no active session for this conversation
    ref = terminal_manager.active_for_conversation(42)
    assert ref is None


# --- bare /terminal default_agent tests ---


@pytest.mark.asyncio
async def test_bare_terminal_uses_default_agent_codex():
    """Bare /terminal with terminal.default_agent=codex must attach codex session."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager

    class FakeAdapter:
        def __init__(self):
            self.refs = []

        async def send_input(self, ref, text):
            pass

    codex_adapter = FakeAdapter()
    claude_adapter = FakeAdapter()
    terminal_manager = TerminalSessionManager(
        adapters={"claude": claude_adapter, "codex": codex_adapter},
    )

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def __init__(self):
            self._updates = []

        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            self._updates.append(kwargs)

        def list_agent_runs(self, conversation_id, limit=50):
            return [
                SimpleNamespace(
                    agent="codex",
                    external_session_id="codex_sess_xyz",
                    status="running",
                ),
            ]

        def list_recent_agent_runs(self, conversation_id, limit=50):
            return self.list_agent_runs(conversation_id, limit=limit)

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True, default_agent="codex"),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=terminal_manager,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/terminal"),
        update_id=500,
    )

    await handlers.terminal_cmd(update, None)

    # Must send confirmation that codex session was attached
    assert len(sent_messages) == 1
    assert "接管现场" in sent_messages[0][1], (
        f"Expected Onsite confirmation in message, got: {sent_messages[0][1]}"
    )
    assert "codex" in sent_messages[0][1]
    assert "codex" in sent_messages[0][1]

    # Mode switch event must have active_agent=codex
    mode_switches = [e for e in store.events
                     if getattr(e, "event_type", "") == "conversation.mode.switched"]
    assert len(mode_switches) == 1
    assert mode_switches[0].payload["active_agent"] == "codex"

    # terminal.session.attached event must have agent=codex
    attached_events = [e for e in store.events
                       if getattr(e, "event_type", "") == "terminal.session.attached"]
    assert len(attached_events) == 1
    assert attached_events[0].visibility == "operator"
    assert attached_events[0].payload["agent"] == "codex"
    assert attached_events[0].payload["external_session_id"] == "codex_sess_xyz"

    # Terminal manager must reference the codex session
    ref = terminal_manager.active_for_conversation(42)
    assert ref is not None
    assert ref.agent == "codex"
    assert ref.external_session_id == "codex_sess_xyz"


@pytest.mark.asyncio
async def test_bare_terminal_uses_default_agent_claude():
    """Bare /terminal with terminal.default_agent=claude must attach claude session."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager

    class FakeAdapter:
        def __init__(self):
            self.refs = []

        async def send_input(self, ref, text):
            pass

    claude_adapter = FakeAdapter()
    terminal_manager = TerminalSessionManager(adapters={"claude": claude_adapter})

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def __init__(self):
            self._updates = []

        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            self._updates.append(kwargs)

        def list_agent_runs(self, conversation_id, limit=50):
            return [
                SimpleNamespace(
                    agent="claude",
                    external_session_id="claude_sess_def",
                    status="done",
                ),
            ]

        def list_recent_agent_runs(self, conversation_id, limit=50):
            return self.list_agent_runs(conversation_id, limit=limit)

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True, default_agent="claude"),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=terminal_manager,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/terminal"),
        update_id=501,
    )

    await handlers.terminal_cmd(update, None)

    assert len(sent_messages) == 1
    assert "接管现场" in sent_messages[0][1]
    assert "claude" in sent_messages[0][1]
    assert "claude" in sent_messages[0][1]

    mode_switches = [e for e in store.events
                     if getattr(e, "event_type", "") == "conversation.mode.switched"]
    assert len(mode_switches) == 1
    assert mode_switches[0].payload["active_agent"] == "claude"

    attached_events = [e for e in store.events
                       if getattr(e, "event_type", "") == "terminal.session.attached"]
    assert len(attached_events) == 1
    assert attached_events[0].payload["agent"] == "claude"

    ref = terminal_manager.active_for_conversation(42)
    assert ref is not None
    assert ref.agent == "claude"


@pytest.mark.asyncio
async def test_bare_terminal_event_payload_agent_is_correct():
    """The mode switch and terminal.session.attached events must both record
    the resolved agent, even when no explicit agent was given."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.manager import TerminalSessionManager

    class FakeAdapter:
        def __init__(self):
            self.refs = []

        async def send_input(self, ref, text):
            pass

    adapter = FakeAdapter()
    terminal_manager = TerminalSessionManager(adapters={"codex": adapter})

    class FakeRuntimeStore:
        def __init__(self):
            self.events = []

        def append(self, event):
            self.events.append(event)

    class FakeLedger:
        def __init__(self):
            self._updates = []

        def get_active_conversation(self, chat_id):
            return SimpleNamespace(id=42)

        def record_telegram_update(self, **kwargs):
            self._updates.append(kwargs)

        def list_agent_runs(self, conversation_id, limit=50):
            return [
                SimpleNamespace(
                    agent="codex",
                    external_session_id="codex_def",
                    status="running",
                ),
            ]

        def list_recent_agent_runs(self, conversation_id, limit=50):
            return self.list_agent_runs(conversation_id, limit=limit)

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            terminal=SimpleNamespace(enabled=True, default_agent="codex"),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
        terminal_manager=terminal_manager,
    )

    sent_messages = []

    async def fake_send(chat_id, text, buttons=None):
        sent_messages.append((chat_id, text))
        return 1

    handlers.send_telegram = fake_send

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=100, type="private"),
        effective_message=SimpleNamespace(text="/terminal"),
        update_id=502,
    )

    await handlers.terminal_cmd(update, None)

    # Both events must agree on agent=codex
    mode_switches = [e for e in store.events
                     if getattr(e, "event_type", "") == "conversation.mode.switched"]
    attached = [e for e in store.events
                if getattr(e, "event_type", "") == "terminal.session.attached"]
    assert len(mode_switches) == 1
    assert len(attached) == 1
    assert mode_switches[0].payload["active_agent"] == "codex"
    assert attached[0].payload["agent"] == "codex"

    # Confirmation message must mention codex
    assert len(sent_messages) == 1
    assert "codex" in sent_messages[0][1]


# --- Preview transport tests ---


@pytest.mark.asyncio
async def test_send_telegram_preview_waits_for_outbox_message_id(tmp_path):
    from types import SimpleNamespace
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.telegram_outbox import TelegramOutbox

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    outbox = TelegramOutbox(store=store)

    class Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=4321)

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural"),
            telegram_output=SimpleNamespace(preview_send_timeout_seconds=2.0),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=Bot(),
        runtime_event_store=store,
        outbox=outbox,
    )

    import asyncio

    waiter = asyncio.create_task(
        handlers.send_telegram_preview(1, "Codex 正在处理")
    )
    await asyncio.sleep(0)  # yield so waiter enqueues
    await outbox.process_all()

    assert await waiter == 4321


# --- Product mode no-fragment integration test ---


@pytest.mark.asyncio
async def test_product_mode_does_not_send_token_fragments_during_stream():
    """Product cockpit must never send single-token body messages.
    Body is buffered until completion."""
    from unittest.mock import patch

    from wlcodex.interaction.events import InteractionEvent

    sent = []
    edited = []

    async def send(chat_id, text, buttons=None):
        sent.append((chat_id, text, buttons))
        return len(sent)

    async def edit(chat_id, message_id, text, buttons=None):
        edited.append((chat_id, message_id, text, buttons))

    async def typing(chat_id):
        return None

    # Build handlers using the _make_handlers helper from workbench tests
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

        def list_recent_agent_runs(self, conversation_id, limit=50):
            return []

    store = FakeRuntimeStore()
    ledger = FakeLedger()

    handlers = WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({123})),
            interaction=SimpleNamespace(profile="natural", edit_min_interval_seconds=0.0),
            telegram_output=SimpleNamespace(
                preview_send_timeout_seconds=2.0,
                semantic_min_chars=20,
                semantic_max_chars=80,
                final_chunk_chars=200,
                product_body_mode="final",
                terminal_body_mode="semantic_blocks",
            ),
        ),
        controller=SimpleNamespace(),
        ledger=ledger,
        approval_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        runtime_event_store=store,
    )
    handlers.send_telegram = send
    handlers.edit_telegram = edit
    handlers.send_telegram_preview = send
    handlers.edit_telegram_preview = edit
    with patch.object(handlers, "_get_active_surface_mode", return_value="product"):
        renderer = handlers.create_interaction_renderer()

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    for token in ["我", "查", "到", "的", "最新", "金价", "如下："]:
        await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text=token))
    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1, conversation_id=7, task_id=10))

    body_texts = [text for _, text, _ in sent if "我查到的最新金价如下：" in text]
    tiny_texts = [text for _, text, _ in sent if text in {"我", "查", "到", "的"}]
    assert len(body_texts) == 1
    assert tiny_texts == []
