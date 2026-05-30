from pathlib import Path
import tomllib

import pytest

from wlcodex.team_capabilities import audit_role_capability_config
from wlcodex.config import (
    AdaptiveTeamConfig,
    AppConfig,
    ApprovalConfig,
    BackendConfig,
    CodexConfig,
    ConfigError,
    DisplayConfig,
    StorageConfig,
    TaskConfig,
    TelegramConfig,
    WorkspaceConfig,
    load_config,
)


def test_example_adaptive_team_role_capabilities_pass_audit() -> None:
    example_path = Path(__file__).resolve().parents[1] / "config" / "wlcodex.example.toml"
    data = tomllib.loads(example_path.read_text(encoding="utf-8"))

    findings = audit_role_capability_config(
        data["adaptive_team"]["role_capabilities"]
    )

    assert findings == ()


def _minimal_app_config() -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(
            bot_token_env="TOKEN",
            allowed_user_ids=frozenset({123}),
        ),
        codex=CodexConfig(
            binary="codex",
            app_server_host="127.0.0.1",
            app_server_port=17431,
            approval_policy="on-request",
            sandbox="workspace-write",
        ),
        storage=StorageConfig(
            sqlite_path=Path("runtime/wlcodex.sqlite3"),
            task_log_dir=Path("runtime/tasks"),
            worktree_root=Path("runtime/worktrees"),
        ),
        display=DisplayConfig(
            status_update_min_interval_seconds=2,
            tail_lines=40,
            diff_max_chars=3500,
        ),
        backend=BackendConfig(
            startup_timeout_seconds=15,
            request_timeout_seconds=60,
            codex_prompt_idle_timeout_seconds=300,
            codex_analysis_hard_timeout_seconds=3600,
            codex_verification_hard_timeout_seconds=3600,
            event_log_max_chars=20000,
        ),
        approval=ApprovalConfig(
            callback_timeout_seconds=3600,
            allow_session_approval=True,
        ),
        task=TaskConfig(
            max_running_seconds=7200,
            max_queued_seconds=1800,
            max_waiting_approval_seconds=3600,
            watchdog_interval_seconds=60,
            backend_dead_grace_seconds=120,
        ),
        workspaces=(
            WorkspaceConfig(
                alias="wlcodex",
                path=Path("/tmp/wlcodex"),
                allow_write=True,
            ),
        ),
    )


def test_default_surface_mode_is_product() -> None:
    """TelegramConfig defaults to product surface mode."""
    from wlcodex.config import TelegramConfig
    config = TelegramConfig(
        bot_token_env="T", allowed_user_ids=frozenset({123})
    )
    assert config.default_surface_mode == "product"


def test_terminal_surface_config_defaults() -> None:
    """TerminalSurfaceConfig defaults: disabled, claude agent, 3500 chars, redaction on."""
    from wlcodex.config import TerminalSurfaceConfig
    config = TerminalSurfaceConfig()
    assert config.enabled is False
    assert config.default_agent == "claude"
    assert config.max_frame_chars == 3500
    assert config.redaction_enabled is True


def _write_live_stream_config(tmp_path: Path, *, live_stream_block: str = "") -> Path:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "/tmp/wlcodex"

{live_stream_block}
""".strip(),
        encoding="utf-8",
    )
    return config_path


def test_live_stream_config_defaults_disabled(tmp_path: Path) -> None:
    config_path = _write_live_stream_config(tmp_path)
    config = load_config(config_path)

    assert config.live_stream.enabled is False
    assert config.live_stream.host == "127.0.0.1"
    assert config.live_stream.port == 18731
    assert config.live_stream.access_token_env == "WLCODEX_LIVE_STREAM_TOKEN"
    assert config.live_stream.allow_unauthenticated_loopback is True
    assert config.codex_native.enabled is False
    assert config.codex_native.transport == "app-server"
    assert config.codex_native.sock_path is None
    assert config.codex_native.listen_endpoint == "ws://127.0.0.1:18742"


def test_live_stream_config_can_be_enabled(tmp_path: Path) -> None:
    config_path = _write_live_stream_config(
        tmp_path,
        live_stream_block="""
[live_stream]
enabled = true
host = "127.0.0.1"
port = 18732
access_token_env = "CUSTOM_LIVE_STREAM_TOKEN"
allow_unauthenticated_loopback = false
""",
    )

    config = load_config(config_path)

    assert config.live_stream.enabled is True
    assert config.live_stream.host == "127.0.0.1"
    assert config.live_stream.port == 18732
    assert config.live_stream.access_token_env == "CUSTOM_LIVE_STREAM_TOKEN"
    assert config.live_stream.allow_unauthenticated_loopback is False


def test_codex_native_config_can_be_enabled(tmp_path: Path) -> None:
    config_path = _write_live_stream_config(
        tmp_path,
        live_stream_block="""
[codex_native]
enabled = true
transport = "proxy"
sock_path = "~/wlcodex-native.sock"
listen_endpoint = "ws://127.0.0.1:19999"
""",
    )

    config = load_config(config_path)

    assert config.codex_native.enabled is True
    assert config.codex_native.transport == "proxy"
    assert config.codex_native.sock_path == Path.home() / "wlcodex-native.sock"
    assert config.codex_native.listen_endpoint == "ws://127.0.0.1:19999"


def test_codex_native_config_rejects_unknown_transport(tmp_path: Path) -> None:
    config_path = _write_live_stream_config(
        tmp_path,
        live_stream_block="""
[codex_native]
enabled = true
transport = "stdio"
""",
    )

    with pytest.raises(ConfigError, match="codex_native.transport"):
        load_config(config_path)


def test_live_stream_config_rejects_non_loopback_host(tmp_path: Path) -> None:
    config_path = _write_live_stream_config(
        tmp_path,
        live_stream_block="""
[live_stream]
enabled = true
host = "0.0.0.0"
port = 18731
""",
    )

    with pytest.raises(ConfigError, match="live_stream.host"):
        load_config(config_path)


def test_conversation_config_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "/tmp/wlcodex"
        """.strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert config.conversation.enabled is True
    assert config.conversation.default_mode == "chief_engineer"
    assert config.claude.enabled is False
    assert config.claude.permission_mode == "acceptEdits"
    assert config.claude.model == "deepseek-v4-pro"
    assert config.claude.effort == "max"
    assert config.claude.request_timeout_seconds == 3600


def test_codex_home_config_is_optional_and_expanded(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431
codex_home = "~/wlcodex-codex-home"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "/tmp/wlcodex"
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.codex.codex_home == Path.home() / "wlcodex-codex-home"
    assert config.claude.stream_idle_timeout_seconds == 600
    assert config.context_budget.codex_to_claude_tokens == 1500
    assert config.orchestration.max_verify_rounds == 0
    assert config.streaming.edit_min_interval_seconds == 1.0
    assert config.menu.register_bot_commands is True
    assert config.backend.request_timeout_seconds == 60
    assert config.backend.codex_prompt_idle_timeout_seconds == 300
    assert config.backend.codex_analysis_hard_timeout_seconds == 3600
    assert config.backend.codex_verification_hard_timeout_seconds == 3600
    assert config.adaptive_team.enabled is True
    assert config.adaptive_team.model_profiles["codex_gpt"] == "codex"
    assert config.adaptive_team.model_profiles["claude_deepseek"] == "claude"
    assert config.adaptive_team.assignments["investigator"] == ("codex_gpt",)
    assert config.adaptive_team.assignments["architect"] == ("codex_gpt",)
    assert config.adaptive_team.assignments["implementer"] == (
        "claude_deepseek",
        "codex_gpt",
    )
    assert config.adaptive_team.assignments["tester"] == ("codex_gpt",)
    assert config.adaptive_team.assignments["auditor"] == ("codex_gpt",)


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("assignments", "implementer"),
        ("role_skills", "investigator"),
        ("role_capabilities", "investigator"),
    ],
)
def test_adaptive_team_rejects_scalar_role_lists(
    tmp_path: Path,
    table: str,
    key: str,
) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[adaptive_team.{table}]
{key} = "codex_gpt"

[[workspaces]]
alias = "wlcodex"
path = "/tmp/wlcodex"
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError,
        match=rf"adaptive_team\.{table}\.{key} must be a list of strings",
    ):
        load_config(config_path)


def test_load_config_reads_adaptive_team_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[adaptive_team]
enabled = false

[adaptive_team.model_profiles]
codex_gpt = "codex"
strong_model = "codex"
local_claude = "claude"

[adaptive_team.assignments]
director = ["strong_model"]
investigator = ["codex_gpt"]
architect = ["strong_model"]
implementer = ["local_claude", "codex_gpt"]
tester = ["codex_gpt"]
auditor = ["strong_model"]

[adaptive_team.role_skills]
investigator = ["systematic-debugging", "gitnexus-exploring"]
architect = ["gitnexus-impact-analysis"]
implementer = ["test-driven-development", "verification-before-completion"]

[adaptive_team.role_capabilities]
investigator = ["read", "shell_readonly", "logs", "gitnexus"]
architect = ["read", "gitnexus"]
implementer = ["read", "write", "shell", "tests"]

[[workspaces]]
alias = "wlcodex"
path = "/tmp/wlcodex"
        """.strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.adaptive_team.enabled is False
    assert config.adaptive_team.model_profiles["strong_model"] == "codex"
    assert config.adaptive_team.model_profiles["local_claude"] == "claude"
    assert config.adaptive_team.assignments["director"] == ("strong_model",)
    assert config.adaptive_team.assignments["implementer"] == (
        "local_claude",
        "codex_gpt",
    )
    assert config.adaptive_team.role_skills["investigator"] == (
        "systematic-debugging",
        "gitnexus-exploring",
    )
    assert config.adaptive_team.role_capabilities["implementer"] == (
        "read",
        "write",
        "shell",
        "tests",
    )


def test_adaptive_team_config_copies_incoming_nested_values() -> None:
    model_profiles = {"codex_gpt": "codex"}
    assignments = {"implementer": ["codex_gpt"]}
    role_skills = {"investigator": ["systematic-debugging"]}
    role_capabilities = {"investigator": ["read"]}

    config = AdaptiveTeamConfig(
        model_profiles=model_profiles,
        assignments=assignments,
        role_skills=role_skills,
        role_capabilities=role_capabilities,
    )

    model_profiles["codex_gpt"] = "changed"
    assignments["implementer"].append("claude_deepseek")
    role_skills["investigator"].append("gitnexus-exploring")
    role_capabilities["investigator"].append("shell_readonly")

    assert config.model_profiles == {"codex_gpt": "codex"}
    assert config.assignments == {"implementer": ("codex_gpt",)}
    assert config.role_skills == {"investigator": ("systematic-debugging",)}
    assert config.role_capabilities == {"investigator": ("read",)}


def test_app_config_default_adaptive_team_is_not_shared() -> None:
    first = _minimal_app_config()
    second = _minimal_app_config()

    first.adaptive_team.model_profiles["extra_profile"] = "codex"

    assert "extra_profile" not in second.adaptive_team.model_profiles


def test_load_config_reads_workspace_and_token_env(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.telegram.bot_token_env == "WLCODEX_TELEGRAM_BOT_TOKEN"
    assert config.telegram.allowed_user_ids == frozenset({123})
    assert config.workspace_by_alias("demo").path == Path("/tmp/demo")


def test_load_config_rejects_duplicate_workspace_alias(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo1"
allow_write = true

[[workspaces]]
alias = "demo"
path = "/tmp/demo2"
allow_write = true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate workspace alias"):
        load_config(config_path)


def test_task_config_defaults_when_section_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.task.max_running_seconds == 7200
    assert config.task.max_queued_seconds == 1800
    assert config.task.max_waiting_approval_seconds == 3600
    assert config.task.watchdog_interval_seconds == 60
    assert config.task.backend_dead_grace_seconds == 120


def test_backend_config_reads_codex_liveness_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[backend]
startup_timeout_seconds = 11
request_timeout_seconds = 7
event_log_max_chars = 12345
codex_prompt_idle_timeout_seconds = 45
codex_analysis_hard_timeout_seconds = 600
codex_verification_hard_timeout_seconds = 900

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.backend.startup_timeout_seconds == 11
    assert config.backend.request_timeout_seconds == 7
    assert config.backend.event_log_max_chars == 12345
    assert config.backend.codex_prompt_idle_timeout_seconds == 45
    assert config.backend.codex_analysis_hard_timeout_seconds == 600
    assert config.backend.codex_verification_hard_timeout_seconds == 900


def test_task_config_reads_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[task]
max_running_seconds = 30
max_queued_seconds = 20
max_waiting_approval_seconds = 10
watchdog_interval_seconds = 5
backend_dead_grace_seconds = 7

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.task.max_running_seconds == 30
    assert config.task.max_queued_seconds == 20
    assert config.task.max_waiting_approval_seconds == 10
    assert config.task.watchdog_interval_seconds == 5
    assert config.task.backend_dead_grace_seconds == 7


def test_load_config_includes_default_interaction_section(tmp_path):
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.interaction.profile == "natural"
    assert config.interaction.streaming_enabled is True
    assert config.interaction.show_footer is False
    assert config.interaction.edit_min_interval_seconds == 1.0


def test_load_config_accepts_cockpit_interaction_profile(tmp_path):
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[interaction]
profile = "cockpit"
streaming_enabled = false
show_footer = true
edit_min_interval_seconds = 2.5

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.interaction.profile == "cockpit"
    assert config.interaction.streaming_enabled is False
    assert config.interaction.show_footer is True
    assert config.interaction.edit_min_interval_seconds == 2.5


def test_load_config_reads_claude_permission_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[claude]
enabled = true
binary = "claude"
model = "deepseek4pro"
effort = "max"
permission_mode = "只规划"
stream_idle_timeout_seconds = 42

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.claude.permission_mode == "plan"
    assert config.claude.model == "deepseek4pro"
    assert config.claude.effort == "max"
    assert config.claude.stream_idle_timeout_seconds == 42


def test_terminal_surface_config_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true
default_surface_mode = "product"

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[terminal]
enabled = true
default_agent = "codex"
max_frame_chars = 5000
redaction_enabled = false

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.terminal.enabled is True
    assert config.terminal.default_agent == "codex"
    assert config.terminal.max_frame_chars == 5000
    assert config.terminal.redaction_enabled is False
    assert config.telegram.default_surface_mode == "product"


def test_terminal_surface_config_defaults_when_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.terminal.enabled is False
    assert config.terminal.default_agent == "claude"
    assert config.terminal.max_frame_chars == 3500
    assert config.terminal.redaction_enabled is True
    assert config.telegram.default_surface_mode == "product"


def test_telegram_output_config_defaults(tmp_path):
    from wlcodex.config import load_config

    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "."
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.telegram_output.preview_enabled is True
    assert config.telegram_output.product_body_mode == "final"
    assert config.telegram_output.terminal_body_mode == "semantic_blocks"
    assert config.telegram_output.semantic_min_chars == 900
    assert config.telegram_output.semantic_max_chars == 3200
    assert config.telegram_output.final_chunk_chars == 3900


def test_surface_policy_builds_from_config(tmp_path: Path) -> None:
    """surface_policy() must return a valid SurfacePolicy with correct field names."""
    from wlcodex.config import load_config

    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "."
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    policy = config.surface_policy()
    # Terminal policy fields (defaults from config, not model defaults)
    assert policy.terminal.max_frame_chars == 3500
    assert policy.terminal.redaction_enabled is True
    assert policy.terminal.body_mode == "semantic_blocks"
    # Product policy fields — verify the typo fix (semantic, not semaphore)
    assert policy.product.semantic_min_chars == 900
    assert policy.product.semantic_max_chars == 3200
    assert policy.product.final_chunk_chars == 3900
    assert policy.product.body_mode == "final"


def test_terminal_default_agent_rejects_invalid_value(tmp_path: Path) -> None:
    """ConfigError must be raised when terminal.default_agent is not claude or codex."""
    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        """
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[terminal]
enabled = true
default_agent = "gemini"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "demo"
path = "/tmp/demo"
allow_write = true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="terminal.default_agent"):
        load_config(config_path)


def test_workspace_discovery_adds_git_children(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    lightfee = root / "LightFee"
    lightfee.mkdir()
    (lightfee / ".git").mkdir()

    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[workspace_discovery]
enabled = true
root = "{root}"
include_git_only = true
allow_write = true

[[workspaces]]
alias = "wlcodex"
path = "{tmp_path / 'wlcodex'}"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.workspace_by_alias("lightfee").path == lightfee
    assert config.workspace_by_alias("lightfee").allow_write is True


def test_explicit_workspace_overrides_discovered_alias(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "LightFee"
    repo.mkdir()
    (repo / ".git").mkdir()
    explicit = tmp_path / "explicit-lightfee"

    config_path = tmp_path / "wlcodex.toml"
    config_path.write_text(
        f"""
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [123]

[codex]
app_server_host = "127.0.0.1"
app_server_port = 17431

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[workspace_discovery]
enabled = true
root = "{root}"
include_git_only = true

[[workspaces]]
alias = "lightfee"
path = "{explicit}"
allow_write = true
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.workspace_by_alias("lightfee").path == explicit
