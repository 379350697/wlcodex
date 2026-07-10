import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.models import NativeAgentCapabilities, NativeAgentControlResult
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore


class _Provider:
    provider = "claude"
    provider_engine = "test"

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(can_start_session=True, can_continue_session=True)

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        del cwd, prompt, kwargs
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="native-test",
            agent_run_id=101,
            turn_id="turn-test",
            active_turn_id="turn-test",
            turn_running=True,
            status="started",
        )

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs: Any):
        del prompt, kwargs
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=101,
            turn_id="turn-test",
            active_turn_id="turn-test",
            turn_running=True,
            status="continued",
        )


def _service(tmp_path: Path) -> RelayService:
    ledger = Ledger.open(tmp_path / "relay.sqlite3")
    ledger.migrate()
    return RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([_Provider()]),
        default_provider="claude",
    )


def _routing_output(
    *,
    required_roles: list[str],
    acceptance_criteria: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "status": "passed",
            "reason": "route task",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "",
            "summary": "route accepted",
            "evidence_refs": [],
            "open_questions": [],
            "next_action": "continue",
            "complexity": "medium",
            "risk": "medium",
            "route": "core_relay",
            "required_roles": required_roles,
            "acceptance_criteria": acceptance_criteria or ["observable result"],
            "stop_conditions": [],
            "requires_user_approval": False,
        },
        ensure_ascii=False,
    )


def _envelope(
    *,
    role: str,
    artifact_type: str,
    handoff_to: str = "",
    evidence_refs: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "status": "passed",
            "reason": "done",
            "role": role,
            "artifact_type": artifact_type,
            "handoff_to": handoff_to,
            "summary": f"{role} done",
            "evidence_refs": list(evidence_refs or []),
            "open_questions": [],
            "next_action": "continue",
        },
        ensure_ascii=False,
    )


def test_readonly_task_detail_exposes_presentation_without_writes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(
        title="Presentation",
        prompt="Give the user one truthful status.",
        workspace="/repo",
        provider="claude",
    )
    conn = service._store._ledger._conn
    before = {
        table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
        for table in ("team_artifacts", "relay_stream_events", "relay_role_attempts")
    }

    detail = service.get_task_readonly(task.id)
    payload = detail.to_dict()["presentation"]

    after = {
        table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
        for table in before
    }
    assert after == before
    assert payload["state"] == "running"
    assert payload["freshness"]["source"] == "relay_lifecycle"
    assert payload["freshness"]["is_stale"] is False
    assert payload["current_actor"]["role"] == "director"
    assert set(payload["allowed_actions"]) == {"add_input", "interrupt"}


@pytest.mark.parametrize(
    "claim_source",
    ["provider_native_resolving", "provider_native_superseding"],
)
def test_presentation_marks_unacknowledged_native_approval_claim_needs_recovery(
    tmp_path: Path,
    claim_source: str,
) -> None:
    """Temporary provider claims must never look like a second approval CTA."""

    service = _service(tmp_path)
    task = service.create_task(
        title="Approval recovery",
        prompt="Keep the native approval exactly once.",
        workspace="/repo",
        provider="claude",
    )
    service._store.lifecycle.set_round_confirmation(
        task.id,
        1,
        source=claim_source,
        kind="command_approval",
        role="director",
        provider="codex",
        provider_request_id="req-recovery",
        runtime_event_id=99,
        native_session_id="native-test",
        agent_run_id=101,
        turn_id="turn-test",
    )

    presentation = service.get_task_readonly(task.id).presentation.to_dict()

    assert presentation["state"] == "blocked"
    assert presentation["freshness"]["recovery_required"] is True
    assert presentation["freshness"]["recovery_state"] == "needs_recovery"
    assert "避免重复授权" in presentation["blocking_reason"]
    assert "恢复审批回执" in presentation["next_action"]
    assert presentation["allowed_actions"] == ["refresh"]


def test_presentation_marks_unupdated_active_task_stale(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(
        title="Stale",
        prompt="Show a recoverable stale status.",
        workspace="/repo",
        provider="claude",
    )
    conn = service._store._ledger._conn
    conn.execute(
        "UPDATE team_runs SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", task.id),
    )
    conn.commit()

    presentation = service.get_task_readonly(task.id).presentation.to_dict()

    assert presentation["state"] == "stale"
    assert presentation["freshness"]["is_stale"] is True
    assert "30 分钟" in presentation["freshness"]["reason"]
    assert presentation["allowed_actions"] == ["refresh"]


@pytest.mark.parametrize(
    "durable_status",
    ["completed", "interrupted", "failed", "blocked", "waiting_user"],
)
def test_durable_task_state_is_not_marked_stale_only_because_it_is_old(
    tmp_path: Path,
    durable_status: str,
) -> None:
    service = _service(tmp_path)
    task = service.create_task(
        title="Terminal history",
        prompt="Keep terminal history truthful.",
        workspace="/repo",
        provider="claude",
    )
    conn = service._store._ledger._conn
    conn.execute(
        "UPDATE team_runs SET status = ?, updated_at = ? WHERE id = ?",
        (durable_status, "2000-01-01T00:00:00+00:00", task.id),
    )
    conn.commit()

    presentation = service.get_task_readonly(task.id).presentation.to_dict()

    assert presentation["state"] == durable_status
    assert presentation["freshness"]["is_stale"] is False
    assert presentation["freshness"]["reason"] == ""


def test_task_page_filters_presentation_in_database_before_hydrating_page(
    tmp_path: Path,
) -> None:
    """Pagination works on the semantic state, not a browser-side card list."""

    service = _service(tmp_path)
    approval = service.create_task(
        title="Approval page",
        prompt="Wait for plan approval.",
        workspace="/repo-a",
        provider="claude",
    )
    stale = service.create_task(
        title="Stale page",
        prompt="Needs recovery.",
        workspace="/repo-a",
        provider="claude",
    )
    service.create_task(
        title="Other workspace",
        prompt="Do not include.",
        workspace="/repo-b",
        provider="claude",
    )
    store = service._store
    conn = store._ledger._conn
    store.lifecycle.set_round_execution(
        approval.id,
        1,
        waiting_reason="plan_approval",
    )
    conn.execute("UPDATE team_runs SET status = 'waiting_user' WHERE id = ?", (approval.id,))
    conn.execute(
        "UPDATE team_runs SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", stale.id),
    )
    conn.commit()
    before = int(conn.execute("SELECT COUNT(*) AS count FROM team_artifacts").fetchone()["count"])

    summaries, total, counts = service.list_tasks_page_readonly(
        workspace="/repo-a",
        presentation_state="waiting_approval",
        page=1,
        page_size=1,
    )

    assert total == 1
    assert [summary.task_id for summary in summaries] == [approval.id]
    assert summaries[0].presentation.state == "waiting_approval"
    assert counts["waiting_approval"] == 1
    assert counts["stale"] == 1
    assert int(conn.execute("SELECT COUNT(*) AS count FROM team_artifacts").fetchone()["count"]) == before


def test_readonly_projection_does_not_backfill_legacy_lifecycle_rows(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(
        title="Legacy read",
        prompt="Do not repair while rendering.",
        workspace="/repo",
        provider="claude",
    )
    conn = service._store._ledger._conn
    conn.execute("DELETE FROM relay_role_attempts WHERE team_run_id = ?", (task.id,))
    conn.execute("DELETE FROM relay_rounds WHERE team_run_id = ?", (task.id,))
    conn.commit()

    detail = service.get_task_readonly(task.id)

    assert detail.current_round_id == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) AS count FROM relay_rounds WHERE team_run_id = ?",
            (task.id,),
        ).fetchone()["count"]
        == 0
    )


@pytest.mark.parametrize("legacy_mode", ["simple", "auto", "team", "unknown"])
def test_legacy_execution_modes_normalize_to_standard(
    tmp_path: Path,
    legacy_mode: str,
) -> None:
    service = _service(tmp_path)

    task = service.create_task(
        title="Mode",
        prompt="Normalize mode.",
        workspace="/repo",
        provider="claude",
        execution_mode=legacy_mode,
    )

    execution = service._store.lifecycle.round_execution(task.id, 1)
    assert execution["execution_mode"] == "standard"


def test_goal_mode_requires_goal_and_acceptance_criteria_for_new_tasks(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="execution_goal"):
        service.create_task(
            title="Goal",
            prompt="Ship it.",
            workspace="/repo",
            provider="claude",
            execution_mode="goal",
        )
    with pytest.raises(ValueError, match="acceptance_criteria"):
        service.create_task(
            title="Goal",
            prompt="Ship it.",
            workspace="/repo",
            provider="claude",
            execution_mode="goal",
            execution_goal="Ship it.",
        )

    task = service.create_task(
        title="Goal",
        prompt="Ship it.",
        workspace="/repo",
        provider="claude",
        execution_mode="goal",
        execution_goal="Ship it.",
        acceptance_criteria=["The focused suite passes."],
    )
    execution = service._store.lifecycle.round_execution(task.id, 1)
    assert execution["execution_mode"] == "goal"
    assert execution["execution_strategy"]["acceptance_criteria"] == [
        "The focused suite passes."
    ]


def test_plan_first_waits_for_explicit_approval_before_implementation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(
        title="Plan first",
        prompt="Plan before editing.",
        workspace="/repo",
        provider="claude",
        execution_mode="plan_first",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _routing_output(required_roles=["director", "architect", "implementer"]),
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "architect",
            _envelope(
                role="architect",
                artifact_type="architecture_plan",
                handoff_to="implementer",
            ),
            dispatch_next=False,
        )
    )

    detail = service.get_task_readonly(task.id)
    jobs = {job.role: job for job in detail.role_jobs}
    assert detail.task.status == "waiting_user"
    assert detail.round_execution["waiting_reason"] == "plan_approval"
    assert jobs["architect"].status == "waiting"
    assert jobs["implementer"].status == "idle"


def test_goal_final_summary_requires_independent_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(
        title="Goal",
        prompt="Ship it.",
        workspace="/repo",
        provider="claude",
        execution_mode="goal",
        execution_goal="Ship it.",
        acceptance_criteria=["The focused suite passes."],
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _routing_output(
                required_roles=["director", "implementer", "tester"],
                acceptance_criteria=["The focused suite passes."],
            ),
            dispatch_next=False,
        )
    )
    service._store.save_artifact(
        task.id,
        "implementer",
        "implementation_report",
        {
            "status": "passed",
            "summary": "implemented",
            "evidence_refs": ["src/app.py"],
            "implementation_run_id": 101,
        },
        summary="implemented",
    )
    service._store.update_role_status(task.id, "implementer", "passed")
    service._store.save_artifact(
        task.id,
        "tester",
        "test_report",
        {"status": "passed", "summary": "claimed pass", "evidence_refs": []},
        summary="claimed pass",
    )
    service._store.update_role_status(task.id, "tester", "passed")
    service._store.update_role_status(task.id, "director", "streaming")

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _envelope(role="director", artifact_type="final_summary"),
            dispatch_next=False,
        )
    )

    detail = service.get_task_readonly(task.id)
    assert detail.task.status == "blocked"
    assert "independent acceptance evidence" in next(
        job.error_message for job in detail.role_jobs if job.role == "director"
    )


def test_completion_claim_prevents_replay_after_service_restart(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(
        title="Replay",
        prompt="Do not duplicate completion artifacts.",
        workspace="/repo",
        provider="claude",
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _routing_output(required_roles=["director", "implementer"]),
            dispatch_next=False,
        )
    )
    service._store.update_role_status(task.id, "director", "streaming")
    completion = _envelope(role="director", artifact_type="final_summary")
    asyncio.run(
        service.handle_role_completion_event(
            task.id,
            "director",
            runtime_event_id=701,
            output=completion,
        )
    )
    before = [
        artifact
        for artifact in service.get_task_readonly(task.id).artifacts
        if artifact.get("artifact_type") == "role_error"
    ]

    restarted = RelayService(
        store=service._store,
        registry=NativeAgentRegistry([_Provider()]),
        default_provider="claude",
    )
    asyncio.run(
        restarted.handle_role_completion_event(
            task.id,
            "director",
            runtime_event_id=701,
            output=completion,
        )
    )
    after = [
        artifact
        for artifact in restarted.get_task_readonly(task.id).artifacts
        if artifact.get("artifact_type") == "role_error"
    ]
    assert len(after) == len(before) == 1
    claim = restarted._store._ledger._conn.execute(
        """
        SELECT status, artifact_id FROM relay_completion_event_claims
        WHERE team_run_id = ? AND role = ? AND runtime_event_id = ?
        """,
        (task.id, "director", 701),
    ).fetchone()
    assert claim["status"] == "applied"
    assert int(claim["artifact_id"] or 0) > 0
