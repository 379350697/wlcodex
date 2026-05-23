"""Conversation state helpers — pure functions without side effects."""

from __future__ import annotations

from datetime import datetime, timezone

from wlcodex.models import ConversationMode


def default_title(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    if len(cleaned) <= 40:
        return cleaned
    return cleaned[:39].rstrip() + "…"


def workbench_title_from_task(prompt: str) -> str:
    """Derive a short workbench title from the first task prompt.

    Takes up to ~30 chars, breaking at a word boundary when possible.
    """
    cleaned = " ".join(prompt.split())
    if len(cleaned) <= 30:
        return cleaned
    cut = cleaned[:30].rstrip()
    # Try to break at a natural boundary
    for sep in ("。", "，", "；", "、", ".", ",", ";", " "):
        idx = cut.rfind(sep)
        if idx > 10:
            return cut[:idx].rstrip() + "…"
    return cut + "…"


def mode_from_command(command_name: str) -> str:
    mapping: dict[str, str] = {
        "codex": ConversationMode.CODEX_DIRECT.value,
        "claude": ConversationMode.CLAUDE_DIRECT.value,
        "auto": ConversationMode.CHIEF_ENGINEER.value,
    }
    return mapping.get(command_name.lower(), ConversationMode.CHIEF_ENGINEER.value)


def is_direct_agent_mode(mode: str) -> bool:
    return mode in (ConversationMode.CODEX_DIRECT.value, ConversationMode.CLAUDE_DIRECT.value)


def relative_time(dt: datetime) -> str:
    """Human-friendly relative time label in Chinese.

    Returns strings like "2d", "3h", "5min", "刚刚".
    """
    now = datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    months = days // 30
    return f"{months}mo"
