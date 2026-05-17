"""Telegram BotCommands definitions and registration helper.

Primary commands are ordered around daily conversation use.
Legacy task commands are hidden from the menu.
"""

from __future__ import annotations

_PRIMARY_COMMANDS: list[tuple[str, str]] = [
    ("new", "新对话"),
    ("codex", "问 Codex"),
    ("claude", "叫 Claude"),
    ("auto", "总工程师模式"),
    ("stop", "停止当前运行"),
    ("status", "当前状态"),
    ("sessions", "会话列表"),
    ("switch", "切换工作区"),
    ("model", "切换模型"),
    ("diff", "查看 diff"),
    ("files", "相关文件"),
    ("verify", "Codex 验收"),
    ("health", "系统健康"),
    ("help", "帮助"),
]


def build_bot_commands() -> list[tuple[str, str]]:
    """Return primary bot commands as (command, description) pairs.

    Legacy commands like /task, /continue, /steer, /tail, /events,
    /archive, /fork, /codex-sessions are excluded from the menu to
    keep the primary UX conversation-first.
    """
    return list(_PRIMARY_COMMANDS)
