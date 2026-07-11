import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.models import NativeAgentCapabilities, NativeAgentControlResult
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.goal_acceptance import ControlledGoalTestExecutor
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore


class _Provider:
    provider = "claude"
    provider_engine = "goal-acceptance-test"

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(can_start_session=True, can_continue_session=True)

    async def start_session(self, cwd: str, prompt: str, **kwargs: Any):
        del cwd, prompt, kwargs
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="goal-native",
            agent_run_id=101,
            status="started",
        )

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs: Any):
        del prompt, kwargs
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            agent_run_id=101,
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


def _envelope(
    *,
    role: str,
    artifact_type: str,
    handoff_to: str = "",
    status: str = "passed",
    evidence_refs: list[str] | None = None,
    **extra: Any,
) -> str:
    return json.dumps(
        {
            "status": status,
            "reason": f"{role} evidence",
            "role": role,
            "artifact_type": artifact_type,
            "handoff_to": handoff_to,
            "summary": f"{role} report",
            "evidence_refs": evidence_refs or [f"evidence/{role}.txt"],
            "open_questions": [],
            "next_action": "continue",
            **extra,
        },
        ensure_ascii=False,
    )


def _route() -> str:
    return _envelope(
        role="director",
        artifact_type="routing_decision",
        complexity="medium",
        risk="medium",
        route="full_relay",
        required_roles=["director", "implementer", "tester", "auditor"],
        acceptance_criteria=["controlled acceptance succeeds"],
        stop_conditions=[],
        requires_user_approval=False,
    )


def _start_goal_task(
    tmp_path: Path,
    *,
    acceptance_criteria: list[str] | None = None,
) -> tuple[RelayService, Any, Path, int]:
    service = _service(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = service.create_task(
        title="Goal acceptance",
        prompt="Implement and independently verify the result.",
        workspace=str(workspace),
        provider="claude",
        execution_mode="goal",
        execution_goal="A run-bound controlled test proves the implementation.",
        acceptance_criteria=acceptance_criteria or ["controlled acceptance succeeds"],
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _route(),
            dispatch_next=False,
        )
    )
    implementation_run_id = 7001
    service._store.update_role_metadata(
        task.id,
        "implementer",
        provider="claude",
        agent_run_id=implementation_run_id,
        dispatch_verified=True,
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "implementer",
            _envelope(
                role="implementer",
                artifact_type="implementation_report",
                handoff_to="tester",
                evidence_refs=["src/implemented.py"],
            ),
            dispatch_next=False,
        )
    )
    service._store.update_role_metadata(
        task.id,
        "tester",
        provider="claude",
        agent_run_id=7002,
        dispatch_verified=True,
    )
    return service, task, workspace, implementation_run_id


def _unittest_declaration(
    run_id: int,
    *,
    pattern: str = "test_goal_contract.py",
    criteria: list[str] | None = None,
) -> dict[str, Any]:
    declaration: dict[str, Any] = {
        "implementation_run_id": run_id,
        "test": {
            "kind": "unittest",
            "args": ["discover", "-s", ".", "-p", pattern],
        },
    }
    if criteria is not None:
        declaration["criteria"] = criteria
    return declaration


def _write_unittest(
    workspace: Path,
    *,
    passing: bool,
    filename: str = "test_goal_contract.py",
) -> None:
    assertion = "self.assertEqual(1, 1)" if passing else "self.assertEqual(1, 2)"
    (workspace / filename).write_text(
        "import unittest\n\n"
        "class GoalContractTests(unittest.TestCase):\n"
        "    def test_goal_contract(self):\n"
        f"        {assertion}\n",
        encoding="utf-8",
    )


def test_goal_acceptance_executes_run_bound_controlled_test_and_persists_it(
    tmp_path: Path,
) -> None:
    service, task, workspace, implementation_run_id = _start_goal_task(tmp_path)
    _write_unittest(workspace, passing=True)

    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            _envelope(
                role="tester",
                artifact_type="test_report",
                handoff_to="auditor",
                goal_acceptance=_unittest_declaration(implementation_run_id),
            ),
            dispatch_next=False,
        )
    )

    detail = service.get_task_readonly(task.id)
    records = [record for record in detail.goal_acceptance_records if record.verifier_role == "tester"]
    assert len(records) == 1
    record = records[0]
    assert record.round_id == detail.current_round_id == 1
    assert record.implementation_run_id == implementation_run_id
    assert record.implementation_artifact_id is not None
    assert record.status == "passed"
    assert record.evidence_status == "passed"
    assert record.exit_code == 0
    assert record.test_execution["executed"] is True
    assert record.test_execution["argv"][1:3] == ["-m", "unittest"]
    verifier_artifact = next(
        artifact for artifact in detail.artifacts if artifact["id"] == record.verifier_artifact_id
    )
    assert verifier_artifact["goal_acceptance"]["id"] == record.id
    detail_payload = detail.to_dict()
    assert detail_payload["goal_acceptance_records"][0]["status"] == "passed"
    assert detail_payload["goal_acceptance_records"][0]["implementation_run_id"] == (
        implementation_run_id
    )
    assert detail_payload["goal_acceptance_records"][0]["test_declaration"]["criteria"] == [
        "controlled acceptance succeeds"
    ]


def test_goal_acceptance_requires_every_criterion_to_be_declared(tmp_path: Path) -> None:
    criteria = ["first observable outcome", "second observable outcome"]
    service, task, workspace, implementation_run_id = _start_goal_task(
        tmp_path,
        acceptance_criteria=criteria,
    )
    _write_unittest(workspace, passing=True)

    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            _envelope(
                role="tester",
                artifact_type="test_report",
                handoff_to="auditor",
                goal_acceptance=_unittest_declaration(implementation_run_id),
            ),
            dispatch_next=False,
        )
    )

    record = service.get_task_readonly(task.id).goal_acceptance_records[-1]
    assert record.status == "not_run"
    assert "cover every stated acceptance criterion" in record.reason


def test_goal_acceptance_failed_test_blocks_then_retry_is_append_only(tmp_path: Path) -> None:
    service, task, workspace, implementation_run_id = _start_goal_task(tmp_path)
    _write_unittest(workspace, passing=False)
    output = _envelope(
        role="tester",
        artifact_type="test_report",
        handoff_to="auditor",
        goal_acceptance=_unittest_declaration(implementation_run_id),
    )

    asyncio.run(service.handle_role_output(task.id, "tester", output, dispatch_next=False))
    blocked = service.get_task_readonly(task.id)
    assert blocked.task.status == "blocked"
    first = [
        record for record in blocked.goal_acceptance_records if record.verifier_role == "tester"
    ]
    assert [(record.attempt_no, record.status, record.evidence_status) for record in first] == [
        (1, "failed", "failed")
    ]

    _write_unittest(workspace, passing=True, filename="test_goal_retry.py")
    service._store.update_task_status(task.id, "running")
    service._store.update_role_status(task.id, "tester", "streaming")
    retry_output = _envelope(
        role="tester",
        artifact_type="test_report",
        handoff_to="auditor",
        goal_acceptance=_unittest_declaration(
            implementation_run_id,
            pattern="test_goal_retry.py",
        ),
    )
    asyncio.run(service.handle_role_output(task.id, "tester", retry_output, dispatch_next=False))

    retried = service.get_task_readonly(task.id)
    records = [
        record for record in retried.goal_acceptance_records if record.verifier_role == "tester"
    ]
    assert [(record.attempt_no, record.status, record.evidence_status) for record in records] == [
        (1, "failed", "failed"),
        (2, "passed", "passed"),
    ]


def test_goal_acceptance_rejects_cross_implementation_run_without_execution(
    tmp_path: Path,
) -> None:
    service, task, workspace, implementation_run_id = _start_goal_task(tmp_path)
    _write_unittest(workspace, passing=True)

    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            _envelope(
                role="tester",
                artifact_type="test_report",
                goal_acceptance=_unittest_declaration(implementation_run_id + 1),
            ),
            dispatch_next=False,
        )
    )

    detail = service.get_task_readonly(task.id)
    record = detail.goal_acceptance_records[-1]
    assert detail.task.status == "blocked"
    assert record.round_id == detail.current_round_id
    assert record.implementation_artifact_id is None
    assert record.implementation_run_id == implementation_run_id + 1
    assert record.status == record.evidence_status == "not_run"
    assert record.test_execution["executed"] is False
    assert "does not match" in record.reason


def test_goal_acceptance_rejects_provider_shell_text_as_not_run(tmp_path: Path) -> None:
    service, task, workspace, implementation_run_id = _start_goal_task(tmp_path)
    sentinel = workspace / "provider-command-ran"
    unsafe_declaration = {
        "implementation_run_id": implementation_run_id,
        "test": {"kind": "pytest", "args": [f"tests; touch {sentinel}"]},
    }

    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            _envelope(
                role="tester",
                artifact_type="test_report",
                goal_acceptance=unsafe_declaration,
            ),
            dispatch_next=False,
        )
    )

    detail = service.get_task_readonly(task.id)
    record = detail.goal_acceptance_records[-1]
    assert record.status == record.evidence_status == "not_run"
    assert record.test_execution["executed"] is False
    assert "shell or redirection" in record.reason
    assert not sentinel.exists()


def test_goal_acceptance_rejects_workspace_escape_as_not_run(tmp_path: Path) -> None:
    service, task, _workspace, implementation_run_id = _start_goal_task(tmp_path)
    escaping_declaration = {
        "implementation_run_id": implementation_run_id,
        "test": {"kind": "pytest", "args": ["../outside_test.py"]},
    }

    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            _envelope(
                role="tester",
                artifact_type="test_report",
                goal_acceptance=escaping_declaration,
            ),
            dispatch_next=False,
        )
    )

    record = service.get_task_readonly(task.id).goal_acceptance_records[-1]
    assert record.status == record.evidence_status == "not_run"
    assert record.test_execution["executed"] is False
    assert "inside the task workspace" in record.reason


def test_controlled_goal_test_executor_bounds_captured_output(tmp_path: Path) -> None:
    workspace = tmp_path / "noisy-workspace"
    workspace.mkdir()
    (workspace / "test_goal_output.py").write_text(
        "import unittest\n\n"
        "class OutputTests(unittest.TestCase):\n"
        "    def test_noisy_output(self):\n"
        "        print('x' * 100000)\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    executor = ControlledGoalTestExecutor(max_output_chars=1_000)

    result = asyncio.run(
        executor.execute(
            workspace=str(workspace),
            declaration={
                "test": {
                    "kind": "unittest",
                    "args": ["discover", "-s", ".", "-p", "test_goal_output.py"],
                }
            },
        )
    )

    assert result["executed"] is True
    assert result["status"] == "passed"
    assert len(result["stdout"]) <= 1_100
    assert result["stdout"].endswith("[output truncated]")


def test_goal_completion_rejects_record_from_a_different_round(tmp_path: Path) -> None:
    service, task, _workspace, implementation_run_id = _start_goal_task(tmp_path)
    detail = service.get_task_readonly(task.id)
    implementation = next(
        artifact
        for artifact in detail.artifacts
        if artifact.get("artifact_type") == "implementation_report"
    )
    verifier = service._store.save_artifact(
        task.id,
        "tester",
        "test_report",
        {
            "status": "passed",
            "summary": "old-round test pass",
            "evidence_refs": ["tests/old_round.py"],
            "round_id": detail.current_round_id,
        },
        summary="old-round test pass",
    )
    service._store.update_role_status(task.id, "tester", "passed")
    with pytest.raises(ValueError, match="does not belong to this round"):
        service._store.record_goal_acceptance(
            task.id,
            round_id=detail.current_round_id + 1,
            implementation_artifact_id=int(implementation["id"]),
            implementation_run_id=implementation_run_id,
            verifier_artifact_id=verifier.id,
            verifier_role="tester",
            test_declaration=_unittest_declaration(implementation_run_id),
            test_execution={"executed": True, "exit_code": 0},
            exit_code=0,
            status="passed",
            evidence_status="passed",
            reason="different round fixture",
        )
    # Historic data can predate the stronger store checks.  The completion
    # gate must still ignore a malformed record from another round.
    conn = service._store._ledger._conn
    conn.execute(
        """
        INSERT INTO relay_goal_acceptance_records (
            team_run_id, round_id, implementation_artifact_id,
            implementation_run_id, verifier_artifact_id, verifier_role,
            attempt_no, test_declaration_json, test_execution_json,
            exit_code, status, evidence_status, reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            detail.current_round_id + 1,
            int(implementation["id"]),
            implementation_run_id,
            verifier.id,
            "tester",
            1,
            json.dumps(_unittest_declaration(implementation_run_id)),
            json.dumps({"executed": True, "exit_code": 0}),
            0,
            "passed",
            "passed",
            "different round fixture",
            "2026-07-10T00:00:00+00:00",
            "2026-07-10T00:00:00+00:00",
        ),
    )
    conn.commit()

    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _envelope(role="director", artifact_type="final_summary"),
            dispatch_next=False,
        )
    )

    final = service.get_task_readonly(task.id)
    director = next(job for job in final.role_jobs if job.role == "director")
    assert final.task.status == "blocked"
    assert "independent acceptance evidence" in director.error_message


def test_goal_final_summary_accepts_only_current_run_bound_evidence(tmp_path: Path) -> None:
    service, task, workspace, implementation_run_id = _start_goal_task(tmp_path)
    _write_unittest(workspace, passing=True)
    asyncio.run(
        service.handle_role_output(
            task.id,
            "tester",
            _envelope(
                role="tester",
                artifact_type="test_report",
                handoff_to="auditor",
                goal_acceptance=_unittest_declaration(implementation_run_id),
            ),
            dispatch_next=False,
        )
    )
    service._store.update_role_metadata(
        task.id,
        "auditor",
        provider="claude",
        agent_run_id=7003,
        dispatch_verified=True,
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "auditor",
            _envelope(
                role="auditor",
                artifact_type="audit_report",
                handoff_to="director",
                goal_acceptance={"implementation_run_id": implementation_run_id},
            ),
            dispatch_next=False,
        )
    )
    asyncio.run(
        service.handle_role_output(
            task.id,
            "director",
            _envelope(role="director", artifact_type="final_summary"),
            dispatch_next=False,
        )
    )

    detail = service.get_task_readonly(task.id)
    assert detail.task.status == "completed"
    records = {record.verifier_role: record for record in detail.goal_acceptance_records}
    assert records["tester"].status == records["tester"].evidence_status == "passed"
    assert records["auditor"].status == "not_run"
    assert records["auditor"].evidence_status == "passed"
