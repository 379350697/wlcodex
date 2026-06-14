from pathlib import Path

from wlcodex.db import Ledger
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
