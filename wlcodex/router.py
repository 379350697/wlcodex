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
class StatusCommand:
    pass


@dataclass(frozen=True)
class TraceCommand:
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
class ExecModeCommand:
    mode_name: str


@dataclass(frozen=True)
class VerifyCommand:
    prompt: str = ""


@dataclass(frozen=True)
class SettingsCommand:
    pass


@dataclass(frozen=True)
class WorkbenchHistoryCommand:
    pass


@dataclass(frozen=True)
class WorkspaceListCommand:
    pass


@dataclass(frozen=True)
class ModeSwitchCommand:
    """Parsed surface mode switch command.

    /mode          -> kind="mode_switch", mode="" (query current)
    /product       -> kind="mode_switch", mode="product"
    /terminal      -> kind="mode_switch", mode="terminal"
    /terminal <agent> -> kind="mode_switch", mode="terminal", agent="..."
    /terminal product -> kind="mode_switch", mode="product"
    """

    kind: str = "mode_switch"
    mode: str = ""  # "product" or "terminal"; empty = query
    agent: str = ""  # "claude" or "codex"; only relevant for terminal


@dataclass(frozen=True)
class TerminalSubCommand:
    """Parsed terminal surface subcommand.

    /terminal tail   -> kind="terminal_subcommand", subcommand="tail"
    /terminal detach -> kind="terminal_subcommand", subcommand="detach"
    """

    kind: str = "terminal_subcommand"
    subcommand: str = ""  # "tail" or "detach"


ParsedCommand = (
    HelpCommand
    | HealthCommand
    | CodexSessionsCommand
    | StartTaskCommand
    | ShowTaskCommand
    | ContinueCommand
    | SteerCommand
    | ListTasksCommand
    | StatusCommand
    | TraceCommand
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
    | ExecModeCommand
    | VerifyCommand
    | ModeSwitchCommand
    | TerminalSubCommand
    | SettingsCommand
    | WorkbenchHistoryCommand
    | WorkspaceListCommand
)


# --- Parser ---


_PROMPT_QUOTE_PAIRS = {
    "「": "」",
    "『": "』",
    "“": "”",
    "‘": "’",
    '"': '"',
    "'": "'",
}


def _strip_wrapping_prompt_quotes(prompt: str) -> str:
    if not prompt:
        return prompt
    closing = _PROMPT_QUOTE_PAIRS.get(prompt[0])
    if closing:
        if prompt.endswith(closing):
            return prompt[1:-1].strip()
        return prompt[1:].strip()
    return prompt


def _prompt_after_verb(stripped: str, verb: str) -> str | None:
    if stripped == verb:
        raise ParseError(f"用法：{verb} <prompt>")
    if not stripped.startswith(verb):
        return None
    rest = stripped[len(verb):]
    if not rest:
        raise ParseError(f"用法：{verb} <prompt>")
    first = rest[0]
    if first.isspace():
        prompt = rest.strip()
    elif first in (":", "："):
        prompt = rest[1:].strip()
    elif first in _PROMPT_QUOTE_PAIRS:
        prompt = _strip_wrapping_prompt_quotes(rest.strip())
    else:
        return None
    if not prompt:
        raise ParseError(f"用法：{verb} <prompt>")
    return prompt


def parse_command(text: str) -> ParsedCommand:
    stripped = text.strip()

    if stripped == "/start" or stripped == "/help":
        return HelpCommand()
    if stripped == "/health":
        return HealthCommand()
    if stripped == "/settings" or stripped.startswith("/settings "):
        return SettingsCommand()
    if stripped == "/tasks":
        return ListTasksCommand()
    if stripped == "/status":
        return StatusCommand()
    if stripped == "/trace":
        return TraceCommand()
    if stripped.startswith("/trace "):
        raw_limit = stripped.split(maxsplit=1)[1].strip()
        if not raw_limit:
            return TraceCommand()
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise ParseError("用法：/trace [条数]") from exc
        return TraceCommand(limit=max(1, min(limit, 100)))
    if stripped == "/codex-sessions" or stripped == "/sessions":
        return CodexSessionsCommand()
    if stripped == "/workbenches" or stripped == "/history":
        return WorkbenchHistoryCommand()
    if stripped == "/workspaces":
        return WorkspaceListCommand()

    # Dual-surface mode commands
    if stripped == "/mode":
        return ModeSwitchCommand(mode="")
    if stripped == "/product":
        return ModeSwitchCommand(mode="product")
    if stripped == "/terminal":
        return ModeSwitchCommand(mode="terminal")
    if stripped.startswith("/terminal "):
        return _parse_terminal_command(stripped)

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

    auto_prompt = _prompt_after_verb(stripped, "/auto")
    if auto_prompt is not None:
        prompt = auto_prompt
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

    if stripped == "/exec_mode":
        raise ParseError("用法：/exec_mode <orchestrated|codex_direct|claude_direct>")
    if stripped.startswith("/exec_mode "):
        mode_name = stripped.split(maxsplit=1)[1].strip()
        if not mode_name:
            raise ParseError("用法：/exec_mode <orchestrated|codex_direct|claude_direct>")
        return ExecModeCommand(mode_name=mode_name)

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


def _parse_terminal_command(text: str) -> ParsedCommand:
    """Parse /terminal <subcommand|agent|product>.

    /terminal           -> mode_switch to terminal
    /terminal claude    -> mode_switch with agent claude
    /terminal codex     -> mode_switch with agent codex
    /terminal agent claude -> mode_switch with agent claude
    /terminal agent codex  -> mode_switch with agent codex
    /terminal tail      -> terminal_subcommand tail
    /terminal pause     -> terminal_subcommand pause
    /terminal detach    -> terminal_subcommand detach
    /terminal product   -> mode_switch to product
    """
    parts = text.split(maxsplit=2)
    arg = parts[1].strip() if len(parts) >= 2 else ""

    if arg == "agent":
        if len(parts) >= 3 and parts[2].strip() in ("claude", "codex"):
            return ModeSwitchCommand(mode="terminal", agent=parts[2].strip())
        raise ParseError("用法：/terminal agent <claude|codex>")

    if arg == "claude":
        return ModeSwitchCommand(mode="terminal", agent="claude")
    if arg == "codex":
        return ModeSwitchCommand(mode="terminal", agent="codex")
    if arg == "tail":
        return TerminalSubCommand(subcommand="tail")
    if arg == "pause":
        return TerminalSubCommand(subcommand="pause")
    if arg == "detach":
        return TerminalSubCommand(subcommand="detach")
    if arg == "product":
        return ModeSwitchCommand(mode="product")

    raise ParseError("用法：/terminal [claude|codex|agent claude|agent codex|tail|pause|detach|product]")


def _parse_task_id(value: str) -> int | None:
    cleaned = value.strip().removeprefix("#")
    if not cleaned.isdigit():
        return None
    return int(cleaned)
