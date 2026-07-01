from pathlib import Path

from wlcodex.db import Ledger
from wlcodex.relay.events import RelayEventBus
from wlcodex.relay.models import HandoffPacket
from wlcodex.relay.store import RelayStore


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return ledger


def test_create_task_reuses_team_run_and_creates_fixed_role_jobs(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)

    task = store.create_task(
        title="Build relay",
        prompt="Implement relay workspace",
        workspace="/repo",
        provider="codex",
    )

    team_run = ledger.get_team_run(task.id)
    assert team_run is not None
    assert team_run.id == task.id
    assert team_run.route == "relay"
    assert team_run.status == "running"

    jobs = ledger.list_team_agent_jobs(task.id)
    assert [job.role for job in jobs] == [
        "director",
        "architect",
        "implementer",
        "tester",
        "auditor",
    ]
    assert [job.status for job in jobs] == [
        "queued",
        "idle",
        "idle",
        "idle",
        "idle",
    ]


def test_list_tasks_excludes_non_relay_team_runs(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)
    relay_task = store.create_task(
        title="Relay",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )
    ledger.create_team_run(0, None, "Ordinary team", route="staged_auto")

    summaries = store.list_tasks()

    assert [summary.task_id for summary in summaries] == [relay_task.id]


def test_relay_stream_events_are_persisted_with_task_sequence(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)
    task = store.create_task(
        title="Relay stream",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    bus = RelayEventBus(store)

    first = bus.emit(task.id, "task.created", role="director")
    second = bus.emit(task.id, "role.status", role="director", payload={"status": "streaming"})
    third = bus.emit(
        task.id,
        "routing.decision",
        role="director",
        payload={"route": "director_only"},
    )

    assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]

    restarted_bus = RelayEventBus(RelayStore(ledger))
    replayed = restarted_bus.list_events(task.id, after=1)

    assert [event.sequence for event in replayed] == [2, 3]
    assert [event.event_type for event in replayed] == ["role.status", "routing.decision"]
    assert replayed[0].payload == {"status": "streaming"}

    fourth = restarted_bus.emit(
        task.id,
        "role.status",
        role="director",
        payload={"status": "completed"},
    )
    replayed_all = restarted_bus.list_events(task.id)

    assert fourth.sequence == 4
    assert [event.sequence for event in replayed_all] == [1, 2, 3, 4]


def test_relay_runtime_delta_stream_event_persists_reference_not_delta_text(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)
    task = store.create_task(
        title="Relay stream",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    bus = RelayEventBus(store)

    event = bus.emit(
        task.id,
        "role.output_delta",
        role="director",
        payload={
            "runtime_event_id": 42,
            "agent_run_id": 101,
            "round_id": 1,
            "delta": "visible text should stay in runtime_events",
        },
    )

    row = ledger._conn.execute(
        "SELECT runtime_event_id, payload_json FROM relay_stream_events WHERE task_id = ?",
        (task.id,),
    ).fetchone()
    replayed = RelayEventBus(RelayStore(ledger)).list_events(task.id)

    assert event.sequence == 1
    assert row["runtime_event_id"] == 42
    assert "visible text should stay in runtime_events" not in row["payload_json"]
    assert replayed[0].payload == {
        "runtime_event_id": 42,
        "agent_run_id": 101,
        "round_id": 1,
    }


def test_relay_native_stream_event_replay_keeps_runtime_payload_out_of_main_stream(
    tmp_path: Path,
) -> None:
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
    store = RelayStore(ledger)
    task = store.create_task(
        title="Relay stream",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    runtime_event = RuntimeEventStore(ledger._conn).append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_TEXT_DELTA,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id="101",
            correlation_id="corr-101",
            source=EventSource.CODEX,
            actor="codex",
            visibility=Visibility.USER,
            payload={
                "delta": "visible transcript text",
                "debug_blob": "large runtime debug payload should stay behind details API",
            },
            occurred_at=now_iso(),
            agent_run_id=101,
        )
    )
    bus = RelayEventBus(store)

    bus.emit(
        task.id,
        "role.native_event",
        role="director",
        payload={
            "runtime_event_id": runtime_event.id,
            "agent_run_id": 101,
            "kind": "text_delta",
            "payload": {
                "delta": "visible transcript text",
                "debug_blob": "large runtime debug payload should stay behind details API",
            },
            "native_event": {"id": runtime_event.id, "payload": runtime_event.payload},
        },
    )

    replayed = RelayEventBus(RelayStore(ledger)).list_events(task.id)

    assert replayed[0].payload["runtime_event_id"] == runtime_event.id
    assert replayed[0].payload["kind"] == "text_delta"
    assert replayed[0].payload["delta"] == "visible transcript text"
    assert "native_event" not in replayed[0].payload
    assert "payload" not in replayed[0].payload
    assert "large runtime debug payload should stay behind details API" not in str(
        replayed[0].payload
    )


def test_backfill_does_not_refresh_task_activity_order(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)
    older = store.create_task(
        title="Older relay",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    newer = store.create_task(
        title="Newer relay",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    older_updated_at = "2026-01-01T00:00:00+00:00"
    newer_updated_at = "2026-01-02T00:00:00+00:00"
    ledger._conn.execute(
        "UPDATE team_runs SET updated_at = ? WHERE id = ?",
        (older_updated_at, older.id),
    )
    ledger._conn.execute(
        "UPDATE team_runs SET updated_at = ? WHERE id = ?",
        (newer_updated_at, newer.id),
    )
    ledger._conn.commit()

    restarted_store = RelayStore(ledger)
    summaries = restarted_store.list_tasks()
    rows = ledger._conn.execute(
        "SELECT id, updated_at FROM team_runs WHERE id IN (?, ?) ORDER BY id ASC",
        (older.id, newer.id),
    ).fetchall()

    assert [summary.task_id for summary in summaries[:2]] == [newer.id, older.id]
    assert [str(row["updated_at"]) for row in rows] == [older_updated_at, newer_updated_at]


def test_list_tasks_orders_by_relay_artifact_activity(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)
    older = store.create_task(
        title="Older relay",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    newer = store.create_task(
        title="Newer relay",
        prompt="Prompt",
        workspace="/repo",
        provider="codex",
    )
    ledger._conn.execute(
        "UPDATE team_artifacts SET created_at = ? WHERE team_run_id = ?",
        ("2026-01-01T00:00:00+00:00", older.id),
    )
    ledger._conn.execute(
        "UPDATE team_artifacts SET created_at = ? WHERE team_run_id = ?",
        ("2026-01-02T00:00:00+00:00", newer.id),
    )
    ledger._conn.execute(
        "UPDATE team_runs SET updated_at = ? WHERE id = ?",
        ("2026-01-03T00:00:00+00:00", older.id),
    )
    ledger._conn.execute(
        "UPDATE team_runs SET updated_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", newer.id),
    )
    ledger._conn.commit()

    summaries = store.list_tasks()

    assert [summary.task_id for summary in summaries[:2]] == [newer.id, older.id]
    assert summaries[0].last_activity_at == "2026-01-02T00:00:00+00:00"


def test_handoff_packets_are_persisted_and_returned_in_detail(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    store = RelayStore(ledger)
    task = store.create_task(
        title="Relay",
        prompt="Prompt",
        workspace="/repo",
        provider="claude",
    )

    handoff = HandoffPacket(
        from_role="architect",
        to_role="implementer",
        summary="Architecture ready",
        confirmed_facts=["Use team tables"],
        open_questions=[],
        evidence_refs=["docs/spec.md"],
        next_action="Implement",
    )
    artifact = store.save_handoff_packet(
        task.id,
        from_role="architect",
        to_role="implementer",
        packet=handoff,
    )
    detail = store.get_task_detail(task.id)

    assert artifact.artifact_type == "handoff_packet"
    assert detail.latest_handoff is not None
    assert detail.latest_handoff.summary == "Architecture ready"
