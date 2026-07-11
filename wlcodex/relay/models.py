from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from wlcodex.relay.artifact_types import ALL_RELAY_ARTIFACT_TYPES
from wlcodex.presentation_contract import build_presentation_payload


@dataclass(frozen=True)
class RelayRoleDefinition:
    role: str
    display_name: str


RELAY_ROLES: tuple[RelayRoleDefinition, ...] = (
    RelayRoleDefinition("director", "总工程师"),
    RelayRoleDefinition("architect", "架构工程师"),
    RelayRoleDefinition("implementer", "开发工程师"),
    RelayRoleDefinition("tester", "测试工程师"),
    RelayRoleDefinition("auditor", "审核工程师"),
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
RELAY_ARTIFACT_TYPES = ALL_RELAY_ARTIFACT_TYPES
RELAY_EXECUTION_MODES = (
    "standard",
    "plan_first",
    "goal",
)
RELAY_PRESENTATION_STATES = (
    "running",
    "waiting_user",
    "waiting_approval",
    "blocked",
    "completed",
    "interrupted",
    "failed",
    "stale",
)
GOAL_ACCEPTANCE_STATUSES = (
    "passed",
    "failed",
    "not_run",
)
GOAL_ACCEPTANCE_TEST_KINDS = (
    "pytest",
    "unittest",
    "npm_test",
    "pnpm_test",
)


def normalize_relay_execution_mode(value: Any) -> str:
    """Return the durable execution contract for new and legacy Relay records."""

    mode = str(value or "").strip().lower()
    legacy_modes = {
        "": "standard",
        "simple": "standard",
        "auto": "standard",
        "team": "standard",
        "standard": "standard",
        "plan": "plan_first",
        "plan_first": "plan_first",
        "goal": "goal",
    }
    return legacy_modes.get(mode, "standard")


def normalize_acceptance_criteria(values: Any) -> list[str]:
    """Keep a small, ordered and de-duplicated public acceptance contract."""

    if not isinstance(values, list | tuple):
        return []
    criteria: list[str] = []
    for value in values:
        criterion = str(value or "").strip()
        if criterion and criterion not in criteria:
            criteria.append(criterion)
    return criteria


def normalize_goal_acceptance_declaration(
    value: Any,
) -> tuple[dict[str, Any], str]:
    """Validate the only provider-supplied input accepted by goal verification.

    Relay never interprets a model-produced shell command.  A verifier can
    bind itself to an implementation run and select one explicitly supported
    test *kind* with structured arguments.  The controlled executor turns the
    declaration into an argv list later, with the task workspace as its cwd.
    """

    if not isinstance(value, dict):
        return {}, "goal_acceptance must be an object"
    unknown = sorted(set(value) - {"implementation_run_id", "test", "criteria"})
    if unknown:
        return {}, "goal_acceptance has unsupported fields: " + ", ".join(unknown)
    raw_run_id = value.get("implementation_run_id")
    if isinstance(raw_run_id, bool) or not isinstance(raw_run_id, int) or raw_run_id <= 0:
        return {}, "goal_acceptance requires a positive implementation_run_id"
    normalized: dict[str, Any] = {"implementation_run_id": int(raw_run_id)}
    if "criteria" in value:
        raw_criteria = value.get("criteria")
        if not isinstance(raw_criteria, list) or not raw_criteria:
            return {}, "goal_acceptance.criteria must be a non-empty list"
        criteria = normalize_acceptance_criteria(raw_criteria)
        if len(criteria) != len(raw_criteria) or len(criteria) > 64:
            return {}, "goal_acceptance.criteria must contain at most 64 unique non-empty strings"
        normalized["criteria"] = criteria
    if "test" not in value or value.get("test") is None:
        return normalized, ""
    raw_test = value.get("test")
    if not isinstance(raw_test, dict):
        return {}, "goal_acceptance.test must be an object"
    unknown_test = sorted(set(raw_test) - {"kind", "args", "script"})
    if unknown_test:
        return {}, "goal_acceptance.test has unsupported fields: " + ", ".join(unknown_test)
    kind = str(raw_test.get("kind") or "").strip()
    if kind not in GOAL_ACCEPTANCE_TEST_KINDS:
        return {}, "goal_acceptance.test.kind is not an approved test kind"
    raw_args = raw_test.get("args", [])
    if not isinstance(raw_args, list) or len(raw_args) > 32:
        return {}, "goal_acceptance.test.args must be a list of at most 32 values"
    args: list[str] = []
    for raw_arg in raw_args:
        if not isinstance(raw_arg, str):
            return {}, "goal_acceptance.test.args must contain only strings"
        arg = raw_arg.strip()
        if not arg or len(arg) > 512:
            return {}, "goal_acceptance.test.args contains an invalid value"
        args.append(arg)
    script = str(raw_test.get("script") or "").strip()
    if kind in {"npm_test", "pnpm_test"}:
        if args:
            return {}, "package-manager goal tests do not accept free-form args"
        if script not in {"test", "test:e2e"}:
            return {}, "package-manager goal tests require an approved script"
    elif script:
        return {}, "python goal tests do not accept a package script"
    normalized["test"] = {
        "kind": kind,
        "args": args,
        **({"script": script} if script else {}),
    }
    return normalized, ""


def _clean_list(values: list[Any] | tuple[Any, ...] | None) -> list[str]:
    if not values:
        return []
    return [str(value) for value in values if str(value).strip()]


def _clean_confirmation_options(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    options: list[dict[str, str]] = []
    for index, item in enumerate(values[:6], start=1):
        if isinstance(item, str):
            label = item.strip()
            summary = ""
            instruction = label
            option_id = f"option_{index}"
        elif isinstance(item, dict):
            option_id = str(item.get("id") or f"option_{index}").strip()
            label = str(
                item.get("label")
                or item.get("title")
                or item.get("name")
                or item.get("summary")
                or option_id
            ).strip()
            summary = str(item.get("summary") or item.get("description") or "").strip()
            instruction = str(
                item.get("instruction")
                or item.get("prompt")
                or item.get("value")
                or item.get("text")
                or label
            ).strip()
        else:
            continue
        if not label and not instruction:
            continue
        if not option_id:
            option_id = f"option_{index}"
        options.append(
            {
                "id": option_id,
                "label": label or instruction,
                "summary": summary,
                "instruction": instruction or label,
            }
        )
    return options


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
    role_providers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GoalAcceptanceRecord:
    """Durable, run-bound outcome of one goal-mode verification attempt."""

    id: int
    task_id: int
    round_id: int
    implementation_artifact_id: int | None
    implementation_run_id: int | None
    verifier_artifact_id: int | None
    verifier_role: str
    attempt_no: int
    test_declaration: dict[str, Any] = field(default_factory=dict)
    test_execution: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    status: str = "not_run"
    evidence_status: str = "not_run"
    reason: str = ""
    created_at: str = ""
    updated_at: str = ""

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
    error_message: str = ""
    idle_reason: str = ""
    updated_at: str = ""

    @property
    def display_name(self) -> str:
        return RELAY_ROLE_DISPLAY_NAMES.get(self.role, self.role)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["display_name"] = self.display_name
        return data


@dataclass(frozen=True)
class RelayPendingInput:
    id: int
    task_id: int
    queued_after_round_id: int
    status: str
    text: str
    attachments: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    consumed_round_id: int | None = None
    steered_round_id: int | None = None
    steered_role: str = ""
    steered_attempt_no: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelayPendingInputClaim:
    """An owned, durable lease for consuming one queued Relay follow-up.

    The pending input remains the historical source record.  A claim is only
    present while a worker may still create the follow-up round or dispatch
    its director.  Releasing or consuming the claim removes the lease row so
    a maintenance drain never mistakes historical inputs for live work.
    """

    pending_input: RelayPendingInput
    workspace: str
    lease_owner: str
    lease_expires_at: str
    attempt_count: int = 1


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
class RelayPresentation:
    """Pure, user-facing projection shared by every Relay surface.

    The Relay task, role jobs and artifacts remain the source records.  This
    object is deliberately a projection, so rendering it never needs to
    reconcile or mutate any lifecycle record.
    """

    state: str
    freshness: dict[str, Any]
    current_actor: dict[str, str]
    blocking_reason: str
    next_action: str
    allowed_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return build_presentation_payload(
            state=self.state,
            freshness=self.freshness,
            current_actor=self.current_actor,
            blocking_reason=self.blocking_reason,
            next_action=self.next_action,
            allowed_actions=self.allowed_actions,
        )


def build_relay_presentation(
    *,
    task: RelayTask,
    role_jobs: list[RelayRoleJob],
    board: RelayBoard,
    round_execution: dict[str, Any] | None = None,
    latest_handoff: dict[str, Any] | None = None,
    source: str = "relay_lifecycle",
) -> RelayPresentation:
    """Project lifecycle truth without changing it.

    The mapping intentionally compresses transport-specific role/job statuses
    into a stable user vocabulary.  Callers can still expose the raw records
    alongside this projection for expert/debug views.
    """

    execution = round_execution if isinstance(round_execution, dict) else {}
    raw_status = str(task.status or "").strip()
    confirmation = execution.get("confirmation")
    if not isinstance(confirmation, dict):
        confirmation = {}
    waiting_reason = str(execution.get("waiting_reason") or "").strip()
    confirmation_kind = str(confirmation.get("kind") or "").strip()
    confirmation_source = str(confirmation.get("source") or "").strip()
    recovery_required = confirmation_source in {
        "provider_native_resolving",
        "provider_native_superseding",
    } or any(
        str(job.error_message or "").find("派发结果无法验证：") >= 0
        or str(job.error_message or "").find("任务只完成了部分中断：") >= 0
        for job in role_jobs
    )
    approval_wait = waiting_reason in {
        "plan_approval",
        "provider_approval",
    } or confirmation_kind.endswith("_approval")
    state = {
        "queued": "running",
        "running": "running",
        "waiting_user": "waiting_approval" if approval_wait else "waiting_user",
        "blocked": "blocked",
        "failed": "failed",
        "completed": "completed",
        "interrupted": "interrupted",
    }.get(raw_status, "stale")
    # The provider may have accepted an approval action just before a process
    # crash, while Relay has not yet durably finalized its own projection.  Do
    # not expose that temporary claim as another actionable approval: replay
    # would risk authorizing or cancelling the same request twice.
    if recovery_required:
        state = "blocked"
    updated_at = str(task.updated_at or "").strip()
    # Only an actively executing task requires a continuing provider
    # heartbeat. Terminal, blocked, and user/approval-waiting records are
    # durable states: their age is useful evidence, not stale execution truth.
    stale_reason = _presentation_stale_reason(updated_at) if state == "running" else ""
    if stale_reason and state == "running":
        state = "stale"
    if state == "stale" and not stale_reason:
        stale_reason = f"未知的 Relay 状态：{raw_status or 'empty'}"
    freshness = {
        "source": source,
        "updated_at": updated_at,
        "is_stale": state == "stale" or bool(stale_reason),
        "reason": stale_reason,
        "recovery_required": recovery_required,
        "recovery_state": "needs_recovery" if recovery_required else "",
    }

    actor = _presentation_current_actor(role_jobs, state=state)
    blocking_reason = _presentation_blocking_reason(
        role_jobs,
        state=state,
        waiting_reason=waiting_reason,
        board=board,
        recovery_required=recovery_required,
        recovery_source=confirmation_source,
    )
    next_action = _presentation_next_action(
        state=state,
        board=board,
        latest_handoff=latest_handoff,
        actor=actor,
        blocking_reason=blocking_reason,
        recovery_required=recovery_required,
        recovery_source=confirmation_source,
    )
    return RelayPresentation(
        state=state,
        freshness=freshness,
        current_actor=actor,
        blocking_reason=blocking_reason,
        next_action=next_action,
        allowed_actions=_presentation_allowed_actions(
            state,
            recovery_required=recovery_required,
        ),
    )


def _presentation_stale_reason(updated_at: str) -> str:
    if not updated_at:
        return "缺少可验证的最后更新时间"
    try:
        observed_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return "最后更新时间格式无法验证"
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - observed_at > timedelta(minutes=30):
        return "超过 30 分钟未收到新的 Relay 状态"
    return ""


def _presentation_current_actor(
    role_jobs: list[RelayRoleJob],
    *,
    state: str,
) -> dict[str, str]:
    preferred_statuses = {
        "blocked": ("blocked", "failed", "waiting", "streaming", "queued"),
        "failed": ("failed", "blocked", "waiting", "streaming", "queued"),
        "waiting_user": ("waiting", "streaming", "queued"),
        "waiting_approval": ("waiting", "streaming", "queued"),
    }.get(state, ("streaming", "queued", "waiting", "blocked", "failed"))
    for status in preferred_statuses:
        for job in role_jobs:
            if str(job.status or "") == status:
                return {
                    "role": job.role,
                    "label": job.display_name,
                    "status": status,
                }
    return {"role": "", "label": "", "status": ""}


def _presentation_blocking_reason(
    role_jobs: list[RelayRoleJob],
    *,
    state: str,
    waiting_reason: str,
    board: RelayBoard,
    recovery_required: bool = False,
    recovery_source: str = "",
) -> str:
    if recovery_required:
        for job in role_jobs:
            if job.error_message:
                return str(job.error_message)
        if recovery_source in {
            "provider_native_resolving",
            "provider_native_superseding",
        }:
            return "原生审批操作尚未获得可验证回执；为避免重复授权，任务需要恢复。"
        return "原生操作尚未获得可验证回执；为避免重复执行，任务需要恢复。"
    if state in {"blocked", "failed"}:
        # A task-level guardrail can deliberately leave the affected role's
        # raw attempt as ``streaming`` while the task is blocked.  Its error is
        # still the authoritative reason; filtering by the projected role
        # status here would hide a partial external-control failure.
        for job in role_jobs:
            if job.error_message:
                return str(job.error_message)
        for job in role_jobs:
            if str(job.status or "") in {"blocked", "failed"} and job.error_message:
                return str(job.error_message)
        return "任务已停止，需要恢复或补充信息。"
    if state in {"waiting_user", "waiting_approval"}:
        if board.open_questions:
            return "；".join(str(item) for item in board.open_questions if str(item).strip())
        return waiting_reason or "等待用户确认。"
    return ""


def _presentation_next_action(
    *,
    state: str,
    board: RelayBoard,
    latest_handoff: dict[str, Any] | None,
    actor: dict[str, str],
    blocking_reason: str,
    recovery_required: bool = False,
    recovery_source: str = "",
) -> str:
    if recovery_required:
        if recovery_source in {
            "provider_native_resolving",
            "provider_native_superseding",
        }:
            return "等待系统恢复审批回执；未确认前不要再次授权或取消。"
        return "查看 Provider 侧证据并确认回执；未确认前不要重派或再次中断。"
    if state == "waiting_approval":
        return "审阅当前方案或审批请求后确认。"
    if state == "waiting_user":
        return "补充必要信息后继续。"
    if state in {"blocked", "failed"}:
        return "恢复任务或补充信息后重试。"
    if state == "completed":
        return "任务已完成；可发送后续需求。"
    if state == "interrupted":
        return "任务已中断；可恢复或创建后续任务。"
    if state == "stale":
        return "刷新运行状态或检查同步来源。"
    if board.next_step:
        return str(board.next_step)
    if isinstance(latest_handoff, dict) and latest_handoff.get("next_action"):
        return str(latest_handoff["next_action"])
    if actor.get("label"):
        return f"等待{actor['label']}处理。"
    return blocking_reason or "等待系统更新。"


def _presentation_allowed_actions(
    state: str,
    *,
    recovery_required: bool = False,
) -> list[str]:
    # A provider-side approval may already have crossed the network boundary.
    # Until its durable acknowledgement is reconciled, a role resume would
    # create a fresh turn and turn an uncertain approval into duplicate work.
    # Refresh only schedules the idempotent lifecycle reconciler.
    if recovery_required:
        return ["refresh"]
    if state == "running":
        return ["add_input", "interrupt"]
    if state in {"waiting_user", "waiting_approval"}:
        return ["add_input", "resolve"]
    if state in {"blocked", "failed"}:
        return ["resume", "add_input", "archive"]
    if state in {"completed", "interrupted"}:
        return ["add_input", "archive"]
    return ["refresh"]


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
    confirmation_options: list[dict[str, str]] = field(default_factory=list)

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
            confirmation_options=_clean_confirmation_options(
                payload.get("confirmation_options")
            ),
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
    role_providers: dict[str, str]
    last_activity_at: str
    presentation: RelayPresentation = field(
        default_factory=lambda: RelayPresentation(
            state="stale",
            freshness={
                "source": "relay_lifecycle",
                "updated_at": "",
                "is_stale": True,
                "reason": "缺少任务投影",
            },
            current_actor={"role": "", "label": "", "status": ""},
            blocking_reason="",
            next_action="刷新运行状态或检查同步来源。",
            allowed_actions=["refresh"],
        )
    )

    @classmethod
    def from_task(
        cls,
        task: RelayTask,
        *,
        role_statuses: dict[str, str],
        role_providers: dict[str, str] | None = None,
        director_decision_summary: str = "",
        latest_handoff_summary: str = "",
        last_activity_at: str | None = None,
        presentation: RelayPresentation | None = None,
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
            role_providers=dict(role_providers or task.role_providers),
            last_activity_at=last_activity_at or task.updated_at,
            presentation=presentation
            or build_relay_presentation(
                task=task,
                role_jobs=[],
                board=RelayBoard(
                    task_id=task.id,
                    current_goal=task.prompt,
                    phase=task.phase,
                ),
            ),
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
    routing_decision: dict[str, Any] | None = None
    current_round_id: int = 1
    pending_inputs: list[RelayPendingInput] = field(default_factory=list)
    round_execution: dict[str, Any] = field(default_factory=dict)
    goal_acceptance_records: list[GoalAcceptanceRecord] = field(default_factory=list)
    presentation: RelayPresentation = field(
        default_factory=lambda: RelayPresentation(
            state="stale",
            freshness={
                "source": "relay_lifecycle",
                "updated_at": "",
                "is_stale": True,
                "reason": "缺少任务投影",
            },
            current_actor={"role": "", "label": "", "status": ""},
            blocking_reason="",
            next_action="刷新运行状态或检查同步来源。",
            allowed_actions=["refresh"],
        )
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "board": self.board.to_json_dict(),
            "role_jobs": [job.to_dict() for job in self.role_jobs],
            "artifacts": self.artifacts,
            "latest_handoff": (self.latest_handoff.to_json_dict() if self.latest_handoff else None),
            "session_links": [link.to_dict() for link in self.session_links],
            "routing_decision": self.routing_decision,
            "current_round_id": self.current_round_id,
            "pending_inputs": [item.to_dict() for item in self.pending_inputs],
            "round_execution": self.round_execution,
            "goal_acceptance_records": [
                record.to_dict() for record in self.goal_acceptance_records
            ],
            "presentation": self.presentation.to_dict(),
        }


@dataclass(frozen=True)
class EnvelopeParseResult:
    ok: bool
    envelope: RoleEnvelope | None = None
    next_role: str | None = None
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
