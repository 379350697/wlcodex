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
    StatusCommand,
    TraceCommand,
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


def test_parse_auto_command_accepts_direct_chinese_quote() -> None:
    cmd = parse_command("/auto「云服务器：\n- IP: 127.0.0.1\n请核验部署是否生效」")

    assert isinstance(cmd, AutoModeCommand)
    assert cmd.prompt == "云服务器：\n- IP: 127.0.0.1\n请核验部署是否生效"


def test_parse_auto_command_accepts_unclosed_direct_chinese_quote() -> None:
    cmd = parse_command("/auto「云服务器：\n- IP: 127.0.0.1\n请核验部署是否生效")

    assert isinstance(cmd, AutoModeCommand)
    assert cmd.prompt == "云服务器：\n- IP: 127.0.0.1\n请核验部署是否生效"


def test_parse_auto_command_does_not_match_longer_command_name() -> None:
    with pytest.raises(ParseError):
        parse_command("/autox核验部署")


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


def test_parse_status_and_trace_commands() -> None:
    assert isinstance(parse_command("/status"), StatusCommand)
    assert isinstance(parse_command("/trace"), TraceCommand)


def test_unknown_command_raises() -> None:
    with pytest.raises(ParseError):
        parse_command("/unknown")


def test_parse_stop_with_args_ignores() -> None:
    cmd = parse_command("/stop 随便什么东西")
    assert isinstance(cmd, StopCurrentCommand)


@pytest.mark.asyncio
async def test_lightweight_greeting_is_short_and_hides_metadata(tmp_path):
    from pathlib import Path
    from wlcodex.codex_backend import FakeCodexBackend
    from wlcodex.config import WorkspaceConfig
    from wlcodex.controller import CommandController
    from wlcodex.db import Ledger
    from wlcodex.inspection import TaskInspector
    from wlcodex.task_service import TaskService

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    service = TaskService(ledger, (
        WorkspaceConfig("demo", Path("/tmp/demo"), True),
    ))
    inspector = TaskInspector(ledger, tmp_path / "logs")
    controller = CommandController(service, backend, inspector, ledger=ledger)

    response = await controller.handle_conversation_text(
        "你好",
        {"chat_id": 123, "user_id": 456},
    )

    assert response.text == "你好！直接说需要我看什么就行。"
    assert "工作区" not in response.text
    assert "当前对话" not in response.text
    assert "模式" not in response.text
