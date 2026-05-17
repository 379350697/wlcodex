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
