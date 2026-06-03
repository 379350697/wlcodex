from pathlib import Path

import pytest

from wlcodex.config import ConfigError, load_config


BASE = """
[telegram]
bot_token_env = "TOKEN"
allowed_user_ids = [1]

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "{sqlite_path}"
task_log_dir = "{task_log_dir}"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "{workspace}"
allow_write = true
"""


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "wlcodex.toml"
    path.write_text(
        BASE.format(
            sqlite_path=tmp_path / "db.sqlite3",
            task_log_dir=tmp_path / "logs",
            workspace=tmp_path,
        )
        + extra,
        encoding="utf-8",
    )
    return path


def test_native_agents_default_to_codex_only_compatibility(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))

    assert config.native_agents.enabled is False
    assert config.native_agents.default_provider == "codex"
    assert config.native_agents.codex.enabled is False
    assert config.native_agents.claude.enabled is False
    assert config.native_agents.claude.engine == "cli-local"
    assert config.native_agents.antigravity.engine == "cli-local"


def test_native_agents_parse_claude_sdk_deepseek(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            """
[native_agents]
enabled = true
default_provider = "claude"

[native_agents.claude]
enabled = true
engine = "sdk-deepseek"

[native_agents.claude.sdk_deepseek]
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"
ccswitch_fallback_enabled = false
ccswitch_db_path = "/tmp/cc-switch.db"
""",
        )
    )

    assert config.native_agents.enabled is True
    assert config.native_agents.default_provider == "claude"
    assert config.native_agents.claude.enabled is True
    assert config.native_agents.claude.engine == "sdk-deepseek"
    assert config.native_agents.claude.sdk_deepseek.model == "deepseek-v4-pro"
    assert config.native_agents.claude.sdk_deepseek.ccswitch_fallback_enabled is False
    assert config.native_agents.claude.sdk_deepseek.ccswitch_db_path == "/tmp/cc-switch.db"


def test_native_agents_parse_claude_cli_local_effort(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            """
[native_agents]
enabled = true
default_provider = "claude"

[native_agents.claude]
enabled = true
engine = "cli-local"

[native_agents.claude.cli_local]
binary = "/Users/wl/.local/bin/claude"
model = "deepseek-v4-pro"
effort = "xhigh"
permission_mode = "acceptEdits"
""",
        )
    )

    assert config.native_agents.claude.cli_local.model == "deepseek-v4-pro"
    assert config.native_agents.claude.cli_local.effort == "xhigh"
    assert config.native_agents.claude.cli_local.permission_mode == "acceptEdits"


def test_native_agents_parse_antigravity_cli_local(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            """
[native_agents]
enabled = true
default_provider = "antigravity"

[native_agents.antigravity]
enabled = true
engine = "cli-local"

[native_agents.antigravity.cli_local]
binary = "/Users/wl/.local/bin/agy"
print_timeout = "9m0s"
dangerously_skip_permissions = true
sandbox = true
""",
        )
    )

    assert config.native_agents.antigravity.enabled is True
    assert config.native_agents.antigravity.engine == "cli-local"
    assert config.native_agents.antigravity.cli_local.binary == "/Users/wl/.local/bin/agy"
    assert config.native_agents.antigravity.cli_local.print_timeout == "9m0s"
    assert config.native_agents.antigravity.cli_local.dangerously_skip_permissions is True
    assert config.native_agents.antigravity.cli_local.sandbox is True


def test_native_agents_parse_antigravity_sdk(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            """
[native_agents.antigravity]
enabled = true
engine = "sdk"
""",
        )
    )

    assert config.native_agents.antigravity.engine == "sdk"


def test_native_agents_reject_unknown_antigravity_engine(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match="native_agents.antigravity.engine must be cli-local or sdk",
    ):
        load_config(
            _write_config(
                tmp_path,
                """
[native_agents.antigravity]
enabled = true
engine = "remote"
""",
            )
        )


def test_native_agents_reject_claude_engine_as_provider(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError,
        match="default_provider must be codex, claude, or antigravity",
    ):
        load_config(
            _write_config(
                tmp_path,
                """
[native_agents]
enabled = true
default_provider = "claude-deepseek"
""",
            )
        )


def test_native_agents_reject_legacy_dual_claude_enabled_flags(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ConfigError,
        match="claude engine must be selected by native_agents.claude.engine",
    ):
        load_config(
            _write_config(
                tmp_path,
                """
[native_agents.claude]
enabled = true
engine = "cli-local"

[native_agents.claude.cli_local]
enabled = true

[native_agents.claude.sdk_deepseek]
enabled = true
""",
            )
        )


def test_codex_native_enabled_enables_native_agents_codex_compatibility(
    tmp_path: Path,
) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            """
[codex_native]
enabled = true
""",
        )
    )

    assert config.native_agents.enabled is True
    assert config.native_agents.default_provider == "codex"
    assert config.native_agents.codex.enabled is True
