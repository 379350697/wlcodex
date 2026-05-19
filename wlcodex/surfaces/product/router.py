"""Product surface route guard.

phase=implementation -> record pending context
phase=verification    -> record pending context
otherwise             -> continue product conversation
"""

from enum import Enum


class ProductRouteDecision(Enum):
    CONTINUE_CONVERSATION = "continue_conversation"
    RECORD_PENDING_CONTEXT = "record_pending_context"


_PENDING_CONTEXT_PHASES = frozenset({"implementation", "verification"})


def product_route_guard(phase: str) -> ProductRouteDecision:
    """Decide whether to continue or record pending context for a given phase."""
    if phase in _PENDING_CONTEXT_PHASES:
        return ProductRouteDecision.RECORD_PENDING_CONTEXT
    return ProductRouteDecision.CONTINUE_CONVERSATION
