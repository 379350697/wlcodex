"""Runtime event store.

Provides append/query APIs so that no other lane writes raw SQL for
runtime events.  Redaction and payload length caps are applied at append
time before data reaches SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    MAX_PAYLOAD_STRING_LENGTH,
    RuntimeEvent,
    Visibility,
    redact_payload,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderRawFrame:
    id: int
    provider: str
    provider_engine: str
    native_session_id: str
    native_turn_id: str
    sequence: int
    raw_kind: str
    raw_payload: dict[str, Any]
    occurred_at: str
    conversation_id: int | None = None
    orchestration_run_id: int | None = None
    agent_run_id: int | None = None
    task_id: int | None = None


@dataclass(frozen=True)
class QueuedRunClaim:
    """A durable lease over one legacy ``run.queued`` event."""

    queued_event: RuntimeEvent
    claim_event: RuntimeEvent
    workspace_alias: str
    lease_owner: str


class RuntimeEventStore:
    """Store for ``runtime_events``.

    The table is created by ``Ledger.migrate()`` in ``wlcodex.db``.
    This store owns all raw SQL for runtime events.  Runtime events are
    appended by default; ``correct_payload_item_turn_id`` is a narrow
    compatibility repair for Codex JSONL transcript items that were mirrored
    before their official turn id was known.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        raw_frame_archive_dir: Path | None = None,
    ) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._projectors: list[Callable[[RuntimeEvent], None]] = []
        if raw_frame_archive_dir is None:
            # Import lazily so the retention CLI can depend on this store
            # without a module-import cycle.
            from wlcodex.runtime_raw_frame_retention import default_archive_dir

            raw_frame_archive_dir = default_archive_dir(conn)
        self._raw_frame_archive_dir = raw_frame_archive_dir

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

    # ------------------------------------------------------------------
    # Workspace-scoped queued-run leases
    # ------------------------------------------------------------------

    def claim_next_queued_run_for_workspace(
        self,
        workspace_alias: str,
        *,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> QueuedRunClaim | None:
        """Claim the oldest runnable queued event for one workspace.

        A queue event is immutable, so claim/release/consume are companion
        events linked by ``causation_id``.  The selection and claim insertion
        share one ``BEGIN IMMEDIATE`` transaction: two free workers cannot
        consume a task merely because it was earlier in another workspace's
        global queue.  Expired claims are deliberately retriable.
        """

        workspace = str(workspace_alias or "").strip()
        owner = str(lease_owner or "").strip()
        if not workspace or not owner:
            return None
        seconds = max(1, int(lease_seconds or 300))
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=seconds)).isoformat()
        stored: RuntimeEvent | None = None
        queued: RuntimeEvent | None = None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """
                SELECT q.*
                FROM runtime_events q
                JOIN conversation_sessions c ON c.id = q.conversation_id
                WHERE q.event_type = ?
                  AND c.workspace_alias = ?
                ORDER BY q.id ASC
                """,
                (EventType.RUN_QUEUED, workspace),
            ).fetchall()
            for row in rows:
                candidate = _row_to_event(row)
                if self._queued_run_is_consumed_locked(candidate):
                    continue
                latest = self._latest_queued_run_lease_event_locked(candidate.id)
                if latest is not None and latest.event_type == EventType.RUN_QUEUED_CLAIMED:
                    lease_expires_at = _queued_lease_expiry(latest.payload)
                    if lease_expires_at is None or lease_expires_at > now:
                        continue
                queued = candidate
                stored = self._append_locked(
                    RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.RUN_QUEUED_CLAIMED,
                        aggregate_type=AggregateType.ORCHESTRATION_RUN,
                        aggregate_id=f"queued-{candidate.id}",
                        correlation_id=candidate.correlation_id,
                        source=EventSource.CONTROLLER,
                        actor="controller",
                        visibility=Visibility.OPERATOR,
                        payload={
                            "queued_event_id": candidate.id,
                            "workspace_alias": workspace,
                            "lease_owner": owner,
                            "lease_expires_at": expires_at,
                        },
                        occurred_at=now_text,
                        conversation_id=candidate.conversation_id,
                        causation_id=candidate.id,
                    )
                )
                break
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if stored is None or queued is None:
            return None
        self._notify_projectors(stored)
        return QueuedRunClaim(
            queued_event=queued,
            claim_event=stored,
            workspace_alias=workspace,
            lease_owner=owner,
        )

    def release_queued_run_claim(
        self,
        claim: QueuedRunClaim,
        *,
        error: str,
    ) -> RuntimeEvent | None:
        """Release an owned claim after launch failed, allowing a retry."""

        queued = claim.queued_event
        stored: RuntimeEvent | None = None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            latest = self._latest_queued_run_lease_event_locked(queued.id)
            if (
                latest is None
                or latest.event_type != EventType.RUN_QUEUED_CLAIMED
                or str(latest.payload.get("lease_owner") or "") != claim.lease_owner
            ):
                self._conn.commit()
                return None
            stored = self._append_locked(
                RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.RUN_QUEUED_RELEASED,
                    aggregate_type=AggregateType.ORCHESTRATION_RUN,
                    aggregate_id=f"queued-{queued.id}",
                    correlation_id=queued.correlation_id,
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "queued_event_id": queued.id,
                        "workspace_alias": claim.workspace_alias,
                        "lease_owner": claim.lease_owner,
                        "error": str(error or "queued launch failed")[:500],
                    },
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    conversation_id=queued.conversation_id,
                    causation_id=queued.id,
                )
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        self._notify_projectors(stored)
        return stored

    def consume_queued_run_claim(
        self,
        claim: QueuedRunClaim,
        *,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent | None:
        """Mark a successfully launched queue event consumed exactly once."""

        queued = claim.queued_event
        stored: RuntimeEvent | None = None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # ``run.started`` is a durable hand-off marker and is therefore
            # enough for a *new* worker to skip an expired claim after a
            # crash.  The owner that just recorded that marker still needs to
            # append the explicit consumed audit event, though.  Checking the
            # consumed marker itself here preserves that audit without making
            # the normal start -> consume path look like a bookkeeping race.
            already_consumed = self._conn.execute(
                """
                SELECT 1
                FROM runtime_events
                WHERE causation_id = ?
                  AND event_type = ?
                LIMIT 1
                """,
                (queued.id, EventType.RUN_QUEUED_CONSUMED),
            ).fetchone()
            if already_consumed is not None:
                self._conn.commit()
                return None
            latest = self._latest_queued_run_lease_event_locked(queued.id)
            if (
                latest is None
                or latest.event_type != EventType.RUN_QUEUED_CLAIMED
                or str(latest.payload.get("lease_owner") or "") != claim.lease_owner
            ):
                self._conn.commit()
                return None
            event_payload = {
                "queued_event_id": queued.id,
                "workspace_alias": claim.workspace_alias,
                "lease_owner": claim.lease_owner,
            }
            if payload:
                event_payload.update(payload)
            stored = self._append_locked(
                RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.RUN_QUEUED_CONSUMED,
                    aggregate_type=AggregateType.ORCHESTRATION_RUN,
                    aggregate_id=f"queued-{queued.id}",
                    correlation_id=queued.correlation_id,
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload=event_payload,
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    conversation_id=queued.conversation_id,
                    causation_id=queued.id,
                )
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        self._notify_projectors(stored)
        return stored

    def mark_unconsumed_legacy_queued_runs_needing_recovery(
        self,
        *,
        reason: str,
        blocking_reason: str,
        next_action: str,
    ) -> list[RuntimeEvent]:
        """Atomically stop legacy queues that this process cannot execute.

        Formal Native/Relay web-only mode has no Telegram controller or its
        execution backend.  Selection and the causal recovery marker share a
        ``BEGIN IMMEDIATE`` transaction so overlapping startup processes
        cannot each write a duplicate recovery event, or leave a selected
        queue runnable after the marker is committed.  An orphaned legacy
        queue is marked too: it has no workspace claim path and must not stay
        silently invisible merely because its historical conversation row is
        missing.  Recovery is a durable non-dispatch outcome; it is
        deliberately not a synthetic claim.
        """

        stored: list[RuntimeEvent] = []
        timestamp = datetime.now(timezone.utc).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                """
                SELECT q.*, c.workspace_alias, c.id AS matched_conversation_id
                FROM runtime_events AS q
                LEFT JOIN conversation_sessions AS c ON c.id = q.conversation_id
                WHERE q.event_type = ?
                  AND (c.legacy_compatible = 1 OR c.id IS NULL)
                ORDER BY q.id ASC
                """,
                (EventType.RUN_QUEUED,),
            ).fetchall()
            for row in rows:
                queued = _row_to_event(row)
                if self._queued_run_is_consumed_locked(queued):
                    continue
                conversation_missing = row["matched_conversation_id"] is None
                stored.append(self._append_locked(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.RUN_RECOVERY_REQUIRED,
                    aggregate_type=AggregateType.ORCHESTRATION_RUN,
                    aggregate_id=f"legacy-queued-{queued.id}",
                    correlation_id=(
                        queued.correlation_id
                        or f"legacy-queue-recovery-{queued.id}"
                    ),
                    source=EventSource.SYSTEM,
                    actor="system",
                    visibility=Visibility.USER,
                    payload={
                        "state": "needs_recovery",
                        "recovery_state": "needs_recovery",
                        "reason": str(reason),
                        "blocking_reason": str(blocking_reason),
                        "next_action": str(next_action),
                        "queue_kind": (
                            "legacy_telegram_orphaned"
                            if conversation_missing else "legacy_telegram"
                        ),
                        "queued_event_id": queued.id,
                        "workspace_alias": str(row["workspace_alias"] or ""),
                        "conversation_missing": conversation_missing,
                    },
                    occurred_at=timestamp,
                    conversation_id=queued.conversation_id,
                    causation_id=queued.id,
                )))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        for event in stored:
            self._notify_projectors(event)
        return stored

    def _append_locked(self, event: RuntimeEvent) -> RuntimeEvent:
        """Append while the caller owns the current SQLite transaction."""

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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        return replace(event, payload=safe_payload, id=int(cur.lastrowid))

    def _queued_run_is_consumed_locked(self, queued: RuntimeEvent) -> bool:
        exact = self._conn.execute(
            """
            SELECT 1
            FROM runtime_events
            WHERE causation_id = ?
              AND event_type IN (?, ?, ?)
            LIMIT 1
            """,
            (
                queued.id,
                EventType.RUN_QUEUED_CONSUMED,
                EventType.RUN_STARTED,
                EventType.RUN_RECOVERY_REQUIRED,
            ),
        ).fetchone()
        if exact is not None:
            return True
        # Historical queue markers predate ``causation_id``.  Keep their
        # existing single-conversation semantics during the rolling upgrade,
        # but all new claims use the precise event id above.
        legacy = self._conn.execute(
            """
            SELECT 1
            FROM runtime_events
            WHERE conversation_id = ?
              AND id > ?
              AND causation_id IS NULL
              AND event_type IN (?, ?)
            LIMIT 1
            """,
            (
                queued.conversation_id,
                queued.id,
                EventType.RUN_QUEUED_CONSUMED,
                EventType.RUN_STARTED,
            ),
        ).fetchone()
        return legacy is not None

    def _latest_queued_run_lease_event_locked(
        self,
        queued_event_id: int,
    ) -> RuntimeEvent | None:
        row = self._conn.execute(
            """
            SELECT *
            FROM runtime_events
            WHERE causation_id = ?
              AND event_type IN (?, ?, ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                int(queued_event_id),
                EventType.RUN_QUEUED_CLAIMED,
                EventType.RUN_QUEUED_RELEASED,
                EventType.RUN_QUEUED_CONSUMED,
            ),
        ).fetchone()
        return _row_to_event(row) if row is not None else None

    def next_provider_raw_frame_sequence(
        self,
        *,
        provider: str,
        provider_engine: str,
        native_session_id: str,
        native_turn_id: str,
    ) -> int:
        cursor_sequence = 0
        try:
            cursor_row = self._conn.execute(
                """
                SELECT last_sequence
                FROM provider_raw_frame_sequence_cursors
                WHERE provider = ?
                  AND provider_engine = ?
                  AND native_session_id = ?
                  AND native_turn_id = ?
                """,
                (provider, provider_engine, native_session_id, native_turn_id),
            ).fetchone()
            if cursor_row is not None:
                cursor_sequence = int(cursor_row["last_sequence"])
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise

        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS max_sequence
            FROM provider_raw_frames
            WHERE provider = ?
              AND provider_engine = ?
              AND native_session_id = ?
              AND native_turn_id = ?
            """,
            (provider, provider_engine, native_session_id, native_turn_id),
        ).fetchone()
        hot_sequence = int(row["max_sequence"]) if row is not None else 0
        archive_sequence = 0
        try:
            archive_row = self._conn.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) AS max_sequence
                FROM provider_raw_frame_archive_index
                WHERE provider = ?
                  AND provider_engine = ?
                  AND native_session_id = ?
                  AND native_turn_id = ?
                """,
                (provider, provider_engine, native_session_id, native_turn_id),
            ).fetchone()
            if archive_row is not None:
                archive_sequence = int(archive_row["max_sequence"])
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
        return max(cursor_sequence, hot_sequence, archive_sequence) + 1

    def append_provider_raw_frame(
        self,
        *,
        provider: str,
        provider_engine: str,
        native_session_id: str,
        native_turn_id: str,
        sequence: int,
        raw_kind: str,
        raw_payload: dict[str, Any],
        occurred_at: str,
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
        agent_run_id: int | None = None,
        task_id: int | None = None,
    ) -> ProviderRawFrame:
        # Raw frames intentionally retain their diagnostic shape and length,
        # but must never bypass the same credential redaction contract as
        # runtime events.  A practically unbounded cap preserves the previous
        # raw-frame behaviour while removing secrets before SQLite writes.
        safe_payload = redact_payload(raw_payload, max_str_len=2**63 - 1)
        cur = self._conn.execute(
            """
            INSERT INTO provider_raw_frames (
                provider, provider_engine, native_session_id, native_turn_id,
                sequence, raw_kind, raw_payload_json, occurred_at,
                conversation_id, orchestration_run_id, agent_run_id, task_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                provider,
                provider_engine,
                native_session_id,
                native_turn_id,
                sequence,
                raw_kind,
                json.dumps(safe_payload, ensure_ascii=False),
                occurred_at,
                conversation_id,
                orchestration_run_id,
                agent_run_id,
                task_id,
            ),
        )
        try:
            self._conn.execute(
                """
                INSERT INTO provider_raw_frame_sequence_cursors (
                    provider, provider_engine, native_session_id, native_turn_id,
                    last_sequence, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_engine, native_session_id, native_turn_id)
                DO UPDATE SET
                    last_sequence = MAX(
                        provider_raw_frame_sequence_cursors.last_sequence,
                        excluded.last_sequence
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    provider_engine,
                    native_session_id,
                    native_turn_id,
                    sequence,
                    occurred_at,
                ),
            )
        except sqlite3.OperationalError as exc:
            # Rolling upgrades may briefly run this code against a database
            # whose Ledger migration has not installed retention tables yet.
            # Preserve existing raw-frame ingestion rather than making it a
            # service outage; the max-sequence fallback remains correct.
            if "no such table" not in str(exc).lower():
                raise
        self._conn.commit()
        return ProviderRawFrame(
            id=int(cur.lastrowid),
            provider=provider,
            provider_engine=provider_engine,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            sequence=sequence,
            raw_kind=raw_kind,
            raw_payload=safe_payload,
            occurred_at=occurred_at,
            conversation_id=conversation_id,
            orchestration_run_id=orchestration_run_id,
            agent_run_id=agent_run_id,
            task_id=task_id,
        )

    def get_provider_raw_frame(self, frame_id: int) -> ProviderRawFrame:
        row = self._conn.execute(
            "SELECT * FROM provider_raw_frames WHERE id = ?", (frame_id,)
        ).fetchone()
        if row is not None:
            return _row_to_provider_raw_frame(row)
        from wlcodex.runtime_raw_frame_retention import read_archived_provider_raw_frame

        archived = read_archived_provider_raw_frame(
            self._conn,
            frame_id,
            archive_dir=self._raw_frame_archive_dir,
        )
        if archived is None:
            raise KeyError(f"unknown provider raw frame id: {frame_id}")
        return _archive_record_to_provider_raw_frame(archived)

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

    def latest_approval_resolution(
        self,
        request_id: str,
        *,
        agent_run_id: int | None = None,
    ) -> RuntimeEvent | None:
        """Return the durable provider-resolution acknowledgement, if any.

        Native provider responses are external side effects, while Relay's
        round projection is SQLite state.  The acknowledgement event is the
        bridge used by the lifecycle worker to finish a projection if the
        process died after the provider accepted a response but before Relay
        committed its final status.
        """
        clean_request_id = str(request_id or "").strip()
        if not clean_request_id:
            return None
        clauses = ["event_type = ?", "aggregate_id = ?"]
        params: list[object] = [EventType.APPROVAL_RESOLVED, clean_request_id]
        if agent_run_id is not None:
            clauses.append("agent_run_id = ?")
            params.append(int(agent_run_id))
        row = self._conn.execute(
            f"""
            SELECT *
            FROM runtime_events
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return _row_to_event(row) if row is not None else None

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

    def has_payload_item_id(self, agent_run_id: int, item_id: str) -> bool:
        """Return whether an agent run already has an event for a payload item."""
        if not item_id:
            return False
        row = self._conn.execute(
            """
            SELECT 1
            FROM runtime_events
            WHERE agent_run_id = ?
              AND (
                json_extract(payload_json, '$.itemId') = ?
                OR json_extract(payload_json, '$.item_id') = ?
              )
            LIMIT 1
            """,
            (agent_run_id, item_id, item_id),
        ).fetchone()
        return row is not None

    def payload_item_ids_by_agent_run(self, agent_run_id: int) -> set[str]:
        """Return all payload item ids already stored for an agent run."""
        return set(self.payload_item_turn_ids_by_agent_run(agent_run_id))

    def payload_item_turn_ids_by_agent_run(
        self,
        agent_run_id: int,
    ) -> dict[str, set[str]]:
        """Return stored payload item ids and their known turn ids."""
        rows = self._conn.execute(
            """
            SELECT payload_json
            FROM runtime_events
            WHERE agent_run_id = ?
            """,
            (agent_run_id,),
        ).fetchall()
        item_turn_ids: dict[str, set[str]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            item_id = ""
            for key in ("itemId", "item_id"):
                value = payload.get(key)
                if value:
                    item_id = str(value)
                    break
            if not item_id:
                continue
            turn_id = str(
                payload.get("turnId") or payload.get("native_turn_id") or ""
            )
            item_turn_ids.setdefault(item_id, set())
            if turn_id:
                item_turn_ids[item_id].add(turn_id)
        return item_turn_ids

    def correct_payload_item_turn_id(
        self,
        agent_run_id: int,
        item_id: str,
        *,
        native_turn_id: str,
        native_thread_id: str = "",
    ) -> int:
        """Correct mirrored Codex transcript turn metadata for an existing item."""
        if not item_id or not native_turn_id:
            return 0
        rows = self._conn.execute(
            """
            SELECT id, payload_json
            FROM runtime_events
            WHERE agent_run_id = ?
              AND (
                json_extract(payload_json, '$.itemId') = ?
                OR json_extract(payload_json, '$.item_id') = ?
              )
            """,
            (agent_run_id, item_id, item_id),
        ).fetchall()
        updated = 0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                continue
            next_payload = dict(payload)
            next_payload["native_turn_id"] = native_turn_id
            next_payload["turnId"] = native_turn_id
            if native_thread_id:
                next_payload["native_thread_id"] = native_thread_id
                next_payload["threadId"] = native_thread_id
            if next_payload == payload:
                continue
            self._conn.execute(
                """
                UPDATE runtime_events
                SET payload_json = ?
                WHERE id = ?
                """,
                (json.dumps(next_payload, ensure_ascii=False), int(row["id"])),
            )
            updated += 1
        if updated:
            self._conn.commit()
        return updated

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
        completions, state changes, recovery requirements) for this
        conversation.  A durable recovery requirement is itself a usable
        state even when the historical conversation predates a
        ``conversation.started`` event.
        """
        conversation_state_events = (
            "conversation.started",
            "conversation.state.changed",
            "conversation.closed",
            "run.phase.changed",
            "run.completed",
            "run.failed",
            "run.recovery.required",
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
            elif etype == "run.recovery.required":
                # Event type is the authoritative contract.  Do not let a
                # malformed historical payload turn a recovery boundary into
                # a state that routes new work automatically.
                state = "needs_recovery"
            elif etype == "run.cancelled":
                state = "aborted"
            elif etype == "verification.decision.recorded":
                if payload.get("decision") == "pass":
                    has_pass = True

        return state


# ------------------------------------------------------------------
# Row mapper
# ------------------------------------------------------------------


def _queued_lease_expiry(payload: dict[str, Any]) -> datetime | None:
    raw = str(payload.get("lease_expires_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

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


def _row_to_provider_raw_frame(row: sqlite3.Row) -> ProviderRawFrame:
    payload = json.loads(str(row["raw_payload_json"]))
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return ProviderRawFrame(
        id=int(row["id"]),
        provider=str(row["provider"]),
        provider_engine=str(row["provider_engine"]),
        native_session_id=str(row["native_session_id"]),
        native_turn_id=str(row["native_turn_id"]),
        sequence=int(row["sequence"]),
        raw_kind=str(row["raw_kind"]),
        raw_payload=payload,
        occurred_at=str(row["occurred_at"]),
        conversation_id=row["conversation_id"],
        orchestration_run_id=row["orchestration_run_id"],
        agent_run_id=row["agent_run_id"],
        task_id=row["task_id"],
    )


def _archive_record_to_provider_raw_frame(record: dict[str, Any]) -> ProviderRawFrame:
    """Map validated archive JSON back to the hot-store lookup contract."""

    raw_payload = record.get("raw_payload")
    if not isinstance(raw_payload, dict):
        raw_payload = {"value": raw_payload}
    return ProviderRawFrame(
        id=int(record["id"]),
        provider=str(record["provider"]),
        provider_engine=str(record["provider_engine"]),
        native_session_id=str(record["native_session_id"]),
        native_turn_id=str(record["native_turn_id"]),
        sequence=int(record["sequence"]),
        raw_kind=str(record["raw_kind"]),
        raw_payload=raw_payload,
        occurred_at=str(record["occurred_at"]),
        conversation_id=_optional_int(record.get("conversation_id")),
        orchestration_run_id=_optional_int(record.get("orchestration_run_id")),
        agent_run_id=_optional_int(record.get("agent_run_id")),
        task_id=_optional_int(record.get("task_id")),
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None
