"""Operator maintenance gate behaviour."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wlcodex.db import Ledger
from wlcodex.maintenance import MaintenanceWindowError, assert_maintenance_window_ready
from wlcodex.relay.store import RelayStore
from wlcodex.runtime_raw_frame_retention import (
    _record_native_probe_connection_failure,
    _record_native_turn_probes,
    initial_retention_migration_verified,
    main as retention_main,
)


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return ledger


def _native_session(ledger: Ledger, native_thread_id: str, *, status: str) -> None:
    from wlcodex.codex_native.session_store import NativeCodexSessionStore

    NativeCodexSessionStore(ledger).get_or_create_session(
        native_thread_id=native_thread_id,
        title="maintenance candidate",
        status=status,
    )


def test_maintenance_begin_freezes_new_relay_submissions_until_cancelled(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)

    status = ledger.begin_maintenance_window(operator_note="release 2026-07-10")

    assert status.submissions_frozen is True
    assert status.active_work == {}
    with pytest.raises(MaintenanceWindowError, match="submissions are temporarily frozen"):
        store.assert_submissions_open()
    assert ledger.cancel_maintenance_window().submissions_frozen is False
    store.assert_submissions_open()


def test_historical_native_sessions_do_not_block_maintenance_drain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _native_session(ledger, "not-loaded", status="notLoaded")
    _native_session(ledger, "idle", status="idle")

    status = ledger.begin_maintenance_window()

    assert status.active_work == {}
    assert assert_maintenance_window_ready(ledger._conn).active_work == {}


def test_blocked_relay_and_native_session_history_do_not_block_drain(tmp_path: Path) -> None:
    from wlcodex.native_agents.session_store import NativeAgentSessionStore

    ledger = _ledger(tmp_path)
    relay = RelayStore(ledger)
    task = relay.create_task(
        title="Blocked historical task",
        prompt="No background worker remains.",
        workspace="/repo",
        provider="codex",
    )
    ledger._conn.execute("UPDATE team_runs SET status = 'blocked' WHERE id = ?", (task.id,))
    NativeAgentSessionStore(ledger).get_or_create_session(
        provider="claude",
        provider_engine="cli",
        native_session_id="stale-native-agent",
        status="running",
    )

    status = ledger.begin_maintenance_window()

    assert status.active_work == {}
    assert assert_maintenance_window_ready(ledger._conn).active_work == {}


def test_native_probe_connection_failure_is_persisted_and_fail_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _native_session(ledger, "unreachable-provider", status="running")
    status = ledger.begin_maintenance_window()

    result = _record_native_probe_connection_failure(
        ledger._conn,
        maintenance_opened_at=status.opened_at,
        candidates=["unreachable-provider"],
        diagnostic="provider_connect_failed:OSError",
    )

    assert result == {"probed": 1, "active": 0, "terminal": 0, "unknown": 1}
    with pytest.raises(MaintenanceWindowError, match="native Codex turn probes pending"):
        assert_maintenance_window_ready(ledger._conn)
    probe = ledger._conn.execute(
        "SELECT verdict, diagnostic FROM runtime_maintenance_native_turn_probes"
    ).fetchone()
    assert tuple(probe) == ("unknown", "provider_connect_failed:OSError")


@pytest.mark.asyncio
async def test_native_turn_probe_is_read_only_and_requires_terminal_verdict(tmp_path: Path) -> None:
    from wlcodex.codex_native.controller import _active_turn_id, _latest_turn, _turns

    ledger = _ledger(tmp_path)
    _native_session(ledger, "stale-running", status="running")
    status = ledger.begin_maintenance_window()
    assert status.active_work == {"native Codex turn probes pending": 1}

    calls: list[tuple[str, bool]] = []

    async def read_session(native_thread_id: str, *, include_turns: bool) -> dict[str, object]:
        calls.append((native_thread_id, include_turns))
        return {
            "thread": {"id": native_thread_id, "status": "idle"},
            "turns": [{"id": "old-turn", "status": "completed"}],
        }

    result = await _record_native_turn_probes(
        ledger._conn,
        maintenance_opened_at=status.opened_at,
        candidates=["stale-running"],
        read_session=read_session,
        active_turn_id=_active_turn_id,
        latest_turn=_latest_turn,
        turns=_turns,
    )

    assert result == {"probed": 1, "active": 0, "terminal": 1, "unknown": 0}
    assert calls == [("stale-running", True)]
    assert assert_maintenance_window_ready(ledger._conn).active_work == {}


@pytest.mark.asyncio
async def test_native_turn_probe_blocks_active_or_unknown_provider_state(tmp_path: Path) -> None:
    from wlcodex.codex_native.controller import _active_turn_id, _latest_turn, _turns

    ledger = _ledger(tmp_path)
    _native_session(ledger, "active-turn", status="running")
    _native_session(ledger, "unknown-turn", status="active")
    status = ledger.begin_maintenance_window()

    async def read_session(native_thread_id: str, *, include_turns: bool) -> dict[str, object]:
        assert include_turns is True
        if native_thread_id == "unknown-turn":
            raise RuntimeError("provider unavailable")
        return {
            "thread": {"id": native_thread_id, "status": "active"},
            "turns": [{"id": "live-turn", "status": "running"}],
        }

    result = await _record_native_turn_probes(
        ledger._conn,
        maintenance_opened_at=status.opened_at,
        candidates=["active-turn", "unknown-turn"],
        read_session=read_session,
        active_turn_id=_active_turn_id,
        latest_turn=_latest_turn,
        turns=_turns,
    )

    assert result == {"probed": 2, "active": 1, "terminal": 0, "unknown": 1}
    blocked = ledger.maintenance_window_status().active_work
    assert blocked == {
        "native Codex turns": 1,
        "native Codex turn probes pending": 1,
    }
    with pytest.raises(MaintenanceWindowError, match="native Codex turns=1"):
        assert_maintenance_window_ready(ledger._conn)


@pytest.mark.asyncio
async def test_native_turn_probe_counts_waiting_turn_as_live(tmp_path: Path) -> None:
    from wlcodex.codex_native.controller import _active_turn_id, _latest_turn, _turns

    ledger = _ledger(tmp_path)
    _native_session(ledger, "waiting-turn", status="waiting_user")
    status = ledger.begin_maintenance_window()

    async def read_session(_native_thread_id: str, *, include_turns: bool) -> dict[str, object]:
        assert include_turns is True
        return {
            "thread": {"id": "waiting-turn", "status": "waiting_user"},
            "turns": [{"id": "waiting-turn-id", "status": "waiting_user"}],
        }

    result = await _record_native_turn_probes(
        ledger._conn,
        maintenance_opened_at=status.opened_at,
        candidates=["waiting-turn"],
        read_session=read_session,
        active_turn_id=_active_turn_id,
        latest_turn=_latest_turn,
        turns=_turns,
    )

    assert result == {"probed": 1, "active": 1, "terminal": 0, "unknown": 0}
    assert ledger.maintenance_window_status().active_work == {"native Codex turns": 1}


def test_maintenance_ready_requires_historical_orchestration_to_drain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger._conn.execute(
        """
        INSERT INTO orchestration_runs (
            conversation_id, goal, status, created_at, updated_at
        ) VALUES (1, 'legacy task', 'needs_user', '2026-07-10T00:00:00+00:00',
                  '2026-07-10T00:00:00+00:00')
        """
    )
    ledger._conn.commit()
    ledger.begin_maintenance_window()

    with pytest.raises(MaintenanceWindowError, match="orchestration_runs=1"):
        assert_maintenance_window_ready(ledger._conn)

    ledger._conn.execute("UPDATE orchestration_runs SET status = 'passed'")
    ledger._conn.commit()
    assert assert_maintenance_window_ready(ledger._conn).active_work == {}


def test_maintenance_drain_counts_completion_claims_and_unconsumed_legacy_queue(
    tmp_path: Path,
) -> None:
    from wlcodex.relay.store import RelayStore
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import (
        AggregateType,
        EventSource,
        EventType,
        RuntimeEvent,
        Visibility,
        now_iso,
    )

    ledger = _ledger(tmp_path)
    relay = RelayStore(ledger)
    task = relay.create_task(
        title="maintenance claim",
        prompt="wait for completion",
        workspace="demo",
        provider="codex",
    )
    # Isolate the drain assertion to the in-flight reconciliation claim; the
    # task itself is historical/terminal.
    ledger._conn.execute(
        "UPDATE team_runs SET status = 'completed' WHERE id = ?",
        (task.id,),
    )
    now = "2026-07-10T00:00:00+00:00"
    ledger._conn.execute(
        """
        INSERT INTO relay_completion_claims (
            team_run_id, round_id, role, runtime_event_id, status,
            artifact_id, claimed_at, applied_at, updated_at
        ) VALUES (?, 1, 'director', 77, 'claimed', NULL, ?, NULL, ?)
        """,
        (task.id, now, now),
    )
    conversation = ledger.create_conversation(
        chat_id=1,
        user_id=1,
        title="legacy queue",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    RuntimeEventStore(ledger._conn).append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_QUEUED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id="queued-maintenance",
        correlation_id="maintenance-queue",
        source=EventSource.CONTROLLER,
        actor="user",
        visibility=Visibility.OPERATOR,
        payload={"goal": "must not start"},
        occurred_at=now_iso(),
        conversation_id=conversation.id,
    ))

    status = ledger.begin_maintenance_window()

    assert status.active_work == {
        "Relay completion claims": 1,
        "legacy queued runs": 1,
    }
    with pytest.raises(MaintenanceWindowError, match="Relay completion claims=1"):
        assert_maintenance_window_ready(ledger._conn)


def test_maintenance_freeze_prevents_legacy_queue_consumer_from_claiming_or_starting(
    tmp_path: Path,
) -> None:
    from wlcodex.config import WorkspaceConfig
    from wlcodex.controller import CommandController
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import (
        AggregateType,
        EventSource,
        EventType,
        RuntimeEvent,
        Visibility,
        now_iso,
    )
    from wlcodex.task_service import TaskService

    class RunnerSpy:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start_chief_engineer(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    ledger = _ledger(tmp_path)
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(
        ledger,
        workspaces=[WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)],
    )
    conversation = ledger.create_conversation(
        chat_id=1,
        user_id=1,
        title="legacy queue",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_QUEUED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id="queued-maintenance",
        correlation_id="maintenance-freeze",
        source=EventSource.CONTROLLER,
        actor="user",
        visibility=Visibility.OPERATOR,
        payload={"goal": "must not start"},
        occurred_at=now_iso(),
        conversation_id=conversation.id,
    ))
    runner = RunnerSpy()
    controller = CommandController(
        task_service=service,
        backend=SimpleNamespace(),
        inspector=SimpleNamespace(),
        ledger=ledger,
        runtime_event_store=store,
        orchestration_runner=runner,
    )
    ledger.begin_maintenance_window()

    asyncio.run(controller.process_queued_runs("demo"))

    events = store.list_by_conversation(conversation.id)
    assert runner.calls == []
    assert EventType.RUN_QUEUED_CLAIMED not in [event.event_type for event in events]
    assert EventType.RUN_QUEUED_CONSUMED not in [event.event_type for event in events]


def test_retention_cli_apply_requires_open_and_drained_maintenance_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wlcodex import config as config_module

    ledger = _ledger(tmp_path)
    db_path = tmp_path / "wlcodex.sqlite3"
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
    argv = ["--config", str(tmp_path / "ignored.toml")]

    assert retention_main([*argv, "apply"]) == 1
    assert "maintenance window is not active" in json.loads(capsys.readouterr().out)["error"]

    assert retention_main([*argv, "maintenance-begin", "--note", "release"]) == 0
    opened = json.loads(capsys.readouterr().out)
    assert opened["submissions_frozen"] is True
    assert opened["ready"] is True

    assert retention_main([*argv, "apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["candidate_frames"] == 0
    assert applied["verification"]["errors"] == []
    assert applied["initial_migration_verified"] is True

    checked = Ledger.open(db_path)
    try:
        assert initial_retention_migration_verified(checked._conn) is True
    finally:
        checked._conn.close()

    assert retention_main([*argv, "maintenance-cancel"]) == 0
    assert json.loads(capsys.readouterr().out)["submissions_frozen"] is False


def test_retention_cli_native_probe_requires_open_window_and_reports_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wlcodex import config as config_module
    from wlcodex import runtime_raw_frame_retention as retention_module

    ledger = _ledger(tmp_path)
    db_path = tmp_path / "wlcodex.sqlite3"
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

    async def probe(_conn, _config):
        return {"probed": 1, "active": 0, "terminal": 1, "unknown": 0}

    monkeypatch.setattr(retention_module, "_probe_native_turns", probe)
    argv = ["--config", str(tmp_path / "ignored.toml")]

    assert retention_main([*argv, "maintenance-probe-native"]) == 1
    assert "maintenance window is not active" in json.loads(capsys.readouterr().out)["error"]

    assert retention_main([*argv, "maintenance-begin"]) == 0
    capsys.readouterr()
    assert retention_main([*argv, "maintenance-probe-native"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["terminal"] == 1
    assert payload["maintenance"]["submissions_frozen"] is True
