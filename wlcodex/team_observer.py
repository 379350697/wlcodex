from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha1
from typing import Any

from wlcodex.team_memory import InstinctMemory, Observation


def observations_from_artifact(
    *,
    team_run_id: int,
    artifact_type: str,
    payload: dict[str, Any],
    evidence_ref: str,
) -> tuple[Observation, ...]:
    if artifact_type != "audit_report":
        return ()
    decision = payload.get("decision")
    findings = payload.get("findings") or []
    missing = payload.get("missing_evidence") or []
    if decision != "block" and not missing:
        return ()
    summary_parts = [str(payload.get("summary") or "audit found missing evidence")]
    summary_parts.extend(str(item) for item in findings[:2])
    summary_parts.extend(str(item) for item in missing[:2])
    return (
        Observation(
            team_run_id=team_run_id,
            domain="audit",
            summary="; ".join(summary_parts),
            evidence_refs=(evidence_ref,),
            confidence=0.7 if decision == "block" else 0.55,
        ),
    )


def candidate_instinct_from_observation(
    observation: Observation,
    *,
    workspace_alias: str,
    repeated_evidence_count: int,
) -> InstinctMemory:
    now = datetime.now(UTC)
    confidence = min(
        0.95,
        observation.confidence + (0.1 * max(0, repeated_evidence_count - 1)),
    )
    digest = sha1(observation.summary.encode("utf-8")).hexdigest()[:12]
    return InstinctMemory(
        instinct_id=f"instinct:{workspace_alias}:{observation.domain}:{digest}",
        scope="project",
        workspace_alias=workspace_alias,
        role="auditor" if observation.domain == "audit" else "*",
        domain=observation.domain,
        trigger=observation.summary[:160],
        action="Check this evidence gap before allowing the downstream gate to pass.",
        confidence=confidence,
        evidence_refs=observation.evidence_refs,
        status="active" if repeated_evidence_count >= 2 else "candidate",
        created_at=now,
        last_validated_at=now,
    )
