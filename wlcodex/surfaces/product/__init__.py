"""Product Surface — event-driven phone UX with speaker labels.

Hides raw diffs, tool output, and JSON. Never calls Terminal Surface.
"""

from wlcodex.surfaces.product.events import ProductDisplayEvent
from wlcodex.surfaces.product.router import ProductRouteDecision, product_route_guard
from wlcodex.surfaces.product.speaker import product_speaker_line
from wlcodex.surfaces.product.renderer import (
    render_cockpit_status,
    render_cockpit_completion,
    render_cockpit_failure,
    render_cockpit_approval,
    render_cockpit_queued,
    render_product_display_event,
)

__all__ = [
    "ProductDisplayEvent",
    "ProductRouteDecision",
    "product_route_guard",
    "product_speaker_line",
    "render_cockpit_status",
    "render_cockpit_completion",
    "render_cockpit_failure",
    "render_cockpit_approval",
    "render_cockpit_queued",
    "render_product_display_event",
]
