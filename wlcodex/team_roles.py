from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RoleId(StrEnum):
    DIRECTOR = "director"
    INVESTIGATOR = "investigator"
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    TESTER = "tester"
    AUDITOR = "auditor"


class TeamRouteKind(StrEnum):
    FEATURE = "feature"
    BUG = "bug"


@dataclass(frozen=True)
class TeamRouteDecision:
    kind: TeamRouteKind
    first_role: RoleId
    reason: str
    matched_signals: tuple[str, ...] = ()


BUG_ROUTE_SIGNALS: tuple[str, ...] = (
    "报错",
    "失败",
    "不对",
    "修复",
    "bug",
    "regression",
    "回归",
    "异常",
    "why",
    "为什么",
    "crash",
    "broken",
    "fail",
    "error",
    "stacktrace",
    "日志",
    "验收不过",
    "测试不过",
)

FEATURE_ROUTE_SIGNALS: tuple[str, ...] = (
    "新增",
    "实现一个",
    "支持",
    "设计",
    "复杂需求",
    "功能",
    "feature",
    "build",
    "add",
    "workflow redesign",
)


def classify_team_route(goal: str) -> TeamRouteDecision:
    text = goal.lower()
    bug_hits = tuple(signal for signal in BUG_ROUTE_SIGNALS if signal.lower() in text)
    feature_hits = tuple(
        signal for signal in FEATURE_ROUTE_SIGNALS if signal.lower() in text
    )
    if bug_hits and not feature_hits:
        return TeamRouteDecision(
            kind=TeamRouteKind.BUG,
            first_role=RoleId.INVESTIGATOR,
            reason="bug_signals",
            matched_signals=bug_hits,
        )
    if feature_hits and not bug_hits:
        return TeamRouteDecision(
            kind=TeamRouteKind.FEATURE,
            first_role=RoleId.ARCHITECT,
            reason="feature_signals",
            matched_signals=feature_hits,
        )
    if feature_hits and bug_hits:
        if any(signal in text for signal in ("失败", "报错", "error", "fail", "回归")):
            return TeamRouteDecision(
                kind=TeamRouteKind.BUG,
                first_role=RoleId.INVESTIGATOR,
                reason="mixed_signals_bug_first",
                matched_signals=bug_hits + feature_hits,
            )
        return TeamRouteDecision(
            kind=TeamRouteKind.FEATURE,
            first_role=RoleId.ARCHITECT,
            reason="mixed_signals_feature_first",
            matched_signals=feature_hits + bug_hits,
        )
    return TeamRouteDecision(
        kind=TeamRouteKind.BUG,
        first_role=RoleId.INVESTIGATOR,
        reason="ambiguous_defaults_to_diagnosis",
        matched_signals=(),
    )


@dataclass(frozen=True)
class TeamRole:
    role_id: RoleId
    display_name: str
    mission: str
    instructions: str
    skills: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    required_artifact_type: str
    can_write_product_code: bool = False
    expert_stance: str = ""
    expert_priorities: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    anti_patterns: tuple[str, ...] = ()
    handoff_focus: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelProfileRef:
    profile_id: str


@dataclass(frozen=True)
class AssignmentPolicy:
    defaults: dict[RoleId, tuple[ModelProfileRef, ...]] = field(default_factory=dict)
    high_risk_overrides: dict[RoleId, tuple[ModelProfileRef, ...]] = field(default_factory=dict)
    role_skills: dict[RoleId, tuple[str, ...]] = field(default_factory=dict)
    role_capabilities: dict[RoleId, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "defaults", _profile_ref_mapping(self.defaults))
        object.__setattr__(
            self,
            "high_risk_overrides",
            _profile_ref_mapping(self.high_risk_overrides),
        )
        object.__setattr__(self, "role_skills", _tuple_mapping(self.role_skills))
        object.__setattr__(self, "role_capabilities", _tuple_mapping(self.role_capabilities))

    def choices_for(self, role_id: RoleId, risk_level: str = "medium") -> list[ModelProfileRef]:
        if risk_level in {"high", "critical"} and role_id in self.high_risk_overrides:
            return list(self.high_risk_overrides[role_id])
        return list(self.defaults.get(role_id, []))

    def requires_user_choice(self, role_id: RoleId, risk_level: str = "medium") -> bool:
        return len(self.choices_for(role_id, risk_level)) > 1

    def skills_for(self, role_id: RoleId) -> tuple[str, ...]:
        return self.role_skills.get(role_id, ())

    def capabilities_for(self, role_id: RoleId) -> tuple[str, ...]:
        return self.role_capabilities.get(role_id, ())


def _profile_ref_mapping(
    values: dict[RoleId, tuple[ModelProfileRef, ...]],
) -> dict[RoleId, tuple[ModelProfileRef, ...]]:
    return {role_id: tuple(profiles) for role_id, profiles in values.items()}


def _tuple_mapping(values: dict[RoleId, tuple[str, ...]]) -> dict[RoleId, tuple[str, ...]]:
    return {role_id: tuple(items) for role_id, items in values.items()}


@dataclass(frozen=True)
class TeamRoleCatalog:
    roles: dict[RoleId, TeamRole]

    @classmethod
    def default(cls) -> "TeamRoleCatalog":
        return cls(
            roles={
                RoleId.DIRECTOR: TeamRole(
                    role_id=RoleId.DIRECTOR,
                    display_name="总工程师",
                    mission="选择现有执行路线，组织角色交接，并对最终结论负责。",
                    instructions="Use WLCodex staged workflow. Do not edit product code.",
                    skills=("planning", "synthesis"),
                    allowed_capabilities=("ledger", "runtime_events", "status"),
                    forbidden_actions=("edit_product_code", "deploy"),
                    required_artifact_type="routing_decision",
                ),
                RoleId.INVESTIGATOR: TeamRole(
                    role_id=RoleId.INVESTIGATOR,
                    display_name="诊断工程师",
                    mission="查症状、日志、运行状态和根因假设。",
                    instructions=(
                        "Investigate evidence and produce hypotheses with references. "
                        "When diagnosis and architecture are combined, keep the "
                        "diagnosis evidence explicit inside the architecture handoff."
                    ),
                    skills=("systematic-debugging", "log-analysis"),
                    allowed_capabilities=("read", "shell_readonly", "logs"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="diagnosis_report",
                    expert_stance="Evidence-first bug investigator.",
                    expert_priorities=(
                        "Reproduce or verify the symptom before claiming a cause.",
                        "Separate symptom, trigger, and root cause.",
                        "Prefer the smallest credible repair.",
                        "Leave unrelated dirty work alone.",
                        "Make uncertainty explicit when evidence is incomplete.",
                    ),
                    required_checks=(
                        "User symptom and expected behavior.",
                        "Current logs, errors, tests, or runtime status when available.",
                        "Recent diff or baseline when relevant.",
                        "Code path that can explain the symptom.",
                        "Regression test or verification command.",
                    ),
                    anti_patterns=(
                        "Do not guess root cause without evidence.",
                        "Do not turn bugs into redesigns.",
                        "Do not blame unrelated workspace diff without conflict evidence.",
                        "Do not claim fixed before verification exists.",
                    ),
                    handoff_focus=(
                        "Root cause or top hypotheses with confidence.",
                        "Minimal fix path.",
                        "Exact evidence references.",
                        "Regression checks.",
                        "Risks if evidence is incomplete.",
                    ),
                ),
                RoleId.ARCHITECT: TeamRole(
                    role_id=RoleId.ARCHITECT,
                    display_name="架构工程师",
                    mission="确定修复边界、影响面、方案和验收标准；必要时承接诊断证据。",
                    instructions=(
                        "Define scope, impact, risk, implementation steps, and "
                        "acceptance criteria. If diagnosis was collected in the same "
                        "black-box step, keep the diagnosis evidence inside the plan."
                    ),
                    skills=("gitnexus-impact-analysis", "system-design"),
                    allowed_capabilities=("read", "gitnexus", "shell_readonly"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="architecture_plan",
                    expert_stance="Systems designer for new or complex work.",
                    expert_priorities=(
                        "Understand current architecture first.",
                        "Reduce ambiguity into executable scope.",
                        "Choose a maintainable, small-enough design.",
                        "Preserve existing product conventions.",
                        "Expose tradeoffs instead of hiding them.",
                    ),
                    required_checks=(
                        "Current state and relevant flows.",
                        "In-scope and out-of-scope files or modules.",
                        "Data, permissions, UI/API, runtime, and deployment impact.",
                        "User-visible behavior.",
                        "Acceptance criteria.",
                    ),
                    anti_patterns=(
                        "Do not fix bugs without diagnosis.",
                        "Do not invent a large framework for a narrow request.",
                        "Do not propose changes without impacted files or modules.",
                        "Do not omit rollback or verification concerns when risk is non-trivial.",
                    ),
                    handoff_focus=(
                        "Implementation boundaries.",
                        "Ordered steps.",
                        "Files or modules to touch and avoid.",
                        "Acceptance criteria.",
                        "Red flags and assumptions.",
                    ),
                ),
                RoleId.IMPLEMENTER: TeamRole(
                    role_id=RoleId.IMPLEMENTER,
                    display_name="开发工程师",
                    mission="按已确认方案做最小实现并记录证据。",
                    instructions="Implement the accepted plan with focused changes and evidence.",
                    skills=("implementation", "test-driven-development"),
                    allowed_capabilities=("read", "write", "shell", "tests"),
                    forbidden_actions=("deploy", "destructive_command"),
                    required_artifact_type="implementation_report",
                    can_write_product_code=True,
                    expert_stance="Focused builder.",
                    expert_priorities=(
                        "Make the smallest change that satisfies the accepted plan.",
                        "Follow existing code patterns.",
                        "Keep unrelated files untouched.",
                        "Collect implementation and test evidence.",
                        "Stop when blocked rather than broadening scope silently.",
                    ),
                    required_checks=(
                        "Accepted Architect or Diagnostician handoff.",
                        "Task-scoped baseline and diff.",
                        "Commands run.",
                        "Tests attempted.",
                        "Known limitations.",
                    ),
                    anti_patterns=(
                        "Do not redesign during implementation without recording why.",
                        "Do not change unrelated files because they are dirty.",
                        "Do not omit commands or tests from the report.",
                        "Do not expose JSON protocol to users.",
                    ),
                    handoff_focus=(
                        "Changed files.",
                        "Diff summary.",
                        "Commands run.",
                        "Tests attempted.",
                        "Limitations and follow-up risks.",
                    ),
                ),
                RoleId.TESTER: TeamRole(
                    role_id=RoleId.TESTER,
                    display_name="测试工程师",
                    mission="复现、补测试、跑验证，并给出测试证据。",
                    instructions=(
                        "Follow the current developer session, validate acceptance "
                        "criteria, and report command evidence. If testing fails, "
                        "send the work back to the developer before the user is "
                        "asked for audit approval."
                    ),
                    skills=("verification-before-completion", "test-driven-development"),
                    allowed_capabilities=("read", "write_tests", "shell_readonly", "tests"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="test_report",
                    expert_stance="Verification discipline inside the implementer loop.",
                    expert_priorities=(
                        "Validate acceptance criteria.",
                        "Prefer focused tests first.",
                        "Record exact command evidence.",
                        "Block audit when tests are missing or failing.",
                        "Cap internal repair loops at 3 attempts.",
                    ),
                    required_checks=(
                        "Current-round commands.",
                        "Current-round pass and fail evidence.",
                        "Coverage of acceptance criteria.",
                    ),
                    anti_patterns=(
                        "Do not start an independent long model conversation for routine personal work.",
                        "Do not treat test evidence from a previous round as current.",
                        "Do not hide repeated test failure after the cap is reached.",
                    ),
                    handoff_focus=(
                        "Commands run.",
                        "Passed and failed checks.",
                        "Failure evidence.",
                        "Developer rework needs.",
                    ),
                ),
                RoleId.AUDITOR: TeamRole(
                    role_id=RoleId.AUDITOR,
                    display_name="审计工程师",
                    mission="独立审查 diff、测试证据、风险和遗漏。",
                    instructions=(
                        "Review evidence and report pass, block, or needs_user. "
                        "Audit only starts after implementation and test evidence "
                        "exist for the current round."
                    ),
                    skills=("code-review", "security-review"),
                    allowed_capabilities=("read", "git_diff", "shell_readonly"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="audit_report",
                    expert_stance="Independent reviewer with anti-false-positive discipline.",
                    expert_priorities=(
                        "Review current task diff, artifacts, and tests.",
                        "Verify implementation matches the accepted handoff.",
                        "Block only on specific, current, evidence-backed risk.",
                        "Allow a clean pass when evidence supports it.",
                    ),
                    required_checks=(
                        "Current task diff, not unrelated workspace noise.",
                        "Implementation report.",
                        "Test report.",
                        "Acceptance criteria coverage.",
                        "Security, regression, UX/API contract, and deployment risk.",
                    ),
                    anti_patterns=(
                        "Do not block on vague concerns.",
                        "Do not block on unrelated dirty files unless they conflict with the task.",
                        "Do not rewrite backend protocol keys into user-facing text.",
                        "Do not treat missing optional polish as a correctness failure.",
                    ),
                    handoff_focus=(
                        "Decision: pass, block, or needs_user.",
                        "Concrete findings with file/path evidence.",
                        "Missing evidence.",
                        "Recommended next action.",
                    ),
                ),
            }
        )

    def role(self, role_id: RoleId) -> TeamRole:
        return self.roles[role_id]
