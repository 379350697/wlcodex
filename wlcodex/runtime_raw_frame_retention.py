"""Crash-safe retention for large provider raw-frame payloads.

The hot SQLite table remains the authoritative recent store.  Expired raw
frames are written to an immutable gzip JSONL archive *before* one SQLite
transaction records its manifest/index and removes hot rows.  A process crash
therefore leaves either the hot row intact or an indexed archive that can be
looked up by frame id; it never deletes a frame before durable archive output
exists.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from wlcodex.maintenance import (
    MaintenanceWindowError,
    active_work_counts,
    assert_maintenance_window_ready,
    begin_maintenance_window,
    cancel_maintenance_window,
    maintenance_window_status,
    native_turn_probe_candidates,
    record_native_turn_probe,
)
from wlcodex.runtime_events import redact_payload

logger = logging.getLogger(__name__)


ARCHIVE_FORMAT_VERSION = 1
_ARCHIVE_MANIFEST_FIELDS = (
    "format_version",
    "archive_id",
    "archive_date",
    "relative_path",
    "sha256",
    "frame_count",
    "min_frame_id",
    "max_frame_id",
    "min_occurred_at",
    "max_occurred_at",
    "created_at",
    "expires_at",
)
_ARCHIVE_MANIFEST_IMMUTABLE_FIELDS = tuple(
    field
    for field in _ARCHIVE_MANIFEST_FIELDS
    if field not in {"created_at", "expires_at"}
)
_ARCHIVE_MANIFEST_INTEGER_FIELDS = {
    "format_version",
    "frame_count",
    "min_frame_id",
    "max_frame_id",
}
# ``queued`` is deliberately absent: it means a future dispatch, not a
# currently writing turn.  Native providers can leave an interrupted session's
# historical run as queued, so treating it as active would retain old raw
# frames forever.
_ACTIVE_RUN_STATUSES = ("running", "waiting_approval", "paused")
_ACTIVE_NATIVE_SESSION_STATUSES = (
    "running",
    "active",
    "waiting_user",
    "waiting_approval",
    "paused",
)


# Kept independently consumable so ``Ledger.migrate``, tests, and the
# standalone CLI use exactly the same idempotent schema.
RETENTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS provider_raw_frame_archives (
    archive_id TEXT PRIMARY KEY,
    format_version INTEGER NOT NULL,
    archive_date TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    frame_count INTEGER NOT NULL,
    min_frame_id INTEGER NOT NULL,
    max_frame_id INTEGER NOT NULL,
    min_occurred_at TEXT NOT NULL,
    max_occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    purge_pending_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_provider_raw_frame_archives_expires
    ON provider_raw_frame_archives(expires_at, archive_id);

CREATE TABLE IF NOT EXISTS provider_raw_frame_archive_index (
    frame_id INTEGER PRIMARY KEY,
    archive_id TEXT NOT NULL,
    archive_line INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_engine TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    native_turn_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    FOREIGN KEY(archive_id) REFERENCES provider_raw_frame_archives(archive_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_raw_frame_archive_index_archive
    ON provider_raw_frame_archive_index(archive_id, archive_line);
CREATE INDEX IF NOT EXISTS idx_provider_raw_frame_archive_index_sequence
    ON provider_raw_frame_archive_index(
        provider, provider_engine, native_session_id, native_turn_id, sequence
    );

CREATE TABLE IF NOT EXISTS provider_raw_frame_sequence_cursors (
    provider TEXT NOT NULL,
    provider_engine TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    native_turn_id TEXT NOT NULL,
    last_sequence INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, provider_engine, native_session_id, native_turn_id)
);

-- ``scheduled_apply_enabled`` is deliberately not enough to authorize an
-- initial archive.  A configuration typo must not turn a normal background
-- worker into a one-shot migration of a decades-old/large runtime database.
-- This durable marker is set only by the explicit, drained maintenance CLI
-- after its apply pass has also verified every indexed archive.
CREATE TABLE IF NOT EXISTS provider_raw_frame_retention_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    initial_migration_verified INTEGER NOT NULL DEFAULT 0
        CHECK (initial_migration_verified IN (0, 1)),
    initial_migration_verified_at TEXT NOT NULL DEFAULT ''
);
"""


class RawFrameArchiveError(RuntimeError):
    """An indexed raw-frame archive cannot be read or verified safely."""


@dataclass(frozen=True)
class RuntimeRetentionPolicy:
    """Retention policy for provider raw frame bytes.

    ``archive_dir`` is deliberately explicit.  It may be placed on a separate
    volume from SQLite, but needs the same durability expectations as the
    runtime database.
    """

    archive_dir: Path
    hot_retention_days: int = 7
    archive_retention_days: int = 90
    interval_seconds: int = 6 * 60 * 60
    batch_size: int = 250

    def __post_init__(self) -> None:
        if self.hot_retention_days < 1:
            raise ValueError("hot_retention_days must be at least 1")
        if self.archive_retention_days < 1:
            raise ValueError("archive_retention_days must be at least 1")
        if self.interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")


@dataclass(frozen=True)
class RetentionResult:
    candidate_frames: int = 0
    archived_frames: int = 0
    skipped_active_frames: int = 0
    purged_archives: int = 0
    archived_bytes: int = 0
    compacted: bool = False


@dataclass(frozen=True)
class ArchiveVerification:
    archive_count: int
    frame_count: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class CompactResult:
    """A verified, atomically swapped SQLite compact result."""

    database_path: Path
    rollback_snapshot_path: Path
    source_bytes: int
    compacted_bytes: int


def apply_retention_schema(conn: sqlite3.Connection) -> None:
    """Install retention support tables for tests and the standalone CLI.

    Production migration belongs in :meth:`Ledger.migrate`; this function is
    intentionally idempotent so a CLI can safely inspect an older database.
    """

    conn.executescript(RETENTION_SCHEMA_SQL)
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("PRAGMA table_info(provider_raw_frame_archives)").fetchall()
    }
    if "purge_pending_at" not in columns:
        conn.execute(
            "ALTER TABLE provider_raw_frame_archives "
            "ADD COLUMN purge_pending_at TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


def initial_retention_migration_verified(conn: sqlite3.Connection) -> bool:
    """Whether an operator has completed the first verified archive pass.

    Missing state is deliberately treated as unverified.  This makes an
    upgrade from any older runtime conservative even before its normal
    migration has installed the new state table.
    """

    try:
        row = conn.execute(
            """
            SELECT initial_migration_verified
            FROM provider_raw_frame_retention_state
            WHERE singleton = 1
            """
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise
    return bool(row is not None and int(row[0]))


async def _probe_native_turns(
    conn: sqlite3.Connection,
    config: Any,
) -> dict[str, object]:
    """Probe only existing Native-turn candidates without controlling them.

    The retention CLI must not create a private app-server as a side effect of
    a maintenance check.  It either attaches to the configured Codex daemon,
    or to the already-running local app-server endpoint.  A missing endpoint
    is deliberately recorded as ``unknown`` so the maintenance window remains
    blocked instead of guessing that an old turn is finished.
    """

    from wlcodex.codex_native.client import CodexNativeClient
    from wlcodex.codex_native.controller import _active_turn_id, _latest_turn, _turns
    from wlcodex.codex_native.transport import (
        CodexAppServerWebSocketTransport,
        CodexDaemonTransport,
    )

    status = maintenance_window_status(conn, include_active_work=False)
    if not status.submissions_frozen or not status.opened_at:
        raise MaintenanceWindowError(
            "maintenance window is not active; run maintenance-begin before maintenance-probe-native"
        )
    candidates = native_turn_probe_candidates(conn)
    if not candidates:
        return {"probed": 0, "active": 0, "terminal": 0, "unknown": 0}

    native_config = config.codex_native
    configured_transport = str(native_config.transport or "daemon").strip().lower()
    sock_path = native_config.sock_path.expanduser() if native_config.sock_path else None
    if configured_transport in {"daemon", "proxy"} and sock_path is not None and sock_path.exists():
        transport = CodexDaemonTransport(
            binary=config.codex.binary,
            sock_path=sock_path,
            fallback_app_server=None,
        )
    else:
        transport = CodexAppServerWebSocketTransport(
            binary=config.codex.binary,
            listen_endpoint=native_config.listen_endpoint,
            connect_endpoint=native_config.listen_endpoint,
            startup_timeout_seconds=config.backend.startup_timeout_seconds,
            remote_control=native_config.remote_control,
            spawn_process=False,
        )

    client = CodexNativeClient(
        send_json=transport.send_json,
        close=transport.close,
        request_timeout_seconds=config.backend.request_timeout_seconds,
        metadata=transport.describe,
    )

    async def on_message(message: dict[str, Any]) -> None:
        await client.rpc.receive_message(message)

    try:
        try:
            await transport.start(on_message)
        except Exception as exc:
            # A provider connection failure is itself an unknown observation.
            # Persist that conclusion for every candidate so maintenance-status
            # remains fail-closed and explains why it cannot advance, rather
            # than relying on an unhandled CLI failure with no durable record.
            results = _record_native_probe_connection_failure(
                conn,
                maintenance_opened_at=status.opened_at,
                candidates=candidates,
                diagnostic=f"provider_connect_failed:{type(exc).__name__}",
            )
        else:
            results = await _record_native_turn_probes(
                conn,
                maintenance_opened_at=status.opened_at,
                candidates=candidates,
                read_session=client.read_session,
                active_turn_id=_active_turn_id,
                latest_turn=_latest_turn,
                turns=_turns,
            )
    finally:
        await client.close()
    return results


def _record_native_probe_connection_failure(
    conn: sqlite3.Connection,
    *,
    maintenance_opened_at: str,
    candidates: list[str],
    diagnostic: str,
) -> dict[str, int]:
    """Make an unavailable read endpoint a durable, fail-closed observation."""

    for native_thread_id in candidates:
        record_native_turn_probe(
            conn,
            maintenance_opened_at=maintenance_opened_at,
            provider="codex",
            native_thread_id=native_thread_id,
            verdict="unknown",
            diagnostic=diagnostic,
        )
    conn.commit()
    return {
        "probed": len(candidates),
        "active": 0,
        "terminal": 0,
        "unknown": len(candidates),
    }


async def _record_native_turn_probes(
    conn: sqlite3.Connection,
    *,
    maintenance_opened_at: str,
    candidates: list[str],
    read_session: Callable[..., Any],
    active_turn_id: Callable[[dict[str, Any]], str],
    latest_turn: Callable[[list[Any]], Any],
    turns: Callable[[dict[str, Any]], list[Any]],
) -> dict[str, int]:
    """Persist probe results; isolated for control-free integration tests."""

    results = {"probed": 0, "active": 0, "terminal": 0, "unknown": 0}
    for native_thread_id in candidates:
        verdict = "unknown"
        native_turn_id = ""
        observed_status = ""
        diagnostic = ""
        try:
            detail = await read_session(native_thread_id, include_turns=True)
            native_turn_id = active_turn_id(detail)
            current_turn = latest_turn(turns(detail))
            if isinstance(current_turn, dict):
                observed_status = str(current_turn.get("status") or "").strip().lower()
                if not native_turn_id and observed_status in {
                    "waiting_user",
                    "waitinguser",
                    "waiting_approval",
                    "waitingapproval",
                    "waitingonapproval",
                    "waiting_on_approval",
                }:
                    native_turn_id = str(
                        current_turn.get("turnId") or current_turn.get("id") or ""
                    ).strip()
            if not observed_status:
                thread = detail.get("thread")
                if isinstance(thread, dict):
                    observed_status = str(thread.get("status") or "").strip().lower()
            if native_turn_id:
                # Includes Codex's active approval-wait state.  A waiting
                # turn remains a live provider-owned turn for maintenance
                # purposes even though it is not currently producing tokens.
                verdict = "active"
            elif observed_status in {
                "completed",
                "complete",
                "done",
                "failed",
                "error",
                "cancelled",
                "canceled",
                "interrupted",
                "aborted",
                "idle",
                "notloaded",
                "not_loaded",
            }:
                verdict = "terminal"
            else:
                # A successful RPC with no current turn alone is not enough
                # to prove that a non-terminal provider state is drained.
                # Keep it fail-closed until the provider returns a known
                # terminal/idle state or a real active turn identifier.
                diagnostic = f"provider_unconfirmed_status:{observed_status or 'missing'}"
        except Exception as exc:
            # Preserve only an error category.  Provider exception text may
            # include a remote payload, which does not belong in maintenance
            # metadata or a CLI transcript.
            diagnostic = f"provider_read_failed:{type(exc).__name__}"
        record_native_turn_probe(
            conn,
            maintenance_opened_at=maintenance_opened_at,
            provider="codex",
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            verdict=verdict,
            observed_status=observed_status,
            diagnostic=diagnostic,
        )
        conn.commit()
        results[verdict] += 1
        results["probed"] += 1
    return results


def mark_initial_retention_migration_verified(conn: sqlite3.Connection) -> None:
    """Persist the scheduler authorization after a verified maintenance pass.

    This defensive second maintenance check keeps the marker coupled to the
    operator gate even if a future caller accidentally reaches this helper
    outside the CLI flow.
    """

    assert_maintenance_window_ready(conn)
    conn.execute(
        """
        INSERT INTO provider_raw_frame_retention_state (
            singleton, initial_migration_verified, initial_migration_verified_at
        ) VALUES (1, 1, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            initial_migration_verified = 1,
            initial_migration_verified_at = excluded.initial_migration_verified_at
        """,
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()


def default_archive_dir(conn: sqlite3.Connection) -> Path:
    """Choose a local archive directory when legacy callers omit one."""

    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        name = str(row[1])
        path = str(row[2])
        if name == "main" and path:
            return Path(path).parent / "provider-raw-frame-archives"
    return Path("runtime") / "provider-raw-frame-archives"


def compact_sqlite_database(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
) -> CompactResult:
    """Build, verify, and atomically install a compacted SQLite copy.

    This is intentionally a maintenance-only operation.  ``VACUUM INTO``
    writes a separate candidate first; the source remains untouched unless
    the candidate passes ``integrity_check``.  A hard-linked pre-compact
    snapshot is kept beside the database for an operator rollback.
    """

    source_path = Path(database_path).expanduser().resolve()
    connection_path = _sqlite_database_path(conn)
    if connection_path != source_path:
        raise RawFrameArchiveError(
            "SQLite compaction source does not match the supplied connection"
        )
    if not source_path.is_file():
        raise RawFrameArchiveError(f"SQLite database is missing: {source_path}")
    # This public helper is also used outside the CLI.  Do not let a caller
    # bypass the frozen-and-drained maintenance contract merely by calling it
    # directly rather than going through ``main``.
    _assert_maintenance_quiescent(conn)
    assert_maintenance_window_ready(conn)
    source_bytes = source_path.stat().st_size
    _preflight_compaction_space(source_path, source_bytes)
    candidate_path = _new_compaction_candidate_path(source_path)
    rollback_snapshot_path = source_path.with_name(
        f"{source_path.name}.precompact-"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:12]}"
    )
    try:
        conn.commit()
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise RawFrameArchiveError(
                    "cannot compact while another SQLite connection holds an active WAL reader"
                )
        except sqlite3.OperationalError:
            # Databases without WAL support still compact safely from the
            # committed connection snapshot.
            pass
        conn.execute("VACUUM INTO ?", (str(candidate_path),))
        _fsync_file(candidate_path)
        integrity = _sqlite_integrity_check(candidate_path)
        if integrity != "ok":
            raise RawFrameArchiveError(
                f"compacted SQLite integrity_check failed: {integrity}"
            )

        # Close before replacement so this works consistently on macOS and
        # Windows, not merely on Unix filesystems that permit renamed opens.
        conn.close()
        _remove_sqlite_sidecars(source_path)
        try:
            os.link(source_path, rollback_snapshot_path)
        except OSError as exc:
            raise RawFrameArchiveError(
                "cannot create pre-compact rollback snapshot; use a local filesystem "
                "that supports hard links or make a backup before compacting"
            ) from exc
        _fsync_directory(source_path.parent)
        os.replace(candidate_path, source_path)
        _fsync_directory(source_path.parent)
        return CompactResult(
            database_path=source_path,
            rollback_snapshot_path=rollback_snapshot_path,
            source_bytes=source_bytes,
            compacted_bytes=source_path.stat().st_size,
        )
    except Exception:
        candidate_path.unlink(missing_ok=True)
        raise


def _sqlite_database_path(conn: sqlite3.Connection) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if str(row[1]) == "main" and str(row[2]):
            return Path(str(row[2])).expanduser().resolve()
    raise RawFrameArchiveError("SQLite compaction requires a file-backed main database")


def _assert_maintenance_quiescent(conn: sqlite3.Connection) -> None:
    """Refuse a database swap until all work that could write has drained."""
    active = active_work_counts(conn)
    if active:
        raise RawFrameArchiveError(
            "refusing SQLite compaction while active work remains: "
            + ", ".join(f"{label}={count}" for label, count in active.items())
        )


def _preflight_compaction_space(source_path: Path, source_bytes: int) -> None:
    filesystem = os.statvfs(source_path.parent)
    available = filesystem.f_bavail * filesystem.f_frsize
    required = max(source_bytes, 16 * 1024 * 1024)
    if available < required:
        raise RawFrameArchiveError(
            f"insufficient disk for compact candidate: need at least {required} bytes, "
            f"have {available}"
        )


def _new_compaction_candidate_path(source_path: Path) -> Path:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{source_path.name}.compact-",
        suffix=".sqlite",
        dir=source_path.parent,
    )
    os.close(fd)
    candidate_path = Path(temporary_name)
    candidate_path.unlink()
    return candidate_path


def _remove_sqlite_sidecars(source_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{source_path}{suffix}")
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            raise RawFrameArchiveError(
                f"cannot clear SQLite sidecar before atomic compaction swap: {sidecar}"
            ) from exc


def _sqlite_integrity_check(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    values = [str(row[0]) for row in rows]
    return "ok" if values == ["ok"] else "; ".join(values)


class RuntimeRawFrameRetention:
    """Archive and verify expired provider raw frames.

    This class is synchronous by design: callers run it in a scheduler or
    explicit maintenance command, never from a page GET, SSE snapshot, or
    request handler.
    """

    def __init__(
        self,
        runtime_store: Any,
        policy: RuntimeRetentionPolicy,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = runtime_store
        self._conn: sqlite3.Connection = runtime_store._conn
        self._policy = policy
        self._now = now or (lambda: datetime.now(UTC))

    def run(
        self,
        *,
        apply: bool,
        compact: bool = False,
        require_maintenance: bool = False,
    ) -> RetentionResult:
        """Run one retention pass.

        ``apply=False`` is a completely non-mutating dry run.  ``compact`` is
        intentionally a separate maintenance action; it verifies archives and
        uses ``VACUUM INTO`` plus an atomic database swap after archival work
        has committed.
        """

        require_maintenance = bool(require_maintenance or compact)
        if require_maintenance:
            # Compaction is one maintenance transaction from archive through
            # database swap.  Check before archival starts so a caller cannot
            # mutate retention state and only then discover that swapping is
            # forbidden.
            _assert_maintenance_quiescent(self._conn)
            assert_maintenance_window_ready(self._conn)
        now = _as_utc(self._now())
        cutoff = now - timedelta(days=self._policy.hot_retention_days)
        candidate_frames = 0
        skipped_active = 0
        archived_frames = 0
        archived_bytes = 0
        cursor: tuple[str, int] | None = None
        archive_dir_created = False
        while True:
            candidates, skipped, cursor = self._eligible_frame_page(
                cutoff,
                after=cursor,
            )
            if cursor is None:
                break
            candidate_frames += len(candidates)
            skipped_active += skipped
            if not apply or not candidates:
                continue
            if not archive_dir_created:
                self._policy.archive_dir.mkdir(parents=True, exist_ok=True)
                archive_dir_created = True
            by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for row in candidates:
                by_date[_utc_date(str(row["occurred_at"]))].append(row)
            for archive_date in sorted(by_date):
                count, byte_count, newly_skipped = self._archive_batch(
                    by_date[archive_date],
                    archive_date=archive_date,
                    now=now,
                    require_maintenance=require_maintenance,
                )
                archived_frames += count
                archived_bytes += byte_count
                skipped_active += newly_skipped

        if not apply:
            return RetentionResult(
                candidate_frames=candidate_frames,
                skipped_active_frames=skipped_active,
            )

        # Archive expiry is destructive too.  A maintenance migration must
        # not continue cleanup after its drain gate becomes non-ready; normal
        # periodic retention deliberately remains available without a freeze.
        if require_maintenance:
            assert_maintenance_window_ready(self._conn)
        purged = self._purge_expired_archives(now)
        compacted = False
        if compact:
            verification = self.verify()
            if not verification.ok:
                raise RawFrameArchiveError(
                    "refusing compaction until archive verification passes: "
                    + "; ".join(verification.errors)
                )
            self.compact()
            compacted = True
        return RetentionResult(
            candidate_frames=candidate_frames,
            archived_frames=archived_frames,
            skipped_active_frames=skipped_active,
            purged_archives=purged,
            archived_bytes=archived_bytes,
            compacted=compacted,
        )

    def compact(self) -> CompactResult:
        """Compact with a verified copy and atomic swap, never in-place VACUUM."""

        # Re-check immediately before handing SQLite to the swap operation.
        # CLI callers may have spent substantial time verifying archives after
        # their earlier maintenance-status check.
        assert_maintenance_window_ready(self._conn)
        return compact_sqlite_database(
            self._conn,
            database_path=_sqlite_database_path(self._conn),
        )

    def verify(self) -> ArchiveVerification:
        """Verify every indexed archive file and its frame index.

        Archives are deliberately allowed to contain a large historical
        migration.  Do not materialize either the decompressed JSONL payload
        or its SQLite index here: verification is an operational safety gate,
        so it must remain usable on the very database it is meant to protect.
        """

        errors: list[str] = []
        archive_count = 0
        frame_count = 0
        try:
            manifests = self._conn.execute(
                "SELECT * FROM provider_raw_frame_archives ORDER BY archive_id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return ArchiveVerification(0, 0, (f"retention schema unavailable: {exc}",))
        for manifest in manifests:
            archive_count += 1
            archive_id = str(manifest["archive_id"])
            try:
                archive_path = _archive_path(
                    self._policy.archive_dir,
                    str(manifest["relative_path"]),
                )
                sidecar_manifest = _read_archive_manifest(
                    _manifest_path(archive_path),
                    expected_archive_id=archive_id,
                    expected_relative_path=str(manifest["relative_path"]),
                )
                _assert_manifest_matches(
                    sidecar_manifest,
                    manifest,
                    fields=_ARCHIVE_MANIFEST_FIELDS,
                    context="sidecar manifest does not match SQLite manifest",
                )
                expected_count = int(manifest["frame_count"])
                index_rows = self._conn.execute(
                    """
                    SELECT frame_id, archive_line
                    FROM provider_raw_frame_archive_index
                    WHERE archive_id = ?
                    ORDER BY archive_line
                    """,
                    (archive_id,),
                )
                indexed = iter(index_rows)
                found_count = 0
                for expected_line, record in enumerate(
                    _iter_archive_records(archive_path, expected_archive_id=archive_id), start=1
                ):
                    found_count = expected_line
                    index = next(indexed, None)
                    if index is None:
                        raise RawFrameArchiveError(
                            f"archive index ends before archive line {expected_line}"
                        )
                    if int(index["archive_line"]) != expected_line or int(
                        index["frame_id"]
                    ) != int(record["id"]):
                        raise RawFrameArchiveError(
                            f"archive index mismatch at line {expected_line}"
                        )
                if found_count != expected_count:
                    raise RawFrameArchiveError(
                        f"expected {expected_count} frames, found {found_count}"
                    )
                extra_index = next(indexed, None)
                if extra_index is not None:
                    raise RawFrameArchiveError("archive index has rows beyond archive payload")
            except RawFrameArchiveError as exc:
                errors.append(f"{archive_id}: {exc}")
                continue
            frame_count += found_count
        return ArchiveVerification(archive_count, frame_count, tuple(errors))

    def _eligible_frame_page(
        self,
        cutoff: datetime,
        *,
        after: tuple[str, int] | None,
    ) -> tuple[list[sqlite3.Row], int, tuple[str, int] | None]:
        """Read one metadata page without retaining an initial 66GB backlog.

        The cursor is based on the immutable ``(occurred_at, id)`` ordering,
        so hot-row deletion from an earlier page cannot make a later page
        disappear from the scan.
        """

        params: list[object] = [cutoff.isoformat()]
        cursor_clause = ""
        if after is not None:
            cursor_clause = """
                AND (
                    occurred_at > ?
                    OR (occurred_at = ? AND id > ?)
                )
            """
            params.extend((after[0], after[0], after[1]))
        params.append(self._policy.batch_size)
        try:
            rows = self._conn.execute(
                f"""
                SELECT id, provider, provider_engine, native_session_id,
                       native_turn_id, sequence, occurred_at, agent_run_id
                FROM provider_raw_frames
                WHERE occurred_at < ?
                {cursor_clause}
                ORDER BY occurred_at ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise RawFrameArchiveError(f"provider_raw_frames unavailable: {exc}") from exc
        if not rows:
            return [], 0, None
        next_cursor = (str(rows[-1]["occurred_at"]), int(rows[-1]["id"]))

        active_agent_ids = self._active_agent_run_ids(
            {int(row["agent_run_id"]) for row in rows if row["agent_run_id"] is not None}
        )
        active_native_turns, active_native_sessions_without_turn = (
            self._active_native_turn_guards()
        )
        eligible: list[sqlite3.Row] = []
        skipped = 0
        for row in rows:
            occurred_at = _parse_timestamp(str(row["occurred_at"]))
            if occurred_at is None or occurred_at >= cutoff:
                # Unknown timestamp must remain hot instead of silently losing data.
                continue
            agent_run_id = row["agent_run_id"]
            native_identity = (
                str(row["provider"]),
                str(row["provider_engine"]),
                str(row["native_session_id"]),
            )
            native_turn_identity = (*native_identity, str(row["native_turn_id"]))
            if (
                agent_run_id is not None and int(agent_run_id) in active_agent_ids
            ) or (
                native_identity in active_native_sessions_without_turn
                or native_turn_identity in active_native_turns
            ):
                skipped += 1
                continue
            eligible.append(row)
        return eligible, skipped, next_cursor

    def _active_agent_run_ids(self, ids: set[int]) -> set[int]:
        if not ids:
            return set()
        active: set[int] = set()
        for chunk in _chunks(sorted(ids), 900):
            placeholders = ",".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""
                SELECT id FROM agent_runs
                WHERE id IN ({placeholders})
                  AND status IN ({','.join('?' for _ in _ACTIVE_RUN_STATUSES)})
                """,
                (*chunk, *_ACTIVE_RUN_STATUSES),
            ).fetchall()
            active.update(int(row["id"]) for row in rows)
        return active

    def _active_native_turn_guards(
        self,
    ) -> tuple[set[tuple[str, str, str, str]], set[tuple[str, str, str]]]:
        """Return active native turns, plus sessions missing a current turn id.

        A long-lived session can have both a current streaming turn and old
        historical turns.  Retention must preserve the former without keeping
        every old raw frame hot forever.  If an older session lacks a current
        turn id, retain it conservatively until the provider state is clear.
        """

        statuses = ",".join("?" for _ in _ACTIVE_NATIVE_SESSION_STATUSES)
        active_turns: set[tuple[str, str, str, str]] = set()
        active_sessions_without_turn: set[tuple[str, str, str]] = set()

        def add_row(
            *,
            provider: str,
            provider_engine: str,
            native_session_id: str,
            native_turn_id: object,
        ) -> None:
            session_identity = (provider, provider_engine, native_session_id)
            turn_id = str(native_turn_id or "").strip()
            if turn_id:
                active_turns.add((*session_identity, turn_id))
            else:
                active_sessions_without_turn.add(session_identity)

        try:
            rows = self._conn.execute(
                f"""
                SELECT provider, provider_engine, native_session_id, last_turn_id
                FROM native_agent_sessions
                WHERE status IN ({statuses})
                """,
                _ACTIVE_NATIVE_SESSION_STATUSES,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            add_row(
                provider=str(row["provider"]),
                provider_engine=str(row["provider_engine"]),
                native_session_id=str(row["native_session_id"]),
                native_turn_id=row["last_turn_id"],
            )

        try:
            codex_rows = self._conn.execute(
                f"""
                SELECT native_thread_id, last_turn_id
                FROM native_codex_sessions
                WHERE status IN ({statuses})
                """,
                _ACTIVE_NATIVE_SESSION_STATUSES,
            ).fetchall()
        except sqlite3.OperationalError:
            codex_rows = []
        for row in codex_rows:
            add_row(
                provider="codex",
                provider_engine="app-server",
                native_session_id=str(row["native_thread_id"]),
                native_turn_id=row["last_turn_id"],
            )
        return active_turns, active_sessions_without_turn

    def _archive_batch(
        self,
        metadata_rows: list[sqlite3.Row],
        *,
        archive_date: str,
        now: datetime,
        require_maintenance: bool = False,
    ) -> tuple[int, int, int]:
        frame_ids = [int(row["id"]) for row in metadata_rows]
        if not frame_ids:
            return 0, 0, 0
        placeholders = ",".join("?" for _ in frame_ids)
        rows = self._conn.execute(
            f"SELECT * FROM provider_raw_frames WHERE id IN ({placeholders}) ORDER BY id ASC",
            frame_ids,
        ).fetchall()
        if not rows:
            return 0, 0, 0

        # Re-check a race with a status transition immediately before archival.
        active_frame_ids = self._active_frame_ids(rows)
        safe_rows = [row for row in rows if int(row["id"]) not in active_frame_ids]
        skipped = len(active_frame_ids)
        if not safe_rows:
            return 0, 0, skipped

        records = [_archive_record(row) for row in safe_rows]
        raw_bytes = _archive_bytes(records)
        archive_id = hashlib.sha256(raw_bytes).hexdigest()
        relative_path = _archive_relative_path(
            archive_date,
            min(int(row["id"]) for row in safe_rows),
            max(int(row["id"]) for row in safe_rows),
            archive_id,
        )
        archive_path = _archive_path(self._policy.archive_dir, relative_path)
        manifest = {
            "format_version": ARCHIVE_FORMAT_VERSION,
            "archive_id": archive_id,
            "archive_date": archive_date,
            "relative_path": relative_path,
            "sha256": archive_id,
            "frame_count": len(records),
            "min_frame_id": min(int(row["id"]) for row in safe_rows),
            "max_frame_id": max(int(row["id"]) for row in safe_rows),
            "min_occurred_at": min(str(row["occurred_at"]) for row in safe_rows),
            "max_occurred_at": max(str(row["occurred_at"]) for row in safe_rows),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=self._policy.archive_retention_days)).isoformat(),
        }
        manifest = _ensure_archive_files(archive_path, raw_bytes, manifest)

        # File durability precedes DB mutation.  The transaction below makes
        # manifest/index insertion and hot-row deletion all-or-nothing.
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # The immediate write lease prevents a later status update from
            # committing while we decide whether deletion is safe.  Re-check
            # the complete maintenance gate after taking it for an explicit
            # maintenance migration, so activity cannot race a destructive
            # hot-row deletion. Periodic retention still uses its active-frame
            # guard without imposing a global submission freeze.
            if require_maintenance:
                assert_maintenance_window_ready(self._conn)
            # A second, frame-specific check guards provider activity that is
            # not represented by a higher-level maintenance count.
            newly_active = self._active_frame_ids(safe_rows)
            if newly_active:
                self._conn.rollback()
                return 0, 0, skipped + len(newly_active)
            self._conn.execute(
                """
                INSERT INTO provider_raw_frame_archives (
                    archive_id, format_version, archive_date, relative_path, sha256,
                    frame_count, min_frame_id, max_frame_id, min_occurred_at,
                    max_occurred_at, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id) DO NOTHING
                """,
                (
                    manifest["archive_id"],
                    manifest["format_version"],
                    manifest["archive_date"],
                    manifest["relative_path"],
                    manifest["sha256"],
                    manifest["frame_count"],
                    manifest["min_frame_id"],
                    manifest["max_frame_id"],
                    manifest["min_occurred_at"],
                    manifest["max_occurred_at"],
                    manifest["created_at"],
                    manifest["expires_at"],
                ),
            )
            stored_manifest = self._conn.execute(
                """
                SELECT relative_path, sha256, frame_count
                FROM provider_raw_frame_archives
                WHERE archive_id = ?
                """,
                (archive_id,),
            ).fetchone()
            if stored_manifest is None or (
                str(stored_manifest["relative_path"]) != relative_path
                or str(stored_manifest["sha256"]) != archive_id
                or int(stored_manifest["frame_count"]) != len(records)
            ):
                raise RawFrameArchiveError(
                    "existing archive manifest conflicts with the durable archive"
                )
            for archive_line, row in enumerate(safe_rows, start=1):
                self._conn.execute(
                    """
                    INSERT INTO provider_raw_frame_archive_index (
                        frame_id, archive_id, archive_line, provider, provider_engine,
                        native_session_id, native_turn_id, sequence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(frame_id) DO NOTHING
                    """,
                    (
                        int(row["id"]),
                        archive_id,
                        archive_line,
                        str(row["provider"]),
                        str(row["provider_engine"]),
                        str(row["native_session_id"]),
                        str(row["native_turn_id"]),
                        int(row["sequence"]),
                    ),
                )
            # Older databases can contain raw frames created before sequence
            # cursors existed.  Seed the durable high-water mark before the
            # hot rows (and, eventually, their archive index) disappear, so
            # a reused provider turn can never restart at sequence 1.
            for row in safe_rows:
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
                        str(row["provider"]),
                        str(row["provider_engine"]),
                        str(row["native_session_id"]),
                        str(row["native_turn_id"]),
                        int(row["sequence"]),
                        now.isoformat(),
                    ),
                )
            current_ids = [int(row["id"]) for row in safe_rows]
            current_placeholders = ",".join("?" for _ in current_ids)
            indexed_rows = self._conn.execute(
                f"""
                SELECT frame_id, archive_id, archive_line
                FROM provider_raw_frame_archive_index
                WHERE frame_id IN ({current_placeholders})
                """,
                current_ids,
            ).fetchall()
            indexed_by_frame_id = {int(row["frame_id"]): row for row in indexed_rows}
            for expected_line, frame_id in enumerate(current_ids, start=1):
                index_row = indexed_by_frame_id.get(frame_id)
                if index_row is None:
                    raise RawFrameArchiveError(
                        f"frame {frame_id} was not indexed before hot-row deletion"
                    )
                if str(index_row["archive_id"]) != archive_id:
                    raise RawFrameArchiveError(
                        f"frame {frame_id} is already indexed by a different archive"
                    )
                if int(index_row["archive_line"]) != expected_line:
                    raise RawFrameArchiveError(
                        f"frame {frame_id} has an inconsistent archive line"
                    )
            self._conn.execute(
                f"DELETE FROM provider_raw_frames WHERE id IN ({current_placeholders})",
                current_ids,
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return len(safe_rows), len(raw_bytes), skipped

    def _active_frame_ids(self, rows: Iterable[sqlite3.Row]) -> set[int]:
        """Identify rows that must stay hot because their turn is active."""

        materialized = list(rows)
        active_agent_ids = self._active_agent_run_ids(
            {
                int(row["agent_run_id"])
                for row in materialized
                if row["agent_run_id"] is not None
            }
        )
        active_native_turns, active_native_sessions_without_turn = (
            self._active_native_turn_guards()
        )
        active_frame_ids: set[int] = set()
        for row in materialized:
            native_identity = (
                str(row["provider"]),
                str(row["provider_engine"]),
                str(row["native_session_id"]),
            )
            native_turn_identity = (*native_identity, str(row["native_turn_id"]))
            if (
                row["agent_run_id"] is not None
                and int(row["agent_run_id"]) in active_agent_ids
            ) or (
                native_identity in active_native_sessions_without_turn
                or native_turn_identity in active_native_turns
            ):
                active_frame_ids.add(int(row["id"]))
        return active_frame_ids

    def _purge_expired_archives(self, now: datetime) -> int:
        rows = self._conn.execute(
            """
            SELECT archive_id, relative_path
            FROM provider_raw_frame_archives
            WHERE expires_at <= ?
            ORDER BY expires_at ASC, archive_id ASC
            """,
            (now.isoformat(),),
        ).fetchall()
        purged = 0
        for row in rows:
            archive_id = str(row["archive_id"])
            relative_path = str(row["relative_path"])
            archive_path = _archive_path(self._policy.archive_dir, relative_path)
            # File deletion and SQLite deletion cannot share one transaction.
            # Publish a durable tombstone first, so a reader never follows a
            # still-live index into a half-deleted gzip or manifest.
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    UPDATE provider_raw_frame_archives
                    SET purge_pending_at = CASE
                        WHEN purge_pending_at = '' THEN ?
                        ELSE purge_pending_at
                    END
                    WHERE archive_id = ?
                    """,
                    (now.isoformat(), archive_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            removal_failed = False
            # Keep the manifest/index until both files have been removed.  The
            # previous order committed the database delete first, so an I/O
            # failure orphaned an archive forever: the scheduler no longer had
            # a durable record from which to retry it.
            for path in (archive_path, _manifest_path(archive_path)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    removal_failed = True
                    logger.warning("Could not remove expired raw frame archive %s", path)
            if removal_failed:
                continue
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "DELETE FROM provider_raw_frame_archive_index WHERE archive_id = ?",
                    (archive_id,),
                )
                self._conn.execute(
                    "DELETE FROM provider_raw_frame_archives WHERE archive_id = ?",
                    (archive_id,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            purged += 1
        return purged


def read_archived_provider_raw_frame(
    conn: sqlite3.Connection,
    frame_id: int,
    *,
    archive_dir: Path,
) -> dict[str, Any] | None:
    """Read one archive-indexed provider raw frame, validating its payload hash."""

    try:
        row = conn.execute(
            """
            SELECT index_row.archive_line, archive.relative_path, archive.archive_id,
                   archive.sha256, archive.purge_pending_at
            FROM provider_raw_frame_archive_index AS index_row
            JOIN provider_raw_frame_archives AS archive
              ON archive.archive_id = index_row.archive_id
            WHERE index_row.frame_id = ?
            """,
            (frame_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    if row is None:
        return None
    if str(row["purge_pending_at"] or ""):
        # The retention worker published this terminal tombstone before touching
        # either file.  A frame past retention is therefore consistently absent
        # even if an interrupted filesystem removal is awaiting a retry.
        return None
    archive_id = str(row["archive_id"])
    if str(row["sha256"]) != archive_id:
        raise RawFrameArchiveError("SQLite archive manifest sha256 does not match archive id")
    archive_path = _archive_path(archive_dir, str(row["relative_path"]))
    try:
        _read_archive_manifest(
            _manifest_path(archive_path),
            expected_archive_id=archive_id,
            expected_relative_path=str(row["relative_path"]),
        )
        record = _read_archive_record_at(
            archive_path,
            expected_archive_id=archive_id,
            line_number=int(row["archive_line"]),
        )
    except RawFrameArchiveError:
        # A reader can race the retention worker between its initial SQLite
        # lookup and opening the files.  Re-read the durable tombstone before
        # reporting corruption: an archive that was deliberately retired is
        # absent, never an index-to-missing-file failure.
        if _archive_is_retired_or_missing(conn, archive_id):
            return None
        raise
    if int(record.get("id", -1)) != frame_id:
        raise RawFrameArchiveError("archive index points to a different frame")
    raw_payload = record.get("raw_payload")
    if not isinstance(raw_payload, dict):
        raw_payload = {"value": raw_payload}
    payload_sha256 = str(record.get("payload_sha256", ""))
    if payload_sha256 != _payload_sha256(raw_payload):
        raise RawFrameArchiveError("raw frame payload hash does not match archive")
    record = dict(record)
    record["raw_payload"] = raw_payload
    return record


def _archive_is_retired_or_missing(conn: sqlite3.Connection, archive_id: str) -> bool:
    try:
        row = conn.execute(
            """
            SELECT purge_pending_at
            FROM provider_raw_frame_archives
            WHERE archive_id = ?
            """,
            (archive_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return True
        raise
    return row is None or bool(str(row["purge_pending_at"] or ""))


def _archive_record(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw_payload = json.loads(str(row["raw_payload_json"]))
    except json.JSONDecodeError as exc:
        raise RawFrameArchiveError(f"frame {row['id']} has invalid JSON payload") from exc
    if not isinstance(raw_payload, dict):
        raw_payload = {"value": raw_payload}
    # Redact again defensively to protect archives created from pre-retention
    # hot rows that were written before raw-frame ingress redaction existed.
    raw_payload = redact_payload(raw_payload, max_str_len=2**63 - 1)
    return {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "id": int(row["id"]),
        "provider": str(row["provider"]),
        "provider_engine": str(row["provider_engine"]),
        "native_session_id": str(row["native_session_id"]),
        "native_turn_id": str(row["native_turn_id"]),
        "sequence": int(row["sequence"]),
        "raw_kind": str(row["raw_kind"]),
        "raw_payload": raw_payload,
        "payload_sha256": _payload_sha256(raw_payload),
        "occurred_at": str(row["occurred_at"]),
        "conversation_id": row["conversation_id"],
        "orchestration_run_id": row["orchestration_run_id"],
        "agent_run_id": row["agent_run_id"],
        "task_id": row["task_id"],
    }


def _archive_bytes(records: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
        for record in records
    )


def _ensure_archive_files(
    archive_path: Path,
    raw_bytes: bytes,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    expected_archive_id = str(manifest["archive_id"])
    if archive_path.exists():
        # A retry can encounter an archive left by a crash after the durable
        # file write.  Validate it without allocating the whole historical
        # payload before deciding whether it can be reused.
        for _ in _iter_archive_records(archive_path, expected_archive_id=expected_archive_id):
            pass
    else:
        _atomic_write_gzip(archive_path, raw_bytes)
    manifest_path = _manifest_path(archive_path)
    if manifest_path.exists():
        existing = _read_archive_manifest(
            manifest_path,
            expected_archive_id=expected_archive_id,
            expected_relative_path=str(manifest["relative_path"]),
        )
        _assert_manifest_matches(
            existing,
            manifest,
            fields=_ARCHIVE_MANIFEST_IMMUTABLE_FIELDS,
            context="existing archive manifest conflicts with archive payload",
        )
        # A crash between the sidecar write and SQLite transaction is retried
        # with the original retention timestamps, not a silently extended TTL.
        return existing
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return manifest


def _read_archive_manifest(
    manifest_path: Path,
    *,
    expected_archive_id: str,
    expected_relative_path: str,
) -> dict[str, Any]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RawFrameArchiveError(f"invalid archive manifest {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise RawFrameArchiveError(f"archive manifest is not an object: {manifest_path}")
    missing = [field for field in _ARCHIVE_MANIFEST_FIELDS if field not in raw]
    if missing:
        raise RawFrameArchiveError(
            f"archive manifest is missing required fields: {', '.join(missing)}"
        )
    try:
        format_version = int(raw["format_version"])
    except (TypeError, ValueError) as exc:
        raise RawFrameArchiveError("archive manifest has an invalid format version") from exc
    if format_version != ARCHIVE_FORMAT_VERSION:
        raise RawFrameArchiveError("archive manifest has an unsupported format version")
    if str(raw["archive_id"]) != expected_archive_id:
        raise RawFrameArchiveError("archive manifest does not match archive payload")
    if str(raw["sha256"]) != expected_archive_id:
        raise RawFrameArchiveError("archive manifest sha256 does not match archive payload")
    if str(raw["relative_path"]) != expected_relative_path:
        raise RawFrameArchiveError("archive manifest has a different relative path")
    try:
        for field in _ARCHIVE_MANIFEST_INTEGER_FIELDS:
            int(raw[field])
    except (TypeError, ValueError) as exc:
        raise RawFrameArchiveError("archive manifest contains invalid numeric metadata") from exc
    if _parse_timestamp(str(raw["created_at"])) is None or _parse_timestamp(
        str(raw["expires_at"])
    ) is None:
        raise RawFrameArchiveError("archive manifest contains invalid retention timestamps")
    return raw


def _assert_manifest_matches(
    actual: Any,
    expected: Any,
    *,
    fields: Iterable[str],
    context: str,
) -> None:
    for field in fields:
        try:
            actual_value = actual[field]
            expected_value = expected[field]
        except (KeyError, IndexError) as exc:
            raise RawFrameArchiveError(f"{context}: missing {field}") from exc
        if field in _ARCHIVE_MANIFEST_INTEGER_FIELDS:
            try:
                matches = int(actual_value) == int(expected_value)
            except (TypeError, ValueError) as exc:
                raise RawFrameArchiveError(f"{context}: invalid {field}") from exc
        else:
            matches = str(actual_value) == str(expected_value)
        if not matches:
            raise RawFrameArchiveError(f"{context}: {field} differs")


def _read_archive_records(
    archive_path: Path,
    *,
    expected_archive_id: str,
) -> list[dict[str, Any]]:
    """Compatibility helper for callers that genuinely need every record.

    Production verification and indexed reads use the streaming helpers
    below.  Keeping this wrapper makes the format helper convenient in small
    tests without making a multi-GB archive a mandatory in-memory object.
    """

    return list(_iter_archive_records(archive_path, expected_archive_id=expected_archive_id))


def _iter_archive_records(
    archive_path: Path,
    *,
    expected_archive_id: str,
) -> Iterable[dict[str, Any]]:
    """Yield archive records while validating the uncompressed payload hash."""

    if not archive_path.is_file():
        raise RawFrameArchiveError(f"archive file is missing: {archive_path}")
    digest = hashlib.sha256()
    try:
        with gzip.open(archive_path, "rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                digest.update(line)
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RawFrameArchiveError(
                        f"invalid JSON at archive line {line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise RawFrameArchiveError(f"archive line {line_number} is not an object")
                yield record
    except (OSError, EOFError) as exc:
        raise RawFrameArchiveError(f"cannot read gzip archive: {archive_path}") from exc
    actual_archive_id = digest.hexdigest()
    if actual_archive_id != expected_archive_id:
        raise RawFrameArchiveError("archive sha256 does not match manifest")


def _read_archive_record_at(
    archive_path: Path,
    *,
    expected_archive_id: str,
    line_number: int,
) -> dict[str, Any]:
    """Return one indexed record while still validating the entire archive."""

    if line_number <= 0:
        raise RawFrameArchiveError(f"indexed archive line {line_number} is out of range")
    selected: dict[str, Any] | None = None
    for current_line, record in enumerate(
        _iter_archive_records(archive_path, expected_archive_id=expected_archive_id), start=1
    ):
        if current_line == line_number:
            selected = record
    if selected is None:
        raise RawFrameArchiveError(f"indexed archive line {line_number} is out of range")
    return selected


def _archive_relative_path(
    archive_date: str,
    min_frame_id: int,
    max_frame_id: int,
    archive_id: str,
) -> str:
    return (
        f"{archive_date}/provider-raw-frames-v{ARCHIVE_FORMAT_VERSION}-"
        f"{min_frame_id}-{max_frame_id}-{archive_id[:16]}.jsonl.gz"
    )


def _archive_path(archive_dir: Path, relative_path: str) -> Path:
    candidate = (archive_dir / relative_path).resolve()
    root = archive_dir.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RawFrameArchiveError("archive path escapes configured archive directory") from exc
    return candidate


def _manifest_path(archive_path: Path) -> Path:
    return archive_path.with_name(f"{archive_path.name}.manifest.json")


def _atomic_write_gzip(path: Path, raw_bytes: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as raw_handle:
            with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as gzip_handle:
                gzip_handle.write(raw_bytes)
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise RawFrameArchiveError(f"cannot fsync compacted SQLite candidate: {path}") from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _payload_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_date(value: str) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise RawFrameArchiveError(f"invalid raw-frame occurred_at: {value!r}")
    return parsed.date().isoformat()


def _chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _policy_from_config(config: Any) -> RuntimeRetentionPolicy:
    retention = config.runtime_retention
    return RuntimeRetentionPolicy(
        archive_dir=retention.archive_dir,
        hot_retention_days=retention.hot_retention_days,
        archive_retention_days=retention.archive_retention_days,
        interval_seconds=retention.interval_seconds,
        batch_size=retention.batch_size,
    )


def main(argv: list[str] | None = None) -> int:
    """Explicit operator CLI for retention and its maintenance-window gate."""

    parser = argparse.ArgumentParser(description="Manage WLCodex provider raw-frame retention")
    parser.add_argument("--config", default="config/wlcodex.toml")
    parser.add_argument(
        "command",
        choices=(
            "dry-run",
            "apply",
            "verify",
            "compact",
            "maintenance-begin",
            "maintenance-probe-native",
            "maintenance-status",
            "maintenance-cancel",
        ),
        help=(
            "apply/compact require a drained maintenance window; compact uses "
            "verified VACUUM INTO and a rollback snapshot"
        ),
    )
    parser.add_argument(
        "--note",
        default="",
        help="operator note recorded when opening a maintenance window",
    )
    args = parser.parse_args(argv)

    from wlcodex.config import load_config
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore

    config = load_config(Path(args.config))
    ledger: Any | None = None
    if args.command in {"dry-run", "verify", "maintenance-status"}:
        database_uri = f"{config.storage.sqlite_path.expanduser().resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(database_uri, uri=True)
        conn.row_factory = sqlite3.Row
    else:
        ledger = Ledger.open(config.storage.sqlite_path)
        conn = ledger._conn
    try:
        # A dry-run/verify must not upgrade a database as a side effect.  Only
        # explicit maintenance mutations are allowed to install schema tables.
        if args.command in {
            "apply",
            "compact",
            "maintenance-begin",
            "maintenance-probe-native",
            "maintenance-cancel",
        }:
            assert ledger is not None
            ledger.migrate()
            apply_retention_schema(conn)
        if args.command == "maintenance-begin":
            status = begin_maintenance_window(conn, operator_note=str(args.note or ""))
            print(json.dumps(status.to_dict(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "maintenance-cancel":
            status = cancel_maintenance_window(conn)
            print(json.dumps(status.to_dict(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "maintenance-probe-native":
            status = maintenance_window_status(conn, include_active_work=False)
            if not status.submissions_frozen:
                raise MaintenanceWindowError(
                    "maintenance window is not active; run maintenance-begin before maintenance-probe-native"
                )
            payload = asyncio.run(_probe_native_turns(conn, config))
            payload["maintenance"] = maintenance_window_status(conn).to_dict()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if int(payload["unknown"]) == 0 and int(payload["active"]) == 0 else 1
        if args.command == "maintenance-status":
            status = maintenance_window_status(conn)
            print(json.dumps(status.to_dict(), ensure_ascii=False, sort_keys=True))
            return 0
        store = RuntimeEventStore(
            conn,
            raw_frame_archive_dir=config.runtime_retention.archive_dir,
        )
        retention = RuntimeRawFrameRetention(store, _policy_from_config(config))
        if args.command == "verify":
            verification = retention.verify()
            print(json.dumps(asdict(verification), ensure_ascii=False, sort_keys=True))
            return 0 if verification.ok else 1
        if args.command == "compact":
            assert_maintenance_window_ready(conn)
            verification = retention.verify()
            if not verification.ok:
                print(json.dumps(asdict(verification), ensure_ascii=False, sort_keys=True))
                return 1
            result = retention.compact()
            print(
                json.dumps(
                    {
                        "compacted": True,
                        "database_path": str(result.database_path),
                        "rollback_snapshot_path": str(result.rollback_snapshot_path),
                        "source_bytes": result.source_bytes,
                        "compacted_bytes": result.compacted_bytes,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "apply":
            assert_maintenance_window_ready(conn)
            result = retention.run(apply=True, require_maintenance=True)
            verification = retention.verify()
            payload = asdict(result)
            payload["verification"] = asdict(verification)
            payload["initial_migration_verified"] = False
            if not verification.ok:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return 1
            mark_initial_retention_migration_verified(conn)
            payload["initial_migration_verified"] = True
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        result = retention.run(apply=False)
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
        return 0
    except (MaintenanceWindowError, RawFrameArchiveError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
