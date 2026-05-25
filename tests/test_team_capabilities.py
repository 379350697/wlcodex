import wlcodex.team_capabilities as team_capabilities
from wlcodex.team_capabilities import (
    CapabilityBudget,
    SkillCatalog,
    SkillDefinition,
    audit_role_capability_config,
    select_capabilities,
)


def test_skill_catalog_selects_role_and_trigger_matches_under_budget():
    catalog = SkillCatalog(
        [
            SkillDefinition(
                skill_id="gitnexus-pr-review",
                roles=("auditor",),
                triggers=("audit", "review", "diff"),
                required_tools=("read", "git_diff"),
                token_cost=160,
            ),
            SkillDefinition(
                skill_id="systematic-debugging",
                roles=("investigator",),
                triggers=("log", "failure", "root cause"),
                required_tools=("read", "logs"),
                token_cost=180,
            ),
            SkillDefinition(
                skill_id="security-review",
                roles=("auditor", "architect"),
                triggers=("security", "permission", "prompt injection"),
                required_tools=("read", "git_diff"),
                token_cost=200,
            ),
        ]
    )
    budget = CapabilityBudget(
        max_skills=2,
        max_tools=4,
        max_memory_snippets=3,
        max_prompt_tokens=700,
    )

    selection = select_capabilities(
        catalog=catalog,
        role="auditor",
        task="audit the diff for prompt injection and permission risk",
        available_tools=("read", "git_diff", "shell_readonly"),
        budget=budget,
    )

    assert [skill.skill_id for skill in selection.skills] == [
        "security-review",
        "gitnexus-pr-review",
    ]
    assert selection.tools == ("git_diff", "read", "shell_readonly")
    assert selection.budget.max_memory_snippets == 3


def test_capability_selection_excludes_skills_when_required_tools_are_missing():
    catalog = SkillCatalog(
        [
            SkillDefinition(
                skill_id="log-analysis",
                roles=("investigator",),
                triggers=("log",),
                required_tools=("read", "logs"),
                token_cost=120,
            )
        ]
    )
    budget = CapabilityBudget(
        max_skills=3,
        max_tools=3,
        max_memory_snippets=2,
        max_prompt_tokens=500,
    )

    selection = select_capabilities(
        catalog=catalog,
        role="investigator",
        task="inspect the log failure",
        available_tools=("read",),
        budget=budget,
    )

    assert selection.skills == ()
    assert selection.tools == ("read",)


def test_capability_selection_includes_required_tools_before_optional_tools():
    catalog = SkillCatalog(
        [
            SkillDefinition(
                skill_id="z-tool-skill",
                roles=("auditor",),
                triggers=("audit",),
                required_tools=("z_tool",),
                token_cost=100,
            )
        ]
    )
    budget = CapabilityBudget(
        max_skills=1,
        max_tools=1,
        max_memory_snippets=1,
        max_prompt_tokens=500,
    )

    selection = select_capabilities(
        catalog=catalog,
        role="auditor",
        task="audit the capability plan",
        available_tools=("z_tool", "a_tool"),
        budget=budget,
    )

    assert [skill.skill_id for skill in selection.skills] == ["z-tool-skill"]
    assert selection.tools == ("z_tool",)


def test_capability_selection_skips_over_budget_skill_and_selects_later_match():
    catalog = SkillCatalog(
        [
            SkillDefinition(
                skill_id="expensive-review",
                roles=("auditor",),
                triggers=("audit", "review"),
                required_tools=("read",),
                token_cost=800,
            ),
            SkillDefinition(
                skill_id="cheap-review",
                roles=("auditor",),
                triggers=("audit",),
                required_tools=("read",),
                token_cost=100,
            ),
        ]
    )
    budget = CapabilityBudget(
        max_skills=2,
        max_tools=2,
        max_memory_snippets=1,
        max_prompt_tokens=200,
    )

    selection = select_capabilities(
        catalog=catalog,
        role="auditor",
        task="audit and review the diff",
        available_tools=("read",),
        budget=budget,
    )

    assert [skill.skill_id for skill in selection.skills] == ["cheap-review"]
    assert selection.tools == ("read",)


def test_module_exports_only_public_capability_api():
    assert "_has_required_tools" not in team_capabilities.__all__
    assert "_trigger_score" not in team_capabilities.__all__


def test_role_capability_audit_flags_write_tools_on_auditor():
    findings = audit_role_capability_config(
        {
            "auditor": ("read", "write", "git_diff"),
            "investigator": ("read", "shell_readonly"),
            "implementer": ("read", "write", "shell"),
        }
    )

    assert findings == ("auditor has forbidden capability write",)


def test_role_capability_audit_uses_catalog_role_boundaries():
    findings = audit_role_capability_config(
        {
            "auditor": ("read", "shell", "shell_readonly"),
            "director": ("ledger", "write"),
            "implementer": ("read", "write", "shell", "deploy"),
        }
    )

    assert findings == (
        "auditor has forbidden capability shell",
        "director has forbidden capability write",
        "implementer has forbidden capability deploy",
    )


def test_default_role_catalog_passes_capability_audit():
    from wlcodex.team_roles import TeamRoleCatalog

    catalog = TeamRoleCatalog.default()
    role_tools = {
        role.role_id.value: role.allowed_capabilities
        for role in catalog.roles.values()
    }

    assert audit_role_capability_config(role_tools) == ()
