from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from wlcodex.relay.models import RoleEnvelope


class RoleGuardrailAction(StrEnum):
    ACCEPTED = "accepted"
    RETRY_ROLE = "retry_role"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RelayRoleSpec:
    role: str
    allowed_artifacts: tuple[str, ...]
    requires_evidence_on_pass: bool = False
    can_complete_task: bool = False


@dataclass(frozen=True)
class RoleGuardrailResult:
    action: RoleGuardrailAction
    reason: str = ""
    open_questions: tuple[str, ...] = ()
    confirmation_options: tuple[dict[str, str], ...] = ()

    @property
    def accepted(self) -> bool:
        return self.action == RoleGuardrailAction.ACCEPTED


_ROLE_SPECS: dict[str, RelayRoleSpec] = {
    "director": RelayRoleSpec(
        role="director",
        allowed_artifacts=("routing_decision", "final_summary"),
        can_complete_task=True,
    ),
    "architect": RelayRoleSpec(
        role="architect",
        allowed_artifacts=("architecture_plan",),
    ),
    "implementer": RelayRoleSpec(
        role="implementer",
        allowed_artifacts=("implementation_report",),
        requires_evidence_on_pass=True,
    ),
    "tester": RelayRoleSpec(
        role="tester",
        allowed_artifacts=("test_report",),
        requires_evidence_on_pass=True,
    ),
    "auditor": RelayRoleSpec(
        role="auditor",
        allowed_artifacts=("audit_report",),
        requires_evidence_on_pass=True,
    ),
}


def role_spec_for(role: str) -> RelayRoleSpec:
    role_id = str(role or "").strip()
    return _ROLE_SPECS.get(
        role_id,
        RelayRoleSpec(role=role_id, allowed_artifacts=()),
    )


def guardrail_role_envelope(
    envelope: RoleEnvelope,
    *,
    role: str,
) -> RoleGuardrailResult:
    role_id = str(role or envelope.role or "").strip()
    spec = role_spec_for(role_id)
    if not spec.allowed_artifacts:
        return RoleGuardrailResult(
            RoleGuardrailAction.BLOCKED,
            f"unknown relay role for guardrail: {role_id}",
        )
    if envelope.role and envelope.role != role_id:
        return RoleGuardrailResult(
            RoleGuardrailAction.RETRY_ROLE,
            f"{role_id} output declared role {envelope.role}",
        )
    if envelope.artifact_type not in spec.allowed_artifacts:
        return RoleGuardrailResult(
            RoleGuardrailAction.RETRY_ROLE,
            f"{role_id} may not produce {envelope.artifact_type}; expected one of "
            f"{', '.join(spec.allowed_artifacts)}",
        )
    if envelope.artifact_type == "final_summary" and not spec.can_complete_task:
        return RoleGuardrailResult(
            RoleGuardrailAction.RETRY_ROLE,
            f"{role_id} may not complete task with final_summary",
        )
    if (
        envelope.status == "passed"
        and spec.requires_evidence_on_pass
        and not envelope.evidence_refs
    ):
        reason = f"{role_id} passed {envelope.artifact_type} without evidence_refs"
        if envelope.open_questions:
            return RoleGuardrailResult(
                RoleGuardrailAction.WAITING_USER,
                reason,
                tuple(envelope.open_questions),
                tuple(dict(option) for option in envelope.confirmation_options),
            )
        return RoleGuardrailResult(
            RoleGuardrailAction.BLOCKED,
            reason,
        )
    return RoleGuardrailResult(RoleGuardrailAction.ACCEPTED)
