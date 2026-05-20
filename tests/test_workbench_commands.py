"""Command parsing tests for /settings and compatibility commands.

Covers /settings addition while preserving all existing compatibility commands:
  /terminal, /product, /codex, /claude, /auto, /terminal <agent|subcommand>
"""

import pytest

from wlcodex.router import (
    AutoModeCommand,
    ClaudeDirectCommand,
    CodexDirectCommand,
    ModeSwitchCommand,
    ParseError,
    SettingsCommand,
    TerminalSubCommand,
    parse_command,
)


# --- /settings (new) ---


def test_settings_command_parses():
    cmd = parse_command("/settings")
    assert isinstance(cmd, SettingsCommand)


# --- /terminal (compatibility) ---


def test_terminal_command_parses_mode_switch():
    cmd = parse_command("/terminal")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "terminal"
    assert cmd.agent == ""


def test_terminal_claude_parses_mode_switch_with_agent():
    cmd = parse_command("/terminal claude")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "terminal"
    assert cmd.agent == "claude"


def test_terminal_codex_parses_mode_switch_with_agent():
    cmd = parse_command("/terminal codex")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "terminal"
    assert cmd.agent == "codex"


def test_terminal_tail_parses_subcommand():
    cmd = parse_command("/terminal tail")
    assert isinstance(cmd, TerminalSubCommand)
    assert cmd.subcommand == "tail"


def test_terminal_pause_parses_subcommand():
    cmd = parse_command("/terminal pause")
    assert isinstance(cmd, TerminalSubCommand)
    assert cmd.subcommand == "pause"


def test_terminal_detach_parses_subcommand():
    cmd = parse_command("/terminal detach")
    assert isinstance(cmd, TerminalSubCommand)
    assert cmd.subcommand == "detach"


def test_terminal_product_switches_to_product():
    cmd = parse_command("/terminal product")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "product"


# --- /product (compatibility) ---


def test_product_command_parses_mode_switch():
    cmd = parse_command("/product")
    assert isinstance(cmd, ModeSwitchCommand)
    assert cmd.mode == "product"


# --- /codex <prompt> (compatibility) ---


def test_codex_direct_command_parses():
    cmd = parse_command("/codex 分析这个模块")
    assert isinstance(cmd, CodexDirectCommand)
    assert cmd.prompt == "分析这个模块"


# --- /claude <prompt> (compatibility) ---


def test_claude_direct_command_parses():
    cmd = parse_command("/claude 修改 README")
    assert isinstance(cmd, ClaudeDirectCommand)
    assert cmd.prompt == "修改 README"


# --- /auto <prompt> (compatibility) ---


def test_auto_mode_command_parses():
    cmd = parse_command("/auto 修复登录 bug")
    assert isinstance(cmd, AutoModeCommand)
    assert cmd.prompt == "修复登录 bug"


# --- Parser determinism ---


def test_parser_is_deterministic():
    a = parse_command("/terminal claude")
    b = parse_command("/terminal claude")
    assert a == b


# --- Settings edge cases ---


def test_settings_with_extra_text_still_parses():
    """Extra text after /settings is ignored (no arguments needed)."""
    cmd = parse_command("/settings extra stuff")
    assert isinstance(cmd, SettingsCommand)


def test_settings_no_space_is_rejected():
    """Ensure /settingsblah (no space after /settings) raises ParseError."""
    with pytest.raises(ParseError):
        parse_command("/settingsblah")
