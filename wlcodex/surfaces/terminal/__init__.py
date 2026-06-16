"""Terminal Surface — raw remote-terminal primitives for Claude Remote / Codex CLI sessions.

Never imports from wlcodex.surfaces.product — isolation is enforced by design.
"""

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.renderer import (
    render_terminal_frame,
    render_terminal_frames_append,
    render_onsite_header,
    render_start_card,
    render_tail_output,
    render_pause_confirmation,
    render_resume_confirmation,
    render_detach_confirmation,
    render_return_to_cockpit,
    render_no_session_hint,
    render_busy_selector,
)
from wlcodex.surfaces.terminal.redaction import redact_terminal_text, redact_and_cap_frame
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
    "render_terminal_frames_append",
    "render_onsite_header",
    "render_start_card",
    "render_tail_output",
    "render_pause_confirmation",
    "render_resume_confirmation",
    "render_detach_confirmation",
    "render_return_to_cockpit",
    "render_no_session_hint",
    "render_busy_selector",
    "redact_terminal_text",
    "redact_and_cap_frame",
    "TerminalSessionManager",
    "TerminalCommand",
    "TerminalCommandKind",
    "route_terminal_command",
    "ClaudeTerminalAdapter",
    "CodexTerminalAdapter",
]
