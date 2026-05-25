from wlcodex.team_observer import (
    candidate_instinct_from_observation,
    observations_from_artifact,
)


def test_observer_extracts_observation_from_blocking_audit_artifact() -> None:
    observations = observations_from_artifact(
        team_run_id=7,
        artifact_type="audit_report",
        payload={
            "decision": "block",
            "summary": "Missing test evidence for auth fallback.",
            "findings": [
                "auth fallback was changed without a regression test",
            ],
            "missing_evidence": [
                "pytest tests/test_auth.py -q",
            ],
            "risk_level": "medium",
        },
        evidence_ref="team_artifact=21",
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.domain == "audit"
    assert "regression test" in observation.summary
    assert observation.evidence_refs == ("team_artifact=21",)


def test_candidate_instinct_from_repeated_observation_starts_active() -> None:
    observation = observations_from_artifact(
        team_run_id=7,
        artifact_type="audit_report",
        payload={
            "decision": "block",
            "summary": "Missing changed-file evidence in verifier packet.",
            "findings": ["verifier lacked changed-file evidence"],
            "missing_evidence": ["changed_files"],
            "risk_level": "medium",
        },
        evidence_ref="team_artifact=22",
    )[0]

    instinct = candidate_instinct_from_observation(
        observation,
        workspace_alias="wlcodex",
        repeated_evidence_count=2,
    )

    assert instinct.status == "active"
    assert instinct.scope == "project"
    assert instinct.confidence >= 0.7
