"""Durable HTTP mutation and Relay archive records.

HTTP handlers deliberately keep this small adapter at the boundary instead of
re-implementing SQLite compare-and-set logic in each route.  It is also useful
to non-HTTP callers that need the exact same retry contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any


_MAX_IDEMPOTENCY_KEY_LENGTH = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_idempotency_key(value: object) -> str:
    """Return a safe client key or an empty string when one was not supplied."""

    key = str(value or "").strip()
    if not key:
        return ""
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError("Idempotency-Key is too long")
    if any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("Idempotency-Key contains unsupported characters")
    return key


@dataclass(frozen=True)
class RelayMutationClaim:
    """The one of four possible outcomes of acquiring a mutation key."""

    key: str
    status: str
    operation: str
    task_id: int | None
    response_status: int | None = None
    response_payload: dict[str, Any] | None = None
    error: str = ""

    @property
    def should_execute(self) -> bool:
        return self.status == "claimed"

    @property
    def is_replay(self) -> bool:
        return self.status == "completed"


class MutationStore:
    """Compare-and-set persistence for HTTP mutations and Relay archives.

    The backing table retains its historical ``relay_`` name because it was
    introduced with Relay.  ``task_id`` is nullable, however, so Native
    session controls can use the exact same durable retry contract without
    pretending that a provider session is a Relay task.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> "MutationStore":
        return cls(connection)

    @classmethod
    def from_relay_service(cls, relay_service: Any) -> "MutationStore":
        return cls(relay_service._store._ledger._conn)

    def claim(
        self,
        *,
        key: object,
        operation: str,
        task_id: int | None,
        payload: Any,
    ) -> RelayMutationClaim | None:
        """Claim a request or return its durable replay/conflict state.

        Empty keys intentionally keep the old API compatibility path.  New UI
        mutations always send a key; legacy Telegram and integrations can be
        migrated without making an unrelated request fail at this boundary.
        """

        normalized_key = normalize_idempotency_key(key)
        if not normalized_key:
            return None
        normalized_operation = str(operation or "").strip()
        if not normalized_operation:
            raise ValueError("mutation operation is required")
        normalized_task_id = int(task_id) if task_id is not None else None
        fingerprint = _json_fingerprint(payload)
        started_transaction = not self._conn.in_transaction
        if started_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM relay_mutation_idempotency WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if row is None:
                now = _now()
                self._conn.execute(
                    """
                    INSERT INTO relay_mutation_idempotency (
                        idempotency_key, operation, task_id, request_fingerprint,
                        status, response_status, response_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'in_progress', NULL, '', ?, ?)
                    """,
                    (
                        normalized_key,
                        normalized_operation,
                        normalized_task_id,
                        fingerprint,
                        now,
                        now,
                    ),
                )
                claim = RelayMutationClaim(
                    key=normalized_key,
                    status="claimed",
                    operation=normalized_operation,
                    task_id=normalized_task_id,
                )
            elif (
                str(row["operation"] or "") != normalized_operation
                or row["task_id"] != normalized_task_id
                or str(row["request_fingerprint"] or "") != fingerprint
            ):
                claim = RelayMutationClaim(
                    key=normalized_key,
                    status="conflict",
                    operation=normalized_operation,
                    task_id=normalized_task_id,
                    error="Idempotency-Key cannot be reused for a different mutation",
                )
            elif str(row["status"] or "") == "completed":
                claim = RelayMutationClaim(
                    key=normalized_key,
                    status="completed",
                    operation=normalized_operation,
                    task_id=(int(row["task_id"]) if row["task_id"] is not None else None),
                    response_status=int(row["response_status"] or 200),
                    response_payload=_decode_payload(str(row["response_json"] or "")),
                )
            else:
                claim = RelayMutationClaim(
                    key=normalized_key,
                    status="in_progress",
                    operation=normalized_operation,
                    task_id=(int(row["task_id"]) if row["task_id"] is not None else None),
                    error="Mutation is still being processed; retry this same key shortly.",
                )
            if started_transaction:
                self._conn.commit()
            return claim
        except Exception:
            if started_transaction:
                self._conn.rollback()
            raise

    def bind_task(self, key: str, task_id: int) -> None:
        self._conn.execute(
            """
            UPDATE relay_mutation_idempotency
            SET task_id = ?, updated_at = ?
            WHERE idempotency_key = ? AND status = 'in_progress'
            """,
            (int(task_id), _now(), key),
        )
        self._conn.commit()

    def complete(self, key: str, *, status: int, payload: dict[str, Any]) -> None:
        self._conn.execute(
            """
            UPDATE relay_mutation_idempotency
            SET status = 'completed', response_status = ?, response_json = ?, updated_at = ?
            WHERE idempotency_key = ?
            """,
            (
                int(status),
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                _now(),
                key,
            ),
        )
        self._conn.commit()

    def abandon(self, key: str) -> None:
        """Release a claim when validation failed before a side effect started."""

        self._conn.execute(
            "DELETE FROM relay_mutation_idempotency WHERE idempotency_key = ? AND status = 'in_progress'",
            (key,),
        )
        self._conn.commit()

    def archive_task(
        self,
        task_id: int,
        *,
        archived_by: str = "user",
        reason: str = "",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO relay_task_archives (team_run_id, archived_at, archived_by, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_run_id) DO UPDATE SET
                archived_at = excluded.archived_at,
                archived_by = excluded.archived_by,
                reason = excluded.reason
            """,
            (int(task_id), _now(), str(archived_by or "user"), str(reason or "")),
        )
        self._conn.commit()

    def restore_task(self, task_id: int) -> None:
        self._conn.execute(
            "DELETE FROM relay_task_archives WHERE team_run_id = ?",
            (int(task_id),),
        )
        self._conn.commit()

    def is_task_archived(self, task_id: int) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM relay_task_archives WHERE team_run_id = ?",
                (int(task_id),),
            ).fetchone()
            is not None
        )


# Kept as a source-compatible name for the Relay-specific call sites and
# integrations.  New Native and shared HTTP paths should use ``MutationStore``.
RelayMutationStore = MutationStore


def _decode_payload(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
