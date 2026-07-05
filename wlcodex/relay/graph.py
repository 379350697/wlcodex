from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from wlcodex.relay.models import EnvelopeParseResult, RelayTaskDetail, RoleEnvelope


_TERMINAL_TASK_STATUSES = {"completed", "blocked", "failed", "interrupted", "superseded"}
_ACTIVE_ROLE_STATUSES = ("streaming", "queued", "waiting", "blocked", "failed")


@dataclass(frozen=True)
class RelayInterrupt:
    kind: str
    role: str
    reason: str = ""
    artifact_type: str = ""
    artifact_id: int = 0
    open_questions: list[str] = field(default_factory=list)
    confirmation_options: list[dict[str, str]] = field(default_factory=list)
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelayTransition:
    update: dict[str, Any] = field(default_factory=dict)
    goto: str = ""
    interrupt: RelayInterrupt | None = None
    terminal: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "update": dict(self.update),
            "goto": self.goto,
            "interrupt": self.interrupt.to_json_dict() if self.interrupt else None,
            "terminal": self.terminal,
            "events": [dict(event) for event in self.events],
        }


@dataclass(frozen=True)
class MarvisRelayState:
    task_id: int
    round_id: int
    current_node: str
    route: str
    required_roles: list[str]
    role_statuses: dict[str, str]
    active_role: str
    latest_user_input: str
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    pending_interrupt: RelayInterrupt | None = None
    terminal_status: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "round_id": self.round_id,
            "current_node": self.current_node,
            "route": self.route,
            "required_roles": list(self.required_roles),
            "role_statuses": dict(self.role_statuses),
            "active_role": self.active_role,
            "latest_user_input": self.latest_user_input,
            "handoffs": [dict(handoff) for handoff in self.handoffs],
            "artifacts": [dict(artifact) for artifact in self.artifacts],
            "pending_interrupt": (
                self.pending_interrupt.to_json_dict() if self.pending_interrupt else None
            ),
            "terminal_status": self.terminal_status,
        }


def transition_from_role_parse_result(
    result: EnvelopeParseResult,
    *,
    role: str,
    round_id: int,
    artifact_id: int = 0,
    prefer_handoff: bool = False,
) -> RelayTransition:
    if not result.ok or result.envelope is None:
        error = str(result.error or "invalid role envelope")
        return RelayTransition(
            update={
                "round_id": int(round_id),
                "role_statuses": {role: "blocked"},
                "task_status": "blocked",
                "error": error,
            },
            goto="blocked",
            terminal="blocked",
            events=[
                {
                    "event_type": "role.error",
                    "role": role,
                    "round_id": int(round_id),
                    "error": error,
                }
            ],
        )
    next_role = result.next_role or ""
    if result.envelope.status in {"blocked", "waiting"} and not result.envelope.handoff_to:
        next_role = ""
    return transition_from_role_envelope(
        result.envelope,
        role=role,
        round_id=round_id,
        next_role=next_role,
        artifact_id=artifact_id,
        prefer_handoff=prefer_handoff,
    )


def transition_from_guardrail_result(
    *,
    action: str,
    role: str,
    round_id: int,
    reason: str = "",
    open_questions: list[str] | tuple[str, ...] | None = None,
    confirmation_options: list[dict[str, str]] | tuple[dict[str, str], ...] | None = None,
) -> RelayTransition:
    action_id = str(action or "").strip()
    role_id = str(role or "").strip()
    round_number = int(round_id)
    base_update = {
        "round_id": round_number,
        "guardrail_action": action_id,
        "guardrail_reason": str(reason or ""),
    }
    if action_id == "retry_role":
        return RelayTransition(
            update={
                **base_update,
                "task_status": "running",
                "role_statuses": {role_id: "queued"},
            },
            goto=role_id,
            events=[
                {
                    "event_type": "role.retrying",
                    "role": role_id,
                    "round_id": round_number,
                    "retry_kind": "guardrail",
                    "error": reason,
                }
            ],
        )
    if action_id == "waiting_user":
        interrupt = RelayInterrupt(
            kind="guardrail_question",
            role=role_id,
            reason=str(reason or ""),
            artifact_type="role_error",
            open_questions=list(open_questions or ([reason] if reason else [])),
            confirmation_options=[dict(option) for option in confirmation_options or ()],
            source="guardrail",
            payload={
                "guardrail_action": action_id,
                "retry_kind": "guardrail",
                "round_id": round_number,
            },
        )
        return RelayTransition(
            update={
                **base_update,
                "task_status": "waiting_user",
                "role_statuses": {role_id: "waiting"},
            },
            goto="waiting_user",
            interrupt=interrupt,
            events=[
                {
                    "event_type": "task.waiting_user",
                    "role": role_id,
                    "round_id": round_number,
                    "interrupt": interrupt.to_json_dict(),
                    "retry_kind": "guardrail",
                }
            ],
        )
    if action_id == "blocked":
        return RelayTransition(
            update={
                **base_update,
                "task_status": "blocked",
                "role_statuses": {role_id: "blocked"},
            },
            goto="blocked",
            terminal="blocked",
            events=[
                {
                    "event_type": "role.status",
                    "role": role_id,
                    "round_id": round_number,
                    "status": "blocked",
                    "error": reason,
                    "retry_kind": "guardrail",
                }
            ],
        )
    return RelayTransition(update=base_update, goto="")


def transition_from_role_envelope(
    envelope: RoleEnvelope,
    *,
    role: str,
    round_id: int,
    next_role: str = "",
    artifact_id: int = 0,
    prefer_handoff: bool = False,
) -> RelayTransition:
    role_id = str(role or envelope.role or "").strip()
    target = str(envelope.handoff_to or next_role or "").strip()
    base_update: dict[str, Any] = {
        "round_id": int(round_id),
        "artifact_type": envelope.artifact_type,
        "role_statuses": {role_id: _role_status_for_envelope(envelope)},
    }
    if target and prefer_handoff:
        return RelayTransition(
            update={
                **base_update,
                "task_status": "running",
                "role_statuses": {
                    role_id: _handoff_source_status(envelope, prefer_handoff=True),
                    target: "queued",
                },
            },
            goto=target,
            events=[
                {
                    "event_type": "handoff.created",
                    "role": role_id,
                    "round_id": int(round_id),
                    "to_role": target,
                    "summary": envelope.summary,
                }
            ],
        )
    if envelope.status == "waiting" and not target:
        interrupt = _interrupt_from_envelope(envelope, role_id, artifact_id)
        return RelayTransition(
            update={**base_update, "task_status": "waiting_user"},
            goto="waiting_user",
            interrupt=interrupt,
            events=[
                {
                    "event_type": "task.waiting_user",
                    "role": role_id,
                    "round_id": int(round_id),
                    "interrupt": interrupt.to_json_dict(),
                }
            ],
        )
    if envelope.status == "blocked" and not target and _has_human_interrupt(envelope):
        interrupt = _interrupt_from_envelope(envelope, role_id, artifact_id)
        return RelayTransition(
            update={
                **base_update,
                "task_status": "waiting_user",
                "role_statuses": {role_id: "waiting"},
            },
            goto="waiting_user",
            interrupt=interrupt,
            events=[
                {
                    "event_type": "task.waiting_user",
                    "role": role_id,
                    "round_id": int(round_id),
                    "interrupt": interrupt.to_json_dict(),
                }
            ],
        )
    if envelope.status == "blocked":
        return RelayTransition(
            update={**base_update, "task_status": "blocked"},
            goto="blocked",
            terminal="blocked",
            events=[_terminal_event("task.blocked", role_id, round_id, envelope)],
        )
    if envelope.status == "failed":
        return RelayTransition(
            update={**base_update, "task_status": "failed"},
            goto="blocked",
            terminal="failed",
            events=[_terminal_event("task.failed", role_id, round_id, envelope)],
        )
    if (
        envelope.status == "passed"
        and role_id == "director"
        and envelope.artifact_type == "final_summary"
        and not target
    ):
        return RelayTransition(
            update={**base_update, "task_status": "completed"},
            goto="completed",
            terminal="completed",
            events=[_terminal_event("task.completed", role_id, round_id, envelope)],
        )
    if target:
        return RelayTransition(
            update={
                **base_update,
                "task_status": "running",
                "role_statuses": {
                    role_id: _handoff_source_status(envelope),
                    target: "queued",
                },
            },
            goto=target,
            events=[
                {
                    "event_type": "handoff.created",
                    "role": role_id,
                    "round_id": int(round_id),
                    "to_role": target,
                    "summary": envelope.summary,
                }
            ],
        )
    if envelope.status == "waiting":
        return RelayTransition(
            update={**base_update, "task_status": "waiting_user"},
            goto="waiting_user",
            events=[
                {
                    "event_type": "task.waiting_user",
                    "role": role_id,
                    "round_id": int(round_id),
                }
            ],
        )
    return RelayTransition(update=base_update, goto="")


def transition_from_round_control(
    *,
    decision: str,
    role: str = "",
    round_id: int,
    next_role: str = "",
) -> RelayTransition:
    decision_id = str(decision or "").strip()
    role_id = str(role or "").strip()
    target = str(next_role or role_id or "").strip()
    if decision_id == "cancel_plan":
        return RelayTransition(
            update={"round_id": int(round_id), "task_status": "interrupted"},
            goto="interrupted",
            terminal="interrupted",
            events=[
                {
                    "event_type": "round.control",
                    "decision": decision_id,
                    "round_id": int(round_id),
                }
            ],
        )
    if decision_id in {"continue", "revise_plan"}:
        return RelayTransition(
            update={
                "round_id": int(round_id),
                "task_status": "running",
                "role_statuses": {role_id: "queued"} if role_id else {},
            },
            goto=role_id,
            events=[
                {
                    "event_type": "round.control",
                    "decision": decision_id,
                    "round_id": int(round_id),
                    "role": role_id,
                },
                {
                    "event_type": "role.queued",
                    "role": role_id,
                    "round_id": int(round_id),
                },
            ],
        )
    if decision_id == "approve_plan":
        role_statuses = {role_id: "passed"} if role_id else {}
        if target:
            role_statuses[target] = "queued"
        return RelayTransition(
            update={
                "round_id": int(round_id),
                "task_status": "running",
                "role_statuses": role_statuses,
            },
            goto=target,
            events=[
                {
                    "event_type": "round.control",
                    "decision": decision_id,
                    "round_id": int(round_id),
                    "role": role_id,
                    "next_role": target,
                }
            ],
        )
    return RelayTransition(
        update={"round_id": int(round_id)},
        events=[
            {
                "event_type": "round.control",
                "decision": decision_id,
                "round_id": int(round_id),
            }
        ],
    )


def build_marvis_relay_state(
    detail: RelayTaskDetail,
    *,
    round_id: int | None = None,
) -> MarvisRelayState:
    detail_round = int(detail.current_round_id or 1)
    current_round = int(round_id or detail_round)
    artifacts = [
        _state_artifact(artifact)
        for artifact in detail.artifacts
        if int(artifact.get("round_id") or current_round) == current_round
    ]
    handoffs = [
        _state_handoff(artifact)
        for artifact in artifacts
        if artifact.get("artifact_type") == "handoff_packet"
    ]
    routing = detail.routing_decision or {}
    role_statuses = {job.role: job.status for job in detail.role_jobs}
    active_role = _active_role(detail, role_statuses)
    pending_interrupt = _pending_interrupt(detail, artifacts, active_role)
    terminal_status = detail.task.status if detail.task.status in _TERMINAL_TASK_STATUSES else ""
    return MarvisRelayState(
        task_id=detail.task.id,
        round_id=current_round,
        current_node=_current_node(detail.task.status, routing, active_role, pending_interrupt),
        route=str(routing.get("route") or ""),
        required_roles=_clean_string_list(routing.get("required_roles")),
        role_statuses=role_statuses,
        active_role=active_role,
        latest_user_input=detail.board.latest_user_input or detail.task.prompt,
        handoffs=handoffs,
        artifacts=artifacts,
        pending_interrupt=pending_interrupt,
        terminal_status=terminal_status,
    )


def _role_status_for_envelope(envelope: RoleEnvelope) -> str:
    if envelope.status == "passed":
        return "passed"
    if envelope.status == "waiting" and envelope.handoff_to:
        return "passed"
    return envelope.status


def _handoff_source_status(envelope: RoleEnvelope, *, prefer_handoff: bool = False) -> str:
    if prefer_handoff and envelope.status in {"blocked", "failed"}:
        return envelope.status
    return "passed"


def _has_human_interrupt(envelope: RoleEnvelope) -> bool:
    return bool(envelope.open_questions or envelope.confirmation_options)


def _interrupt_from_envelope(
    envelope: RoleEnvelope,
    role: str,
    artifact_id: int,
) -> RelayInterrupt:
    return RelayInterrupt(
        kind=_interrupt_kind(envelope),
        role=role,
        reason=envelope.reason,
        artifact_type=envelope.artifact_type,
        artifact_id=int(artifact_id or 0),
        open_questions=list(envelope.open_questions),
        confirmation_options=[dict(option) for option in envelope.confirmation_options],
        source="role_envelope",
        payload=envelope.to_json_dict(),
    )


def _interrupt_kind(envelope: RoleEnvelope) -> str:
    if envelope.status == "blocked" and envelope.open_questions:
        return "blocked_question"
    if envelope.artifact_type == "architecture_plan":
        return "plan_approval"
    if envelope.confirmation_options:
        return "confirmation_options"
    return "user_input"


def _terminal_event(
    event_type: str,
    role: str,
    round_id: int,
    envelope: RoleEnvelope,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "role": role,
        "round_id": int(round_id),
        "summary": envelope.summary,
    }


def _active_role(detail: RelayTaskDetail, role_statuses: dict[str, str]) -> str:
    confirmation = _confirmation(detail)
    confirmation_role = str(confirmation.get("role") or "").strip()
    if detail.task.status == "waiting_user" and confirmation_role:
        return confirmation_role
    for status in _ACTIVE_ROLE_STATUSES:
        for job in detail.role_jobs:
            if role_statuses.get(job.role) == status:
                return job.role
    return ""


def _current_node(
    task_status: str,
    routing: dict[str, Any],
    active_role: str,
    pending_interrupt: RelayInterrupt | None,
) -> str:
    if pending_interrupt is not None:
        return "waiting_user"
    if task_status == "completed":
        return "completed"
    if task_status in {"blocked", "failed"}:
        return "blocked"
    if task_status == "interrupted":
        return "interrupted"
    if task_status == "superseded":
        return "interrupted"
    if active_role:
        return active_role
    if not routing:
        return "director_routing"
    return "director_final"


def _pending_interrupt(
    detail: RelayTaskDetail,
    artifacts: list[dict[str, Any]],
    active_role: str,
) -> RelayInterrupt | None:
    if detail.task.status in _TERMINAL_TASK_STATUSES:
        return None
    execution = detail.round_execution if isinstance(detail.round_execution, dict) else {}
    waiting_reason = str(execution.get("waiting_reason") or "none").strip() or "none"
    confirmation = _confirmation(detail)
    role = str(confirmation.get("role") or active_role or "").strip()
    has_confirmation = any(
        str(confirmation.get(key) or "").strip()
        for key in ("source", "kind", "role", "provider_request_id")
    )
    if waiting_reason == "none" and not has_confirmation:
        return None
    artifact = _latest_waiting_artifact(artifacts, role)
    if not artifact and waiting_reason == "none":
        return None
    artifact_type = str(artifact.get("artifact_type") or "")
    kind = str(confirmation.get("kind") or "").strip()
    if not kind:
        kind = "plan_approval" if artifact_type == "architecture_plan" else waiting_reason
    if kind in {"", "none", "relay_question"}:
        if waiting_reason not in {"", "none", "relay_question"}:
            kind = waiting_reason
        else:
            kind = "plan_approval" if artifact_type == "architecture_plan" else "user_input"
    return RelayInterrupt(
        kind=kind,
        role=role,
        reason=waiting_reason,
        artifact_type=artifact_type,
        artifact_id=int(artifact.get("id") or 0),
        open_questions=_clean_string_list(artifact.get("open_questions")),
        confirmation_options=[
            dict(option)
            for option in artifact.get("confirmation_options", [])
            if isinstance(option, dict)
        ],
        source=str(confirmation.get("source") or ""),
        payload=dict(confirmation),
    )


def _confirmation(detail: RelayTaskDetail) -> dict[str, Any]:
    execution = detail.round_execution if isinstance(detail.round_execution, dict) else {}
    confirmation = execution.get("confirmation")
    return dict(confirmation) if isinstance(confirmation, dict) else {}


def _latest_waiting_artifact(artifacts: list[dict[str, Any]], role: str) -> dict[str, Any]:
    for artifact in reversed(artifacts):
        if role and str(artifact.get("relay_role") or artifact.get("role") or "") != role:
            continue
        artifact_type = str(artifact.get("artifact_type") or "")
        if artifact_type in {"relay_board", "handoff_packet", "routing_decision"}:
            continue
        if artifact_type == "user_followup":
            return {}
        if str(artifact.get("status") or "") == "waiting":
            return artifact
        return {}
    return {}


def _state_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "artifact_type",
        "relay_role",
        "role",
        "status",
        "summary",
        "handoff_to",
        "from_role",
        "to_role",
        "round_id",
        "open_questions",
        "confirmation_options",
        "created_at",
    )
    return {key: artifact[key] for key in keys if key in artifact}


def _state_handoff(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": int(artifact.get("id") or 0),
        "from_role": str(artifact.get("from_role") or artifact.get("relay_role") or ""),
        "to_role": str(artifact.get("to_role") or artifact.get("handoff_to") or ""),
        "summary": str(artifact.get("summary") or ""),
        "round_id": int(artifact.get("round_id") or 1),
    }


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
