"""Restart recovery and reattach semantics for dual-surface sessions.

On daemon restart the runtime event log is the single source of truth.
Recovery replays events, checks terminal-session process liveness, and
appends orphan events for sessions whose processes are gone.

Product mode stays usable regardless of terminal-session state.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    RuntimeEvent,
    Visibility,
    now_iso,
    EventType,
)
from wlcodex.runtime_state import SurfaceStateSnapshot, replay_surface_events

# Terminal session states that are final — no recheck needed.
_TERMINAL_SESSION_FINAL = frozenset({"aborted", "orphaned", "detached"})


class ProcessChecker(Protocol):
    """Pluggable process-liveness check so tests don't need real pids."""

    def is_process_alive(self, external_session_id: str) -> bool: ...


class RecoveryManager:
    """Replay event log on restart and recover surface sessions.

    Does NOT require SQLite.  Consumes a list of RuntimeEvents and produces
    a SurfaceStateSnapshot plus any new recovery events (orphan markers,
    bookend events).
    """

    def __init__(self, process_checker: ProcessChecker | None = None):
        self._process_checker = process_checker

    def recover(
        self, events: list[RuntimeEvent],
    ) -> tuple[SurfaceStateSnapshot, list[RuntimeEvent]]:
        """Replay *events* and produce (state, new_recovery_events).

        The caller is responsible for appending *new_recovery_events* to the
        runtime event log and any projection stores.
        """
        state = replay_surface_events(events)
        new_events: list[RuntimeEvent] = []
        run_id = f"recovery:{uuid4().hex[:12]}"

        # Bookend: recovery started
        new_events.append(_make_system_event(EventType.SYSTEM_RECOVERY_STARTED, run_id))

        # Check each chat's terminal sessions for orphaned processes
        last_id = max((e.id for e in events), default=0)
        next_id = last_id + 1

        for chat_id, view in state.by_chat.items():
            for agent, session in list(view.terminal_sessions.items()):
                if session.status in _TERMINAL_SESSION_FINAL:
                    continue  # already final, no recheck

                if session.status == "attached":
                    alive = self._check_process(session.external_session_id)
                    if not alive:
                        orphan_event = _make_orphan_event(
                            next_id=next_id,
                            run_id=run_id,
                            chat_id=chat_id,
                            conversation_id=session.conversation_id,
                            agent=agent,
                            external_session_id=session.external_session_id,
                        )
                        new_events.append(orphan_event)
                        next_id += 1

        # Bookend: recovery completed
        new_events.append(_make_system_event(
            EventType.SYSTEM_RECOVERY_COMPLETED, run_id,
        ))

        return state, new_events

    def _check_process(self, external_session_id: str) -> bool:
        if self._process_checker is None:
            return False
        return self._process_checker.is_process_alive(external_session_id)


def _make_system_event(event_type: str, run_id: str) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.SYSTEM,
        aggregate_id=run_id,
        correlation_id=run_id,
        source=EventSource.SYSTEM,
        actor="system",
        visibility=Visibility.INTERNAL,
        payload={},
        occurred_at=now_iso(),
    )


def _make_orphan_event(
    *,
    next_id: int,
    run_id: str,
    chat_id: int,
    conversation_id: int | None,
    agent: str,
    external_session_id: str,
) -> RuntimeEvent:
    event = RuntimeEvent(
        schema_version=1,
        event_type=EventType.TERMINAL_SESSION_DETACHED,
        aggregate_type=AggregateType.SURFACE_SESSION,
        aggregate_id=f"terminal:{chat_id}:{agent}",
        correlation_id=run_id,
        source=EventSource.SYSTEM,
        actor="system",
        visibility=Visibility.OPERATOR,
        payload={
            "chat_id": chat_id,
            "conversation_id": conversation_id,
            "agent": agent,
            "external_session_id": external_session_id,
            "status": "orphaned",
            "reason": "process_not_found_on_restart",
        },
        occurred_at=now_iso(),
        conversation_id=conversation_id,
    )
    return event.with_id(next_id)
