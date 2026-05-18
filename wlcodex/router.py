from __future__ import annotations

from dataclasses import dataclass


class ParseError(ValueError):
    pass


# --- Command types ---


@dataclass(frozen=True)
class HelpCommand:
    pass


@dataclass(frozen=True)
class HealthCommand:
    pass


@dataclass(frozen=True)
class CodexSessionsCommand:
    pass


@dataclass(frozen=True)
class StartTaskCommand:
    workspace_alias: str
    prompt: str


@dataclass(frozen=True)
class ShowTaskCommand:
    task_id: int


@dataclass(frozen=True)
class ContinueCommand:
    task_id: int
    prompt: str


@dataclass(frozen=True)
class SteerCommand:
    task_id: int
    prompt: str


@dataclass(frozen=True)
class ListTasksCommand:
    limit: int = 20


@dataclass(frozen=True)
class TailCommand:
    task_id: int


@dataclass(frozen=True)
class EventsCommand:
    task_id: int


@dataclass(frozen=True)
class DiffCommand:
    task_id: int | None = None


@dataclass(frozen=True)
class FilesCommand:
    task_id: int | None = None


@dataclass(frozen=True)
class PauseCommand:
    task_id: int


@dataclass(frozen=True)
class AbortCommand:
    task_id: int


@dataclass(frozen=True)
class ArchiveCommand:
    task_id: int


@dataclass(frozen=True)
class ForkCommand:
    task_id: int
    prompt: str


@dataclass(frozen=True)
class NewConversationCommand:
    title: str = ""


@dataclass(frozen=True)
class CodexDirectCommand:
    prompt: str


@dataclass(frozen=True)
class ClaudeDirectCommand:
    prompt: str


@dataclass(frozen=True)
class ClaudePermissionCommand:
    mode_name: str = ""


@dataclass(frozen=True)
class AutoModeCommand:
    prompt: str


@dataclass(frozen=True)
class StopCurrentCommand:
    pass


@dataclass(frozen=True)
class SwitchWorkspaceCommand:
    workspace_alias: str


@dataclass(frozen=True)
class ModelCommand:
    model_name: str = ""


@dataclass(frozen=True)
class RecentCommand:
    n: int = 5


@dataclass(frozen=True)
class VerifyCommand:
    prompt: str = ""


ParsedCommand = (
    HelpCommand
    | HealthCommand
    | CodexSessionsCommand
    | StartTaskCommand
    | ShowTaskCommand
    | ContinueCommand
    | SteerCommand
    | ListTasksCommand
    | TailCommand
    | EventsCommand
    | DiffCommand
    | FilesCommand
    | PauseCommand
    | AbortCommand
    | ArchiveCommand
    | ForkCommand
    | NewConversationCommand
    | CodexDirectCommand
    | ClaudeDirectCommand
    | ClaudePermissionCommand
    | AutoModeCommand
    | StopCurrentCommand
    | SwitchWorkspaceCommand
    | ModelCommand
    | RecentCommand
    | VerifyCommand
)


# --- Parser ---


def parse_command(text: str) -> ParsedCommand:
    stripped = text.strip()

    if stripped == "/start" or stripped == "/help":
        return HelpCommand()
    if stripped == "/health":
        return HealthCommand()
    if stripped == "/tasks" or stripped == "/status":
        return ListTasksCommand()
    if stripped == "/codex-sessions" or stripped == "/sessions":
        return CodexSessionsCommand()

    # New conversation commands
    if stripped == "/new":
        return NewConversationCommand()
    if stripped.startswith("/new "):
        title = stripped.split(maxsplit=1)[1].strip()
        return NewConversationCommand(title=title)

    if stripped.startswith("/codex "):
        prompt = stripped.split(maxsplit=1)[1].strip()
        if not prompt:
            raise ParseError("用法：/codex <prompt>")
        return CodexDirectCommand(prompt=prompt)

    if stripped.startswith("/claude "):
        prompt = stripped.split(maxsplit=1)[1].strip()
        if not prompt:
            raise ParseError("用法：/claude <prompt>")
        return ClaudeDirectCommand(prompt=prompt)

    for verb in ("/claude_mode", "/claude_permission", "/claude-permission", "/claude权限"):
        if stripped == verb:
            return ClaudePermissionCommand()
        if stripped.startswith(f"{verb} "):
            mode_name = stripped.split(maxsplit=1)[1].strip()
            return ClaudePermissionCommand(mode_name=mode_name)

    if stripped.startswith("/auto "):
        prompt = stripped.split(maxsplit=1)[1].strip()
        if not prompt:
            raise ParseError("用法：/auto <prompt>")
        return AutoModeCommand(prompt=prompt)

    if stripped == "/stop":
        return StopCurrentCommand()
    if stripped.startswith("/stop "):
        return StopCurrentCommand()

    if stripped == "/switch":
        raise ParseError("用法：/switch <workspace>")
    if stripped.startswith("/switch "):
        alias = stripped.split(maxsplit=1)[1].strip()
        if not alias:
            raise ParseError("用法：/switch <workspace>")
        return SwitchWorkspaceCommand(workspace_alias=alias)

    if stripped == "/model":
        return ModelCommand()
    if stripped.startswith("/model "):
        model_name = stripped.split(maxsplit=1)[1].strip()
        return ModelCommand(model_name=model_name)

    if stripped == "/recent":
        return RecentCommand()
    if stripped.startswith("/recent "):
        arg = stripped.split(maxsplit=1)[1].strip()
        if not arg.isdigit():
            raise ParseError("用法：/recent 或 /recent <n>（n 为 1-20 的整数）")
        n = int(arg)
        if n < 1 or n > 20:
            raise ParseError("n 的取值范围为 1-20")
        return RecentCommand(n=n)

    if stripped == "/verify":
        return VerifyCommand()
    if stripped.startswith("/verify "):
        prompt = stripped.split(maxsplit=1)[1].strip()
        return VerifyCommand(prompt=prompt)

    if stripped.startswith("/task "):
        return _parse_task(stripped)
    if stripped.startswith("/continue "):
        return _parse_task_id_prompt(stripped, "/continue", ContinueCommand)
    if stripped.startswith("/steer "):
        return _parse_task_id_prompt(stripped, "/steer", SteerCommand)
    if stripped.startswith("/fork "):
        return _parse_task_id_prompt(stripped, "/fork", ForkCommand)

    # Diff and files can be used without task ID (conversation context)
    if stripped == "/diff":
        return DiffCommand(task_id=None)
    if stripped == "/files":
        return FilesCommand(task_id=None)

    # Single-argument commands
    for verb, cls in _SINGLE_ARG.items():
        if stripped.startswith(f"{verb} "):
            return _parse_task_id_only(stripped, verb, cls)

    raise ParseError("未知命令。发送 /help 查看可用命令。")


_SINGLE_ARG: dict[str, type] = {
    "/tail": TailCommand,
    "/events": EventsCommand,
    "/diff": DiffCommand,
    "/files": FilesCommand,
    "/pause": PauseCommand,
    "/abort": AbortCommand,
    "/archive": ArchiveCommand,
}


def _parse_task_id_only(text: str, verb: str, cls: type) -> ParsedCommand:
    parts = text.split(maxsplit=1)
    task_id = _parse_task_id(parts[1]) if len(parts) >= 2 else None
    if task_id is None:
        raise ParseError(f"用法：{verb} <task_id>")
    return cls(task_id=task_id)


def _parse_task_id_prompt(text: str, verb: str, cls: type) -> ParsedCommand:
    parts = text.split(maxsplit=2)
    task_id = _parse_task_id(parts[1]) if len(parts) >= 2 else None
    if len(parts) < 3 or task_id is None or not parts[2].strip():
        raise ParseError(f"用法：{verb} <task_id> <prompt>")
    return cls(task_id=task_id, prompt=parts[2].strip())


def _parse_task(text: str) -> StartTaskCommand | ShowTaskCommand:
    parts = text.split(maxsplit=2)
    task_id = _parse_task_id(parts[1]) if len(parts) >= 2 else None
    if len(parts) == 2 and task_id is not None:
        return ShowTaskCommand(task_id=task_id)
    if len(parts) < 3:
        raise ParseError("用法：/task <workspace> <prompt> 或 /task <task_id>")
    workspace_alias = parts[1].strip()
    prompt = parts[2].strip()
    if not workspace_alias or not prompt:
        raise ParseError("用法：/task <workspace> <prompt>")
    return StartTaskCommand(workspace_alias=workspace_alias, prompt=prompt)


def _parse_task_id(value: str) -> int | None:
    cleaned = value.strip().removeprefix("#")
    if not cleaned.isdigit():
        return None
    return int(cleaned)
