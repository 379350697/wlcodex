from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RelayRoleDefinition:
    role: str
    display_name: str


RELAY_ROLES: tuple[RelayRoleDefinition, ...] = (
    RelayRoleDefinition("director", "总工程师"),
    RelayRoleDefinition("architect", "架构工程师"),
    RelayRoleDefinition("implementer", "开发工程师"),
    RelayRoleDefinition("tester", "测试工程师"),
    RelayRoleDefinition("auditor", "审计工程师"),
)
RELAY_ROLE_IDS = tuple(role.role for role in RELAY_ROLES)
RELAY_ROLE_DISPLAY_NAMES = {role.role: role.display_name for role in RELAY_ROLES}

RELAY_TASK_STATUSES = (
    "queued",
    "running",
    "waiting_user",
    "blocked",
    "failed",
    "completed",
    "interrupted",
)
RELAY_ROLE_JOB_STATUSES = (
    "idle",
    "queued",
    "streaming",
    "waiting",
    "passed",
    "failed",
    "blocked",
    "interrupted",
)
RELAY_ARTIFACT_TYPES = (
    "relay_board",
    "routing_decision",
    "architecture_plan",
    "implementation_report",
    "test_report",
    "audit_report",
    "handoff_packet",
    "final_summary",
)


def _clean_list(values: list[Any] | tuple[Any, ...] | None) -> list[str]:
    if not values:
        return []
    return [str(value) for value in values if str(value).strip()]


@dataclass(frozen=True)
class RelayTask:
    id: int
    title: str
    prompt: str
    workspace: str
    provider: str
    status: str
    phase: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelayRoleJob:
    id: int
    task_id: int
    role: str
    status: str
    provider: str = ""
    provider_engine: str = ""
    model: str = ""
    native_session_id: str = ""
    agent_run_id: int | None = None
    turn_id: str = ""
    active_turn_id: str = ""
    turn_running: bool = False
    dispatch_verified: bool = False
    fallback_reason: str = ""
    output: str = ""
    latest_handoff_summary: str = ""
    open_questions: list[str] = field(default_factory=list)
    updated_at: str = ""

    @property
    def display_name(self) -> str:
        return RELAY_ROLE_DISPLAY_NAMES.get(self.role, self.role)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["display_name"] = self.display_name
        return data


@dataclass(frozen=True)
class RelayBoard:
    task_id: int
    current_goal: str
    phase: str
    latest_user_input: str = ""
    confirmed_facts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    current_dispatch: str = ""
    next_step: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoleContextPacket:
    task_id: int
    role: str
    workspace: str
    current_goal: str
    phase: str
    latest_user_input: str
    confirmed_facts: list[str]
    role_relevant_artifacts: list[dict[str, Any]]
    handoff_summaries: list[str]
    constraints: list[str]
    expected_output_envelope: dict[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoleEnvelope:
    status: str
    reason: str
    role: str
    artifact_type: str
    handoff_to: str
    summary: str
    evidence_refs: list[str]
    open_questions: list[str]
    next_action: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RoleEnvelope":
        return cls(
            status=str(payload.get("status", "")).strip(),
            reason=str(payload.get("reason", "")).strip(),
            role=str(payload.get("role", "")).strip(),
            artifact_type=str(payload.get("artifact_type", "")).strip(),
            handoff_to=str(payload.get("handoff_to", "")).strip(),
            summary=str(payload.get("summary", "")).strip(),
            evidence_refs=_clean_list(payload.get("evidence_refs")),
            open_questions=_clean_list(payload.get("open_questions")),
            next_action=str(payload.get("next_action", "")).strip(),
        )

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HandoffPacket:
    from_role: str
    to_role: str
    summary: str
    confirmed_facts: list[str]
    open_questions: list[str]
    evidence_refs: list[str]
    next_action: str

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "HandoffPacket":
        return cls(
            from_role=str(payload.get("from_role", "")).strip(),
            to_role=str(payload.get("to_role", "")).strip(),
            summary=str(payload.get("summary", "")).strip(),
            confirmed_facts=_clean_list(payload.get("confirmed_facts")),
            open_questions=_clean_list(payload.get("open_questions")),
            evidence_refs=_clean_list(payload.get("evidence_refs")),
            next_action=str(payload.get("next_action", "")).strip(),
        )


@dataclass(frozen=True)
class RelaySessionLink:
    role: str
    provider: str
    native_session_id: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelayTaskSummary:
    task_id: int
    title: str
    workspace: str
    status: str
    phase: str
    provider: str
    director_decision_summary: str
    latest_handoff_summary: str
    role_statuses: dict[str, str]
    last_activity_at: str

    @classmethod
    def from_task(
        cls,
        task: RelayTask,
        *,
        role_statuses: dict[str, str],
        director_decision_summary: str = "",
        latest_handoff_summary: str = "",
    ) -> "RelayTaskSummary":
        return cls(
            task_id=task.id,
            title=task.title,
            workspace=task.workspace,
            status=task.status,
            phase=task.phase,
            provider=task.provider,
            director_decision_summary=director_decision_summary,
            latest_handoff_summary=latest_handoff_summary,
            role_statuses=role_statuses,
            last_activity_at=task.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelayTaskDetail:
    task: RelayTask
    board: RelayBoard
    role_jobs: list[RelayRoleJob]
    artifacts: list[dict[str, Any]]
    latest_handoff: HandoffPacket | None
    session_links: list[RelaySessionLink]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "board": self.board.to_json_dict(),
            "role_jobs": [job.to_dict() for job in self.role_jobs],
            "artifacts": self.artifacts,
            "latest_handoff": (
                self.latest_handoff.to_json_dict() if self.latest_handoff else None
            ),
            "session_links": [link.to_dict() for link in self.session_links],
        }


@dataclass(frozen=True)
class EnvelopeParseResult:
    ok: bool
    envelope: RoleEnvelope | None = None
    next_role: str | None = None
    error: str = ""
