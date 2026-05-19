"""Claude terminal adapter — thin shell over a Claude backend for terminal surface.

The adapter does NOT own subprocess lifecycle or strategy selection. It only
provides the send_input protocol that TerminalSessionManager expects.

Real strategy implementations may later map to:
  - official_remote_control: Claude Remote Control attach
  - stream_json: Claude Code --output-format stream-json with multi-turn input
  - pty: PTY/tmux capture fallback

The V1 real implementation uses ``claude --resume <session_id> -p <text>``
to send input and capture the stream-json output.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ClaudeTerminalAdapter:
    """Terminal surface adapter for Claude Code sessions.

    Delegates input to a backend that implements send_terminal_input(session_id, text)
    and returns an AgentResult with the captured output.
    """

    def __init__(self, backend: object):
        self._backend = backend

    async def send_input(self, session_ref, text: str) -> Any:
        """Send user input to the Claude session (manager protocol).

        Returns an AgentResult when the backend is real;
        returns None for fake backends that just record the call.
        """
        sid = session_ref.external_session_id
        logger.info("Claude terminal input: session=%s text=%r", sid, text)
        return await self._backend.send_terminal_input(sid, text)

    async def send_input_by_session_id(self, session_id: str, text: str) -> Any:
        """Convenience: send input by raw session id, bypassing session_ref."""
        return await self._backend.send_terminal_input(session_id, text)
