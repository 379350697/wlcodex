from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

from wlcodex.claude_permissions import normalize_claude_permission_mode


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token_env: str
    allowed_user_ids: frozenset[int]
    private_chat_only: bool


@dataclass(frozen=True)
class CodexConfig:
    binary: str
    app_server_host: str
    app_server_port: int
    approval_policy: str
    sandbox: str


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: Path
    task_log_dir: Path
    worktree_root: Path


@dataclass(frozen=True)
class DisplayConfig:
    status_update_min_interval_seconds: int
    tail_lines: int
    diff_max_chars: int


@dataclass(frozen=True)
class BackendConfig:
    startup_timeout_seconds: float
    request_timeout_seconds: float
    codex_prompt_idle_timeout_seconds: float
    codex_analysis_hard_timeout_seconds: float
    codex_verification_hard_timeout_seconds: float
    event_log_max_chars: int


@dataclass(frozen=True)
class ApprovalConfig:
    callback_timeout_seconds: int
    allow_session_approval: bool


@dataclass(frozen=True)
class TaskConfig:
    max_running_seconds: int
    max_queued_seconds: int
    max_waiting_approval_seconds: int
    watchdog_interval_seconds: int
    backend_dead_grace_seconds: int


@dataclass(frozen=True)
class WorkspaceConfig:
    alias: str
    path: Path
    allow_write: bool


@dataclass(frozen=True)
class ConversationConfig:
    enabled: bool = True
    default_mode: str = "chief_engineer"
    default_workspace: str = "wlcodex"
    summary_max_tokens: int = 800


@dataclass(frozen=True)
class OrchestrationConfig:
    enabled: bool = True
    max_verify_rounds: int = 3
    auto_delegate_simple_edits: bool = False


@dataclass(frozen=True)
class ClaudeConfig:
    enabled: bool = False
    binary: str = "claude"
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 600.0
    permission_mode: str = "acceptEdits"
    model: str = "deepseek-v4-pro"
    effort: str = "max"


@dataclass(frozen=True)
class ContextBudgetConfig:
    codex_analysis_tokens: int = 2500
    codex_to_claude_tokens: int = 1500
    claude_to_codex_tokens: int = 2500
    conversation_summary_tokens: int = 800


@dataclass(frozen=True)
class StreamingConfig:
    enabled: bool = True
    edit_min_interval_seconds: float = 1.0


@dataclass(frozen=True)
class InteractionConfig:
    profile: str = "natural"
    streaming_enabled: bool = True
    show_footer: bool = False
    edit_min_interval_seconds: float = 1.0


@dataclass(frozen=True)
class MenuConfig:
    register_bot_commands: bool = True


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    codex: CodexConfig
    storage: StorageConfig
    display: DisplayConfig
    backend: BackendConfig
    approval: ApprovalConfig
    task: TaskConfig
    workspaces: tuple[WorkspaceConfig, ...]
    conversation: ConversationConfig = ConversationConfig()
    orchestration: OrchestrationConfig = OrchestrationConfig()
    claude: ClaudeConfig = ClaudeConfig()
    context_budget: ContextBudgetConfig = ContextBudgetConfig()
    streaming: StreamingConfig = StreamingConfig()
    interaction: InteractionConfig = InteractionConfig()
    menu: MenuConfig = MenuConfig()

    def workspace_by_alias(self, alias: str) -> WorkspaceConfig:
        for workspace in self.workspaces:
            if workspace.alias == alias:
                return workspace
        raise ConfigError(f"unknown workspace alias: {alias}")


def load_config(path: Path) -> AppConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    workspaces = tuple(_workspace(item) for item in data.get("workspaces", []))
    aliases = [workspace.alias for workspace in workspaces]
    if len(aliases) != len(set(aliases)):
        raise ConfigError("duplicate workspace alias")
    if not workspaces:
        raise ConfigError("at least one workspace is required")

    telegram = data["telegram"]
    codex = data["codex"]
    storage = data["storage"]
    display = data["display"]
    backend_raw = data.get("backend", {})
    approval_raw = data.get("approval", {})
    task_raw = data.get("task", {})
    conv_raw = data.get("conversation", {})
    orch_raw = data.get("orchestration", {})
    claude_raw = data.get("claude", {})
    budget_raw = data.get("context_budget", {})
    streaming_raw = data.get("streaming", {})
    interaction_raw = data.get("interaction", {})
    menu_raw = data.get("menu", {})
    try:
        claude_permission_mode = normalize_claude_permission_mode(
            str(claude_raw.get("permission_mode", "acceptEdits"))
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return AppConfig(
        telegram=TelegramConfig(
            bot_token_env=str(telegram["bot_token_env"]),
            allowed_user_ids=frozenset(int(value) for value in telegram["allowed_user_ids"]),
            private_chat_only=bool(telegram.get("private_chat_only", True)),
        ),
        codex=CodexConfig(
            binary=str(codex.get("binary", "codex")),
            app_server_host=str(codex.get("app_server_host", "127.0.0.1")),
            app_server_port=int(codex.get("app_server_port", 17431)),
            approval_policy=str(codex.get("approval_policy", "on-request")),
            sandbox=str(codex.get("sandbox", "workspace-write")),
        ),
        storage=StorageConfig(
            sqlite_path=Path(storage["sqlite_path"]),
            task_log_dir=Path(storage["task_log_dir"]),
            worktree_root=Path(
                storage.get(
                    "worktree_root",
                    os.path.expanduser("~/.local/state/wlcodex/worktrees"),
                )
            ),
        ),
        display=DisplayConfig(
            status_update_min_interval_seconds=int(
                display.get("status_update_min_interval_seconds", 2)
            ),
            tail_lines=int(display.get("tail_lines", 40)),
            diff_max_chars=int(display.get("diff_max_chars", 3500)),
        ),
        backend=BackendConfig(
            startup_timeout_seconds=float(
                backend_raw.get("startup_timeout_seconds", 15)
            ),
            request_timeout_seconds=float(
                backend_raw.get("request_timeout_seconds", 60)
            ),
            codex_prompt_idle_timeout_seconds=float(
                backend_raw.get("codex_prompt_idle_timeout_seconds", 300)
            ),
            codex_analysis_hard_timeout_seconds=float(
                backend_raw.get("codex_analysis_hard_timeout_seconds", 1200)
            ),
            codex_verification_hard_timeout_seconds=float(
                backend_raw.get("codex_verification_hard_timeout_seconds", 1200)
            ),
            event_log_max_chars=int(
                backend_raw.get("event_log_max_chars", 20000)
            ),
        ),
        approval=ApprovalConfig(
            callback_timeout_seconds=int(
                approval_raw.get("callback_timeout_seconds", 3600)
            ),
            allow_session_approval=bool(
                approval_raw.get("allow_session_approval", True)
            ),
        ),
        task=TaskConfig(
            max_running_seconds=int(task_raw.get("max_running_seconds", 7200)),
            max_queued_seconds=int(task_raw.get("max_queued_seconds", 1800)),
            max_waiting_approval_seconds=int(
                task_raw.get("max_waiting_approval_seconds", 3600)
            ),
            watchdog_interval_seconds=int(task_raw.get("watchdog_interval_seconds", 60)),
            backend_dead_grace_seconds=int(task_raw.get("backend_dead_grace_seconds", 120)),
        ),
        workspaces=workspaces,
        conversation=ConversationConfig(
            enabled=bool(conv_raw.get("enabled", True)),
            default_mode=str(conv_raw.get("default_mode", "chief_engineer")),
            default_workspace=str(conv_raw.get("default_workspace", "wlcodex")),
            summary_max_tokens=int(conv_raw.get("summary_max_tokens", 800)),
        ),
        orchestration=OrchestrationConfig(
            enabled=bool(orch_raw.get("enabled", True)),
            max_verify_rounds=int(orch_raw.get("max_verify_rounds", 3)),
            auto_delegate_simple_edits=bool(orch_raw.get("auto_delegate_simple_edits", False)),
        ),
        claude=ClaudeConfig(
            enabled=bool(claude_raw.get("enabled", False)),
            binary=str(claude_raw.get("binary", "claude")),
            startup_timeout_seconds=float(claude_raw.get("startup_timeout_seconds", 15.0)),
            request_timeout_seconds=float(claude_raw.get("request_timeout_seconds", 600.0)),
            permission_mode=claude_permission_mode,
            model=str(claude_raw.get("model", "deepseek-v4-pro")),
            effort=str(claude_raw.get("effort", "max")),
        ),
        context_budget=ContextBudgetConfig(
            codex_analysis_tokens=int(budget_raw.get("codex_analysis_tokens", 2500)),
            codex_to_claude_tokens=int(budget_raw.get("codex_to_claude_tokens", 1500)),
            claude_to_codex_tokens=int(budget_raw.get("claude_to_codex_tokens", 2500)),
            conversation_summary_tokens=int(budget_raw.get("conversation_summary_tokens", 800)),
        ),
        streaming=StreamingConfig(
            enabled=bool(streaming_raw.get("enabled", True)),
            edit_min_interval_seconds=float(streaming_raw.get("edit_min_interval_seconds", 1.0)),
        ),
        interaction=_interaction_config(interaction_raw),
        menu=MenuConfig(
            register_bot_commands=bool(menu_raw.get("register_bot_commands", True)),
        ),
    )


def _interaction_config(data: dict[str, object]) -> InteractionConfig:
    profile = str(data.get("profile", "natural"))
    if profile not in {"natural", "legacy", "cockpit"}:
        raise ConfigError(
            "interaction.profile must be one of: natural, legacy, cockpit"
        )
    return InteractionConfig(
        profile=profile,
        streaming_enabled=bool(data.get("streaming_enabled", True)),
        show_footer=bool(data.get("show_footer", False)),
        edit_min_interval_seconds=float(
            data.get("edit_min_interval_seconds", 1.0)
        ),
    )


def _workspace(data: dict[str, object]) -> WorkspaceConfig:
    alias = str(data["alias"]).strip()
    if not alias:
        raise ConfigError("workspace alias cannot be empty")
    return WorkspaceConfig(
        alias=alias,
        path=Path(str(data["path"])),
        allow_write=bool(data.get("allow_write", True)),
    )
