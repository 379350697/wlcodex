# WLCodex Adaptive Engineering Team Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refine the existing Codex -> implementation -> Codex verification workflow into a role-aware Adaptive Engineering Team without replacing staged `/auto`.

**Architecture:** Add per-role model/tool/skills assignment, capability-library selection, TeamRun projections, structured artifacts, gates, event-sourced Instinct Memory, and canonical Context Packets around the current `orchestration_runs`, `agent_runs`, `runtime_events`, `/auto` callbacks, Cockpit, and Onsite surfaces. The current default model pool is `codex_gpt` and `claude_deepseek`; every engineer role can be configured, and the accepted plan presents configured Implementer choices when more than one exists.

**Scope correction for the strict repair pass:** first release remains a
single-writer staged-auto overlay. Wave/parallel execution, blocked/cancelled
TeamRun semantics, `team.gate.started`, Team Board append/decision workflow,
`team_board_entries`, and startup rebuild of active TeamRuns from runtime
events are later slices. This pass keeps durable projection persistence and
runtime-event evidence, but does not claim active-run recovery or board tables.

**Tech Stack:** Python 3.12, SQLite ledger, existing WLCodex controller/router/callback protocol, existing Codex app-server backend, existing Claude backend, runtime events, pytest, GitNexus MCP for impact analysis.

---

## Required Reading

- Spec: `docs/superpowers/specs/2026-05-25-wlcodex-adaptive-engineering-team-design.md`
- Existing staged `/auto` spec: `docs/superpowers/specs/2026-05-22-wlcodex-stage-gated-auto-workflow-design.md`
- Existing event-sourced platform spec: `docs/superpowers/specs/2026-05-18-wlcodex-light-event-sourced-agent-platform-design.md`
- Existing controller staged-auto flow: `wlcodex/controller.py`
- Existing callback protocol: `wlcodex/conversation_callback.py`
- Existing context packets: `wlcodex/context_packets.py`
- Existing models: `wlcodex/models.py`
- Existing ledger/migrations: `wlcodex/db.py`
- Existing runtime events: `wlcodex/runtime_events.py`
- Existing status/Cockpit renderers: `wlcodex/status.py`, `wlcodex/orchestration_progress_text.py`
- ECC capability-library reference: `https://github.com/affaan-m/ECC/blob/main/README.zh-CN.md`
- ECC continuous-learning reference: `https://github.com/affaan-m/ECC/blob/main/skills/continuous-learning-v2/SKILL.md`
- ECC session hook reference: `https://github.com/affaan-m/ECC/blob/main/scripts/hooks/session-start.js`

## Non-Negotiable Product Rules

- Do not replace staged `/auto`; enrich it.
- Keep the current user-confirmed flow: configured planning role creates the plan -> user chooses implementer -> selected implementer executes -> configured auditor verifies. The first-release default auditor is `codex_gpt`.
- First release tester policy: **Auditor performs tester duties in v1**. The
  system must still record and gate `test_report` evidence, and auditor context
  packets must say that the auditor verifies test coverage until a separate
  Tester job is introduced.
- First release investigator policy: **Architect performs investigator duties
  in v1**. The role catalog keeps `investigator`, but this slice does not create
  an independent Investigator job or `diagnosis_report` gate. Architect context
  packets and `architecture_plan` artifacts must carry diagnosis/evidence
  responsibility explicitly until the separate role lands.
- Do not silently pick an implementer when multiple configured Implementer profiles are available after the final plan.
- Roles are model-agnostic. `investigator`, `architect`, `implementer`, `tester`, and `auditor` must not hard-code Codex or Claude.
- Every role has configurable model profiles, tools, and skills. The first-release defaults may assign all non-implementation roles to `codex_gpt`, but that is only configuration.
- Implementation choices are model profiles, not new roles. `claude_deepseek` Implementer and `codex_gpt` Implementer are two assignments for the same `implementer` role.
- Context Packets are the cross-model/cross-session handoff contract. They must not include full chat transcripts, full logs, raw diffs, or full file contents.
- Context Packets must include selected skills/tools/memory snippets and the Capability Budget that selected them.
- Long-term memory is evidence-linked Instinct Memory. It is historical advice, not current truth, and it must never outrank the current user request, code, logs, command output, or role mission.
- Do not copy ECC's large catalog size as a target. Borrow layered packaging, event-triggered learning, and session-continuity ideas while keeping WLCodex's first-release role set small.
- Existing `orchestration_runs`, `agent_runs`, `tasks`, runtime event flow, workspace lock, approvals, Cockpit, and Onsite remain active compatibility surfaces.
- A TeamRun is an overlay/projection tied to existing Workbench execution, not a second workflow engine.

## Impact Baseline Commands

Before modifying any existing symbol below, run the corresponding GitNexus impact command. If risk is HIGH or CRITICAL, stop and report before editing.

```text
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "load_config",
  "file_path": "wlcodex/config.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "migrate",
  "file_path": "wlcodex/db.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "handle_auto_mode",
  "file_path": "wlcodex/controller.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "_handle_auto_final_plan",
  "file_path": "wlcodex/controller.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "_handle_auto_send_to_claude",
  "file_path": "wlcodex/controller.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "_handle_auto_codex_verify",
  "file_path": "wlcodex/controller.py",
  "kind": "Method",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "build_auto_stage_buttons",
  "file_path": "wlcodex/auto_workflow.py",
  "kind": "Function",
  "direction": "upstream"
})
```

Expected risk: LOW or MEDIUM. The GitNexus index may be stale; if the tool warns about staleness, run `npx gitnexus analyze` from the repo root before relying on the result.

## File Structure

| File | Responsibility |
| --- | --- |
| `wlcodex/team_roles.py` | New pure role catalog, model profile references, per-role tool/skills assignment policy helpers, and route decision types. |
| `wlcodex/team_capabilities.py` | New pure SkillCatalog, CapabilityBudget, capability selection, Role Config Audit, and skill activation helpers. |
| `wlcodex/team_artifacts.py` | New pure artifact schemas, validation, concise summaries, and gate checks. |
| `wlcodex/team_context.py` | New Context Packet compiler for canonical JSON packets, compact prompt rendering, role-specific skills/tools, Instinct Memory snippets, stale-replay guard, and token-budget trimming. |
| `wlcodex/team_memory.py` | New pure Observation and Instinct Memory models, relevance scoring, and packet selection helpers. |
| `wlcodex/team_observer.py` | New runtime-event observer that turns artifacts/audit evidence into Observations and candidate Instincts. |
| `wlcodex/models.py` | Add TeamRun, TeamAgentJob, TeamContextPacketRecord, TeamArtifact, TeamAssignment, TeamSkillActivation, TeamObservation, and TeamInstinct dataclasses and role/status enums. |
| `wlcodex/config.py` | Add optional adaptive team config with model profiles and assignment policy. |
| `config/wlcodex.example.toml` | Document default role/model assignment examples. |
| `wlcodex/db.py` | Add projection tables and ledger methods for TeamRun, AgentJob, Context Packet records, artifacts, assignments, skill activations, Observations, and Instincts. |
| `wlcodex/runtime_events.py` | Add team event constants. |
| `wlcodex/conversation_callback.py` | Add callback actions for choosing Codex Implementer and viewing team status/artifacts. |
| `wlcodex/auto_workflow.py` | Add role-aware stage buttons, including `交给 Codex 执行` when configured. |
| `wlcodex/controller.py` | Tie TeamRun/AgentJob/artifact recording into existing `/auto` phases and add Codex Implementer callback handling. |
| `wlcodex/status.py` | Render role-aware Cockpit summaries and artifact previews. |
| `tests/test_team_roles.py` | Role catalog and assignment policy tests. |
| `tests/test_team_capabilities.py` | SkillCatalog, capability budget, and activation tests. |
| `tests/test_team_artifacts.py` | Artifact validation and gate tests. |
| `tests/test_team_context.py` | Context Packet canonical JSON, cross-session handoff, memory precedence, and minimization tests. |
| `tests/test_team_memory.py` | Observation, Instinct Memory, and relevance selection tests. |
| `tests/test_team_observer.py` | Runtime-event observation and candidate-instinct tests. |
| `tests/test_db.py` | Team projection persistence tests. |
| `tests/test_config.py` | Config parsing tests. |
| `tests/test_controller_flow.py` | Staged-auto role overlay and implementer-choice tests. |
| `tests/test_status.py` | Role-aware status rendering tests. |

---

## Task 1: Role Catalog And Assignment Policy

**Files:**
- Create: `wlcodex/team_roles.py`
- Modify: `wlcodex/config.py`
- Modify: `config/wlcodex.example.toml`
- Test: `tests/test_team_roles.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing role catalog tests**

Add these tests to `tests/test_team_roles.py`:

```python
from __future__ import annotations

from wlcodex.team_roles import (
    AssignmentPolicy,
    ModelProfileRef,
    RoleId,
    TeamRoleCatalog,
)


def test_role_catalog_keeps_roles_backend_agnostic() -> None:
    catalog = TeamRoleCatalog.default()

    implementer = catalog.role(RoleId.IMPLEMENTER)

    assert implementer.role_id == RoleId.IMPLEMENTER
    assert implementer.can_write_product_code is True
    assert "claude" not in implementer.instructions.lower()
    assert "codex" not in implementer.instructions.lower()
    assert "implementation_report" in implementer.required_artifact_type


def test_assignment_policy_returns_user_choice_for_multiple_implementers() -> None:
    policy = AssignmentPolicy(
        defaults={
            RoleId.INVESTIGATOR: [ModelProfileRef("codex_gpt")],
            RoleId.ARCHITECT: [ModelProfileRef("codex_gpt")],
            RoleId.IMPLEMENTER: [
                ModelProfileRef("claude_deepseek"),
                ModelProfileRef("codex_gpt"),
            ],
            RoleId.TESTER: [ModelProfileRef("codex_gpt")],
            RoleId.AUDITOR: [ModelProfileRef("codex_gpt")],
        }
    )

    choices = policy.choices_for(RoleId.IMPLEMENTER, risk_level="medium")

    assert [choice.profile_id for choice in choices] == ["claude_deepseek", "codex_gpt"]
    assert policy.requires_user_choice(RoleId.IMPLEMENTER, risk_level="medium") is True
    assert policy.choices_for(RoleId.ARCHITECT, risk_level="medium")[0].profile_id == "codex_gpt"


def test_assignment_policy_keeps_non_implementer_roles_configurable() -> None:
    policy = AssignmentPolicy(
        defaults={
            RoleId.AUDITOR: [ModelProfileRef("codex_gpt")],
        }
    )

    assert policy.choices_for(RoleId.AUDITOR, risk_level="low")[0].profile_id == "codex_gpt"
    assert policy.requires_user_choice(RoleId.AUDITOR, risk_level="low") is False


def test_role_assignments_can_override_tools_and_skills() -> None:
    policy = AssignmentPolicy(
        defaults={RoleId.INVESTIGATOR: [ModelProfileRef("codex_gpt")]},
        role_skills={RoleId.INVESTIGATOR: ("systematic-debugging", "gitnexus-exploring")},
        role_capabilities={RoleId.INVESTIGATOR: ("read", "shell_readonly", "logs")},
    )

    assert policy.skills_for(RoleId.INVESTIGATOR) == ("systematic-debugging", "gitnexus-exploring")
    assert policy.capabilities_for(RoleId.INVESTIGATOR) == ("read", "shell_readonly", "logs")
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_roles.py -q
```

Expected: fails because `wlcodex.team_roles` does not exist.

- [ ] **Step 3: Implement role catalog**

Create `wlcodex/team_roles.py` with these public types:

```python
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
    defaults: dict[RoleId, list[ModelProfileRef]] = field(default_factory=dict)
    high_risk_overrides: dict[RoleId, list[ModelProfileRef]] = field(default_factory=dict)
    role_skills: dict[RoleId, tuple[str, ...]] = field(default_factory=dict)
    role_capabilities: dict[RoleId, tuple[str, ...]] = field(default_factory=dict)

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


@dataclass(frozen=True)
class TeamRoleCatalog:
    roles: dict[RoleId, TeamRole]

    @classmethod
    def default(cls) -> "TeamRoleCatalog":
        return cls(roles={
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
                instructions="Investigate evidence and produce hypotheses with references.",
                skills=("systematic-debugging", "log-analysis"),
                allowed_capabilities=("read", "shell_readonly", "logs"),
                forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                required_artifact_type="diagnosis_report",
            ),
            RoleId.ARCHITECT: TeamRole(
                role_id=RoleId.ARCHITECT,
                display_name="架构工程师",
                mission="确定修复边界、影响面、方案和验收标准。",
                instructions="Define scope, impact, risk, implementation steps, and acceptance criteria.",
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
                instructions="Validate acceptance criteria and report command evidence.",
                skills=("verification-before-completion", "test-driven-development"),
                allowed_capabilities=("read", "write_tests", "shell", "tests"),
                forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                required_artifact_type="test_report",
            ),
            RoleId.AUDITOR: TeamRole(
                role_id=RoleId.AUDITOR,
                display_name="审计工程师",
                mission="独立审查 diff、测试证据、风险和遗漏。",
                instructions="Review evidence and report pass, block, or needs_user.",
                skills=("code-review", "security-review"),
                allowed_capabilities=("read", "git_diff", "shell_readonly"),
                forbidden_actions=("edit_product_code", "deploy", "destructive_command"),
                required_artifact_type="audit_report",
            ),
        })

    def role(self, role_id: RoleId) -> TeamRole:
        return self.roles[role_id]
```

- [ ] **Step 4: Add config parsing tests**

Extend the existing `test_conversation_config_defaults` in `tests/test_config.py` with:

```python
assert config.adaptive_team.enabled is True
assert config.adaptive_team.model_profiles["codex_gpt"] == "codex"
assert config.adaptive_team.model_profiles["claude_deepseek"] == "claude"
assert config.adaptive_team.assignments["investigator"] == ("codex_gpt",)
assert config.adaptive_team.assignments["architect"] == ("codex_gpt",)
assert config.adaptive_team.assignments["implementer"] == ("claude_deepseek", "codex_gpt")
assert config.adaptive_team.assignments["tester"] == ("codex_gpt",)
assert config.adaptive_team.assignments["auditor"] == ("codex_gpt",)
```

- [ ] **Step 5: Implement config dataclasses and defaults**

Update the import in `wlcodex/config.py` to `from dataclasses import dataclass, field`, then add:

```python
@dataclass(frozen=True)
class AdaptiveTeamConfig:
    enabled: bool = True
    model_profiles: dict[str, str] = field(default_factory=lambda: {
        "codex_gpt": "codex",
        "claude_deepseek": "claude",
    })
    assignments: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "director": ("codex_gpt",),
        "investigator": ("codex_gpt",),
        "architect": ("codex_gpt",),
        "implementer": ("claude_deepseek", "codex_gpt"),
        "tester": ("codex_gpt",),
        "auditor": ("codex_gpt",),
    })
    role_skills: dict[str, tuple[str, ...]] = field(default_factory=dict)
    role_capabilities: dict[str, tuple[str, ...]] = field(default_factory=dict)
```

Add `adaptive_team: AdaptiveTeamConfig = AdaptiveTeamConfig()` to `AppConfig`. In `load_config`, read `[adaptive_team]`, `[adaptive_team.model_profiles]`, `[adaptive_team.assignments]`, `[adaptive_team.role_skills]`, and `[adaptive_team.role_capabilities]`. Keep missing config safe by using the defaults above.

- [ ] **Step 6: Document example config**

Add this section to `config/wlcodex.example.toml`:

```toml
[adaptive_team]
enabled = true

[adaptive_team.model_profiles]
codex_gpt = "codex"
claude_deepseek = "claude"

[adaptive_team.assignments]
director = ["codex_gpt"]
investigator = ["codex_gpt"]
architect = ["codex_gpt"]
implementer = ["claude_deepseek", "codex_gpt"]
tester = ["codex_gpt"]
auditor = ["codex_gpt"]

[adaptive_team.role_skills]
investigator = ["systematic-debugging", "gitnexus-exploring"]
architect = ["gitnexus-impact-analysis", "karpathy-guidelines"]
implementer = ["test-driven-development", "verification-before-completion"]
tester = ["test-driven-development", "verification-before-completion"]
auditor = ["github:gh-address-comments", "gitnexus-pr-review"]

[adaptive_team.role_capabilities]
investigator = ["read", "shell_readonly", "logs", "gitnexus"]
architect = ["read", "shell_readonly", "gitnexus"]
implementer = ["read", "write", "shell", "tests"]
tester = ["read", "write_tests", "shell", "tests"]
auditor = ["read", "git_diff", "shell_readonly", "gitnexus"]
```

- [ ] **Step 7: Verify Task 1**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_roles.py tests/test_config.py -q
```

Expected: all selected tests pass.

## Task 1A: Capability Library And Budget

**Files:**
- Create: `wlcodex/team_capabilities.py`
- Test: `tests/test_team_capabilities.py`

- [ ] **Step 1: Write failing capability selection tests**

Create `tests/test_team_capabilities.py`:

```python
from __future__ import annotations

from wlcodex.team_capabilities import (
    CapabilityBudget,
    SkillCatalog,
    SkillDefinition,
    audit_role_capability_config,
    select_capabilities,
)


def test_skill_catalog_selects_role_and_trigger_matches_under_budget() -> None:
    catalog = SkillCatalog(skills=(
        SkillDefinition(
            skill_id="gitnexus-pr-review",
            summary="Review diffs and PR risks.",
            triggers=("audit", "review", "diff"),
            allowed_roles=("auditor",),
            required_tools=("read", "git_diff"),
            token_cost=160,
        ),
        SkillDefinition(
            skill_id="systematic-debugging",
            summary="Investigate symptoms with evidence.",
            triggers=("log", "failure", "root cause"),
            allowed_roles=("investigator",),
            required_tools=("read", "logs"),
            token_cost=180,
        ),
        SkillDefinition(
            skill_id="security-review",
            summary="Look for injection and permission risks.",
            triggers=("security", "permission", "prompt injection"),
            allowed_roles=("auditor", "architect"),
            required_tools=("read", "git_diff"),
            token_cost=200,
        ),
    ))

    selection = select_capabilities(
        catalog=catalog,
        role="auditor",
        task_text="audit the diff for prompt injection and permission risk",
        available_tools=("read", "git_diff", "shell_readonly"),
        budget=CapabilityBudget(max_skills=2, max_tools=4, max_memory_snippets=3, max_prompt_tokens=700),
    )

    assert [skill.skill_id for skill in selection.skills] == ["security-review", "gitnexus-pr-review"]
    assert selection.tools == ("git_diff", "read", "shell_readonly")
    assert selection.budget.max_memory_snippets == 3


def test_capability_selection_excludes_skills_when_required_tools_are_missing() -> None:
    catalog = SkillCatalog(skills=(
        SkillDefinition(
            skill_id="log-analysis",
            summary="Read service logs.",
            triggers=("log",),
            allowed_roles=("investigator",),
            required_tools=("logs",),
            token_cost=100,
        ),
    ))

    selection = select_capabilities(
        catalog=catalog,
        role="investigator",
        task_text="check logs for crash",
        available_tools=("read",),
        budget=CapabilityBudget(max_skills=2, max_tools=2, max_memory_snippets=1, max_prompt_tokens=400),
    )

    assert selection.skills == ()
    assert selection.tools == ("read",)


def test_role_capability_audit_flags_write_tools_on_auditor() -> None:
    findings = audit_role_capability_config({
        "auditor": ("read", "write", "git_diff"),
        "investigator": ("read", "shell_readonly"),
        "implementer": ("read", "write", "shell"),
    })

    assert findings == ("auditor has forbidden capability write",)
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_capabilities.py -q
```

Expected: fails because `wlcodex.team_capabilities` does not exist.

- [ ] **Step 3: Implement capability library**

Create `wlcodex/team_capabilities.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    summary: str
    triggers: tuple[str, ...]
    allowed_roles: tuple[str, ...]
    required_tools: tuple[str, ...]
    token_cost: int


@dataclass(frozen=True)
class SkillCatalog:
    skills: tuple[SkillDefinition, ...]

    def for_role(self, role: str) -> tuple[SkillDefinition, ...]:
        return tuple(skill for skill in self.skills if role in skill.allowed_roles)


@dataclass(frozen=True)
class CapabilityBudget:
    max_skills: int
    max_tools: int
    max_memory_snippets: int
    max_prompt_tokens: int


@dataclass(frozen=True)
class CapabilitySelection:
    skills: tuple[SkillDefinition, ...]
    tools: tuple[str, ...]
    budget: CapabilityBudget


def _trigger_score(skill: SkillDefinition, task_text: str) -> int:
    lowered = task_text.lower()
    return sum(1 for trigger in skill.triggers if trigger.lower() in lowered)


def _has_required_tools(skill: SkillDefinition, available_tools: set[str]) -> bool:
    return set(skill.required_tools).issubset(available_tools)


def select_capabilities(
    *,
    catalog: SkillCatalog,
    role: str,
    task_text: str,
    available_tools: tuple[str, ...],
    budget: CapabilityBudget,
) -> CapabilitySelection:
    available = set(available_tools)
    scored: list[tuple[int, int, SkillDefinition]] = []
    for skill in catalog.for_role(role):
        if not _has_required_tools(skill, available):
            continue
        score = _trigger_score(skill, task_text)
        if score == 0:
            continue
        scored.append((score, -skill.token_cost, skill))

    selected: list[SkillDefinition] = []
    token_total = 0
    for _score, _cost_rank, skill in sorted(scored, key=lambda item: (-item[0], item[1], item[2].skill_id)):
        if len(selected) >= budget.max_skills:
            break
        if token_total + skill.token_cost > budget.max_prompt_tokens:
            continue
        selected.append(skill)
        token_total += skill.token_cost

    tool_names = sorted(set(available_tools[: budget.max_tools]))
    return CapabilitySelection(
        skills=tuple(selected),
        tools=tuple(tool_names),
        budget=budget,
    )


def audit_role_capability_config(role_capabilities: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    review_only_roles = {"auditor", "investigator", "architect"}
    forbidden = {"write", "deploy", "secret_read", "destructive_command"}
    findings: list[str] = []
    for role, capabilities in sorted(role_capabilities.items()):
        if role not in review_only_roles:
            continue
        for capability in capabilities:
            if capability in forbidden:
                findings.append(f"{role} has forbidden capability {capability}")
    return tuple(findings)
```

- [ ] **Step 4: Record skill activation shape**

When Task 2 adds persistence, use this payload shape for every selected skill
and tool in a Context Packet:

```python
{
    "team_run_id": team_run.id,
    "agent_job_id": agent_job.id,
    "activation_type": "skill",
    "activation_id": "security-review",
    "source": "capability_budget",
    "token_cost": 200,
}
```

Tools use `activation_type="tool"` and the tool id in `activation_id`.

- [ ] **Step 5: Verify Task 1A**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_capabilities.py -q
```

Expected: all selected capability tests pass.

## Task 2: Team Projection Models And Ledger Tables

**Files:**
- Modify: `wlcodex/models.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing DB tests**

Add to `tests/test_db.py`:

```python
def test_team_run_links_to_existing_orchestration_run(ledger: Ledger) -> None:
    convo = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Role-aware auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="fix bug")

    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="fix bug",
        route="staged_auto",
        risk_level="medium",
    )

    loaded = ledger.get_team_run(team.id)
    assert loaded is not None
    assert loaded.orchestration_run_id == orch.id
    assert loaded.route == "staged_auto"
    assert loaded.status == "running"


def test_team_artifact_round_trip(ledger: Ledger) -> None:
    convo = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Artifacts",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="fix bug")
    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="fix bug",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="architect",
        model_profile="codex_gpt",
        status="running",
        agent_run_id=None,
    )

    artifact = ledger.record_team_artifact(
        team_run_id=team.id,
        agent_job_id=job.id,
        artifact_type="architecture_plan",
        summary="Change one file and add one test.",
        payload={"acceptance_criteria": ["pytest passes"]},
    )

    artifacts = ledger.list_team_artifacts(team.id)
    assert artifacts[0].id == artifact.id
    assert artifacts[0].artifact_type == "architecture_plan"
    assert artifacts[0].payload["acceptance_criteria"] == ["pytest passes"]


def test_team_context_packet_round_trip(ledger: Ledger) -> None:
    convo = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Context",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="fix bug")
    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="fix bug",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )

    record = ledger.record_team_context_packet(
        team_run_id=team.id,
        agent_job_id=job.id,
        packet_json={
            "team_run_id": team.id,
            "agent_job_id": job.id,
            "role": "auditor",
            "model_profile": "codex_gpt",
            "resume_state": "implementation done; audit next",
        },
        prompt_text="role: auditor\nresume_state: implementation done; audit next",
        prompt_tokens=14,
    )

    loaded = ledger.get_team_context_packet_for_job(job.id)
    assert loaded is not None
    assert loaded.id == record.id
    assert loaded.packet["role"] == "auditor"
    assert loaded.packet["resume_state"] == "implementation done; audit next"


def test_team_skill_activation_round_trip(ledger: Ledger) -> None:
    convo = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Activation",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="audit diff")
    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="audit diff",
        route="staged_auto",
        risk_level="medium",
    )
    job = ledger.create_team_agent_job(
        team_run_id=team.id,
        role="auditor",
        model_profile="codex_gpt",
        status="queued",
        agent_run_id=None,
    )

    activation = ledger.record_team_skill_activation(
        team_run_id=team.id,
        agent_job_id=job.id,
        activation_type="skill",
        activation_id="security-review",
        source="capability_budget",
        token_cost=200,
    )

    activations = ledger.list_team_skill_activations(job.id)
    assert activations[0].id == activation.id
    assert activations[0].activation_id == "security-review"
    assert activations[0].source == "capability_budget"
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -q
```

Expected: fails because ledger methods and model dataclasses do not exist.

- [ ] **Step 3: Add model dataclasses**

Add to `wlcodex/models.py`:

```python
@dataclass(frozen=True)
class TeamRun:
    id: int
    conversation_id: int
    orchestration_run_id: int | None
    goal: str
    route: str
    risk_level: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TeamAgentJob:
    id: int
    team_run_id: int
    role: str
    model_profile: str
    status: str
    agent_run_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TeamContextPacketRecord:
    id: int
    team_run_id: int
    agent_job_id: int
    packet: dict[str, Any]
    prompt_text: str
    prompt_tokens: int
    created_at: datetime


@dataclass(frozen=True)
class TeamArtifact:
    id: int
    team_run_id: int
    agent_job_id: int | None
    artifact_type: str
    summary: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class TeamAssignment:
    id: int
    team_run_id: int
    role: str
    model_profile: str
    selected_by: str
    created_at: datetime


@dataclass(frozen=True)
class TeamSkillActivation:
    id: int
    team_run_id: int
    agent_job_id: int
    activation_type: str
    activation_id: str
    source: str
    token_cost: int
    created_at: datetime
```

- [ ] **Step 4: Add SQLite tables**

In `wlcodex/db.py`, extend migration with:

```sql
CREATE TABLE IF NOT EXISTS team_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    orchestration_run_id INTEGER,
    goal TEXT NOT NULL,
    route TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_agent_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_run_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    model_profile TEXT NOT NULL,
    status TEXT NOT NULL,
    agent_run_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_context_packets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_run_id INTEGER NOT NULL,
    agent_job_id INTEGER NOT NULL,
    packet_json TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_run_id INTEGER NOT NULL,
    agent_job_id INTEGER,
    artifact_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_run_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    model_profile TEXT NOT NULL,
    selected_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_skill_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_run_id INTEGER NOT NULL,
    agent_job_id INTEGER NOT NULL,
    activation_type TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    token_cost INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_runs_conversation
    ON team_runs(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_team_runs_orchestration
    ON team_runs(orchestration_run_id, id);
CREATE INDEX IF NOT EXISTS idx_team_agent_jobs_team
    ON team_agent_jobs(team_run_id, id);
CREATE INDEX IF NOT EXISTS idx_team_context_packets_job
    ON team_context_packets(agent_job_id, id);
CREATE INDEX IF NOT EXISTS idx_team_artifacts_team
    ON team_artifacts(team_run_id, id);
CREATE INDEX IF NOT EXISTS idx_team_skill_activations_job
    ON team_skill_activations(agent_job_id, id);
```

- [ ] **Step 5: Add ledger methods**

Add these public methods to the existing ledger class in `wlcodex/db.py`.

| Method | Behavior |
| --- | --- |
| `create_team_run` | Insert into `team_runs` with `status="running"`, current timestamps, and return the inserted row mapped to `TeamRun`. |
| `get_team_run(team_run_id) -> TeamRun | None` | Select one `team_runs` row by id and return `None` when absent. |
| `get_team_run_for_orchestration(orchestration_run_id) -> TeamRun | None` | Select the newest `team_runs` row for the orchestration id. |
| `update_team_run_status(team_run_id, status) -> None` | Update `status` and `updated_at`. |
| `create_team_agent_job` | Insert into `team_agent_jobs` with current timestamps and return the inserted row. |
| `update_team_agent_job_status(job_id, status) -> None` | Update job `status` and `updated_at`. |
| `record_team_context_packet` | JSON-encode canonical Context Packet JSON, insert prompt rendering and prompt token estimate into `team_context_packets`, and return the inserted record. |
| `get_team_context_packet_for_job(agent_job_id) -> TeamContextPacketRecord | None` | Return the newest packet for an AgentJob, JSON-decoding `packet_json`. |
| `record_team_artifact` | JSON-encode `payload`, insert into `team_artifacts`, and return the inserted artifact. |
| `list_team_artifacts(team_run_id) -> list[TeamArtifact]` | Return artifacts ordered by id ascending, JSON-decoding `payload_json`. |
| `record_team_assignment` | Insert the selected role/model pair into `team_assignments` and return the inserted row. |
| `list_team_agent_jobs(team_run_id) -> list[TeamAgentJob]` | Return jobs ordered by id ascending. |
| `record_team_skill_activation` | Insert one selected skill/tool/memory activation for a Context Packet and return the inserted row. |
| `list_team_skill_activations(agent_job_id) -> list[TeamSkillActivation]` | Return activations ordered by id ascending. |

Use the existing timestamp, SQL execution, row-mapping, transaction, and JSON
helper style already used in `db.py`. Add private row mappers named
`_row_to_team_run`, `_row_to_team_agent_job`,
`_row_to_team_context_packet`, `_row_to_team_artifact`, and
`_row_to_team_assignment`, and `_row_to_team_skill_activation` next to the
existing mapper helpers.

- [ ] **Step 6: Verify Task 2**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -q
```

Expected: all selected DB tests pass.

## Task 2A: Event-Sourced Instinct Memory

**Files:**
- Create: `wlcodex/team_memory.py`
- Create: `wlcodex/team_observer.py`
- Modify: `wlcodex/models.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_team_memory.py`
- Test: `tests/test_team_observer.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing memory selection tests**

Create `tests/test_team_memory.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from wlcodex.team_memory import InstinctMemory, select_relevant_instincts


def test_select_relevant_instincts_prefers_scope_role_and_confidence() -> None:
    now = datetime(2026, 5, 25, tzinfo=UTC)
    instincts = (
        InstinctMemory(
            instinct_id="project-audit",
            scope="project",
            workspace_alias="wlcodex",
            role="auditor",
            domain="audit",
            trigger="verification after implementation diff",
            action="Check changed files and missing test evidence before passing.",
            confidence=0.84,
            evidence_refs=("audit_report=12",),
            status="active",
            created_at=now,
            last_validated_at=now,
        ),
        InstinctMemory(
            instinct_id="global-debug",
            scope="global",
            workspace_alias=None,
            role="investigator",
            domain="debugging",
            trigger="logs show crash loop",
            action="Collect process status before editing code.",
            confidence=0.92,
            evidence_refs=("observation=9",),
            status="active",
            created_at=now,
            last_validated_at=now,
        ),
        InstinctMemory(
            instinct_id="weak-audit",
            scope="project",
            workspace_alias="wlcodex",
            role="auditor",
            domain="audit",
            trigger="diff review",
            action="Ask for broad manual review.",
            confidence=0.2,
            evidence_refs=("observation=2",),
            status="active",
            created_at=now,
            last_validated_at=now,
        ),
    )

    selected = select_relevant_instincts(
        instincts,
        workspace_alias="wlcodex",
        role="auditor",
        task_text="verification after implementation diff",
        limit=2,
        min_confidence=0.6,
    )

    assert [instinct.instinct_id for instinct in selected] == ["project-audit"]


def test_select_relevant_instincts_marks_memory_as_historical_advice() -> None:
    now = datetime(2026, 5, 25, tzinfo=UTC)
    instinct = InstinctMemory(
        instinct_id="project-context",
        scope="project",
        workspace_alias="wlcodex",
        role="architect",
        domain="architecture",
        trigger="staged auto plan",
        action="Keep staged-auto compatibility columns populated.",
        confidence=0.77,
        evidence_refs=("spec=adaptive-team",),
        status="active",
        created_at=now,
        last_validated_at=now,
    )

    selected = select_relevant_instincts(
        (instinct,),
        workspace_alias="wlcodex",
        role="architect",
        task_text="staged auto plan",
        limit=1,
        min_confidence=0.6,
    )

    assert selected[0].as_packet_item()["precedence"] == "historical_advice_current_evidence_wins"
```

- [ ] **Step 2: Write failing observer tests**

Create `tests/test_team_observer.py`:

```python
from __future__ import annotations

from wlcodex.team_observer import candidate_instinct_from_observation, observations_from_artifact


def test_observer_extracts_observation_from_blocking_audit_artifact() -> None:
    observations = observations_from_artifact(
        team_run_id=7,
        artifact_type="audit_report",
        payload={
            "decision": "block",
            "summary": "Missing test evidence for auth fallback.",
            "findings": ["auth fallback was changed without a regression test"],
            "missing_evidence": ["pytest tests/test_auth.py -q"],
            "risk_level": "medium",
        },
        evidence_ref="team_artifact=21",
    )

    assert len(observations) == 1
    assert observations[0].domain == "audit"
    assert "regression test" in observations[0].summary
    assert observations[0].evidence_refs == ("team_artifact=21",)


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
```

- [ ] **Step 3: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_memory.py tests/test_team_observer.py -q
```

Expected: fails because `wlcodex.team_memory` and `wlcodex.team_observer` do not exist.

- [ ] **Step 4: Implement Instinct Memory helpers**

Create `wlcodex/team_memory.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Observation:
    team_run_id: int
    domain: str
    summary: str
    evidence_refs: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class InstinctMemory:
    instinct_id: str
    scope: str
    workspace_alias: str | None
    role: str
    domain: str
    trigger: str
    action: str
    confidence: float
    evidence_refs: tuple[str, ...]
    status: str
    created_at: datetime
    last_validated_at: datetime

    def as_packet_item(self) -> dict[str, object]:
        return {
            "id": self.instinct_id,
            "scope": self.scope,
            "role": self.role,
            "trigger": self.trigger,
            "action": self.action,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "precedence": "historical_advice_current_evidence_wins",
        }


def _text_score(needle: str, haystack: str) -> int:
    words = [word for word in needle.lower().replace("-", " ").split() if len(word) >= 4]
    lowered = haystack.lower()
    return sum(1 for word in words if word in lowered)


def _scope_score(instinct: InstinctMemory, workspace_alias: str) -> int:
    if instinct.scope in {"project", "workspace"} and instinct.workspace_alias == workspace_alias:
        return 3
    if instinct.scope == "global":
        return 1
    return 0


def select_relevant_instincts(
    instincts: tuple[InstinctMemory, ...],
    *,
    workspace_alias: str,
    role: str,
    task_text: str,
    limit: int,
    min_confidence: float,
) -> tuple[InstinctMemory, ...]:
    scored: list[tuple[int, float, str, InstinctMemory]] = []
    for instinct in instincts:
        if instinct.status != "active":
            continue
        if instinct.confidence < min_confidence:
            continue
        if instinct.role not in {role, "*"}:
            continue
        scope_score = _scope_score(instinct, workspace_alias)
        if scope_score == 0:
            continue
        relevance = _text_score(instinct.trigger, task_text) + _text_score(instinct.domain, task_text)
        if relevance == 0:
            continue
        scored.append((scope_score + relevance, instinct.confidence, instinct.instinct_id, instinct))

    ordered = sorted(scored, key=lambda item: (-item[0], -item[1], item[2]))
    return tuple(item[3] for item in ordered[:limit])
```

- [ ] **Step 5: Implement TeamObserver helpers**

Create `wlcodex/team_observer.py`:

```python
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
    active = repeated_evidence_count >= 2
    confidence = min(0.95, observation.confidence + (0.1 * max(0, repeated_evidence_count - 1)))
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
        status="active" if active else "candidate",
        created_at=now,
        last_validated_at=now,
    )
```

- [ ] **Step 6: Add persistence models and tables**

Add these dataclasses to `wlcodex/models.py`:

```python
@dataclass(frozen=True)
class TeamObservation:
    id: int
    team_run_id: int
    domain: str
    summary: str
    evidence_refs: tuple[str, ...]
    confidence: float
    created_at: datetime


@dataclass(frozen=True)
class TeamInstinct:
    id: int
    instinct_id: str
    scope: str
    workspace_alias: str | None
    role: str
    domain: str
    trigger: str
    action: str
    confidence: float
    evidence_refs: tuple[str, ...]
    status: str
    created_at: datetime
    last_validated_at: datetime
```

Extend `wlcodex/db.py` migration with:

```sql
CREATE TABLE IF NOT EXISTS team_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_run_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_instincts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instinct_id TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    workspace_alias TEXT,
    role TEXT NOT NULL,
    domain TEXT NOT NULL,
    trigger TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_validated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_team_observations_team
    ON team_observations(team_run_id, id);
CREATE INDEX IF NOT EXISTS idx_team_instincts_scope
    ON team_instincts(scope, workspace_alias, role, status);
```

Add ledger methods `record_team_observation`,
`list_team_observations(team_run_id)`, `upsert_team_instinct`, and
`list_team_instincts(status="active")`. JSON-encode evidence refs as arrays of
strings.

- [ ] **Step 7: Add DB round-trip test**

Add to `tests/test_db.py`:

```python
def test_team_instinct_round_trip(ledger: Ledger) -> None:
    convo = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Memory",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="audit")
    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="audit",
        route="staged_auto",
        risk_level="medium",
    )

    observation = ledger.record_team_observation(
        team_run_id=team.id,
        domain="audit",
        summary="Verifier packet missed changed files.",
        evidence_refs=("team_artifact=22",),
        confidence=0.7,
    )
    instinct = ledger.upsert_team_instinct(
        instinct_id="instinct:wlcodex:audit:changed-files",
        scope="project",
        workspace_alias="wlcodex",
        role="auditor",
        domain="audit",
        trigger="verifier packet missed changed files",
        action="Include changed files before audit starts.",
        confidence=0.8,
        evidence_refs=("team_observation=%s" % observation.id,),
        status="active",
    )

    instincts = ledger.list_team_instincts(status="active")
    assert instincts[0].id == instinct.id
    assert instincts[0].evidence_refs == ("team_observation=%s" % observation.id,)
```

- [ ] **Step 8: Verify Task 2A**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_memory.py tests/test_team_observer.py tests/test_db.py -q
```

Expected: selected memory, observer, and DB tests pass.

## Task 3: Artifact Schemas And Gates

**Files:**
- Create: `wlcodex/team_artifacts.py`
- Test: `tests/test_team_artifacts.py`

- [ ] **Step 1: Write failing artifact tests**

Create `tests/test_team_artifacts.py`:

```python
from __future__ import annotations

from wlcodex.team_artifacts import (
    GateResult,
    validate_architecture_plan,
    validate_implementation_report,
    validate_test_report,
    validate_audit_report,
)


def test_architecture_plan_gate_requires_acceptance_criteria_and_scope() -> None:
    result = validate_architecture_plan({
        "summary": "Fix auth null path.",
        "risk_level": "medium",
        "files_or_modules_in_scope": ["wlcodex/auth.py"],
        "implementation_steps": ["Add guard."],
        "acceptance_criteria": ["pytest tests/test_auth.py -q passes"],
    })

    assert result == GateResult(passed=True, missing=())


def test_architecture_plan_gate_blocks_missing_acceptance_criteria() -> None:
    result = validate_architecture_plan({
        "summary": "Fix auth null path.",
        "risk_level": "medium",
        "files_or_modules_in_scope": ["wlcodex/auth.py"],
        "implementation_steps": ["Add guard."],
    })

    assert result.passed is False
    assert "acceptance_criteria" in result.missing


def test_audit_report_requires_explicit_decision() -> None:
    result = validate_audit_report({
        "summary": "Reviewed diff and tests.",
        "findings": [],
        "missing_evidence": [],
        "risk_level": "low",
    })

    assert result.passed is False
    assert "decision" in result.missing
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_artifacts.py -q
```

Expected: fails because `wlcodex.team_artifacts` does not exist.

- [ ] **Step 3: Implement artifact validation helpers**

Create `wlcodex/team_artifacts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateResult:
    passed: bool
    missing: tuple[str, ...] = ()


def _missing(payload: dict[str, Any], required: tuple[str, ...]) -> tuple[str, ...]:
    missing: list[str] = []
    for key in required:
        value = payload.get(key)
        if value is None or value == "" or value == []:
            missing.append(key)
    return tuple(missing)


def _result(payload: dict[str, Any], required: tuple[str, ...]) -> GateResult:
    missing = _missing(payload, required)
    return GateResult(passed=not missing, missing=missing)


def validate_architecture_plan(payload: dict[str, Any]) -> GateResult:
    return _result(payload, (
        "summary",
        "risk_level",
        "files_or_modules_in_scope",
        "implementation_steps",
        "acceptance_criteria",
    ))


def validate_implementation_report(payload: dict[str, Any]) -> GateResult:
    return _result(payload, (
        "summary",
        "changed_files",
        "diff_summary",
    ))


def validate_test_report(payload: dict[str, Any]) -> GateResult:
    return _result(payload, (
        "summary",
        "commands_run",
        "coverage_of_acceptance_criteria",
    ))


def validate_audit_report(payload: dict[str, Any]) -> GateResult:
    result = _result(payload, (
        "decision",
        "summary",
        "risk_level",
    ))
    if payload.get("decision") not in (None, "", "pass", "block", "needs_user"):
        return GateResult(False, result.missing + ("decision",))
    return result
```

- [ ] **Step 4: Verify Task 3**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_artifacts.py -q
```

Expected: all selected artifact tests pass.

## Task 4: Role-Specific Context Packets

**Files:**
- Create: `wlcodex/team_context.py`
- Test: `tests/test_team_context.py`

- [ ] **Step 1: Write failing context packet tests**

Create `tests/test_team_context.py`:

```python
from __future__ import annotations

from wlcodex.team_context import TeamContextInput, build_team_context_packet
from wlcodex.team_roles import RoleId, TeamRoleCatalog


def test_context_packet_is_role_specific_and_excludes_full_history() -> None:
    role = TeamRoleCatalog.default().role(RoleId.AUDITOR)
    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=9,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="codex_gpt",
            user_goal="修复登录偶发失败",
            workspace_alias="wlcodex",
            skills=("code-review", "gitnexus-pr-review"),
            allowed_capabilities=("read", "git_diff", "shell_readonly"),
            artifact_summaries=[
                "architecture_plan: touch auth.py and add test_auth.py",
                "implementation_report: changed auth.py",
            ],
            evidence_refs=["team_artifact=12", "changed_file=wlcodex/auth.py"],
            resume_state="implementation completed; audit is next",
            open_questions=["whether new test covers null token path"],
            output_schema="audit_report",
            token_budget=900,
        )
    )

    rendered = packet.render()
    canonical = packet.as_json()

    assert "role: auditor" in rendered
    assert "model_profile: codex_gpt" in rendered
    assert "full transcript" not in rendered.lower()
    assert "team_artifact=12" in rendered
    assert canonical["conversation_id"] == 3
    assert canonical["orchestration_run_id"] == 5
    assert canonical["skills"] == ["code-review", "gitnexus-pr-review"]
    assert canonical["resume_state"] == "implementation completed; audit is next"
    assert packet.within_budget() is True


def test_context_packet_trims_large_artifact_summaries() -> None:
    role = TeamRoleCatalog.default().role(RoleId.IMPLEMENTER)
    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=1,
            agent_job_id=2,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="claude_deepseek",
            user_goal="修复状态漂移",
            workspace_alias="lightfeev2",
            skills=("test-driven-development",),
            allowed_capabilities=("read", "write", "shell", "tests"),
            artifact_summaries=["x" * 8000],
            evidence_refs=["log=runtime_events:10"],
            resume_state="architecture accepted; implementation is next",
            output_schema="implementation_report",
            token_budget=500,
        )
    )

    assert packet.within_budget() is True
    assert len(packet.render()) <= 2200
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_context.py -q
```

Expected: fails because `wlcodex.team_context` does not exist.

- [ ] **Step 3: Implement Context Packet compiler**

Create `wlcodex/team_context.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wlcodex.context_packets import approx_tokens, trim_to_budget
from wlcodex.team_roles import TeamRole


@dataclass(frozen=True)
class TeamContextInput:
    team_run_id: int
    agent_job_id: int
    conversation_id: int
    orchestration_run_id: int | None
    role: TeamRole
    model_profile: str
    user_goal: str
    workspace_alias: str
    skills: tuple[str, ...] = ()
    allowed_capabilities: tuple[str, ...] = ()
    artifact_summaries: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    resume_state: str = ""
    open_questions: list[str] = field(default_factory=list)
    output_schema: str = ""
    token_budget: int = 1200


@dataclass(frozen=True)
class TeamContextPacket:
    data: TeamContextInput
    artifact_summary_text: str

    def as_json(self) -> dict[str, Any]:
        role = self.data.role
        return {
            "team_run_id": self.data.team_run_id,
            "agent_job_id": self.data.agent_job_id,
            "conversation_id": self.data.conversation_id,
            "orchestration_run_id": self.data.orchestration_run_id,
            "role": role.role_id.value,
            "role_display_name": role.display_name,
            "model_profile": self.data.model_profile,
            "workspace": self.data.workspace_alias,
            "user_goal": self.data.user_goal,
            "role_mission": role.mission,
            "skills": list(self.data.skills or role.skills),
            "allowed_capabilities": list(self.data.allowed_capabilities or role.allowed_capabilities),
            "forbidden_actions": list(role.forbidden_actions),
            "artifact_summaries": list(self.data.artifact_summaries),
            "evidence_refs": list(self.data.evidence_refs),
            "resume_state": self.data.resume_state,
            "open_questions": list(self.data.open_questions),
            "required_output_schema": self.data.output_schema or role.required_artifact_type,
        }

    def render(self) -> str:
        canonical = self.as_json()
        lines = [
            f"team_run_id: {canonical['team_run_id']}",
            f"agent_job_id: {canonical['agent_job_id']}",
            f"conversation_id: {canonical['conversation_id']}",
            f"orchestration_run_id: {canonical['orchestration_run_id']}",
            f"role: {canonical['role']}",
            f"role_display_name: {canonical['role_display_name']}",
            f"model_profile: {canonical['model_profile']}",
            f"workspace: {canonical['workspace']}",
            f"user_goal: {canonical['user_goal']}",
            f"role_mission: {canonical['role_mission']}",
            f"skills: {', '.join(canonical['skills'])}",
            f"allowed_capabilities: {', '.join(canonical['allowed_capabilities'])}",
            f"forbidden_actions: {', '.join(canonical['forbidden_actions'])}",
            "context_policy: Use this packet and evidence references. Do not ask for or rely on full chat history.",
            f"resume_state: {canonical['resume_state']}",
            f"open_questions: {'; '.join(canonical['open_questions'])}",
            f"artifact_summaries: {self.artifact_summary_text}",
            f"evidence_refs: {', '.join(canonical['evidence_refs'])}",
            f"required_output_schema: {canonical['required_output_schema']}",
            f"token_budget: {self.data.token_budget}",
        ]
        return "\n".join(lines)

    def within_budget(self) -> bool:
        return approx_tokens(self.render()) <= self.data.token_budget


def build_team_context_packet(data: TeamContextInput) -> TeamContextPacket:
    fixed_overhead = 700
    summary_budget = max(100, data.token_budget - fixed_overhead)
    summary_text = trim_to_budget("; ".join(data.artifact_summaries), summary_budget)
    packet = TeamContextPacket(data=data, artifact_summary_text=summary_text)
    if packet.within_budget():
        return packet
    tighter_budget = max(40, data.token_budget // 3)
    return TeamContextPacket(
        data=data,
        artifact_summary_text=trim_to_budget(summary_text, tighter_budget),
    )
```

- [ ] **Step 4: Verify Task 4**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_context.py -q
```

Expected: all selected context tests pass.

## Task 4A: Context Packet v2 Memory And Activation Fields

**Files:**
- Modify: `wlcodex/team_context.py`
- Modify: `wlcodex/controller.py`
- Test: `tests/test_team_context.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing Context Packet v2 test**

Add to `tests/test_team_context.py`:

```python
from datetime import UTC, datetime

from wlcodex.team_memory import InstinctMemory


def test_context_packet_includes_memory_as_historical_advice() -> None:
    role = TeamRoleCatalog.default().role(RoleId.AUDITOR)
    instinct = InstinctMemory(
        instinct_id="instinct:wlcodex:audit:changed-files",
        scope="project",
        workspace_alias="wlcodex",
        role="auditor",
        domain="audit",
        trigger="verifier packet missed changed files",
        action="Include changed files before audit starts.",
        confidence=0.8,
        evidence_refs=("team_observation=4",),
        status="active",
        created_at=datetime(2026, 5, 25, tzinfo=UTC),
        last_validated_at=datetime(2026, 5, 25, tzinfo=UTC),
    )

    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=9,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="codex_gpt",
            user_goal="验收实现",
            workspace_alias="wlcodex",
            skills=("gitnexus-pr-review",),
            allowed_capabilities=("read", "git_diff"),
            artifact_summaries=["implementation_report: changed auth.py"],
            evidence_refs=["changed_file=wlcodex/auth.py"],
            resume_state="implementation completed; audit is next",
            output_schema="audit_report",
            relevant_instincts=(instinct.as_packet_item(),),
            capability_budget={
                "max_skills": 2,
                "max_tools": 4,
                "max_memory_snippets": 2,
                "max_prompt_tokens": 900,
            },
            skill_activations=("gitnexus-pr-review", "git_diff"),
            source_refs=("team_artifact=12", "team_instinct=1"),
            token_budget=900,
        )
    )

    canonical = packet.as_json()
    rendered = packet.render()

    assert canonical["relevant_instincts"][0]["precedence"] == "historical_advice_current_evidence_wins"
    assert canonical["historical_context_policy"] == "current_user_goal_and_current_evidence_override_history"
    assert canonical["capability_budget"]["max_memory_snippets"] == 2
    assert canonical["skill_activations"] == ["gitnexus-pr-review", "git_diff"]
    assert "historical_context_policy" in rendered
    assert "current evidence override history" in rendered
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_context.py::test_context_packet_includes_memory_as_historical_advice -q
```

Expected: fails because `TeamContextInput` has no memory or activation fields.

- [ ] **Step 3: Extend Context Packet input and JSON**

In `wlcodex/team_context.py`, add these fields to `TeamContextInput`:

```python
    relevant_instincts: tuple[dict[str, object], ...] = ()
    capability_budget: dict[str, int] = field(default_factory=dict)
    skill_activations: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
```

In `TeamContextPacket.as_json`, add:

```python
            "relevant_instincts": list(self.data.relevant_instincts),
            "capability_budget": dict(self.data.capability_budget),
            "skill_activations": list(self.data.skill_activations),
            "historical_context_policy": "current_user_goal_and_current_evidence_override_history",
            "source_refs": list(self.data.source_refs),
```

- [ ] **Step 4: Render stale-replay guard**

In `TeamContextPacket.render`, add these lines after `context_policy`:

```python
            "historical_context_policy: current evidence override history; memories and prior artifacts are advisory only.",
            f"capability_budget: {canonical['capability_budget']}",
            f"skill_activations: {', '.join(canonical['skill_activations'])}",
            f"relevant_instincts: {canonical['relevant_instincts']}",
            f"source_refs: {', '.join(canonical['source_refs'])}",
```

Keep the existing artifact-summary trimming. If the rendered packet exceeds the
budget, trim `artifact_summary_text` first, then trim `relevant_instincts` to
the first item, then keep only instinct ids plus actions.

- [ ] **Step 5: Record activations when controller builds a packet**

In `_build_team_context_packet_for_job(...)`, after selecting skills/tools and
building the packet, call `record_team_skill_activation(...)` once for each
skill, tool, and selected instinct:

```python
for skill_id in selected_skill_ids:
    self._ledger.record_team_skill_activation(
        team_run_id=team_run.id,
        agent_job_id=agent_job.id,
        activation_type="skill",
        activation_id=skill_id,
        source="capability_budget",
        token_cost=0,
    )
for tool_id in selected_tool_ids:
    self._ledger.record_team_skill_activation(
        team_run_id=team_run.id,
        agent_job_id=agent_job.id,
        activation_type="tool",
        activation_id=tool_id,
        source="capability_budget",
        token_cost=0,
    )
for instinct in selected_instincts:
    self._ledger.record_team_skill_activation(
        team_run_id=team_run.id,
        agent_job_id=agent_job.id,
        activation_type="memory",
        activation_id=str(instinct["id"]),
        source="instinct_memory",
        token_cost=0,
    )
```

Use real token costs from `SkillDefinition` when the selected skill object is
available in controller scope.

- [ ] **Step 6: Verify Task 4A**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_context.py tests/test_controller_flow.py -q
```

Expected: selected context and controller tests pass or only fail at later
controller wiring tasks that have not run yet.

## Task 5: Runtime Events And Callback Actions

**Files:**
- Modify: `wlcodex/runtime_events.py`
- Modify: `wlcodex/conversation_callback.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing callback/action expectations**

In the existing callback tests in `tests/test_controller_flow.py`, add assertions that generated auto buttons can include:

```python
assert "auto_send_to_codex" in callback_actions
assert "team_view_status" in callback_actions
```

Use the existing button extraction helper in the file. If the file has no helper, add:

```python
def _callback_actions(buttons: list[list[dict[str, str]]]) -> set[str]:
    actions: set[str] = set()
    for row in buttons:
        for button in row:
            callback_data = button.get("callback_data", "")
            parts = callback_data.split(":")
            if len(parts) >= 3:
                actions.add(parts[2])
    return actions
```

- [ ] **Step 2: Add event constants**

Add constants to `wlcodex/runtime_events.py`:

```python
TEAM_RUN_REQUESTED = "team.run.requested"
TEAM_RUN_ROUTED = "team.run.routed"
TEAM_RUN_STARTED = "team.run.started"
TEAM_RUN_COMPLETED = "team.run.completed"
TEAM_RUN_FAILED = "team.run.failed"
TEAM_AGENT_JOB_QUEUED = "team.agent_job.queued"
TEAM_AGENT_JOB_STARTED = "team.agent_job.started"
TEAM_AGENT_JOB_COMPLETED = "team.agent_job.completed"
TEAM_AGENT_JOB_FAILED = "team.agent_job.failed"
TEAM_CONTEXT_PACKET_RECORDED = "team.context_packet.recorded"
TEAM_ARTIFACT_RECORDED = "team.artifact.recorded"
TEAM_GATE_PASSED = "team.gate.passed"
TEAM_GATE_FAILED = "team.gate.failed"
TEAM_ASSIGNMENT_SELECTED = "team.assignment.selected"
TEAM_ASSIGNMENT_FALLBACK_USED = "team.assignment.fallback_used"
TEAM_SKILL_ACTIVATED = "team.skill_activated"
TEAM_CAPABILITY_BUDGET_APPLIED = "team.capability_budget.applied"
TEAM_OBSERVATION_RECORDED = "team.observation.recorded"
TEAM_INSTINCT_PROPOSED = "team.instinct.proposed"
TEAM_INSTINCT_PROMOTED = "team.instinct.promoted"
TEAM_INSTINCT_DEPRECATED = "team.instinct.deprecated"
TEAM_INSTINCT_SELECTED = "team.instinct.selected"
```

Keep names inside the existing `EventType` class.

- [ ] **Step 3: Add callback constants**

Add to `wlcodex/conversation_callback.py`:

```python
AUTO_SEND_TO_CODEX = "auto_send_to_codex"
TEAM_VIEW_STATUS = "team_view_status"
TEAM_VIEW_ARTIFACTS = "team_view_artifacts"
```

Use the same exported constant style as the existing staged-auto actions.

- [ ] **Step 4: Verify Task 5**

Run:

```bash
.venv/bin/python -m pytest tests/test_controller_flow.py -q
```

Expected: existing tests may still fail until Task 6 wires buttons. New constants import cleanly.

## Task 6: Role-Aware Staged Auto Buttons And TeamRun Creation

**Files:**
- Modify: `wlcodex/auto_workflow.py`
- Modify: `wlcodex/controller.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Write failing controller test for role-aware `/auto`**

Add to `tests/test_controller_flow.py`:

```python
async def test_auto_final_plan_offers_claude_and_codex_implementers(controller, ledger) -> None:
    response = await controller.handle_auto_mode(
        AutoModeCommand("修复登录偶发失败"),
        {"chat_id": 123, "user_id": 456},
    )
    assert response.already_rendered or response.text

    convo = ledger.get_active_conversation(123)
    orch = ledger.get_latest_active_auto_run(convo.id)
    ledger.update_orchestration_run(
        orch.id,
        status="needs_user",
        current_step="draft_ready",
        last_codex_analysis="方案：修改 auth.py，并运行 pytest tests/test_auth.py -q",
    )

    callback = ConversationCallback(
        conversation_id=convo.id,
        action=AUTO_VIEW_STATUS,
        raw="",
    )
    status_response = await controller.handle_conversation_callback(callback)

    actions = _callback_actions(status_response.buttons)
    assert "auto_send_to_claude" in actions
    assert "auto_send_to_codex" in actions
```

Adjust imports to match existing test style.

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_controller_flow.py::test_auto_final_plan_offers_claude_and_codex_implementers -q
```

Expected: fails because `auto_send_to_codex` button is not generated.

- [ ] **Step 3: Create TeamRun during `/auto`**

In `CommandController.handle_auto_mode`, after `create_orchestration_run`, create a TeamRun linked to the orchestration run:

```python
team_run = self._ledger.create_team_run(
    conversation_id=active.id,
    orchestration_run_id=orch_run.id,
    goal=command.prompt,
    route="staged_auto",
    risk_level="medium",
)
```

Record a Director/Architect job for the planning run. Use the configured
Architect profile; the first-release default is `codex_gpt`.

```python
architect_profile = self._architect_model_profile
architect_job = self._ledger.create_team_agent_job(
    team_run_id=team_run.id,
    role="architect",
    model_profile=architect_profile,
    status="running",
    agent_run_id=codex_analysis_run.id,
)
self._ledger.record_team_assignment(
    team_run_id=team_run.id,
    role="architect",
    model_profile=architect_profile,
    selected_by="policy",
)
```

Before the Codex planning backend turn starts, compile and record the Architect
Context Packet for `architect_job` with
`record_team_context_packet(...)`. Emit `team.run.started`,
`team.agent_job.started`, and `team.context_packet.recorded` runtime events
using the existing `_emit_event` helper.

- [ ] **Step 4: Add role-aware implementation buttons**

Modify `build_auto_stage_buttons` in `wlcodex/auto_workflow.py` so `AUTO_DRAFT_READY` and `AUTO_RETRY_READY` can include both:

```python
{"text": "交给 Claude 执行", "callback_data": encode_conversation_callback(conversation_id, AUTO_SEND_TO_CLAUDE)}
{"text": "交给 Codex 执行", "callback_data": encode_conversation_callback(conversation_id, AUTO_SEND_TO_CODEX)}
```

Keep the existing Claude button. Add the Codex button only when the caller passes a flag such as `codex_implementer_enabled=True`. Default the flag to `False` to avoid changing old call sites until the controller opts in.

- [ ] **Step 5: Opt staged-auto status into Codex Implementer button**

In controller call sites that render final-plan buttons, pass `codex_implementer_enabled=True` when:

```python
self._adaptive_team_enabled
and "codex_gpt" in self._implementer_model_profiles
```

`CommandController` currently does not retain `AppConfig`. Add constructor parameters
`adaptive_team_enabled: bool = True` and
`implementer_model_profiles: tuple[str, ...] = ("claude_deepseek", "codex_gpt")`.
Also add single-profile constructor parameters for the current defaults:
`architect_model_profile: str = "codex_gpt"`,
`investigator_model_profile: str = "codex_gpt"`,
`tester_model_profile: str = "codex_gpt"`, and
`auditor_model_profile: str = "codex_gpt"`.
Store them on `self`, wire them from `main.py`, and use those fields in role
assignment and button decisions. Do not import global config from
`controller.py`.

- [ ] **Step 6: Verify Task 6**

Run:

```bash
.venv/bin/python -m pytest tests/test_controller_flow.py -q
```

Expected: controller flow tests pass or only fail at Task 7's missing Codex Implementer handler.

## Task 7: Codex Implementer Callback Path

**Files:**
- Modify: `wlcodex/controller.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Write failing Codex Implementer test**

Add to `tests/test_controller_flow.py`:

```python
async def test_auto_send_to_codex_starts_implementer_agent_job(controller, ledger) -> None:
    convo = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Codex implementer",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="修复 bug")
    ledger.update_orchestration_run(
        orch.id,
        status="needs_user",
        current_step="draft_ready",
        last_codex_analysis="请按方案修改 wlcodex/example.py，并运行 pytest tests/test_example.py -q",
    )
    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="修复 bug",
        route="staged_auto",
        risk_level="medium",
    )

    response = await controller.handle_conversation_callback(
        ConversationCallback(
            conversation_id=convo.id,
            action=AUTO_SEND_TO_CODEX,
            raw="",
        )
    )

    assert "Codex 开始执行" in response.text
    jobs = ledger.list_team_agent_jobs(team.id)
    assert any(job.role == "implementer" and job.model_profile == "codex_gpt" for job in jobs)
```

Use local fixtures and imports already present in `tests/test_controller_flow.py`.

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_controller_flow.py::test_auto_send_to_codex_starts_implementer_agent_job -q
```

Expected: fails because `AUTO_SEND_TO_CODEX` is not handled.

- [ ] **Step 3: Add callback dispatch**

In `CommandController.handle_conversation_callback`, route `AUTO_SEND_TO_CODEX` to a new private method:

```python
if callback.action == AUTO_SEND_TO_CODEX:
    return await self._handle_auto_send_to_codex(callback)
```

- [ ] **Step 4: Implement `_handle_auto_send_to_codex`**

Add a method mirroring `_handle_auto_send_to_claude`, but using Codex direct execution:

```python
async def _handle_auto_send_to_codex(
    self, callback: ConversationCallback
) -> ControllerResponse:
    convo = self._ledger.get_conversation(callback.conversation_id)
    orch_run = self._latest_active_auto_run(callback.conversation_id)
    if orch_run is None:
        return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
    if orch_run.current_step not in (AUTO_DRAFT_READY, AUTO_RETRY_READY):
        return ControllerResponse(
            f"当前阶段是 {auto_stage_label(orch_run.current_step)}，不能启动 Codex 执行。"
        )

    codex_prompt = (orch_run.last_codex_analysis or "").strip()
    if not codex_prompt:
        return ControllerResponse("没有可执行的最终方案，请先生成最终方案。")

    self._ledger.update_orchestration_run(
        orch_run.id,
        status="running",
        current_step=AUTO_CLAUDE_RUNNING,
    )

    task = self._reserve_execution_lease(
        conversation_id=convo.id,
        workspace_alias=convo.workspace_alias,
        prompt=codex_prompt,
        telegram_chat_id=convo.chat_id,
        purpose="auto_codex_implementation",
    )
    agent_run = self._ledger.create_agent_run(
        conversation_id=convo.id,
        agent="codex",
        role="auto_implementation",
        hidden_task_id=task.id,
        prompt_packet_summary=codex_prompt[:200],
    )
    self._ledger.update_agent_run_status(agent_run.id, "running")

    team = self._ledger.get_team_run_for_orchestration(orch_run.id)
    if team is not None:
        implementer_profile = "codex_gpt"
        self._ledger.record_team_assignment(
            team_run_id=team.id,
            role="implementer",
            model_profile=implementer_profile,
            selected_by="user",
        )
        implementer_job = self._ledger.create_team_agent_job(
            team_run_id=team.id,
            role="implementer",
            model_profile=implementer_profile,
            status="running",
            agent_run_id=agent_run.id,
        )
        packet = self._build_team_context_packet_for_job(
            team_run=team,
            agent_job=implementer_job,
            role="implementer",
            model_profile=implementer_profile,
            resume_state="final plan accepted; Codex implementation selected by user",
            output_schema="implementation_report",
        )
        self._ledger.record_team_context_packet(
            team_run_id=team.id,
            agent_job_id=implementer_job.id,
            packet_json=packet.as_json(),
            prompt_text=packet.render(),
            prompt_tokens=approx_tokens(packet.render()),
        )

    workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)
    await self._start_codex_turn_for_conversation(
        active=convo,
        task=task,
        workspace_path=workspace_path,
        prompt=codex_prompt,
        interaction_mode="general",
    )

    return ControllerResponse("Codex 开始执行。完成后请点「Codex 验收」。")
```

Add `_build_team_context_packet_for_job(...)` as a narrow controller helper
that gathers existing artifacts for the TeamRun, applies the configured
role skills/capabilities, and calls `build_team_context_packet(...)`. Import
`approx_tokens` from `wlcodex.context_packets`.

For the first release, reuse the existing implementation-running and
implementation-done staged-auto steps. Add Codex-specific status text through
agent/job metadata instead of adding `AUTO_CODEX_RUNNING` or `AUTO_CODEX_DONE`.

- [ ] **Step 5: Mark Codex implementation completion**

Where Codex task completion currently updates conversation/task state, add a small hook that detects an active staged-auto Codex Implementer agent run and transitions the orchestration to the same done stage used after Claude implementation. Keep this hook narrow:

```python
if agent_run.agent == "codex" and agent_run.role == "auto_implementation":
    self._transition_auto_claude_completed(
        conversation_id,
        agent_status="done",
        completion_summary=completion_summary,
    )
```

Keep the existing transition helper name for this release. Rename it only in a
separate refactor plan so this feature stays focused.

- [ ] **Step 6: Verify Task 7**

Run:

```bash
.venv/bin/python -m pytest tests/test_controller_flow.py tests/test_telegram_handlers.py -q
```

Expected: selected controller and Telegram tests pass.

## Task 8: Artifact Recording For Existing Auto Phases

**Files:**
- Modify: `wlcodex/controller.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Write failing artifact recording test**

Add to `tests/test_controller_flow.py`:

```python
async def test_auto_verification_records_audit_artifact(controller, ledger) -> None:
    convo = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Audit artifact",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="修复 bug")
    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="修复 bug",
        route="staged_auto",
        risk_level="medium",
    )

    ledger.record_team_artifact(
        team_run_id=team.id,
        agent_job_id=None,
        artifact_type="architecture_plan",
        summary="Plan ready",
        payload={"acceptance_criteria": ["pytest passes"]},
    )
    ledger.update_orchestration_run(
        orch.id,
        status="needs_user",
        current_step="claude_done",
        last_codex_analysis="Plan ready",
        last_claude_summary="Changed auth.py",
    )

    response = await controller.handle_conversation_callback(
        ConversationCallback(
            conversation_id=convo.id,
            action=AUTO_CODEX_VERIFY,
            raw="",
        )
    )

    assert response.text
    artifacts = ledger.list_team_artifacts(team.id)
    assert any(artifact.artifact_type in {"audit_report", "verification_request"} for artifact in artifacts)
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_controller_flow.py::test_auto_verification_records_audit_artifact -q
```

Expected: fails because verification artifacts are not recorded.

- [ ] **Step 3: Record architecture artifact after final plan**

In `_handle_auto_final_plan`, when the Codex final plan is accepted and stored in `last_codex_analysis`, record a `architecture_plan` artifact with:

```python
payload={
    "summary": orch_run.last_codex_analysis[:1200],
    "risk_level": "medium",
    "acceptance_criteria": [],
    "source": "last_codex_analysis",
}
```

For the first release, store acceptance criteria as an empty list unless a
dedicated parser already exists in the codebase. Keep the plan text in
`summary`; Gate A warns but does not block legacy staged-auto plans in
compatibility mode.

- [ ] **Step 4: Record implementation Context Packet before selected implementer starts**

In both `_handle_auto_send_to_claude` and `_handle_auto_send_to_codex`, create
the Implementer `team_agent_job`, build the Implementer Context Packet, and call
`record_team_context_packet(...)` before the backend run starts.

The packet must include:

```python
resume_state = "final plan accepted; implementation selected by user"
artifact_summaries = [
    f"architecture_plan: {orch_run.last_codex_analysis[:1200]}",
]
evidence_refs = [
    f"orchestration_run={orch_run.id}",
    f"conversation={convo.id}",
]
output_schema = "implementation_report"
```

This step is mandatory for both `claude_deepseek` and `codex_gpt` implementers
because they may run in different backend sessions.

- [ ] **Step 5: Record implementation artifact after selected implementer finishes**

In the completion paths for Claude Implementer and Codex Implementer, record `implementation_report`:

```python
payload={
    "summary": completion_summary[:1200],
    "changed_files": changed_files[:50],
    "diff_summary": diff_summary[:2000],
    "source_agent": agent_run.agent,
}
```

Use existing inspector/file evidence where available. Use empty lists only when the existing flow cannot collect evidence; the later Auditor packet must include that evidence is missing.

- [ ] **Step 6: Record audit Context Packet and request artifact when verification starts**

In `_handle_auto_codex_verify`, record a `verification_request` artifact before starting Codex verification:

```python
payload={
    "goal": goal,
    "codex_plan_summary": codex_analysis[:800],
    "implementation_summary": claude_summary[:1500],
    "changed_files": changed_files[:20],
    "diff_summary": diff_summary[:1500],
    "verify_round": verify_round,
}
```

Create the Auditor `team_agent_job`, build and store the Auditor Context Packet,
then start the configured Auditor backend run. The first-release default Auditor
profile is `codex_gpt`, but use the configured auditor profile field rather
than hard-coding that value. When verification completes, record `audit_report`
from `last_verification_result`.

After recording `audit_report`, run `observations_from_artifact(...)` and store
each result with `record_team_observation(...)`. For repeated blocking evidence,
call `candidate_instinct_from_observation(...)` and persist it with
`upsert_team_instinct(...)`. Emit `team.observation.recorded` and
`team.instinct.promoted` only after the ledger write succeeds.

- [ ] **Step 7: Verify Task 8**

Run:

```bash
.venv/bin/python -m pytest tests/test_controller_flow.py tests/test_db.py -q
```

Expected: selected controller and DB tests pass.

## Task 9: Cockpit Status Rendering

**Files:**
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/orchestration_progress_text.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Write failing status render test**

Add to `tests/test_status.py`:

```python
def test_role_aware_auto_status_lists_engineer_roles() -> None:
    text = render_team_status_summary(
        goal="修复登录偶发失败",
        route="staged_auto",
        roles=[
            ("architect", "codex_gpt", "done"),
            ("implementer", "claude_deepseek", "running"),
            ("auditor", "codex_gpt", "queued"),
        ],
        latest_artifacts=["architecture_plan: Plan ready"],
    )

    assert "架构工程师" in text
    assert "开发工程师" in text
    assert "审计工程师" in text
    assert "claude_deepseek" in text
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_status.py::test_role_aware_auto_status_lists_engineer_roles -q
```

Expected: fails because `render_team_status_summary` does not exist.

- [ ] **Step 3: Add status renderer**

Add `render_team_status_summary` to `wlcodex/status.py`:

```python
ROLE_LABELS = {
    "director": "总工程师",
    "investigator": "诊断工程师",
    "architect": "架构工程师",
    "implementer": "开发工程师",
    "tester": "测试工程师",
    "auditor": "审计工程师",
}


def render_team_status_summary(
    *,
    goal: str,
    route: str,
    roles: list[tuple[str, str, str]],
    latest_artifacts: list[str],
) -> str:
    lines = [
        f"工程队：{goal}",
        f"路线：{route}",
    ]
    for role, model_profile, status in roles:
        label = ROLE_LABELS.get(role, role)
        lines.append(f"- {label} / {model_profile}：{status}")
    if latest_artifacts:
        lines.append("")
        lines.append("最近证据：")
        for artifact in latest_artifacts[:4]:
            lines.append(f"- {artifact}")
    return "\n".join(lines)
```

- [ ] **Step 4: Integrate with existing status paths**

Where staged-auto status currently renders `orchestration_runs`, look up
`team_run` by `orchestration_run_id`. Append the role-aware summary under the
current stage text only when a TeamRun exists. Do not remove existing auto
status text.

- [ ] **Step 5: Verify Task 9**

Run:

```bash
.venv/bin/python -m pytest tests/test_status.py tests/test_controller_flow.py -q
```

Expected: selected status and controller tests pass.

## Task 10: Recovery And Final Verification

**Files:**
- Modify: `wlcodex/db.py`
- Modify: `wlcodex/main.py`
- Test: `tests/test_runtime_state.py`
- Test: `tests/test_main_composition.py`

- [ ] **Step 1: Add projection persistence test**

Add a DB persistence test proving completed team artifacts remain visible after ledger reload. Active TeamRun runtime-event rebuild is later scope:

```python
def test_team_artifacts_survive_ledger_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "wlcodex.sqlite3"
    ledger = Ledger(db_path)
    ledger.migrate()
    convo = ledger.create_conversation(
        chat_id=1,
        user_id=2,
        title="Recover team",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    orch = ledger.create_orchestration_run(conversation_id=convo.id, goal="fix")
    team = ledger.create_team_run(
        conversation_id=convo.id,
        orchestration_run_id=orch.id,
        goal="fix",
        route="staged_auto",
        risk_level="medium",
    )
    ledger.record_team_artifact(
        team_run_id=team.id,
        agent_job_id=None,
        artifact_type="architecture_plan",
        summary="Plan survives",
        payload={"acceptance_criteria": ["pass"]},
    )
    ledger.close()

    reopened = Ledger(db_path)
    reopened.migrate()

    artifacts = reopened.list_team_artifacts(team.id)
    assert artifacts[0].summary == "Plan survives"
```

Use the existing `Ledger` constructor/close pattern in the test suite.

- [ ] **Step 2: Run selected persistence tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_runtime_state.py tests/test_main_composition.py -q
```

Expected: selected tests pass.

- [ ] **Step 3: Run all adaptive-team tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_team_roles.py tests/test_team_capabilities.py tests/test_team_memory.py tests/test_team_observer.py tests/test_team_artifacts.py tests/test_team_context.py tests/test_config.py tests/test_db.py tests/test_controller_flow.py tests/test_status.py -q
```

Expected: all selected adaptive-team tests pass.

- [ ] **Step 4: Run full test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: full suite passes.

- [ ] **Step 5: Check diff hygiene**

Run:

```bash
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 6: GitNexus change detection**

Run:

```text
mcp__gitnexus__.detect_changes({
  "repo": "wlcodex",
  "scope": "all"
})
```

Expected: affected symbols and execution flows match config, DB, staged-auto controller, callback, context-packet, status, and tests.

## Execution Handoff

Plan complete when this document and the paired spec are reviewed by the user.

Recommended implementation approach:

1. Subagent-Driven: one fresh implementation agent per task, with review between tasks.
2. Inline Execution: execute tasks in this session with checkpoints after each task.

Use Subagent-Driven for this feature unless the user explicitly prefers inline execution, because Tasks 1-10 plus the capability and memory inserts touch independent but connected surfaces and benefit from fresh-context review.
