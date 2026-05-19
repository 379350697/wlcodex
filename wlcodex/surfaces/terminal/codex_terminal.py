"""Codex terminal adapter — thin shell over a Codex backend for terminal surface.

The adapter does NOT own subprocess lifecycle or strategy selection. It only
provides the send_input protocol that TerminalSessionManager expects.

Real strategy implementations may later map to:
  - app_server: Codex app-server thread/turn events as terminal frames
  - exec_json: codex exec --json JSONL stream
  - pty: PTY/tmux capture fallback

For now, this adapter works with a fake backend that records steer_thread
calls, enabling the manager to be tested without a real Codex process.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class CodexTerminalAdapter:
    """Terminal surface adapter for Codex CLI / app-server sessions.

    Delegates input to a backend that implements steer_thread(thread_id, text).
    """

    def __init__(self, backend: object):
        self._backend = backend

    async def send_input(self, session_ref, text: str) -> None:
        """Send user input to the Codex session (manager protocol)."""
        tid = session_ref.external_session_id
        logger.info("Codex terminal input: thread=%s text=%r", tid, text)
        await self._backend.steer_thread(tid, text)

    async def send_input_by_thread_id(self, thread_id: str, text: str) -> None:
        """Convenience: send input by raw thread id, bypassing session_ref."""
        await self._backend.steer_thread(thread_id, text)
