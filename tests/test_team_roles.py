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


def test_tester_default_capabilities_do_not_include_product_code_shell() -> None:
    catalog = TeamRoleCatalog.default()

    tester = catalog.role(RoleId.TESTER)

    assert tester.can_write_product_code is False
    assert "shell" not in tester.allowed_capabilities
    assert "shell_readonly" in tester.allowed_capabilities
    assert "write" not in tester.allowed_capabilities


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


def test_assignment_policy_uses_high_risk_overrides_for_high_and_critical() -> None:
    policy = AssignmentPolicy(
        defaults={RoleId.ARCHITECT: [ModelProfileRef("codex_gpt")]},
        high_risk_overrides={RoleId.ARCHITECT: [ModelProfileRef("strong_model")]},
    )

    assert policy.choices_for(RoleId.ARCHITECT, risk_level="medium") == [
        ModelProfileRef("codex_gpt")
    ]
    assert policy.choices_for(RoleId.ARCHITECT, risk_level="high") == [
        ModelProfileRef("strong_model")
    ]
    assert policy.choices_for(RoleId.ARCHITECT, risk_level="critical") == [
        ModelProfileRef("strong_model")
    ]


def test_role_assignments_can_override_tools_and_skills() -> None:
    policy = AssignmentPolicy(
        defaults={RoleId.INVESTIGATOR: [ModelProfileRef("codex_gpt")]},
        role_skills={RoleId.INVESTIGATOR: ("systematic-debugging", "gitnexus-exploring")},
        role_capabilities={RoleId.INVESTIGATOR: ("read", "shell_readonly", "logs")},
    )

    assert policy.skills_for(RoleId.INVESTIGATOR) == ("systematic-debugging", "gitnexus-exploring")
    assert policy.capabilities_for(RoleId.INVESTIGATOR) == ("read", "shell_readonly", "logs")


def test_assignment_policy_copies_defaults_and_returns_fresh_choices() -> None:
    defaults = {RoleId.IMPLEMENTER: [ModelProfileRef("codex_gpt")]}
    policy = AssignmentPolicy(defaults=defaults)

    defaults[RoleId.IMPLEMENTER].append(ModelProfileRef("claude_deepseek"))
    choices = policy.choices_for(RoleId.IMPLEMENTER)
    choices.append(ModelProfileRef("another_profile"))

    assert policy.defaults[RoleId.IMPLEMENTER] == (ModelProfileRef("codex_gpt"),)
    assert policy.choices_for(RoleId.IMPLEMENTER) == [ModelProfileRef("codex_gpt")]
