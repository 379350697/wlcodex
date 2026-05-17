"""Tests for conversation command parsing and routing."""

import pytest
from wlcodex.router import (
    ParseError,
    NewConversationCommand,
    CodexDirectCommand,
    ClaudeDirectCommand,
    AutoModeCommand,
    StopCurrentCommand,
    SwitchWorkspaceCommand,
    ModelCommand,
    VerifyCommand,
    StartTaskCommand,
    parse_command,
)


def test_parse_new_conversation_command() -> None:
    cmd = parse_command("/new")
    assert isinstance(cmd, NewConversationCommand)

    cmd2 = parse_command("/new 新对话标题")
    assert isinstance(cmd2, NewConversationCommand)
    assert cmd2.title == "新对话标题"


def test_parse_codex_direct_command() -> None:
    cmd = parse_command("/codex 分析这个模块")
    assert isinstance(cmd, CodexDirectCommand)
    assert cmd.prompt == "分析这个模块"


def test_parse_claude_direct_command() -> None:
    cmd = parse_command("/claude 修改 README")
    assert isinstance(cmd, ClaudeDirectCommand)
    assert cmd.prompt == "修改 README"


def test_parse_auto_command() -> None:
    cmd = parse_command("/auto 修复登录 bug")
    assert isinstance(cmd, AutoModeCommand)
    assert cmd.prompt == "修复登录 bug"


def test_legacy_task_command_still_works() -> None:
    cmd = parse_command("/task wlcodex 修复 bug")
    assert isinstance(cmd, StartTaskCommand)
    assert cmd.workspace_alias == "wlcodex"


def test_parse_stop_command() -> None:
    cmd = parse_command("/stop")
    assert isinstance(cmd, StopCurrentCommand)


def test_parse_switch_command() -> None:
    cmd = parse_command("/switch wlcodex")
    assert isinstance(cmd, SwitchWorkspaceCommand)
    assert cmd.workspace_alias == "wlcodex"


def test_parse_switch_missing_workspace() -> None:
    with pytest.raises(ParseError):
        parse_command("/switch")


def test_parse_model_command() -> None:
    cmd = parse_command("/model")
    assert isinstance(cmd, ModelCommand)
    assert cmd.model_name == ""

    cmd2 = parse_command("/model gpt-5.2")
    assert isinstance(cmd2, ModelCommand)
    assert cmd2.model_name == "gpt-5.2"


def test_parse_verify_command() -> None:
    cmd = parse_command("/verify")
    assert isinstance(cmd, VerifyCommand)
    assert cmd.prompt == ""

    cmd2 = parse_command("/verify 确认这个修改")
    assert isinstance(cmd2, VerifyCommand)
    assert cmd2.prompt == "确认这个修改"


def test_unknown_command_raises() -> None:
    with pytest.raises(ParseError):
        parse_command("/unknown")


def test_parse_stop_with_args_ignores() -> None:
    cmd = parse_command("/stop 随便什么东西")
    assert isinstance(cmd, StopCurrentCommand)
