"""Append-only runtime event store.

Provides append/query APIs so that no other lane writes raw SQL for
runtime events.  Redaction and payload length caps are applied at append
time before data reaches SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from collections.abc import Callable

from wlcodex.runtime_events import (
    MAX_PAYLOAD_STRING_LENGTH,
    RuntimeEvent,
    redact_payload,
)

logger = logging.getLogger(__name__)


class RuntimeEventStore:
    """Append-only store for ``runtime_events``.

    The table is created by ``Ledger.migrate()`` in ``wlcodex.db``.
    This store only inserts and queries.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._projectors: list[Callable[[RuntimeEvent], None]] = []

    def add_projector(self, projector: Callable[[RuntimeEvent], None]) -> None:
        """Register a post-append projector callback.

        Projectors are called after the event commit with the persisted event
        id.  Their failures are isolated so append-only storage remains the
        source of truth even when a compatibility projection has a bug.
        """
        self._projectors.append(projector)

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        """Redact, validate, and persist *event*.  Returns a new
        ``RuntimeEvent`` with the SQLite-assigned id.

        Historical events are never mutated by this method.
        """
        safe_payload = redact_payload(
            event.payload, max_str_len=MAX_PAYLOAD_STRING_LENGTH
        )

        cur = self._conn.execute(
            """
            INSERT INTO runtime_events (
                schema_version, event_type, aggregate_type, aggregate_id,
                conversation_id, orchestration_run_id, agent_run_id, task_id,
                correlation_id, causation_id, source, actor, visibility,
                payload_json, occurred_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                event.schema_version,
                event.event_type,
                event.aggregate_type,
                event.aggregate_id,
                event.conversation_id,
                event.orchestration_run_id,
                event.agent_run_id,
                event.task_id,
                event.correlation_id,
                event.causation_id,
                event.source,
                event.actor,
                event.visibility,
                json.dumps(safe_payload, ensure_ascii=False),
                event.occurred_at,
            ),
        )
        event_id = int(cur.lastrowid)
        self._conn.commit()
        stored = replace(event, payload=safe_payload, id=event_id)
        self._notify_projectors(stored)
        return stored

    def _notify_projectors(self, event: RuntimeEvent) -> None:
        for projector in list(self._projectors):
            try:
                projector(event)
            except Exception:
                logger.debug("Runtime projector callback failed", exc_info=True)

    # ------------------------------------------------------------------
    # Single lookup
    # ------------------------------------------------------------------

    def get_by_id(self, event_id: int) -> RuntimeEvent:
        """Return a single event by its primary key."""
        row = self._conn.execute(
            "SELECT * FROM runtime_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown runtime event id: {event_id}")
        return _row_to_event(row)

    # ------------------------------------------------------------------
    # List / query
    # ------------------------------------------------------------------

    def list_by_correlation(
        self, correlation_id: str, *, limit: int = 200
    ) -> list[RuntimeEvent]:
        """Events for a correlation, ordered by id (insertion order)."""
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE correlation_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (correlation_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_by_agent_run(
        self, agent_run_id: int, *, limit: int = 200
    ) -> list[RuntimeEvent]:
        """Events for a specific agent run."""
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE agent_run_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (agent_run_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_by_agent_run_after(
        self,
        agent_run_id: int,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[RuntimeEvent]:
        """Events for a specific agent run after a runtime event id.

        Ordered by id ascending so clients can use the last event id as a
        reconnect cursor.
        """
        if limit <= 0:
            return []
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE agent_run_id = ?
              AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (agent_run_id, after_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_by_agent_run_tail(
        self,
        agent_run_id: int,
        *,
        limit: int = 100,
    ) -> list[RuntimeEvent]:
        """Most recent events for an agent run, ordered oldest first."""
        if limit <= 0:
            return []
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE agent_run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (agent_run_id, limit),
        ).fetchall()
        events = [_row_to_event(r) for r in rows]
        events.reverse()
        return events

    def list_by_agent_run_before(
        self,
        agent_run_id: int,
        *,
        before_id: int,
        limit: int = 100,
    ) -> list[RuntimeEvent]:
        """Events immediately before an event id, ordered oldest first."""
        if before_id <= 0 or limit <= 0:
            return []
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE agent_run_id = ?
              AND id < ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (agent_run_id, before_id, limit),
        ).fetchall()
        events = [_row_to_event(r) for r in rows]
        events.reverse()
        return events

    def count_by_agent_run_before(
        self,
        agent_run_id: int,
        *,
        before_id: int,
    ) -> int:
        """Count events for an agent run before an event id."""
        if before_id <= 0:
            return 0
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM runtime_events
            WHERE agent_run_id = ?
              AND id < ?
            """,
            (agent_run_id, before_id),
        ).fetchone()
        if row is None:
            return 0
        return int(row["count"])

    def list_by_conversation(
        self, conversation_id: int, *, limit: int = 200
    ) -> list[RuntimeEvent]:
        """Events for a conversation, ordered by id."""
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE conversation_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_by_orchestration_run(
        self, orchestration_run_id: int, *, limit: int = 200
    ) -> list[RuntimeEvent]:
        """Events for an orchestration run."""
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE orchestration_run_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (orchestration_run_id, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_recent_for_conversation(
        self, conversation_id: int, *, limit: int = 50
    ) -> list[RuntimeEvent]:
        """Most recent events for a conversation (descending id order)."""
        rows = self._conn.execute(
            """
            SELECT * FROM runtime_events
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
        events = [_row_to_event(r) for r in rows]
        events.reverse()
        return events

    def get_conversation_runtime_state(self, conversation_id: int) -> str | None:
        """Return the current conversation state derived from runtime events.

        Looks at the latest state-affecting events (phase changes,
        completions, state changes) for this conversation.  Returns
        ``None`` when no conversation-starting event has been recorded.
        """
        conversation_state_events = (
            "conversation.started",
            "conversation.state.changed",
            "conversation.closed",
            "run.phase.changed",
            "run.completed",
            "run.failed",
            "run.cancelled",
            "verification.decision.recorded",
        )
        placeholders = ",".join("?" for _ in conversation_state_events)
        rows = self._conn.execute(
            f"""
            SELECT event_type, payload_json, id
            FROM runtime_events
            WHERE conversation_id = ?
              AND event_type IN ({placeholders})
            ORDER BY id ASC
            """,
            (conversation_id, *conversation_state_events),
        ).fetchall()

        # Replay state from events (pure reducer).
        from wlcodex.runtime_state import _ORCH_PHASE_TO_CONVERSATION_STATE
        state: str | None = None
        has_pass = False

        for row in rows:
            etype = str(row["event_type"])
            payload = json.loads(row["payload_json"])

            if etype == "conversation.started":
                state = "new"
            elif etype == "conversation.state.changed":
                new_state = payload.get("to", "")
                if new_state:
                    state = new_state
                phase = payload.get("phase", "")
                mapped = _ORCH_PHASE_TO_CONVERSATION_STATE.get(phase)
                if mapped:
                    state = mapped
            elif etype == "run.phase.changed":
                phase = payload.get("phase", "")
                mapped = _ORCH_PHASE_TO_CONVERSATION_STATE.get(phase)
                if mapped:
                    state = mapped
            elif etype == "run.completed":
                state = "passed" if has_pass else "failed"
            elif etype == "run.failed":
                state = "failed"
            elif etype == "run.cancelled":
                state = "aborted"
            elif etype == "verification.decision.recorded":
                if payload.get("decision") == "pass":
                    has_pass = True

        return state


# ------------------------------------------------------------------
# Row mapper
# ------------------------------------------------------------------

def _row_to_event(row: sqlite3.Row) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=int(row["schema_version"]),
        event_type=str(row["event_type"]),
        aggregate_type=str(row["aggregate_type"]),
        aggregate_id=str(row["aggregate_id"]),
        conversation_id=row["conversation_id"],
        orchestration_run_id=row["orchestration_run_id"],
        agent_run_id=row["agent_run_id"],
        task_id=row["task_id"],
        correlation_id=str(row["correlation_id"]),
        causation_id=row["causation_id"],
        source=str(row["source"]),
        actor=str(row["actor"]),
        visibility=str(row["visibility"]),
        payload=json.loads(str(row["payload_json"])),
        occurred_at=str(row["occurred_at"]),
        id=int(row["id"]),
    )
