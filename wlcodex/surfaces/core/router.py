"""Pure routing function — no Telegram, no SQLite."""

from wlcodex.surfaces.core.models import SurfaceMode, SurfaceRouteDecision


def route_text_by_mode(
    mode: SurfaceMode,
    text: str,
    selected_terminal_agent: str,
) -> SurfaceRouteDecision:
    if mode is SurfaceMode.PRODUCT:
        return SurfaceRouteDecision.PRODUCT_CONVERSATION
    if mode is SurfaceMode.TERMINAL:
        return SurfaceRouteDecision.TERMINAL_INPUT
    raise ValueError(f"Unknown surface mode: {mode}")
