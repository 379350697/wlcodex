"""Pure dataclasses for the dual-surface core.

No Telegram, no SQLite — just data definitions.
"""

from dataclasses import dataclass, field
from enum import Enum


class SurfaceMode(Enum):
    PRODUCT = "product"
    TERMINAL = "terminal"


class SurfaceRouteDecision(Enum):
    PRODUCT_CONVERSATION = "product_conversation"
    TERMINAL_INPUT = "terminal_input"


@dataclass(frozen=True)
class SurfaceCursor:
    surface: str
    position: int = 0


@dataclass(frozen=True)
class ModeSwitchCheckpoint:
    conversation_id: int
    chat_id: int
    from_mode: SurfaceMode
    to_mode: SurfaceMode
    active_agent: str
    active_phase: str
    workspace_alias: str
    codex_thread_id: str
    codex_turn_id: str
    claude_session_id: str
    product_cursor: SurfaceCursor = field(default_factory=lambda: SurfaceCursor(surface="product"))
    terminal_cursor: SurfaceCursor = field(default_factory=lambda: SurfaceCursor(surface="terminal"))
