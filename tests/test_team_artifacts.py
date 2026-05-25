from wlcodex.team_artifacts import (
    GateResult,
    _missing,
    architecture_plan_payload,
    implementation_report_payload,
    structured_implementation_evidence_from_text,
    test_report_payload_from_implementation as build_test_report_payload_from_implementation,
    validate_architecture_plan,
    validate_audit_report,
    validate_implementation_report,
    validate_test_report,
)


def test_architecture_plan_gate_requires_acceptance_criteria_and_scope():
    payload = {
        "summary": "Split the team artifacts into explicit handoff contracts.",
        "risk_level": "low",
        "files_or_modules_in_scope": ["wlcodex/team_artifacts.py"],
        "files_or_modules_out_of_scope": ["wlcodex/controller.py"],
        "impact_notes": "Pure validation helper only.",
        "implementation_steps": ["Add schemas", "Add gates"],
        "acceptance_criteria": ["Architecture plans without criteria are blocked"],
        "parallelization_policy": "single writer",
    }

    result = validate_architecture_plan(payload)

    assert result == GateResult(passed=True, missing=())


def test_architecture_plan_gate_blocks_missing_acceptance_criteria():
    payload = {
        "summary": "Split the team artifacts into explicit handoff contracts.",
        "risk_level": "low",
        "files_or_modules_in_scope": ["wlcodex/team_artifacts.py"],
        "files_or_modules_out_of_scope": ["wlcodex/controller.py"],
        "impact_notes": "Pure validation helper only.",
        "implementation_steps": ["Add schemas", "Add gates"],
        "parallelization_policy": "single writer",
    }

    result = validate_architecture_plan(payload)

    assert result.passed is False
    assert "acceptance_criteria" in result.missing


def test_architecture_plan_payload_declares_investigator_v1_policy():
    payload = architecture_plan_payload(
        summary="Plan includes diagnosis duties.",
        risk_level="medium",
        source="auto_context",
    )

    assert payload["investigator_policy"] == "Architect performs investigator duties in v1"
    assert validate_architecture_plan(payload).passed is True


def test_missing_allows_zero_and_false_required_values():
    payload = {"zero": 0, "flag": False}

    missing = _missing(payload, ("zero", "flag"))

    assert missing == ()


def test_implementation_report_gate_accepts_required_fields():
    payload = {
        "summary": "Implementation complete.",
        "changed_files": ["wlcodex/team_artifacts.py"],
        "diff_summary": "Added artifact gates.",
        "commands_run": [
            {
                "command": "pytest tests/test_team_artifacts.py -q",
                "exit_status": 0,
                "summary": "artifact tests passed",
            }
        ],
        "tests_attempted": [
            {
                "command": "pytest tests/test_team_artifacts.py -q",
                "exit_status": 0,
                "summary": "artifact tests passed",
            }
        ],
        "known_limitations": ["None known"],
    }

    result = validate_implementation_report(payload)

    assert result == GateResult(passed=True, missing=())


def test_implementation_report_gate_blocks_missing_diff_summary():
    payload = {
        "summary": "Implementation complete.",
        "changed_files": ["wlcodex/team_artifacts.py"],
    }

    result = validate_implementation_report(payload)

    assert result.passed is False
    assert "diff_summary" in result.missing


def test_test_report_gate_accepts_required_fields():
    payload = {
        "summary": "Focused tests pass.",
        "commands_run": [
            {
                "command": ".venv/bin/python -m pytest tests/test_team_artifacts.py -q",
                "exit_status": 0,
                "summary": "artifact gate tests passed",
            }
        ],
        "passed": ["artifact gate tests"],
        "failed": ["None"],
        "coverage_of_acceptance_criteria": [
            {
                "criterion": "All artifact gates are covered.",
                "status": "covered",
                "evidence": "pytest tests/test_team_artifacts.py -q",
            }
        ],
        "failure_evidence": ["None"],
    }

    result = validate_test_report(payload)

    assert result == GateResult(passed=True, missing=())


def test_test_report_gate_blocks_missing_commands_run():
    payload = {
        "summary": "Focused tests pass.",
        "coverage_of_acceptance_criteria": ["All artifact gates are covered."],
    }

    result = validate_test_report(payload)

    assert result.passed is False
    assert "commands_run" in result.missing


def test_audit_report_requires_explicit_decision():
    payload = {
        "summary": "Artifact gate review is complete.",
        "findings": [],
        "missing_evidence": [],
        "risk_level": "low",
        "recommended_next_action": "close",
    }

    result = validate_audit_report(payload)

    assert result.passed is False
    assert "decision" in result.missing


def test_audit_report_blocks_invalid_explicit_decision():
    payload = {
        "decision": "defer",
        "summary": "Artifact gate review is complete.",
        "findings": [],
        "missing_evidence": [],
        "risk_level": "low",
        "recommended_next_action": "ask_user",
    }

    result = validate_audit_report(payload)

    assert result.passed is False
    assert "decision" in result.missing


def test_audit_report_blocks_false_decision():
    payload = {
        "decision": False,
        "summary": "x",
        "risk_level": "low",
    }

    result = validate_audit_report(payload)

    assert result.passed is False
    assert "decision" in result.missing


def test_audit_report_blocks_zero_decision():
    payload = {
        "decision": 0,
        "summary": "x",
        "risk_level": "low",
    }

    result = validate_audit_report(payload)

    assert result.passed is False
    assert "decision" in result.missing


def test_implementation_report_gate_blocks_placeholder_evidence():
    payload = {
        "summary": "Implementation complete.",
        "changed_files": ["No changed files reported."],
        "diff_summary": "No diff evidence collected.",
        "commands_run": ["No structured command evidence reported by implementer."],
        "tests_attempted": ["No structured test attempt reported by implementer."],
        "known_limitations": ["No known limitations reported."],
    }

    result = validate_implementation_report(payload)

    assert result.passed is False
    assert set(result.missing) >= {
        "changed_files",
        "diff_summary",
        "commands_run",
        "tests_attempted",
    }


def test_implementation_payload_records_explicit_missing_evidence_instead_of_empty_arrays():
    payload = implementation_report_payload(
        summary="Claude implementation completed without structured evidence.",
        changed_files=[],
        diff_summary="",
        source_agent="claude",
        commands_run=[],
        tests_attempted=[],
    )

    assert payload["changed_files"]
    assert payload["diff_summary"]
    assert payload["commands_run"]
    assert payload["tests_attempted"]
    result = validate_implementation_report(payload)
    assert result.passed is False
    assert set(result.missing) >= {
        "changed_files",
        "diff_summary",
        "commands_run",
        "tests_attempted",
    }


def test_claude_implementation_report_shape_becomes_gate_ready_evidence():
    text = """
```json
{
  "implementation_report": {
    "files_changed": [
      {
        "path": "docs/manual-aet-e2e.md",
        "action": "created",
        "evidence": "AET_MANUAL_E2E_V1 low-risk documentation artifact"
      }
    ],
    "acceptance_verification": {
      "command": "test -f docs/manual-aet-e2e.md && grep -Fq AET_MANUAL_E2E_V1 docs/manual-aet-e2e.md",
      "result": "ALL CHECKS PASSED",
      "checks": {
        "file_exists": true,
        "AET_MANUAL_E2E_V1": true
      }
    },
    "no_other_files_modified": true
  }
}
```
"""

    evidence = structured_implementation_evidence_from_text(text)
    payload = implementation_report_payload(
        summary="Claude implementation completed.",
        changed_files=evidence.get("changed_files", []),
        diff_summary=evidence.get("diff_summary", ""),
        source_agent="claude",
        commands_run=evidence.get("commands_run", []),
        tests_attempted=evidence.get("tests_attempted", []),
    )
    test_payload = build_test_report_payload_from_implementation(
        summary="Implementation test evidence collected.",
        implementation_artifact_id=2,
        commands_run=evidence.get("tests_attempted", []),
        acceptance_criteria=["docs/manual-aet-e2e.md contains required phrases"],
    )

    assert evidence["changed_files"] == ["docs/manual-aet-e2e.md"]
    assert "created docs/manual-aet-e2e.md" in evidence["diff_summary"]
    assert evidence["commands_run"][0]["exit_status"] == 0
    assert evidence["tests_attempted"] == evidence["commands_run"]
    assert validate_implementation_report(payload).passed is True
    assert validate_test_report(test_payload).passed is True


def test_test_report_gate_blocks_placeholder_evidence():
    payload = {
        "summary": "Implementation test evidence requires audit review.",
        "commands_run": ["No structured test command evidence reported by implementer."],
        "passed": ["No passing test evidence reported."],
        "failed": ["Test evidence requires auditor review."],
        "coverage_of_acceptance_criteria": [
            "Acceptance criteria coverage was not reported structurally."
        ],
        "failure_evidence": ["team_artifact=1"],
    }

    result = validate_test_report(payload)

    assert result.passed is False
    assert set(result.missing) >= {
        "commands_run",
        "passed",
        "coverage_of_acceptance_criteria",
    }


def test_test_report_payload_records_missing_test_evidence_instead_of_empty_arrays():
    payload = build_test_report_payload_from_implementation(
        summary="No structured test evidence was reported.",
        implementation_artifact_id=7,
        commands_run=[],
        acceptance_criteria=["Focused verification passes"],
    )

    assert payload["commands_run"]
    assert payload["passed"]
    assert payload["coverage_of_acceptance_criteria"]
    result = validate_test_report(payload)
    assert result.passed is False
    assert set(result.missing) >= {
        "commands_run",
        "passed",
        "coverage_of_acceptance_criteria",
    }


def test_audit_report_pass_requires_test_evidence_reference():
    payload = {
        "decision": "pass",
        "summary": "Verification passed.",
        "findings": ["No blocking findings."],
        "missing_evidence": ["No missing evidence reported."],
        "risk_level": "low",
        "recommended_next_action": "close",
    }

    result = validate_audit_report(payload)

    assert result.passed is False
    assert "test_evidence_refs" in result.missing


def test_audit_report_pass_accepts_test_evidence_reference():
    payload = {
        "decision": "pass",
        "summary": "Reviewed focused pytest evidence.",
        "findings": ["No blocking findings."],
        "missing_evidence": ["None"],
        "risk_level": "low",
        "recommended_next_action": "close",
        "test_evidence_refs": ["team_artifact=42"],
    }

    result = validate_audit_report(payload)

    assert result == GateResult(passed=True, missing=())


def test_architecture_plan_gate_requires_full_spec_contract():
    payload = {
        "summary": "Plan is too thin.",
        "risk_level": "medium",
        "files_or_modules_in_scope": ["wlcodex/controller.py"],
        "implementation_steps": ["Patch controller"],
        "acceptance_criteria": ["Focused tests pass"],
    }

    result = validate_architecture_plan(payload)

    assert result.passed is False
    assert set(result.missing) == {
        "files_or_modules_out_of_scope",
        "impact_notes",
        "parallelization_policy",
    }


def test_implementation_report_gate_requires_full_spec_contract():
    payload = {
        "summary": "Implementation complete.",
        "changed_files": ["wlcodex/controller.py"],
        "diff_summary": "Added gate checks.",
    }

    result = validate_implementation_report(payload)

    assert result.passed is False
    assert set(result.missing) == {
        "commands_run",
        "tests_attempted",
        "known_limitations",
    }


def test_test_report_gate_requires_full_spec_contract():
    payload = {
        "summary": "Focused tests pass.",
        "commands_run": ["pytest -q"],
        "coverage_of_acceptance_criteria": ["All covered"],
    }

    result = validate_test_report(payload)

    assert result.passed is False
    assert set(result.missing) == {
        "commands_run",
        "passed",
        "failed",
        "coverage_of_acceptance_criteria",
        "failure_evidence",
    }


def test_audit_report_gate_requires_full_spec_contract():
    payload = {
        "decision": "pass",
        "summary": "No blocking findings.",
        "risk_level": "low",
    }

    result = validate_audit_report(payload)

    assert result.passed is False
    assert set(result.missing) == {
        "findings",
        "missing_evidence",
        "recommended_next_action",
        "test_evidence_refs",
    }
