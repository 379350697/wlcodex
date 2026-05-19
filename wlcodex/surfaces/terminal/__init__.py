"""Terminal Surface — raw remote-terminal primitives for Claude Remote / Codex CLI sessions."""

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.renderer import render_terminal_frame
from wlcodex.surfaces.terminal.redaction import redact_terminal_text
from wlcodex.surfaces.terminal.manager import TerminalSessionManager
from wlcodex.surfaces.terminal.router import (
    TerminalCommand,
    TerminalCommandKind,
    route_terminal_command,
)
from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter
from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

__all__ = [
    "TerminalFrame",
    "TerminalSessionRef",
    "render_terminal_frame",
    "redact_terminal_text",
    "TerminalSessionManager",
    "TerminalCommand",
    "TerminalCommandKind",
    "route_terminal_command",
    "ClaudeTerminalAdapter",
    "CodexTerminalAdapter",
]
