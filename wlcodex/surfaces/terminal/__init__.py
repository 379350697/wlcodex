"""Terminal Surface — raw remote-terminal primitives for Claude Remote / Codex CLI sessions."""

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.renderer import render_terminal_frame
from wlcodex.surfaces.terminal.redaction import redact_terminal_text

__all__ = [
    "TerminalFrame",
    "TerminalSessionRef",
    "render_terminal_frame",
    "redact_terminal_text",
]
