from __future__ import annotations

import asyncio
from typing import Any

from wlcodex.relay.context import build_role_context_packet
from wlcodex.relay.envelopes import parse_role_envelope
from wlcodex.relay.events import RelayEvent, RelayEventBus
from wlcodex.relay.models import HandoffPacket, RelayTask, RelayTaskDetail
from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES, RELAY_ROLE_IDS
from wlcodex.relay.store import RELAY_ASSIGNMENT_PREFIX


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
            payload={"role": role, "native_session_id": native_session_id},
        )
        self._events.emit(
            task_id,
            "dispatch.verified",
            role=role,
            payload={"native_session_id": native_session_id},
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
            self._store.update_role_status(task_id, role, "blocked")
            self._store.update_task_status(task_id, "blocked")
            self._events.emit(
                task_id,
                "role.status",
                role=role,
                payload={"status": "blocked", "error": result.error},
            )
            return result

        envelope = result.envelope
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
            self._store.update_task_status(task_id, "completed")
            self._events.emit(
                task_id,
                "task.completed",
                role=role,
                payload={"summary": envelope.summary},
            )
            return result
        if envelope.status == "passed" and result.next_role:
            handoff = HandoffPacket(
                from_role=role,
                to_role=result.next_role,
                summary=envelope.summary,
                confirmed_facts=[],
                open_questions=envelope.open_questions,
                evidence_refs=envelope.evidence_refs,
                next_action=envelope.next_action,
            )
            self._store.save_handoff_packet(
                task_id,
                from_role=role,
                to_role=result.next_role,
                packet=handoff,
            )
            self._store.update_role_status(task_id, result.next_role, "queued")
            self._events.emit(
                task_id,
                "handoff.created",
                role=role,
                payload=handoff.to_json_dict(),
            )
            self._events.emit(
                task_id,
                "role.queued",
                role=result.next_role,
                payload={"role": result.next_role},
            )
            if dispatch_next:
                await self.dispatch_role(task_id, result.next_role)
        return result

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
        if self._project_runtime_delta(runtime_event):
            return
        if not self._is_runtime_completion(runtime_event):
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
        if not self._is_runtime_completion(runtime_event):
            return None
        text = _runtime_event_text(runtime_event)
        if not text.strip():
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
        return _runtime_event_type(runtime_event) in {
            "model.message.completed",
            "model_message_completed",
        }


def _is_runtime_delta(runtime_event: Any) -> bool:
    return _runtime_event_type(runtime_event) in {
        "model.text.delta",
        "model.reasoning.delta",
        "command.output.delta",
        "model_text_delta",
        "model_reasoning_delta",
        "command_output_delta",
    }


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
