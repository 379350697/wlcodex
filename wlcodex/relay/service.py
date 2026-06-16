from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any

from wlcodex.live_stream.models import stream_event_from_runtime
from wlcodex.relay.context import build_role_context_packet
from wlcodex.relay.envelopes import parse_role_envelope
from wlcodex.relay.events import RelayEvent, RelayEventBus
from wlcodex.relay.models import HandoffPacket, RelayTask, RelayTaskDetail
from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES, RELAY_ROLE_IDS
from wlcodex.relay.store import RELAY_ASSIGNMENT_PREFIX
from wlcodex.runtime_event_store import RuntimeEventStore


_ROUTING_DECISION_ROUTES = {
    "director_only",
    "core_relay",
    "full_relay",
    "audit_first",
    "waiting_user",
    "blocked",
}
_ROUTING_DECISION_RISKS = {"low", "medium", "high", "critical"}
_FULL_RELAY_INTENT_KEYWORDS = (
    "完整接力",
    "接力流程",
    "测试接力",
    "五角色",
    "走五",
    "测试流程",
    "按工作流",
    "完整流程",
    "验收",
    "full relay",
    "five roles",
)
_HIGH_RISK_INTENT_KEYWORDS = (
    "上线",
    "部署",
    "deploy",
    "production",
    "权限",
    "授权",
    "认证",
    "密钥",
    "secret",
    "credential",
    "迁移",
    "migration",
    "数据库",
    "schema",
    "跨模块",
    "api",
    "公开接口",
    "重构",
    "删除",
    "删",
    "delete",
    "remove",
    "rm ",
)


class RelayNativeRunWatchdog:
    def __init__(self, service: "RelayService", *, max_idle_seconds: int) -> None:
        self._service = service
        self._max_idle_seconds = max_idle_seconds

    async def scan_once(self) -> int:
        return await self._service.scan_stale_native_roles(
            max_idle_seconds=self._max_idle_seconds
        )


class RelayRuntimeEventProjector:
    def __init__(
        self,
        service: "RelayService",
        runtime_store: RuntimeEventStore,
        *,
        limit: int = 500,
    ) -> None:
        self._service = service
        self._runtime_store = runtime_store
        self._limit = limit

    async def scan_once(self) -> int:
        return await self._service.scan_active_native_runtime_events(
            self._runtime_store,
            limit=self._limit,
        )


def relay_provider_defaults_from_team_config(
    assignments: dict[str, tuple[str, ...]],
    model_profiles: dict[str, str],
    *,
    fallback_provider: str,
) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for role in RELAY_ROLE_IDS:
        profiles = assignments.get(role, ())
        profile = profiles[0] if profiles else fallback_provider
        defaults[role] = str(model_profiles.get(profile, profile) or fallback_provider)
    return defaults


class RelayService:
    def __init__(
        self,
        *,
        store: Any,
        registry: Any,
        default_provider: str = "codex",
        events: RelayEventBus | None = None,
        role_provider_defaults: dict[str, str] | None = None,
        role_skills: dict[str, tuple[str, ...]] | None = None,
        role_capabilities: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._default_provider = default_provider
        self._events = events or RelayEventBus()
        self._handled_runtime_completion_ids: set[int] = set()
        self._runtime_projection_cursors: dict[int, int] = {}
        self._runtime_tasks: set[asyncio.Task[Any]] = set()
        self._role_provider_defaults = self._normalize_assignments(
            role_provider_defaults or {},
            allow_partial=True,
        )
        self._role_skills = {
            str(role): tuple(str(item) for item in items)
            for role, items in (role_skills or {}).items()
        }
        self._role_capabilities = {
            str(role): tuple(str(item) for item in items)
            for role, items in (role_capabilities or {}).items()
        }

    def create_task(
        self,
        *,
        title: str,
        prompt: str,
        workspace: str,
        provider: str = "",
        role_providers: dict[str, str] | None = None,
    ) -> RelayTask:
        base_provider = provider or self._default_provider
        assignments = (
            self._normalize_assignments(role_providers)
            if role_providers is not None
            else self.role_provider_assignments()
        )
        task = self._store.create_task(
            title=title,
            prompt=prompt,
            workspace=workspace,
            provider=base_provider,
            role_providers=assignments,
        )
        director_job = next(job for job in self._store.get_task_detail(task.id).role_jobs if job.role == "director")
        self._events.emit(task.id, "task.created", payload={"title": title})
        self._events.emit(
            task.id,
            "role.queued",
            role="director",
            job_id=director_job.id,
            payload={"role": "director"},
        )
        self._events.emit(
            task.id,
            "artifact.created",
            role="director",
            job_id=director_job.id,
            payload={"artifact_type": "relay_board"},
        )
        return task

    def list_tasks(self, **kwargs: Any):
        return self._store.list_tasks(**kwargs)

    def config(self) -> dict[str, Any]:
        return {
            "roles": [
                {
                    "role": role,
                    "display_name": RELAY_ROLE_DISPLAY_NAMES.get(role, role),
                    "skills": list(self._role_skills.get(role, ())),
                    "capabilities": list(self._role_capabilities.get(role, ())),
                }
                for role in RELAY_ROLE_IDS
            ],
            "providers": self._registry.list_provider_summaries(),
            "assignments": self.role_provider_assignments(),
        }

    def save_config(self, assignments: dict[str, str]) -> dict[str, Any]:
        normalized = self._normalize_assignments(assignments)
        for role, provider in normalized.items():
            self._store.set_runtime_setting(
                f"{RELAY_ASSIGNMENT_PREFIX}{role}",
                provider,
            )
        return self.config()

    def role_provider_assignments(self) -> dict[str, str]:
        available = self._available_providers()
        assignments = {}
        for role in RELAY_ROLE_IDS:
            stored = self._store.get_runtime_setting(f"{RELAY_ASSIGNMENT_PREFIX}{role}")
            value = (
                stored
                or self._role_provider_defaults.get(role)
                or self._default_provider
            )
            if value not in available:
                value = self._default_available_provider()
            assignments[role] = value
        return assignments

    def get_task(self, task_id: int) -> RelayTaskDetail:
        return self._store.get_task_detail(task_id)

    def events_for_task(self, task_id: int, *, after: int = 0) -> list[RelayEvent]:
        return self._events.list_events(task_id, after=after)

    def subscribe_events(self, task_id: int) -> asyncio.Queue[RelayEvent]:
        return self._events.subscribe(task_id)

    def unsubscribe_events(
        self,
        task_id: int,
        queue: asyncio.Queue[RelayEvent],
    ) -> None:
        self._events.unsubscribe(task_id, queue)

    async def dispatch_role(self, task_id: int, role: str) -> None:
        detail = self._store.get_task_detail(task_id)
        provider_name = (
            detail.task.role_providers.get(role)
            or detail.task.provider
            or self._default_provider
        )
        try:
            provider = self._registry.get(provider_name)
        except KeyError as exc:
            await self._mark_role_fallback(
                task_id,
                role,
                provider_name=provider_name,
                provider_engine="",
                reason=str(exc),
            )
            return
        capabilities = provider.capabilities()
        if not bool(getattr(capabilities, "can_start_session", False)):
            await self._mark_role_fallback(
                task_id,
                role,
                provider_name=provider_name,
                provider_engine=str(getattr(provider, "provider_engine", "")),
                reason="provider cannot start native sessions",
            )
            return

        packet = build_role_context_packet(
            task=detail.task,
            role=role,
            board=detail.board,
            handoffs=self._store.handoffs_for_role(task_id, role),
            artifacts=detail.artifacts,
        )
        context_record = self._store.save_context_packet(task_id, role, packet)
        try:
            result = await provider.start_session(
                detail.task.workspace,
                context_record.prompt_text,
            )
        except Exception as exc:
            await self._mark_role_fallback(
                task_id,
                role,
                provider_name=provider_name,
                provider_engine=str(getattr(provider, "provider_engine", "")),
                reason=str(exc) or "provider failed to start native session",
            )
            return
        native_session_id = str(getattr(result, "native_session_id", "") or "")
        agent_run_id = _result_agent_run_id(result)
        if not _control_result_verified(result):
            await self._mark_role_fallback(
                task_id,
                role,
                provider_name=provider_name,
                provider_engine=str(getattr(provider, "provider_engine", "")),
                reason=_control_result_failure_reason(result),
            )
            return
        self._store.update_role_metadata(
            task_id,
            role,
            provider=provider_name,
            provider_engine=str(getattr(provider, "provider_engine", "")),
            native_session_id=native_session_id,
            agent_run_id=agent_run_id,
            turn_id=_result_turn_id(result),
            active_turn_id=_result_active_turn_id(result),
            turn_running=_result_turn_running(result),
            dispatch_verified=bool(native_session_id),
        )
        self._store.update_role_status(task_id, role, "streaming")
        self._events.emit(
            task_id,
            "role.streaming",
            role=role,
            payload={
                "role": role,
                "provider": provider_name,
                "native_session_id": native_session_id,
            },
        )
        self._events.emit(
            task_id,
            "dispatch.verified",
            role=role,
            payload={
                "provider": provider_name,
                "native_session_id": native_session_id,
            },
        )

    def _available_providers(self) -> set[str]:
        return {
            str(summary.get("provider") or "").strip()
            for summary in self._registry.list_provider_summaries()
            if str(summary.get("provider") or "").strip()
        }

    def _default_available_provider(self) -> str:
        available = self._available_providers()
        if self._default_provider in available:
            return self._default_provider
        return sorted(available)[0] if available else self._default_provider

    def _normalize_assignments(
        self,
        assignments: dict[str, str],
        *,
        allow_partial: bool = False,
    ) -> dict[str, str]:
        available = self._available_providers()
        if not available:
            available = {self._default_provider}
        unknown_roles = sorted(
            str(role) for role in assignments if str(role) not in RELAY_ROLE_IDS
        )
        if unknown_roles:
            raise ValueError(f"unknown relay role: {unknown_roles[0]}")
        normalized: dict[str, str] = {}
        roles = assignments.keys() if allow_partial else RELAY_ROLE_IDS
        for role in roles:
            role_id = str(role)
            provider = str(assignments.get(role_id) or self._default_available_provider()).strip()
            if provider not in available:
                raise ValueError(f"unknown relay provider: {provider}")
            normalized[role_id] = provider
        if not allow_partial:
            for role in RELAY_ROLE_IDS:
                normalized.setdefault(role, self._default_available_provider())
        return normalized

    async def handle_role_output(
        self,
        task_id: int,
        role: str,
        output: str,
        *,
        dispatch_next: bool = True,
    ):
        result = parse_role_envelope(output)
        if not result.ok or result.envelope is None:
            if dispatch_next and await self._retry_role_envelope_format(
                task_id,
                role,
                error=result.error or "invalid role envelope",
                output=output,
            ):
                return result
            if dispatch_next and await self._recover_director_routing_decision(
                task_id,
                role,
                error=result.error or "invalid role envelope",
                output=output,
            ):
                return result
            self._block_role_with_error(
                task_id,
                role,
                result.error or "invalid role envelope",
                output=output,
            )
            return result

        envelope = result.envelope
        if envelope.artifact_type == "routing_decision":
            return await self._handle_routing_decision(
                task_id,
                role,
                output,
                result.payload,
                dispatch_next=dispatch_next,
            )
        if role == "director" and not self._store.get_task_detail(task_id).routing_decision:
            self._block_role_with_error(
                task_id,
                role,
                (
                    "director must produce routing_decision before "
                    f"{envelope.artifact_type}"
                ),
                output=output,
            )
            return result
        self._events.emit(
            task_id,
            "role.envelope",
            role=role,
            payload=envelope.to_json_dict(),
        )
        self._store.save_artifact(
            task_id,
            role,
            envelope.artifact_type,
            {
                **envelope.to_json_dict(),
                "output": output,
                "open_questions": envelope.open_questions,
            },
            summary=envelope.summary,
        )
        self._store.update_role_status(
            task_id,
            role,
            "passed" if envelope.status == "passed" else envelope.status,
        )
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": "passed" if envelope.status == "passed" else envelope.status},
        )
        if envelope.status == "blocked":
            self._store.update_task_status(task_id, "blocked")
            return result
        if envelope.status == "waiting":
            self._store.update_task_status(task_id, "waiting_user")
            return result
        if envelope.status == "failed":
            self._store.update_task_status(task_id, "failed")
            return result
        if (
            envelope.status == "passed"
            and role == "director"
            and envelope.artifact_type == "final_summary"
            and not envelope.handoff_to
        ):
            detail = self._store.get_task_detail(task_id)
            completion_error = self._final_summary_completion_error(detail)
            if completion_error:
                self._block_role_with_error(task_id, role, completion_error, output=output)
                return result
            self._store.update_task_status(task_id, "completed")
            self._events.emit(
                task_id,
                "task.completed",
                role=role,
                payload={"summary": envelope.summary},
            )
            return result
        if envelope.status == "passed" and result.next_role:
            detail = self._store.get_task_detail(task_id)
            next_role, handoff_error = self._resolve_handoff_target(
                detail,
                role,
                envelope.handoff_to,
                result.next_role,
            )
            if handoff_error:
                self._block_role_with_error(
                    task_id,
                    role,
                    handoff_error,
                    output=output,
                )
                return result
            if not next_role:
                return result
            handoff = HandoffPacket(
                from_role=role,
                to_role=next_role,
                summary=envelope.summary,
                confirmed_facts=[],
                open_questions=envelope.open_questions,
                evidence_refs=envelope.evidence_refs,
                next_action=envelope.next_action,
            )
            self._store.save_handoff_packet(
                task_id,
                from_role=role,
                to_role=next_role,
                packet=handoff,
            )
            self._store.update_role_status(task_id, next_role, "queued")
            self._events.emit(
                task_id,
                "handoff.created",
                role=role,
                payload=handoff.to_json_dict(),
            )
            self._events.emit(
                task_id,
                "role.queued",
                role=next_role,
                payload={"role": next_role},
            )
            if dispatch_next:
                await self.dispatch_role(task_id, next_role)
        return result

    async def _handle_routing_decision(
        self,
        task_id: int,
        role: str,
        output: str,
        payload: dict[str, Any],
        *,
        dispatch_next: bool,
    ):
        result = parse_role_envelope(payload)
        if role != "director":
            self._block_role_with_error(
                task_id,
                role,
                "routing_decision must be produced by director",
                output=output,
            )
            return result
        envelope = result.envelope
        if envelope is None:
            self._block_role_with_error(
                task_id,
                role,
                result.error or "invalid routing_decision",
                output=output,
            )
            return result
        detail = self._store.get_task_detail(task_id)
        decision, error = self._normalize_routing_decision(detail.task.prompt, payload)
        if error:
            self._block_role_with_error(task_id, role, error, output=output)
            return result

        artifact_payload = {
            **envelope.to_json_dict(),
            **decision,
            "output": output,
            "open_questions": envelope.open_questions,
        }
        self._store.save_artifact(
            task_id,
            role,
            "routing_decision",
            artifact_payload,
            summary=envelope.summary,
        )
        self._events.emit(
            task_id,
            "routing.decision",
            role=role,
            payload=artifact_payload,
        )
        self._events.emit(
            task_id,
            "role.envelope",
            role=role,
            payload=artifact_payload,
        )

        route = decision["route"]
        if route == "blocked":
            self._store.update_role_status(task_id, role, "blocked")
            self._store.update_task_status(task_id, "blocked")
            self._events.emit(
                task_id,
                "role.status",
                role=role,
                payload={"status": "blocked"},
            )
            return result
        if route == "waiting_user" or decision["requires_user_approval"]:
            self._store.update_role_status(task_id, role, "waiting")
            self._store.update_task_status(task_id, "waiting_user")
            self._events.emit(
                task_id,
                "role.status",
                role=role,
                payload={"status": "waiting"},
            )
            return result

        self._store.update_role_status(task_id, role, "passed")
        self._store.update_task_status(task_id, "running")
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": "passed"},
        )
        next_role = _first_required_role_after_director(
            decision["required_roles"],
            route=route,
        )
        if route == "director_only" and dispatch_next:
            self._store.update_role_status(task_id, role, "queued")
            self._events.emit(
                task_id,
                "role.queued",
                role=role,
                payload={"role": role, "reason": "director_only_final_summary"},
            )
            await self.dispatch_role(task_id, role)
            return result
        if next_role:
            handoff = HandoffPacket(
                from_role=role,
                to_role=next_role,
                summary=envelope.summary,
                confirmed_facts=[],
                open_questions=envelope.open_questions,
                evidence_refs=envelope.evidence_refs,
                next_action=envelope.next_action,
            )
            self._store.save_handoff_packet(
                task_id,
                from_role=role,
                to_role=next_role,
                packet=handoff,
            )
            self._store.update_role_status(task_id, next_role, "queued")
            self._events.emit(
                task_id,
                "handoff.created",
                role=role,
                payload=handoff.to_json_dict(),
            )
            self._events.emit(
                task_id,
                "role.queued",
                role=next_role,
                payload={"role": next_role},
            )
            if dispatch_next:
                await self.dispatch_role(task_id, next_role)
        return result

    def _normalize_routing_decision(
        self,
        prompt: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        missing = [
            field
            for field in (
                "complexity",
                "risk",
                "route",
                "required_roles",
                "acceptance_criteria",
                "stop_conditions",
                "requires_user_approval",
            )
            if field not in payload
        ]
        if missing:
            return {}, f"missing routing_decision fields: {', '.join(missing)}"
        route = str(payload.get("route") or "").strip()
        if route not in _ROUTING_DECISION_ROUTES:
            return {}, f"invalid routing route: {route}"
        risk = str(payload.get("risk") or "").strip().lower()
        if risk not in _ROUTING_DECISION_RISKS:
            return {}, f"invalid routing risk: {risk}"
        required_roles = _clean_required_roles(payload.get("required_roles"))
        if not required_roles:
            return {}, "routing_decision requires at least one role"
        if route == "director_only" and required_roles != ["director"]:
            return {}, "director_only route may only require director"
        if route == "director_only" and _user_requested_full_relay(prompt):
            return {}, "user requested full relay; director_only is not allowed"
        if route == "director_only" and _prompt_looks_high_risk(prompt):
            return {}, "high risk task cannot use director_only"
        return {
            "complexity": str(payload.get("complexity") or "").strip(),
            "risk": risk,
            "route": route,
            "required_roles": required_roles,
            "acceptance_criteria": _clean_string_list(
                payload.get("acceptance_criteria")
            ),
            "stop_conditions": _clean_string_list(payload.get("stop_conditions")),
            "requires_user_approval": bool(payload.get("requires_user_approval")),
        }, ""

    def _final_summary_completion_error(self, detail: RelayTaskDetail) -> str:
        decision = detail.routing_decision or {}
        route = str(decision.get("route") or "")
        if not route:
            return "missing routing_decision before final_summary"
        if route == "director_only":
            return ""
        required_roles = _clean_required_roles(decision.get("required_roles"))
        completed_roles = {
            str(job.role)
            for job in detail.role_jobs
            if str(job.status) in {"passed", "completed"}
        }
        missing_roles = [
            role
            for role in required_roles
            if role != "director" and role not in completed_roles
        ]
        if missing_roles:
            labels = ", ".join(RELAY_ROLE_DISPLAY_NAMES.get(role, role) for role in missing_roles)
            return f"final_summary before required relay roles completed: {labels}"
        return ""

    def _resolve_handoff_target(
        self,
        detail: RelayTaskDetail,
        role: str,
        explicit_handoff_to: str,
        proposed_next_role: str | None,
    ) -> tuple[str | None, str]:
        decision = detail.routing_decision or {}
        if not decision:
            return None, "missing routing_decision before role handoff"
        required_roles = _clean_required_roles(decision.get("required_roles"))
        if role != "director" and role not in required_roles:
            return (
                None,
                f"{RELAY_ROLE_DISPLAY_NAMES.get(role, role)} is not in routing_decision.required_roles",
            )
        if not proposed_next_role:
            return None, ""
        explicit = str(explicit_handoff_to or "").strip()
        proposed = str(proposed_next_role or "").strip()
        route = str(decision.get("route") or "")
        completed_roles = _completed_required_roles(detail, current_role=role)
        next_required = _next_uncompleted_required_role(
            required_roles,
            completed_roles=completed_roles,
            route=route,
        )
        if proposed == "director":
            if next_required:
                return (
                    None,
                    "handoff_to 总工程师 before required role "
                    f"{RELAY_ROLE_DISPLAY_NAMES.get(next_required, next_required)} completed",
                )
            return "director", ""
        if proposed not in required_roles:
            if not explicit and not next_required:
                return "director", ""
            return (
                None,
                f"handoff_to {RELAY_ROLE_DISPLAY_NAMES.get(proposed, proposed)} is not in routing_decision.required_roles",
            )
        if next_required and proposed != next_required:
            return (
                None,
                f"handoff_to {RELAY_ROLE_DISPLAY_NAMES.get(proposed, proposed)} before required role "
                f"{RELAY_ROLE_DISPLAY_NAMES.get(next_required, next_required)} completed",
            )
        return proposed, ""

    def _block_role_with_error(
        self,
        task_id: int,
        role: str,
        reason: str,
        *,
        output: str = "",
    ) -> None:
        self._store.save_artifact(
            task_id,
            role,
            "role_error",
            {
                "error": reason,
                "output": output,
            },
            summary=reason,
        )
        self._store.update_role_status(task_id, role, "blocked")
        self._store.update_task_status(task_id, "blocked")
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": "blocked", "error": reason},
        )

    async def _retry_role_envelope_format(
        self,
        task_id: int,
        role: str,
        *,
        error: str,
        output: str,
    ) -> bool:
        detail = self._store.get_task_detail(task_id)
        if _has_format_retry(detail.artifacts, role):
            return False
        job = next((job for job in detail.role_jobs if job.role == role), None)
        if job is None or not job.provider:
            return False
        try:
            provider = self._registry.get(job.provider)
        except KeyError:
            return False
        capabilities = provider.capabilities()
        can_continue = bool(getattr(capabilities, "can_continue_session", False))
        if not (can_continue and job.native_session_id):
            return False

        packet = build_role_context_packet(
            task=detail.task,
            role=role,
            board=detail.board,
            handoffs=self._store.handoffs_for_role(task_id, role),
            artifacts=detail.artifacts,
        )
        self._store.save_artifact(
            task_id,
            role,
            "role_error",
            {
                "error": error,
                "output": output,
                "retry_kind": "format",
            },
            summary=f"角色输出格式错误，已要求重新输出合法 JSON：{error}",
        )
        self._store.update_task_status(task_id, "running")
        self._store.update_role_status(task_id, role, "queued")
        self._events.emit(
            task_id,
            "role.retrying",
            role=role,
            payload={"retry_kind": "format", "error": error},
        )
        prompt = _role_envelope_retry_prompt(
            role=role,
            error=error,
            output=output,
            expected_output_envelope=packet.expected_output_envelope,
        )
        try:
            result = await provider.continue_session(job.native_session_id, prompt)
        except Exception:
            return False
        if not _control_result_verified(result):
            return False
        native_session_id = (
            str(getattr(result, "native_session_id", "") or "") or job.native_session_id
        )
        self._store.update_role_metadata(
            task_id,
            role,
            provider=job.provider,
            provider_engine=str(getattr(provider, "provider_engine", "")),
            native_session_id=native_session_id,
            agent_run_id=_result_agent_run_id(result),
            turn_id=_result_turn_id(result),
            active_turn_id=_result_active_turn_id(result),
            turn_running=_result_turn_running(result),
            dispatch_verified=True,
        )
        self._store.update_role_status(task_id, role, "streaming")
        self._events.emit(
            task_id,
            "role.streaming",
            role=role,
            payload={"role": role, "native_session_id": native_session_id},
        )
        self._events.emit(
            task_id,
            "dispatch.verified",
            role=role,
            payload={"native_session_id": native_session_id},
        )
        return True

    async def _recover_director_routing_decision(
        self,
        task_id: int,
        role: str,
        *,
        error: str,
        output: str,
    ) -> bool:
        if role != "director":
            return False
        detail = self._store.get_task_detail(task_id)
        if detail.routing_decision:
            return False
        recovered = _recover_routing_decision_from_invalid_output(
            detail.task.prompt,
            output,
        )
        if not recovered:
            return False
        self._store.save_artifact(
            task_id,
            role,
            "role_error",
            {
                "error": error,
                "output": output,
                "recovered_as": "routing_decision",
            },
            summary=f"总工程师路由输出格式错误，已按明确语义恢复路由：{recovered['route']}",
        )
        recovered_output = json.dumps(recovered, ensure_ascii=False, sort_keys=True)
        await self._handle_routing_decision(
            task_id,
            role,
            recovered_output,
            recovered,
            dispatch_next=True,
        )
        return True

    async def handle_role_completion_event(
        self,
        task_id: int,
        role: str,
        *,
        runtime_event_id: int,
        output: str,
    ):
        detail = self._store.get_task_detail(task_id)
        current_job = next((job for job in detail.role_jobs if job.role == role), None)
        if current_job is not None and current_job.status in {
            "passed",
            "waiting",
            "failed",
            "blocked",
            "interrupted",
        }:
            return None
        if runtime_event_id > 0 and runtime_event_id in self._handled_runtime_completion_ids:
            return None
        if runtime_event_id > 0:
            self._handled_runtime_completion_ids.add(runtime_event_id)
        return await self.handle_role_output(task_id, role, output)

    async def add_user_message(self, task_id: int, text: str) -> None:
        detail = self._store.get_task_detail(task_id)
        board = detail.board
        self._store.save_artifact(
            task_id,
            "director",
            "relay_board",
            {
                **board.to_json_dict(),
                "latest_user_input": text,
                "current_dispatch": "director",
                "next_step": "director review latest user input",
            },
            summary="User follow-up routed to director",
        )
        if detail.task.status == "waiting_user":
            self._store.update_task_status(task_id, "running")
        self._store.update_role_status(task_id, "director", "queued")
        self._events.emit(
            task_id,
            "role.queued",
            role="director",
            payload={"latest_user_input": text},
        )
        director = next(
            (job for job in detail.role_jobs if job.role == "director"),
            None,
        )
        if director and director.native_session_id and director.provider:
            try:
                provider = self._registry.get(director.provider)
            except KeyError:
                await self.dispatch_role(task_id, "director")
                return
            capabilities = provider.capabilities()
            can_steer = bool(getattr(capabilities, "can_steer_active_turn", False))
            active_turn_id = director.active_turn_id or director.turn_id
            can_continue = bool(getattr(capabilities, "can_continue_session", False))
            if (can_steer and director.turn_running and active_turn_id) or can_continue:
                refreshed = self._store.get_task_detail(task_id)
                packet = build_role_context_packet(
                    task=refreshed.task,
                    role="director",
                    board=refreshed.board,
                    handoffs=self._store.handoffs_for_role(task_id, "director"),
                    artifacts=refreshed.artifacts,
                )
                context_record = self._store.save_context_packet(
                    task_id,
                    "director",
                    packet,
                )
                try:
                    if can_steer and director.turn_running and active_turn_id:
                        result = await provider.steer_session(
                            director.native_session_id,
                            active_turn_id,
                            context_record.prompt_text,
                        )
                    else:
                        result = await provider.continue_session(
                            director.native_session_id,
                            context_record.prompt_text,
                        )
                except Exception as exc:
                    self._events.emit(
                        task_id,
                        "dispatch.fallback",
                        role="director",
                        payload={
                            "fallback_reason": str(exc)
                            or "provider failed to continue native session",
                            "provider": director.provider,
                            "fallback_action": "dispatch_role",
                        },
                    )
                    await self.dispatch_role(task_id, "director")
                    return
                if not _control_result_verified(result):
                    self._events.emit(
                        task_id,
                        "dispatch.fallback",
                        role="director",
                        payload={
                            "fallback_reason": _control_result_failure_reason(result),
                            "provider": director.provider,
                            "fallback_action": "dispatch_role",
                        },
                    )
                    await self.dispatch_role(task_id, "director")
                    return
                native_session_id = str(
                    getattr(result, "native_session_id", "")
                    or director.native_session_id
                )
                self._store.update_role_metadata(
                    task_id,
                    "director",
                    provider=director.provider,
                    provider_engine=str(getattr(provider, "provider_engine", "")),
                    native_session_id=native_session_id,
                    agent_run_id=_result_agent_run_id(result),
                    turn_id=_result_turn_id(result),
                    active_turn_id=_result_active_turn_id(result),
                    turn_running=_result_turn_running(result),
                    dispatch_verified=True,
                )
                self._store.update_role_status(task_id, "director", "streaming")
                self._events.emit(
                    task_id,
                    "role.streaming",
                    role="director",
                    payload={
                        "role": "director",
                        "native_session_id": native_session_id,
                    },
                )
                self._events.emit(
                    task_id,
                    "dispatch.verified",
                    role="director",
                    payload={"native_session_id": native_session_id},
                )
                return
        await self.dispatch_role(task_id, "director")

    def project_runtime_event(self, runtime_event: Any) -> None:
        self._project_native_event(runtime_event)
        if self._project_runtime_delta(runtime_event):
            return
        if not self._is_runtime_completion(runtime_event) and not _is_runtime_failure(
            runtime_event
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.handle_runtime_event(runtime_event))
            return
        task = loop.create_task(self.handle_runtime_event(runtime_event))
        self._runtime_tasks.add(task)
        task.add_done_callback(self._runtime_task_done)

    async def handle_runtime_event(self, runtime_event: Any):
        agent_run_id = getattr(runtime_event, "agent_run_id", None)
        if agent_run_id is None:
            return None
        mapping = self._store.find_role_by_agent_run_id(int(agent_run_id))
        if mapping is None:
            return None
        task_id, role = mapping
        event_id = int(getattr(runtime_event, "id", 0) or 0)
        if self._project_runtime_delta(runtime_event, task_id=task_id, role=role):
            return None
        if _is_runtime_failure(runtime_event):
            self._block_role_with_error(
                task_id,
                role,
                _runtime_failure_reason(runtime_event),
            )
            return None
        if not self._is_runtime_completion(runtime_event):
            return None
        text = self._runtime_completion_text(runtime_event)
        if not text.strip():
            if _is_agent_run_completed_event(runtime_event):
                self._block_role_with_error(
                    task_id,
                    role,
                    "native provider completed without assistant output",
                )
            return None
        if (
            not _is_agent_run_completed_event(runtime_event)
            and not parse_role_envelope(text).ok
        ):
            return None
        try:
            return await self.handle_role_completion_event(
                task_id,
                role,
                runtime_event_id=event_id,
                output=text,
            )
        except Exception as exc:
            reason = str(exc) or "runtime completion projector failed"
            self._store.update_role_status(task_id, role, "blocked")
            self._store.update_task_status(task_id, "blocked")
            self._events.emit(
                task_id,
                "role.status",
                role=role,
                payload={"status": "blocked", "error": reason},
            )
            return None

    async def scan_active_native_runtime_events(
        self,
        runtime_store: RuntimeEventStore,
        *,
        limit: int = 500,
    ) -> int:
        if limit <= 0:
            return 0
        projected = 0
        for detail in self._store.list_tasks(status="running"):
            try:
                task_detail = self._store.get_task_detail(detail.task_id)
            except KeyError:
                continue
            for job in task_detail.role_jobs:
                if job.status not in {"running", "streaming"} or not job.agent_run_id:
                    continue
                agent_run_id = int(job.agent_run_id)
                after_id = self._runtime_projection_cursors.get(agent_run_id, 0)
                events = runtime_store.list_by_agent_run_after(
                    agent_run_id,
                    after_id=after_id,
                    limit=limit,
                )
                for event in events:
                    self._runtime_projection_cursors[agent_run_id] = int(
                        getattr(event, "id", 0) or 0
                    )
                    self._project_native_event(
                        event,
                        task_id=task_detail.task.id,
                        role=job.role,
                    )
                    if self._project_runtime_delta(
                        event,
                        task_id=task_detail.task.id,
                        role=job.role,
                    ):
                        projected += 1
                        continue
                    if self._is_runtime_completion(event) or _is_runtime_failure(event):
                        await self.handle_runtime_event(event)
                    projected += 1
        return projected

    async def scan_stale_native_roles(
        self,
        *,
        max_idle_seconds: int,
        now: datetime | None = None,
    ) -> int:
        if max_idle_seconds <= 0:
            return 0
        current = now or datetime.now(timezone.utc)
        runtime_store = RuntimeEventStore(self._store._ledger._conn)
        changed = 0
        for detail in self._store.list_tasks(status="running"):
            try:
                task_detail = self._store.get_task_detail(detail.task_id)
            except KeyError:
                continue
            for job in task_detail.role_jobs:
                if job.status not in {"running", "streaming"} or not job.agent_run_id:
                    continue
                last_activity = self._last_native_role_activity_at(
                    runtime_store,
                    int(job.agent_run_id),
                    fallback=job.updated_at or task_detail.task.updated_at,
                )
                idle_seconds = int((current - last_activity).total_seconds())
                if idle_seconds <= max_idle_seconds:
                    continue
                last_event_id = self._last_native_role_event_id(
                    runtime_store,
                    int(job.agent_run_id),
                )
                await self._sync_stale_native_role(job)
                await asyncio.sleep(0)
                refreshed = self._store.get_task_detail(task_detail.task.id)
                refreshed_job = next(
                    (
                        candidate
                        for candidate in refreshed.role_jobs
                        if candidate.role == job.role
                    ),
                    None,
                )
                if refreshed_job is None or refreshed_job.status not in {
                    "running",
                    "streaming",
                }:
                    continue
                if await self._complete_stale_native_delta_if_ready(
                    runtime_store,
                    task_id=task_detail.task.id,
                    role=job.role,
                    agent_run_id=int(job.agent_run_id),
                    after_id=0,
                    provider_name=job.provider,
                    native_session_id=job.native_session_id,
                ):
                    changed += 1
                    continue
                completion_error = self._stale_native_completion_error(
                    runtime_store,
                    int(job.agent_run_id),
                )
                if completion_error:
                    self._block_role_with_error(
                        task_detail.task.id,
                        job.role,
                        completion_error,
                    )
                    changed += 1
                    continue
                if self._sync_produced_meaningful_runtime_event(
                    runtime_store,
                    int(job.agent_run_id),
                    after_id=last_event_id,
                ):
                    continue
                self._block_role_with_error(
                    task_detail.task.id,
                    job.role,
                    (
                        "native provider stayed running without assistant output "
                        f"for {idle_seconds}s (limit {max_idle_seconds}s)"
                    ),
                )
                changed += 1
        return changed

    def _runtime_task_done(self, task: asyncio.Task[Any]) -> None:
        self._runtime_tasks.discard(task)
        try:
            task.result()
        except Exception:
            pass

    async def interrupt(self, task_id: int, *, role: str | None = None) -> None:
        if role:
            detail = self._store.get_task_detail(task_id)
            job = next((job for job in detail.role_jobs if job.role == role), None)
            if job is not None:
                await self._interrupt_native_job(job)
            self._store.update_role_status(task_id, role, "interrupted")
            self._events.emit(
                task_id,
                "role.status",
                role=role,
                payload={"status": "interrupted"},
            )
            return
        detail = self._store.get_task_detail(task_id)
        self._store.update_task_status(task_id, "interrupted")
        for job in detail.role_jobs:
            if job.status in {"queued", "streaming", "waiting"}:
                await self._interrupt_native_job(job)
                self._store.update_role_status(task_id, job.role, "interrupted")
        self._events.emit(task_id, "task.interrupted", payload={})

    async def _mark_role_fallback(
        self,
        task_id: int,
        role: str,
        *,
        provider_name: str,
        provider_engine: str,
        reason: str,
    ) -> None:
        self._store.update_role_metadata(
            task_id,
            role,
            provider=provider_name,
            provider_engine=provider_engine,
            fallback_reason=reason,
        )
        self._store.update_role_status(task_id, role, "blocked")
        self._store.update_task_status(task_id, "blocked")
        self._events.emit(
            task_id,
            "dispatch.fallback",
            role=role,
            payload={"fallback_reason": reason, "provider": provider_name},
        )
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": "blocked", "error": reason},
        )

    async def _interrupt_native_job(self, job: Any) -> None:
        if not job.native_session_id or not job.provider:
            return
        try:
            provider = self._registry.get(job.provider)
        except KeyError:
            return
        interrupt = getattr(provider, "interrupt_session", None)
        if not callable(interrupt):
            return
        await interrupt(job.native_session_id)

    def _project_native_event(
        self,
        runtime_event: Any,
        *,
        task_id: int | None = None,
        role: str = "",
    ) -> bool:
        agent_run_id = getattr(runtime_event, "agent_run_id", None)
        if task_id is None:
            if agent_run_id is None:
                return False
            mapping = self._store.find_role_by_agent_run_id(int(agent_run_id))
            if mapping is None:
                return False
            task_id, role = mapping
        stream_event = stream_event_from_runtime(runtime_event)
        self._events.emit(
            int(task_id),
            "role.native_event",
            role=role,
            payload={
                "role": role,
                "agent_run_id": agent_run_id,
                "runtime_event_id": int(getattr(runtime_event, "id", 0) or 0),
                "kind": stream_event.kind,
                "native_event": stream_event.to_json_dict(),
                "payload": stream_event.payload,
            },
        )
        return True

    def _project_runtime_delta(
        self,
        runtime_event: Any,
        *,
        task_id: int | None = None,
        role: str = "",
    ) -> bool:
        if not _is_runtime_delta(runtime_event):
            return False
        agent_run_id = getattr(runtime_event, "agent_run_id", None)
        if task_id is None:
            if agent_run_id is None:
                return True
            mapping = self._store.find_role_by_agent_run_id(int(agent_run_id))
            if mapping is None:
                return True
            task_id, role = mapping
        delta = _runtime_event_text(runtime_event)
        self._events.emit(
            int(task_id),
            "role.output_delta",
            role=role,
            payload={
                "role": role,
                "agent_run_id": agent_run_id,
                "runtime_event_id": int(getattr(runtime_event, "id", 0) or 0),
                "delta": delta,
            },
        )
        return True

    @staticmethod
    def _is_runtime_completion(runtime_event: Any) -> bool:
        if _is_turn_completed_activity(runtime_event):
            return True
        if _is_agent_run_completed_event(runtime_event):
            return True
        return _runtime_event_type(runtime_event) in {
            "model.message.completed",
            "model_message_completed",
        }

    def _runtime_completion_text(self, runtime_event: Any) -> str:
        text = _runtime_event_text(runtime_event)
        if text.strip():
            return text
        if not (
            _is_turn_completed_activity(runtime_event)
            or _is_agent_run_completed_event(runtime_event)
        ):
            return ""
        agent_run_id = getattr(runtime_event, "agent_run_id", None)
        if agent_run_id is None:
            return ""
        event_id = int(getattr(runtime_event, "id", 0) or 0)
        runtime_store = RuntimeEventStore(self._store._ledger._conn)
        if event_id > 0:
            events = runtime_store.list_by_agent_run_before(
                int(agent_run_id),
                before_id=event_id,
                limit=5000,
            )
        else:
            events = runtime_store.list_by_agent_run_tail(int(agent_run_id), limit=5000)
        completed = _completed_role_envelope_text(events)
        if completed is not None:
            return completed
        return "".join(
            _runtime_event_text(event)
            for event in events
            if _is_runtime_model_text_delta(event)
        )

    async def _complete_stale_native_delta_if_ready(
        self,
        runtime_store: RuntimeEventStore,
        *,
        task_id: int,
        role: str,
        agent_run_id: int,
        after_id: int,
        provider_name: str = "",
        native_session_id: str = "",
    ) -> bool:
        if after_id > 0:
            events = runtime_store.list_by_agent_run_after(
                agent_run_id,
                after_id=after_id,
                limit=500,
            )
        else:
            events = runtime_store.list_by_agent_run_tail(agent_run_id, limit=5000)
        completed = _completed_role_envelope_event(events)
        if completed is not None:
            await self.handle_role_completion_event(
                task_id,
                role,
                runtime_event_id=int(getattr(completed, "id", 0) or 0),
                output=_runtime_event_text(completed),
            )
            return True
        read_session_output = await self._read_native_session_role_envelope(
            provider_name=provider_name,
            native_session_id=native_session_id,
        )
        if read_session_output:
            await self.handle_role_completion_event(
                task_id,
                role,
                runtime_event_id=0,
                output=read_session_output,
            )
            return True
        delta_events = [event for event in events if _is_runtime_model_text_delta(event)]
        if not delta_events:
            return False
        output = "".join(_runtime_event_text(event) for event in delta_events)
        if not parse_role_envelope(output).ok:
            return False
        runtime_event_id = int(getattr(delta_events[-1], "id", 0) or 0)
        await self.handle_role_completion_event(
            task_id,
            role,
            runtime_event_id=runtime_event_id,
            output=output,
        )
        return True

    def _stale_native_completion_error(
        self,
        runtime_store: RuntimeEventStore,
        agent_run_id: int,
    ) -> str:
        events = runtime_store.list_by_agent_run_tail(agent_run_id, limit=5000)
        completion = next(
            (
                event
                for event in reversed(events)
                if self._is_runtime_completion(event) or _is_runtime_failure(event)
            ),
            None,
        )
        if completion is None:
            return ""
        if _is_runtime_failure(completion):
            return _runtime_failure_reason(completion)
        text = self._runtime_completion_text(completion)
        if not text.strip():
            return "native provider completed without assistant output"
        result = parse_role_envelope(text)
        if result.ok:
            return ""
        return (
            "native provider completed without valid relay envelope: "
            f"{result.error or 'invalid role envelope'}"
        )

    async def _sync_stale_native_role(self, job: Any) -> None:
        if not job.provider or not job.native_session_id:
            return
        try:
            provider = self._registry.get(job.provider)
        except KeyError:
            return
        sync_session = getattr(provider, "sync_session", None)
        if sync_session is None:
            return
        try:
            await sync_session(job.native_session_id)
        except Exception:
            return

    async def _read_native_session_role_envelope(
        self,
        *,
        provider_name: str,
        native_session_id: str,
    ) -> str:
        if not provider_name or not native_session_id:
            return ""
        try:
            provider = self._registry.get(provider_name)
        except KeyError:
            return ""
        read_session = getattr(provider, "read_session", None)
        if read_session is None:
            return ""
        try:
            payload = await read_session(native_session_id)
        except Exception:
            return ""
        return _session_assistant_role_envelope_text(payload)

    @staticmethod
    def _last_native_role_activity_at(
        runtime_store: RuntimeEventStore,
        agent_run_id: int,
        *,
        fallback: str,
    ) -> datetime:
        events = runtime_store.list_by_agent_run_tail(agent_run_id, limit=1)
        if events:
            return _parse_runtime_datetime(events[-1].occurred_at)
        return _parse_runtime_datetime(fallback)

    @staticmethod
    def _last_native_role_event_id(
        runtime_store: RuntimeEventStore,
        agent_run_id: int,
    ) -> int:
        events = runtime_store.list_by_agent_run_tail(agent_run_id, limit=1)
        if not events:
            return 0
        return int(getattr(events[-1], "id", 0) or 0)

    @staticmethod
    def _sync_produced_meaningful_runtime_event(
        runtime_store: RuntimeEventStore,
        agent_run_id: int,
        *,
        after_id: int,
    ) -> bool:
        events = runtime_store.list_by_agent_run_after(
            agent_run_id,
            after_id=after_id,
            limit=500,
        )
        for event in events:
            if _is_runtime_failure(event) or RelayService._is_runtime_completion(event):
                return True
            if _is_runtime_model_text_delta(event) and _runtime_event_text(event).strip():
                return True
        return False


def _is_runtime_delta(runtime_event: Any) -> bool:
    return _runtime_event_type(runtime_event) in {
        "model.text.delta",
        "model.reasoning.delta",
        "command.output.delta",
        "model_text_delta",
        "model_reasoning_delta",
        "command_output_delta",
    }


def _parse_runtime_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _clean_required_roles(values: Any) -> list[str]:
    roles = []
    for value in _clean_string_list(values):
        if value not in RELAY_ROLE_IDS:
            continue
        if value not in roles:
            roles.append(value)
    return roles


def _first_required_role_after_director(
    required_roles: list[str],
    *,
    route: str,
) -> str | None:
    if route == "audit_first" and "auditor" in required_roles:
        return "auditor"
    for relay_role in RELAY_ROLE_IDS:
        if relay_role != "director" and relay_role in required_roles:
            return relay_role
    return None


def _ordered_required_roles(required_roles: list[str], *, route: str) -> list[str]:
    ordered = [role for role in RELAY_ROLE_IDS if role in required_roles]
    if route == "audit_first" and "auditor" in ordered:
        ordered = [role for role in ordered if role != "auditor"]
        insert_at = 1 if "director" in ordered else 0
        ordered.insert(insert_at, "auditor")
    return ordered


def _completed_required_roles(
    detail: RelayTaskDetail,
    *,
    current_role: str,
) -> set[str]:
    completed = {
        str(job.role)
        for job in detail.role_jobs
        if str(job.status) in {"passed", "completed"}
    }
    if current_role:
        completed.add(current_role)
    return completed


def _next_uncompleted_required_role(
    required_roles: list[str],
    *,
    completed_roles: set[str],
    route: str,
) -> str | None:
    for role in _ordered_required_roles(required_roles, route=route):
        if role == "director":
            continue
        if role not in completed_roles:
            return role
    return None


def _has_format_retry(artifacts: list[dict[str, Any]], role: str) -> bool:
    for artifact in artifacts:
        if str(artifact.get("artifact_type") or "") != "role_error":
            continue
        if str(artifact.get("relay_role") or artifact.get("role") or "") != role:
            continue
        if str(artifact.get("retry_kind") or "") == "format":
            return True
    return False


def _role_envelope_retry_prompt(
    *,
    role: str,
    error: str,
    output: str,
    expected_output_envelope: dict[str, Any],
) -> str:
    display_role = RELAY_ROLE_DISPLAY_NAMES.get(role, role)
    expected_json = json.dumps(
        expected_output_envelope,
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        f"你刚才作为{display_role}输出的接力结果不是合法 role_envelope JSON，"
        "服务端无法继续工作流。\n"
        f"校验错误：{error}\n\n"
        "请只重新输出一个合法 JSON object，不要 Markdown，不要解释，不要代码块。\n"
        "必须包含 required fields: status, reason, role, artifact_type, handoff_to, "
        "summary, evidence_refs, open_questions, next_action。\n"
        "不要创造新的 artifact_type；必须使用 expected_output_envelope 里的 artifact_type。\n"
        "如果这是路由决策，内部 route 字段可以继续使用协议枚举，"
        "但 summary/next_action/reason 请用中文白话描述给人看。\n\n"
        f"expected_output_envelope:\n{expected_json}\n\n"
        "上一版无效输出如下，请保留语义、修正结构：\n"
        f"{output}"
    )


def _recover_routing_decision_from_invalid_output(
    prompt: str,
    output: str,
) -> dict[str, Any] | None:
    text = f"{prompt}\n{output}".lower()
    if "routing_decision" not in text and "route" not in text:
        return None
    if _user_requested_full_relay(prompt):
        return {
            "status": "passed",
            "reason": "用户明确要求完整接力流程，格式恢复后继续调度五个角色。",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "",
            "summary": "按完整五角色接力处理：先由架构工程师审查，再交给开发、测试、审计，最后由总工程师收口。",
            "evidence_refs": ["recovered malformed director routing output"],
            "open_questions": [],
            "next_action": "调度架构工程师继续接力",
            "complexity": "high",
            "risk": "medium",
            "route": "full_relay",
            "required_roles": [
                "director",
                "architect",
                "implementer",
                "tester",
                "auditor",
            ],
            "acceptance_criteria": [
                "五个角色均参与并产出协议化结果",
                "不修改文件、不提交、不部署",
                "最终由总工程师给出中文收口总结",
            ],
            "stop_conditions": [
                "发现需要修改文件、提交或部署时停止并说明",
                "发现高风险或目标不清时停止并要求用户确认",
            ],
            "requires_user_approval": False,
        }
    if _prompt_looks_high_risk(prompt):
        return None
    if "director_only" in text or "route_only" in text or "routedirector" in text:
        return {
            "status": "passed",
            "reason": "任务为低风险直接回答，格式恢复后由总工程师直接收口。",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "",
            "summary": "由总工程师直接处理并给出最终回答。",
            "evidence_refs": ["recovered malformed director routing output"],
            "open_questions": [],
            "next_action": "由总工程师继续给出最终总结",
            "complexity": "low",
            "risk": "low",
            "route": "director_only",
            "required_roles": ["director"],
            "acceptance_criteria": ["直接回答用户问题"],
            "stop_conditions": ["需要改动文件或执行高风险操作时停止"],
            "requires_user_approval": False,
        }
    return None


def _user_requested_full_relay(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(keyword.lower() in lowered for keyword in _FULL_RELAY_INTENT_KEYWORDS)


def _prompt_looks_high_risk(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(keyword.lower() in lowered for keyword in _HIGH_RISK_INTENT_KEYWORDS)


def _is_runtime_model_text_delta(runtime_event: Any) -> bool:
    return _runtime_event_type(runtime_event) in {
        "model.text.delta",
        "model_text_delta",
    }


def _is_runtime_model_message_completed(runtime_event: Any) -> bool:
    return _runtime_event_type(runtime_event) in {
        "model.message.completed",
        "model_message_completed",
    }


def _completed_role_envelope_event(events: list[Any]) -> Any | None:
    for event in reversed(events):
        if not _is_runtime_model_message_completed(event):
            continue
        text = _runtime_event_text(event)
        if text.strip() and parse_role_envelope(text).ok:
            return event
    return None


def _completed_role_envelope_text(events: list[Any]) -> str | None:
    event = _completed_role_envelope_event(events)
    if event is None:
        return None
    return _runtime_event_text(event)


def _is_turn_completed_activity(runtime_event: Any) -> bool:
    if _runtime_event_type(runtime_event) not in {
        "agent.run.activity",
        "agent_run_activity",
    }:
        return False
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    return (
        str(payload.get("action") or "").strip() == "turn_completed"
        and str(payload.get("status") or "completed").strip() == "completed"
    )


def _is_agent_run_completed_event(runtime_event: Any) -> bool:
    return _runtime_event_type(runtime_event) in {
        "agent.run.completed",
        "agent_run_completed",
    }


def _is_runtime_failure(runtime_event: Any) -> bool:
    event_type = _runtime_event_type(runtime_event)
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    if event_type in {"agent.run.failed", "agent_run_failed"}:
        return True
    if event_type not in {"agent.run.activity", "agent_run_activity"}:
        return False
    action = str(payload.get("action") or "").strip()
    status = str(payload.get("status") or "").strip()
    return action in {"turn_failed", "run_failed"} or status == "failed"


def _runtime_failure_reason(runtime_event: Any) -> str:
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    reason = str(
        payload.get("error")
        or payload.get("message")
        or payload.get("reason")
        or payload.get("status")
        or ""
    ).strip()
    return reason or "native provider failed before producing relay output"


def _is_codex_native_app_server_event(runtime_event: Any) -> bool:
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    return (
        str(getattr(runtime_event, "source", "") or "") == "codex"
        and str(getattr(runtime_event, "actor", "") or "") == "codex_native"
        and str(payload.get("source_kind") or "") == "codex_native"
        and str(payload.get("provider") or "") == "codex"
        and str(payload.get("provider_engine") or "") == "app-server"
    )


def _runtime_event_type(runtime_event: Any) -> str:
    return str(getattr(runtime_event, "event_type", "") or "")


def _runtime_event_text(runtime_event: Any) -> str:
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    return str(
        payload.get("delta")
        or payload.get("text")
        or payload.get("message")
        or payload.get("content")
        or payload.get("output")
        or payload.get("chunk")
        or ""
    )


def _session_assistant_role_envelope_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    turns = payload.get("turns")
    if not isinstance(turns, list):
        return ""
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role") or "").strip().lower() != "assistant":
            continue
        text = str(
            turn.get("text")
            or turn.get("message")
            or turn.get("content")
            or turn.get("output")
            or ""
        )
        if text.strip() and parse_role_envelope(text).ok:
            return text
    return ""


def _result_agent_run_id(result: Any) -> int | None:
    try:
        agent_run_id = int(getattr(result, "agent_run_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return agent_run_id if agent_run_id > 0 else None


def _result_turn_id(result: Any) -> str:
    return str(getattr(result, "turn_id", "") or "")


def _result_active_turn_id(result: Any) -> str:
    return str(getattr(result, "active_turn_id", "") or _result_turn_id(result))


def _result_turn_running(result: Any) -> bool:
    return bool(getattr(result, "turn_running", False))


def _control_result_verified(result: Any) -> bool:
    status = str(getattr(result, "status", "") or "").strip().lower()
    return (
        bool(str(getattr(result, "native_session_id", "") or ""))
        and _result_agent_run_id(result) is not None
        and status not in {"blocked", "error", "failed"}
    )


def _control_result_failure_reason(result: Any) -> str:
    status = str(getattr(result, "status", "") or "").strip()
    if not str(getattr(result, "native_session_id", "") or ""):
        return f"provider returned unverified native session ({status or 'unknown status'})"
    if _result_agent_run_id(result) is None:
        return f"provider returned unverified agent run ({status or 'unknown status'})"
    return f"provider returned {status or 'unverified'} status"
