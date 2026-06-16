from __future__ import annotations

from typing import Any

from wlcodex.relay.models import (
    HandoffPacket,
    RelayBoard,
    RelayTask,
    RoleContextPacket,
)


_RELAY_CONTEXT_CONSTRAINTS = [
    "Latest user input has priority over stale handoff summaries.",
    "Old handoff information is advisory background only.",
    "No role inherits old permissions from another role.",
    "No role inherits old execution state from another role.",
    "Do not auto commit or auto deploy in relay v1.",
    "Return only one strict JSON object with the required role_envelope fields.",
]
_DIRECTOR_ROUTING_DECISION_CONSTRAINTS = [
    "Director first action must be a routing_decision artifact before any task execution.",
    "Do not inspect, edit, delete, test, commit, or deploy files before routing_decision is accepted.",
    "If the user asks for a relay/process/test workflow, do not choose director_only.",
    "If the task is destructive, high risk, cross-module, deployment, credentials, permissions, or migration related, do not choose director_only.",
    "Use waiting_user when the target file/path or acceptance criteria are ambiguous.",
]
_DIRECTOR_FINAL_SUMMARY_CONSTRAINTS = [
    "A director action after an accepted routing_decision must return a final_summary artifact when no handoff is needed.",
    "Do not invent task-specific artifact_type values such as weather_answer, code_answer, or analysis_answer.",
    "Use final_summary.summary for the user-facing answer or closure summary.",
]


def build_relay_board(
    task: RelayTask,
    *,
    latest_user_input: str = "",
    confirmed_facts: list[str] | None = None,
    open_questions: list[str] | None = None,
    risks: list[str] | None = None,
    current_dispatch: str = "",
    next_step: str = "",
    handoffs: list[HandoffPacket] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    transcript: list[dict[str, Any]] | None = None,
) -> RelayBoard:
    del artifacts, transcript
    handoff = handoffs[-1] if handoffs else None
    return RelayBoard(
        task_id=task.id,
        current_goal=task.prompt or task.title,
        phase=task.phase,
        latest_user_input=latest_user_input or task.prompt,
        confirmed_facts=list(confirmed_facts or []),
        open_questions=list(open_questions or []),
        risks=list(risks or []),
        current_dispatch=current_dispatch or task.phase,
        next_step=next_step or (handoff.next_action if handoff else "director review"),
    )


def build_role_context_packet(
    *,
    task: RelayTask,
    role: str,
    board: RelayBoard,
    handoffs: list[HandoffPacket],
    artifacts: list[dict[str, Any]],
    transcript: list[dict[str, Any]] | None = None,
) -> RoleContextPacket:
    del transcript
    has_routing_decision = any(
        str(artifact.get("artifact_type") or artifact.get("type") or "")
        == "routing_decision"
        and "dispatch_verified" not in artifact
        for artifact in artifacts
    )
    role_artifacts = [
        _artifact_summary(artifact)
        for artifact in artifacts
        if _artifact_is_relevant(artifact, role)
    ]
    constraints = list(_RELAY_CONTEXT_CONSTRAINTS)
    expected_output_envelope = _default_expected_output_envelope(role)
    if role == "director" and not has_routing_decision:
        constraints.extend(_DIRECTOR_ROUTING_DECISION_CONSTRAINTS)
        expected_output_envelope = _director_routing_decision_envelope()
    elif role == "director" and has_routing_decision:
        constraints.extend(_DIRECTOR_FINAL_SUMMARY_CONSTRAINTS)
        expected_output_envelope = _director_final_summary_envelope()
    return RoleContextPacket(
        task_id=task.id,
        role=role,
        workspace=task.workspace,
        current_goal=board.current_goal,
        phase=board.phase,
        latest_user_input=board.latest_user_input,
        confirmed_facts=list(board.confirmed_facts),
        role_relevant_artifacts=role_artifacts,
        handoff_summaries=[
            handoff.summary for handoff in handoffs if handoff.to_role in (role, "")
        ],
        constraints=constraints,
        expected_output_envelope=expected_output_envelope,
    )


def _default_expected_output_envelope(role: str) -> dict[str, Any]:
    return {
        "status": "passed|failed|blocked|waiting",
        "reason": "brief reason",
        "role": role,
        "artifact_type": "relay artifact type",
        "handoff_to": "next role or empty string",
        "summary": "compact result summary",
        "evidence_refs": [],
        "open_questions": [],
        "next_action": "what should happen next",
    }


def _director_routing_decision_envelope() -> dict[str, Any]:
    return {
        "status": "passed|blocked|waiting",
        "reason": "why this route is appropriate",
        "role": "director",
        "artifact_type": "routing_decision",
        "handoff_to": "",
        "summary": "director routing decision summary",
        "evidence_refs": [],
        "open_questions": [],
        "next_action": "dispatch next role, wait for user, block, or complete directly",
        "complexity": "low|medium|high",
        "risk": "low|medium|high|critical",
        "route": "director_only|core_relay|full_relay|audit_first|waiting_user|blocked",
        "required_roles": ["director"],
        "acceptance_criteria": ["observable acceptance criterion"],
        "stop_conditions": ["condition that should stop or ask user"],
        "requires_user_approval": False,
    }


def _director_final_summary_envelope() -> dict[str, Any]:
    return {
        "status": "passed|failed|blocked|waiting",
        "reason": "brief reason",
        "role": "director",
        "artifact_type": "final_summary",
        "handoff_to": "",
        "summary": "user-facing final answer or relay closure summary",
        "evidence_refs": [],
        "open_questions": [],
        "next_action": "empty string or no further action",
    }


def _artifact_is_relevant(artifact: dict[str, Any], role: str) -> bool:
    relay_role = str(artifact.get("relay_role") or artifact.get("role") or "")
    artifact_type = str(artifact.get("artifact_type") or artifact.get("type") or "")
    if artifact_type == "relay_board":
        return False
    return not relay_role or relay_role == role or artifact_type == "handoff_packet"


def _artifact_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_type": str(artifact.get("artifact_type") or artifact.get("type") or ""),
        "relay_role": str(artifact.get("relay_role") or artifact.get("role") or ""),
        "summary": str(artifact.get("safe_summary") or artifact.get("summary") or "")[:300],
        "evidence_refs": list(artifact.get("evidence_refs") or []),
    }
