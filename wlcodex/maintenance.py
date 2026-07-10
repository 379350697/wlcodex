"""Durable maintenance-window controls.

The retention migration is deliberately an operator action, not a convenient
button hidden behind a page request.  This module owns the small bit of shared
state needed to make that boundary real: freeze new user submissions first,
wait for every writer to drain, then allow archive/compact work to proceed.

It intentionally depends only on :mod:`sqlite3` so it can be used by the
standalone retention CLI without importing ``Ledger`` (which imports the
retention module during schema setup).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import sqlite3


MAINTENANCE_WINDOW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_maintenance_window (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    submissions_frozen INTEGER NOT NULL DEFAULT 0
        CHECK (submissions_frozen IN (0, 1)),
    opened_at TEXT NOT NULL DEFAULT '',
    closed_at TEXT NOT NULL DEFAULT '',
    operator_note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class MaintenanceWindowError(RuntimeError):
    """An operation is unsafe while the maintenance window is open."""


@dataclass(frozen=True)
class MaintenanceWindowStatus:
    submissions_frozen: bool
    opened_at: str = ""
    closed_at: str = ""
    operator_note: str = ""
    updated_at: str = ""
    active_work: dict[str, int] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "submissions_frozen": self.submissions_frozen,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "operator_note": self.operator_note,
            "updated_at": self.updated_at,
            "active_work": dict(self.active_work or {}),
            "ready": self.submissions_frozen and not bool(self.active_work),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc).lower()


def ensure_maintenance_schema(conn: sqlite3.Connection) -> None:
    """Install the idempotent singleton table during normal DB migration."""

    conn.executescript(MAINTENANCE_WINDOW_SCHEMA_SQL)


def _window_row(conn: sqlite3.Connection) -> sqlite3.Row | tuple[object, ...] | None:
    try:
        return conn.execute(
            """
            SELECT submissions_frozen, opened_at, closed_at, operator_note, updated_at
            FROM runtime_maintenance_window
            WHERE singleton = 1
            """
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc):
            return None
        raise


def _row_value(row: sqlite3.Row | tuple[object, ...], key: str, index: int) -> object:
    if isinstance(row, sqlite3.Row):
        return row[key]
    return row[index]


def maintenance_window_status(
    conn: sqlite3.Connection,
    *,
    include_active_work: bool = True,
) -> MaintenanceWindowStatus:
    """Return current durable gate state without mutating the database."""

    row = _window_row(conn)
    active_work = active_work_counts(conn) if include_active_work else {}
    if row is None:
        return MaintenanceWindowStatus(False, active_work=active_work)
    return MaintenanceWindowStatus(
        submissions_frozen=bool(_row_value(row, "submissions_frozen", 0)),
        opened_at=str(_row_value(row, "opened_at", 1) or ""),
        closed_at=str(_row_value(row, "closed_at", 2) or ""),
        operator_note=str(_row_value(row, "operator_note", 3) or ""),
        updated_at=str(_row_value(row, "updated_at", 4) or ""),
        active_work=active_work,
    )


# A maintenance operation must be more conservative than an individual UI
# projection.  Any row that could still cause a runtime write blocks the swap;
# historical terminal rows remain untouched and never count as active work.
_ACTIVE_WORK_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "tasks",
        """
        SELECT COUNT(*) FROM tasks
        WHERE status NOT IN (
            'done', 'failed', 'aborted', 'archived', 'completed', 'interrupted',
            'cancelled', 'superseded'
        )
        """,
    ),
    (
        "approvals",
        "SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'",
    ),
    (
        "agent_runs",
        """
        SELECT COUNT(*) FROM agent_runs
        WHERE status NOT IN (
            'done', 'failed', 'aborted', 'completed', 'interrupted',
            'cancelled', 'superseded'
        )
        """,
    ),
    (
        "orchestration_runs",
        """
        SELECT COUNT(*) FROM orchestration_runs
        WHERE status NOT IN (
            'passed', 'failed', 'aborted', 'completed', 'interrupted',
            'cancelled', 'superseded'
        )
        """,
    ),
    (
        "Relay task runs",
        """
        SELECT COUNT(*) FROM team_runs
        WHERE status NOT IN (
            'done', 'completed', 'failed', 'aborted', 'interrupted', 'archived',
            'cancelled', 'superseded'
        )
        """,
    ),
    (
        "Relay queued inputs",
        """
        SELECT COUNT(*) FROM relay_pending_inputs
        WHERE status IN ('pending', 'claimed', 'steered')
        """,
    ),
    (
        "Relay queue leases",
        "SELECT COUNT(*) FROM relay_workspace_queue_leases",
    ),
    (
        "Relay workspace queue locks",
        "SELECT COUNT(*) FROM relay_workspace_queue_locks",
    ),
    (
        "Relay completion claims",
        "SELECT COUNT(*) FROM relay_completion_claims WHERE status = 'claimed'",
    ),
    (
        "Relay completion event claims",
        "SELECT COUNT(*) FROM relay_completion_event_claims WHERE status = 'claimed'",
    ),
    (
        "legacy queued runs",
        """
        SELECT COUNT(*)
        FROM runtime_events AS queued
        WHERE queued.event_type = 'run.queued'
          AND NOT EXISTS (
              SELECT 1
              FROM runtime_events AS outcome
              WHERE outcome.causation_id = queued.id
                AND outcome.event_type IN (
                    'run.queued.consumed', 'run.started', 'run.recovery.required'
                )
          )
        """,
    ),
    (
        "Relay mutations",
        "SELECT COUNT(*) FROM relay_mutation_idempotency WHERE status = 'in_progress'",
    ),
    (
        "backend requests",
        "SELECT COUNT(*) FROM backend_requests WHERE status = 'pending'",
    ),
    (
        "native agent sessions",
        """
        SELECT COUNT(*) FROM native_agent_sessions
        WHERE status NOT IN (
            'done', 'completed', 'failed', 'error', 'aborted', 'interrupted',
            'cancelled', 'superseded', 'idle'
        )
        """,
    ),
    (
        "native Codex sessions",
        """
        SELECT COUNT(*) FROM native_codex_sessions
        WHERE status NOT IN (
            'done', 'completed', 'failed', 'error', 'aborted', 'interrupted',
            'cancelled', 'superseded', 'idle'
        )
        """,
    ),
    (
        "workflow handoffs",
        """
        SELECT COUNT(*) FROM collaboration_workflow_runs
        WHERE status NOT IN (
            'done', 'completed', 'failed', 'error', 'aborted', 'interrupted',
            'cancelled', 'superseded'
        )
        """,
    ),
)


def active_work_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return non-terminal writer counts, tolerating pre-migration databases."""

    counts: dict[str, int] = {}
    for label, query in _ACTIVE_WORK_QUERIES:
        try:
            row = conn.execute(query).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_missing_table(exc):
                continue
            raise
        count = int(row[0]) if row is not None else 0
        if count:
            counts[label] = count
    return counts


def _begin_immediate(conn: sqlite3.Connection) -> bool:
    """Start an immediate transaction unless the caller already owns one."""

    if conn.in_transaction:
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


def _finish_transaction(conn: sqlite3.Connection, owns_transaction: bool) -> None:
    if owns_transaction:
        conn.commit()


def _rollback_transaction(conn: sqlite3.Connection, owns_transaction: bool) -> None:
    if owns_transaction:
        conn.rollback()


def begin_maintenance_window(
    conn: sqlite3.Connection,
    *,
    operator_note: str = "",
) -> MaintenanceWindowStatus:
    """Freeze new user submissions before counting active work.

    The write lock makes the freeze and its initial quiescence snapshot atomic
    with respect to the next SQLite-backed submission.  The window stays open
    when work remains; operators explicitly cancel it instead of accidentally
    starting archival work on a partially drained database.
    """

    owns_transaction = _begin_immediate(conn)
    try:
        ensure_maintenance_schema(conn)
        now = _now()
        conn.execute(
            """
            INSERT INTO runtime_maintenance_window (
                singleton, submissions_frozen, opened_at, closed_at,
                operator_note, updated_at
            ) VALUES (1, 1, ?, '', ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                submissions_frozen = 1,
                opened_at = CASE
                    WHEN runtime_maintenance_window.submissions_frozen = 1
                         AND runtime_maintenance_window.opened_at != ''
                    THEN runtime_maintenance_window.opened_at
                    ELSE excluded.opened_at
                END,
                closed_at = '',
                operator_note = CASE
                    WHEN excluded.operator_note = ''
                    THEN runtime_maintenance_window.operator_note
                    ELSE excluded.operator_note
                END,
                updated_at = excluded.updated_at
            """,
            (now, str(operator_note or "").strip()[:1000], now),
        )
        status = maintenance_window_status(conn, include_active_work=True)
        _finish_transaction(conn, owns_transaction)
        return status
    except Exception:
        _rollback_transaction(conn, owns_transaction)
        raise


def cancel_maintenance_window(conn: sqlite3.Connection) -> MaintenanceWindowStatus:
    """Re-open normal submissions after an operator cancels maintenance."""

    owns_transaction = _begin_immediate(conn)
    try:
        ensure_maintenance_schema(conn)
        now = _now()
        conn.execute(
            """
            INSERT INTO runtime_maintenance_window (
                singleton, submissions_frozen, opened_at, closed_at,
                operator_note, updated_at
            ) VALUES (1, 0, '', ?, '', ?)
            ON CONFLICT(singleton) DO UPDATE SET
                submissions_frozen = 0,
                closed_at = excluded.closed_at,
                updated_at = excluded.updated_at
            """,
            (now, now),
        )
        status = maintenance_window_status(conn, include_active_work=True)
        _finish_transaction(conn, owns_transaction)
        return status
    except Exception:
        _rollback_transaction(conn, owns_transaction)
        raise


def assert_submissions_open(conn: sqlite3.Connection) -> None:
    """Reject a new external submission while maintenance is draining work."""

    status = maintenance_window_status(conn, include_active_work=False)
    if status.submissions_frozen:
        raise MaintenanceWindowError(
            "maintenance window is active: new submissions are temporarily frozen"
        )


def assert_maintenance_window_ready(conn: sqlite3.Connection) -> MaintenanceWindowStatus:
    """Require a frozen, fully drained database before apply/compact work."""

    owns_transaction = _begin_immediate(conn)
    try:
        status = maintenance_window_status(conn, include_active_work=True)
        if not status.submissions_frozen:
            raise MaintenanceWindowError(
                "maintenance window is not active; run maintenance-begin before apply or compact"
            )
        if status.active_work:
            detail = ", ".join(
                f"{label}={count}" for label, count in status.active_work.items()
            )
            raise MaintenanceWindowError(
                "maintenance window is not drained: " + detail
            )
        _finish_transaction(conn, owns_transaction)
        return status
    except Exception:
        _rollback_transaction(conn, owns_transaction)
        raise
