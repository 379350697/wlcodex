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
    assert "尚未启用" in sent_messages[0][1]
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
    assert "已切到 terminal 模式" in sent_messages[0][1]
    assert "claude" in sent_messages[0][1]
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
    # Must send terminal mode hint
    assert len(sent_messages) == 1
    assert "terminal 模式" in sent_messages[0][1]


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
    assert "product" in sent_messages[0][1].lower()
    # Must NOT be the "terminal mode not enabled" message
    assert "尚未启用" not in sent_messages[0][1]
    # Mode switch event must be recorded
    mode_switches = [e for e in store.events
                     if getattr(e, "event_type", "") == "conversation.mode.switched"]
    assert len(mode_switches) == 1
    assert mode_switches[0].payload["to_mode"] == "product"


# --- Terminal input with active terminal session ---


@pytest.mark.asyncio
async def test_terminal_input_with_active_session_calls_manager_not_controller():
    """When terminal mode has an active session, plain text must be sent to
    the terminal manager and must NOT call the product controller."""
    from types import SimpleNamespace

    from wlcodex.telegram_app import WlCodexHandlers
    from wlcodex.surfaces.terminal.models import TerminalSessionRef

    controller_called = []

    class FakeController:
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
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_session_1",
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
        sent_messages.append((chat_id, text))
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
    # Must send input to terminal adapter
    assert len(adapter.inputs) == 1, (
        f"adapter.inputs has {len(adapter.inputs)} entries"
    )
    assert adapter.inputs[0] == ("claude_session_1", "pytest -q")
    # No hint message since session was active
    assert len(sent_messages) == 0
    # terminal.session.input.sent event must be recorded
    input_events = [e for e in store.events
                    if getattr(e, "event_type", "") == "terminal.session.input.sent"]
    assert len(input_events) == 1
    assert input_events[0].payload["agent"] == "claude"
    assert input_events[0].payload["external_session_id"] == "claude_session_1"


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
    assert "terminal" in sent_messages[0][1].lower()
    assert "product" in sent_messages[0][1].lower()


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
    assert "已接入" in sent_messages[0][1]

    # Must record mode switch AND terminal.session.attached
    event_types = [getattr(e, "event_type", "") for e in store.events]
    assert "conversation.mode.switched" in event_types
    assert "terminal.session.attached" in event_types

    # Verify attached event payload
    attached_events = [e for e in store.events
                       if getattr(e, "event_type", "") == "terminal.session.attached"]
    assert len(attached_events) == 1
    ae = attached_events[0]
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
    assert "无可接入" in sent_messages[0][1]

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
    assert "terminal" in sent_messages[0][1].lower()


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
    assert "product" in sent_messages[0][1].lower()

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
    assert "已接入" in sent_messages[0][1], (
        f"Expected '已接入' in message, got: {sent_messages[0][1]}"
    )
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
    assert "已接入" in sent_messages[0][1]
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
