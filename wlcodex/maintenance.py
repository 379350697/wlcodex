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

-- A Native session is long-lived history; it is not evidence that a provider
-- turn is still writing.  A maintenance window therefore records a fresh,
-- read-only provider probe for each pre-existing active-session candidate.
-- The probe belongs to one concrete window so an old successful observation
-- can never authorize a later archive/compact operation.
CREATE TABLE IF NOT EXISTS runtime_maintenance_native_turn_probes (
    maintenance_opened_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    native_thread_id TEXT NOT NULL,
    native_turn_id TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL CHECK (verdict IN ('active', 'terminal', 'unknown')),
    observed_status TEXT NOT NULL DEFAULT '',
    diagnostic TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (maintenance_opened_at, provider, native_thread_id)
);

CREATE INDEX IF NOT EXISTS idx_runtime_maintenance_native_turn_probes_window
    ON runtime_maintenance_native_turn_probes(maintenance_opened_at, verdict);

-- Freeze the candidate set at the instant submissions are frozen. Querying
-- the mutable session index later would allow a stale ``running`` row to
-- disappear before it receives the required provider observation.
CREATE TABLE IF NOT EXISTS runtime_maintenance_native_turn_candidates (
    maintenance_opened_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    native_thread_id TEXT NOT NULL,
    captured_status TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (maintenance_opened_at, provider, native_thread_id)
);

CREATE INDEX IF NOT EXISTS idx_runtime_maintenance_native_turn_candidates_window
    ON runtime_maintenance_native_turn_candidates(maintenance_opened_at);
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
          AND role NOT LIKE '%\\_native' ESCAPE '\\'
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
        WHERE status IN ('queued', 'running', 'waiting_approval', 'paused')
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

_NATIVE_TURN_CANDIDATE_STATUSES = (
    "running",
    "active",
    "waiting_user",
    "waiting_approval",
    "paused",
)


def native_turn_probe_candidates(conn: sqlite3.Connection) -> list[str]:
    """Return stored sessions that require a live provider observation.

    ``native_codex_sessions`` is a historical session index.  In particular,
    imported ``notLoaded`` rows must never be treated as a live turn.  Only
    statuses that previously represented an in-flight turn become candidates,
    and they are resolved by ``maintenance-probe-native`` after submissions
    have been frozen.
    """

    placeholders = ",".join("?" for _ in _NATIVE_TURN_CANDIDATE_STATUSES)
    try:
        rows = conn.execute(
            f"""
            SELECT native_thread_id
            FROM native_codex_sessions
            WHERE lower(status) IN ({placeholders})
            ORDER BY id ASC
            """,
            _NATIVE_TURN_CANDIDATE_STATUSES,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc):
            return []
        raise
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


def record_native_turn_probe(
    conn: sqlite3.Connection,
    *,
    maintenance_opened_at: str,
    provider: str,
    native_thread_id: str,
    native_turn_id: str = "",
    verdict: str,
    observed_status: str = "",
    diagnostic: str = "",
) -> None:
    """Persist one sanitized, read-only Native turn observation."""

    clean_verdict = str(verdict or "").strip().lower()
    if clean_verdict not in {"active", "terminal", "unknown"}:
        raise ValueError(f"invalid native maintenance probe verdict: {verdict!r}")
    ensure_maintenance_schema(conn)
    conn.execute(
        """
        INSERT INTO runtime_maintenance_native_turn_probes (
            maintenance_opened_at, provider, native_thread_id, native_turn_id,
            verdict, observed_status, diagnostic, checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(maintenance_opened_at, provider, native_thread_id)
        DO UPDATE SET
            native_turn_id = excluded.native_turn_id,
            verdict = excluded.verdict,
            observed_status = excluded.observed_status,
            diagnostic = excluded.diagnostic,
            checked_at = excluded.checked_at
        """,
        (
            str(maintenance_opened_at or ""),
            str(provider or "codex").strip().lower() or "codex",
            str(native_thread_id or "").strip(),
            str(native_turn_id or "").strip(),
            clean_verdict,
            str(observed_status or "").strip()[:120],
            str(diagnostic or "").strip()[:500],
            _now(),
        ),
    )


def _native_turn_probe_work(
    conn: sqlite3.Connection,
    *,
    maintenance_opened_at: str,
) -> dict[str, int]:
    try:
        candidate_rows = conn.execute(
            """
            SELECT native_thread_id
            FROM runtime_maintenance_native_turn_candidates
            WHERE maintenance_opened_at = ? AND provider = 'codex'
            ORDER BY native_thread_id ASC
            """,
            (maintenance_opened_at,),
        ).fetchall()
        candidates = [str(row[0]) for row in candidate_rows if str(row[0] or "").strip()]
        if not candidates:
            return {}
        placeholders = ",".join("?" for _ in candidates)
        rows = conn.execute(
            f"""
            SELECT native_thread_id, verdict
            FROM runtime_maintenance_native_turn_probes
            WHERE maintenance_opened_at = ?
              AND provider = 'codex'
              AND native_thread_id IN ({placeholders})
            """,
            (maintenance_opened_at, *candidates),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_missing_table(exc):
            # A missing snapshot table on an old database is never treated as
            # approval to archive; fail closed until migration/begin runs.
            return {"native Codex turn probes pending": 1}
        raise
    verdicts = {str(row[0]): str(row[1]) for row in rows}
    active = sum(1 for candidate in candidates if verdicts.get(candidate) == "active")
    pending = sum(
        1 for candidate in candidates if verdicts.get(candidate) not in {"active", "terminal"}
    )
    work: dict[str, int] = {}
    if active:
        work["native Codex turns"] = active
    if pending:
        work["native Codex turn probes pending"] = pending
    return work


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
    row = _window_row(conn)
    if row is not None and bool(_row_value(row, "submissions_frozen", 0)):
        opened_at = str(_row_value(row, "opened_at", 1) or "")
        if opened_at:
            counts.update(_native_turn_probe_work(conn, maintenance_opened_at=opened_at))
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
        window_row = _window_row(conn)
        opened_at = (
            str(_row_value(window_row, "opened_at", 1) or "")
            if window_row is not None
            else now
        )
        # Capture exactly the sessions which looked in-flight *at freeze*.
        # ``notLoaded``/idle history never enters this table, while each
        # captured candidate remains probe-required even if its cache status
        # changes before the operator runs maintenance-probe-native.
        placeholders = ",".join("?" for _ in _NATIVE_TURN_CANDIDATE_STATUSES)
        conn.execute(
            f"""
            INSERT OR IGNORE INTO runtime_maintenance_native_turn_candidates (
                maintenance_opened_at, provider, native_thread_id,
                captured_status, captured_at
            )
            SELECT ?, 'codex', native_thread_id, lower(status), ?
            FROM native_codex_sessions
            WHERE lower(status) IN ({placeholders})
              AND trim(native_thread_id) != ''
            """,
            (opened_at, now, *_NATIVE_TURN_CANDIDATE_STATUSES),
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
