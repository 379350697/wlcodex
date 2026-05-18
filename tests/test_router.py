import pytest

from wlcodex.router import (
    AbortCommand,
    ArchiveCommand,
    CodexSessionsCommand,
    ClaudePermissionCommand,
    ContinueCommand,
    DiffCommand,
    EventsCommand,
    FilesCommand,
    ForkCommand,
    HealthCommand,
    HelpCommand,
    ListTasksCommand,
    PauseCommand,
    ParseError,
    ShowTaskCommand,
    StartTaskCommand,
    StatusCommand,
    SteerCommand,
    TailCommand,
    parse_command,
)


def test_parse_help_command() -> None:
    assert isinstance(parse_command("/help"), HelpCommand)
    assert isinstance(parse_command("/start"), HelpCommand)


def test_parse_health_command() -> None:
    assert isinstance(parse_command("/health"), HealthCommand)


def test_parse_tasks_command() -> None:
    assert isinstance(parse_command("/tasks"), ListTasksCommand)
    assert isinstance(parse_command("/status"), StatusCommand)


def test_parse_codex_sessions() -> None:
    assert isinstance(parse_command("/codex-sessions"), CodexSessionsCommand)


def test_parse_new_task_command() -> None:
    command = parse_command("/task lightfee Fix health checks")
    assert command == StartTaskCommand(workspace_alias="lightfee", prompt="Fix health checks")


def test_parse_show_task_command() -> None:
    command = parse_command("/task 42")
    assert command == ShowTaskCommand(task_id=42)


def test_parse_show_task_command_accepts_hash_prefixed_id() -> None:
    command = parse_command("/task #42")
    assert command == ShowTaskCommand(task_id=42)


def test_parse_continue_command() -> None:
    command = parse_command("/continue 42 Use conservative fix")
    assert command == ContinueCommand(task_id=42, prompt="Use conservative fix")


def test_parse_continue_command_accepts_hash_prefixed_id() -> None:
    command = parse_command("/continue #42 Use conservative fix")
    assert command == ContinueCommand(task_id=42, prompt="Use conservative fix")


def test_parse_steer_command() -> None:
    command = parse_command("/steer 42 Stop touching config")
    assert command == SteerCommand(task_id=42, prompt="Stop touching config")


def test_parse_tail_command() -> None:
    assert parse_command("/tail 42") == TailCommand(task_id=42)


def test_parse_events_command() -> None:
    assert parse_command("/events 42") == EventsCommand(task_id=42)


def test_parse_diff_command() -> None:
    assert parse_command("/diff 42") == DiffCommand(task_id=42)


def test_parse_files_command() -> None:
    assert parse_command("/files 42") == FilesCommand(task_id=42)


def test_parse_pause_command() -> None:
    assert parse_command("/pause 42") == PauseCommand(task_id=42)


def test_parse_pause_command_accepts_hash_prefixed_id() -> None:
    assert parse_command("/pause #42") == PauseCommand(task_id=42)


def test_parse_abort_command() -> None:
    assert parse_command("/abort 42") == AbortCommand(task_id=42)


def test_parse_archive_command() -> None:
    assert parse_command("/archive 42") == ArchiveCommand(task_id=42)


def test_parse_fork_command() -> None:
    cmd = parse_command("/fork 42 New approach")
    assert cmd == ForkCommand(task_id=42, prompt="New approach")


def test_parse_rejects_empty_task_prompt() -> None:
    with pytest.raises(ParseError, match="用法"):
        parse_command("/task lightfee")


def test_parse_rejects_unknown_command() -> None:
    with pytest.raises(ParseError, match="未知命令"):
        parse_command("/banana")


def test_parse_rejects_tail_no_id() -> None:
    with pytest.raises(ParseError):
        parse_command("/tail")

def test_parse_rejects_unknown_command_only_slash() -> None:
    with pytest.raises(ParseError):
        parse_command("/unknown")


def test_parse_sessions_command() -> None:
    assert isinstance(parse_command("/sessions"), CodexSessionsCommand)


def test_parse_claude_permission_command_with_chinese_mode() -> None:
    assert parse_command("/claude_mode") == ClaudePermissionCommand(mode_name="")
    assert parse_command("/claude_mode 允许编辑") == ClaudePermissionCommand(
        mode_name="允许编辑"
    )


def test_parse_rejects_continue_no_prompt() -> None:
    with pytest.raises(ParseError, match="用法"):
        parse_command("/continue 42")
