from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from wlcodex.context_packets import approx_tokens, trim_to_budget
from wlcodex.team_roles import TeamRole


@dataclass(frozen=True)
class TeamContextInput:
    team_run_id: int
    agent_job_id: int
    conversation_id: int
    orchestration_run_id: int
    role: TeamRole
    model_profile: str
    user_goal: str
    workspace_alias: str
    skills: tuple[str, ...] = ()
    allowed_capabilities: tuple[str, ...] = ()
    artifact_summaries: list[str] = field(default_factory=list)
    relevant_instincts: tuple[dict[str, object], ...] = ()
    capability_budget: dict[str, int] = field(default_factory=dict)
    skill_activations: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    resume_state: str = ""
    open_questions: tuple[str, ...] = ()
    output_schema: str = ""
    handoff_rules: tuple[str, ...] = ()
    token_budget: int = 0


@dataclass(frozen=True)
class TeamContextPacket:
    data: TeamContextInput
    compact: bool = False
    ultra_compact: bool = False

    def as_json(self) -> dict[str, Any]:
        role = self.data.role
        skills = list(self.data.skills or role.skills)
        allowed_tools = list(self.data.allowed_capabilities or role.allowed_capabilities)
        inputs = [
            {"kind": "artifact_summary", "summary": summary}
            for summary in self.data.artifact_summaries
        ]
        output_schema = self.data.output_schema or role.required_artifact_type
        handoff_rules = list(self.data.handoff_rules) or [
            f"Produce {output_schema} as structured JSON.",
            "Reference evidence by id/path instead of copying long logs, raw diffs, or transcripts.",
            "Downstream roles must treat prior artifacts as advisory until confirmed by current evidence.",
        ]
        if role.role_id.value == "auditor" and not any(
            "Audit only starts after implementation and test evidence" in rule
            for rule in handoff_rules
        ):
            handoff_rules.append(
                "Audit only starts after implementation and test evidence exist; "
                "do not pass without current-round test evidence."
            )
        if role.role_id.value == "architect" and not any(
            "diagnosis evidence" in rule
            for rule in handoff_rules
        ):
            handoff_rules.append(
                "If diagnosis and architecture are combined, gather diagnosis evidence "
                "from runtime/code checks and capture it inside architecture_plan."
            )
        return {
            "team_run_id": self.data.team_run_id,
            "agent_job_id": self.data.agent_job_id,
            "conversation_id": self.data.conversation_id,
            "orchestration_run_id": self.data.orchestration_run_id,
            "role": role.role_id.value,
            "role_display_name": role.display_name,
            "model_profile": self.data.model_profile,
            "workspace": self.data.workspace_alias,
            "workspace_alias": self.data.workspace_alias,
            "user_goal": self.data.user_goal,
            "role_mission": role.mission,
            "skills": skills,
            "allowed_capabilities": allowed_tools,
            "allowed_tools": allowed_tools,
            "forbidden_actions": list(role.forbidden_actions),
            "inputs": inputs,
            "artifact_summaries": list(self.data.artifact_summaries),
            "relevant_instincts": [
                dict(instinct) for instinct in self.data.relevant_instincts
            ],
            "historical_context_policy": (
                "current_user_goal_and_current_evidence_override_history"
            ),
            "capability_budget": dict(self.data.capability_budget),
            "skill_activations": list(self.data.skill_activations),
            "source_refs": list(self.data.source_refs),
            "evidence_refs": list(self.data.evidence_refs),
            "resume_state": self.data.resume_state,
            "open_questions": list(self.data.open_questions),
            "output_schema": output_schema,
            "required_output_schema": output_schema,
            "handoff_rules": handoff_rules,
            "token_budget": self.data.token_budget,
        }

    def render(self) -> str:
        payload = self.as_json()
        if self.ultra_compact:
            return self._render_ultra_compact(payload)
        if self.compact:
            return self._render_compact(payload)
        lines = [
            f"team_run_id: {payload['team_run_id']}",
            f"agent_job_id: {payload['agent_job_id']}",
            f"conversation_id: {payload['conversation_id']}",
            f"orchestration_run_id: {payload['orchestration_run_id']}",
            f"role: {payload['role']}",
            f"role_display_name: {payload['role_display_name']}",
            f"model_profile: {payload['model_profile']}",
            f"workspace: {payload['workspace']}",
            f"user_goal: {payload['user_goal']}",
            f"role_mission: {payload['role_mission']}",
            "context_policy: Use this packet and evidence references. Do not ask for or rely "
            "on full chat history.",
            "historical_context_policy: current evidence override history; memories and "
            "prior artifacts are advisory only.",
            f"capability_budget: {payload['capability_budget']}",
            f"skill_activations: {', '.join(payload['skill_activations'])}",
            f"relevant_instincts: {payload['relevant_instincts']}",
            f"source_refs: {', '.join(payload['source_refs'])}",
            f"skills: {', '.join(payload['skills'])}",
            f"allowed_capabilities: {', '.join(payload['allowed_capabilities'])}",
            f"allowed_tools: {', '.join(payload['allowed_tools'])}",
            f"forbidden_actions: {', '.join(payload['forbidden_actions'])}",
        ]
        if payload["artifact_summaries"]:
            lines.append("artifact_summaries:")
            for summary in payload["artifact_summaries"]:
                lines.append(f"  - {summary}")
        if payload["evidence_refs"]:
            lines.append(f"evidence_refs: {', '.join(payload['evidence_refs'])}")
        if payload["resume_state"]:
            lines.append(f"resume_state: {payload['resume_state']}")
        if payload["open_questions"]:
            lines.append(f"open_questions: {'; '.join(payload['open_questions'])}")
        if payload["required_output_schema"]:
            lines.append(f"required_output_schema: {payload['required_output_schema']}")
        if payload["handoff_rules"]:
            lines.append("handoff_rules:")
            for rule in payload["handoff_rules"]:
                lines.append(f"  - {rule}")
        if self.data.token_budget:
            lines.append(f"token_budget: {self.data.token_budget}")
        return "\n".join(lines)

    def _render_compact(self, payload: dict[str, Any]) -> str:
        lines = [
            f"team_run_id: {payload['team_run_id']}",
            f"agent_job_id: {payload['agent_job_id']}",
            f"conversation_id: {payload['conversation_id']}",
            f"orchestration_run_id: {payload['orchestration_run_id']}",
            f"role: {payload['role']}",
            f"model_profile: {payload['model_profile']}",
            f"workspace: {payload['workspace']}",
            f"user_goal: {payload['user_goal']}",
            "context_policy: Use this packet and evidence references. Do not ask for or rely "
            "on full chat history.",
        ]
        if payload["evidence_refs"]:
            lines.append(f"evidence_refs: {', '.join(payload['evidence_refs'])}")
        if payload["resume_state"]:
            lines.append(f"resume_state: {payload['resume_state']}")
        if payload["required_output_schema"]:
            lines.append(f"required_output_schema: {payload['required_output_schema']}")
        if payload["token_budget"]:
            lines.append(f"token_budget: {payload['token_budget']}")
        return "\n".join(lines)

    def _render_ultra_compact(self, payload: dict[str, Any]) -> str:
        return "\n".join(
            [
                f"team_run_id: {payload['team_run_id']}",
                f"agent_job_id: {payload['agent_job_id']}",
                f"role: {payload['role']}",
                f"model_profile: {payload['model_profile']}",
                f"token_budget: {payload['token_budget']}",
            ]
        )

    def within_budget(self) -> bool:
        if self.data.token_budget <= 0:
            return True
        return approx_tokens(self.render()) <= self.data.token_budget


def build_team_context_packet(data: TeamContextInput) -> TeamContextPacket:
    fixed_overhead = 700
    summary_budget = max(100, data.token_budget - fixed_overhead)
    packet = TeamContextPacket(_with_trimmed_artifacts(data, summary_budget))
    if data.token_budget > 0 and not packet.within_budget():
        packet = TeamContextPacket(
            _with_trimmed_artifacts(data, max(40, data.token_budget // 3))
        )
    if data.token_budget > 0 and not packet.within_budget():
        packet = TeamContextPacket(_with_minimal_budget_fields(data), compact=True)
    if data.token_budget > 0 and not packet.within_budget():
        packet = TeamContextPacket(_with_ultra_budget_fields(data), ultra_compact=True)
    return packet


def _with_trimmed_artifacts(data: TeamContextInput, max_tokens: int) -> TeamContextInput:
    if not data.artifact_summaries:
        return data
    per_summary_budget = max(1, max_tokens // len(data.artifact_summaries))
    return replace(
        data,
        artifact_summaries=[
            trim_to_budget(summary, per_summary_budget)
            for summary in data.artifact_summaries
        ],
    )


def _with_minimal_budget_fields(data: TeamContextInput) -> TeamContextInput:
    text_budget = max(1, data.token_budget // 20)
    return replace(
        data,
        user_goal=trim_to_budget(data.user_goal, text_budget),
        artifact_summaries=[],
        relevant_instincts=_compact_relevant_instincts(
            data.relevant_instincts,
            limit=1,
            action_tokens=text_budget,
        ),
        skill_activations=data.skill_activations[:2],
        source_refs=data.source_refs[:2],
        resume_state=trim_to_budget(data.resume_state, max(1, text_budget // 2)),
        open_questions=(),
    )


def _with_ultra_budget_fields(data: TeamContextInput) -> TeamContextInput:
    return replace(
        data,
        user_goal="",
        artifact_summaries=[],
        relevant_instincts=(),
        skill_activations=(),
        source_refs=(),
        resume_state="",
        open_questions=(),
    )


def _compact_relevant_instincts(
    instincts: tuple[dict[str, object], ...],
    *,
    limit: int,
    action_tokens: int,
) -> tuple[dict[str, object], ...]:
    compacted: list[dict[str, object]] = []
    for instinct in instincts[:limit]:
        compacted.append(
            {
                "id": instinct.get("id", ""),
                "action": trim_to_budget(str(instinct.get("action", "")), action_tokens),
                "precedence": instinct.get(
                    "precedence",
                    "historical_advice_current_evidence_wins",
                ),
            }
        )
    return tuple(compacted)
