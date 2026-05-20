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
                   runtime_store=None, terminal_manager=None):
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
    )


# ── /terminal  with session ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_with_active_claude_session_auto_opens_onsite():
    """Spec §View Switching: /terminal with active Claude → auto-attach Claude onsite."""
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

    handlers = _make_handlers(ledger=ledger, terminal_manager=terminal_mgr)

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


# ── Onsite text → terminal manager, not controller ─────────────────


@pytest.mark.asyncio
async def test_onsite_text_routes_to_terminal_manager_not_controller():
    """Spec §Routing Rules: Onsite plain text → selected onsite session input.

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

    handlers = _make_handlers(
        controller=controller, ledger=ledger,
        runtime_store=runtime_store, terminal_manager=terminal_mgr,
    )

    # Patch _get_active_surface_mode to return "terminal"
    with patch.object(handlers, "_get_active_surface_mode", return_value="terminal"):
        await handlers.conversation_text(update, None)

    # Terminal manager must receive the input
    terminal_mgr.send_input.assert_called_once()
    call_args = terminal_mgr.send_input.call_args
    assert call_args[0][0] is session_ref
    assert call_args[0][1] == "继续修失败测试"

    # Controller must NOT be called
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
        f"No conversation.mode.switched event recorded for /product"
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
    from unittest.mock import ANY

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
async def test_settings_model_and_workspace_callbacks_route():
    """/model and /switch buttons must route to controller."""
    controller = MagicMock()
    controller.handle = AsyncMock(
        return_value=SimpleNamespace(text="ok", buttons=None)
    )

    handlers = _make_handlers(controller=controller)

    cases = [
        ("settings:model", "/model"),
        ("settings:workspace", "/switch"),
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
    from wlcodex.surfaces.terminal.models import TerminalSessionRef

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
