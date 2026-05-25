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
                        "In v1 this role is performed by the architect unless an "
                        "independent investigator job is explicitly created."
                    ),
                    skills=("systematic-debugging", "log-analysis"),
                    allowed_capabilities=("read", "shell_readonly", "logs"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="diagnosis_report",
                ),
                RoleId.ARCHITECT: TeamRole(
                    role_id=RoleId.ARCHITECT,
                    display_name="架构工程师",
                    mission="确定修复边界、影响面、方案和验收标准；v1 同时承担诊断工程师职责。",
                    instructions=(
                        "Define scope, impact, risk, implementation steps, and "
                        "acceptance criteria. Architect performs investigator duties "
                        "in v1 by collecting diagnosis evidence inside the plan."
                    ),
                    skills=("gitnexus-impact-analysis", "system-design"),
                    allowed_capabilities=("read", "gitnexus", "shell_readonly"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="architecture_plan",
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
                ),
                RoleId.TESTER: TeamRole(
                    role_id=RoleId.TESTER,
                    display_name="测试工程师",
                    mission="复现、补测试、跑验证，并给出测试证据。",
                    instructions=(
                        "Validate acceptance criteria and report command evidence. "
                        "In v1 this role is performed by the auditor unless an "
                        "independent tester job is explicitly created."
                    ),
                    skills=("verification-before-completion", "test-driven-development"),
                    allowed_capabilities=("read", "write_tests", "shell_readonly", "tests"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="test_report",
                ),
                RoleId.AUDITOR: TeamRole(
                    role_id=RoleId.AUDITOR,
                    display_name="审计工程师",
                    mission="独立审查 diff、测试证据、风险和遗漏；v1 同时承担测试工程师职责。",
                    instructions=(
                        "Review evidence and report pass, block, or needs_user. "
                        "Auditor performs tester duties in v1: verify test_report "
                        "evidence and block missing current-round test coverage."
                    ),
                    skills=("code-review", "security-review"),
                    allowed_capabilities=("read", "git_diff", "shell_readonly"),
                    forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                    required_artifact_type="audit_report",
                ),
            }
        )

    def role(self, role_id: RoleId) -> TeamRole:
        return self.roles[role_id]
