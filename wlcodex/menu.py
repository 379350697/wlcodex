"""Telegram commands for the compatibility bridge.

Telegram is no longer a task-creation surface.  The command menu must not
advertise the historical Workbench flow, because Telegram menus are global and
cannot be made conditional on a conversation's ``legacy_compatible`` flag.
"""

from __future__ import annotations

_PRIMARY_COMMANDS: list[tuple[str, str]] = [
    ("native", "开始直接会话"),
    ("relay", "创建协作任务"),
    ("new", "打开新入口"),
    ("help", "兼容说明"),
]

_NATURAL_COMMANDS = _PRIMARY_COMMANDS


def build_bot_commands(profile: str = "natural") -> list[tuple[str, str]]:
    """Return bot commands as (command, description) pairs.

    Historical commands remain handled for persisted legacy conversations,
    but are intentionally excluded from the global menu.
    """
    if profile == "natural":
        return list(_NATURAL_COMMANDS)
    return list(_PRIMARY_COMMANDS)
