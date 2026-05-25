from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    roles: tuple[str, ...]
    triggers: tuple[str, ...]
    required_tools: tuple[str, ...]
    token_cost: int


@dataclass(frozen=True)
class SkillCatalog:
    skills: tuple[SkillDefinition, ...]

    def __init__(self, skills: Sequence[SkillDefinition]) -> None:
        object.__setattr__(self, "skills", tuple(skills))

    def for_role(self, role: str) -> tuple[SkillDefinition, ...]:
        return tuple(skill for skill in self.skills if role in skill.roles)


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


def _trigger_score(skill: SkillDefinition, task: str) -> int:
    normalized_task = task.casefold()
    score = 0
    for trigger in skill.triggers:
        normalized_trigger = trigger.casefold()
        if normalized_trigger and normalized_trigger in normalized_task:
            score += len(normalized_trigger.split())
    return score


def _has_required_tools(
    skill: SkillDefinition, available_tools: Sequence[str]
) -> bool:
    available = set(available_tools)
    return set(skill.required_tools).issubset(available)


def select_capabilities(
    *,
    catalog: SkillCatalog,
    role: str,
    task: str,
    available_tools: Sequence[str],
    budget: CapabilityBudget,
) -> CapabilitySelection:
    scored_skills = [
        (_trigger_score(skill, task), skill)
        for skill in catalog.for_role(role)
        if _has_required_tools(skill, available_tools)
    ]
    ranked_skills = sorted(
        (
            (score, skill)
            for score, skill in scored_skills
            if score > 0
        ),
        key=lambda item: (-item[0], item[1].token_cost, item[1].skill_id),
    )

    selected: list[SkillDefinition] = []
    selected_tokens = 0
    selected_required_tools: set[str] = set()
    for _, skill in ranked_skills:
        if len(selected) >= budget.max_skills:
            break
        if selected_tokens + skill.token_cost > budget.max_prompt_tokens:
            continue
        required_tools = selected_required_tools | set(skill.required_tools)
        if len(required_tools) > budget.max_tools:
            continue
        selected.append(skill)
        selected_tokens += skill.token_cost
        selected_required_tools = required_tools

    selected_tools = sorted(selected_required_tools)
    selected_tools.extend(
        tool
        for tool in sorted(set(available_tools) - selected_required_tools)
        if len(selected_tools) < budget.max_tools
    )

    return CapabilitySelection(
        skills=tuple(selected),
        tools=tuple(selected_tools),
        budget=budget,
    )


def audit_role_capability_config(
    role_tools: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    findings: list[str] = []
    from wlcodex.team_roles import RoleId, TeamRoleCatalog

    catalog = TeamRoleCatalog.default()
    for role, tools in role_tools.items():
        try:
            role_def = catalog.role(RoleId(role))
        except ValueError:
            continue
        forbidden_tools: set[str] = set()
        for action in role_def.forbidden_actions:
            if action == "deploy":
                forbidden_tools.add("deploy")
            elif action == "destructive_command":
                forbidden_tools.add("destructive_command")
            elif action == "edit_product_code":
                forbidden_tools.update({"shell", "write"})
            elif action == "secret_read":
                forbidden_tools.add("secret_read")
        if not role_def.can_write_product_code:
            forbidden_tools.update({"shell", "write"})
        for tool in sorted(set(tools) & forbidden_tools):
            findings.append(f"{role} has forbidden capability {tool}")
    return tuple(findings)


__all__ = [
    "CapabilityBudget",
    "CapabilitySelection",
    "SkillCatalog",
    "SkillDefinition",
    "audit_role_capability_config",
    "select_capabilities",
]
