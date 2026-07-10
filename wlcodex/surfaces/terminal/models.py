"""Pure dataclasses for the Terminal Surface.

No Telegram, no backend adapters — just data definitions.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalSessionRef:
    """A lightweight reference to an external terminal session.

    The Terminal Surface uses this to track which external agent session
    (Claude or Codex) is attached for a given conversation, without owning
    the actual subprocess or websocket.
    """

    conversation_id: int
    agent: str  # "claude" or "codex"
    strategy: str  # "official_remote_control" | "stream_json" | "app_server" | "exec_json" | "pty"
    external_session_id: str  # Claude session_id or Codex thread_id
    status: str = "detached"  # "attached" | "detached" | "orphaned"


@dataclass(frozen=True)
class TerminalFrame:
    """A single frame of raw terminal output, ready for rendering and redaction.

    Frames are immutable snapshots. The terminal cursor advances one frame at
    a time, and the renderer turns each frame into Telegram-safe text.
    """

    conversation_id: int
    agent: str  # "claude" | "codex" | "system" | "user"
    phase: str  # "analysis" | "implementation" | "verification" | "planning" | "startup" | ...
    text: str
    frame_kind: str = "stdout"  # "stdout" | "stderr" | "diff" | "tool" | "system" | "error"
    sequence: int = 0
