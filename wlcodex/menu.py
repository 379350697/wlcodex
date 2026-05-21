"""Telegram BotCommands definitions and registration helper.

Primary commands are ordered around daily conversation use.
Diagnostic-only legacy commands are hidden from the menu.
"""

from __future__ import annotations

_PRIMARY_COMMANDS: list[tuple[str, str]] = [
    ("new", "新工作台"),
    ("codex", "问 Codex"),
    ("claude", "叫 Claude"),
    ("auto", "总工程师模式"),
    ("stop", "停止当前运行"),
    ("status", "当前状态"),
    ("sessions", "会话列表"),
    ("history", "历史工作台"),
    ("workspaces", "可用工作区"),
    ("switch", "切换工作区"),
    ("model", "切换模型"),
    ("claude_mode", "Claude 权限"),
    ("diff", "查看 diff"),
    ("files", "相关文件"),
    ("verify", "Codex 验收"),
    ("health", "系统健康"),
    ("help", "帮助"),
]

_NATURAL_COMMANDS: list[tuple[str, str]] = [
    ("new", "新工作台"),
    ("status", "状态"),
    ("terminal", "接管现场"),
    ("history", "历史工作台"),
    ("workspaces", "工作区"),
    ("diff", "变更"),
    ("settings", "设置"),
    ("help", "帮助"),
]


def build_bot_commands(profile: str = "natural") -> list[tuple[str, str]]:
    """Return bot commands as (command, description) pairs.

    Diagnostic-only legacy commands are excluded from the menu to keep
    the primary UX conversation-first.
    """
    if profile == "natural":
        return list(_NATURAL_COMMANDS)
    return list(_PRIMARY_COMMANDS)
