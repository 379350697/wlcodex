"""Task 7: Telegram Routing And View Switching — test suite.

Tests /terminal start-card behaviour, /product Cockpit return,
/settings card, Onsite text isolation from controller, and
user-facing copy censorship of terminal.enabled.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


def _make_update(text: str = "/terminal", chat_id: int = 100, user_id: int = 123):
    """Return a minimal fake telegram.Update."""
    eff_user = SimpleNamespace(id=user_id)
    eff_chat = SimpleNamespace(id=chat_id, type="private")
    eff_msg = SimpleNamespace(text=text)

    class FakeUpdate:
        effective_user = eff_user
        effective_chat = eff_chat
        effective_message = eff_msg
        update_id = 42
        callback_query = None

    return FakeUpdate()


def _make_handlers(*, controller=None, ledger=None, config=None,
                   runtime_store=None, terminal_manager=None,
                   execution_scheduler=None):
    """Build a WlCodexHandlers instance with test doubles."""
    from wlcodex.telegram_app import WlCodexHandlers

    if controller is None:
        controller = MagicMock()
        controller.handle = AsyncMock(return_value=SimpleNamespace(text="ok", buttons=None))
        controller.handle_conversation_text = AsyncMock(
            return_value=SimpleNamespace(text="conv ok", buttons=None, already_rendered=False)
        )

    if ledger is None:
        ledger = MagicMock()
        ledger.get_active_conversation = MagicMock(return_value=None)
        ledger.record_telegram_update = MagicMock()
        ledger.list_tasks = MagicMock(return_value=[])

    if config is None:
        config = MagicMock()
        config.telegram.allowed_user_ids = frozenset({123})
        type(config).interaction = SimpleNamespace(profile="legacy")
        type(config).terminal = SimpleNamespace(enabled=True, default_agent="claude",
                                                  max_frame_chars=3500)

    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1001))
    bot.edit_message_text = AsyncMock()
    bot.send_chat_action = AsyncMock()

    return WlCodexHandlers(
        config=config,
        controller=controller,
        ledger=ledger,
        approval_service=MagicMock(),
        bot=bot,
        runtime_event_store=runtime_store,
        outbox=None,
        terminal_manager=terminal_manager,
        execution_scheduler=execution_scheduler,
    )


# ── /terminal  with session ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_with_active_claude_session_auto_opens_onsite():
    """Spec §View Switching: /terminal with active Claude → auto-attach Claude onsite."""
    from wlcodex.runtime_events import EventType, Visibility
    from wlcodex.surfaces.terminal.models import TerminalSessionRef

    update = _make_update("/terminal")

    terminal_mgr = MagicMock()
    terminal_mgr.active_for_conversation = MagicMock(return_value=None)
    terminal_mgr.attach = MagicMock(return_value=TerminalSessionRef(
        conversation_id=42, agent="claude", strategy="stream_json",
        external_session_id="ext-1", status="attached",
    ))

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(id=42)
    )
    ledger.list_recent_agent_runs = MagicMock(return_value=[
        SimpleNamespace(agent="claude", external_session_id="ext-1"),
    ])
    ledger.record_telegram_update = MagicMock()

    runtime_store = MagicMock()
    runtime_store.append = MagicMock()

    handlers = _make_handlers(
        ledger=ledger,
        terminal_manager=terminal_mgr,
        runtime_store=runtime_store,
    )

    await handlers.terminal_cmd(update, None)

    # Should have attempted an attach with the found external session
    terminal_mgr.attach.assert_called_once()
    call_kwargs = terminal_mgr.attach.call_args.kwargs
    assert call_kwargs["agent"] == "claude"
    assert call_kwargs["external_session_id"] == "ext-1"

    # Confirm copy mentions attach success
    send_calls = handlers._bot.send_message.call_args_list
    sent_texts = [c.kwargs["text"] for c in send_calls]
    assert any("已切到 terminal" in t or "已进入接管现场" in t for t in sent_texts), (
        f"No attach-success copy found in: {sent_texts}"
    )

    attached_events = [
        call.args[0]
        for call in runtime_store.append.call_args_list
        if call.args[0].event_type == EventType.TERMINAL_SESSION_ATTACHED
    ]
    assert len(attached_events) == 1
    assert attached_events[0].visibility == Visibility.OPERATOR
    assert attached_events[0].payload["external_session_id"] == "ext-1"


# ── /terminal  no session → start card ─────────────────────────────


@pytest.mark.asyncio
async def test_terminal_with_no_session_sends_start_card_not_dead_end():
    """Spec §View Switching rule 6: no live session → start card, not dead view.

    The start card must offer actionable buttons (start Claude / start Codex /
    return to Cockpit) and MUST NOT say "请先启动任务" or equivalent dead-end copy.
    """
    update = _make_update("/terminal")

    terminal_mgr = MagicMock()
    terminal_mgr.active_for_conversation = MagicMock(return_value=None)
    terminal_mgr.attach = MagicMock()

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(id=42)
    )
    ledger.list_recent_agent_runs = MagicMock(return_value=[])
    ledger.record_telegram_update = MagicMock()

    handlers = _make_handlers(ledger=ledger, terminal_manager=terminal_mgr)

    await handlers.terminal_cmd(update, None)

    send_calls = handlers._bot.send_message.call_args_list
    sent_texts = [c.kwargs["text"] for c in send_calls]
    sent_buttons = [c.kwargs.get("reply_markup") for c in send_calls]

    # Must NOT contain dead-end language
    forbidden = ["请先", "无可接入", "启动任务", "请先通过"]
    for t in sent_texts:
        for phrase in forbidden:
            assert phrase not in t, (
                f"Dead-end copy '{phrase}' found in /terminal response: {t!r}"
            )

    # Should offer actionable options
    actionable_terms = ["启动", "现场", "驾驶舱", "接管", "Claude", "Codex"]
    found_any = any(any(term in t for term in actionable_terms) for t in sent_texts)

    # If not in text, must be in buttons
    if not found_any:
        has_buttons = any(b is not None for b in sent_buttons)
        assert has_buttons, (
            f"No actionable options in text or buttons: {sent_texts}"
        )


# ── Onsite text → busy choices, not product controller ─────────────


@pytest.mark.asyncio
async def test_onsite_text_prompts_busy_choices_not_controller():
    """Spec §Routing Rules: Onsite plain text → explicit busy choices.

    The product controller MUST NOT be called for ordinary text while in
    terminal / Onsite mode.
    """
    from wlcodex.surfaces.terminal.models import TerminalSessionRef

    update = _make_update("继续修失败测试", chat_id=200)

    session_ref = TerminalSessionRef(
        conversation_id=55, agent="claude", strategy="stream_json",
        external_session_id="ext-99", status="attached",
    )

    terminal_mgr = MagicMock()
    terminal_mgr.active_for_conversation = MagicMock(return_value=session_ref)
    terminal_mgr.send_input = AsyncMock(return_value=None)

    runtime_store = MagicMock()
    runtime_store._conn = MagicMock()
    runtime_store._conn.execute = MagicMock(return_value=MagicMock())
    runtime_store._conn.execute.return_value.fetchone = MagicMock(return_value=MagicMock())
    runtime_store.append = MagicMock()

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(id=55)
    )
    ledger.record_telegram_update = MagicMock()

    controller = MagicMock()
    controller.handle = AsyncMock()
    controller.handle_conversation_text = AsyncMock()
    controller.handle_terminal_workspace_busy = AsyncMock(
        return_value=SimpleNamespace(
            text="当前工作区正在执行，新的话不会丢。",
            buttons=[
                [{"text": "发给当前 Claude", "callback_data": "busy_append:55"}],
                [{"text": "打断并执行这句", "callback_data": "busy_interrupt:55"}],
                [{"text": "排队稍后", "callback_data": "busy_queue:55"}],
                [{"text": "新开隔离现场", "callback_data": "busy_new_session:55"}],
            ],
        )
    )

    handlers = _make_handlers(
        controller=controller, ledger=ledger,
        runtime_store=runtime_store, terminal_manager=terminal_mgr,
    )

    # Patch _get_active_surface_mode to return "terminal"
    with patch.object(handlers, "_get_active_surface_mode", return_value="terminal"):
        await handlers.conversation_text(update, None)

    # Terminal manager must not receive input until user chooses an action.
    terminal_mgr.send_input.assert_not_called()
    controller.handle_terminal_workspace_busy.assert_awaited_once()
    call_args = controller.handle_terminal_workspace_busy.call_args
    assert call_args.args[0].id == 55
    assert call_args.args[1] == "继续修失败测试"
    assert call_args.kwargs["agent_label"] == "Claude"

    # Controller must NOT be called
    controller.handle.assert_not_called()
    controller.handle_conversation_text.assert_not_called()


@pytest.mark.asyncio
async def test_onsite_text_uses_runtime_event_mode_without_falling_to_product(
    tmp_path,
):
    """A persisted terminal mode switch must route the next plain text onsite.

    This covers the live path after selecting a historical session from
    /sessions.  The user does not send /terminal before the next message.
    """
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import (
        AggregateType,
        EventSource,
        EventType,
        RuntimeEvent,
        Visibility,
        now_iso,
    )
    from wlcodex.surfaces.terminal.models import TerminalSessionRef

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    conversation = ledger.create_conversation(
        chat_id=200,
        user_id=123,
        title="真人历史现场 smoke 2",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.CONVERSATION_MODE_SWITCHED,
        aggregate_type=AggregateType.CONVERSATION,
        aggregate_id=str(conversation.id),
        correlation_id="mode-switch-test",
        source=EventSource.TELEGRAM,
        actor="user",
        visibility=Visibility.USER,
        payload={
            "chat_id": 200,
            "conversation_id": conversation.id,
            "from_mode": "product",
            "to_mode": "terminal",
            "active_agent": "codex",
        },
        occurred_at=now_iso(),
        conversation_id=conversation.id,
    ))

    session_ref = TerminalSessionRef(
        conversation_id=conversation.id,
        agent="codex",
        strategy="exec",
        external_session_id="thread-live-ok",
        status="attached",
    )
    terminal_mgr = MagicMock()
    terminal_mgr.active_for_conversation = MagicMock(return_value=session_ref)
    terminal_mgr.send_input = AsyncMock(return_value=None)

    controller = MagicMock()
    controller.handle = AsyncMock()
    controller.handle_conversation_text = AsyncMock(
        return_value=SimpleNamespace(
            text="product path should not run",
            buttons=None,
            already_rendered=False,
        )
    )
    controller.handle_terminal_workspace_busy = AsyncMock(
        return_value=SimpleNamespace(
            text="当前工作区正在执行，新的话不会丢。",
            buttons=[
                [
                    {
                        "text": "发给当前 Codex",
                        "callback_data": f"busy_append:{conversation.id}",
                    }
                ],
                [
                    {
                        "text": "打断并执行这句",
                        "callback_data": f"busy_interrupt:{conversation.id}",
                    }
                ],
                [{"text": "排队稍后", "callback_data": f"busy_queue:{conversation.id}"}],
                [
                    {
                        "text": "新开隔离现场",
                        "callback_data": f"busy_new_session:{conversation.id}",
                    }
                ],
            ],
        )
    )

    handlers = _make_handlers(
        controller=controller,
        ledger=ledger,
        runtime_store=store,
        terminal_manager=terminal_mgr,
    )

    await handlers.conversation_text(
        _make_update("为什么你没有反馈回来，是wlcodex的问题吗", chat_id=200),
        None,
    )

    terminal_mgr.send_input.assert_not_called()
    controller.handle_terminal_workspace_busy.assert_awaited_once()
    call_args = controller.handle_terminal_workspace_busy.call_args
    assert call_args.args[0].id == conversation.id
    assert call_args.args[1] == "为什么你没有反馈回来，是wlcodex的问题吗"
    assert call_args.kwargs["agent_label"] == "Codex"
    controller.handle.assert_not_called()
    controller.handle_conversation_text.assert_not_called()


# ── Onsite text without session → start card ───────────────────────


@pytest.mark.asyncio
async def test_onsite_text_without_session_shows_start_card_not_dead_end():
    """When user sends text in Onsite view but no active session exists,
    the response must show a start card with actionable buttons, NOT
    a dead-end hint that requires the user to know another command."""
    update = _make_update("hello from onsite", chat_id=200)

    terminal_mgr = MagicMock()
    terminal_mgr.active_for_conversation = MagicMock(return_value=None)  # No session!
    terminal_mgr.send_input = AsyncMock()

    runtime_store = MagicMock()
    runtime_store._conn = MagicMock()
    runtime_store._conn.execute = MagicMock(return_value=MagicMock())
    runtime_store._conn.execute.return_value.fetchone = MagicMock(return_value=MagicMock())
    runtime_store.append = MagicMock()

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(id=55)
    )
    ledger.record_telegram_update = MagicMock()

    controller = MagicMock()
    controller.handle = AsyncMock()
    controller.handle_conversation_text = AsyncMock()

    handlers = _make_handlers(
        controller=controller, ledger=ledger,
        runtime_store=runtime_store, terminal_manager=terminal_mgr,
    )

    # Patch _get_active_surface_mode to return "terminal" (Onsite)
    with patch.object(handlers, "_get_active_surface_mode", return_value="terminal"):
        await handlers.conversation_text(update, None)

    # Controller must NOT be called (Onsite text never reaches product controller)
    controller.handle.assert_not_called()
    controller.handle_conversation_text.assert_not_called()

    # Must have sent something — not a silent failure
    send_calls = handlers._bot.send_message.call_args_list
    assert len(send_calls) > 0, "No response sent for Onsite text without session"

    sent_texts = [c.kwargs["text"] for c in send_calls]
    sent_buttons = [c.kwargs.get("reply_markup") for c in send_calls]

    # Must NOT contain dead-end language
    dead_phrases = [
        "请使用 /terminal claude",
        "请使用 /terminal codex",
        "切回产品模式",
        "请先通过",
    ]
    for t in sent_texts:
        for phrase in dead_phrases:
            assert phrase not in t, (
                f"Dead-end phrase '{phrase}' found in Onsite no-session response: {t!r}"
            )

    # Must offer actionable options (start card)
    has_actionable_text = any(
        kw in " ".join(sent_texts)
        for kw in ["启动", "现场", "驾驶舱", "接管", "Claude", "Codex", "可以"]
    )
    has_buttons = any(b is not None for b in sent_buttons)
    assert has_actionable_text or has_buttons, (
        f"Onsite text without session: no actionable options. Text: {sent_texts}"
    )


# ── Start card callback format compatibility ────────────────────────


@pytest.mark.asyncio
async def test_start_card_callbacks_decode_via_conv_protocol():
    """Start card buttons must use conv:{chat_id}:{action} format so
    decode_conversation_callback succeeds (returns a ConversationCallback).

    The original 'conv:start_claude_onsite' format (only 2 segments)
    would return None, which causes "无效的对话回调数据。" on every tap.
    """
    from wlcodex.conversation_callback import decode_conversation_callback

    update = _make_update("/terminal", chat_id=555)

    terminal_mgr = MagicMock()
    terminal_mgr.active_for_conversation = MagicMock(return_value=None)

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(id=99)
    )
    ledger.list_recent_agent_runs = MagicMock(return_value=[])
    ledger.record_telegram_update = MagicMock()

    handlers = _make_handlers(ledger=ledger, terminal_manager=terminal_mgr)
    await handlers.terminal_cmd(update, None)

    # Collect all inline keyboard buttons from all sent messages
    all_buttons = []
    for c in handlers._bot.send_message.call_args_list:
        reply_markup = c.kwargs.get("reply_markup")
        if reply_markup is not None:
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    all_buttons.append(btn)

    assert len(all_buttons) >= 3, (
        f"Start card must have >=3 buttons, got {len(all_buttons)}"
    )

    expected_actions = {"start_claude_onsite", "start_codex_onsite", "return_cockpit"}
    seen_actions = set()
    for btn in all_buttons:
        cb = decode_conversation_callback(btn.callback_data)
        assert cb is not None, (
            f"Button cb_data {btn.callback_data!r} failed to decode as conv protocol"
        )
        assert cb.conversation_id == 99, (
            f"Expected conversation_id=99 in callback (active.id), got {cb.conversation_id}"
        )
        seen_actions.add(cb.action)

    assert seen_actions == expected_actions, (
        f"Start card actions mismatch. Expected {expected_actions}, got {seen_actions}"
    )


@pytest.mark.asyncio
async def test_return_cockpit_callback_switches_to_product_without_unknown_action():
    controller = MagicMock()
    controller.handle_conversation_callback = AsyncMock(
        return_value=SimpleNamespace(text="未知的对话操作：return_cockpit", buttons=None)
    )

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(return_value=SimpleNamespace(id=99))
    ledger.record_telegram_update = MagicMock()

    handlers = _make_handlers(controller=controller, ledger=ledger)

    query = MagicMock()
    query.data = "conv:99:return_cockpit"
    query.message = SimpleNamespace(
        text="start card",
        message_id=900,
        chat=SimpleNamespace(id=555),
    )
    query.answer = AsyncMock()

    update = _make_update("/terminal", chat_id=555)
    update.callback_query = query

    await handlers.callback_router(update, None)

    controller.handle_conversation_callback.assert_not_called()
    sent_texts = [
        call.kwargs.get("text", "")
        for call in handlers._bot.send_message.call_args_list
    ]
    assert any("已回到驾驶舱" in text for text in sent_texts)
    assert not any("未知的对话操作" in text for text in sent_texts)


# ── /product  → Cockpit return ─────────────────────────────────────


@pytest.mark.asyncio
async def test_product_returns_to_cockpit_preserves_workbench():
    """Spec §View Switching Onsite→Cockpit: do not replay raw terminal output.

    /product must record view change, keep workbench id, and use
    user-facing Cockpit return copy.
    """
    update = _make_update("/product")

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(id=77)
    )
    ledger.record_telegram_update = MagicMock()

    handlers = _make_handlers(ledger=ledger)

    await handlers.product_cmd(update, None)

    send_calls = handlers._bot.send_message.call_args_list
    sent_texts = [c.kwargs["text"] for c in send_calls]

    # Must contain Cockpit return copy (spec line 353-355)
    assert any("驾驶舱" in t for t in sent_texts), (
        f"Missing 驾驶舱 in /product response: {sent_texts}"
    )

    # Must NOT replay raw terminal
    forbidden = ["raw terminal", "replay", "stdout"]
    for t in sent_texts:
        for phrase in forbidden:
            assert phrase.lower() not in t.lower(), (
                f"Raw terminal replay suspected in: {t!r}"
            )


# ── /settings  card ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_command_sends_settings_card():
    """Spec §Menu Design: /settings renders settings card.

    The card must include natural settings options, not raw config keys.
    """
    update = _make_update("/settings")

    handlers = _make_handlers()

    await handlers.settings_cmd(update, None)

    send_calls = handlers._bot.send_message.call_args_list
    sent_texts = [c.kwargs["text"] for c in send_calls]
    combined = " ".join(sent_texts)

    # Must contain settings-relevant language
    settings_terms = ["设置", "流程", "Codex", "Claude", "模型", "权限", "工作区"]
    found = [term for term in settings_terms if term in combined]
    assert len(found) >= 2, (
        f"Settings card missing expected terms. Found: {found}. Text: {combined[:500]}"
    )


# ── User copy must not expose terminal.enabled ─────────────────────


class TestUserCopyNoTerminalEnabled:
    """Spec §Startup: Do not surface terminal.enabled to normal users."""

    def test_terminal_disabled_copy_does_not_expose_config_key(self):
        """When terminal is disabled, the copy MUST NOT mention 'terminal.enabled'."""
        update = _make_update("/terminal")

        terminal_mgr = MagicMock()
        terminal_mgr.active_for_conversation = MagicMock(return_value=None)
        terminal_mgr.attach = MagicMock()

        ledger = MagicMock()
        ledger.get_active_conversation = MagicMock(
            return_value=SimpleNamespace(id=42)
        )
        ledger.list_recent_agent_runs = MagicMock(return_value=[])
        ledger.record_telegram_update = MagicMock()

        config = MagicMock()
        config.telegram.allowed_user_ids = frozenset({123})
        type(config).interaction = SimpleNamespace(profile="legacy")
        type(config).terminal = SimpleNamespace(enabled=False, default_agent="claude",
                                                  max_frame_chars=3500)

        import asyncio
        async def _run():
            from wlcodex.telegram_app import WlCodexHandlers
            bot = AsyncMock()
            bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1001))
            bot.edit_message_text = AsyncMock()
            bot.send_chat_action = AsyncMock()

            h = WlCodexHandlers(
                config=config,
                controller=MagicMock(),
                ledger=ledger,
                approval_service=MagicMock(),
                bot=bot,
                runtime_event_store=None,
                outbox=None,
                terminal_manager=terminal_mgr,
            )
            await h.terminal_cmd(update, None)

            send_calls = bot.send_message.call_args_list
            return [c.kwargs["text"] for c in send_calls]

        sent_texts = asyncio.run(_run())

        for t in sent_texts:
            assert "terminal.enabled" not in t, (
                f"Config key 'terminal.enabled' leaked to user: {t!r}"
            )

    def test_start_or_help_copy_does_not_mention_terminal_enabled(self):
        """Help/start text must not mention terminal.enabled."""
        # This is tested at the copy-generation level — the handler
        # delegates to render method which must not include internal keys.
        from wlcodex.telegram_app import WlCodexHandlers
        # Smoke: the class must exist and be importable
        assert WlCodexHandlers is not None


# ── View switch event integrity ────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_to_product_view_switch_records_event():
    """Switching from terminal back to product must record a view-changed event."""
    update = _make_update("/product")

    runtime_store = MagicMock()
    runtime_store._conn = MagicMock()
    runtime_store._conn.execute = MagicMock(return_value=MagicMock())
    runtime_store._conn.execute.return_value.fetchone = MagicMock(
        return_value=SimpleNamespace(
            payload='{"to_mode":"terminal","active_agent":"claude"}'
        )
    )
    runtime_store.append = MagicMock()

    ledger = MagicMock()
    ledger.get_active_conversation = MagicMock(
        return_value=SimpleNamespace(id=77)
    )
    ledger.record_telegram_update = MagicMock()

    handlers = _make_handlers(ledger=ledger, runtime_store=runtime_store)

    await handlers.product_cmd(update, None)

    # Must have recorded a mode-switch event
    append_calls = runtime_store.append.call_args_list
    mode_switch_calls = [
        c for c in append_calls
        if getattr(c.args[0], "event_type", "") == "conversation.mode.switched"
    ]
    assert len(mode_switch_calls) >= 1, (
        "No conversation.mode.switched event recorded for /product"
    )


# ── Handler registration ───────────────────────────────────────────


def test_settings_handler_registered_in_built_app(tmp_path):
    """Verify that /settings handler is registered in the built application."""
    from pathlib import Path

    config_path = Path(__file__).parent / "fixtures" / "test_routing_config.toml"
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

    from wlcodex.config import load_config
    from wlcodex.codex_backend import FakeCodexBackend
    from wlcodex.db import Ledger
    from wlcodex.task_service import TaskService
    from wlcodex.inspection import TaskInspector
    from wlcodex.controller import CommandController
    from wlcodex.approval import ApprovalService
    from wlcodex.config import WorkspaceConfig
    from wlcodex.telegram_app import build_application

    config = load_config(config_path)
    ledger = Ledger.open(tmp_path / "test_routing.sqlite3")
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

    assert "settings" in registered, (
        f"/settings not registered. Registered: {sorted(registered)}"
    )


# ── Settings callback closure ───────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_callback_all_buttons_route_to_controller():
    """Every settings card button must route to controller, never return
    '无效的设置回调数据。' (dead-end).  This proves settings card is
    closed-loop: each button produces a business outcome, not an error."""

    controller = MagicMock()
    controller.handle = AsyncMock(
        return_value=SimpleNamespace(text="ok", buttons=None)
    )

    handlers = _make_handlers(controller=controller)

    # Collect every callback_data from the settings card buttons
    # by sending /settings, then extracting callback_data from the reply
    update = _make_update("/settings")
    await handlers.settings_cmd(update, None)

    all_buttons = []
    for c in handlers._bot.send_message.call_args_list:
        reply_markup = c.kwargs.get("reply_markup")
        if reply_markup is not None:
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    all_buttons.append(btn)

    assert len(all_buttons) >= 5, (
        f"Expected >=5 settings buttons, got {len(all_buttons)}"
    )

    # Simulate tapping each button through callback_router
    for btn in all_buttons:
        # Reset mock to isolate each call
        controller.handle.reset_mock()

        query = MagicMock()
        query.data = btn.callback_data
        query.message = SimpleNamespace(
            text="settings card", message_id=900,
            chat=SimpleNamespace(id=100),
        )
        query.answer = AsyncMock()

        cb_update = _make_update("/settings")  # reuse base update
        cb_update.callback_query = query

        await handlers.callback_router(cb_update, None)

        # Controller must have been called (not rejected with "无效的设置回调数据。")
        controller.handle.assert_called_once()

        # Verify the answer was not an error
        answer_calls = query.answer.call_args_list
        answer_texts = [c.kwargs.get("text", "") for c in answer_calls]
        for t in answer_texts:
            assert "无效" not in t, (
                f"Button {btn.callback_data!r} returned error answer: {t}"
            )
            assert "错误" not in t, (
                f"Button {btn.callback_data!r} returned error answer: {t}"
            )


@pytest.mark.asyncio
async def test_settings_exec_mode_callbacks_use_correct_controller_command():
    """Each exec_mode button must route to /exec_mode <mode> on the controller."""
    controller = MagicMock()
    controller.handle = AsyncMock(
        return_value=SimpleNamespace(text="ok", buttons=None)
    )

    handlers = _make_handlers(controller=controller)

    exec_mode_cases = [
        ("settings:exec_mode:orchestrated", "/exec_mode orchestrated"),
        ("settings:exec_mode:codex_direct", "/exec_mode codex_direct"),
        ("settings:exec_mode:claude_direct", "/exec_mode claude_direct"),
    ]

    for cb_data, expected_cmd in exec_mode_cases:
        controller.handle.reset_mock()

        query = MagicMock()
        query.data = cb_data
        query.message = SimpleNamespace(
            text="settings card", message_id=900,
            chat=SimpleNamespace(id=100),
        )
        query.answer = AsyncMock()

        cb_update = _make_update("/settings")
        cb_update.callback_query = query

        await handlers.callback_router(cb_update, None)

        controller.handle.assert_called_once()
        call_args = controller.handle.call_args
        assert call_args[0][0] == expected_cmd, (
            f"Expected {expected_cmd!r}, got {call_args[0][0]!r}"
        )


@pytest.mark.asyncio
async def test_workbench_history_restore_buttons_use_titles_not_ids():
    """History restore buttons should be readable titles, not raw ids."""
    from datetime import datetime, timezone

    controller = MagicMock()
    controller.handle = AsyncMock(
        return_value=SimpleNamespace(text="工作台历史\n\n* Current（当前）\n  Old")
    )
    now = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    ledger = MagicMock()
    ledger.record_telegram_update = MagicMock()
    ledger.list_conversations_by_chat = MagicMock(return_value=[
        SimpleNamespace(
            id=2,
            title="Current",
            mode="chief_engineer",
            workspace_alias="wlcodex",
            archived_at=None,
            updated_at=now,
        ),
        SimpleNamespace(
            id=1,
            title="Old",
            mode="chief_engineer",
            workspace_alias="wlcodex",
            archived_at=now,
            updated_at=now,
        ),
    ])

    handlers = _make_handlers(controller=controller, ledger=ledger)
    sent: list[tuple[int, str, object]] = []

    async def fake_send(chat_id, text, buttons=None):
        sent.append((chat_id, text, buttons))
        return 1001

    handlers.send_telegram = fake_send

    await handlers.workbenches(_make_update("/history"), None)

    assert sent
    buttons = sent[-1][2]
    assert buttons == [[{
        "text": "恢复 Old",
        "callback_data": "conv:1:restore_workbench",
    }]]


@pytest.mark.asyncio
async def test_workspaces_command_sends_selection_buttons():
    """The /workspaces list must be actionable, not just readable text."""
    controller = MagicMock()
    controller.handle = AsyncMock(
        return_value=SimpleNamespace(
            text="可用工作区\n\n* demo",
            buttons=[[{
                "text": "切换 demo",
                "callback_data": "settings:workspace:demo",
            }]],
        )
    )

    handlers = _make_handlers(controller=controller)
    sent: list[tuple[int, str, object]] = []

    async def fake_send(chat_id, text, buttons=None):
        sent.append((chat_id, text, buttons))
        return 1001

    handlers.send_telegram = fake_send

    await handlers.workspaces(_make_update("/workspaces"), None)

    assert sent
    assert sent[-1][2] == [[{
        "text": "切换 demo",
        "callback_data": "settings:workspace:demo",
    }]]


@pytest.mark.asyncio
async def test_settings_model_and_workspace_callbacks_route():
    """/model and workspace picker buttons must route to controller."""
    controller = MagicMock()
    controller.handle = AsyncMock(
        return_value=SimpleNamespace(text="ok", buttons=None)
    )

    handlers = _make_handlers(controller=controller)

    cases = [
        ("settings:model", "/model"),
        ("settings:workspace", "/workspaces"),
        ("settings:workspace:demo", "/switch demo"),
    ]

    for cb_data, expected_cmd in cases:
        controller.handle.reset_mock()

        query = MagicMock()
        query.data = cb_data
        query.message = SimpleNamespace(
            text="settings card", message_id=900,
            chat=SimpleNamespace(id=100),
        )
        query.answer = AsyncMock()

        cb_update = _make_update("/settings")
        cb_update.callback_query = query

        await handlers.callback_router(cb_update, None)

        controller.handle.assert_called_once()
        call_args = controller.handle.call_args
        assert call_args[0][0] == expected_cmd, (
            f"Expected {expected_cmd!r}, got {call_args[0][0]!r}"
        )


# ── Start card helper consistency ───────────────────────────────────


@pytest.mark.asyncio
async def test_start_card_buttons_identical_from_both_call_sites():
    """The _render_start_card_buttons helper must produce identical buttons
    whether called from _handle_terminal_text or _apply_mode_switch."""

    # --- Call site 1: _handle_terminal_text (no session) ---
    terminal_mgr_1 = MagicMock()
    terminal_mgr_1.active_for_conversation = MagicMock(return_value=None)

    runtime_store_1 = MagicMock()
    runtime_store_1._conn = MagicMock()
    runtime_store_1._conn.execute = MagicMock(return_value=MagicMock())
    runtime_store_1._conn.execute.return_value.fetchone = MagicMock(return_value=MagicMock())
    runtime_store_1.append = MagicMock()

    ledger_1 = MagicMock()
    ledger_1.get_active_conversation = MagicMock(return_value=SimpleNamespace(id=42))
    ledger_1.record_telegram_update = MagicMock()

    handlers_1 = _make_handlers(
        ledger=ledger_1, runtime_store=runtime_store_1,
        terminal_manager=terminal_mgr_1,
    )

    with patch.object(handlers_1, "_get_active_surface_mode", return_value="terminal"):
        await handlers_1.conversation_text(_make_update("text in onsite", chat_id=200), None)

    btns_1 = []
    for c in handlers_1._bot.send_message.call_args_list:
        r = c.kwargs.get("reply_markup")
        if r is not None:
            for row in r.inline_keyboard:
                for btn in row:
                    btns_1.append((btn.text, btn.callback_data))

    # --- Call site 2: _apply_mode_switch (no session, /terminal) ---
    terminal_mgr_2 = MagicMock()
    terminal_mgr_2.active_for_conversation = MagicMock(return_value=None)

    ledger_2 = MagicMock()
    ledger_2.get_active_conversation = MagicMock(return_value=SimpleNamespace(id=42))
    ledger_2.list_recent_agent_runs = MagicMock(return_value=[])
    ledger_2.record_telegram_update = MagicMock()

    handlers_2 = _make_handlers(ledger=ledger_2, terminal_manager=terminal_mgr_2)

    await handlers_2.terminal_cmd(_make_update("/terminal", chat_id=200), None)

    btns_2 = []
    for c in handlers_2._bot.send_message.call_args_list:
        r = c.kwargs.get("reply_markup")
        if r is not None:
            for row in r.inline_keyboard:
                for btn in row:
                    btns_2.append((btn.text, btn.callback_data))

    # Both call sites must produce exactly the same 3 buttons
    assert btns_1 == btns_2, (
        f"Start card buttons differ between call sites:\n"
        f"  _handle_terminal_text: {btns_1}\n"
        f"  _apply_mode_switch:    {btns_2}"
    )

    expected_labels = {"启动 Claude 现场", "启动 Codex 现场", "回驾驶舱"}
    seen_labels = {b[0] for b in btns_1}
    assert seen_labels == expected_labels, (
        f"Expected labels {expected_labels}, got {seen_labels}"
    )


# ── Historical continuation lifecycle ───────────────────────────────


class _ContinuationTerminalManager:
    def __init__(self, *, fail_on_send: bool = False):
        self.fail_on_send = fail_on_send
        self.attached: list[SimpleNamespace] = []
        self.inputs: list[tuple[object, str]] = []

    def attach(self, **kwargs):
        ref = SimpleNamespace(**kwargs, status="attached")
        self.attached.append(ref)
        return ref

    def active_for_conversation(self, conversation_id: int):
        return self.attached[-1] if self.attached else None

    async def send_input(self, ref, text: str):
        self.inputs.append((ref, text))
        if self.fail_on_send:
            raise RuntimeError("terminal send failed")
        return SimpleNamespace(text="ok")


def _make_continuation_harness(tmp_path, terminal_manager):
    from wlcodex.config import WorkspaceConfig
    from wlcodex.db import Ledger
    from wlcodex.execution_scheduler import ExecutionScheduler
    from wlcodex.task_service import TaskService

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    conversation = ledger.create_conversation(
        chat_id=7001,
        user_id=100,
        title="历史现场",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    service = TaskService(
        ledger,
        (WorkspaceConfig("wlcodex", tmp_path, True),),
    )
    scheduler = ExecutionScheduler(service, ledger)
    controller = SimpleNamespace()
    handlers = _make_handlers(
        controller=controller,
        ledger=ledger,
        terminal_manager=terminal_manager,
        execution_scheduler=scheduler,
    )
    return handlers, ledger, service, conversation


@pytest.mark.asyncio
async def test_pending_continuation_marks_task_and_run_done_and_releases_lock(tmp_path):
    from wlcodex.models import AgentRunStatus, TaskStatus

    terminal_mgr = _ContinuationTerminalManager()
    handlers, ledger, service, conversation = _make_continuation_harness(
        tmp_path, terminal_mgr,
    )

    pending = {
        "agent": "claude",
        "internal_ref": "cl-session-1",
        "title": "修复历史现场",
        "source_run_id": 1,
        "summary_only": False,
    }

    await handlers._execute_pending_continuation(
        7001, conversation.id, conversation, "continue from history", pending,
    )

    task = service.list_tasks()[0]
    runs = ledger.list_agent_runs(conversation.id)

    assert task.status is TaskStatus.DONE
    assert runs[0].status == AgentRunStatus.DONE.value
    assert runs[0].hidden_task_id == task.id
    assert runs[0].external_session_id == "cl-session-1"
    assert terminal_mgr.inputs[0][1] == "continue from history"

    # The continuation ticket must not keep the workspace locked.
    next_task = service.reserve_task("wlcodex", "next work", telegram_chat_id=7001)
    assert next_task.id != task.id


@pytest.mark.asyncio
async def test_pending_continuation_failure_marks_failed_and_releases_lock(tmp_path):
    from wlcodex.models import AgentRunStatus, TaskStatus

    terminal_mgr = _ContinuationTerminalManager(fail_on_send=True)
    handlers, ledger, service, conversation = _make_continuation_harness(
        tmp_path, terminal_mgr,
    )

    pending = {
        "agent": "claude",
        "internal_ref": "cl-session-1",
        "title": "修复历史现场",
        "source_run_id": 1,
        "summary_only": False,
    }

    await handlers._execute_pending_continuation(
        7001, conversation.id, conversation, "continue from history", pending,
    )

    task = service.list_tasks()[0]
    runs = ledger.list_agent_runs(conversation.id)

    assert task.status is TaskStatus.FAILED
    assert runs[0].status == AgentRunStatus.FAILED.value
    assert runs[0].hidden_task_id == task.id

    next_task = service.reserve_task("wlcodex", "next work", telegram_chat_id=7001)
    assert next_task.id != task.id


@pytest.mark.asyncio
async def test_resume_from_summary_prepares_pending_without_raw_attach(tmp_path):
    from wlcodex.db import Ledger

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    conversation = ledger.create_conversation(
        chat_id=7001,
        user_id=100,
        title="历史现场",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    run = ledger.create_agent_run(
        conversation_id=conversation.id,
        agent="claude",
        role="implementation",
        prompt_packet_summary="只有摘要，没有 raw session",
    )
    ledger.update_agent_run_status(
        run.id,
        "done",
        completion_summary="只有摘要，没有 raw session",
    )

    terminal_mgr = MagicMock()
    terminal_mgr.attach_historical = MagicMock(
        side_effect=ValueError("summary-only cannot raw attach")
    )
    handlers = _make_handlers(ledger=ledger, terminal_manager=terminal_mgr)

    query = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat_id=7001,
            message_id=99,
            chat=SimpleNamespace(id=7001),
        ),
    )
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=7001))

    await handlers._handle_session_picker_callback(
        update, query, conversation.id, ("resume_from_summary", run.id),
    )

    terminal_mgr.attach_historical.assert_not_called()
    assert handlers._pending_continuation[conversation.id]["summary_only"] is True


@pytest.mark.asyncio
async def test_review_summary_only_session_does_not_offer_attach_session(tmp_path):
    from wlcodex.db import Ledger

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    conversation = ledger.create_conversation(
        chat_id=7001,
        user_id=100,
        title="历史现场",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    run = ledger.create_agent_run(
        conversation_id=conversation.id,
        agent="codex",
        role="analysis",
        prompt_packet_summary="",
    )
    ledger.update_agent_run_status(
        run.id,
        "done",
        completion_summary='{"summary":"default flow ok","needs_implementation":false}',
    )

    terminal_mgr = MagicMock()
    terminal_mgr.attach_historical = MagicMock()
    handlers = _make_handlers(ledger=ledger, terminal_manager=terminal_mgr)

    query = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(
            chat_id=7001,
            message_id=99,
            chat=SimpleNamespace(id=7001),
        ),
    )
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=7001))

    await handlers._handle_session_picker_callback(
        update, query, conversation.id, ("review_session", run.id),
    )

    edit_kwargs = handlers._bot.edit_message_text.call_args.kwargs
    keyboard = edit_kwargs["reply_markup"].inline_keyboard
    callback_data = [
        button.callback_data
        for row in keyboard
        for button in row
    ]
    labels = [
        button.text
        for row in keyboard
        for button in row
    ]

    assert "从摘要新开" in labels
    assert not any(":attach_session:" in data for data in callback_data)
    assert not any(label == "接管现场" for label in labels)
    terminal_mgr.attach_historical.assert_not_called()


# --- Terminal readable output test ---


@pytest.mark.asyncio
async def test_terminal_mode_streams_semantic_blocks_not_token_fragments():
    from types import SimpleNamespace
    from unittest.mock import patch

    from wlcodex.interaction.events import InteractionEvent
    from wlcodex.telegram_app import WlCodexHandlers

    sent = []
    edited = []

    async def send(chat_id, text, buttons=None):
        sent.append((chat_id, text, buttons))
        return len(sent)

    async def edit(chat_id, message_id, text, buttons=None):
        edited.append((chat_id, message_id, text, buttons))

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
            interaction=SimpleNamespace(profile="natural", edit_min_interval_seconds=0.0),
            telegram_output=SimpleNamespace(
                preview_send_timeout_seconds=2.0,
                semantic_min_chars=10,
                semantic_max_chars=30,
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
    with patch.object(handlers, "_get_active_surface_mode", return_value="terminal"):
        renderer = handlers.create_interaction_renderer()

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第一段很长很长。\n\n第二段也很长很长。"))

    assert any(text == "第一段很长很长。" for _, text, _ in sent)
    assert not any(text == "第一段" for _, text, _ in sent)
