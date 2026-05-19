"""Terminal session manager — tracks session refs and delegates to adapters.

The manager tracks which external sessions are attached for each conversation.
It does not know Telegram, subprocess creation, or rendering — it only manages
TerminalSessionRef objects and delegates input to agent-specific adapters.
"""

from __future__ import annotations

import logging

from wlcodex.surfaces.terminal.models import TerminalSessionRef

logger = logging.getLogger(__name__)


class TerminalSessionManager:
    """Tracks terminal session refs and delegates input to adapters."""

    def __init__(self, adapters: dict[str, object] | None = None):
        self._adapters: dict[str, object] = dict(adapters or {})
        self._sessions: dict[int, list[TerminalSessionRef]] = {}

    def attach(
        self,
        *,
        conversation_id: int,
        agent: str,
        strategy: str,
        external_session_id: str,
    ) -> TerminalSessionRef:
        """Create and track a new attached session ref."""
        if agent not in self._adapters:
            raise ValueError(
                f"No adapter registered for agent '{agent}'. "
                f"Available: {list(self._adapters)}"
            )
        ref = TerminalSessionRef(
            conversation_id=conversation_id,
            agent=agent,
            strategy=strategy,
            external_session_id=external_session_id,
            status="attached",
        )
        self._sessions.setdefault(conversation_id, []).append(ref)
        logger.info("Terminal session attached: conv=%d agent=%s id=%s",
                     conversation_id, agent, external_session_id)
        return ref

    def detach(self, ref: TerminalSessionRef) -> TerminalSessionRef:
        """Mark a session ref as detached."""
        detached = TerminalSessionRef(
            conversation_id=ref.conversation_id,
            agent=ref.agent,
            strategy=ref.strategy,
            external_session_id=ref.external_session_id,
            status="detached",
        )
        # Replace matching ref in the list.
        ses = self._sessions.get(ref.conversation_id, [])
        for i, s in enumerate(ses):
            if s.external_session_id == ref.external_session_id and s.agent == ref.agent:
                ses[i] = detached
                break
        logger.info("Terminal session detached: conv=%d agent=%s id=%s",
                     ref.conversation_id, ref.agent, ref.external_session_id)
        return detached

    async def send_input(self, ref: TerminalSessionRef, text: str) -> None:
        """Send user input text to the agent adapter for this session."""
        adapter = self._adapters.get(ref.agent)
        if adapter is None:
            raise ValueError(
                f"No adapter registered for agent '{ref.agent}'. "
                f"Available: {list(self._adapters)}"
            )
        await adapter.send_input(ref, text)

    def active_for_conversation(self, conversation_id: int) -> TerminalSessionRef | None:
        """Return the latest attached session for a conversation, or None.

        Only returns sessions with status "attached". Sessions marked as
        "detached" or "orphaned" are skipped.
        """
        sessions = self._sessions.get(conversation_id, [])
        for s in reversed(sessions):
            if s.status == "attached":
                return s
        return None
