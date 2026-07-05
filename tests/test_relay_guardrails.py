import json

from wlcodex.relay.envelopes import parse_role_envelope
from wlcodex.relay.guardrails import (
    RoleGuardrailAction,
    guardrail_role_envelope,
    role_spec_for,
)


def _envelope(**overrides):
    payload = {
        "status": "passed",
        "reason": "ready",
        "role": "implementer",
        "artifact_type": "implementation_report",
        "handoff_to": "tester",
        "summary": "Implementation is ready.",
        "evidence_refs": ["tests/test_relay_guardrails.py"],
        "open_questions": [],
        "next_action": "test it",
    }
    payload.update(overrides)
    return parse_role_envelope(json.dumps(payload, ensure_ascii=False)).envelope


def test_role_specs_make_existing_role_contracts_explicit() -> None:
    director = role_spec_for("director")
    tester = role_spec_for("tester")

    assert director.allowed_artifacts == ("routing_decision", "final_summary")
    assert director.can_complete_task is True
    assert tester.allowed_artifacts == ("test_report",)
    assert tester.requires_evidence_on_pass is True
    assert tester.can_complete_task is False


def test_guardrail_accepts_valid_role_output_without_changing_transition_layer() -> None:
    envelope = _envelope()
    assert envelope is not None

    result = guardrail_role_envelope(envelope, role="implementer")

    assert result.action == RoleGuardrailAction.ACCEPTED
    assert result.reason == ""


def test_guardrail_retries_role_artifact_that_violates_role_contract() -> None:
    envelope = _envelope(
        role="tester",
        artifact_type="final_summary",
        handoff_to="",
        evidence_refs=["pytest"],
    )
    assert envelope is not None

    result = guardrail_role_envelope(envelope, role="tester")

    assert result.action == RoleGuardrailAction.RETRY_ROLE
    assert "tester may not produce final_summary" in result.reason


def test_guardrail_blocks_empty_evidence_on_passed_verification_artifacts() -> None:
    envelope = _envelope(
        role="tester",
        artifact_type="test_report",
        handoff_to="auditor",
        evidence_refs=[],
    )
    assert envelope is not None

    result = guardrail_role_envelope(envelope, role="tester")

    assert result.action == RoleGuardrailAction.BLOCKED
    assert "tester passed test_report without evidence_refs" in result.reason


def test_guardrail_waits_for_user_when_missing_evidence_has_open_question() -> None:
    envelope = _envelope(
        role="tester",
        artifact_type="test_report",
        handoff_to="auditor",
        evidence_refs=[],
        open_questions=["Which test evidence should be cited?"],
    )
    assert envelope is not None

    result = guardrail_role_envelope(envelope, role="tester")

    assert result.action == RoleGuardrailAction.WAITING_USER
    assert result.open_questions == ("Which test evidence should be cited?",)
