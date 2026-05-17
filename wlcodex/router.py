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
    task_id: int


@dataclass(frozen=True)
class FilesCommand:
    task_id: int


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

    if stripped.startswith("/task "):
        return _parse_task(stripped)
    if stripped.startswith("/continue "):
        return _parse_task_id_prompt(stripped, "/continue", ContinueCommand)
    if stripped.startswith("/steer "):
        return _parse_task_id_prompt(stripped, "/steer", SteerCommand)
    if stripped.startswith("/fork "):
        return _parse_task_id_prompt(stripped, "/fork", ForkCommand)

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
