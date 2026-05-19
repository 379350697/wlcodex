"""Event type constants for the dual-surface architecture.

These match the design spec in docs/superpowers/specs/...dual-surface...design.md.
"""

CONVERSATION_MODE_SWITCHED = "conversation.mode.switched"
SURFACE_CURSOR_ADVANCED = "surface.cursor.advanced"
TERMINAL_SESSION_ATTACHED = "terminal.session.attached"
TERMINAL_SESSION_DETACHED = "terminal.session.detached"
TERMINAL_SESSION_INPUT_SENT = "terminal.session.input.sent"
TERMINAL_SESSION_OUTPUT_FRAME = "terminal.session.output.frame"
TERMINAL_SESSION_ABORTED = "terminal.session.aborted"
PRODUCT_DISPLAY_FRAME = "product.display.frame"
PRODUCT_PENDING_CONTEXT_RECORDED = "product.pending_context.recorded"
SURFACE_DELIVERY_SENT = "surface.delivery.sent"
SURFACE_DELIVERY_EDITED = "surface.delivery.edited"
SURFACE_DELIVERY_FAILED = "surface.delivery.failed"
