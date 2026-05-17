from pathlib import Path

import pytest

from wlcodex.config import ConfigError, load_config


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
    assert config.context_budget.codex_to_claude_tokens == 1500
    assert config.orchestration.max_verify_rounds == 3
    assert config.streaming.edit_min_interval_seconds == 1.0
    assert config.menu.register_bot_commands is True


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
