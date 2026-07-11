"""Behavioural coverage for provider raw-frame retention."""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import wlcodex.runtime_raw_frame_retention as retention_module
from wlcodex.db import Ledger
from wlcodex.maintenance import MaintenanceWindowError
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_raw_frame_retention import (
    NativeTurnGuardObservation,
    RawFrameArchiveError,
    RuntimeRawFrameRetention,
    RuntimeRetentionPolicy,
    initial_retention_migration_verified,
    mark_initial_retention_migration_verified,
)


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> RuntimeEventStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return RuntimeEventStore(
        ledger._conn,
        raw_frame_archive_dir=tmp_path / "provider-raw-frame-archives",
    )


def _append_frame(
    store: RuntimeEventStore,
    *,
    occurred_at: datetime,
    sequence: int = 1,
    agent_run_id: int | None = None,
    payload: dict[str, object] | None = None,
):
    return store.append_provider_raw_frame(
        provider="claude",
        provider_engine="sdk",
        native_session_id="session-1",
        native_turn_id="turn-1",
        sequence=sequence,
        raw_kind="sdk.message",
        raw_payload=payload or {"delta": "hello"},
        occurred_at=occurred_at.isoformat(),
        agent_run_id=agent_run_id,
    )


def _retention(store: RuntimeEventStore, tmp_path: Path, *, now: datetime = NOW):
    return RuntimeRawFrameRetention(
        store,
        RuntimeRetentionPolicy(archive_dir=tmp_path / "provider-raw-frame-archives"),
        now=lambda: now,
    )


def test_raw_frame_retention_scan_index_is_migrated(tmp_path: Path) -> None:
    store = _store(tmp_path)

    indexes = {
        str(row[1])
        for row in store._conn.execute("PRAGMA index_list('provider_raw_frames')").fetchall()
    }

    assert "idx_provider_raw_frames_retention_scan" in indexes


def test_raw_frame_is_redacted_before_it_reaches_sqlite(tmp_path: Path) -> None:
    store = _store(tmp_path)

    frame = _append_frame(
        store,
        occurred_at=NOW,
        payload={"token": "top-secret", "nested": {"api_key": "also-secret"}},
    )

    loaded = store.get_provider_raw_frame(frame.id)
    assert loaded.raw_payload == {
        "token": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]"},
    }
    stored = store._conn.execute(
        "SELECT raw_payload_json FROM provider_raw_frames WHERE id = ?", (frame.id,)
    ).fetchone()
    assert "top-secret" not in str(stored["raw_payload_json"])
    assert "also-secret" not in str(stored["raw_payload_json"])


def test_nested_raw_frame_secrets_are_redacted_in_hot_store_and_archive(tmp_path: Path) -> None:
    store = _store(tmp_path)
    frame = _append_frame(
        store,
        occurred_at=NOW - timedelta(days=8),
        payload={"events": [{"token": "hot-list-secret", "text": "api_key=archive-secret"}]},
    )

    stored = store._conn.execute(
        "SELECT raw_payload_json FROM provider_raw_frames WHERE id = ?", (frame.id,)
    ).fetchone()
    assert "hot-list-secret" not in str(stored["raw_payload_json"])
    assert "archive-secret" not in str(stored["raw_payload_json"])

    assert _retention(store, tmp_path).run(apply=True).archived_frames == 1
    archived = store.get_provider_raw_frame(frame.id).raw_payload
    serialized = json.dumps(archived, ensure_ascii=False)
    assert "hot-list-secret" not in serialized
    assert "archive-secret" not in serialized
    assert archived["events"][0]["token"] == "[REDACTED]"


def test_apply_archives_expired_frame_and_lookup_falls_back_to_gzip(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    frame = _append_frame(
        store,
        occurred_at=NOW - timedelta(days=8),
        payload={"delta": "old", "token": "never-in-archive"},
    )

    result = _retention(store, tmp_path).run(apply=True)

    assert result.archived_frames == 1
    assert store._conn.execute(
        "SELECT 1 FROM provider_raw_frames WHERE id = ?", (frame.id,)
    ).fetchone() is None
    assert store.get_provider_raw_frame(frame.id).raw_payload == {
        "delta": "old",
        "token": "[REDACTED]",
    }
    manifest = store._conn.execute(
        "SELECT relative_path, frame_count, sha256 FROM provider_raw_frame_archives"
    ).fetchone()
    assert manifest is not None
    assert int(manifest["frame_count"]) == 1
    archive_path = tmp_path / "provider-raw-frame-archives" / str(manifest["relative_path"])
    assert archive_path.suffix == ".gz"
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        record = json.loads(handle.readline())
    assert record["id"] == frame.id
    assert record["raw_payload"]["token"] == "[REDACTED]"
    assert record["payload_sha256"]


def test_dry_run_never_writes_archive_or_deletes_hot_frame(tmp_path: Path) -> None:
    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))

    result = _retention(store, tmp_path).run(apply=False)

    assert result.candidate_frames == 1
    assert result.archived_frames == 0
    assert store.get_provider_raw_frame(frame.id).id == frame.id
    assert not (tmp_path / "provider-raw-frame-archives").exists()


def test_configured_scheduler_waits_for_verified_maintenance_migration(
    tmp_path: Path,
) -> None:
    """A true config flag cannot turn the first backlog into background work."""

    from wlcodex.main import _run_configured_runtime_retention_once

    store = _store(tmp_path)
    database_path = tmp_path / "wlcodex.sqlite3"
    first = _append_frame(store, occurred_at=NOW - timedelta(days=8))
    config = SimpleNamespace(
        storage=SimpleNamespace(sqlite_path=database_path),
        runtime_retention=SimpleNamespace(
            archive_dir=tmp_path / "provider-raw-frame-archives",
            hot_retention_days=7,
            archive_retention_days=90,
            interval_seconds=6 * 60 * 60,
            batch_size=250,
        ),
    )
    store._conn.close()

    skipped = _run_configured_runtime_retention_once(config)

    assert skipped == {"applied": False, "reason": "initial_migration_not_verified"}
    ledger = Ledger.open(database_path)
    ledger.migrate()
    store = RuntimeEventStore(
        ledger._conn,
        raw_frame_archive_dir=tmp_path / "provider-raw-frame-archives",
    )
    assert store.get_provider_raw_frame(first.id).id == first.id
    assert initial_retention_migration_verified(ledger._conn) is False

    ledger.begin_maintenance_window(operator_note="first verified archive")
    retention = _retention(store, tmp_path)
    assert retention.run(apply=True).archived_frames == 1
    assert retention.verify().ok is True
    mark_initial_retention_migration_verified(ledger._conn)
    assert initial_retention_migration_verified(ledger._conn) is True

    paused = _run_configured_runtime_retention_once(config)
    assert paused == {"applied": False, "reason": "maintenance_window_active"}

    ledger.cancel_maintenance_window()
    second = _append_frame(
        store,
        occurred_at=NOW - timedelta(days=8),
        sequence=2,
    )
    ledger._conn.close()

    applied = _run_configured_runtime_retention_once(config)

    assert applied.archived_frames == 1
    reopened = Ledger.open(database_path)
    try:
        reopened_store = RuntimeEventStore(
            reopened._conn,
            raw_frame_archive_dir=tmp_path / "provider-raw-frame-archives",
        )
        assert reopened_store.get_provider_raw_frame(second.id).id == second.id
    finally:
        reopened._conn.close()


def test_dry_run_cli_does_not_run_a_database_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inspection must not make initial maintenance migration implicit."""

    from wlcodex import config as config_module
    from wlcodex import runtime_raw_frame_retention as retention_module

    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()
    ledger._conn.close()
    config = SimpleNamespace(
        storage=SimpleNamespace(sqlite_path=db_path),
        runtime_retention=SimpleNamespace(
            archive_dir=tmp_path / "provider-raw-frame-archives",
            hot_retention_days=7,
            archive_retention_days=90,
            interval_seconds=6 * 60 * 60,
            batch_size=250,
        ),
    )
    monkeypatch.setattr(config_module, "load_config", lambda _path: config)

    def _unexpected_migrate(_ledger: Ledger) -> None:
        raise AssertionError("dry-run must not migrate SQLite")

    monkeypatch.setattr(Ledger, "migrate", _unexpected_migrate)

    assert retention_module.main(["--config", str(tmp_path / "ignored.toml"), "dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["candidate_frames"] == 0


def test_compact_uses_a_verified_copy_and_preserves_a_rollback_snapshot(
    tmp_path: Path,
) -> None:
    from wlcodex.runtime_raw_frame_retention import compact_sqlite_database

    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    frame = _append_frame(store, occurred_at=NOW)
    ledger.begin_maintenance_window(operator_note="test compaction")

    result = compact_sqlite_database(ledger._conn, database_path=db_path)

    assert result.database_path == db_path
    assert result.rollback_snapshot_path.is_file()
    compacted = sqlite3.connect(db_path)
    compacted.row_factory = sqlite3.Row
    try:
        assert compacted.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert RuntimeEventStore(compacted).get_provider_raw_frame(frame.id).id == frame.id
    finally:
        compacted.close()


def test_compact_leaves_source_untouched_when_candidate_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wlcodex import runtime_raw_frame_retention as retention_module

    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()
    ledger.begin_maintenance_window(operator_note="test compaction")
    original_bytes = db_path.read_bytes()
    monkeypatch.setattr(retention_module, "_sqlite_integrity_check", lambda _path: "bad")

    with pytest.raises(RawFrameArchiveError, match="integrity_check"):
        retention_module.compact_sqlite_database(ledger._conn, database_path=db_path)

    assert db_path.read_bytes() == original_bytes
    ledger._conn.close()


def test_compact_refuses_without_a_frozen_maintenance_window(tmp_path: Path) -> None:
    from wlcodex.runtime_raw_frame_retention import compact_sqlite_database

    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()

    with pytest.raises(MaintenanceWindowError, match="maintenance window is not active"):
        compact_sqlite_database(ledger._conn, database_path=db_path)

    ledger._conn.close()


def test_compact_run_refuses_before_archiving_without_maintenance_window(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))

    with pytest.raises(MaintenanceWindowError, match="maintenance window is not active"):
        _retention(store, tmp_path).run(apply=True, compact=True)

    assert store._conn.execute(
        "SELECT 1 FROM provider_raw_frames WHERE id = ?", (frame.id,)
    ).fetchone()


def test_compact_refuses_while_active_work_remains(tmp_path: Path) -> None:
    from wlcodex.runtime_raw_frame_retention import compact_sqlite_database

    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger.open(db_path)
    ledger.migrate()
    ledger._conn.execute(
        """
        INSERT INTO agent_runs (
            id, conversation_id, agent, role, status, prompt_packet_summary,
            created_at, updated_at
        ) VALUES (99, 1, 'claude', 'implementer', 'running', '', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    ledger._conn.commit()

    with pytest.raises(RawFrameArchiveError, match="active work"):
        compact_sqlite_database(ledger._conn, database_path=db_path)

    assert ledger._conn.execute("SELECT 1 FROM agent_runs WHERE id = 99").fetchone()
    ledger._conn.close()


def test_active_agent_run_frame_is_not_archived(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store._conn.execute(
        """
        INSERT INTO agent_runs (
            id, conversation_id, agent, role, status, prompt_packet_summary,
            created_at, updated_at
        ) VALUES (41, 1, 'claude', 'implementer', 'running', '', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    store._conn.commit()
    frame = _append_frame(
        store,
        occurred_at=NOW - timedelta(days=8),
        agent_run_id=41,
    )

    result = _retention(store, tmp_path).run(apply=True)

    assert result.skipped_active_frames == 1
    assert result.archived_frames == 0
    assert store.get_provider_raw_frame(frame.id).id == frame.id


def test_queued_agent_run_does_not_block_retention_of_an_old_completed_turn(
    tmp_path: Path,
) -> None:
    """Queued is a dispatch state, not proof that this raw frame is active."""

    store = _store(tmp_path)
    store._conn.execute(
        """
        INSERT INTO agent_runs (
            id, conversation_id, agent, role, status, prompt_packet_summary,
            created_at, updated_at
        ) VALUES (42, 1, 'claude', 'implementer', 'queued', '', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    store._conn.commit()
    frame = _append_frame(
        store,
        occurred_at=NOW - timedelta(days=8),
        agent_run_id=42,
    )

    result = _retention(store, tmp_path).run(apply=True)

    assert result.archived_frames == 1
    assert store.get_provider_raw_frame(frame.id).id == frame.id


def test_active_native_turn_does_not_pin_prior_turns_in_the_same_session(
    tmp_path: Path,
) -> None:
    """The active-turn guard must not retain an entire long-lived session."""

    store = _store(tmp_path)
    store._conn.execute(
        """
        INSERT INTO native_codex_sessions (
            native_thread_id, agent_run_id, conversation_id, status, last_turn_id,
            created_at, updated_at
        ) VALUES ('thread-1', 1, 1, 'running', 'active-turn', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    store._conn.commit()
    historical = store.append_provider_raw_frame(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        native_turn_id="historical-turn",
        sequence=1,
        raw_kind="codex.event",
        raw_payload={"delta": "historical"},
        occurred_at=(NOW - timedelta(days=8)).isoformat(),
    )
    active = store.append_provider_raw_frame(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        native_turn_id="active-turn",
        sequence=1,
        raw_kind="codex.event",
        raw_payload={"delta": "active"},
        occurred_at=(NOW - timedelta(days=8)).isoformat(),
    )

    result = _retention(store, tmp_path).run(apply=True)

    assert result.archived_frames == 1
    assert result.skipped_active_frames == 1
    assert store.get_provider_raw_frame(historical.id).raw_payload == {
        "delta": "historical"
    }
    assert store._conn.execute(
        "SELECT 1 FROM provider_raw_frames WHERE id = ?", (active.id,)
    ).fetchone()


def test_provider_terminal_observation_releases_only_the_observed_stale_turn(
    tmp_path: Path,
) -> None:
    """Periodic retention must prefer fresh provider truth over stale cache."""

    store = _store(tmp_path)
    store._conn.execute(
        """
        INSERT INTO native_codex_sessions (
            native_thread_id, agent_run_id, conversation_id, status, last_turn_id,
            created_at, updated_at
        ) VALUES ('thread-terminal', 1, 1, 'running', 'old-turn', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    store._conn.commit()
    frame = store.append_provider_raw_frame(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-terminal",
        native_turn_id="old-turn",
        sequence=1,
        raw_kind="codex.event",
        raw_payload={"delta": "complete"},
        occurred_at=(NOW - timedelta(days=8)).isoformat(),
    )
    retention = RuntimeRawFrameRetention(
        store,
        RuntimeRetentionPolicy(archive_dir=tmp_path / "provider-raw-frame-archives"),
        now=lambda: NOW,
        native_turn_observer=lambda: NativeTurnGuardObservation(
            observed_codex_turns=(("thread-terminal", "old-turn"),),
        ),
    )

    assert retention.run(apply=True).archived_frames == 1
    assert store.get_provider_raw_frame(frame.id).id == frame.id


def test_unverified_provider_observation_keeps_native_raw_frames_hot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store._conn.execute(
        """
        INSERT INTO native_codex_sessions (
            native_thread_id, agent_run_id, conversation_id, status, last_turn_id,
            created_at, updated_at
        ) VALUES ('thread-unknown', 1, 1, 'running', 'unknown-turn', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    store._conn.commit()
    frame = store.append_provider_raw_frame(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-unknown",
        native_turn_id="unknown-turn",
        sequence=1,
        raw_kind="codex.event",
        raw_payload={"delta": "waiting"},
        occurred_at=(NOW - timedelta(days=8)).isoformat(),
    )
    retention = RuntimeRawFrameRetention(
        store,
        RuntimeRetentionPolicy(archive_dir=tmp_path / "provider-raw-frame-archives"),
        now=lambda: NOW,
        native_turn_observer=lambda: NativeTurnGuardObservation(
            unverified_codex_sessions=("thread-unknown",),
        ),
    )

    assert retention.run(apply=True).skipped_active_frames == 1
    assert store._conn.execute(
        "SELECT 1 FROM provider_raw_frames WHERE id = ?", (frame.id,)
    ).fetchone()


def test_sequence_cursor_survives_hot_frame_archival(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _append_frame(store, occurred_at=NOW - timedelta(days=8), sequence=1)

    _retention(store, tmp_path).run(apply=True)

    assert store.next_provider_raw_frame_sequence(
        provider="claude",
        provider_engine="sdk",
        native_session_id="session-1",
        native_turn_id="turn-1",
    ) == 2


def test_legacy_sequence_cursor_survives_archive_expiry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Simulate a pre-cursor frame: normal insertion would populate the cursor,
    # but a historical database can legitimately lack it.
    store._conn.execute(
        """
        INSERT INTO provider_raw_frames (
            provider, provider_engine, native_session_id, native_turn_id,
            sequence, raw_kind, raw_payload_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "claude",
            "sdk",
            "legacy-session",
            "legacy-turn",
            7,
            "sdk.message",
            json.dumps({"delta": "legacy"}),
            (NOW - timedelta(days=8)).isoformat(),
        ),
    )
    store._conn.commit()

    _retention(store, tmp_path).run(apply=True)
    cursor = store._conn.execute(
        """
        SELECT last_sequence FROM provider_raw_frame_sequence_cursors
        WHERE provider = ? AND provider_engine = ?
          AND native_session_id = ? AND native_turn_id = ?
        """,
        ("claude", "sdk", "legacy-session", "legacy-turn"),
    ).fetchone()
    assert cursor is not None and int(cursor["last_sequence"]) == 7

    _retention(store, tmp_path, now=NOW + timedelta(days=91)).run(apply=True)

    assert store.next_provider_raw_frame_sequence(
        provider="claude",
        provider_engine="sdk",
        native_session_id="legacy-session",
        native_turn_id="legacy-turn",
    ) == 8


def test_apply_paginates_a_large_backlog_without_skipping_rows_after_deletion(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    frames = [
        _append_frame(
            store,
            occurred_at=NOW - timedelta(days=8),
            sequence=sequence,
        )
        for sequence in range(1, 5)
    ]
    retention = RuntimeRawFrameRetention(
        store,
        RuntimeRetentionPolicy(
            archive_dir=tmp_path / "provider-raw-frame-archives",
            batch_size=1,
        ),
        now=lambda: NOW,
    )

    result = retention.run(apply=True)

    assert result.candidate_frames == 4
    assert result.archived_frames == 4
    assert [store.get_provider_raw_frame(frame.id).id for frame in frames] == [
        frame.id for frame in frames
    ]


def test_expired_archive_is_removed_after_archive_retention_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))
    _retention(store, tmp_path).run(apply=True)

    future = NOW + timedelta(days=91)
    result = _retention(store, tmp_path, now=future).run(apply=True)

    assert result.purged_archives == 1
    assert store._conn.execute(
        "SELECT 1 FROM provider_raw_frame_archives"
    ).fetchone() is None
    try:
        store.get_provider_raw_frame(frame.id)
    except KeyError:
        pass
    else:
        raise AssertionError("expired raw archive must no longer be retrievable")


def test_expired_archive_file_removal_failure_keeps_manifest_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))
    _retention(store, tmp_path).run(apply=True)
    archive_path = tmp_path / "provider-raw-frame-archives" / str(
        store._conn.execute(
            "SELECT relative_path FROM provider_raw_frame_archives"
        ).fetchone()["relative_path"]
    )
    original_unlink = Path.unlink

    def fail_manifest_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self == Path(f"{archive_path}.manifest.json"):
            raise OSError("simulated archive filesystem failure")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_manifest_unlink)
    future = NOW + timedelta(days=91)

    result = _retention(store, tmp_path, now=future).run(apply=True)

    assert result.purged_archives == 0
    assert store._conn.execute(
        "SELECT 1 FROM provider_raw_frame_archives"
    ).fetchone() is not None
    with pytest.raises(KeyError):
        store.get_provider_raw_frame(frame.id)

    monkeypatch.setattr(Path, "unlink", original_unlink)
    retried = _retention(store, tmp_path, now=future).run(apply=True)
    assert retried.purged_archives == 1
    assert store._conn.execute(
        "SELECT 1 FROM provider_raw_frame_archives"
    ).fetchone() is None


def test_archived_frame_read_race_with_purge_is_consistently_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))
    _retention(store, tmp_path).run(apply=True)
    archive_id = str(
        store._conn.execute(
            "SELECT archive_id FROM provider_raw_frame_archives"
        ).fetchone()["archive_id"]
    )

    def retire_during_read(*_args, **_kwargs):
        store._conn.execute(
            "UPDATE provider_raw_frame_archives SET purge_pending_at = ? WHERE archive_id = ?",
            (NOW.isoformat(), archive_id),
        )
        store._conn.commit()
        raise RawFrameArchiveError("archive file disappeared during read")

    monkeypatch.setattr(retention_module, "_read_archive_record_at", retire_during_read)

    with pytest.raises(KeyError):
        store.get_provider_raw_frame(frame.id)


def test_verify_streams_archive_records_without_list_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _append_frame(store, occurred_at=NOW - timedelta(days=8))
    retention = _retention(store, tmp_path)
    retention.run(apply=True)

    def list_materialization_is_forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verify must stream archive records")

    monkeypatch.setattr(retention_module, "_read_archive_records", list_materialization_is_forbidden)

    assert retention.verify().ok is True


def test_verify_rejects_a_sidecar_manifest_with_a_wrong_payload_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))
    retention = _retention(store, tmp_path)
    retention.run(apply=True)
    relative_path = str(
        store._conn.execute(
            "SELECT relative_path FROM provider_raw_frame_archives"
        ).fetchone()["relative_path"]
    )
    sidecar = tmp_path / "provider-raw-frame-archives" / f"{relative_path}.manifest.json"
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    manifest["sha256"] = "tampered"
    sidecar.write_text(json.dumps(manifest), encoding="utf-8")

    verification = retention.verify()

    assert verification.ok is False
    assert verification.errors
    with pytest.raises(RawFrameArchiveError, match="manifest sha256"):
        store.get_provider_raw_frame(frame.id)


def test_apply_refuses_to_delete_a_frame_indexed_by_another_archive(tmp_path: Path) -> None:
    """A retry must never turn a pre-existing index conflict into data loss."""

    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))
    store._conn.execute(
        """
        INSERT INTO provider_raw_frame_archives (
            archive_id, format_version, archive_date, relative_path, sha256,
            frame_count, min_frame_id, max_frame_id, min_occurred_at,
            max_occurred_at, created_at, expires_at
        ) VALUES (?, 1, '2026-07-01', 'orphan.jsonl.gz', ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        (
            "other-archive",
            "other-archive",
            frame.id,
            frame.id,
            frame.occurred_at,
            frame.occurred_at,
            NOW.isoformat(),
            (NOW + timedelta(days=90)).isoformat(),
        ),
    )
    store._conn.execute(
        """
        INSERT INTO provider_raw_frame_archive_index (
            frame_id, archive_id, archive_line, provider, provider_engine,
            native_session_id, native_turn_id, sequence
        ) VALUES (?, 'other-archive', 1, 'claude', 'sdk', 'session-1', 'turn-1', 1)
        """,
        (frame.id,),
    )
    store._conn.commit()

    with pytest.raises(RawFrameArchiveError, match="different archive"):
        _retention(store, tmp_path).run(apply=True)

    assert store.get_provider_raw_frame(frame.id).id == frame.id


def test_apply_recovers_after_sidecar_write_crash_without_losing_hot_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry reuses a durable sidecar and only then indexes/deletes its row."""

    from wlcodex import runtime_raw_frame_retention as retention_module

    store = _store(tmp_path)
    frame = _append_frame(store, occurred_at=NOW - timedelta(days=8))
    retention = _retention(store, tmp_path)
    original_ensure = retention_module._ensure_archive_files

    def _crash_after_sidecar(*args: object, **kwargs: object) -> dict[str, object]:
        original_ensure(*args, **kwargs)
        raise RuntimeError("simulated crash after sidecar durability before SQLite transaction")

    monkeypatch.setattr(retention_module, "_ensure_archive_files", _crash_after_sidecar)
    with pytest.raises(RuntimeError, match="simulated crash"):
        retention.run(apply=True)

    # The only durable output at this point is the sidecar pair.  SQLite must
    # still own the hot row, so there is no data-loss window to recover from.
    assert store.get_provider_raw_frame(frame.id).id == frame.id
    assert store._conn.execute(
        "SELECT COUNT(*) FROM provider_raw_frame_archives"
    ).fetchone()[0] == 0
    assert len(list((tmp_path / "provider-raw-frame-archives").rglob("*.jsonl.gz"))) == 1

    monkeypatch.setattr(retention_module, "_ensure_archive_files", original_ensure)
    result = retention.run(apply=True)

    assert result.archived_frames == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM provider_raw_frame_archives"
    ).fetchone()[0] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM provider_raw_frame_archive_index WHERE frame_id = ?",
        (frame.id,),
    ).fetchone()[0] == 1
    assert retention.verify().ok is True
    assert store.get_provider_raw_frame(frame.id).id == frame.id


def test_apply_keeps_hot_frame_when_turn_becomes_active_during_archive_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final write-lease check wins over an earlier inactive snapshot."""

    store = _store(tmp_path)
    store._conn.execute(
        """
        INSERT INTO agent_runs (
            id, conversation_id, agent, role, status, prompt_packet_summary,
            created_at, updated_at
        ) VALUES (41, 1, 'claude', 'implementer', 'completed', '', ?, ?)
        """,
        (NOW.isoformat(), NOW.isoformat()),
    )
    store._conn.commit()
    frame = _append_frame(
        store,
        occurred_at=NOW - timedelta(days=8),
        agent_run_id=41,
    )
    retention = _retention(store, tmp_path)
    active_states = iter((set(), set(), {41}))
    monkeypatch.setattr(
        retention,
        "_active_agent_run_ids",
        lambda _ids: next(active_states),
    )
    monkeypatch.setattr(retention, "_active_native_turn_guards", lambda: (set(), set()))

    result = retention.run(apply=True)

    assert result.archived_frames == 0
    assert result.skipped_active_frames == 1
    assert store.get_provider_raw_frame(frame.id).id == frame.id
