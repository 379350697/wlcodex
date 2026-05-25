from __future__ import annotations

from datetime import UTC, datetime

from wlcodex.team_context import TeamContextInput, build_team_context_packet
from wlcodex.team_memory import InstinctMemory
from wlcodex.team_roles import RoleId, TeamRouteKind, TeamRoleCatalog


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
            user_goal="审计实现是否符合计划",
            workspace_alias="wlcodex",
            skills=("code-review", "gitnexus-pr-review"),
            allowed_capabilities=("read", "git_diff", "shell_readonly"),
            artifact_summaries=[
                "architecture_plan: 按最小边界实现。",
                "implementation_report: 新增角色上下文包。",
            ],
            evidence_refs=("team_artifact=12", "changed_file=wlcodex/team_context.py"),
            resume_state="resume from audit checklist item 2",
            open_questions=("是否需要补充端到端测试？",),
            output_schema="audit_report",
            token_budget=900,
        )
    )

    rendered = packet.render()

    assert "role: auditor" in rendered
    assert "model_profile: codex_gpt" in rendered
    assert "full transcript" not in rendered.lower()
    assert "team_artifact=12" in rendered
    assert "team_run_id: 7" in rendered
    assert "agent_job_id: 9" in rendered
    assert "conversation_id: 3" in rendered
    assert "orchestration_run_id: 5" in rendered
    assert "skills: code-review, gitnexus-pr-review" in rendered
    assert "resume_state: resume from audit checklist item 2" in rendered
    assert packet.as_json()["artifact_summaries"] == [
        "architecture_plan: 按最小边界实现。",
        "implementation_report: 新增角色上下文包。",
    ]
    assert packet.as_json()["workspace_alias"] == "wlcodex"
    assert packet.as_json()["allowed_tools"] == [
        "read",
        "git_diff",
        "shell_readonly",
    ]
    assert packet.as_json()["inputs"] == [
        {
            "kind": "artifact_summary",
            "summary": "architecture_plan: 按最小边界实现。",
        },
        {
            "kind": "artifact_summary",
            "summary": "implementation_report: 新增角色上下文包。",
        },
    ]
    assert packet.as_json()["output_schema"] == "audit_report"
    assert packet.as_json()["required_output_schema"] == "audit_report"
    assert "handoff_rules" in packet.as_json()
    assert packet.as_json()["handoff_rules"]
    assert packet.as_json()["token_budget"] == 900
    assert packet.within_budget() is True


def test_team_context_packet_includes_architect_expert_profile() -> None:
    role = TeamRoleCatalog.default().role(RoleId.ARCHITECT)
    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=8,
            conversation_id=9,
            orchestration_run_id=10,
            role=role,
            model_profile="codex_gpt",
            user_goal="新增专家判断模式",
            workspace_alias="wlcodex",
            route_kind=TeamRouteKind.FEATURE.value,
            output_schema="architecture_plan",
        )
    )

    payload = packet.as_json()
    rendered = packet.render()

    assert payload["route_kind"] == "feature"
    assert payload["expert_profile"]["stance"]
    assert "tradeoff" in rendered.lower()
    assert "architecture_plan" in rendered


def test_team_context_packet_includes_diagnostician_expert_profile() -> None:
    role = TeamRoleCatalog.default().role(RoleId.INVESTIGATOR)
    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=8,
            conversation_id=9,
            orchestration_run_id=10,
            role=role,
            model_profile="codex_gpt",
            user_goal="Telegram 验收失败",
            workspace_alias="wlcodex",
            route_kind=TeamRouteKind.BUG.value,
            output_schema="diagnosis_report",
        )
    )

    rendered = packet.render()

    assert "route_kind: bug" in rendered
    assert "root cause" in rendered.lower()
    assert "diagnosis_report" in rendered


def test_context_packet_includes_memory_as_historical_advice() -> None:
    role = TeamRoleCatalog.default().role(RoleId.AUDITOR)
    instinct = InstinctMemory(
        instinct_id="audit-memory-1",
        scope="project",
        workspace_alias="wlcodex",
        role="auditor",
        domain="audit",
        trigger="review implementation evidence",
        action="Require current diff and test evidence before approving.",
        confidence=0.8,
        evidence_refs=("team_observation=1",),
        status="active",
        created_at=datetime(2026, 5, 24, tzinfo=UTC),
        last_validated_at=datetime(2026, 5, 24, tzinfo=UTC),
    )

    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=9,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="codex_gpt",
            user_goal="audit implementation evidence",
            workspace_alias="wlcodex",
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
            artifact_summaries=["implementation_report: changed auth.py"],
        )
    )

    canonical = packet.as_json()
    rendered = packet.render()

    assert (
        canonical["relevant_instincts"][0]["precedence"]
        == "historical_advice_current_evidence_wins"
    )
    assert (
        canonical["historical_context_policy"]
        == "current_user_goal_and_current_evidence_override_history"
    )
    assert canonical["capability_budget"]["max_memory_snippets"] == 2
    assert canonical["skill_activations"] == ["gitnexus-pr-review", "git_diff"]
    assert "historical_context_policy" in rendered
    assert "current evidence override history" in rendered


def test_context_packet_compacts_v2_fields_in_canonical_json_for_tiny_budget() -> None:
    role = TeamRoleCatalog.default().role(RoleId.AUDITOR)
    relevant_instincts = (
        {
            "id": "memory-1",
            "scope": "project",
            "role": "auditor",
            "trigger": "implementation evidence",
            "action": "Require current diff and test evidence before approving. " * 80,
            "confidence": 0.9,
            "evidence_refs": ["team_observation=1"],
            "precedence": "historical_advice_current_evidence_wins",
        },
        {
            "id": "memory-2",
            "scope": "project",
            "role": "auditor",
            "trigger": "audit report",
            "action": "Ask for missing regression evidence.",
            "confidence": 0.8,
            "evidence_refs": ["team_observation=2"],
            "precedence": "historical_advice_current_evidence_wins",
        },
    )

    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=9,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="codex_gpt",
            user_goal="audit implementation evidence " * 80,
            workspace_alias="wlcodex",
            relevant_instincts=relevant_instincts,
            capability_budget={
                "max_skills": 2,
                "max_tools": 4,
                "max_memory_snippets": 2,
                "max_prompt_tokens": 80,
            },
            skill_activations=tuple(f"skill-{index}" for index in range(8)),
            source_refs=tuple(f"source={index}" for index in range(8)),
            token_budget=80,
            artifact_summaries=["implementation_report: " + "changed auth.py " * 200],
        )
    )

    canonical = packet.as_json()

    assert len(canonical["relevant_instincts"]) <= 1
    if canonical["relevant_instincts"]:
        assert set(canonical["relevant_instincts"][0]) == {
            "id",
            "action",
            "precedence",
        }
    assert len(canonical["skill_activations"]) <= 2
    assert canonical["skill_activations"] == ["skill-0", "skill-1"]
    assert len(canonical["source_refs"]) <= 2
    assert canonical["source_refs"] == ["source=0", "source=1"]
    assert "output_schema" in canonical
    assert "handoff_rules" in canonical
    assert canonical["capability_budget"]["max_prompt_tokens"] == 80


def test_context_packet_trims_large_artifact_summaries() -> None:
    role = TeamRoleCatalog.default().role(RoleId.IMPLEMENTER)

    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=9,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="codex_gpt",
            user_goal="实现角色上下文包",
            workspace_alias="wlcodex",
            artifact_summaries=["architecture_plan: " + "x" * 8000],
            token_budget=500,
        )
    )

    rendered = packet.render()

    assert packet.within_budget() is True
    assert len(rendered) <= 2200


def test_context_packet_degrades_deterministically_for_tiny_budget() -> None:
    role = TeamRoleCatalog.default().role(RoleId.AUDITOR)

    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=9,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="codex_gpt",
            user_goal="审计实现是否符合计划" * 50,
            workspace_alias="wlcodex",
            artifact_summaries=[
                "architecture_plan: " + "x" * 8000,
                "implementation_report: " + "y" * 8000,
            ],
            evidence_refs=("team_artifact=12",),
            resume_state="resume from a very long prior audit state " * 200,
            open_questions=("是否需要补充端到端测试？" * 200,),
            output_schema="audit_report",
            token_budget=100,
        )
    )

    rendered = packet.render()
    payload = packet.as_json()

    assert packet.within_budget() is True
    assert "team_run_id: 7" in rendered
    assert "agent_job_id: 9" in rendered
    assert "conversation_id: 3" in rendered
    assert "orchestration_run_id: 5" in rendered
    assert packet.data.artifact_summaries == []
    assert isinstance(packet.data.artifact_summaries, list)
    assert payload["evidence_refs"] == ["team_artifact=12"]
    assert payload["open_questions"] == []
    assert payload["resume_state"] != "resume from a very long prior audit state " * 200
    assert payload["user_goal"] != "审计实现是否符合计划" * 50


def test_context_packet_uses_ultra_compact_render_for_very_small_budget() -> None:
    role = TeamRoleCatalog.default().role(RoleId.AUDITOR)

    packet = build_team_context_packet(
        TeamContextInput(
            team_run_id=7,
            agent_job_id=9,
            conversation_id=3,
            orchestration_run_id=5,
            role=role,
            model_profile="codex_gpt",
            user_goal="审计实现是否符合计划" * 50,
            workspace_alias="wlcodex",
            artifact_summaries=[
                "architecture_plan: " + "x" * 8000,
                "implementation_report: " + "y" * 8000,
            ],
            resume_state="resume from a very long prior audit state " * 200,
            open_questions=("是否需要补充端到端测试？" * 200,),
            output_schema="audit_report",
            token_budget=50,
        )
    )

    rendered = packet.render()

    assert packet.within_budget() is True
    assert "team_run_id: 7" in rendered
    assert "agent_job_id: 9" in rendered
    assert "role: auditor" in rendered
    assert "model_profile: codex_gpt" in rendered
    assert "token_budget: 50" in rendered
