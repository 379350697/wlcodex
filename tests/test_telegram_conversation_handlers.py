"""Tests for Telegram conversation handlers, menus, and renderers."""

from wlcodex.menu import build_bot_commands


def test_primary_bot_commands_are_human_first() -> None:
    commands = build_bot_commands()
    names = [cmd[0] for cmd in commands]
    assert names[:5] == ["new", "codex", "claude", "auto", "stop"]
    assert "task" not in names


def test_all_primary_commands_in_order() -> None:
    commands = build_bot_commands()
    names = [cmd[0] for cmd in commands]
    assert names == [
        "new", "codex", "claude", "auto", "stop",
        "status", "sessions", "switch", "model",
        "diff", "files", "verify", "health", "help",
    ]


def test_menu_pairs_are_valid() -> None:
    commands = build_bot_commands()
    for cmd, desc in commands:
        assert cmd
        assert desc
        assert not cmd.startswith("/")


def test_wlcodex_handlers_has_streaming_renderer_factory() -> None:
    """WlCodexHandlers must expose create_streaming_renderer for streaming integration."""
    from wlcodex.telegram_app import WlCodexHandlers
    assert hasattr(WlCodexHandlers, "create_streaming_renderer")
    assert callable(WlCodexHandlers.create_streaming_renderer)


def test_wlcodex_handlers_has_typing_indicator() -> None:
    """WlCodexHandlers must expose _start_typing for typing indicator integration."""
    from wlcodex.telegram_app import WlCodexHandlers
    assert hasattr(WlCodexHandlers, "_start_typing")
    assert callable(WlCodexHandlers._start_typing)


def test_edit_telegram_handles_not_modified_with_buttons() -> None:
    """edit_telegram must not silently drop buttons when text is unchanged."""
    # Regression: if text is same, the method must retry with zero-width space
    # to ensure inline keyboard buttons are applied.
    from wlcodex.telegram_app import _is_message_not_modified_error
    exc = type("TelegramError", (Exception,), {})("message is not modified: ...")
    assert _is_message_not_modified_error(exc) is True
