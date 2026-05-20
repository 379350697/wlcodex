"""Terminal session manager — tracks session refs and delegates to adapters.

The manager tracks which external sessions are attached for each conversation.
It does not know Telegram, subprocess creation, or rendering — it only manages
TerminalSessionRef objects, delegates input to agent-specific adapters, and
keeps an in-memory frame history for tail support.

Onsite semantics:
- open_for_conversation returns either a live session or a start-card decision.
  A "no session" state is never a dead end.
- leave_view and pause_delivery stop phone push without killing local work.
- record_frame + tail provide recent-output inspection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef

logger = logging.getLogger(__name__)


class OnsiteDecisionKind(Enum):
    AUTO_OPEN = "auto_open"
    START_CARD = "start_card"


@dataclass(frozen=True)
class OnsiteDecision:
    """Result of open_for_conversation: either a live session or a start-card.

    When kind is AUTO_OPEN, session_ref holds the attached session.
    When kind is START_CARD, available_agents and return_action guide the
    user to the next step — never a dead end.
    """

    kind: OnsiteDecisionKind
    session_ref: TerminalSessionRef | None = None
    available_agents: tuple[str, ...] = ()
    return_action: str = "回驾驶舱"


class TerminalSessionManager:
    """Tracks terminal session refs, delegates input to adapters, and keeps
    an in-memory frame history for tail support (V1)."""

    def __init__(self, adapters: dict[str, object] | None = None):
        self._adapters: dict[str, object] = dict(adapters or {})
        self._sessions: dict[int, list[TerminalSessionRef]] = {}
        # In-memory frame history: external_session_id -> list[TerminalFrame]
        self._frames: dict[str, list[TerminalFrame]] = {}
        # Paused delivery set: external_session_id -> True
        self._paused: set[str] = set()

    # ── session lifecycle ────────────────────────────────────────────────

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
        self._frames.setdefault(external_session_id, [])
        logger.info(
            "Terminal session attached: conv=%d agent=%s id=%s",
            conversation_id,
            agent,
            external_session_id,
        )
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
        ses = self._sessions.get(ref.conversation_id, [])
        for i, s in enumerate(ses):
            if (
                s.external_session_id == ref.external_session_id
                and s.agent == ref.agent
            ):
                ses[i] = detached
                break
        logger.info(
            "Terminal session detached: conv=%d agent=%s id=%s",
            ref.conversation_id,
            ref.agent,
            ref.external_session_id,
        )
        return detached

    async def send_input(self, ref: TerminalSessionRef, text: str):
        """Send user input text to the agent adapter for this session."""
        adapter = self._adapters.get(ref.agent)
        if adapter is None:
            raise ValueError(
                f"No adapter registered for agent '{ref.agent}'. "
                f"Available: {list(self._adapters)}"
            )
        return await adapter.send_input(ref, text)

    def active_for_conversation(
        self, conversation_id: int
    ) -> TerminalSessionRef | None:
        """Return the latest attached session for a conversation, or None."""
        sessions = self._sessions.get(conversation_id, [])
        for s in reversed(sessions):
            if s.status == "attached":
                return s
        return None

    # ── Onsite open decision (Onsite) ─────────────────────────────────────

    def open_for_conversation(
        self, conversation_id: int, preferred_agent: str = ""
    ) -> OnsiteDecision:
        """Try to open Onsite for *conversation_id*.

        * If an attached session exists (preferred agent first), return
          AUTO_OPEN with that session ref.
        * Otherwise return START_CARD with available agents so the user
          always has a next action.
        """
        sessions = self._sessions.get(conversation_id, [])

        # Collect attached sessions only.
        attached: list[TerminalSessionRef] = [
            s for s in sessions if s.status == "attached"
        ]
        if not attached:
            agents = tuple(self._adapters.keys()) if self._adapters else ("claude", "codex")
            return OnsiteDecision(
                kind=OnsiteDecisionKind.START_CARD,
                available_agents=agents,
                return_action="回驾驶舱",
            )

        # Prefer the requested agent.
        if preferred_agent:
            for s in attached:
                if s.agent == preferred_agent:
                    return OnsiteDecision(
                        kind=OnsiteDecisionKind.AUTO_OPEN, session_ref=s
                    )
            # Preferred agent not found → fall back to any attached.
            return OnsiteDecision(
                kind=OnsiteDecisionKind.AUTO_OPEN,
                session_ref=attached[-1],
            )

        # No preference → latest attached.
        return OnsiteDecision(
            kind=OnsiteDecisionKind.AUTO_OPEN,
            session_ref=attached[-1],
        )

    # ── frame recording + tail (Onsite) ───────────────────────────────────

    def record_frame(
        self, ref: TerminalSessionRef, frame: TerminalFrame
    ) -> None:
        """Store a raw frame in the in-memory history for this session.

        Raises KeyError if *ref* was not created by :meth:`attach`.
        """
        self._frames[ref.external_session_id].append(frame)

    def tail(
        self, ref: TerminalSessionRef, limit: int = 20
    ) -> list[TerminalFrame]:
        """Return the most recent *limit* frames, newest last."""
        all_frames = self._frames.get(ref.external_session_id, [])
        return all_frames[-limit:] if limit > 0 else []

    # ── delivery control — pause/resume/leave without killing (Onsite) ────

    def pause_delivery(self, ref: TerminalSessionRef) -> None:
        """Stop phone push for this session. The session stays attached."""
        self._paused.add(ref.external_session_id)
        logger.info(
            "Delivery paused for session %s (conv=%d)",
            ref.external_session_id,
            ref.conversation_id,
        )

    def resume_delivery(self, ref: TerminalSessionRef) -> None:
        """Re-enable phone push for this session."""
        self._paused.discard(ref.external_session_id)

    def is_delivery_paused(self, ref: TerminalSessionRef) -> bool:
        """Check whether delivery is currently paused."""
        return ref.external_session_id in self._paused

    def leave_view(self, ref: TerminalSessionRef) -> None:
        """Leave Onsite view without killing the local session.

        Pauses delivery (phone push stops) but the session remains
        attached so the user can re-open it later.  Unlike :meth:`detach`
        this is a pure side-effect — the session is still findable via
        :meth:`active_for_conversation` and :meth:`open_for_conversation`.
        """
        self.pause_delivery(ref)
        logger.info(
            "Left Onsite view: conv=%d agent=%s id=%s (session still attached)",
            ref.conversation_id,
            ref.agent,
            ref.external_session_id,
        )
