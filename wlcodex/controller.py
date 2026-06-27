"""Command controller — routes parsed commands to services, returns responses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import subprocess
import uuid
from typing import Any

from wlcodex.health_snapshot import build_health_snapshot
from wlcodex.execution_scheduler import ExecutionScheduler, RunIntent
from wlcodex.interaction.errors import classify_user_error
from wlcodex.inspection import TaskInspector
from wlcodex.legacy_diagnostics import LegacyDiagnosticsController
from wlcodex.models import TaskStatus
from wlcodex.conversation_state_machine import (
    RouteDecision,
    classify_intent,
    route_message,
    MID_RUN_ACKNOWLEDGEMENT,
    build_workspace_busy_buttons,
    BUSY_APPEND,
    BUSY_INTERRUPT,
    BUSY_QUEUE,
    BUSY_CANCEL,
    BUSY_NEW_SESSION,
)
from wlcodex.conversation import default_title
from wlcodex.conversation_callback import (
    CARRY_CANCEL,
    CARRY_REFRESH,
    CARRY_SHOW,
    CARRY_START,
    CONTINUE,
    DIFF,
    NEW_CONVO,
    RESTORE_WORKBENCH,
    RETRY,
    STATUS,
    VERIFY,
    ConversationCallback,
    encode_conversation_callback,
)
from wlcodex.context_packets import (
    ContextBudget,
    approx_tokens,
    build_codex_analysis_packet,
    build_codex_verification_packet as make_verification_packet,
    build_auto_context_packet,
    build_auto_final_plan_packet,
    build_auto_verification_packet,
    build_auto_repair_packet,
    trim_to_budget,
)
from wlcodex.auto_workflow import (
    AUTO_COLLECTING_CONTEXT,
    AUTO_DRAFT_READY,
    AUTO_CLAUDE_RUNNING,
    AUTO_CLAUDE_DONE,
    AUTO_VERIFYING,
    AUTO_RETRY_READY,
    AUTO_ROUTE_SELECT,
    AUTO_CODEX_TAKEOVER_RUNNING,
    AUTO_COMPLETED,
    AUTO_FINAL_PLAN,
    AUTO_ROUTE_DIAGNOSE,
    AUTO_ROUTE_DESIGN,
    AUTO_ROUTE_CODEX_EXECUTE,
    AUTO_ROUTE_CLAUDE_EXECUTE,
    AUTO_SHOW_DRAFT,
    AUTO_CANCEL,
    AUTO_SEND_TO_CLAUDE,
    AUTO_SEND_TO_CODEX,
    AUTO_CONTINUE_CONTEXT,
    AUTO_REWRITE_PLAN,
    AUTO_CODEX_TAKEOVER,
    AUTO_CLOSE,
    AUTO_CODEX_VERIFY,
    AUTO_SEND_REPAIR_TO_CLAUDE,
    AUTO_REWRITE_REPAIR,
    AUTO_INTERRUPT_CLAUDE,
    AUTO_VIEW_DIFF,
    AUTO_VIEW_STATUS,
    TEAM_VIEW_ARTIFACTS,
    TEAM_VIEW_STATUS,
    ROLE_AUTO_ANALYSIS,
    ROLE_AUTO_CONTEXT_SUPPLEMENT,
    ROLE_AUTO_FINAL_PLAN,
    ROLE_AUTO_VERIFICATION,
    ROLE_AUTO_IMPLEMENTATION,
    ROLE_AUTO_REPAIR,
    ROLE_AUTO_CODEX_TAKEOVER,
    auto_stage_label,
    build_auto_stage_buttons,
    is_auto_collecting_context,
)
from wlcodex.codex_thread_policy import (
    can_reuse_codex_thread,
    codex_thread_policy_fingerprint,
)
from wlcodex.claude_permissions import (
    DEFAULT_CLAUDE_PERMISSION_MODE,
    RUNTIME_CLAUDE_PERMISSION_MODE_KEY,
    ClaudePermissionState,
    build_claude_permission_buttons,
    render_claude_permission_status,
)
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
    safe_text_preview,
)
from wlcodex.carryover import (
    CarryoverSource,
    build_continuity_brief,
    build_carryover_preview,
    build_source_fingerprint,
)
from wlcodex.router import (
    AutoModeCommand,
    CarryWorkbenchCommand,
    ClaudeDirectCommand,
    ClaudePermissionCommand,
    CodexDirectCommand,
    CodexSessionsCommand,
    DiffCommand,
    ExecModeCommand,
    FilesCommand,
    HealthCommand,
    HelpCommand,
    ModelCommand,
    NewConversationCommand,
    ParseError,
    StatusCommand,
    StopCurrentCommand,
    SwitchWorkspaceCommand,
    TraceCommand,
    VerifyCommand,
    WorkbenchHistoryCommand,
    WorkspaceListCommand,
    WorkspaceStatusCommand,
    parse_command,
)
from wlcodex.status import (
    MODE_LABELS,
    render_carryover_brief_view,
    render_carryover_cancelled,
    render_carryover_candidates,
    render_carryover_target_created,
    render_conversation_help,
    render_conversation_status,
    render_prepared_carryover,
    render_team_artifact_summary,
    render_team_status_summary,
    render_workspace_list,
    render_current_workspace,
)
from wlcodex.models import ConversationMode
from wlcodex.status import render_health_card
from wlcodex.task_service import TaskService
from wlcodex.auto_digest_llm import (
    DeepSeekDigestUsage,
    render_auto_draft_digest_with_llm,
)
from wlcodex.team_model_settings import (
    encode_assignment,
    is_multi_select_role,
    normalize_assignment,
    ordered_model_profiles,
    ordered_team_roles,
    role_display_name,
    runtime_assignment_key,
)

logger = logging.getLogger(__name__)

HELP_TEXT = render_conversation_help()
MAX_INTERNAL_TEST_ATTEMPTS = 3


def _changed_files_from_inspection_body(body: str) -> list[str]:
    changed_files: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("相关文件：", "暂无文件记录。"):
            continue
        if stripped.startswith("[") and "]" in stripped:
            stripped = stripped.split("]", 1)[1].strip()
        if stripped:
            changed_files.append(stripped[:200])
    return changed_files


def _diff_body_has_evidence(body: str) -> bool:
    stripped = body.strip()
    return bool(
        stripped
        and stripped not in ("暂无 diff 信息。", "工作区没有未提交变更。")
    )


def _changed_files_from_diff_summary(body: str) -> list[str]:
    changed_files: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("diff --git "):
            parts = stripped.split()
            if len(parts) >= 4:
                path = parts[2]
                if path.startswith("a/"):
                    path = path[2:]
                changed_files.append(path[:200])
            continue
        if "|" in stripped:
            path = stripped.split("|", 1)[0].strip()
            if path and not path[0].isdigit():
                changed_files.append(path[:200])
    return list(dict.fromkeys(changed_files))


def _filter_diff_summary_to_files(body: str, files: list[str]) -> str:
    if not body or not files:
        return body
    file_set = set(files)
    lines = body.splitlines()
    if any(line.startswith("diff --git ") for line in lines):
        filtered_sections: list[str] = []
        current: list[str] = []
        current_in_scope = False
        for line in lines:
            if line.startswith("diff --git "):
                if current and current_in_scope:
                    filtered_sections.extend(current)
                current = [line]
                current_in_scope = _diff_git_line_path(line) in file_set
                continue
            if current:
                current.append(line)
            elif _summary_stat_line_path(line) in file_set:
                filtered_sections.append(line)
        if current and current_in_scope:
            filtered_sections.extend(current)
        return "\n".join(filtered_sections).strip()

    filtered_lines = [
        line
        for line in lines
        if _summary_stat_line_path(line) in file_set
    ]
    return "\n".join(filtered_lines).strip()


def _diff_git_line_path(line: str) -> str:
    parts = line.split()
    if len(parts) < 3:
        return ""
    path = parts[2]
    return path[2:] if path.startswith("a/") else path


def _summary_stat_line_path(line: str) -> str:
    stripped = line.strip()
    if "|" not in stripped:
        return ""
    path = stripped.split("|", 1)[0].strip()
    if not path or path[0].isdigit():
        return ""
    return path[:200]


def _human_artifact_type(artifact_type: str) -> str:
    labels = {
        "architecture_plan": "方案记录",
        "diagnosis_report": "诊断工程师交接报告",
        "implementation_report": "实现记录",
        "test_report": "测试记录",
        "audit_report": "验收记录",
        "verification_request": "验收请求",
    }
    return labels.get(artifact_type, "流程记录")


def _human_gate_field_name(field: str) -> str:
    labels = {
        "summary": "简要说明",
        "changed_files": "改动文件",
        "diff_summary": "改动说明",
        "commands_run": "执行记录",
        "tests_attempted": "测试记录",
        "known_limitations": "已知限制",
        "passed": "是否通过",
        "coverage_of_acceptance_criteria": "验收标准覆盖情况",
        "decision": "验收结论",
        "architecture_plan": "方案记录",
        "diagnosis_report": "诊断工程师交接报告",
        "implementation_report": "实现记录",
        "test_report": "测试记录",
        "audit_report": "验收记录",
        "test_evidence_refs": "测试证据引用",
        "implementation_report_current_agent_job": "本轮开发任务的实现记录",
        "test_report_current_agent_job": "本轮开发任务的测试记录",
    }
    return labels.get(field, field.replace("_", " "))


def _is_lightweight_greeting(text: str) -> bool:
    normalized = text.strip().lower()
    normalized = normalized.strip(" \t\r\n.!?。！？~～")
    return normalized in {
        "hi",
        "hello",
        "hey",
        "你好",
        "您好",
        "哈喽",
        "嗨",
    }


@dataclass
class ControllerResponse:
    text: str
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    already_rendered: bool = False


class CommandController:
    def __init__(
        self,
        task_service: TaskService,
        backend: object,
        inspector: TaskInspector,
        ledger: object | None = None,
        claude_backend: object | None = None,
        claude_permission_state: ClaudePermissionState | None = None,
        default_mode: str = "chief_engineer",
        default_workspace: str = "wlcodex",
        interaction_renderer: object | None = None,
        orchestration_runner: object | None = None,
        runtime_event_store: object | None = None,
        execution_scheduler: object | None = None,
        adaptive_team_enabled: bool = True,
        implementer_model_profiles: tuple[str, ...] = (
            "claude_deepseek",
            "codex_gpt",
        ),
        adaptive_team_model_profiles: dict[str, str] | None = None,
        adaptive_team_role_skills: dict[str, tuple[str, ...]] | None = None,
        adaptive_team_role_capabilities: dict[str, tuple[str, ...]] | None = None,
        director_model_profile: str = "codex_gpt",
        architect_model_profile: str = "codex_gpt",
        investigator_model_profile: str = "codex_gpt",
        tester_model_profile: str = "codex_gpt",
        auditor_model_profile: str = "codex_gpt",
    ) -> None:
        self._service = task_service
        self._backend = backend
        self._inspector = inspector
        self._ledger = ledger
        self._claude = claude_backend
        self._claude_permission_state = claude_permission_state
        self._default_mode = default_mode
        self._default_workspace = default_workspace
        self._adaptive_team_enabled = adaptive_team_enabled
        self._implementer_model_profiles = tuple(implementer_model_profiles)
        self._adaptive_team_model_profiles = {
            "claude_deepseek": "claude",
            "codex_gpt": "codex",
            **{
                str(key): str(value)
                for key, value in (adaptive_team_model_profiles or {}).items()
            },
        }
        self._adaptive_team_role_skills = {
            str(role): tuple(str(skill) for skill in skills)
            for role, skills in (adaptive_team_role_skills or {}).items()
        }
        self._adaptive_team_role_capabilities = {
            str(role): tuple(str(capability) for capability in capabilities)
            for role, capabilities in (adaptive_team_role_capabilities or {}).items()
        }
        if self._adaptive_team_enabled:
            from wlcodex.team_capabilities import audit_role_capability_config
            from wlcodex.team_roles import TeamRoleCatalog

            catalog = TeamRoleCatalog.default()
            role_capability_config = {
                role.role_id.value: role.allowed_capabilities
                for role in catalog.roles.values()
            }
            role_capability_config.update(self._adaptive_team_role_capabilities)
            capability_findings = audit_role_capability_config(role_capability_config)
            if capability_findings:
                raise ValueError(
                    "invalid adaptive team role capabilities: "
                    + "; ".join(capability_findings)
                )
        self._director_model_profile = director_model_profile
        self._architect_model_profile = architect_model_profile
        self._investigator_model_profile = investigator_model_profile
        self._tester_model_profile = tester_model_profile
        self._auditor_model_profile = auditor_model_profile
        self._interaction_renderer = interaction_renderer
        self._orchestration_runner = orchestration_runner
        self._store = runtime_event_store
        self._execution_scheduler = (
            execution_scheduler
            if execution_scheduler is not None
            else ExecutionScheduler(task_service, ledger) if ledger is not None else None
        )
        self._legacy_diagnostics = LegacyDiagnosticsController(
            task_service, backend, inspector
        )
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _codex_implementer_enabled(self) -> bool:
        return (
            self._adaptive_team_enabled
            and any(
                self._adaptive_team_model_profiles.get(profile, profile).lower()
                == "codex"
                for profile in self._implementer_model_profiles
            )
        )

    def _codex_implementer_model_profile(self) -> str | None:
        for profile in self._implementer_model_profiles:
            if (
                self._adaptive_team_model_profiles.get(profile, profile).lower()
                == "codex"
            ):
                return profile
        return None

    def _claude_implementer_model_profile(self) -> str | None:
        for profile in self._implementer_model_profiles:
            if (
                self._adaptive_team_model_profiles.get(profile, profile).lower()
                == "claude"
            ):
                return profile
        return None

    def _mark_auto_team_failed(
        self,
        *,
        team_run: object | None,
        architect_job: object | None,
    ) -> None:
        if team_run is not None and hasattr(self._ledger, "update_team_run_status"):
            self._ledger.update_team_run_status(team_run.id, "failed")
        if architect_job is not None and hasattr(
            self._ledger,
            "update_team_agent_job_status",
        ):
            self._ledger.update_team_agent_job_status(architect_job.id, "failed")

    def set_interaction_renderer(self, renderer: object) -> None:
        """Set the interaction renderer after construction (created after handlers)."""
        self._interaction_renderer = renderer

    def set_orchestration_runner(self, runner: object | None) -> None:
        """Set the background orchestration runner after runtime wiring."""
        self._orchestration_runner = runner

    def set_legacy_diagnostics(self, adapter: object) -> None:
        """Replace the legacy diagnostics adapter in tests or composition."""
        self._legacy_diagnostics = adapter

    def set_execution_scheduler(self, scheduler: object) -> None:
        """Replace the internal execution scheduler in tests or composition."""
        self._execution_scheduler = scheduler

    @property
    def execution_scheduler(self) -> object | None:
        """Return the internal Workbench execution scheduler."""
        return self._execution_scheduler

    def _build_team_context_packet_for_job(
        self,
        *,
        team_run: object,
        agent_job: object,
        role: str,
        model_profile: str,
        resume_state: str,
        output_schema: str,
    ) -> object:
        from wlcodex.team_capabilities import (
            CapabilityBudget,
            SkillCatalog,
            SkillDefinition,
            audit_role_capability_config,
            select_capabilities,
        )
        from wlcodex.team_context import TeamContextInput, build_team_context_packet
        from wlcodex.team_memory import InstinctMemory, select_relevant_instincts
        from wlcodex.team_roles import RoleId, TeamRoleCatalog

        if self._ledger is None:
            raise RuntimeError("team context packets require a ledger")

        try:
            role_id = RoleId(role)
        except ValueError:
            raise ValueError(f"unknown team role '{role}'") from None
        if agent_job.role != role_id.value:
            raise ValueError(
                f"agent job role '{agent_job.role}' does not match requested "
                f"role '{role_id.value}'"
            )

        role_def = TeamRoleCatalog.default().role(role_id)
        conversation = self._ledger.get_conversation(team_run.conversation_id)
        budget = CapabilityBudget(
            max_skills=2,
            max_tools=4,
            max_memory_snippets=2,
            max_prompt_tokens=1200,
        )
        capability_budget = {
            "max_skills": budget.max_skills,
            "max_tools": budget.max_tools,
            "max_memory_snippets": budget.max_memory_snippets,
            "max_prompt_tokens": budget.max_prompt_tokens,
        }

        artifacts = []
        if hasattr(self._ledger, "list_team_artifacts"):
            artifacts = self._ledger.list_team_artifacts(team_run.id)
        artifact_summaries = [
            f"{artifact.artifact_type}: {artifact.summary[:300]}"
            for artifact in artifacts
        ]

        active_instincts = self._ledger.list_team_instincts(status="active")
        task_text = f"{team_run.goal}\n{resume_state}".strip()
        selected_instincts = select_relevant_instincts(
            tuple(active_instincts),
            workspace_alias=conversation.workspace_alias,
            role=role,
            task_text=task_text,
            limit=budget.max_memory_snippets,
            min_confidence=0.6,
        )
        instinct_memories = tuple(
            InstinctMemory(
                instinct_id=instinct.instinct_id,
                scope=instinct.scope,
                workspace_alias=instinct.workspace_alias,
                role=instinct.role,
                domain=instinct.domain,
                trigger=instinct.trigger,
                action=instinct.action,
                confidence=instinct.confidence,
                evidence_refs=instinct.evidence_refs,
                status=instinct.status,
                created_at=instinct.created_at,
                last_validated_at=instinct.last_validated_at,
            )
            for instinct in selected_instincts
        )
        relevant_instincts = tuple(
            memory.as_packet_item() for memory in instinct_memories
        )

        configured_skill_ids = self._adaptive_team_role_skills.get(role_id.value)
        configured_tool_ids = self._adaptive_team_role_capabilities.get(role_id.value)
        role_skill_ids = configured_skill_ids or tuple(role_def.skills)
        role_tool_ids = configured_tool_ids or tuple(role_def.allowed_capabilities)
        if configured_tool_ids is not None:
            capability_findings = audit_role_capability_config({
                role_id.value: configured_tool_ids,
            })
            if capability_findings:
                raise ValueError(
                    "invalid adaptive team role capabilities: "
                    + "; ".join(capability_findings)
                )
        capability_selection = select_capabilities(
            catalog=SkillCatalog(
                [
                    SkillDefinition(
                        skill_id=skill_id,
                        roles=(role_id.value,),
                        triggers=(
                            role_id.value,
                            role_def.display_name,
                            role_def.mission,
                            role_def.instructions,
                        ),
                        required_tools=(),
                        token_cost=0,
                    )
                    for skill_id in role_skill_ids
                ]
            ),
            role=role_id.value,
            task=task_text,
            available_tools=role_tool_ids,
            budget=budget,
        )
        selected_skill_ids = tuple(
            skill.skill_id for skill in capability_selection.skills
        )
        if not selected_skill_ids:
            selected_skill_ids = tuple(role_skill_ids[: budget.max_skills])
        selected_tool_pool = set(capability_selection.tools)
        selected_tool_ids = tuple(
            tool for tool in role_tool_ids if tool in selected_tool_pool
        )[: budget.max_tools]
        if not selected_tool_ids:
            selected_tool_ids = tuple(role_tool_ids[: budget.max_tools])
        selected_activation_ids = selected_skill_ids + selected_tool_ids
        source_refs = tuple(
            [f"team_artifact={artifact.id}" for artifact in artifacts]
            + [f"team_instinct={instinct.id}" for instinct in selected_instincts]
        )
        evidence_refs: list[str] = [
            f"orchestration_run={team_run.orchestration_run_id or 0}",
            f"conversation={team_run.conversation_id}",
            f"team_run={team_run.id}",
            f"agent_job={agent_job.id}",
        ]
        evidence_refs.extend(source_refs)
        for artifact in artifacts:
            payload = artifact.payload if isinstance(artifact.payload, dict) else {}
            for changed_file in payload.get("changed_files", [])[:10]:
                evidence_refs.append(f"changed_file={str(changed_file)[:200]}")
            for command in payload.get("commands_run", [])[:5]:
                evidence_refs.append(f"command={str(command)[:200]}")
        existing_activation_keys = {
            (
                activation.activation_type,
                activation.activation_id,
                activation.source,
            )
            for activation in self._ledger.list_team_skill_activations(agent_job.id)
        }

        def record_activation_once(
            *,
            activation_type: str,
            activation_id: str,
            source: str,
        ) -> None:
            activation_key = (activation_type, activation_id, source)
            if activation_key in existing_activation_keys:
                return
            self._ledger.record_team_skill_activation(
                team_run_id=team_run.id,
                agent_job_id=agent_job.id,
                activation_type=activation_type,
                activation_id=activation_id,
                source=source,
                token_cost=0,
            )
            self._emit_team_runtime_event(
                EventType.TEAM_SKILL_ACTIVATED,
                conversation_id=team_run.conversation_id,
                orchestration_run_id=team_run.orchestration_run_id or 0,
                team_run_id=team_run.id,
                agent_job_id=agent_job.id,
                payload={
                    "activation_type": activation_type,
                    "activation_id": activation_id,
                    "source": source,
                    "role": role_id.value,
                    "model_profile": model_profile,
                },
            )
            existing_activation_keys.add(activation_key)

        for skill_id in selected_skill_ids:
            record_activation_once(
                activation_type="skill",
                activation_id=skill_id,
                source="role_default",
            )
        for tool_id in selected_tool_ids:
            record_activation_once(
                activation_type="tool",
                activation_id=tool_id,
                source="role_capability",
            )
        for instinct in selected_instincts:
            record_activation_once(
                activation_type="memory",
                activation_id=instinct.instinct_id,
                source="instinct_memory",
            )
        self._emit_team_runtime_event(
            EventType.TEAM_CAPABILITY_BUDGET_APPLIED,
            conversation_id=team_run.conversation_id,
            orchestration_run_id=team_run.orchestration_run_id or 0,
            team_run_id=team_run.id,
            agent_job_id=agent_job.id,
            payload={
                "role": role_id.value,
                "model_profile": model_profile,
                "budget": capability_budget,
                "selected_skills": list(selected_skill_ids),
                "selected_tools": list(selected_tool_ids),
                "selected_memories": [
                    instinct.instinct_id for instinct in selected_instincts
                ],
            },
        )

        return build_team_context_packet(
            TeamContextInput(
                team_run_id=team_run.id,
                agent_job_id=agent_job.id,
                conversation_id=team_run.conversation_id,
                orchestration_run_id=team_run.orchestration_run_id or 0,
                role=role_def,
                model_profile=model_profile,
                user_goal=team_run.goal,
                workspace_alias=conversation.workspace_alias,
                route_kind=self._team_route_kind(team_run) or "",
                skills=selected_skill_ids,
                allowed_capabilities=selected_tool_ids,
                artifact_summaries=artifact_summaries,
                relevant_instincts=relevant_instincts,
                capability_budget=capability_budget,
                skill_activations=selected_activation_ids,
                source_refs=source_refs,
                evidence_refs=tuple(dict.fromkeys(evidence_refs)),
                resume_state=resume_state,
                output_schema=output_schema,
                token_budget=budget.max_prompt_tokens,
            )
        )

    def _team_route_kind(self, team_run: object | None) -> str:
        if team_run is None or not hasattr(self._ledger, "list_team_artifacts"):
            return ""
        for artifact in self._ledger.list_team_artifacts(team_run.id):
            if artifact.artifact_type != "routing_decision":
                continue
            payload = getattr(artifact, "payload", {})
            if isinstance(payload, dict):
                route_kind = str(payload.get("route_kind", "")).strip()
                if route_kind:
                    return route_kind
        return ""

    def _gate_a_artifact_type_and_validator(self, team_run: object) -> tuple[str, object]:
        route_kind = self._team_route_kind(team_run) or "feature"
        if route_kind == "bug":
            from wlcodex.team_artifacts import validate_diagnosis_report

            return "diagnosis_report", validate_diagnosis_report
        from wlcodex.team_artifacts import validate_architecture_plan

        return "architecture_plan", validate_architecture_plan

    def _emit_event(self, event: RuntimeEvent) -> RuntimeEvent:
        if self._store is None:
            return event
        return self._store.append(event)

    def _emit_team_runtime_event(
        self,
        event_type: str,
        *,
        conversation_id: int,
        orchestration_run_id: int,
        team_run_id: int,
        agent_job_id: int | None = None,
        agent_run_id: int | None = None,
        task_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        event_payload: dict[str, Any] = {"team_run_id": team_run_id}
        if agent_job_id is not None:
            event_payload["agent_job_id"] = agent_job_id
        if payload:
            event_payload.update(payload)
        return self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=event_type,
            aggregate_type=AggregateType.ORCHESTRATION_RUN,
            aggregate_id=str(orchestration_run_id),
            correlation_id=f"team-run-{team_run_id}",
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload=event_payload,
            occurred_at=now_iso(),
            conversation_id=conversation_id,
            orchestration_run_id=orchestration_run_id,
            agent_run_id=agent_run_id,
            task_id=task_id,
        ))

    def _latest_team_artifact_payload(
        self,
        *,
        team_run: object,
        artifact_type: str,
        agent_job_id: int | None = None,
    ) -> tuple[int | None, dict[str, Any] | None]:
        if not hasattr(self._ledger, "list_team_artifacts"):
            return None, None
        for artifact in reversed(self._ledger.list_team_artifacts(team_run.id)):
            if artifact.artifact_type == artifact_type:
                if agent_job_id is not None and artifact.agent_job_id != agent_job_id:
                    continue
                return artifact.id, dict(artifact.payload)
        return None, None

    def _latest_team_job_id(
        self,
        team_run: object,
        *,
        role: str,
        status: str = "done",
    ) -> int | None:
        if not hasattr(self._ledger, "list_team_agent_jobs"):
            return None
        for job in reversed(self._ledger.list_team_agent_jobs(team_run.id)):
            if job.role == role and job.status == status:
                return int(job.id)
        return None

    def _latest_implementation_scope(
        self,
        team_run: object,
        *,
        agent_job_id: int | None,
    ) -> tuple[list[str], str]:
        _artifact_id, payload = self._latest_team_artifact_payload(
            team_run=team_run,
            artifact_type="implementation_report",
            agent_job_id=agent_job_id,
        )
        if payload is None:
            return [], ""
        from wlcodex.team_artifacts import structured_implementation_evidence_from_text

        summary_evidence = structured_implementation_evidence_from_text(
            str(payload.get("summary", ""))
        )
        merged = {**payload, **summary_evidence}
        files = [
            str(path).strip()
            for path in merged.get("changed_files", [])
            if str(path).strip()
        ]
        diff_summary = str(merged.get("diff_summary", "")).strip()
        return list(dict.fromkeys(files)), diff_summary

    def _task_workspace_baseline_dirty_files(self, task_id: int | None) -> list[str]:
        if task_id is None:
            return []
        events = self._ledger.list_events(task_id, limit=500)
        for event in reversed(events):
            if event.event_type != "task_workspace_baseline":
                continue
            dirty_files = event.payload.get("dirty_files")
            if not isinstance(dirty_files, list):
                return []
            paths: list[str] = []
            for item in dirty_files:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "")).strip()
                if path:
                    paths.append(path)
            return list(dict.fromkeys(paths))
        return []

    def _team_gate_response(
        self,
        *,
        gate_name: str,
        artifact_type: str,
        validator: object,
        team_run: object,
        conversation_id: int,
        orchestration_run_id: int,
        agent_job_id: int | None = None,
        bind_agent_job: bool = False,
        failure_step: str | None = None,
    ) -> ControllerResponse | None:
        if bind_agent_job and agent_job_id is None:
            artifact_id = None
            payload = None
            missing = (f"{artifact_type}_current_agent_job",)
        else:
            artifact_id, payload = self._latest_team_artifact_payload(
                team_run=team_run,
                artifact_type=artifact_type,
                agent_job_id=agent_job_id if bind_agent_job else None,
            )
            missing = (artifact_type,) if payload is None else ()
        if payload is None:
            pass
        else:
            if artifact_type == "implementation_report":
                from wlcodex.team_artifacts import (
                    structured_implementation_evidence_from_text,
                )

                summary_evidence = structured_implementation_evidence_from_text(
                    str(payload.get("summary", ""))
                )
                if summary_evidence:
                    payload = {**payload, **summary_evidence}
            elif artifact_type == "test_report":
                impl_artifact_id, impl_payload = self._latest_team_artifact_payload(
                    team_run=team_run,
                    artifact_type="implementation_report",
                    agent_job_id=agent_job_id if bind_agent_job else None,
                )
                if impl_payload is not None:
                    from wlcodex.team_artifacts import (
                        acceptance_criteria_from_artifacts,
                        structured_implementation_evidence_from_text,
                        test_report_payload_from_implementation,
                    )

                    summary_evidence = structured_implementation_evidence_from_text(
                        str(impl_payload.get("summary", ""))
                    )
                    if summary_evidence.get("tests_attempted"):
                        payload = test_report_payload_from_implementation(
                            summary=str(payload.get("summary", "")),
                            implementation_artifact_id=impl_artifact_id,
                            commands_run=summary_evidence["tests_attempted"],
                            acceptance_criteria=acceptance_criteria_from_artifacts(
                                self._ledger.list_team_artifacts(team_run.id)
                            ),
                        )
            result = validator(payload)
            if result.passed:
                self._emit_team_runtime_event(
                    EventType.TEAM_GATE_PASSED,
                    conversation_id=conversation_id,
                    orchestration_run_id=orchestration_run_id,
                    team_run_id=team_run.id,
                    payload={
                        "gate": gate_name,
                        "artifact_type": artifact_type,
                        "artifact_id": artifact_id,
                        "agent_job_id": agent_job_id,
                    },
                )
                return None
            missing = result.missing
        self._emit_team_runtime_event(
            EventType.TEAM_GATE_FAILED,
            conversation_id=conversation_id,
            orchestration_run_id=orchestration_run_id,
            team_run_id=team_run.id,
            payload={
                "gate": gate_name,
                "artifact_type": artifact_type,
                "artifact_id": artifact_id,
                "agent_job_id": agent_job_id,
                "missing": list(missing),
            },
        )
        buttons = None
        if failure_step:
            self._ledger.update_orchestration_run(
                orchestration_run_id,
                status="needs_user",
                current_step=failure_step,
            )
            orch_run = self._ledger.get_orchestration_run(orchestration_run_id)
            buttons = self._auto_stage_buttons(
                conversation_id,
                failure_step,
                orch_run=orch_run,
                last_codex_analysis=(
                    getattr(orch_run, "last_codex_analysis", "") if orch_run else ""
                ),
            )
        missing_text = "、".join(_human_gate_field_name(field) for field in missing)
        artifact_label = _human_artifact_type(artifact_type)
        tester_note = ""
        if gate_name == "Gate C":
            tester_note = (
                "\n说明：测试工程师跟随开发工程师，本轮测试记录需要来自当前实现。"
            )
        retry_note = (
            "\n已进入返工阶段，可交给 DeepSeek 开发工程师或 GPT 开发工程师补齐后重新验收。"
            if failure_step else ""
        )
        return ControllerResponse(
            f"验收前还缺少{artifact_label}：{missing_text}。\n"
            "请先让开发团队补齐这些证据，再进入下一步。"
            f"{tester_note}{retry_note}",
            buttons=buttons,
        )

    def _new_correlation_id(self) -> str:
        return str(uuid.uuid4())

    def _reserve_execution_lease(
        self,
        *,
        conversation_id: int,
        workspace_alias: str,
        prompt: str,
        telegram_chat_id: int | None,
        purpose: str,
    ) -> object:
        if self._execution_scheduler is None:
            raise RuntimeError("execution scheduler unavailable")
        lease = self._execution_scheduler.reserve(RunIntent(
            conversation_id=conversation_id,
            workspace_alias=workspace_alias,
            prompt=prompt,
            telegram_chat_id=telegram_chat_id,
            purpose=purpose,
        ))
        task = getattr(lease, "task", None)
        if task is not None:
            return task
        return self._service.get_task(lease.hidden_task_id)

    async def handle(
        self, text: str, telegram_context: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Parse a text command and return a response. Never injects status/log
        text into Codex prompts."""
        try:
            command = parse_command(text)
        except ParseError as exc:
            return ControllerResponse(str(exc))

        try:
            if self._legacy_diagnostics.can_handle(command):
                response = await self._legacy_diagnostics.handle(
                    command, telegram_context
                )
                return ControllerResponse(
                    response.text,
                    buttons=getattr(response, "buttons", []),
                    already_rendered=getattr(response, "already_rendered", False),
                )

            if isinstance(command, HelpCommand):
                return ControllerResponse(HELP_TEXT)

            elif isinstance(command, HealthCommand):
                if self._ledger is not None:
                    snapshot = build_health_snapshot(self._ledger, self._backend)
                else:
                    snapshot = None
                return ControllerResponse(render_health_card(
                    self._backend.health(), snapshot=snapshot
                ))

            elif isinstance(command, StatusCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        runs = self._ledger.list_agent_runs(active.id, limit=1)
                        latest_run = runs[0] if runs else None
                        orch_runs = self._ledger.list_orchestration_runs(active.id, limit=1)
                        orch_run = orch_runs[0] if orch_runs else None
                        surface_mode = self._latest_surface_mode(active.id) or "product"
                        buttons: list[list[dict[str, str]]] = []
                        auto_run = self._latest_active_auto_run(active.id)
                        if auto_run is not None:
                            buttons = self._auto_stage_buttons(
                                active.id,
                                auto_run.current_step,
                                orch_run=auto_run,
                                last_codex_analysis=auto_run.last_codex_analysis or "",
                            )
                            orch_run = auto_run
                        status_text = render_conversation_status(
                            active, latest_run=latest_run, orch_run=orch_run,
                            surface_mode=surface_mode,
                        )
                        if (
                            orch_run is not None
                            and hasattr(self._ledger, "get_team_run_for_orchestration")
                        ):
                            team_run = self._ledger.get_team_run_for_orchestration(
                                orch_run.id
                            )
                            if team_run is not None:
                                roles: list[tuple[str, str, str]] = []
                                if hasattr(self._ledger, "list_team_agent_jobs"):
                                    roles = [
                                        (
                                            job.role,
                                            job.model_profile,
                                            job.status,
                                        )
                                        for job in self._ledger.list_team_agent_jobs(
                                            team_run.id
                                        )
                                    ]
                                latest_artifacts: list[str] = []
                                if hasattr(self._ledger, "list_team_artifacts"):
                                    artifacts = self._ledger.list_team_artifacts(
                                        team_run.id
                                    )
                                    latest_artifacts = [
                                        (
                                            f"{artifact.artifact_type}: "
                                            f"{artifact.summary}"
                                        )
                                        for artifact in reversed(artifacts[-4:])
                                    ]
                                team_summary = render_team_status_summary(
                                    team_run.goal,
                                    team_run.route,
                                    roles,
                                    latest_artifacts,
                                )
                                status_text = f"{status_text}\n\n{team_summary}"
                        return ControllerResponse(
                            status_text,
                            buttons=buttons,
                        )
                return ControllerResponse(
                    "当前还没有工作台。发送 /new 开始一个新的工作台。"
                )

            elif isinstance(command, TraceCommand):
                if self._ledger is None or self._store is None:
                    return ControllerResponse("运行时事件记录不可用。")
                chat_id = telegram_context.get("chat_id", 0) if telegram_context else 0
                active = self._ledger.get_active_conversation(chat_id)
                if active is None:
                    return ControllerResponse("暂无活跃对话。")
                result = self._inspector.trace_runtime(
                    self._store,
                    active.id,
                    limit=command.limit,
                    visibility_filter="operator",
                )
                return ControllerResponse(f"{result.title}\n\n{result.body}")

            elif isinstance(command, DiffCommand):
                task_id = command.task_id
                if task_id is None and self._ledger is not None:
                    active = self._ledger.get_active_conversation(
                        telegram_context.get("chat_id", 0) if telegram_context else 0
                    )
                    if active and active.active_codex_task_id:
                        task_id = active.active_codex_task_id
                    elif active and active.active_claude_run_id:
                        # Claude run: use workspace git diff directly
                        ws = str(self._service.get_workspace(active.workspace_alias).path)
                        result = self._inspector.diff(
                            active.active_claude_run_id, ws
                        )
                        return ControllerResponse(
                            f"{result.title}\n\n{result.body}"
                        )
                if task_id is None:
                    return ControllerResponse(
                        "当前还没有可查看变更的工作台。发送 /new 开始新的工作台。"
                    )
                task = self._service.get_task(task_id)
                result = self._inspector.diff(task_id, task.workspace_path)
                return ControllerResponse(
                    f"{result.title}\n\n{result.body}"
                )

            elif isinstance(command, FilesCommand):
                task_id = command.task_id
                if task_id is None and self._ledger is not None:
                    active = self._ledger.get_active_conversation(
                        telegram_context.get("chat_id", 0) if telegram_context else 0
                    )
                    if active and active.active_codex_task_id:
                        task_id = active.active_codex_task_id
                    elif active and active.active_claude_run_id:
                        # Claude run: use agent run for files lookup
                        task_id = active.active_claude_run_id
                if task_id is None:
                    return ControllerResponse(
                        "当前还没有可查看文件的工作台。发送 /new 开始新的工作台。"
                    )
                result = self._inspector.files(task_id)
                return ControllerResponse(
                    f"{result.title}\n\n{result.body}"
                )

            elif isinstance(command, CodexSessionsCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        from wlcodex.workbench.rendering import render_session_library
                        from wlcodex.workbench.sessions import AgentSessionLibrary

                        sessions = AgentSessionLibrary(self._ledger).list_for_workbench(
                            active.id
                        )
                        return ControllerResponse(render_session_library(sessions))
                return ControllerResponse(
                    "当前还没有工作台。发送 /new 开始一个新的工作台。"
                )

            elif isinstance(command, WorkbenchHistoryCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    sessions = self._ledger.list_conversations_by_chat(
                        chat_id, include_archived=True
                    )
                    active_count = sum(1 for s in sessions if s.archived_at is None)
                    archived_count = sum(1 for s in sessions if s.archived_at is not None)
                    if not sessions:
                        return ControllerResponse(
                            "还没有历史工作台。发送 /new 开始新的工作台。"
                        )
                    return ControllerResponse(
                        f"工作台历史 — {active_count} 个进行中，{archived_count} 个已归档\n"
                        f"点击下方按钮恢复已归档的工作台："
                    )
                return ControllerResponse(
                    "还没有历史工作台。发送 /new 开始新的工作台。"
                )

            elif isinstance(command, WorkspaceListCommand):
                active_alias = ""
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        active_alias = active.workspace_alias
                workspaces = list(self._service._workspaces.values())
                return ControllerResponse(
                    render_workspace_list(workspaces, active_alias=active_alias),
                    buttons=self._workspace_selection_buttons(
                        workspaces, active_alias=active_alias
                    ),
                )

            elif isinstance(command, WorkspaceStatusCommand):
                return await self.handle_current_workspace(telegram_context)

            # --- New conversation commands ---

            elif isinstance(command, NewConversationCommand):
                return await self.handle_new_conversation(command, telegram_context)

            elif isinstance(command, CodexDirectCommand):
                return await self.handle_codex_direct(command, telegram_context)

            elif isinstance(command, ClaudeDirectCommand):
                return await self.handle_claude_direct(command, telegram_context)

            elif isinstance(command, ClaudePermissionCommand):
                return await self.handle_claude_permission(command, telegram_context)

            elif isinstance(command, AutoModeCommand):
                return await self.handle_auto_mode(command, telegram_context)

            elif isinstance(command, StopCurrentCommand):
                return await self.handle_stop_current(telegram_context)

            elif isinstance(command, SwitchWorkspaceCommand):
                return await self.handle_switch_workspace(command, telegram_context)

            elif isinstance(command, ModelCommand):
                return await self.handle_model(command, telegram_context)

            elif isinstance(command, ExecModeCommand):
                return await self.handle_exec_mode(command, telegram_context)

            elif isinstance(command, VerifyCommand):
                return await self.handle_verify(command, telegram_context)

            elif isinstance(command, CarryWorkbenchCommand):
                return await self.handle_carry_workbench(command, telegram_context)

            else:
                return ControllerResponse("未处理的命令类型。")

        except Exception as exc:
            logger.exception("Command handler error")
            return ControllerResponse(f"错误：{exc}")

    # --- Conversation handlers ---

    def _workspace_selection_buttons(
        self, workspaces: list[object], *, active_alias: str = ""
    ) -> list[list[dict[str, str]]]:
        buttons: list[list[dict[str, str]]] = []
        for workspace in workspaces:
            alias = str(getattr(workspace, "alias", "")).strip()
            if not alias:
                continue
            label = f"当前 {alias}" if alias == active_alias else f"切换 {alias}"
            buttons.append([{
                "text": label,
                "callback_data": f"settings:workspace:{alias}",
            }])
        return buttons

    async def handle_conversation_text(
        self, text: str, telegram_context: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Route plain text through the conversation state machine.

        Diagnostic commands → read-only handler.
        Normal text → either creates new conversation or appends to active one
        based on conversation state.
        Never injects status/log into model prompts.
        """
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = telegram_context.get("chat_id", 0) if telegram_context else 0
        user_id = telegram_context.get("user_id", 0) if telegram_context else 0

        # --- Lightweight greeting: fast path ---
        if _is_lightweight_greeting(text):
            active = self._ledger.get_active_conversation(chat_id)
            if active is None:
                active = self._ledger.create_conversation(
                    chat_id=chat_id, user_id=user_id,
                    title=default_title(text),
                    mode=self._default_mode,
                    workspace_alias=self._default_workspace,
                )
            self._ledger.update_conversation_summary(
                active.id,
                trim_to_budget(
                    "用户打招呼，等待具体需求。",
                    ContextBudget().conversation_summary_tokens,
                ),
            )
            return ControllerResponse("你好！直接说需要我看什么就行。")

        # --- Route diagnostic commands to the existing command parser ---
        intent = classify_intent(text)
        if intent == "diagnostic":
            try:
                return await self.handle(text, telegram_context)
            except Exception:
                return ControllerResponse("命令处理出错，请重试。")

        # --- Consume pending carryover: next non-command text creates target ---
        pending = self._ledger.get_latest_prepared_carryover(chat_id)
        if pending is not None:
            return await self._consume_prepared_carryover(
                pending, text, telegram_context,
            )

        # --- Get active conversation and its runtime state ---
        active = self._ledger.get_active_conversation(chat_id)
        conv_state: str | None = None
        if active is not None and self._store is not None:
            conv_state = self._store.get_conversation_runtime_state(active.id)

        # --- Check workspace busy for new-conversation triggers ---
        workspace_busy = False
        blocking_task_id: int | None = None
        blocking_run_id: int | None = None
        if intent == "new_trigger" or active is None or conv_state in (None, "passed", "failed", "aborted", "done"):
            blocker = self._service.blocker_for_workspace(self._default_workspace)
            if blocker is not None:
                workspace_busy = True
                blocking_task_id = blocker.id

        # --- Route ---
        decision = route_message(
            text,
            active_conversation_id=active.id if active else None,
            active_conversation_state=conv_state,
            chat_id=chat_id,
            workspace_busy=workspace_busy,
            blocking_task_id=blocking_task_id,
            blocking_run_id=blocking_run_id,
        )

        # --- Emit routing events ---
        cid = self._new_correlation_id()
        if active is None and decision.route == "new_conversation":
            # Create conversation first so we have an id for events
            title = default_title(text)
            active = self._ledger.create_conversation(
                chat_id=chat_id, user_id=user_id,
                title=title, mode=self._default_mode,
                workspace_alias=self._default_workspace,
            )
            # Emit conversation.started
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_STARTED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"chat_id": chat_id, "title": title,
                         "mode": self._default_mode,
                         "workspace_alias": self._default_workspace},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))

        # Always emit user.message.received
        conv_id = active.id if active else 0
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(conv_id),
            correlation_id=cid,
            source=EventSource.TELEGRAM,
            actor="user",
            visibility=Visibility.USER,
            payload={
                "text_preview": safe_text_preview(text),
                "text_length": len(text),
                "chat_id": chat_id,
            },
            occurred_at=now_iso(),
            conversation_id=conv_id if conv_id else None,
        ))

        # --- Handle route ---
        if decision.route == "workspace_busy":
            return await self._handle_workspace_busy(
                decision, active, blocking_task_id, blocking_run_id, cid,
                original_text=text,
            )

        # /auto owns plain text while its staged workflow is active.  This must
        # run before the generic append/reanalysis route, otherwise supplement
        # text cancels the auto run and falls back to long Codex-direct output.
        if active is not None and self._ledger is not None:
            auto_run = self._latest_active_auto_run(active.id)
            if auto_run is not None:
                step = getattr(auto_run, "current_step", "")

                if step == AUTO_COLLECTING_CONTEXT:
                    return await self._handle_auto_context_supplement(
                        text, active, auto_run, telegram_context, cid
                    )

                if step == AUTO_DRAFT_READY:
                    self._ledger.update_orchestration_run(
                        auto_run.id,
                        status="running",
                        current_step=AUTO_COLLECTING_CONTEXT,
                    )
                    return await self._handle_auto_context_supplement(
                        text, active, auto_run, telegram_context, cid
                    )

                if step in (AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
                    self._ledger.update_conversation_summary(
                        active.id,
                        trim_to_budget(
                            f"{active.conversation_summary}\n[用户备注] {text[:200]}",
                            ContextBudget().conversation_summary_tokens,
                        ),
                    )
                    buttons = self._auto_stage_buttons(
                        active.id,
                        step,
                        orch_run=auto_run,
                        last_codex_analysis=auto_run.last_codex_analysis or "",
                    )
                    return ControllerResponse(
                        f"已记录备注：{text[:100]}",
                        buttons=buttons,
                    )

        if decision.route == "append_active_conversation":
            return await self._handle_append_to_conversation(
                decision, text, active, telegram_context, cid
            )

        # --- new_conversation (default) ---
        # If active conversation is terminal, archive it first.
        if active is not None and conv_state in ("passed", "failed", "aborted", "done"):
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_CLOSED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"reason": conv_state, "next_conversation": True},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))
            self._ledger.archive_conversation(active.id)
            title = default_title(text)
            active = self._ledger.create_conversation(
                chat_id=chat_id, user_id=user_id,
                title=title, mode=self._default_mode,
                workspace_alias=self._default_workspace,
            )
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_STARTED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"chat_id": chat_id, "title": title,
                         "mode": self._default_mode,
                         "workspace_alias": self._default_workspace},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))

        # Emit conversation.message.routed
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_MESSAGE_ROUTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={
                "chat_id": chat_id,
                "route": decision.route,
                "reason": decision.reason,
                "conversation_state": conv_state,
                "conversation_id": active.id,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # --- Plain text: read-only Codex analysis by default.
        # Full Codex -> Claude -> Codex orchestration is explicit via /auto.
        return await self._handle_codex_analysis_only(
            text, active, telegram_context, cid
        )

    async def _start_codex_turn_for_conversation(
        self,
        *,
        active: object,
        task: object,
        workspace_path: str,
        prompt: str,
        interaction_mode: str = "general",
    ) -> str:
        thread_id = str(getattr(active, "codex_thread_id", "") or "")
        current_policy = codex_thread_policy_fingerprint(self._backend)
        stored_policy = str(getattr(active, "codex_thread_policy", "") or "")
        if can_reuse_codex_thread(thread_id, stored_policy, current_policy):
            self._service.set_task_thread(task.id, thread_id)
            continue_prompt_turn = getattr(self._backend, "continue_prompt_turn", None)
            continue_turn = getattr(self._backend, "continue_turn", None)
            if interaction_mode != "general" and callable(continue_prompt_turn):
                await continue_prompt_turn(thread_id, prompt, interaction_mode)
            elif callable(continue_turn):
                await continue_turn(thread_id, prompt)
            else:
                await self._backend.start_turn(thread_id, prompt)
            return thread_id

        create_prompt_thread = getattr(self._backend, "create_prompt_thread", None)
        if interaction_mode != "general" and callable(create_prompt_thread):
            thread_id = await create_prompt_thread(workspace_path, interaction_mode)
        else:
            thread_id = await self._backend.create_thread(workspace_path)
        self._service.set_task_thread(task.id, thread_id)
        self._ledger.set_conversation_codex_thread(
            active.id, thread_id, current_policy
        )
        start_prompt_turn = getattr(self._backend, "start_prompt_turn", None)
        if interaction_mode != "general" and callable(start_prompt_turn):
            await start_prompt_turn(thread_id, prompt, interaction_mode)
        else:
            await self._backend.start_turn(thread_id, prompt)
        return thread_id

    async def _handle_codex_analysis_only(
        self,
        text: str,
        active: object,
        ctx: dict[str, Any] | None,
        correlation_id: str,
    ) -> ControllerResponse:
        """Codex-direct or Claude-disabled: Codex analysis only."""
        chat_id = ctx.get("chat_id", 0) if ctx else 0
        budget = ContextBudget()
        packet = build_codex_analysis_packet(
            user_goal=text,
            conversation_summary=trim_to_budget(
                active.conversation_summary, budget.conversation_summary_tokens
            ),
            constraints=[],
            workspace=active.workspace_alias,
            budget=budget,
            handoff=False,
        )
        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=text,
            telegram_chat_id=chat_id,
            purpose="codex_analysis",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id, agent="codex", role="analysis",
            hidden_task_id=task.id, prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")
        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
        try:
            await self._start_codex_turn_for_conversation(
                active=active,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))
        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(f"用户请求：{text[:200]}", budget.conversation_summary_tokens),
        )
        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(active.id, CONTINUE)},
        ]]
        return ControllerResponse(
            "我先看一下。完成后会把结论发在这里。",
            buttons=buttons,
        )

    async def _handle_auto_context_supplement(
        self,
        text: str,
        active: object,
        auto_run: object,
        ctx: dict[str, Any] | None,
        correlation_id: str,
    ) -> ControllerResponse:
        """Handle plain text during auto collecting_context stage.

        Steers existing Codex analysis turn if one is active,
        or starts a new read-only context supplement turn.
        Does NOT create a new task or start Claude.
        """
        # Update conversation summary with supplemented context
        summary_prefix = active.conversation_summary or ""
        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"{summary_prefix}\n[Auto补充] {text[:200]}",
                ContextBudget().conversation_summary_tokens,
            ),
        )

        # Try to steer the current active Codex turn
        task_id = getattr(active, "active_codex_task_id", None)
        if task_id:
            try:
                task = self._service.get_task(task_id)
                if (
                    task is not None
                    and getattr(task, "codex_thread_id", None)
                    and getattr(task, "active_turn_id", None)
                ):
                    await self._backend.steer_turn(
                        task.codex_thread_id,
                        task.active_turn_id,
                        text,
                    )
                    buttons = self._auto_stage_buttons(
                        active.id,
                        AUTO_COLLECTING_CONTEXT,
                        orch_run=auto_run,
                    )
                    return ControllerResponse(
                        f"已补充到当前诊断工程师分析：{text[:100]}",
                        buttons=buttons,
                    )
            except Exception:
                logger.debug("Failed to steer existing auto Codex turn", exc_info=True)

        # If no active turn to steer, start a new read-only context supplement turn
        budget = ContextBudget()
        packet = build_auto_context_packet(
            user_goal=text,
            conversation_summary=trim_to_budget(
                active.conversation_summary, budget.conversation_summary_tokens
            ),
            workspace=active.workspace_alias,
            budget=budget,
        )

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=text,
            telegram_chat_id=chat_id,
            purpose="auto_context_supplement",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role=ROLE_AUTO_CONTEXT_SUPPLEMENT,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=active,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        buttons = self._auto_stage_buttons(
            active.id,
            AUTO_COLLECTING_CONTEXT,
            orch_run=auto_run,
        )
        return ControllerResponse(
            f"已补充信息到当前 /auto 分析。{text[:100]}",
            buttons=buttons,
        )

    async def _handle_append_to_conversation(
        self,
        decision: RouteDecision,
        text: str,
        active: object,
        ctx: dict[str, Any] | None,
        correlation_id: str,
    ) -> ControllerResponse:
        """Append user context to active non-terminal conversation."""
        # Emit conversation.message.routed
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_MESSAGE_ROUTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={
                "chat_id": ctx.get("chat_id", 0) if ctx else 0,
                "route": decision.route,
                "reason": decision.reason,
                "conversation_state": decision.conversation_state,
                "conversation_id": active.id,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # Approval supersession: cancel pending approvals
        if decision.requires_approval_supersession:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.APPROVAL_SUPERSEDED,
                aggregate_type=AggregateType.APPROVAL,
                aggregate_id=f"approval-conv-{active.id}",
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"reason": "user_context_appended",
                         "conversation_id": active.id},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))

        # Emit user.context.appended
        delivery_policy = decision.delivery_policy or "codex_immediate_review"
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_CONTEXT_APPENDED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="user",
            visibility=Visibility.USER,
            payload={
                "chat_id": ctx.get("chat_id", 0) if ctx else 0,
                "text_preview": safe_text_preview(text),
                "text_length": len(text),
                "conversation_state_at_append": decision.conversation_state,
                "delivery_policy": delivery_policy,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # --- Implementation / verification: record pending context ---
        if delivery_policy == "codex_phase_boundary_review":
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_PENDING_CONTEXT_RECORDED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={
                    "text_preview": safe_text_preview(text),
                    "text_length": len(text),
                    "conversation_state": decision.conversation_state,
                },
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))
            # Update conversation summary with pending context note
            self._ledger.update_conversation_summary(
                active.id,
                trim_to_budget(
                    f"{active.conversation_summary}\n[待审核追加] {text[:200]}",
                    500,
                ),
            )
            return ControllerResponse(MID_RUN_ACKNOWLEDGEMENT)

        # --- Analysis / waiting_approval / needs_user: immediate Codex review ---
        # Cancel the active orchestration run (if any) before re-analysis.
        await self._cancel_active_runs_for_conversation(active.id, correlation_id)

        # Update conversation summary with appended context.
        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"{active.conversation_summary}\n[用户补充] {text[:200]}",
                500,
            ),
        )
        # Plain text follow-ups remain read-only by default. Use /auto for
        # explicit Codex -> Claude -> Codex implementation flow.
        return await self._handle_codex_analysis_only(
            text, active, ctx, correlation_id,
        )

    async def _cancel_active_runs_for_conversation(
        self, conversation_id: int, correlation_id: str,
    ) -> None:
        """Cancel all active orchestration/agent runs for a conversation.

        Emits run.cancelled events so the re-analyze path doesn't hit
        WorkspaceBusy from the previous run.
        """
        try:
            orch_runs = self._ledger.list_orchestration_runs(conversation_id, limit=10)
            for orch_run in orch_runs:
                if orch_run.status not in ("passed", "failed", "aborted"):
                    self._emit_event(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.RUN_CANCEL_REQUESTED,
                        aggregate_type=AggregateType.ORCHESTRATION_RUN,
                        aggregate_id=str(orch_run.id),
                        correlation_id=correlation_id,
                        source=EventSource.CONTROLLER,
                        actor="controller",
                        visibility=Visibility.OPERATOR,
                        payload={"reason": "user_context_appended_reanalysis",
                                 "conversation_id": conversation_id},
                        occurred_at=now_iso(),
                        conversation_id=conversation_id,
                        orchestration_run_id=orch_run.id,
                    ))
                    self._emit_event(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.RUN_CANCELLED,
                        aggregate_type=AggregateType.ORCHESTRATION_RUN,
                        aggregate_id=str(orch_run.id),
                        correlation_id=correlation_id,
                        source=EventSource.CONTROLLER,
                        actor="controller",
                        visibility=Visibility.OPERATOR,
                        payload={"reason": "user_context_appended_reanalysis",
                                 "conversation_id": conversation_id},
                        occurred_at=now_iso(),
                        conversation_id=conversation_id,
                        orchestration_run_id=orch_run.id,
                    ))
                    # Abort the underlying task
                    task_id = getattr(orch_run, "task_id", None)
                    if task_id is None:
                        # Find task by conversation
                        conv = self._ledger.get_conversation(conversation_id)
                        task_id = conv.active_codex_task_id
                    if task_id is not None:
                        try:
                            self._service.abort_task(task_id)
                        except Exception:
                            pass
        except Exception:
            logger.debug("Failed to cancel active runs for reanalysis", exc_info=True)

    def _execution_lane_decision(
        self, active_run: object | None, incoming_kind: str
    ) -> str:
        """Return idle, append, explicit_choice, or onsite_input.

        Rules (Repair Plan Task 2):
        - Cockpit ordinary text + idle -> idle
        - Cockpit ordinary text + active task/run -> append
        - explicit /codex or /claude + active task/run -> explicit_choice
        - Onsite text + selected session -> onsite_input
        """
        if incoming_kind == "onsite_text":
            return "onsite_input"
        if incoming_kind in ("codex_direct", "claude_direct"):
            if active_run is not None:
                return "explicit_choice"
            return "idle"
        if active_run is not None:
            return "append"
        return "idle"

    def _blocking_task_for(self, workspace_alias: str) -> object | None:
        try:
            return self._service.blocker_for_workspace(workspace_alias)
        except Exception:
            logger.debug("Failed to check workspace blocker", exc_info=True)
            return None

    def _busy_append_agent_label(self, convo: object, fallback: str = "现场") -> str:
        if getattr(convo, "active_codex_task_id", None):
            return "GPT 开发工程师"
        if getattr(convo, "active_claude_run_id", None):
            return "DeepSeek 开发工程师"
        return fallback

    async def _direct_command_busy_response(
        self,
        *,
        active: object,
        original_text: str,
        agent_label: str,
    ) -> ControllerResponse | None:
        blocker = self._blocking_task_for(active.workspace_alias)
        if blocker is None:
            return None
        decision = RouteDecision(
            route="workspace_busy",
            reason="direct_command_workspace_busy",
            new_conversation=False,
            intent="diagnostic",
            conversation_state="running",
        )
        return await self._handle_workspace_busy(
            decision,
            active,
            getattr(blocker, "id", None),
            None,
            self._new_correlation_id(),
            original_text=original_text,
            agent_label=self._busy_append_agent_label(active, agent_label),
        )

    async def handle_terminal_workspace_busy(
        self,
        active: object,
        original_text: str,
        *,
        agent_label: str = "现场",
    ) -> ControllerResponse:
        """Return the same busy choice card for terminal/onsite input."""
        decision = RouteDecision(
            route="workspace_busy",
            reason="terminal_workspace_busy",
            new_conversation=False,
            intent="normal_text",
            conversation_state="running",
        )
        return await self._handle_workspace_busy(
            decision,
            active,
            getattr(active, "active_codex_task_id", None),
            getattr(active, "active_claude_run_id", None),
            self._new_correlation_id(),
            original_text=original_text,
            agent_label=agent_label,
        )

    def _prompt_from_pending_text(self, text: str) -> str:
        stripped = text.strip()
        try:
            command = parse_command(stripped)
        except Exception:
            return stripped
        return str(getattr(command, "prompt", stripped) or stripped)

    async def _send_pending_to_current_session(
        self, convo: object, original_text: str
    ) -> ControllerResponse | None:
        prompt = self._prompt_from_pending_text(original_text)
        if not prompt:
            return None

        task_id = getattr(convo, "active_codex_task_id", None)
        if task_id:
            try:
                task = self._service.get_task(task_id)
            except Exception:
                task = None
            if (
                task is not None
                and getattr(task, "codex_thread_id", None)
                and getattr(task, "active_turn_id", None)
            ):
                await self._backend.steer_turn(
                    task.codex_thread_id,
                    task.active_turn_id,
                    prompt,
                )
                return ControllerResponse("已发给当前 GPT 开发工程师。")

        run_id = getattr(convo, "active_claude_run_id", None)
        if run_id and self._claude is not None:
            try:
                run = self._ledger.get_agent_run(run_id)
            except Exception:
                run = None
            session_id = getattr(run, "external_session_id", "") if run else ""
            if session_id and hasattr(self._claude, "send_terminal_input"):
                result = await self._claude.send_terminal_input(session_id, prompt)
                text = getattr(result, "text", "") if result is not None else ""
                if text:
                    return ControllerResponse(text)
                return ControllerResponse("已发给当前 DeepSeek 开发工程师。")

        return None

    async def _abort_active_execution(self, convo: object) -> None:
        task_id = getattr(convo, "active_codex_task_id", None)
        if task_id:
            try:
                task = self._service.get_task(task_id)
                if getattr(task, "codex_thread_id", None) and getattr(
                    task, "active_turn_id", None
                ):
                    try:
                        await self._backend.interrupt_turn(
                            task.codex_thread_id,
                            task.active_turn_id,
                        )
                    except Exception as exc:
                        logger.warning("interrupt_turn failed: %s", exc)
                self._service.abort_task(task_id)
            except Exception:
                logger.debug("Failed to abort active Codex task", exc_info=True)

        run_id = getattr(convo, "active_claude_run_id", None)
        if run_id:
            if self._claude is not None:
                try:
                    self._claude.interrupt()
                except Exception as exc:
                    logger.warning("Claude interrupt failed: %s", exc)
            try:
                self._ledger.update_agent_run_status(run_id, "aborted")
            except Exception:
                logger.debug("Failed to mark Claude run aborted", exc_info=True)

    async def _handle_workspace_busy(
        self,
        decision: RouteDecision,
        active: object | None,
        blocking_task_id: int | None,
        blocking_run_id: int | None,
        correlation_id: str,
        *,
        original_text: str = "",
        agent_label: str = "现场",
    ) -> ControllerResponse:
        """Handle workspace busy: emit events and return user choice buttons."""
        conv_id = active.id if active else 0
        workspace_alias = (
            str(getattr(active, "workspace_alias", "") or self._default_workspace)
            if active else self._default_workspace
        )

        # Store the original message text so callbacks can carry it.
        if original_text and self._ledger is not None and conv_id:
            self._ledger.update_conversation_summary(
                conv_id,
                trim_to_budget(
                    f"[工作区忙待处理] {original_text[:300]}",
                    500,
                ),
            )

        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.WORKSPACE_BUSY_DETECTED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id=f"workspace-{workspace_alias}",
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={
                "blocking_task_id": blocking_task_id,
                "blocking_conversation_id": conv_id,
                "blocking_run_id": blocking_run_id,
                "blocking_state": decision.conversation_state,
                "requested_route": decision.route,
                "original_text_preview": safe_text_preview(original_text),
                "original_text_length": len(original_text),
            },
            occurred_at=now_iso(),
            conversation_id=conv_id if conv_id else None,
        ))
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_REQUESTED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id=f"workspace-{workspace_alias}",
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.USER,
            payload={
                "blocking_task_id": blocking_task_id,
                "choices": ["append", "interrupt", "queue", "new_session", "cancel"],
                "original_text_preview": safe_text_preview(original_text),
                "original_text_length": len(original_text),
                "agent_label": agent_label,
            },
            occurred_at=now_iso(),
            conversation_id=conv_id if conv_id else None,
        ))
        buttons = build_workspace_busy_buttons(conv_id, agent_label=agent_label)
        return ControllerResponse(
            "当前工作区正在执行，新的话不会丢。\n\n"
            f"你刚发的新话可以这样处理：\n"
            f"- 发给当前 {agent_label}：把这句话送进当前正在跑的现场\n"
            "- 打断并执行这句：停止当前任务，立刻改做最新输入\n"
            "- 排队稍后：不影响当前任务，结束后再执行\n"
            "- 新开隔离现场：另开现场，不混入当前上下文",
            buttons=buttons,
        )

    async def handle_new_conversation(
        self, command: NewConversationCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        # Archive any active conversation
        old = self._ledger.get_active_conversation(chat_id)
        if old is not None:
            self._ledger.archive_conversation(old.id)

        title = command.title if command.title else "新工作台"
        convo = self._ledger.create_conversation(
            chat_id=chat_id,
            user_id=user_id,
            title=title,
            mode=self._default_mode,
            workspace_alias=self._default_workspace,
        )
        mode_label = MODE_LABELS.get(self._default_mode, self._default_mode)
        workspaces = list(self._service._workspaces.values())
        buttons: list[list[dict[str, str]]] = [
            [
                {"text": "查看状态", "callback_data": encode_conversation_callback(convo.id, CONTINUE)},
                {"text": "切换模式", "callback_data": encode_conversation_callback(convo.id, NEW_CONVO)},
            ],
        ]
        # Add workspace quick-switch buttons
        ws_buttons = self._workspace_selection_buttons(
            workspaces, active_alias=convo.workspace_alias
        )
        buttons.extend(ws_buttons)
        return ControllerResponse(
            f"新工作台已创建：「{convo.title}」\n"
            f"模式：{mode_label}\n"
            f"工作区：{convo.workspace_alias}\n\n"
            f"直接发消息继续这个工作台，或用 /codex /claude /auto 切换执行模式。\n\n"
            f"💡 切换工作区：点击下方按钮",
            buttons=buttons,
        )

    async def handle_codex_direct(
        self, command: CodexDirectCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Codex Direct Mode — Codex-only work, independent from /auto."""
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            title = default_title(command.prompt)
            active = self._ledger.create_conversation(
                chat_id=chat_id,
                user_id=user_id,
                title=title,
                mode=ConversationMode.CODEX_DIRECT.value,
                workspace_alias=self._default_workspace,
            )

        busy = await self._direct_command_busy_response(
            active=active,
            original_text=f"/codex {command.prompt}".strip(),
            agent_label="GPT 开发工程师",
        )
        if busy is not None:
            return busy

        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=command.prompt,
            telegram_chat_id=chat_id,
            purpose="codex_direct",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role="implementation",
            hidden_task_id=task.id,
            prompt_packet_summary=command.prompt[:200],
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
        try:
            thread_id = await self._start_codex_turn_for_conversation(
                active=active,
                task=task,
                workspace_path=workspace_path,
                prompt=command.prompt,
            )
            # Persist thread reference so /sessions shows this as resumable.
            self._ledger.update_agent_run_status(
                agent_run.id, "running", external_session_id=thread_id,
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"GPT 开发工程师独立处理：{command.prompt[:200]}",
                ContextBudget().conversation_summary_tokens,
            ),
        )
        # When interaction renderer is active, EventBridge forwards deltas and
        # terminal events — don't send a duplicate static response.
        if self._interaction_renderer is not None:
            # Emit run_started so typing indicator begins
            from wlcodex.interaction.events import InteractionEvent
            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_started",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                )
            )
            return ControllerResponse("", already_rendered=True)

        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(active.id, CONTINUE)},
        ]]

        return ControllerResponse(
            "这次只交给 GPT 开发工程师独立处理，不会调用其他开发工程师或进入 /auto 编排。",
            buttons=buttons,
        )

    async def handle_claude_direct(
        self, command: ClaudeDirectCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """DeepSeek direct mode: developer-only implementation.

        No automatic diagnosis or audit. Offers an audit action after completion.
        """
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        if self._claude is None or not getattr(self._claude, "enabled", False):
            return ControllerResponse(
                "DeepSeek 开发工程师未启用。请在配置中设置 claude.enabled = true 后重试。"
            )

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            title = default_title(command.prompt)
            active = self._ledger.create_conversation(
                chat_id=chat_id,
                user_id=user_id,
                title=title,
                mode=ConversationMode.CLAUDE_DIRECT.value,
                workspace_alias=self._default_workspace,
            )

        busy = await self._direct_command_busy_response(
            active=active,
            original_text=f"/claude {command.prompt}".strip(),
            agent_label="DeepSeek 开发工程师",
        )
        if busy is not None:
            return busy

        return await self._handle_claude_direct_impl(command, active, ctx)

    async def _handle_claude_direct_impl(
        self,
        command: ClaudeDirectCommand,
        active: object,
        ctx: dict[str, Any] | None = None,
    ) -> ControllerResponse:
        """DeepSeek direct run: no pre-analysis, no automatic audit.

        Creates a developer agent run and task, then launches the backend as a
        background asyncio task so the controller can return immediately.
        The response includes an audit button so the user can explicitly request
        verification after development completes.
        """
        chat_id = ctx.get("chat_id", 0) if ctx else 0
        budget = ContextBudget()

        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=command.prompt,
            telegram_chat_id=chat_id,
            purpose="claude_direct",
        )

        claude_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="claude",
            role="implementation",
            hidden_task_id=task.id,
            prompt_packet_summary=command.prompt[:200],
        )
        self._ledger.update_agent_run_status(claude_run.id, "running")
        self._ledger.set_conversation_active_claude_run(active.id, claude_run.id)

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"DeepSeek 开发工程师直接实施：{command.prompt[:200]}",
                budget.conversation_summary_tokens,
            ),
        )
        # Emit user message received event
        cid = self._new_correlation_id()
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.TELEGRAM,
            actor="user",
            visibility=Visibility.USER,
            payload={
                "text_preview": safe_text_preview(command.prompt),
                "text_length": len(command.prompt),
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)

        # Launch Claude as a background task — must actually execute,
        # not just record a database row.
        bg_task = asyncio.create_task(
            self._run_claude_direct_async(
                agent_run_id=claude_run.id,
                task_id=task.id,
                conversation_id=active.id,
                prompt=command.prompt,
                workspace_path=workspace_path,
                chat_id=chat_id,
                correlation_id=cid,
            ),
            name=f"claude-direct-{claude_run.id}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        # Streaming path: delegate rendering to interaction renderer
        if self._interaction_renderer is not None:
            from wlcodex.interaction.events import InteractionEvent
            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_started",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                )
            )
            return ControllerResponse("", already_rendered=True)

        # Static response: mode label + verification affordance
        buttons: list[list[dict[str, str]]] = [
            [
                {
                    "text": "让审计工程师验收",
                    "callback_data": encode_conversation_callback(active.id, VERIFY),
                },
            ],
            [
                {
                    "text": "查看状态",
                    "callback_data": encode_conversation_callback(active.id, CONTINUE),
                },
            ],
        ]
        return ControllerResponse(
            "这次直接交给 DeepSeek 开发工程师实施。完成后你可以点“让审计工程师验收”。",
            buttons=buttons,
        )

    async def _run_claude_direct_async(
        self,
        *,
        agent_run_id: int,
        task_id: int,
        conversation_id: int,
        prompt: str,
        workspace_path: str,
        chat_id: int,
        correlation_id: str = "",
    ) -> None:
        """Background coroutine that actually invokes Claude as a subprocess.

        Called via asyncio.create_task so the controller can return
        immediately.  Updates the agent run status on completion/failure.
        """
        from wlcodex.agent_backend import AgentRequest

        try:
            try:
                self._ledger.set_task_status(
                    task_id,
                    TaskStatus.RUNNING,
                    phase="claude_direct",
                )
                self._ledger.add_event(
                    task_id,
                    "claude_direct_started",
                    {"agent_run_id": agent_run_id},
                )
            except Exception:
                logger.debug("Unable to mark Claude direct task running", exc_info=True)

            try:
                conversation = self._ledger.get_conversation(conversation_id)
                resume_session_id = conversation.claude_session_id
            except Exception:
                resume_session_id = ""
            extra = (
                {"resume_session_id": resume_session_id}
                if resume_session_id
                else {}
            )
            request = AgentRequest(
                prompt=prompt,
                workspace_path=workspace_path,
                extra=extra,
            )
            result = None
            had_error = False
            error_text = ""
            claude_session_id: str = ""
            stream_chunks: list[str] = []
            if self._interaction_renderer is not None:
                stream = self._claude.send_streaming(request)
                if hasattr(stream, "__aiter__"):
                    async for stream_event in stream:
                        event_type = getattr(stream_event, "event_type", "")
                        delta = getattr(stream_event, "delta", "")
                        if delta and event_type != "error":
                            stream_chunks.append(str(delta))
                        # Capture latest non-empty session_id for persistence.
                        sid = getattr(stream_event, "session_id", "")
                        if sid:
                            claude_session_id = sid
                        if event_type == "error":
                            had_error = True
                            error_text = delta or "Claude streaming returned error"
                        from wlcodex.interaction.events import InteractionEvent
                        await self._interaction_renderer.handle(
                            InteractionEvent(
                                event_type="claude_delta",
                                chat_id=chat_id,
                                task_id=task_id,
                                conversation_id=conversation_id,
                                metadata={"delta": delta, "event_type": event_type},
                            )
                        )
                else:
                    result = await stream
            else:
                # Non-streaming path: call send() and capture result
                result = await self._claude.send(request)

            # Capture session_id from non-streaming result.
            if not claude_session_id and result is not None:
                claude_session_id = getattr(result, "session_id", "") or ""

            if had_error:
                try:
                    self._service.fail_task(task_id, error_text or "Claude direct failed")
                except Exception:
                    logger.debug("Unable to mark Claude direct task failed", exc_info=True)
                self._ledger.update_agent_run_status(
                    agent_run_id,
                    "failed",
                    completion_summary=error_text,
                    external_session_id=claude_session_id or None,
                )
                if claude_session_id:
                    self._ledger.set_conversation_claude_session(
                        conversation_id, claude_session_id
                    )
                # Update staged-auto orchestration run if applicable
                self._transition_auto_claude_completed(
                    conversation_id, agent_status="failed",
                    completion_summary=error_text,
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.RUN_FAILED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(agent_run_id),
                    correlation_id=correlation_id,
                    source=EventSource.CONTROLLER,
                    actor="claude",
                    visibility=Visibility.OPERATOR,
                    payload={"status": "failed", "reason": error_text[:500],
                             "task_id": task_id, "session_id": claude_session_id},
                    occurred_at=now_iso(),
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                ))
                return

            # On success: mark agent run as done
            completion_summary = ""
            if result is not None:
                try:
                    completion_summary = result.text
                except Exception:
                    completion_summary = ""
            if not completion_summary and stream_chunks:
                completion_summary = "".join(stream_chunks).strip()
            self._ledger.update_agent_run_status(
                agent_run_id,
                "done",
            completion_summary=completion_summary or "DeepSeek 开发工程师执行完成",
                external_session_id=claude_session_id or None,
            )
            if claude_session_id:
                self._ledger.set_conversation_claude_session(
                    conversation_id, claude_session_id
                )
            # Update staged-auto orchestration run if applicable
            self._transition_auto_claude_completed(
                conversation_id, agent_status="done",
                completion_summary=completion_summary or "DeepSeek 开发工程师执行完成",
            )
            try:
                self._ledger.set_task_status(
                    task_id,
                    TaskStatus.DONE,
                    phase="claude_direct",
                    summary=completion_summary or "DeepSeek 开发工程师执行完成",
                )
                self._ledger.add_event(
                    task_id,
                    "claude_direct_completed",
                    {"agent_run_id": agent_run_id},
                )
            except Exception:
                logger.debug("Unable to mark Claude direct task done", exc_info=True)

            # Emit run.completed
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_COMPLETED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="claude",
                visibility=Visibility.OPERATOR,
                payload={"status": "done", "task_id": task_id},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
            ))

        except Exception as exc:
            logger.exception("Claude direct background task failed")
            try:
                self._service.fail_task(task_id, str(exc))
            except Exception:
                logger.debug("Failed to mark Claude direct task failed", exc_info=True)
            self._ledger.update_agent_run_status(
                agent_run_id,
                "failed",
                completion_summary=str(exc),
            )
            self._transition_auto_claude_completed(
                conversation_id, agent_status="failed",
                completion_summary=str(exc),
            )
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_FAILED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="claude",
                visibility=Visibility.OPERATOR,
                payload={"status": "failed", "reason": str(exc)[:500],
                         "task_id": task_id},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
            ))

    async def handle_auto_mode(
        self, command: AutoModeCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Stage-gated /auto: starts in collecting_context, never starts Claude
        automatically. User must explicitly advance through button clicks."""
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            title = default_title(command.prompt)
            active = self._ledger.create_conversation(
                chat_id=chat_id,
                user_id=user_id,
                title=title,
                mode=ConversationMode.CHIEF_ENGINEER.value,
                workspace_alias=self._default_workspace,
            )

        existing_auto_run = self._latest_active_auto_run(active.id)
        if existing_auto_run is not None:
            buttons = build_auto_stage_buttons(
                active.id,
                existing_auto_run.current_step,
                last_codex_analysis=existing_auto_run.last_codex_analysis or "",
                codex_implementer_enabled=self._codex_implementer_enabled(),
            )
            return ControllerResponse(
                "已有 /auto 工作流正在进行："
                f"{auto_stage_label(existing_auto_run.current_step)}",
                buttons=buttons,
            )

        # Check if workspace is busy
        busy = await self._direct_command_busy_response(
            active=active,
            original_text=f"/auto {command.prompt}".strip(),
            agent_label="Auto",
        )
        if busy is not None:
            return busy

        cid = self._new_correlation_id()
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.TELEGRAM,
            actor="user",
            visibility=Visibility.USER,
            payload={
                "text_preview": safe_text_preview(command.prompt),
                "text_length": len(command.prompt),
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # Emit auto.stage.transitioned event
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_STARTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.CONTROLLER,
            actor="user",
            visibility=Visibility.OPERATOR,
            payload={
                "goal": command.prompt,
                "stage": AUTO_COLLECTING_CONTEXT,
                "mode": "staged_auto",
                "chat_id": chat_id,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # Create orchestration_run with collecting_context stage
        orch_run = self._ledger.create_orchestration_run(
            conversation_id=active.id,
            goal=command.prompt,
        )
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="needs_user",
            current_step=AUTO_ROUTE_SELECT,
        )
        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(f"[Auto] {command.prompt[:200]}", ContextBudget().conversation_summary_tokens),
        )
        buttons = build_auto_stage_buttons(
            active.id,
            AUTO_ROUTE_SELECT,
            codex_implementer_enabled=self._codex_implementer_enabled(),
        )
        return ControllerResponse(
            "请选择执行路线：\n\n"
            "诊断：先查问题、找根因、收集证据，不直接改代码。\n"
            "设计：先出方案和交接提示，不直接改代码。\n"
            "GPT 执行：直接交给 GPT 开发工程师处理轻量任务。\n"
            "DeepSeek 执行：直接交给 DeepSeek 开发工程师处理轻量任务。",
            buttons=buttons,
        )

    # --- Staged-auto callback handlers ---

    async def _handle_auto_route_direct(
        self, callback: ConversationCallback, *, target: str
    ) -> ControllerResponse:
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step != AUTO_ROUTE_SELECT:
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，不能选择执行路线。"
            )
        if target == "codex" and not self._codex_implementer_enabled():
            return ControllerResponse(
                "GPT 开发工程师未启用。请选择其他路线，或先启用 GPT 开发工程师。",
                buttons=build_auto_stage_buttons(
                    callback.conversation_id,
                    AUTO_ROUTE_SELECT,
                    codex_implementer_enabled=self._codex_implementer_enabled(),
                ),
            )
        if target == "claude" and (
            self._claude is None or not getattr(self._claude, "enabled", False)
        ):
            return ControllerResponse(
                "DeepSeek 开发工程师未启用。请选择其他路线，或先启用 DeepSeek 开发工程师。",
                buttons=build_auto_stage_buttons(
                    callback.conversation_id,
                    AUTO_ROUTE_SELECT,
                    codex_implementer_enabled=self._codex_implementer_enabled(),
                ),
            )
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="needs_user",
            current_step=AUTO_DRAFT_READY,
            last_codex_analysis=orch_run.goal,
        )
        if target == "codex":
            return await self._handle_auto_send_to_codex(callback)
        return await self._handle_auto_send_to_claude(callback)

    async def _handle_auto_route_analysis(
        self, callback: ConversationCallback, *, route_kind: str
    ) -> ControllerResponse:
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step != AUTO_ROUTE_SELECT:
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，不能选择执行路线。"
            )

        is_diagnosis = route_kind == "bug"
        first_role = "investigator" if is_diagnosis else "architect"
        first_model_profile = (
            self._investigator_model_profile
            if is_diagnosis
            else self._architect_model_profile
        )
        first_output_schema = "diagnosis_report" if is_diagnosis else "architecture_plan"
        first_role_display = "诊断工程师" if is_diagnosis else "架构工程师"
        reason = "用户选择诊断路线" if is_diagnosis else "用户选择设计路线"

        chat_id = convo.chat_id
        goal = orch_run.goal
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=goal,
            telegram_chat_id=chat_id,
            purpose="auto_analysis",
        )
        packet = build_auto_context_packet(
            user_goal=goal,
            conversation_summary=trim_to_budget(
                convo.conversation_summary,
                ContextBudget().conversation_summary_tokens,
            ),
            workspace=convo.workspace_alias,
            budget=ContextBudget(),
        )
        codex_prompt = packet.render()
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=ROLE_AUTO_ANALYSIS,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        team_run = None
        first_job = None
        if self._adaptive_team_enabled and hasattr(self._ledger, "create_team_run"):
            team_run = self._ledger.create_team_run(
                conversation_id=convo.id,
                orchestration_run_id=orch_run.id,
                goal=goal,
                route="staged_auto",
                risk_level="medium",
            )
            first_job = self._ledger.create_team_agent_job(
                team_run_id=team_run.id,
                role=first_role,
                model_profile=first_model_profile,
                status="running",
                agent_run_id=agent_run.id,
            )
            self._ledger.record_team_assignment(
                team_run_id=team_run.id,
                role=first_role,
                model_profile=first_model_profile,
                selected_by="user",
            )
            self._ledger.record_team_artifact(
                team_run_id=team_run.id,
                agent_job_id=first_job.id,
                artifact_type="routing_decision",
                summary=f"{first_role_display}路线：{reason}",
                payload={
                    "route_kind": route_kind,
                    "first_role": first_role,
                    "reason": reason,
                    "matched_signals": ["user_button"],
                },
            )
            context_packet = self._build_team_context_packet_for_job(
                team_run=team_run,
                agent_job=first_job,
                role=first_role,
                model_profile=first_model_profile,
                resume_state="staged auto route selected by user",
                output_schema=first_output_schema,
            )
            codex_prompt = context_packet.render()
            self._ledger.record_team_context_packet(
                team_run_id=team_run.id,
                agent_job_id=first_job.id,
                packet_json=context_packet.as_json(),
                prompt_text=codex_prompt,
                prompt_tokens=approx_tokens(codex_prompt),
            )

        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_COLLECTING_CONTEXT,
        )
        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)
        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=codex_prompt,
                interaction_mode="general",
            )
        except Exception as exc:
            self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            self._ledger.update_orchestration_run(
                orch_run.id,
                status="failed",
                last_codex_analysis=str(exc),
            )
            if team_run is not None and hasattr(self._ledger, "update_team_run_status"):
                self._ledger.update_team_run_status(team_run.id, "failed")
            if first_job is not None and hasattr(self._ledger, "update_team_agent_job_status"):
                self._ledger.update_team_agent_job_status(first_job.id, "failed")
            return ControllerResponse(classify_user_error(exc))

        return ControllerResponse(
            f"{first_role_display}开始分析。完成后会显示「生成最终方案」。\n\n"
            "当前不会启动开发工程师。",
            buttons=[[
                {
                    "text": "查看状态",
                    "callback_data": encode_conversation_callback(convo.id, AUTO_VIEW_STATUS),
                },
                {
                    "text": "取消",
                    "callback_data": encode_conversation_callback(convo.id, AUTO_CANCEL),
                },
            ]],
        )

    def _latest_active_auto_run(self, conversation_id: int) -> object | None:
        """Find the latest orchestration run for this conversation that is
        in an active auto stage."""
        if self._ledger is None:
            return None
        return self._ledger.get_latest_active_auto_run(conversation_id)

    def _auto_run_has_team(self, orch_run: object | None) -> bool:
        if (
            orch_run is None
            or self._ledger is None
            or not hasattr(self._ledger, "get_team_run_for_orchestration")
        ):
            return False
        return self._ledger.get_team_run_for_orchestration(orch_run.id) is not None

    def _auto_stage_buttons(
        self,
        conversation_id: int,
        stage: str,
        *,
        orch_run: object | None = None,
        last_codex_analysis: str = "",
    ) -> list[list[dict[str, str]]]:
        if orch_run is None:
            orch_run = self._latest_active_auto_run(conversation_id)
        return build_auto_stage_buttons(
            conversation_id,
            stage,
            last_codex_analysis=last_codex_analysis,
            codex_implementer_enabled=self._codex_implementer_enabled(),
            include_team_controls=self._auto_run_has_team(orch_run),
        )

    def _latest_team_run(self, conversation_id: int) -> object | None:
        if self._ledger is None or not hasattr(
            self._ledger,
            "get_team_run_for_orchestration",
        ):
            return None
        active_auto_run = self._latest_active_auto_run(conversation_id)
        if active_auto_run is not None:
            return self._ledger.get_team_run_for_orchestration(active_auto_run.id)
        for orch_run in self._ledger.list_orchestration_runs(conversation_id, limit=20):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
            if team_run is not None:
                return team_run
        return None

    def _team_status_buttons(self, conversation_id: int) -> list[list[dict[str, str]]]:
        orch_run = self._latest_active_auto_run(conversation_id)
        if orch_run is None:
            return [[{
                "text": "查看状态",
                "callback_data": encode_conversation_callback(
                    conversation_id,
                    AUTO_VIEW_STATUS,
                ),
            }, {
                "text": "团队状态",
                "callback_data": encode_conversation_callback(
                    conversation_id,
                    TEAM_VIEW_STATUS,
                ),
            }, {
                "text": "团队证据",
                "callback_data": encode_conversation_callback(
                    conversation_id,
                    TEAM_VIEW_ARTIFACTS,
                ),
            }]]
        return self._auto_stage_buttons(
            conversation_id,
            orch_run.current_step,
            orch_run=orch_run,
            last_codex_analysis=orch_run.last_codex_analysis or "",
        )

    async def _handle_team_view_status(
        self,
        callback: ConversationCallback,
    ) -> ControllerResponse:
        team_run = self._latest_team_run(callback.conversation_id)
        if team_run is None or self._ledger is None:
            return ControllerResponse("暂无团队状态。")
        roles = [
            (job.role, job.model_profile, job.status)
            for job in self._ledger.list_team_agent_jobs(team_run.id)
        ]
        artifacts = [
            f"{artifact.artifact_type}: {artifact.summary}"
            for artifact in self._ledger.list_team_artifacts(team_run.id)
        ]
        return ControllerResponse(
            render_team_status_summary(
                team_run.goal,
                "开发团队",
                roles,
                artifacts[-4:],
            ),
            buttons=self._team_status_buttons(callback.conversation_id),
        )

    async def _handle_team_view_artifacts(
        self,
        callback: ConversationCallback,
    ) -> ControllerResponse:
        team_run = self._latest_team_run(callback.conversation_id)
        if team_run is None or self._ledger is None:
            return ControllerResponse("暂无团队证据。")
        artifacts = self._ledger.list_team_artifacts(team_run.id)
        if not artifacts:
            return ControllerResponse(
                "团队证据：\n暂无团队证据。",
                buttons=self._team_status_buttons(callback.conversation_id),
            )
        lines = ["团队证据："]
        for artifact in artifacts[-8:]:
            lines.append(
                "- "
                + render_team_artifact_summary(
                    f"{artifact.artifact_type}: {artifact.summary[:160]}"
                )
            )
        return ControllerResponse(
            "\n".join(lines),
            buttons=self._team_status_buttons(callback.conversation_id),
        )

    def _collect_task_evidence(self, task_id: int | None) -> tuple[list[str], str]:
        if not task_id:
            return [], ""
        workspace_path: str | None = None
        try:
            task = self._service.get_task(task_id)
            workspace_path = str(self._service.get_workspace(task.workspace_alias).path)
        except Exception:
            workspace_path = None

        changed_files: list[str] = []
        diff_summary = ""
        try:
            files_result = self._inspector.files(task_id)
            if files_result and files_result.body:
                changed_files = _changed_files_from_inspection_body(files_result.body)
        except Exception:
            changed_files = []
        try:
            diff_result = self._inspector.diff(task_id, workspace_path)
            if (
                diff_result
                and diff_result.body
                and _diff_body_has_evidence(diff_result.body)
            ):
                diff_summary = diff_result.body[:1500]
        except Exception:
            diff_summary = ""
        return changed_files[:20], diff_summary[:1500]

    def _hidden_task_id_for_agent_run(self, agent_run_id: int | None) -> int | None:
        if self._ledger is None or agent_run_id is None:
            return None
        try:
            agent_run = self._ledger.get_agent_run(agent_run_id)
        except Exception:
            return None
        hidden_task_id = getattr(agent_run, "hidden_task_id", None)
        return int(hidden_task_id) if hidden_task_id is not None else None

    def _implementation_evidence_task_id(
        self,
        convo: object,
        orch_run: object,
    ) -> int | None:
        if self._ledger is None:
            return None
        if hasattr(self._ledger, "get_team_run_for_orchestration"):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
            if team_run is not None and hasattr(self._ledger, "list_team_agent_jobs"):
                for job in reversed(self._ledger.list_team_agent_jobs(team_run.id)):
                    if job.role != "implementer" or job.agent_run_id is None:
                        continue
                    task_id = self._hidden_task_id_for_agent_run(job.agent_run_id)
                    if task_id is not None:
                        return task_id

        active_claude_run_id = getattr(convo, "active_claude_run_id", None)
        task_id = self._hidden_task_id_for_agent_run(active_claude_run_id)
        if task_id is not None:
            return task_id

        try:
            if hasattr(self._ledger, "list_recent_agent_runs"):
                agent_runs = self._ledger.list_recent_agent_runs(convo.id, limit=20)
            else:
                agent_runs = list(reversed(self._ledger.list_agent_runs(convo.id, limit=20)))
            for agent_run in agent_runs:
                if agent_run.role not in (ROLE_AUTO_IMPLEMENTATION, ROLE_AUTO_REPAIR):
                    continue
                if agent_run.hidden_task_id is not None:
                    return int(agent_run.hidden_task_id)
        except Exception:
            pass

        active_codex_task_id = getattr(convo, "active_codex_task_id", None)
        return int(active_codex_task_id) if active_codex_task_id is not None else None

    def _auto_digest_usage_recorder(
        self,
        *,
        conversation_id: int,
        orchestration_run_id: int,
        task_id: int | None = None,
        agent_run_id: int | None = None,
    ):
        def record(usage: DeepSeekDigestUsage) -> None:
            self._record_auto_digest_usage(
                conversation_id=conversation_id,
                orchestration_run_id=orchestration_run_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
                usage=usage,
            )

        return record

    def _record_auto_digest_usage(
        self,
        *,
        conversation_id: int,
        orchestration_run_id: int,
        usage: DeepSeekDigestUsage,
        task_id: int | None = None,
        agent_run_id: int | None = None,
    ) -> None:
        if self._ledger is None or not hasattr(self._ledger, "record_usage_event"):
            return
        metadata = {
            "digest_kind": usage.digest_kind,
            "source_chars": usage.source_chars,
            "prompt_chars": usage.prompt_chars,
            "response_chars": usage.response_chars,
            "digest_chars": usage.digest_chars,
            "failure_reason": usage.failure_reason,
        }
        try:
            self._ledger.record_usage_event(
                agent="deepseek",
                role="auto_digest",
                phase=usage.digest_kind,
                request_kind="telegram_digest",
                model=usage.model,
                source="exact" if usage.total_tokens else "derived",
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                total_tokens=usage.total_tokens,
                latency_ms=usage.latency_ms,
                status=usage.status,
                conversation_id=conversation_id,
                orchestration_run_id=orchestration_run_id,
                agent_run_id=agent_run_id,
                task_id=task_id,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        except Exception:
            logger.debug("Failed to record DeepSeek digest token usage", exc_info=True)

    def _transition_auto_claude_completed(
        self,
        conversation_id: int,
        *,
        agent_status: str,
        completion_summary: str = "",
    ) -> None:
        """Transition staged-auto orchestration run from claude_running to
        claude_done when a Claude background task completes, and send the
        new stage buttons to Telegram."""
        if self._ledger is None:
            return
        orch_run = self._latest_active_auto_run(conversation_id)
        if orch_run is None:
            return
        if orch_run.current_step not in (AUTO_CLAUDE_RUNNING,):
            return
        new_status = "needs_user" if agent_status == "done" else "failed"
        new_step = AUTO_CLAUDE_DONE if agent_status == "done" else orch_run.current_step
        self._ledger.update_orchestration_run(
            orch_run.id,
            status=new_status,
            current_step=new_step,
            last_claude_summary=completion_summary,
        )
        test_gate_passed: bool | None = None
        internal_repair_started = False
        agent_run_id = None
        try:
            agent_run_id = self._ledger.get_conversation(
                conversation_id
            ).active_claude_run_id
        except Exception:
            agent_run_id = None
        implementation_task_id = self._hidden_task_id_for_agent_run(agent_run_id)
        if agent_status == "done" and hasattr(
            self._ledger, "get_team_run_for_orchestration"
        ):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
            if team_run is not None and hasattr(self._ledger, "list_team_agent_jobs"):
                for job in self._ledger.list_team_agent_jobs(team_run.id):
                    if job.role != "implementer" or job.status != "running":
                        continue
                    if agent_run_id is not None and job.agent_run_id != agent_run_id:
                        continue
                    if agent_run_id is None and job.agent_run_id is not None:
                        continue
                    if hasattr(self._ledger, "record_team_artifact"):
                        existing = [
                            artifact
                            for artifact in self._ledger.list_team_artifacts(team_run.id)
                            if artifact.artifact_type == "implementation_report"
                            and artifact.agent_job_id == job.id
                        ]
                        if not existing:
                            task_id = self._hidden_task_id_for_agent_run(agent_run_id)
                            changed_files, diff_summary = self._collect_task_evidence(
                                task_id
                            )
                            from wlcodex.team_artifacts import (
                                acceptance_criteria_from_artifacts,
                                command_evidence_from_task_events,
                                implementation_report_payload,
                                structured_implementation_evidence_from_text,
                                test_command_evidence,
                                test_report_payload_from_implementation,
                                validate_test_report,
                            )

                            task_events = (
                                self._ledger.list_events(task_id, limit=1000)
                                if task_id
                                else []
                            )
                            commands_run = command_evidence_from_task_events(task_events)
                            tests_attempted = test_command_evidence(commands_run)
                            structured_evidence = (
                                structured_implementation_evidence_from_text(
                                    completion_summary
                                )
                            )
                            changed_files = (
                                structured_evidence.get("changed_files")
                                or changed_files
                            )
                            diff_summary = (
                                structured_evidence.get("diff_summary")
                                or diff_summary
                            )
                            commands_run = (
                                structured_evidence.get("commands_run")
                                or commands_run
                            )
                            tests_attempted = (
                                structured_evidence.get("tests_attempted")
                                or tests_attempted
                            )

                            artifact = self._ledger.record_team_artifact(
                                team_run_id=team_run.id,
                                agent_job_id=job.id,
                                artifact_type="implementation_report",
                                summary=completion_summary[:2000]
                                or "DeepSeek 开发工程师已完成实现。",
                                payload=implementation_report_payload(
                                    summary=completion_summary
                                    or "DeepSeek 开发工程师已完成实现。",
                                    changed_files=changed_files,
                                    diff_summary=diff_summary,
                                    source_agent="claude",
                                    commands_run=commands_run,
                                    tests_attempted=tests_attempted,
                                ),
                            )
                            self._emit_team_runtime_event(
                                EventType.TEAM_ARTIFACT_RECORDED,
                                conversation_id=conversation_id,
                                orchestration_run_id=orch_run.id,
                                team_run_id=team_run.id,
                                agent_job_id=job.id,
                                agent_run_id=agent_run_id,
                                task_id=task_id,
                                payload={
                                    "artifact_id": artifact.id,
                                    "artifact_type": artifact.artifact_type,
                                },
                            )
                            tester_job = None
                            tester_model_profile = (
                                getattr(job, "model_profile", None)
                                or self._tester_model_profile
                            )
                            if hasattr(self._ledger, "create_team_agent_job"):
                                tester_job = self._ledger.create_team_agent_job(
                                    team_run_id=team_run.id,
                                    role="tester",
                                    model_profile=tester_model_profile,
                                    status="running",
                                    agent_run_id=agent_run_id,
                                )
                                if hasattr(self._ledger, "record_team_assignment"):
                                    self._ledger.record_team_assignment(
                                        team_run_id=team_run.id,
                                        role="tester",
                                        model_profile=tester_model_profile,
                                        selected_by="follow_implementer",
                                    )
                                self._emit_team_runtime_event(
                                    EventType.TEAM_AGENT_JOB_STARTED,
                                    conversation_id=conversation_id,
                                    orchestration_run_id=orch_run.id,
                                    team_run_id=team_run.id,
                                    agent_job_id=tester_job.id,
                                    agent_run_id=agent_run_id,
                                    task_id=task_id,
                                    payload={
                                        "role": "tester",
                                        "model_profile": tester_model_profile,
                                        "follow_role": "implementer",
                                    },
                                )
                            test_existing = [
                                artifact
                                for artifact in self._ledger.list_team_artifacts(team_run.id)
                                if artifact.artifact_type == "test_report"
                                and tester_job is not None
                                and artifact.agent_job_id == tester_job.id
                            ]
                            if tester_job is not None and not test_existing:
                                acceptance_criteria = acceptance_criteria_from_artifacts(
                                    self._ledger.list_team_artifacts(team_run.id)
                                )
                                test_payload = test_report_payload_from_implementation(
                                    summary="测试工程师已收集测试结果。",
                                    implementation_artifact_id=artifact.id,
                                    commands_run=tests_attempted,
                                    acceptance_criteria=acceptance_criteria,
                                )
                                test_artifact = self._ledger.record_team_artifact(
                                    team_run_id=team_run.id,
                                    agent_job_id=tester_job.id,
                                    artifact_type="test_report",
                                    summary="测试工程师已收集测试结果。",
                                    payload=test_payload,
                                )
                                self._emit_team_runtime_event(
                                    EventType.TEAM_ARTIFACT_RECORDED,
                                    conversation_id=conversation_id,
                                    orchestration_run_id=orch_run.id,
                                    team_run_id=team_run.id,
                                    agent_job_id=tester_job.id,
                                    agent_run_id=agent_run_id,
                                    task_id=task_id,
                                    payload={
                                        "artifact_id": test_artifact.id,
                                        "artifact_type": test_artifact.artifact_type,
                                    },
                                )
                                test_gate_passed = validate_test_report(test_payload).passed
                                tester_status = "done" if test_gate_passed else "failed"
                                if hasattr(
                                    self._ledger, "update_team_agent_job_status"
                                ):
                                    self._ledger.update_team_agent_job_status(
                                        tester_job.id, tester_status
                                    )
                                self._emit_team_runtime_event(
                                    EventType.TEAM_AGENT_JOB_COMPLETED
                                    if test_gate_passed
                                    else EventType.TEAM_AGENT_JOB_FAILED,
                                    conversation_id=conversation_id,
                                    orchestration_run_id=orch_run.id,
                                    team_run_id=team_run.id,
                                    agent_job_id=tester_job.id,
                                    agent_run_id=agent_run_id,
                                    task_id=task_id,
                                    payload={
                                        "role": "tester",
                                        "status": tester_status,
                                    },
                                )
                    if hasattr(self._ledger, "update_team_agent_job_status"):
                        self._ledger.update_team_agent_job_status(job.id, "done")
                        self._emit_team_runtime_event(
                            EventType.TEAM_AGENT_JOB_COMPLETED,
                            conversation_id=conversation_id,
                            orchestration_run_id=orch_run.id,
                            team_run_id=team_run.id,
                            agent_job_id=job.id,
                            agent_run_id=agent_run_id,
                            payload={
                                "role": "implementer",
                                "status": "done",
                            },
                        )
                    break
            if test_gate_passed is False:
                attempt_count = self._tester_attempt_count(team_run)
                new_step = AUTO_RETRY_READY
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="needs_user",
                    current_step=new_step,
                    last_claude_summary=completion_summary,
                    last_verification_result=self._internal_test_failure_text(
                        attempt_count
                    ),
                )
                if attempt_count < MAX_INTERNAL_TEST_ATTEMPTS:
                    internal_task = asyncio.create_task(
                        self._run_internal_test_repair(
                            conversation_id=conversation_id,
                            attempt_count=attempt_count,
                        ),
                        name=f"auto-internal-test-repair-{conversation_id}-{attempt_count}",
                    )
                    self._background_tasks.add(internal_task)
                    internal_task.add_done_callback(self._background_tasks.discard)
                    internal_repair_started = True
            elif test_gate_passed is True:
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="needs_user",
                    current_step=AUTO_CLAUDE_DONE,
                    last_claude_summary=completion_summary,
                    last_verification_result="开发完成，测试通过。",
                )

        # Send stage buttons to Telegram
        if (
            self._interaction_renderer is not None
            and agent_status == "done"
            and not internal_repair_started
        ):
            try:
                convo = self._ledger.get_conversation(conversation_id)
                chat_id = convo.chat_id
            except Exception:
                return
            buttons = self._auto_stage_buttons(
                conversation_id, new_step,
                orch_run=orch_run,
                last_codex_analysis=orch_run.last_codex_analysis or "",
            )
            from wlcodex.interaction.events import InteractionEvent

            async def send_stage_update() -> None:
                digest_kind = (
                    "diagnosis" if new_step == AUTO_RETRY_READY else "implementation"
                )
                digest = await render_auto_draft_digest_with_llm(
                    completion_summary or "结论：DeepSeek 开发工程师已完成实现。",
                    digest_kind=digest_kind,
                    usage_recorder=self._auto_digest_usage_recorder(
                        conversation_id=conversation_id,
                        orchestration_run_id=orch_run.id,
                        task_id=implementation_task_id,
                        agent_run_id=agent_run_id,
                    ),
                )
                if new_step == AUTO_RETRY_READY:
                    attempt_count = (
                        self._tester_attempt_count_for_conversation(conversation_id) or 1
                    )
                    text = (
                        "测试未通过。\n\n"
                        f"{digest}\n\n"
                        f"{self._internal_test_failure_text(attempt_count)}"
                    )
                else:
                    text = f"开发完成，测试通过。\n\n{digest}\n\n请选择下一步："
                await self._interaction_renderer.handle(
                    InteractionEvent(
                        event_type="run_completed",
                        chat_id=chat_id,
                        conversation_id=conversation_id,
                        text=text,
                        buttons=buttons,
                    )
                )

            asyncio.create_task(send_stage_update())

    async def _run_internal_test_repair(
        self, *, conversation_id: int, attempt_count: int
    ) -> None:
        try:
            await self._handle_auto_send_repair_to_claude(
                ConversationCallback(conversation_id, AUTO_SEND_REPAIR_TO_CLAUDE)
            )
        except Exception as exc:
            try:
                orch_run = self._latest_active_auto_run(conversation_id)
                if orch_run is not None:
                    self._ledger.update_orchestration_run(
                        orch_run.id,
                        status="needs_user",
                        current_step=AUTO_RETRY_READY,
                        last_verification_result=(
                            self._internal_test_failure_text(attempt_count)
                            + f"\n内部返工启动失败：{classify_user_error(exc)}"
                        ),
                    )
            except Exception:
                logger.exception("Failed to record internal test repair failure")
            logger.warning("Internal test repair failed: %s", exc)

    def _tester_attempt_count(self, team_run: object) -> int:
        if not hasattr(self._ledger, "list_team_agent_jobs"):
            return 0
        return sum(
            1
            for job in self._ledger.list_team_agent_jobs(team_run.id)
            if job.role == "tester"
        )

    def _tester_attempt_count_for_conversation(self, conversation_id: int) -> int:
        try:
            orch_run = self._latest_active_auto_run(conversation_id)
            if orch_run is None or not hasattr(
                self._ledger, "get_team_run_for_orchestration"
            ):
                return 0
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
            return self._tester_attempt_count(team_run) if team_run is not None else 0
        except Exception:
            return 0

    def _internal_test_failure_text(self, attempt_count: int) -> str:
        attempt = max(1, attempt_count)
        if attempt >= MAX_INTERNAL_TEST_ATTEMPTS:
            return (
                f"测试连续 {MAX_INTERNAL_TEST_ATTEMPTS} 次未通过或缺少测试证据，"
                "已停止内部返工循环。请查看测试证据后决定返工、接管或结束。"
            )
        return (
            f"测试第 {attempt}/{MAX_INTERNAL_TEST_ATTEMPTS} 次未通过或缺少测试证据，"
            f"最多还会内部返工 {MAX_INTERNAL_TEST_ATTEMPTS - attempt} 次。"
        )

    async def _handle_auto_final_plan(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """User clicked '生成最终方案': start Codex read-only final plan generation.
        Does NOT start Claude."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        wait_buttons = [[
            {
                "text": "查看状态",
                "callback_data": encode_conversation_callback(convo.id, AUTO_VIEW_STATUS),
            },
            {
                "text": "取消",
                "callback_data": encode_conversation_callback(convo.id, AUTO_CANCEL),
            },
        ]]
        final_plan_pending_text = "工程师正在生成最终方案，请等待完成。"
        final_plan_started_text = "工程师正在生成最终方案，完成后将显示方案和执行按钮。"
        team_run = None
        if self._adaptive_team_enabled and hasattr(
            self._ledger,
            "get_team_run_for_orchestration",
        ):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
        route_kind = self._team_route_kind(team_run) if team_run is not None else ""
        if route_kind == "bug":
            final_plan_pending_text = "诊断工程师正在整理修复方案和交接报告，请等待完成。"
            final_plan_started_text = (
                "诊断工程师正在整理修复方案和交接报告，完成后将显示方案和执行按钮。"
            )
        elif route_kind == "feature":
            final_plan_pending_text = "架构工程师正在生成最终方案，请等待完成。"
            final_plan_started_text = "架构工程师正在生成最终方案，完成后将显示方案和执行按钮。"
        if orch_run.current_step == AUTO_COLLECTING_CONTEXT and orch_run.status != "needs_user":
            return ControllerResponse(
                final_plan_pending_text,
                buttons=wait_buttons,
            )
        rewrite_from_draft = (
            callback.action == AUTO_REWRITE_PLAN
            and orch_run.status == "needs_user"
            and orch_run.current_step == AUTO_DRAFT_READY
        )
        if not (is_auto_collecting_context(orch_run) or rewrite_from_draft):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "无法生成最终方案。请先回到上下文收集阶段。"
            )

        # Reset the orchestration run context and start final plan Codex turn
        goal = orch_run.goal
        conversation_summary = convo.conversation_summary
        budget = ContextBudget()
        packet = build_auto_final_plan_packet(
            user_goal=goal,
            conversation_summary=trim_to_budget(
                conversation_summary, budget.conversation_summary_tokens
            ),
            workspace=convo.workspace_alias,
            budget=budget,
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=f"生成最终方案：{goal}",
            telegram_chat_id=chat_id,
            purpose="auto_final_plan",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=ROLE_AUTO_FINAL_PLAN,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_COLLECTING_CONTEXT,
        )
        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        return ControllerResponse(
            final_plan_started_text,
            buttons=wait_buttons,
        )

    async def _handle_auto_send_to_claude(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """Start the DeepSeek developer from the generated architecture prompt.

        Exactly one developer run is started. Audit never starts automatically.
        """
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_DRAFT_READY, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不能启动 DeepSeek 开发工程师执行。"
            )

        if self._claude is None or not getattr(self._claude, "enabled", False):
            return ControllerResponse(
                "DeepSeek 开发工程师未启用。请在配置中设置 claude.enabled = true 后重试。"
            )

        # Extract the developer execution prompt from the orchestration run's analysis.
        claude_prompt = (orch_run.last_codex_analysis or "").strip()
        if not claude_prompt:
            return ControllerResponse(
                "没有可见的最终方案正文，不能交给 DeepSeek 开发工程师执行。\n"
                "请先继续补充上下文。",
                buttons=self._auto_stage_buttons(
                    convo.id,
                    orch_run.current_step,
                    orch_run=orch_run,
                    last_codex_analysis=orch_run.last_codex_analysis or "",
                ),
            )
        team_run = None
        if self._adaptive_team_enabled and hasattr(
            self._ledger, "get_team_run_for_orchestration"
        ):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
        if team_run is not None:
            artifact_type, validator = self._gate_a_artifact_type_and_validator(
                team_run
            )

            gate_response = self._team_gate_response(
                gate_name="Gate A",
                artifact_type=artifact_type,
                validator=validator,
                team_run=team_run,
                conversation_id=convo.id,
                orchestration_run_id=orch_run.id,
            )
            if gate_response is not None:
                return gate_response
        chat_id = convo.chat_id

        # Update orchestration run to claude_running
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_CLAUDE_RUNNING,
        )

        # Create Claude agent run
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=claude_prompt,
            telegram_chat_id=chat_id,
            purpose="auto_implementation" if orch_run.current_step != AUTO_RETRY_READY else "auto_repair",
        )

        role = ROLE_AUTO_IMPLEMENTATION if orch_run.current_step != AUTO_RETRY_READY else ROLE_AUTO_REPAIR
        claude_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="claude",
            role=role,
            hidden_task_id=task.id,
            prompt_packet_summary=claude_prompt[:200],
        )
        self._ledger.update_agent_run_status(claude_run.id, "running")
        self._ledger.set_conversation_active_claude_run(convo.id, claude_run.id)

        implementer_job = None
        if team_run is not None:
            try:
                model_profile = (
                    self._claude_implementer_model_profile()
                    or self._implementer_model_profiles[0]
                )
                implementer_job = self._ledger.create_team_agent_job(
                    team_run_id=team_run.id,
                    role="implementer",
                    model_profile=model_profile,
                    status="running",
                    agent_run_id=claude_run.id,
                )
                self._ledger.record_team_assignment(
                    team_run_id=team_run.id,
                    role="implementer",
                    model_profile=model_profile,
                    selected_by="policy",
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_AGENT_JOB_STARTED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(claude_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": implementer_job.id,
                        "role": "implementer",
                        "model_profile": model_profile,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=claude_run.id,
                ))
                context_packet = self._build_team_context_packet_for_job(
                    team_run=team_run,
                    agent_job=implementer_job,
                    role="implementer",
                    model_profile=model_profile,
                    resume_state=(
                        "final plan accepted; implementation selected by user "
                        "(claude)\n\n"
                        f"Final plan:\n{claude_prompt}"
                    ),
                    output_schema="implementation_report",
                )
                claude_prompt = context_packet.render()
                packet_record = self._ledger.record_team_context_packet(
                    team_run_id=team_run.id,
                    agent_job_id=implementer_job.id,
                    packet_json=context_packet.as_json(),
                    prompt_text=claude_prompt,
                    prompt_tokens=approx_tokens(claude_prompt),
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_CONTEXT_PACKET_RECORDED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(claude_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": implementer_job.id,
                        "context_packet_id": packet_record.id,
                        "prompt_tokens": packet_record.prompt_tokens,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=claude_run.id,
                ))
            except Exception as exc:
                task = self._service.fail_task(task.id, str(exc))
                self._ledger.update_agent_run_status(
                    claude_run.id,
                    "failed",
                    completion_summary=str(exc)[:2000],
                )
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="failed",
                    last_claude_summary=str(exc),
                )
                if hasattr(self._ledger, "update_team_run_status"):
                    self._ledger.update_team_run_status(team_run.id, "failed")
                if implementer_job is not None and hasattr(
                    self._ledger,
                    "update_team_agent_job_status",
                ):
                    self._ledger.update_team_agent_job_status(
                        implementer_job.id,
                        "failed",
                    )
                return ControllerResponse(classify_user_error(exc))

        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        # Launch Claude as a background task
        bg_task = asyncio.create_task(
            self._run_claude_direct_async(
                agent_run_id=claude_run.id,
                task_id=task.id,
                conversation_id=convo.id,
                prompt=claude_prompt,
                workspace_path=workspace_path,
                chat_id=chat_id,
                correlation_id=self._new_correlation_id(),
            ),
            name=f"auto-claude-{claude_run.id}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        buttons = self._auto_stage_buttons(
            convo.id,
            AUTO_CLAUDE_RUNNING,
            orch_run=orch_run,
        )
        return ControllerResponse(
            "DeepSeek 开发工程师开始执行。完成后请点「审计工程师验收」。",
            buttons=buttons,
        )

    async def _handle_auto_send_to_codex(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_DRAFT_READY, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不能启动 GPT 开发工程师执行。"
            )
        if not self._codex_implementer_enabled():
            return ControllerResponse(
                "GPT 开发工程师未启用。请在团队配置中启用 GPT 开发工程师后重试。"
            )

        plan_text = (orch_run.last_codex_analysis or "").strip()
        if not plan_text:
            return ControllerResponse(
                "没有可见的最终方案正文，不能交给 GPT 开发工程师执行。\n"
                "请先继续补充上下文。",
                buttons=self._auto_stage_buttons(
                    convo.id,
                    orch_run.current_step,
                    orch_run=orch_run,
                    last_codex_analysis=orch_run.last_codex_analysis or "",
                ),
            )

        model_profile = self._codex_implementer_model_profile()
        if not model_profile:
            return ControllerResponse(
                "GPT 开发工程师未启用。请在团队配置中启用 GPT 开发工程师后重试。"
            )
        team_run = None
        if self._adaptive_team_enabled and hasattr(
            self._ledger, "get_team_run_for_orchestration"
        ):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
        if team_run is not None:
            artifact_type, validator = self._gate_a_artifact_type_and_validator(
                team_run
            )

            gate_response = self._team_gate_response(
                gate_name="Gate A",
                artifact_type=artifact_type,
                validator=validator,
                team_run=team_run,
                conversation_id=convo.id,
                orchestration_run_id=orch_run.id,
            )
            if gate_response is not None:
                return gate_response

        is_repair = orch_run.current_step == AUTO_RETRY_READY
        role = ROLE_AUTO_REPAIR if is_repair else ROLE_AUTO_IMPLEMENTATION
        purpose = "auto_codex_repair" if is_repair else "auto_codex_implementation"
        chat_id = convo.chat_id
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_CLAUDE_RUNNING,
        )

        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=plan_text,
            telegram_chat_id=chat_id,
            purpose=purpose,
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=role,
            hidden_task_id=task.id,
            prompt_packet_summary=plan_text[:200],
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        prompt_text = plan_text
        implementer_job = None
        if team_run is not None:
            try:
                implementer_job = self._ledger.create_team_agent_job(
                    team_run_id=team_run.id,
                    role="implementer",
                    model_profile=model_profile,
                    status="running",
                    agent_run_id=agent_run.id,
                )
                self._ledger.record_team_assignment(
                    team_run_id=team_run.id,
                    role="implementer",
                    model_profile=model_profile,
                    selected_by="policy",
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_AGENT_JOB_STARTED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(agent_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": implementer_job.id,
                        "role": "implementer",
                        "model_profile": model_profile,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=agent_run.id,
                ))
                context_packet = self._build_team_context_packet_for_job(
                    team_run=team_run,
                    agent_job=implementer_job,
                    role="implementer",
                    model_profile=model_profile,
                    resume_state=(
                        "final plan accepted; implementation selected by user "
                        "(codex)\n\n"
                        f"Final plan:\n{plan_text}"
                    ),
                    output_schema="implementation_report",
                )
                prompt_text = context_packet.render()
                packet_record = self._ledger.record_team_context_packet(
                    team_run_id=team_run.id,
                    agent_job_id=implementer_job.id,
                    packet_json=context_packet.as_json(),
                    prompt_text=prompt_text,
                    prompt_tokens=approx_tokens(prompt_text),
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_CONTEXT_PACKET_RECORDED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(agent_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": implementer_job.id,
                        "context_packet_id": packet_record.id,
                        "prompt_tokens": packet_record.prompt_tokens,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=agent_run.id,
                ))
            except Exception as exc:
                task = self._service.fail_task(task.id, str(exc))
                self._ledger.update_agent_run_status(
                    agent_run.id,
                    "failed",
                    completion_summary=str(exc)[:2000],
                )
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="failed",
                    last_claude_summary=str(exc),
                )
                if team_run is not None and hasattr(
                    self._ledger,
                    "update_team_run_status",
                ):
                    self._ledger.update_team_run_status(team_run.id, "failed")
                if implementer_job is not None and hasattr(
                    self._ledger,
                    "update_team_agent_job_status",
                ):
                    self._ledger.update_team_agent_job_status(
                        implementer_job.id,
                        "failed",
                    )
                return ControllerResponse(classify_user_error(exc))

        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)
        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=prompt_text,
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            self._ledger.update_orchestration_run(
                orch_run.id,
                status="failed",
                last_claude_summary=str(exc),
            )
            if team_run is not None and hasattr(
                self._ledger,
                "update_team_run_status",
            ):
                self._ledger.update_team_run_status(team_run.id, "failed")
            if implementer_job is not None and hasattr(
                self._ledger,
                "update_team_agent_job_status",
            ):
                self._ledger.update_team_agent_job_status(
                    implementer_job.id,
                    "failed",
                )
            return ControllerResponse(classify_user_error(exc))

        buttons = self._auto_stage_buttons(
            convo.id,
            AUTO_CLAUDE_RUNNING,
            orch_run=orch_run,
        )
        return ControllerResponse(
            "GPT 开发工程师开始执行。完成后请点「审计工程师验收」。",
            buttons=buttons,
        )

    async def _handle_auto_codex_verify(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """Start read-only audit verification.
        Only starts after explicit click, never automatically."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不是等待验收阶段。请等开发工程师完成后再点「审计工程师验收」。"
            )

        goal = orch_run.goal
        codex_analysis = orch_run.last_codex_analysis or ""
        claude_summary = orch_run.last_claude_summary or ""
        verify_round = orch_run.verify_round + 1

        task_id = self._implementation_evidence_task_id(convo, orch_run)
        changed_files, diff_summary = self._collect_task_evidence(task_id)
        unrelated_changed_files: list[str] = []
        team_run = None
        if self._adaptive_team_enabled and hasattr(
            self._ledger, "get_team_run_for_orchestration"
        ):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
        if team_run is not None:
            from wlcodex.team_artifacts import (
                validate_implementation_report,
                validate_test_report,
            )

            implementer_job_id = self._latest_team_job_id(
                team_run,
                role="implementer",
                status="done",
            )
            tester_job_id = self._latest_team_job_id(
                team_run,
                role="tester",
                status="done",
            ) or implementer_job_id
            gate_response = self._team_gate_response(
                gate_name="Gate B",
                artifact_type="implementation_report",
                validator=validate_implementation_report,
                team_run=team_run,
                conversation_id=convo.id,
                orchestration_run_id=orch_run.id,
                agent_job_id=implementer_job_id,
                bind_agent_job=True,
                failure_step=AUTO_RETRY_READY,
            )
            if gate_response is not None:
                return gate_response
            gate_response = self._team_gate_response(
                gate_name="Gate C",
                artifact_type="test_report",
                validator=validate_test_report,
                team_run=team_run,
                conversation_id=convo.id,
                orchestration_run_id=orch_run.id,
                agent_job_id=tester_job_id,
                bind_agent_job=True,
                failure_step=AUTO_RETRY_READY,
            )
            if gate_response is not None:
                return gate_response
            task_changed_files, task_diff_summary = self._latest_implementation_scope(
                team_run,
                agent_job_id=implementer_job_id,
            )
            if task_changed_files:
                workspace_files = changed_files or _changed_files_from_diff_summary(
                    diff_summary
                )
                task_file_set = set(task_changed_files)
                unrelated_changed_files = [
                    path for path in workspace_files if path not in task_file_set
                ]
                changed_files = task_changed_files
                if task_diff_summary:
                    diff_summary = task_diff_summary
                scoped_diff_summary = _filter_diff_summary_to_files(
                    diff_summary,
                    task_changed_files,
                )
                if scoped_diff_summary:
                    diff_summary = scoped_diff_summary
            else:
                baseline_dirty = set(
                    self._task_workspace_baseline_dirty_files(task_id)
                )
                if baseline_dirty:
                    workspace_files = changed_files or _changed_files_from_diff_summary(
                        diff_summary
                    )
                    changed_files = [
                        path for path in workspace_files if path not in baseline_dirty
                    ]
                    unrelated_changed_files = [
                        path for path in workspace_files if path in baseline_dirty
                    ]
                    scoped_diff_summary = _filter_diff_summary_to_files(
                        diff_summary,
                        changed_files,
                    )
                    if scoped_diff_summary:
                        diff_summary = scoped_diff_summary

        budget = ContextBudget()
        if unrelated_changed_files:
            side_note = (
                "旁路变更提示：当前工作区还存在非本任务文件 "
                + ", ".join(unrelated_changed_files[:10])
                + "。这些文件不属于 changed_files 主验收范围；仅当它们与任务文件冲突或导致任务无法复核时才阻断。"
            )
            test_results = side_note
        else:
            test_results = ""
        packet = build_auto_verification_packet(
            user_goal=goal,
            codex_plan_summary=codex_analysis[:800],
            claude_completion_summary=claude_summary[:1500],
            changed_files=changed_files[:20],
            unrelated_changed_files=unrelated_changed_files[:20],
            test_results=test_results,
            diff_summary=diff_summary[:1500],
            workspace=convo.workspace_alias,
            budget=budget,
            verify_round=verify_round,
        )

        # Increment verify round
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_VERIFYING,
            verify_round=verify_round,
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=f"验收：{goal}",
            telegram_chat_id=chat_id,
            purpose="auto_verification",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=ROLE_AUTO_VERIFICATION,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        prompt_text = packet.render()
        auditor_job = None
        if team_run is not None:
            try:
                auditor_job = self._ledger.create_team_agent_job(
                    team_run_id=team_run.id,
                    role="auditor",
                    model_profile=self._auditor_model_profile,
                    status="running",
                    agent_run_id=agent_run.id,
                )
                self._ledger.record_team_assignment(
                    team_run_id=team_run.id,
                    role="auditor",
                    model_profile=self._auditor_model_profile,
                    selected_by="policy",
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_AGENT_JOB_STARTED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(agent_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": auditor_job.id,
                        "role": "auditor",
                        "model_profile": self._auditor_model_profile,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=agent_run.id,
                ))
                verification_artifact = self._ledger.record_team_artifact(
                    team_run_id=team_run.id,
                    agent_job_id=auditor_job.id,
                    artifact_type="verification_request",
                    summary=f"Verification round {verify_round}: {goal[:200]}",
                    payload={
                        "goal": goal,
                        "codex_plan_summary": codex_analysis[:800],
                        "implementation_summary": claude_summary[:1500],
                        "changed_files": changed_files[:20],
                        "unrelated_changed_files": unrelated_changed_files[:20],
                        "diff_summary": diff_summary[:1500],
                        "verify_round": verify_round,
                        "tester_policy": (
                            "Audit starts only after current-round test evidence exists"
                        ),
                    },
                )
                self._emit_team_runtime_event(
                    EventType.TEAM_ARTIFACT_RECORDED,
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    team_run_id=team_run.id,
                    agent_job_id=auditor_job.id,
                    agent_run_id=agent_run.id,
                    task_id=task.id,
                    payload={
                        "artifact_id": verification_artifact.id,
                        "artifact_type": verification_artifact.artifact_type,
                    },
                )
                context_packet = self._build_team_context_packet_for_job(
                    team_run=team_run,
                    agent_job=auditor_job,
                    role="auditor",
                    model_profile=self._auditor_model_profile,
                    resume_state=(
                        f"verification requested; verify_round={verify_round}\n\n"
                        f"{prompt_text}"
                    ),
                    output_schema="audit_report",
                )
                prompt_text = context_packet.render()
                packet_record = self._ledger.record_team_context_packet(
                    team_run_id=team_run.id,
                    agent_job_id=auditor_job.id,
                    packet_json=context_packet.as_json(),
                    prompt_text=prompt_text,
                    prompt_tokens=approx_tokens(prompt_text),
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_CONTEXT_PACKET_RECORDED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(agent_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": auditor_job.id,
                        "context_packet_id": packet_record.id,
                        "prompt_tokens": packet_record.prompt_tokens,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=agent_run.id,
                ))
            except Exception as exc:
                task = self._service.fail_task(task.id, str(exc))
                self._ledger.update_agent_run_status(
                    agent_run.id,
                    "failed",
                    completion_summary=str(exc)[:2000],
                )
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="failed",
                    last_verification_result=str(exc),
                )
                if hasattr(self._ledger, "update_team_run_status"):
                    self._ledger.update_team_run_status(team_run.id, "failed")
                if auditor_job is not None and hasattr(
                    self._ledger,
                    "update_team_agent_job_status",
                ):
                    self._ledger.update_team_agent_job_status(
                        auditor_job.id,
                        "failed",
                    )
                return ControllerResponse(classify_user_error(exc))

        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=prompt_text,
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            self._ledger.update_orchestration_run(
                orch_run.id,
                status="failed",
                last_verification_result=str(exc),
            )
            if team_run is not None and hasattr(
                self._ledger,
                "update_team_run_status",
            ):
                self._ledger.update_team_run_status(team_run.id, "failed")
            if auditor_job is not None and hasattr(
                self._ledger,
                "update_team_agent_job_status",
            ):
                self._ledger.update_team_agent_job_status(
                    auditor_job.id,
                    "failed",
                )
            return ControllerResponse(classify_user_error(exc))

        buttons = self._auto_stage_buttons(
            convo.id,
            AUTO_VERIFYING,
            orch_run=orch_run,
        )
        return ControllerResponse("审计工程师开始验收，完成后将显示验收结果。", buttons=buttons)

    async def _handle_auto_codex_takeover(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """User clicked 'Codex 接管修': explicitly allow Codex to write code."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_DRAFT_READY, AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不能在此阶段接管。"
            )

        goal = orch_run.goal
        analysis = orch_run.last_codex_analysis or ""
        claude_summary = orch_run.last_claude_summary or ""
        verification = orch_run.last_verification_result or ""

        # Update orchestration run to codex_takeover_running
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_CODEX_TAKEOVER_RUNNING,
        )

        prompt = (
            f"GPT 开发工程师接管修复任务\n\n"
            f"原始目标：{goal}\n\n"
            f"架构方案：{analysis[:500]}\n\n"
            f"开发工程师产出：{claude_summary[:300]}\n\n"
            f"验收结果：{verification[:300]}\n\n"
            f"用户明确要求 GPT 开发工程师直接修复，请直接修改代码完成目标。"
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=prompt,
            telegram_chat_id=chat_id,
            purpose="auto_codex_takeover",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=ROLE_AUTO_CODEX_TAKEOVER,
            hidden_task_id=task.id,
            prompt_packet_summary=prompt[:200],
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")
        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=prompt,
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        buttons = self._auto_stage_buttons(
            convo.id,
            AUTO_CODEX_TAKEOVER_RUNNING,
            orch_run=orch_run,
        )
        return ControllerResponse("GPT 开发工程师开始直接修复。", buttons=buttons)

    async def _handle_auto_send_repair_to_claude(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """Start developer rework from retry_ready with a generated repair prompt."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step == AUTO_COMPLETED:
            return ControllerResponse(
                "这条任务已经完成，不能再返工。你可以查看状态或开始新的任务。",
                buttons=self._auto_stage_buttons(
                    convo.id,
                    AUTO_COMPLETED,
                    orch_run=orch_run,
                ),
            )
        if orch_run.current_step not in (AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不能返工。"
            )

        if self._claude is None or not getattr(self._claude, "enabled", False):
            return ControllerResponse(
                "DeepSeek 开发工程师未启用。请在配置中设置 claude.enabled = true 后重试。"
            )

        goal = orch_run.goal
        verification = orch_run.last_verification_result or ""
        analysis = orch_run.last_codex_analysis or ""

        # Build repair prompt from verification result
        budget = ContextBudget()
        repair_packet = build_auto_repair_packet(
            user_goal=goal,
            codex_plan_summary=analysis[:500],
            claude_completion_summary="",
            verification_result=verification[:500],
            workspace=convo.workspace_alias,
            budget=budget,
        )
        claude_prompt = repair_packet.render()

        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_CLAUDE_RUNNING,
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=claude_prompt,
            telegram_chat_id=chat_id,
            purpose="auto_repair",
        )

        claude_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="claude",
            role=ROLE_AUTO_REPAIR,
            hidden_task_id=task.id,
            prompt_packet_summary=claude_prompt[:200],
        )
        self._ledger.update_agent_run_status(claude_run.id, "running")
        self._ledger.set_conversation_active_claude_run(convo.id, claude_run.id)

        team_run = None
        if self._adaptive_team_enabled and hasattr(
            self._ledger, "get_team_run_for_orchestration"
        ):
            team_run = self._ledger.get_team_run_for_orchestration(orch_run.id)
        implementer_job = None
        if team_run is not None:
            try:
                model_profile = (
                    self._claude_implementer_model_profile()
                    or self._implementer_model_profiles[0]
                )
                implementer_job = self._ledger.create_team_agent_job(
                    team_run_id=team_run.id,
                    role="implementer",
                    model_profile=model_profile,
                    status="running",
                    agent_run_id=claude_run.id,
                )
                self._ledger.record_team_assignment(
                    team_run_id=team_run.id,
                    role="implementer",
                    model_profile=model_profile,
                    selected_by="policy",
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_AGENT_JOB_STARTED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(claude_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": implementer_job.id,
                        "role": "implementer",
                        "model_profile": model_profile,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=claude_run.id,
                ))
                context_packet = self._build_team_context_packet_for_job(
                    team_run=team_run,
                    agent_job=implementer_job,
                    role="implementer",
                    model_profile=model_profile,
                    resume_state=(
                        "repair selected by user (claude)\n\n"
                        f"Verification result:\n{verification[:1200]}\n\n"
                        f"Repair prompt:\n{claude_prompt}"
                    ),
                    output_schema="implementation_report",
                )
                claude_prompt = context_packet.render()
                packet_record = self._ledger.record_team_context_packet(
                    team_run_id=team_run.id,
                    agent_job_id=implementer_job.id,
                    packet_json=context_packet.as_json(),
                    prompt_text=claude_prompt,
                    prompt_tokens=approx_tokens(claude_prompt),
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TEAM_CONTEXT_PACKET_RECORDED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(claude_run.id),
                    correlation_id=self._new_correlation_id(),
                    source=EventSource.CONTROLLER,
                    actor="controller",
                    visibility=Visibility.OPERATOR,
                    payload={
                        "team_run_id": team_run.id,
                        "agent_job_id": implementer_job.id,
                        "context_packet_id": packet_record.id,
                        "prompt_tokens": packet_record.prompt_tokens,
                    },
                    occurred_at=now_iso(),
                    conversation_id=convo.id,
                    orchestration_run_id=orch_run.id,
                    agent_run_id=claude_run.id,
                ))
            except Exception as exc:
                task = self._service.fail_task(task.id, str(exc))
                self._ledger.update_agent_run_status(
                    claude_run.id,
                    "failed",
                    completion_summary=str(exc)[:2000],
                )
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="failed",
                    last_claude_summary=str(exc),
                )
                if hasattr(self._ledger, "update_team_run_status"):
                    self._ledger.update_team_run_status(team_run.id, "failed")
                if implementer_job is not None and hasattr(
                    self._ledger,
                    "update_team_agent_job_status",
                ):
                    self._ledger.update_team_agent_job_status(
                        implementer_job.id,
                        "failed",
                    )
                return ControllerResponse(classify_user_error(exc))

        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        bg_task = asyncio.create_task(
            self._run_claude_direct_async(
                agent_run_id=claude_run.id,
                task_id=task.id,
                conversation_id=convo.id,
                prompt=claude_prompt,
                workspace_path=workspace_path,
                chat_id=chat_id,
                correlation_id=self._new_correlation_id(),
            ),
            name=f"auto-repair-{claude_run.id}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        buttons = self._auto_stage_buttons(
            convo.id,
            AUTO_CLAUDE_RUNNING,
            orch_run=orch_run,
        )
        return ControllerResponse(
            "DeepSeek 开发工程师开始返工。完成后请点「审计工程师验收」。",
            buttons=buttons,
        )

    async def _handle_auto_close(self, callback: ConversationCallback) -> ControllerResponse:
        """User clicked '结束任务': mark the auto run as completed."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。")
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="passed",
            current_step=AUTO_COMPLETED,
        )
        buttons = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(convo.id, STATUS)},
        ]]
        return ControllerResponse("自动工作流已结束。", buttons=buttons)

    async def _handle_auto_cancel(self, callback: ConversationCallback) -> ControllerResponse:
        """User clicked '取消': abort the auto run."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。")

        # Abort any active tasks
        if convo.active_codex_task_id:
            try:
                self._service.abort_task(convo.active_codex_task_id)
            except Exception:
                pass
        if convo.active_claude_run_id and self._claude is not None:
            try:
                self._claude.interrupt()
            except Exception:
                pass

        self._ledger.update_orchestration_run(
            orch_run.id,
            status="aborted",
            current_step=AUTO_COMPLETED,
        )
        buttons = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(convo.id, STATUS)},
        ]]
        return ControllerResponse("自动工作流已取消。", buttons=buttons)

    async def handle_verify(
        self, command: VerifyCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if active is None:
            return ControllerResponse("当前没有活跃对话。请先开始对话。")

        runs = self._ledger.list_agent_runs(active.id, limit=5)
        if not runs:
            return ControllerResponse("当前对话没有已完成的运行。")

        # Find the latest completed Claude run for verification evidence
        latest_claude_run: object | None = None
        for r in reversed(runs):
            if r.agent == "claude" and r.status == "done":
                latest_claude_run = r
                break
        if latest_claude_run is None:
            latest_claude_run = runs[-1]

        # Collect diff evidence from the workspace
        diff_summary = ""
        changed_files: list[str] = []
        test_results = ""
        try:
            workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
            diff_result = self._inspector.diff(
                latest_claude_run.hidden_task_id or 0, workspace_path
            )
            if diff_result and diff_result.body:
                diff_summary = diff_result.body[:1500]
            # Extract file paths from diff or use inspector
            files_result = self._inspector.files(
                latest_claude_run.hidden_task_id or 0
            )
            if files_result and files_result.body:
                for line in files_result.body.split("\n"):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        changed_files.append(stripped[:200])

            # Collect test evidence from workspace
            test_files = [f for f in changed_files if "test" in f.lower()]
            if test_files:
                test_results = (
                    "Tests were modified in this change. "
                    "Test commands were NOT executed — manual verification required. "
                    f"Modified test files: {', '.join(test_files[:10])}"
                )
            else:
                test_results = (
                    "No test files were modified in this change. "
                    "Manual verification of correctness is required."
                )
        except Exception:
            pass

        # Get actual Claude completion output (not the input prompt)
        completion = getattr(latest_claude_run, "completion_summary", "")
        if not completion:
            completion = getattr(latest_claude_run, "prompt_packet_summary", "")

        # Build verification packet with real evidence
        verify_payload = command.prompt if command.prompt else active.title
        codex_plan = ""
        orch_runs = self._ledger.list_orchestration_runs(active.id, limit=1)
        if orch_runs:
            codex_plan = orch_runs[0].last_codex_analysis

        packet = make_verification_packet(
            user_goal=verify_payload,
            codex_plan_summary=codex_plan[:800] if codex_plan else "",
            claude_completion_summary=completion[:1500] if completion else "",
            changed_files=changed_files[:20],
            diff_summary=diff_summary[:1500] if diff_summary else "",
            test_results=test_results,
            workspace=active.workspace_alias,
        )

        # Call Codex backend for verification
        try:
            if active.active_codex_task_id:
                task = self._service.get_task(active.active_codex_task_id)
                if task.codex_thread_id:
                    await self._backend.start_turn(task.codex_thread_id, packet.render())
                    verification_text = f"已向审计工程师发送验收请求。\n" \
                                        f"对话：{active.title}\n" \
                                        f"变更文件：{len(changed_files)} 个\n" \
                                        f"验收证据：架构方案 + 开发工程师输出摘要 + diff"
                else:
                    verification_text = "审计工程师线程未就绪，无法发送验收。"
            else:
                workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
                thread_id = await self._backend.create_thread(workspace_path)
                task = self._reserve_execution_lease(
                    conversation_id=active.id,
                    workspace_alias=active.workspace_alias,
                    prompt=f"验证：{verify_payload}",
                    telegram_chat_id=chat_id,
                    purpose="codex_verification",
                )
                self._service.set_task_thread(task.id, thread_id)
                await self._backend.start_turn(thread_id, packet.render())
                verification_text = "已向审计工程师发送验收请求。\n" \
                                    f"对话：{active.title}\n" \
                                    f"验收内容：{verify_payload}\n" \
                                    f"变更文件：{len(changed_files)} 个"
        except Exception as exc:
            logger.warning("verify: Codex call failed: %s", exc)
            verification_text = f"审计工程师验收请求失败：{exc}"

        return ControllerResponse(
            f"审计工程师验收 — 对话「{active.title}」\n\n"
            f"最近运行：#{latest_claude_run.id}（{latest_claude_run.agent}/{latest_claude_run.status}）\n"
            f"输入摘要：{latest_claude_run.prompt_packet_summary[:200]}\n"
            f"输出摘要：{completion[:200] if completion else '(无)'}\n"
            f"变更文件：{len(changed_files)} 个\n"
            f"Token：{latest_claude_run.token_input} 输入 / {latest_claude_run.token_output} 输出\n\n"
            f"{verification_text}"
        )

    # --- Carryover handlers ---

    async def handle_carry_workbench(
        self, command: CarryWorkbenchCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")
        chat_id = ctx.get("chat_id", 0) if ctx else 0
        query = command.query.strip()
        if query.isdigit():
            return await self._prepare_workbench_carryover(int(query), chat_id)
        conversations = self._ledger.list_conversations_by_chat(
            chat_id, limit=20, include_archived=True
        )
        # Build carryover sources for all candidates so we can deep-search.
        candidates: list[tuple[object, CarryoverSource, str]] = []
        for convo in conversations:
            source = self._build_carryover_source(convo)
            brief = build_continuity_brief(source)
            candidates.append((convo, source, brief))
        if query:
            lowered = query.lower()
            candidates = [
                (convo, source, brief)
                for convo, source, brief in candidates
                if lowered in convo.title.lower()
                or lowered in convo.workspace_alias.lower()
                or lowered in convo.conversation_summary.lower()
                or lowered in source.latest_codex_summary.lower()
                or lowered in source.latest_claude_summary.lower()
                or lowered in source.latest_verification_result.lower()
                or lowered in brief.lower()
            ]
        # Render up to 8 candidates with previews.
        items: list[tuple] = [
            (convo, build_carryover_preview(source))
            for convo, source, _brief in candidates[:8]
        ]
        buttons = self._carryover_candidate_buttons(
            [item[0] for item in items]
        )
        return ControllerResponse(
            render_carryover_candidates(items), buttons=buttons,
        )

    def _build_carryover_source(
        self, conversation: object
    ) -> CarryoverSource:
        evidence = self._ledger.list_carryover_evidence(conversation.id)  # type: ignore[union-attr]
        agent_runs = evidence.agent_runs
        orch_runs = evidence.orchestration_runs
        latest_codex = next(
            (
                r.completion_summary
                for r in agent_runs
                if r.agent == "codex" and r.completion_summary
            ),
            "",
        )
        latest_claude = next(
            (
                r.completion_summary
                for r in agent_runs
                if r.agent == "claude" and r.completion_summary
            ),
            "",
        )
        # Fall back to orchestration_run fields when agent_runs don't have summaries.
        if not latest_codex:
            latest_codex = next(
                (r.last_codex_analysis for r in orch_runs if r.last_codex_analysis),
                "",
            )
        if not latest_claude:
            latest_claude = next(
                (r.last_claude_summary for r in orch_runs if r.last_claude_summary),
                "",
            )
        latest_verification = next(
            (
                r.last_verification_result
                for r in orch_runs
                if r.last_verification_result
            ),
            "",
        )
        refs = [
            *(f"agent_run={run.id}:{run.agent}/{run.role}/{run.status}" for run in agent_runs[:3]),
            *(f"orchestration_run={run.id}:{run.status}/{run.current_step}" for run in orch_runs[:3]),
        ]
        return CarryoverSource(
            source_conversation_id=conversation.id,
            title=conversation.title,
            workspace_alias=conversation.workspace_alias,
            conversation_summary=conversation.conversation_summary,
            latest_codex_summary=latest_codex,
            latest_claude_summary=latest_claude,
            latest_verification_result=latest_verification,
            evidence_refs=refs,
        )

    def _carryover_candidate_buttons(
        self, conversations: list
    ) -> list[list[dict[str, str]]]:
        button_rows: list[list[dict[str, str]]] = []
        for convo in conversations:
            button_rows.append([
                {
                    "text": "接棒开新工作台",
                    "callback_data": encode_conversation_callback(convo.id, CARRY_START),
                },
                {
                    "text": "查看接棒摘要",
                    "callback_data": encode_conversation_callback(convo.id, CARRY_SHOW),
                },
                {
                    "text": "刷新摘要",
                    "callback_data": encode_conversation_callback(convo.id, CARRY_REFRESH),
                },
            ])
        return button_rows

    async def _cancel_previous_prepared_for_chat(
        self, chat_id: int,
    ) -> None:
        """Cancel all previously prepared carryovers for this chat."""
        if self._ledger is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._ledger._conn.execute(
            """
            UPDATE workbench_carryovers
            SET status = 'cancelled', updated_at = ?
            WHERE chat_id = ? AND status = 'prepared'
            """,
            (now, chat_id),
        )
        self._ledger._conn.commit()

    async def _prepare_workbench_carryover(
        self, source_conversation_id: int, chat_id: int
    ) -> ControllerResponse:
        try:
            source_convo = self._ledger.get_conversation(source_conversation_id)  # type: ignore[union-attr]
        except KeyError:
            return ControllerResponse("工作台不存在或已被删除。")
        if source_convo.chat_id != chat_id:
            return ControllerResponse("不能接棒其他聊天里的工作台。")
        await self._cancel_previous_prepared_for_chat(chat_id)
        source = self._build_carryover_source(source_convo)
        brief = build_continuity_brief(source)
        preview = build_carryover_preview(source)
        fingerprint = build_source_fingerprint(
            conversation_id=source_convo.id,
            latest_agent_run_ids=[
                run.id
                for run in self._ledger.list_recent_agent_runs(source_convo.id, limit=5)  # type: ignore[union-attr]
            ],
            latest_orchestration_run_ids=[
                run.id
                for run in self._ledger.list_orchestration_runs(source_convo.id, limit=5)  # type: ignore[union-attr]
            ],
        )
        self._ledger.create_workbench_carryover(  # type: ignore[union-attr]
            chat_id=chat_id,
            source_conversation_id=source_convo.id,
            workspace_alias=source_convo.workspace_alias,
            brief_text=brief,
            preview_text=preview,
            source_fingerprint=fingerprint,
            status="prepared",
        )
        return ControllerResponse(
            render_prepared_carryover(
                source_conversation_id=source_convo.id,
                source_title=source_convo.title,
                workspace_alias=source_convo.workspace_alias,
                preview=preview,
            ),
            buttons=[[
                {
                    "text": "查看接棒摘要",
                    "callback_data": encode_conversation_callback(source_convo.id, CARRY_SHOW),
                },
                {
                    "text": "取消接棒",
                    "callback_data": encode_conversation_callback(source_convo.id, CARRY_CANCEL),
                },
            ]],
        )

    async def _handle_carry_start(
        self, convo: object,
    ) -> ControllerResponse:
        return await self._prepare_workbench_carryover(convo.id, convo.chat_id)

    async def _handle_carry_show(
        self, convo: object,
    ) -> ControllerResponse:
        source = self._build_carryover_source(convo)
        brief = build_continuity_brief(source)
        buttons: list[list[dict[str, str]]] = [[
            {
                "text": "刷新摘要",
                "callback_data": encode_conversation_callback(convo.id, CARRY_REFRESH),
            },
        ]]
        return ControllerResponse(
            render_carryover_brief_view(
                source_conversation_id=convo.id, brief_text=brief,
            ),
            buttons=buttons,
        )

    async def _handle_carry_refresh(
        self, convo: object,
    ) -> ControllerResponse:
        source = self._build_carryover_source(convo)
        brief = build_continuity_brief(source)
        preview = build_carryover_preview(source)
        prepared = self._ledger.get_latest_prepared_carryover(convo.chat_id)  # type: ignore[union-attr]
        if prepared is not None and prepared.source_conversation_id == convo.id:
            fingerprint = build_source_fingerprint(
                conversation_id=convo.id,
                latest_agent_run_ids=[
                    run.id
                    for run in self._ledger.list_recent_agent_runs(convo.id, limit=5)  # type: ignore[union-attr]
                ],
                latest_orchestration_run_ids=[
                    run.id
                    for run in self._ledger.list_orchestration_runs(convo.id, limit=5)  # type: ignore[union-attr]
                ],
            )
            self._ledger.update_workbench_carryover_brief(  # type: ignore[union-attr]
                prepared.id,
                brief_text=brief,
                preview_text=preview,
                source_fingerprint=fingerprint,
            )
        buttons: list[list[dict[str, str]]] = [[
            {
                "text": "接棒开新工作台",
                "callback_data": encode_conversation_callback(convo.id, CARRY_START),
            },
        ]]
        return ControllerResponse(
            "摘要已刷新。\n\n"
            + render_carryover_brief_view(
                source_conversation_id=convo.id, brief_text=brief,
            ),
            buttons=buttons,
        )

    async def _handle_carry_cancel(
        self, convo: object,
    ) -> ControllerResponse:
        chat_id = convo.chat_id
        if chat_id:
            prepared = self._ledger.get_latest_prepared_carryover(chat_id)  # type: ignore[union-attr]
            if prepared is not None:
                self._ledger.update_workbench_carryover_status(prepared.id, "cancelled")  # type: ignore[union-attr]
        return ControllerResponse(render_carryover_cancelled())

    async def _consume_prepared_carryover(
        self,
        carryover: object,
        text: str,
        ctx: dict[str, Any] | None,
    ) -> ControllerResponse:
        chat_id = (
            ctx.get("chat_id", 0)
            if ctx
            else getattr(carryover, "chat_id", 0)
        )
        user_id = ctx.get("user_id", 0) if ctx else 0
        try:
            source = self._ledger.get_conversation(carryover.source_conversation_id)  # type: ignore[union-attr]
        except KeyError:
            self._ledger.update_workbench_carryover_status(carryover.id, "cancelled")  # type: ignore[union-attr]
            return ControllerResponse("接棒来源工作台已不存在，已取消。")
        if source.chat_id != chat_id:
            self._ledger.update_workbench_carryover_status(carryover.id, "cancelled")  # type: ignore[union-attr]
            return ControllerResponse("接棒来源不属于当前聊天，已取消。")
        try:
            self._service.get_workspace(carryover.workspace_alias)
        except Exception:
            return ControllerResponse(
                f"来源工作区 {carryover.workspace_alias} 当前未配置。"
                "请先在配置中加入该工作区，或取消接棒后使用 /switch。"
            )
        blocker = self._service.blocker_for_workspace(carryover.workspace_alias)
        if blocker is not None:
            busy_convo = self._find_workspace_busy_conversation(
                chat_id=chat_id,
                workspace_alias=carryover.workspace_alias,
                blocking_task_id=blocker.id,
            )
            decision = RouteDecision(
                route="workspace_busy",
                reason="carryover_workspace_busy",
                new_conversation=False,
                intent="normal_text",
            )
            response = await self._handle_workspace_busy(
                decision,
                busy_convo or source,
                blocker.id,
                None,
                self._new_correlation_id(),
                original_text=text,
                agent_label="现场",
            )
            return ControllerResponse(
                response.text + "\n\n接棒状态已保留，不会丢失。",
                buttons=response.buttons,
            )
        old = self._ledger.get_active_conversation(chat_id)  # type: ignore[union-attr]
        if old is not None:
            self._ledger.archive_conversation(old.id)  # type: ignore[union-attr]
        title = default_title(text)
        target = self._ledger.create_conversation(  # type: ignore[union-attr]
            chat_id=chat_id,
            user_id=user_id,
            title=title,
            mode=self._default_mode,
            workspace_alias=carryover.workspace_alias,
        )
        summary = trim_to_budget(
            f"{carryover.brief_text}\n\n当前用户新任务：{text[:500]}",
            ContextBudget().conversation_summary_tokens,
        )
        self._ledger.update_conversation_summary(target.id, summary)  # type: ignore[union-attr]
        self._ledger.mark_workbench_carryover_used(carryover.id, target.id)  # type: ignore[union-attr]
        return ControllerResponse(
            render_carryover_target_created(
                source_conversation_id=source.id,
                target_title=target.title,
                workspace_alias=target.workspace_alias,
            ),
            buttons=[[
                {
                    "text": "查看状态",
                    "callback_data": encode_conversation_callback(target.id, STATUS),
                },
            ]],
        )

    def _find_workspace_busy_conversation(
        self,
        *,
        chat_id: int,
        workspace_alias: str,
        blocking_task_id: int | None,
    ) -> object | None:
        if self._ledger is None:
            return None
        candidates: list[object] = []
        active = self._ledger.get_active_conversation(chat_id)
        if active is not None:
            candidates.append(active)
        for convo in self._ledger.list_conversations_by_chat(
            chat_id, limit=20, include_archived=False
        ):
            if active is not None and convo.id == active.id:
                continue
            candidates.append(convo)

        workspace_candidates = [
            convo
            for convo in candidates
            if getattr(convo, "workspace_alias", "") == workspace_alias
        ]
        if blocking_task_id is not None:
            for convo in workspace_candidates:
                if getattr(convo, "active_codex_task_id", None) == blocking_task_id:
                    return convo
        for convo in workspace_candidates:
            if getattr(convo, "active_codex_task_id", None) or getattr(
                convo, "active_claude_run_id", None
            ):
                return convo
        return workspace_candidates[0] if workspace_candidates else None

    async def _handle_restore_workbench(self, conversation_id: int) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")
        try:
            target = self._ledger.get_conversation(conversation_id)
            previous_active = self._ledger.get_active_conversation(target.chat_id)
            restored = self._ledger.restore_conversation(conversation_id)
        except KeyError:
            return ControllerResponse("工作台不存在或已被删除。")
        self._record_workbench_restore_events(restored, previous_active)
        try:
            self._service.get_workspace(restored.workspace_alias)
            workspace_note = f"工作区：{restored.workspace_alias}"
        except Exception:
            workspace_note = (
                f"工作区：{restored.workspace_alias}（当前配置不存在，"
                "请先添加该 workspace 后再执行任务）"
            )
        return ControllerResponse(
            f"已恢复工作台 #{restored.id}：「{restored.title}」\n"
            f"{workspace_note}\n\n"
            "直接发消息会继续这个工作台。"
        )

    def _record_workbench_restore_events(
        self, restored: object, previous_active: object | None
    ) -> None:
        correlation_id = self._new_correlation_id()
        if previous_active is not None and previous_active.id != restored.id:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_CLOSED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(previous_active.id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.USER,
                payload={
                    "chat_id": previous_active.chat_id,
                    "conversation_id": previous_active.id,
                    "reason": "workbench_restore",
                    "activated_conversation_id": restored.id,
                },
                occurred_at=now_iso(),
                conversation_id=previous_active.id,
            ))
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_ACTIVATED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(restored.id),
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.USER,
            payload={
                "chat_id": restored.chat_id,
                "conversation_id": restored.id,
                "previous_conversation_id": (
                    previous_active.id if previous_active is not None else None
                ),
                "workspace_alias": restored.workspace_alias,
                "mode": restored.mode,
            },
            occurred_at=now_iso(),
            conversation_id=restored.id,
        ))
        previous_surface_mode = self._latest_surface_mode(restored.id)
        if previous_surface_mode is not None and previous_surface_mode != "product":
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_MODE_SWITCHED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(restored.id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.USER,
                payload={
                    "chat_id": restored.chat_id,
                    "conversation_id": restored.id,
                    "from_mode": previous_surface_mode,
                    "to_mode": "product",
                    "reason": "workbench_restore",
                },
                occurred_at=now_iso(),
                conversation_id=restored.id,
            ))

    def _latest_surface_mode(self, conversation_id: int) -> str | None:
        if self._store is None:
            return None
        try:
            events = self._store.list_recent_for_conversation(conversation_id, limit=200)
        except Exception:
            logger.debug("Failed to read latest surface mode", exc_info=True)
            return None
        for event in reversed(events):
            if event.event_type != EventType.CONVERSATION_MODE_SWITCHED:
                continue
            mode = event.payload.get("to_mode")
            return mode if isinstance(mode, str) else None
        return None

    async def handle_stop_current(
        self, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if active is None:
            return ControllerResponse("当前没有活跃对话。")

        stopped_items: list[str] = []

        # Abort the active Codex task if any
        if active.active_codex_task_id:
            try:
                task = self._service.get_task(active.active_codex_task_id)
                if task.active_turn_id and task.codex_thread_id:
                    try:
                        await self._backend.interrupt_turn(
                            task.codex_thread_id, task.active_turn_id
                        )
                    except Exception as exc:
                        logger.warning("interrupt_turn failed: %s", exc)
                self._service.abort_task(active.active_codex_task_id)
                stopped_items.append("GPT 开发工程师执行")
            except KeyError:
                pass

        # Interrupt the active Claude run if any
        if active.active_claude_run_id:
            if self._claude is not None:
                try:
                    self._claude.interrupt()
                except Exception as exc:
                    logger.warning("Claude interrupt failed: %s", exc)
            self._ledger.update_agent_run_status(
                active.active_claude_run_id, "aborted"
            )
            stopped_items.append(f"DeepSeek 开发工程师运行 #{active.active_claude_run_id}")

        detail = "；".join(stopped_items) if stopped_items else "无活跃执行"
        return ControllerResponse(
            f"对话「{active.title}」已停止。\n{detail}"
        )

    async def handle_switch_workspace(
        self, command: SwitchWorkspaceCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if active is None:
            return ControllerResponse("当前没有活跃对话。请先创建对话。")

        try:
            self._service.get_workspace(command.workspace_alias)
        except Exception:
            return ControllerResponse(
                f"工作区 '{command.workspace_alias}' 不存在。发送 /workspaces 查看可用工作区。"
            )

        updated = self._ledger.set_conversation_workspace(
            active.id, command.workspace_alias
        )
        return ControllerResponse(
            f"对话「{updated.title}」工作区已切换至 {command.workspace_alias}。"
        )

    async def handle_current_workspace(
        self, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Show current workspace with quick-switch buttons — the workspace
        indicator the user wants above the chat input."""
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)
        workspaces = list(self._service._workspaces.values())

        active_alias = active.workspace_alias if active is not None else ""
        title = active.title if active is not None else ""

        return ControllerResponse(
            render_current_workspace(
                workspaces,
                active_alias=active_alias,
                conversation_title=title,
            ),
            buttons=self._workspace_selection_buttons(
                workspaces, active_alias=active_alias
            ),
        )

    async def handle_claude_permission(
        self,
        command: ClaudePermissionCommand,
        ctx: dict[str, Any] | None = None,
    ) -> ControllerResponse:
        current = self._current_claude_permission_mode()

        if command.mode_name:
            try:
                current = self._set_claude_permission_mode(command.mode_name)
            except ValueError as exc:
                return ControllerResponse(
                    f"{exc}\n\n{render_claude_permission_status(current)}",
                    buttons=build_claude_permission_buttons(current),
                )

        text = render_claude_permission_status(current)
        if command.mode_name:
            text = f"已切换 DeepSeek 开发工程师权限模式。\n\n{text}"
        if self._claude is None or not getattr(self._claude, "enabled", False):
            text += "\n\n提示：DeepSeek 开发工程师当前未启用，此设置会在启用后生效。"
        return ControllerResponse(
            text,
            buttons=build_claude_permission_buttons(current),
        )

    def _current_claude_permission_mode(self) -> str:
        if self._claude_permission_state is not None:
            return self._claude_permission_state.get()
        if self._claude is not None and hasattr(self._claude, "permission_mode"):
            return str(getattr(self._claude, "permission_mode"))
        return DEFAULT_CLAUDE_PERMISSION_MODE

    def _set_claude_permission_mode(self, mode_name: str) -> str:
        if self._claude_permission_state is None:
            self._claude_permission_state = ClaudePermissionState(
                self._current_claude_permission_mode()
            )
        current = self._claude_permission_state.set(mode_name)
        if self._claude is not None and hasattr(self._claude, "set_permission_mode"):
            self._claude.set_permission_mode(current)
        if self._ledger is not None and hasattr(self._ledger, "set_runtime_setting"):
            self._ledger.set_runtime_setting(RUNTIME_CLAUDE_PERMISSION_MODE_KEY, current)
        return current

    def render_engineer_model_settings(
        self, role_id: str | None = None
    ) -> ControllerResponse:
        assignments = self._engineer_model_assignments()
        available_profiles = self._available_engineer_model_profiles()
        if role_id is not None and role_id not in assignments:
            return ControllerResponse("没有这个工程师配置。")

        if role_id is None:
            lines = ["工程师大模型", ""]
            for role in ordered_team_roles(assignments):
                profiles = (
                    "跟随开发工程师"
                    if role == "tester"
                    else self._format_engineer_profiles(assignments[role])
                )
                lines.append(f"{role_display_name(role)}：{profiles}")
            lines.extend([
                "",
                "选择工程师后可调整使用的大模型；测试工程师跟随开发工程师。",
            ])
            buttons = [
                [{
                    "text": role_display_name(role),
                    "callback_data": f"settings:engineer_models:{role}",
                }]
                for role in ordered_team_roles(assignments)
                if role != "tester"
            ]
            buttons.append([{
                "text": "返回设置",
                "callback_data": "settings:root",
            }])
            return ControllerResponse("\n".join(lines), buttons=buttons)

        if role_id == "tester":
            return ControllerResponse(
                "测试工程师大模型\n\n"
                "当前：跟随开发工程师\n"
                "说明：个人开发模式下，测试工程师不单独开启模型会话，"
                "会使用本轮开发工程师的执行上下文和测试证据。",
                buttons=[[
                    {
                        "text": "返回工程师大模型",
                        "callback_data": "settings:engineer_models",
                    }
                ]],
            )

        selected = set(assignments[role_id])
        mode = "多选" if is_multi_select_role(role_id) else "单选"
        lines = [
            f"{role_display_name(role_id)}大模型",
            "",
            f"当前：{self._format_engineer_profiles(assignments[role_id])}",
            f"模式：{mode}",
        ]
        buttons: list[list[dict[str, str]]] = []
        for profile in available_profiles:
            marker = "✅ " if profile in selected else ""
            buttons.append([{
                "text": marker + self._engineer_model_profile_label(profile),
                "callback_data": f"settings:engineer_models:{role_id}:{profile}",
            }])
        buttons.append([{
            "text": "返回工程师大模型",
            "callback_data": "settings:engineer_models",
        }])
        return ControllerResponse("\n".join(lines), buttons=buttons)

    def set_engineer_model_assignment(
        self, role_id: str, profile_id: str
    ) -> ControllerResponse:
        assignments = self._engineer_model_assignments()
        if role_id not in assignments:
            return ControllerResponse("没有这个工程师配置。")
        if role_id == "tester":
            response = self.render_engineer_model_settings(role_id)
            return ControllerResponse(
                "测试工程师跟随开发工程师，不单独设置大模型。\n\n"
                f"{response.text}",
                response.buttons,
            )

        available_profiles = self._available_engineer_model_profiles()
        if profile_id not in available_profiles:
            return ControllerResponse("没有这个大模型选项。")

        current = list(assignments[role_id])
        if is_multi_select_role(role_id):
            if profile_id in current:
                if len(current) == 1:
                    text = "开发工程师至少保留一个大模型。"
                    response = self.render_engineer_model_settings(role_id)
                    return ControllerResponse(
                        f"{text}\n\n{response.text}", response.buttons
                    )
                current.remove(profile_id)
            else:
                current.append(profile_id)
            next_profiles = tuple(current)
        else:
            next_profiles = (profile_id,)

        next_profiles = normalize_assignment(
            role_id,
            next_profiles,
            available_profiles,
            assignments[role_id],
        )
        self._apply_engineer_model_assignment(role_id, next_profiles)
        if self._ledger is not None and hasattr(self._ledger, "set_runtime_setting"):
            self._ledger.set_runtime_setting(
                runtime_assignment_key(role_id),
                encode_assignment(next_profiles),
            )
        response = self.render_engineer_model_settings(role_id)
        return ControllerResponse(f"已更新。\n\n{response.text}", response.buttons)

    def _available_engineer_model_profiles(self) -> tuple[str, ...]:
        return ordered_model_profiles(self._adaptive_team_model_profiles)

    def _engineer_model_assignments(self) -> dict[str, tuple[str, ...]]:
        return {
            "director": (self._director_model_profile,),
            "investigator": (self._investigator_model_profile,),
            "architect": (self._architect_model_profile,),
            "implementer": tuple(self._implementer_model_profiles),
            "tester": (self._tester_model_profile,),
            "auditor": (self._auditor_model_profile,),
        }

    def _apply_engineer_model_assignment(
        self, role_id: str, profiles: tuple[str, ...]
    ) -> None:
        if role_id == "director":
            self._director_model_profile = profiles[0]
        elif role_id == "investigator":
            self._investigator_model_profile = profiles[0]
        elif role_id == "architect":
            self._architect_model_profile = profiles[0]
        elif role_id == "implementer":
            self._implementer_model_profiles = profiles
        elif role_id == "tester":
            self._tester_model_profile = profiles[0]
        elif role_id == "auditor":
            self._auditor_model_profile = profiles[0]

    def _format_engineer_profiles(self, profiles: tuple[str, ...]) -> str:
        return "、".join(
            self._engineer_model_profile_label(profile) for profile in profiles
        )

    def _engineer_model_profile_label(self, profile_id: str) -> str:
        provider = self._adaptive_team_model_profiles.get(profile_id, profile_id).lower()
        if provider == "codex":
            return "codex-gpt5.5"
        if provider == "claude":
            model = "deepseek-v4-pro"
            config = getattr(self._claude, "_config", None)
            if config is not None and getattr(config, "model", None):
                model = str(getattr(config, "model"))
            return f"claude-{model}"
        return f"{provider}-{profile_id}"

    async def handle_model(
        self, command: ModelCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if command.model_name:
            if active is None:
                return ControllerResponse("当前没有活跃对话。请先创建对话。")
            self._ledger._conn.execute(
                "UPDATE conversation_sessions SET current_model = ?, updated_at = ? WHERE id = ?",
                (command.model_name, datetime.now(timezone.utc).isoformat(), active.id),
            )
            self._ledger._conn.commit()
            return ControllerResponse(
                f"对话「{active.title}」模型偏好已保存为 {command.model_name}。"
                f"\n注意：模型切换需要后端支持，当前仅为偏好保存。"
            )
        else:
            current = active.current_model if active else ""
            if current:
                return ControllerResponse(f"当前偏好模型：{current}\n使用 /model <name> 切换。")
            return ControllerResponse("当前未设置偏好模型。使用 /model <name> 切换。")

    async def handle_exec_mode(
        self, command: ExecModeCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            return ControllerResponse("当前没有活跃工作台。请先发送 /new。")

        mode_aliases = {
            "orchestrated": ConversationMode.CHIEF_ENGINEER.value,
            "chief_engineer": ConversationMode.CHIEF_ENGINEER.value,
            "auto": ConversationMode.CHIEF_ENGINEER.value,
            "codex": ConversationMode.CODEX_DIRECT.value,
            "codex_direct": ConversationMode.CODEX_DIRECT.value,
            "claude": ConversationMode.CLAUDE_DIRECT.value,
            "claude_direct": ConversationMode.CLAUDE_DIRECT.value,
        }
        target_mode = mode_aliases.get(command.mode_name)
        if target_mode is None:
            return ControllerResponse(
                "未知执行模式。可选：orchestrated、codex_direct、claude_direct。"
            )

        updated = self._ledger.set_active_conversation_mode(active.id, target_mode)
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(updated.id),
            correlation_id=self._new_correlation_id(),
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.USER,
            payload={
                "chat_id": updated.chat_id,
                "conversation_id": updated.id,
                "from_mode": active.mode,
                "to_mode": updated.mode,
            },
            occurred_at=now_iso(),
            conversation_id=updated.id,
        ))
        mode_label = MODE_LABELS.get(updated.mode, updated.mode)
        return ControllerResponse(
            f"已切换执行模式：{mode_label}\n"
            "直接发消息会按这个模式继续当前工作台。"
        )

    # --- Conversation callback handler ---

    async def handle_conversation_callback(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """Route conversation inline button callbacks (conv:* protocol)."""
        try:
            convo = self._ledger.get_conversation(callback.conversation_id)
        except KeyError:
            return ControllerResponse("对话不存在或已被删除。")

        # --- Staged-auto callback actions ---
        if callback.action == AUTO_ROUTE_DIAGNOSE:
            return await self._handle_auto_route_analysis(callback, route_kind="bug")
        elif callback.action == AUTO_ROUTE_DESIGN:
            return await self._handle_auto_route_analysis(callback, route_kind="feature")
        elif callback.action == AUTO_ROUTE_CODEX_EXECUTE:
            return await self._handle_auto_route_direct(callback, target="codex")
        elif callback.action == AUTO_ROUTE_CLAUDE_EXECUTE:
            return await self._handle_auto_route_direct(callback, target="claude")
        elif callback.action == AUTO_FINAL_PLAN:
            return await self._handle_auto_final_plan(callback)
        elif callback.action == AUTO_SHOW_DRAFT:
            orch_run = self._latest_active_auto_run(callback.conversation_id)
            if orch_run and orch_run.last_codex_analysis:
                return ControllerResponse(
                    f"当前方案：\n\n{orch_run.last_codex_analysis[:3500]}",
                    buttons=self._auto_stage_buttons(
                        callback.conversation_id, orch_run.current_step,
                        orch_run=orch_run,
                        last_codex_analysis=orch_run.last_codex_analysis or "",
                    ),
                )
            return ControllerResponse("暂无方案草稿。")
        elif callback.action == AUTO_CANCEL:
            return await self._handle_auto_cancel(callback)
        elif callback.action == AUTO_SEND_TO_CLAUDE:
            return await self._handle_auto_send_to_claude(callback)
        elif callback.action == AUTO_SEND_TO_CODEX:
            return await self._handle_auto_send_to_codex(callback)
        elif callback.action == AUTO_CONTINUE_CONTEXT:
            # Re-enter collecting_context from draft_ready or retry_ready
            orch_run = self._latest_active_auto_run(callback.conversation_id)
            if orch_run is not None and orch_run.current_step in (
                AUTO_DRAFT_READY, AUTO_RETRY_READY
            ):
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="running",
                    current_step=AUTO_COLLECTING_CONTEXT,
                )
                return ControllerResponse(
                    "已回到上下文收集阶段。请补充信息，然后点「生成最终方案」。",
                    buttons=self._auto_stage_buttons(
                        callback.conversation_id,
                        AUTO_COLLECTING_CONTEXT,
                        orch_run=orch_run,
                    ),
                )
            return ControllerResponse(
                "当前不在方案就绪阶段，无法回到上下文收集。"
            )
        elif callback.action == AUTO_REWRITE_PLAN:
            return await self._handle_auto_final_plan(callback)
        elif callback.action == AUTO_CODEX_TAKEOVER:
            return await self._handle_auto_codex_takeover(callback)
        elif callback.action == AUTO_CLOSE:
            return await self._handle_auto_close(callback)
        elif callback.action == AUTO_CODEX_VERIFY:
            return await self._handle_auto_codex_verify(callback)
        elif callback.action == AUTO_SEND_REPAIR_TO_CLAUDE:
            return await self._handle_auto_send_repair_to_claude(callback)
        elif callback.action == AUTO_REWRITE_REPAIR:
            # Re-verify to regenerate repair prompt
            return await self._handle_auto_codex_verify(callback)
        elif callback.action == AUTO_INTERRUPT_CLAUDE:
            # Interrupt the active Claude run
            if convo.active_claude_run_id and self._claude is not None:
                try:
                    self._claude.interrupt()
                except Exception:
                    pass
            return ControllerResponse("已请求打断 DeepSeek 开发工程师。", buttons=[[{
                "text": "查看状态",
                "callback_data": encode_conversation_callback(callback.conversation_id, STATUS),
            }]])
        elif callback.action == AUTO_VIEW_DIFF:
            response = await self.handle(
                "/diff",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
            return ControllerResponse(
                response.text,
                buttons=self._team_status_buttons(callback.conversation_id),
                already_rendered=response.already_rendered,
            )
        elif callback.action == AUTO_VIEW_STATUS:
            return await self.handle(
                "/status",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == TEAM_VIEW_STATUS:
            return await self._handle_team_view_status(callback)
        elif callback.action == TEAM_VIEW_ARTIFACTS:
            return await self._handle_team_view_artifacts(callback)

        # --- Carryover callback actions ---
        if callback.action == CARRY_START:
            return await self._handle_carry_start(convo)
        elif callback.action == CARRY_SHOW:
            return await self._handle_carry_show(convo)
        elif callback.action == CARRY_REFRESH:
            return await self._handle_carry_refresh(convo)
        elif callback.action == CARRY_CANCEL:
            return await self._handle_carry_cancel(convo)

        # --- Original callback actions ---
        if callback.action == RESTORE_WORKBENCH:
            return await self._handle_restore_workbench(callback.conversation_id)

        if callback.action == DIFF:
            return await self.handle(
                "/diff",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == VERIFY:
            return await self.handle(
                "/verify",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == RETRY:
            # Re-run the orchestrator with the conversation's last goal
            orch_runs = self._ledger.list_orchestration_runs(
                callback.conversation_id, limit=1
            )
            if orch_runs:
                goal = orch_runs[0].goal
                return await self.handle(
                    f"/auto {goal}",
                    {"chat_id": convo.chat_id, "user_id": convo.user_id},
                )
            return ControllerResponse("没有找到可重试的编排运行。")
        elif callback.action == CONTINUE:
            return ControllerResponse(
                f"继续工作台「{convo.title}」。\n"
                "直接发消息即可继续，不会新开工作台。"
            )
        elif callback.action == STATUS:
            return await self.handle(
                "/status",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == NEW_CONVO:
            return await self.handle(
                "/new",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        else:
            return ControllerResponse(f"未知的对话操作：{callback.action}")

    async def handle_workspace_busy_callback(
        self, action: str, conversation_id: int
    ) -> ControllerResponse:
        """Handle workspace busy inline button callbacks."""
        cid = self._new_correlation_id()

        try:
            convo = self._ledger.get_conversation(conversation_id)
        except KeyError:
            return ControllerResponse("对话不存在或已被删除。")
        workspace_alias = str(
            getattr(convo, "workspace_alias", "") or self._default_workspace
        )

        # Retrieve original message text from conversation summary
        original_text = ""
        try:
            summary = getattr(convo, "conversation_summary", "")
            marker = "[工作区忙待处理] "
            if marker in summary:
                original_text = summary.split(marker, 1)[1][:500]
        except Exception:
            pass

        if action == BUSY_APPEND:
            sent = await self._send_pending_to_current_session(convo, original_text)
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "append_to_current",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            if sent is not None:
                return sent
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.USER_CONTEXT_APPENDED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(conversation_id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.USER,
                payload={
                    "conversation_id": conversation_id,
                    "text_preview": safe_text_preview(original_text),
                    "text_length": len(original_text),
                    "conversation_state_at_append": "implementation",
                    "delivery_policy": "codex_phase_boundary_review",
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            return ControllerResponse("已追加到当前执行。当前阶段结束后由总工程师判断处理。")

        elif action == BUSY_INTERRUPT:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "interrupt_and_run_latest",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            await self._abort_active_execution(convo)
            if original_text.strip().startswith("/"):
                return await self.handle(
                    original_text,
                    {"chat_id": convo.chat_id, "user_id": convo.user_id},
                )
            return await self.handle_conversation_text(
                original_text,
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )

        elif action == BUSY_QUEUE:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "queue_new_task",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_QUEUED,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=f"queued-{conversation_id}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={
                    "goal": original_text[:500],
                    "conversation_id": conversation_id,
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            return ControllerResponse("已安排在当前执行之后，工作区空闲后自动启动。")

        elif action == BUSY_NEW_SESSION:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "new_isolated_session",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            title = default_title(self._prompt_from_pending_text(original_text))
            new_convo = self._ledger.create_conversation(
                chat_id=convo.chat_id,
                user_id=convo.user_id,
                title=title,
                mode=self._default_mode,
                workspace_alias=convo.workspace_alias,
            )
            self._ledger.update_conversation_summary(
                new_convo.id,
                trim_to_budget(
                    f"[工作区忙待处理] {original_text[:300]}",
                    500,
                ),
            )
            return ControllerResponse(
                f"已新开隔离现场：「{new_convo.title}」。当前工作区仍在执行，"
                "这句话已放到新现场里，等你选择排队或当前释放后再启动。",
                buttons=build_workspace_busy_buttons(
                    new_convo.id, agent_label="现场"
                ),
            )

        elif action == BUSY_CANCEL:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "cancel",
                         "conversation_id": conversation_id},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            return ControllerResponse("已取消。")

        return ControllerResponse("未知的操作。")

    async def process_queued_runs(self, workspace_alias: str) -> None:
        """Consume unconsumed ``run.queued`` events when workspace is free.

        Called by EventBridge after drain_workspace detects the workspace
        is no longer blocked.  Finds queued runs that haven't been started
        yet and launches them through the chief-engineer loop.
        """
        if self._store is None or self._ledger is None:
            return

        # Check if workspace is actually free.
        blocker = self._service.blocker_for_workspace(workspace_alias)
        if blocker is not None:
            return

        # Find unconsumed run.queued events: those without a later
        # run.started or run.queued.consumed for the same conversation.
        rows = self._store._conn.execute(
            """
            SELECT q.id AS queued_id, q.payload_json, q.conversation_id, q.correlation_id
            FROM runtime_events q
            WHERE q.event_type = 'run.queued'
              AND NOT EXISTS (
                SELECT 1 FROM runtime_events c
                WHERE c.conversation_id = q.conversation_id
                  AND c.id > q.id
                  AND c.event_type IN ('run.queued.consumed', 'run.started')
              )
            ORDER BY q.id ASC
            LIMIT 1
            """
        ).fetchall()

        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except Exception:
                payload = {}
            conversation_id = int(row["conversation_id"]) if row["conversation_id"] else 0
            goal = str(payload.get("goal", "") or payload.get("text_preview", ""))
            if not goal or not conversation_id:
                continue

            # Emit consumed marker first to prevent double-processing.
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_QUEUED_CONSUMED,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=f"queued-{conversation_id}",
                correlation_id=self._new_correlation_id(),
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={
                    "conversation_id": conversation_id,
                    "workspace_alias": workspace_alias,
                    "goal_preview": goal[:200],
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))

            # Get the conversation and start the chief-engineer loop.
            try:
                conv = self._ledger.get_conversation(conversation_id)
            except KeyError:
                logger.warning("Queued conversation %d not found", conversation_id)
                continue

            # Check if orchestrator is available.
            if self._orchestration_runner is None:
                logger.warning(
                    "run.queued consumer: orchestrator not available for conv %d",
                    conversation_id,
                )
                continue

            cid = self._new_correlation_id()

            orch_run = self._ledger.create_orchestration_run(
                conversation_id=conv.id, goal=goal,
            )
            codex_analysis_run = self._ledger.create_agent_run(
                conversation_id=conv.id, agent="codex", role="analysis",
            )
            self._ledger.update_agent_run_status(codex_analysis_run.id, "running")

            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_REQUESTED,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=str(orch_run.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"goal": goal, "from": "run_queued_consumer"},
                occurred_at=now_iso(),
                conversation_id=conv.id,
                orchestration_run_id=orch_run.id,
            ))

            workspace_path = str(
                self._service.get_workspace(conv.workspace_alias).path
            )
            task = self._reserve_execution_lease(
                conversation_id=conv.id,
                workspace_alias=conv.workspace_alias,
                prompt=goal,
                telegram_chat_id=conv.chat_id,
                purpose="queued_chief_engineer",
            )

            logger.info(
                "run.queued consumer: starting chief-engineer for conv %d, goal=%s",
                conversation_id, goal[:80],
            )
            self._orchestration_runner.start_chief_engineer(
                prompt=goal,
                conversation=conv,
                task_id=task.id,
                orchestration_run_id=orch_run.id,
                codex_analysis_run_id=codex_analysis_run.id,
                chat_id=conv.chat_id,
                workspace_path=workspace_path,
                codex_thread_id=(
                    getattr(conv, "codex_thread_id", "") or ""
                    if can_reuse_codex_thread(
                        getattr(conv, "codex_thread_id", "") or "",
                        getattr(conv, "codex_thread_policy", "") or "",
                        codex_thread_policy_fingerprint(self._backend),
                    )
                    else ""
                ),
                claude_session_id=getattr(conv, "claude_session_id", "") or "",
                correlation_id=cid,
            )

def _trim_result_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1].rstrip() + "…"


def _workspace_has_changes(workspace_path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", workspace_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _telegram_visible_model_summary(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if not stripped:
        return "暂无内容。"
    if not _contains_cjk(stripped) and any(char.isalpha() for char in stripped):
        return "模型返回了非中文内容，已隐藏原文；详细记录已保留在运行日志中。"
    return _trim_result_text(stripped, max_chars)
