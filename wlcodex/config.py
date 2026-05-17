from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib


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
class AppConfig:
    telegram: TelegramConfig
    codex: CodexConfig
    storage: StorageConfig
    display: DisplayConfig
    backend: BackendConfig
    approval: ApprovalConfig
    task: TaskConfig
    workspaces: tuple[WorkspaceConfig, ...]

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
