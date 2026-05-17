"""Conversation state helpers — pure functions without side effects."""

from __future__ import annotations

from wlcodex.models import ConversationMode


def default_title(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    if len(cleaned) <= 40:
        return cleaned
    return cleaned[:39].rstrip() + "…"


def mode_from_command(command_name: str) -> str:
    mapping: dict[str, str] = {
        "codex": ConversationMode.CODEX_DIRECT.value,
        "claude": ConversationMode.CLAUDE_DIRECT.value,
        "auto": ConversationMode.CHIEF_ENGINEER.value,
    }
    return mapping.get(command_name.lower(), ConversationMode.CODEX_DIRECT.value)


def is_direct_agent_mode(mode: str) -> bool:
    return mode in (ConversationMode.CODEX_DIRECT.value, ConversationMode.CLAUDE_DIRECT.value)
