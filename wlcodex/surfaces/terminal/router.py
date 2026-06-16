"""Terminal command router — parses /terminal commands into structured decisions.

This router only parses commands; it does not execute them. The parsed
TerminalCommand is consumed by the Telegram command handler or upper-level
dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TerminalCommandKind(Enum):
    SELECT_AGENT = "select_agent"       # /terminal codex | /terminal agent codex
    SWITCH_TO_PRODUCT = "switch_to_product"  # /terminal product
    DETACH = "detach"                   # /terminal detach
    TAIL = "tail"                       # /terminal tail
    PAUSE = "pause"                     # /terminal pause
    LEAVE = "leave"                     # /terminal leave (stop phone push)
    SHOW_STATUS = "show_status"         # /terminal (bare)


@dataclass(frozen=True)
class TerminalCommand:
    """Parsed result of a /terminal subcommand."""

    kind: TerminalCommandKind
    agent: str | None = None     # set for SELECT_AGENT
    mode: str | None = None      # set for SWITCH_TO_PRODUCT
    limit: int | None = None     # set for TAIL
    before_sequence: int | None = None  # set for paged TAIL


def route_terminal_command(text: str) -> TerminalCommand:
    """Parse a /terminal command string into a TerminalCommand.

    Supported forms:
        /terminal                     -> SHOW_STATUS
        /terminal codex               -> SELECT_AGENT agent=codex
        /terminal claude              -> SELECT_AGENT agent=claude
        /terminal agent codex         -> SELECT_AGENT agent=codex
        /terminal agent claude        -> SELECT_AGENT agent=claude
        /terminal product             -> SWITCH_TO_PRODUCT
        /terminal detach              -> DETACH
        /terminal tail                -> TAIL
        /terminal tail 50             -> TAIL limit=50
        /terminal tail before 120     -> TAIL before_sequence=120
        /terminal pause               -> PAUSE
        /terminal leave               -> LEAVE
    """
    stripped = text.strip()
    parts = stripped.split()

    if len(parts) < 2:
        # Bare "/terminal"
        return TerminalCommand(kind=TerminalCommandKind.SHOW_STATUS)

    sub = parts[1]

    if sub == "agent":
        if len(parts) >= 3 and parts[2] in ("claude", "codex"):
            return TerminalCommand(
                kind=TerminalCommandKind.SELECT_AGENT, agent=parts[2]
            )
        return TerminalCommand(kind=TerminalCommandKind.SHOW_STATUS)

    if sub == "product":
        return TerminalCommand(kind=TerminalCommandKind.SWITCH_TO_PRODUCT, mode="product")

    if sub == "detach":
        return TerminalCommand(kind=TerminalCommandKind.DETACH)

    if sub == "tail":
        limit: int | None = None
        before_sequence: int | None = None
        if len(parts) >= 3:
            if parts[2] == "before" and len(parts) >= 4:
                before_sequence = _positive_int(parts[3])
                if len(parts) >= 5:
                    limit = _positive_int(parts[4])
            else:
                limit = _positive_int(parts[2])
        return TerminalCommand(
            kind=TerminalCommandKind.TAIL,
            limit=limit,
            before_sequence=before_sequence,
        )

    if sub == "pause":
        return TerminalCommand(kind=TerminalCommandKind.PAUSE)

    if sub == "leave":
        return TerminalCommand(kind=TerminalCommandKind.LEAVE)

    if sub in ("claude", "codex"):
        return TerminalCommand(kind=TerminalCommandKind.SELECT_AGENT, agent=sub)

    # Unknown subcommand → treat as status display
    return TerminalCommand(kind=TerminalCommandKind.SHOW_STATUS)


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
