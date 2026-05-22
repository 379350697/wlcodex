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


# ---------------------------------------------------------------------------
# Surface policy — per-surface configuration for rendering and delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalPolicy:
    """Terminal surface policy — controls frame rendering, redaction, and caps.

    Terminal is the 'onsite' surface: close-to-raw output with safety guards.
    """

    max_frame_chars: int = 3900
    redaction_enabled: bool = True
    body_mode: str = "semantic_blocks"
    block_idle_seconds: float = 2.0
    preview_enabled: bool = True
    preview_edit_min_interval_seconds: float = 2.0


@dataclass(frozen=True)
class ProductPolicy:
    """Product/Cockpit surface policy — controls compact status rendering.

    Product is the 'cockpit' surface: controlled, summarized, no raw output.
    """

    body_mode: str = "final"
    preview_enabled: bool = True
    preview_edit_min_interval_seconds: float = 2.0
    semantic_min_chars: int = 900
    semantic_max_chars: int = 3200
    final_chunk_chars: int = 3900


@dataclass(frozen=True)
class SurfacePolicy:
    """Combined surface configuration for both modes.

    Hydrated from TOML config at startup.  Both policies are immutable to
    prevent accidental mutation during a session.
    """

    terminal: TerminalPolicy = field(default_factory=TerminalPolicy)
    product: ProductPolicy = field(default_factory=ProductPolicy)
