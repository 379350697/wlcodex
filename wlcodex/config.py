from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import tomllib
from typing import TYPE_CHECKING

from wlcodex.claude_permissions import normalize_claude_permission_mode

if TYPE_CHECKING:
    from wlcodex.surfaces.core.models import SurfacePolicy


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token_env: str
    allowed_user_ids: frozenset[int]
    private_chat_only: bool = True
    default_surface_mode: str = "product"


@dataclass(frozen=True)
class CodexConfig:
    binary: str
    app_server_host: str
    app_server_port: int
    approval_policy: str
    sandbox: str
    codex_home: Path | None = None


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
    relay_native_idle_timeout_seconds: int = 300


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
    max_verify_rounds: int = 0
    auto_delegate_simple_edits: bool = False


@dataclass(frozen=True)
class AdaptiveTeamConfig:
    enabled: bool = True
    model_profiles: dict[str, str] = field(
        default_factory=lambda: {
            "codex_gpt": "codex",
            "claude_deepseek": "claude",
        }
    )
    assignments: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "director": ("codex_gpt",),
            "investigator": ("codex_gpt",),
            "architect": ("codex_gpt",),
            "implementer": ("claude_deepseek", "codex_gpt"),
            "tester": ("codex_gpt",),
            "auditor": ("codex_gpt",),
        }
    )
    role_skills: dict[str, tuple[str, ...]] = field(default_factory=dict)
    role_capabilities: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "model_profiles",
            {str(key): str(value) for key, value in self.model_profiles.items()},
        )
        object.__setattr__(
            self,
            "assignments",
            {
                str(key): tuple(str(item) for item in value)
                for key, value in self.assignments.items()
            },
        )
        object.__setattr__(
            self,
            "role_skills",
            {
                str(key): tuple(str(item) for item in value)
                for key, value in self.role_skills.items()
            },
        )
        object.__setattr__(
            self,
            "role_capabilities",
            {
                str(key): tuple(str(item) for item in value)
                for key, value in self.role_capabilities.items()
            },
        )


@dataclass(frozen=True)
class ClaudeConfig:
    enabled: bool = False
    binary: str = "auto"
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 3600.0
    stream_idle_timeout_seconds: float = 600.0
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
class TerminalSurfaceConfig:
    enabled: bool = False
    default_agent: str = "claude"
    max_frame_chars: int = 3500
    redaction_enabled: bool = True
    block_idle_seconds: float = 2.0


@dataclass(frozen=True)
class ProductSurfaceConfig:
    preview_enabled: bool = True
    preview_edit_min_interval_seconds: float = 2.0


@dataclass(frozen=True)
class WorkspaceDiscoveryConfig:
    enabled: bool = False
    root: Path | None = None
    include_git_only: bool = True
    allow_write: bool = True
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelegramOutputConfig:
    preview_enabled: bool = True
    preview_edit_min_interval_seconds: float = 2.0
    preview_send_timeout_seconds: float = 5.0
    product_body_mode: str = "final"
    terminal_body_mode: str = "semantic_blocks"
    semantic_min_chars: int = 900
    semantic_max_chars: int = 3200
    final_chunk_chars: int = 3900
    terminal_block_idle_seconds: float = 2.0


@dataclass(frozen=True)
class LiveStreamConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 18731
    access_token_env: str = "WLCODEX_LIVE_STREAM_TOKEN"
    allow_unauthenticated_loopback: bool = True


@dataclass(frozen=True)
class CodexNativeConfig:
    enabled: bool = False
    transport: str = "daemon"
    sock_path: Path | None = None
    listen_endpoint: str = "ws://127.0.0.1:18742"
    remote_control: bool = True


@dataclass(frozen=True)
class NativeAgentsCodexConfig:
    enabled: bool = False


@dataclass(frozen=True)
class NativeAgentsClaudeCliLocalConfig:
    binary: str = "auto"
    model: str = "deepseek-v4-pro"
    effort: str = "max"
    permission_mode: str = "acceptEdits"


@dataclass(frozen=True)
class NativeAgentsClaudeSdkDeepSeekConfig:
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com/anthropic"
    model: str = "deepseek-v4-pro"
    effort: str = "xhigh"
    permission_mode: str = "acceptEdits"
    system_prompt: str = ""
    cli_path: str = ""
    ccswitch_fallback_enabled: bool = True
    ccswitch_db_path: str = "~/.cc-switch/cc-switch.db"


@dataclass(frozen=True)
class NativeAgentsClaudeConfig:
    enabled: bool = False
    engine: str = "sdk-deepseek"
    cli_local: NativeAgentsClaudeCliLocalConfig = NativeAgentsClaudeCliLocalConfig()
    sdk_deepseek: NativeAgentsClaudeSdkDeepSeekConfig = (
        NativeAgentsClaudeSdkDeepSeekConfig()
    )


@dataclass(frozen=True)
class NativeAgentsAntigravityCliLocalConfig:
    binary: str = "auto"
    print_timeout: str = "5m0s"
    default_model: str = ""
    dangerously_skip_permissions: bool = False
    sandbox: bool = False


@dataclass(frozen=True)
class NativeAgentsAntigravityConfig:
    enabled: bool = False
    engine: str = "cli-local"
    cli_local: NativeAgentsAntigravityCliLocalConfig = (
        NativeAgentsAntigravityCliLocalConfig()
    )


@dataclass(frozen=True)
class NativeAgentsConfig:
    enabled: bool = False
    default_provider: str = "codex"
    codex: NativeAgentsCodexConfig = NativeAgentsCodexConfig()
    claude: NativeAgentsClaudeConfig = NativeAgentsClaudeConfig()
    antigravity: NativeAgentsAntigravityConfig = NativeAgentsAntigravityConfig()


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
    adaptive_team: AdaptiveTeamConfig = field(default_factory=AdaptiveTeamConfig)
    claude: ClaudeConfig = ClaudeConfig()
    context_budget: ContextBudgetConfig = ContextBudgetConfig()
    streaming: StreamingConfig = StreamingConfig()
    interaction: InteractionConfig = InteractionConfig()
    menu: MenuConfig = MenuConfig()
    terminal: TerminalSurfaceConfig = TerminalSurfaceConfig()
    product: ProductSurfaceConfig = ProductSurfaceConfig()
    telegram_output: TelegramOutputConfig = TelegramOutputConfig()
    workspace_discovery: WorkspaceDiscoveryConfig = WorkspaceDiscoveryConfig()
    live_stream: LiveStreamConfig = LiveStreamConfig()
    codex_native: CodexNativeConfig = CodexNativeConfig()
    native_agents: NativeAgentsConfig = NativeAgentsConfig()

    def workspace_by_alias(self, alias: str) -> WorkspaceConfig:
        for workspace in self.workspaces:
            if workspace.alias == alias:
                return workspace
        raise ConfigError(f"unknown workspace alias: {alias}")

    def surface_policy(self) -> "SurfacePolicy":
        """Build a SurfacePolicy from this config."""
        from wlcodex.surfaces.core.models import (
            TerminalPolicy,
            ProductPolicy,
            SurfacePolicy,
        )
        return SurfacePolicy(
            terminal=TerminalPolicy(
                max_frame_chars=self.terminal.max_frame_chars,
                redaction_enabled=self.terminal.redaction_enabled,
                body_mode=self.telegram_output.terminal_body_mode,
                block_idle_seconds=self.terminal.block_idle_seconds,
                preview_enabled=self.telegram_output.preview_enabled,
                preview_edit_min_interval_seconds=self.telegram_output.preview_edit_min_interval_seconds,
            ),
            product=ProductPolicy(
                body_mode=self.telegram_output.product_body_mode,
                preview_enabled=self.product.preview_enabled,
                preview_edit_min_interval_seconds=self.product.preview_edit_min_interval_seconds,
                semantic_min_chars=self.telegram_output.semantic_min_chars,
                semantic_max_chars=self.telegram_output.semantic_max_chars,
                final_chunk_chars=self.telegram_output.final_chunk_chars,
            ),
        )


def load_config(path: Path) -> AppConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    discovery = _workspace_discovery(data.get("workspace_discovery", {}))
    explicit_workspaces = tuple(_workspace(item) for item in data.get("workspaces", []))
    discovered_workspaces = _discover_workspaces(discovery)

    explicit_aliases = {w.alias for w in explicit_workspaces}
    workspaces = explicit_workspaces + tuple(
        w for w in discovered_workspaces if w.alias not in explicit_aliases
    )
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
    adaptive_team_raw = data.get("adaptive_team", {})
    claude_raw = data.get("claude", {})
    budget_raw = data.get("context_budget", {})
    streaming_raw = data.get("streaming", {})
    interaction_raw = data.get("interaction", {})
    menu_raw = data.get("menu", {})
    terminal_raw = data.get("terminal", {})
    product_raw = data.get("product", {})
    telegram_output_raw = data.get("telegram_output", {})
    live_stream_raw = data.get("live_stream", {})
    codex_native_raw = data.get("codex_native", {})
    native_agents_raw = data.get("native_agents", {})
    native_agents_config = _native_agents_config(native_agents_raw)
    codex_native_config = _codex_native_config(codex_native_raw)
    if codex_native_config.enabled and not native_agents_config.enabled:
        native_agents_config = NativeAgentsConfig(
            enabled=True,
            default_provider="codex",
            codex=NativeAgentsCodexConfig(enabled=True),
            claude=native_agents_config.claude,
            antigravity=native_agents_config.antigravity,
        )
    terminal_default_agent = str(terminal_raw.get("default_agent", "claude"))
    if terminal_default_agent not in ("claude", "codex"):
        raise ConfigError(
            f"terminal.default_agent must be 'claude' or 'codex', got: {terminal_default_agent!r}"
        )
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
            default_surface_mode=str(telegram.get("default_surface_mode", "product")),
        ),
        codex=CodexConfig(
            binary=str(codex.get("binary", "codex")),
            app_server_host=str(codex.get("app_server_host", "127.0.0.1")),
            app_server_port=int(codex.get("app_server_port", 17431)),
            approval_policy=str(codex.get("approval_policy", "on-request")),
            sandbox=str(codex.get("sandbox", "workspace-write")),
            codex_home=_optional_path(codex.get("codex_home")),
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
                backend_raw.get("codex_analysis_hard_timeout_seconds", 3600)
            ),
            codex_verification_hard_timeout_seconds=float(
                backend_raw.get("codex_verification_hard_timeout_seconds", 3600)
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
            relay_native_idle_timeout_seconds=int(
                task_raw.get("relay_native_idle_timeout_seconds", 300)
            ),
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
            max_verify_rounds=int(orch_raw.get("max_verify_rounds", 0)),
            auto_delegate_simple_edits=bool(orch_raw.get("auto_delegate_simple_edits", False)),
        ),
        adaptive_team=_adaptive_team_config(adaptive_team_raw),
        claude=ClaudeConfig(
            enabled=bool(claude_raw.get("enabled", False)),
            binary=str(claude_raw.get("binary", "auto")),
            startup_timeout_seconds=float(claude_raw.get("startup_timeout_seconds", 15.0)),
            request_timeout_seconds=float(claude_raw.get("request_timeout_seconds", 3600.0)),
            stream_idle_timeout_seconds=float(
                claude_raw.get("stream_idle_timeout_seconds", 600.0)
            ),
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
        terminal=TerminalSurfaceConfig(
            enabled=bool(terminal_raw.get("enabled", False)),
            default_agent=terminal_default_agent,
            max_frame_chars=int(terminal_raw.get("max_frame_chars", 3500)),
            redaction_enabled=bool(terminal_raw.get("redaction_enabled", True)),
            block_idle_seconds=float(terminal_raw.get("block_idle_seconds", 2.0)),
        ),
        product=ProductSurfaceConfig(
            preview_enabled=bool(product_raw.get("preview_enabled", True)),
            preview_edit_min_interval_seconds=float(
                product_raw.get("preview_edit_min_interval_seconds", 2.0)
            ),
        ),
        telegram_output=_telegram_output_config(telegram_output_raw),
        workspace_discovery=discovery,
        live_stream=_live_stream_config(live_stream_raw),
        codex_native=codex_native_config,
        native_agents=native_agents_config,
    )


def _adaptive_team_config(data: dict[str, object]) -> AdaptiveTeamConfig:
    defaults = AdaptiveTeamConfig()
    model_profiles_raw = data.get("model_profiles", {})
    assignments_raw = data.get("assignments", {})
    role_skills_raw = data.get("role_skills", {})
    role_capabilities_raw = data.get("role_capabilities", {})

    model_profiles = {
        **defaults.model_profiles,
        **{str(key): str(value) for key, value in dict(model_profiles_raw).items()},
    }
    assignments = {
        **defaults.assignments,
        **_string_tuple_mapping("assignments", assignments_raw),
    }
    role_skills = {
        **defaults.role_skills,
        **_string_tuple_mapping("role_skills", role_skills_raw),
    }
    role_capabilities = {
        **defaults.role_capabilities,
        **_string_tuple_mapping("role_capabilities", role_capabilities_raw),
    }
    return AdaptiveTeamConfig(
        enabled=bool(data.get("enabled", defaults.enabled)),
        model_profiles=model_profiles,
        assignments=assignments,
        role_skills=role_skills,
        role_capabilities=role_capabilities,
    )


def _string_tuple_mapping(table: str, raw: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"adaptive_team.{table} must be a table")
    result: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"adaptive_team.{table}.{key} must be a list of strings")
        if not all(isinstance(item, str) for item in value):
            raise ConfigError(f"adaptive_team.{table}.{key} must be a list of strings")
        result[str(key)] = tuple(value)
    return result


def _telegram_output_config(data: dict[str, object]) -> TelegramOutputConfig:
    product_mode = str(data.get("product_body_mode", "final"))
    terminal_mode = str(data.get("terminal_body_mode", "semantic_blocks"))
    allowed = {"final", "semantic_blocks"}
    if product_mode not in allowed:
        raise ConfigError(
            "telegram_output.product_body_mode must be final or semantic_blocks"
        )
    if terminal_mode not in allowed:
        raise ConfigError(
            "telegram_output.terminal_body_mode must be final or semantic_blocks"
        )
    return TelegramOutputConfig(
        preview_enabled=bool(data.get("preview_enabled", True)),
        preview_edit_min_interval_seconds=float(
            data.get("preview_edit_min_interval_seconds", 2.0)
        ),
        preview_send_timeout_seconds=float(
            data.get("preview_send_timeout_seconds", 5.0)
        ),
        product_body_mode=product_mode,
        terminal_body_mode=terminal_mode,
        semantic_min_chars=int(data.get("semantic_min_chars", 900)),
        semantic_max_chars=int(data.get("semantic_max_chars", 3200)),
        final_chunk_chars=int(data.get("final_chunk_chars", 3900)),
        terminal_block_idle_seconds=float(
            data.get("terminal_block_idle_seconds", 2.0)
        ),
    )


def _live_stream_config(data: dict[str, object]) -> LiveStreamConfig:
    host = str(data.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost"):
        raise ConfigError(
            f"live_stream.host must be loopback-only in this release, got: {host!r}"
        )
    port = int(data.get("port", 18731))
    if port <= 0 or port > 65535:
        raise ConfigError(f"live_stream.port must be 1-65535, got: {port}")
    return LiveStreamConfig(
        enabled=bool(data.get("enabled", False)),
        host=host,
        port=port,
        access_token_env=str(
            data.get("access_token_env", "WLCODEX_LIVE_STREAM_TOKEN")
        ),
        allow_unauthenticated_loopback=bool(
            data.get("allow_unauthenticated_loopback", True)
        ),
    )


def _codex_native_config(data: dict[str, object]) -> CodexNativeConfig:
    transport = str(data.get("transport", "daemon"))
    if transport == "proxy":
        logging.getLogger(__name__).warning(
            "codex_native.transport='proxy' is deprecated; using 'daemon'"
        )
        transport = "daemon"
    if transport not in {"daemon", "app-server"}:
        raise ConfigError(
            "codex_native.transport must be 'daemon', 'app-server', or 'proxy', "
            f"got: {transport!r}"
        )
    return CodexNativeConfig(
        enabled=bool(data.get("enabled", False)),
        transport=transport,
        sock_path=_optional_path(data.get("sock_path")),
        listen_endpoint=str(
            data.get("listen_endpoint", "ws://127.0.0.1:18742")
        ),
        remote_control=bool(data.get("remote_control", True)),
    )


def _native_agents_config(data: dict[str, object]) -> NativeAgentsConfig:
    default_provider = str(data.get("default_provider", "codex"))
    if default_provider not in {"codex", "claude", "antigravity"}:
        raise ConfigError(
            "native_agents.default_provider must be codex, claude, or antigravity"
        )

    codex_raw = dict(data.get("codex", {}) or {})
    claude_raw = dict(data.get("claude", {}) or {})
    antigravity_raw = dict(data.get("antigravity", {}) or {})
    cli_raw = dict(claude_raw.get("cli_local", {}) or {})
    sdk_raw = dict(claude_raw.get("sdk_deepseek", {}) or {})
    antigravity_cli_raw = dict(antigravity_raw.get("cli_local", {}) or {})

    if "enabled" in cli_raw or "enabled" in sdk_raw:
        raise ConfigError(
            "claude engine must be selected by native_agents.claude.engine, "
            "not by per-engine enabled flags"
        )

    engine = str(claude_raw.get("engine", "sdk-deepseek"))
    if engine not in {"cli-local", "sdk-deepseek"}:
        raise ConfigError(
            "native_agents.claude.engine must be cli-local or sdk-deepseek"
        )

    antigravity_engine = str(antigravity_raw.get("engine", "cli-local"))
    if antigravity_engine not in {"cli-local", "sdk"}:
        raise ConfigError(
            "native_agents.antigravity.engine must be cli-local or sdk"
        )

    try:
        cli_permission_mode = normalize_claude_permission_mode(
            str(cli_raw.get("permission_mode", "acceptEdits"))
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    try:
        sdk_permission_mode = normalize_claude_permission_mode(
            str(sdk_raw.get("permission_mode", "acceptEdits"))
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return NativeAgentsConfig(
        enabled=bool(data.get("enabled", False)),
        default_provider=default_provider,
        codex=NativeAgentsCodexConfig(
            enabled=bool(codex_raw.get("enabled", False)),
        ),
        claude=NativeAgentsClaudeConfig(
            enabled=bool(claude_raw.get("enabled", False)),
            engine=engine,
            cli_local=NativeAgentsClaudeCliLocalConfig(
                binary=str(cli_raw.get("binary", "auto")),
                model=str(cli_raw.get("model", "deepseek-v4-pro")).strip()
                or "deepseek-v4-pro",
                effort=str(cli_raw.get("effort", "max")),
                permission_mode=cli_permission_mode,
            ),
            sdk_deepseek=NativeAgentsClaudeSdkDeepSeekConfig(
                api_key_env=str(sdk_raw.get("api_key_env", "DEEPSEEK_API_KEY")),
                base_url=str(
                    sdk_raw.get("base_url", "https://api.deepseek.com/anthropic")
                ),
                model=str(sdk_raw.get("model", "deepseek-v4-pro")),
                effort=str(sdk_raw.get("effort", "xhigh")),
                permission_mode=sdk_permission_mode,
                system_prompt=str(sdk_raw.get("system_prompt", "")),
                cli_path=str(sdk_raw.get("cli_path", "")),
                ccswitch_fallback_enabled=bool(
                    sdk_raw.get("ccswitch_fallback_enabled", True)
                ),
                ccswitch_db_path=str(
                    sdk_raw.get("ccswitch_db_path", "~/.cc-switch/cc-switch.db")
                ),
            ),
        ),
        antigravity=NativeAgentsAntigravityConfig(
            enabled=bool(antigravity_raw.get("enabled", False)),
            engine=antigravity_engine,
            cli_local=NativeAgentsAntigravityCliLocalConfig(
                binary=str(antigravity_cli_raw.get("binary", "auto")),
                print_timeout=str(
                    antigravity_cli_raw.get("print_timeout", "5m0s")
                ),
                default_model=str(
                    antigravity_cli_raw.get("default_model", "")
                ).strip(),
                dangerously_skip_permissions=bool(
                    antigravity_cli_raw.get(
                        "dangerously_skip_permissions",
                        False,
                    )
                ),
                sandbox=bool(antigravity_cli_raw.get("sandbox", False)),
            ),
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


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(os.path.expanduser(text))


def _workspace_discovery(raw: dict) -> WorkspaceDiscoveryConfig:
    root_value = raw.get("root")
    return WorkspaceDiscoveryConfig(
        enabled=bool(raw.get("enabled", False)),
        root=Path(str(root_value)) if root_value else None,
        include_git_only=bool(raw.get("include_git_only", True)),
        allow_write=bool(raw.get("allow_write", True)),
        exclude=tuple(str(item) for item in raw.get("exclude", [])),
    )


def _workspace_alias_from_dir(name: str) -> str:
    chars: list[str] = []
    previous_dash = False
    for char in name.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    alias = "".join(chars).strip("-")
    if not alias:
        raise ConfigError(f"cannot derive workspace alias from directory: {name}")
    return alias


def _discover_workspaces(discovery: WorkspaceDiscoveryConfig) -> tuple[WorkspaceConfig, ...]:
    if not discovery.enabled:
        return ()
    if discovery.root is None:
        raise ConfigError("workspace_discovery.root is required when discovery is enabled")
    if not discovery.root.exists():
        logging.getLogger(__name__).warning(
            "workspace_discovery.root does not exist: %s — discovery skipped",
            discovery.root,
        )
        return ()
    excluded = set(discovery.exclude)
    discovered: list[WorkspaceConfig] = []
    seen: set[str] = set()
    for child in sorted(discovery.root.iterdir(), key=lambda p: p.name.lower()):
        if child.name in excluded:
            continue
        if not child.is_dir() or child.is_symlink():
            continue
        if discovery.include_git_only and not (child / ".git").exists():
            continue
        alias = _workspace_alias_from_dir(child.name)
        if alias in seen:
            raise ConfigError(f"duplicate discovered workspace alias: {alias}")
        seen.add(alias)
        discovered.append(
            WorkspaceConfig(alias=alias, path=child, allow_write=discovery.allow_write)
        )
    return tuple(discovered)
