from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
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
_RELAY_MAX_TEXT_ATTACHMENTS = 5
_RELAY_MAX_TEXT_ATTACHMENT_CHARS = 80_000
_RELAY_MAX_TOTAL_TEXT_ATTACHMENT_CHARS = 180_000


def _relay_clean_image_attachments(
    images: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    if not images:
        return cleaned
    for raw in list(images)[:8]:
        if not isinstance(raw, dict):
            continue
        url = raw.get("url") or raw.get("data_url")
        if not isinstance(url, str) or not url.startswith("data:image/") or "," not in url:
            continue
        item = {"url": url}
        filename = raw.get("filename")
        if isinstance(filename, str) and filename.strip():
            item["filename"] = filename.strip()[:160]
        mime_type = raw.get("mime_type") or raw.get("mimeType")
        if isinstance(mime_type, str) and mime_type.startswith("image/"):
            item["mime_type"] = mime_type[:80]
        cleaned.append(item)
    return cleaned


def _relay_clean_text_file_attachments(
    files: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    if not files:
        return cleaned
    used_chars = 0
    for raw in list(files)[:_RELAY_MAX_TEXT_ATTACHMENTS]:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        if not isinstance(text, str):
            continue
        remaining = _RELAY_MAX_TOTAL_TEXT_ATTACHMENT_CHARS - used_chars
        if remaining <= 0:
            break
        clipped = text[: min(_RELAY_MAX_TEXT_ATTACHMENT_CHARS, remaining)]
        used_chars += len(clipped)
        filename = raw.get("filename")
        clean: dict[str, Any] = {
            "filename": (
                filename.strip()[:160]
                if isinstance(filename, str) and filename.strip()
                else "attachment.txt"
            ),
            "text": clipped,
        }
        mime_type = raw.get("mime_type") or raw.get("mimeType")
        if isinstance(mime_type, str) and mime_type.strip():
            clean["mime_type"] = mime_type.strip()[:120]
        size = raw.get("size")
        if isinstance(size, int) and size >= 0:
            clean["size"] = size
        cleaned.append(clean)
    return cleaned


def _relay_attachment_prompt_suffix(
    *,
    images: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if images:
        parts.append("用户附带图片：")
        for index, image in enumerate(images, start=1):
            filename = str(image.get("filename") or f"image-{index}")
            mime_type = str(image.get("mime_type") or "image")
            parts.append(f"- {filename} ({mime_type})")
    if files:
        parts.append("用户附带文本文件：")
        for file in files:
            filename = str(file.get("filename") or "attachment.txt")
            mime_type = str(file.get("mime_type") or "text/plain")
            parts.append(f"- {filename} ({mime_type})")
            parts.append(str(file.get("text") or ""))
    return "\n".join(parts).strip()


def _relay_user_input_with_attachments(
    text: str,
    *,
    images: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> str:
    base = str(text or "").strip()
    suffix = _relay_attachment_prompt_suffix(images=images, files=files)
    if base and suffix:
        return f"{base}\n\n{suffix}"
    return base or suffix


def _relay_attachment_payload(
    *,
    images: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if images:
        payload["images"] = images
    if files:
        payload["files"] = files
    return payload


def _relay_images_for_role(artifacts: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    if role != "director":
        return []
    for artifact in reversed(artifacts):
        artifact_type = str(artifact.get("artifact_type") or "")
        if artifact_type not in {"user_followup", "user_attachments"}:
            continue
        images = _relay_clean_image_attachments(artifact.get("images"))
        if images:
            return images
    return []


class RelayNativeRunWatchdog:
    def __init__(self, service: "RelayService", *, max_idle_seconds: int) -> None:
        self._service = service
        self._max_idle_seconds = max_idle_seconds

    async def scan_once(self) -> int:
        return await self._service.scan_stale_native_roles(max_idle_seconds=self._max_idle_seconds)


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
        configured_roles: tuple[str, ...] | None = None,
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
        clean_configured_roles = tuple(
            role for role in (configured_roles or RELAY_ROLE_IDS) if role in RELAY_ROLE_IDS
        )
        self._configured_roles = clean_configured_roles or RELAY_ROLE_IDS
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
        images: list[dict[str, Any]] | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> RelayTask:
        clean_images = _relay_clean_image_attachments(images)
        clean_files = _relay_clean_text_file_attachments(files)
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
        if clean_images or clean_files:
            self._store.save_artifact(
                task.id,
                "director",
                "user_attachments",
                {
                    "source": "initial_task",
                    "text": prompt,
                    **_relay_attachment_payload(images=clean_images, files=clean_files),
                },
                summary=", ".join(
                    [
                        *[str(image.get("filename") or "image") for image in clean_images],
                        *[str(file.get("filename") or "file") for file in clean_files],
                    ]
                )
                or "用户附件",
            )
            detail = self._store.get_task_detail(task.id)
            self._store.save_artifact(
                task.id,
                "director",
                "relay_board",
                {
                    **detail.board.to_json_dict(),
                    "latest_user_input": _relay_user_input_with_attachments(
                        prompt,
                        images=clean_images,
                        files=clean_files,
                    ),
                },
                summary="RelayBoard initialized with attachments",
            )
        director_job = next(
            job for job in self._store.get_task_detail(task.id).role_jobs if job.role == "director"
        )
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

    def today_token_stats(self) -> dict[str, int]:
        if hasattr(self._store, "today_token_stats"):
            return self._store.today_token_stats()
        return {"consumed_tokens": 0, "total_consumed_tokens": 0}

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
            "configured_roles": [
                {
                    "role": role,
                    "display_name": RELAY_ROLE_DISPLAY_NAMES.get(role, role),
                    "skills": list(self._role_skills.get(role, ())),
                    "capabilities": list(self._role_capabilities.get(role, ())),
                }
                for role in self._configured_roles
            ],
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
            value = stored or self._role_provider_defaults.get(role) or self._default_provider
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

    def add_event_projector(self, projector: Any) -> None:
        self._events.add_projector(projector)

    def remove_event_projector(self, projector: Any) -> None:
        self._events.remove_projector(projector)

    def role_for_agent_run(self, agent_run_id: int) -> tuple[int, str] | None:
        return self._store.find_role_by_agent_run_id(agent_run_id)

    async def resume_role(self, task_id: int, role: str, *, force: bool = False) -> None:
        if role not in RELAY_ROLE_IDS:
            raise ValueError(f"unknown relay role: {role}")
        detail = self._store.get_task_detail(task_id)
        job = next((candidate for candidate in detail.role_jobs if candidate.role == role), None)
        if job is None:
            raise ValueError(f"unknown relay role: {role}")
        if job.status in {"running", "streaming", "queued"} and not force:
            return
        self._store.update_task_status(task_id, "running")
        self._store.update_role_status(task_id, role, "queued")
        self._store.save_artifact(
            task_id,
            role,
            "role_resume",
            {"role": role, "previous_status": job.status},
            summary=f"{RELAY_ROLE_DISPLAY_NAMES.get(role, role)}重新派发",
        )
        self._events.emit(
            task_id,
            "role.queued",
            role=role,
            payload={"role": role, "reason": "resume_role"},
        )
        await self.dispatch_role(task_id, role)

    async def dispatch_role(
        self,
        task_id: int,
        role: str,
        *,
        prefer_continue: bool = True,
    ) -> None:
        detail = self._store.get_task_detail(task_id)
        round_id = int(getattr(detail, "current_round_id", 1) or 1)
        job = next(
            (candidate for candidate in detail.role_jobs if candidate.role == role),
            None,
        )
        provider_name = (
            detail.task.role_providers.get(role) or detail.task.provider or self._default_provider
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
        can_start = bool(getattr(capabilities, "can_start_session", False))
        can_continue = bool(getattr(capabilities, "can_continue_session", False))
        existing_native_session_id = ""
        if prefer_continue and job is not None and job.provider == provider_name:
            existing_native_session_id = str(job.native_session_id or "").strip()

        packet = build_role_context_packet(
            task=detail.task,
            role=role,
            board=detail.board,
            handoffs=self._store.handoffs_for_role(task_id, role),
            artifacts=detail.artifacts,
        )
        context_record = self._store.save_context_packet(task_id, role, packet)
        images = _relay_images_for_role(detail.artifacts, role)
        provider_kwargs = {"images": images} if images else {}
        result: Any | None = None
        if can_continue and existing_native_session_id:
            try:
                continued = await provider.continue_session(
                    existing_native_session_id,
                    context_record.prompt_text,
                    **provider_kwargs,
                )
            except Exception as exc:
                self._events.emit(
                    task_id,
                    "dispatch.fallback",
                    role=role,
                    payload={
                        "fallback_reason": str(exc) or "provider failed to continue native session",
                        "provider": provider_name,
                        "fallback_action": "start_session",
                    },
                )
            else:
                if _control_result_verified(continued):
                    result = continued
                else:
                    self._events.emit(
                        task_id,
                        "dispatch.fallback",
                        role=role,
                        payload={
                            "fallback_reason": _control_result_failure_reason(continued),
                            "provider": provider_name,
                            "fallback_action": "start_session",
                        },
                    )
        if result is None:
            if not can_start:
                await self._mark_role_fallback(
                    task_id,
                    role,
                    provider_name=provider_name,
                    provider_engine=str(getattr(provider, "provider_engine", "")),
                    reason="provider cannot start native sessions",
                )
                return
            try:
                result = await provider.start_session(
                    detail.task.workspace,
                    context_record.prompt_text,
                    **provider_kwargs,
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
        native_session_id = (
            str(getattr(result, "native_session_id", "") or "") or existing_native_session_id
        )
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
                "round_id": round_id,
            },
        )
        self._events.emit(
            task_id,
            "dispatch.verified",
            role=role,
            payload={
                "provider": provider_name,
                "native_session_id": native_session_id,
                "round_id": round_id,
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
        unknown_roles = sorted(str(role) for role in assignments if str(role) not in RELAY_ROLE_IDS)
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
            if dispatch_next and await self._recover_director_routing_decision(
                task_id,
                role,
                error=result.error or "invalid role envelope",
                output=output,
            ):
                return result
            if dispatch_next and await self._retry_role_envelope_format(
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
        detail_for_output = self._store.get_task_detail(task_id)
        if role == "director" and not detail_for_output.routing_decision:
            if envelope.handoff_to and self._ensure_followup_routing_decision(
                task_id,
                detail=detail_for_output,
                envelope_payload=result.payload,
            ):
                detail_for_output = self._store.get_task_detail(task_id)
            else:
                self._block_role_with_error(
                    task_id,
                    role,
                    (f"director must produce routing_decision before {envelope.artifact_type}"),
                    output=output,
                )
                return result
        round_id = self._store.current_round_id(task_id)
        envelope_payload = {
            **envelope.to_json_dict(),
            "round_id": round_id,
        }
        self._events.emit(
            task_id,
            "role.envelope",
            role=role,
            payload=envelope_payload,
        )
        self._store.save_artifact(
            task_id,
            role,
            envelope.artifact_type,
            {
                **envelope_payload,
                "output": output,
                "open_questions": envelope.open_questions,
            },
            summary=envelope.summary,
        )
        role_status = "passed" if envelope.status == "passed" else envelope.status
        if envelope.status == "waiting" and envelope.handoff_to:
            role_status = "passed"
        self._store.update_role_status(task_id, role, role_status)
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": role_status, "round_id": round_id},
        )
        if (
            role == "auditor"
            and envelope.status in {"blocked", "failed"}
            and envelope.handoff_to == "implementer"
        ):
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
            if next_role == "implementer":
                handoff = HandoffPacket(
                    from_role=role,
                    to_role=next_role,
                    summary=envelope.summary,
                    confirmed_facts=[],
                    open_questions=envelope.open_questions,
                    evidence_refs=envelope.evidence_refs,
                    next_action=envelope.next_action,
                )
                handoff_artifact = self._store.save_handoff_packet(
                    task_id,
                    from_role=role,
                    to_role=next_role,
                    packet=handoff,
                )
                self._store.update_task_status(task_id, "running")
                self._store.update_role_status(task_id, next_role, "queued")
                self._events.emit(
                    task_id,
                    "handoff.created",
                    role=role,
                    payload={
                        **handoff.to_json_dict(),
                        "round_id": round_id,
                        "artifact_id": int(getattr(handoff_artifact, "id", 0) or 0),
                    },
                )
                self._events.emit(
                    task_id,
                    "role.queued",
                    role=next_role,
                    payload={
                        "role": next_role,
                        "reason": "auditor_rework",
                        "round_id": round_id,
                    },
                )
                if dispatch_next:
                    await self.dispatch_role(task_id, next_role)
                return result
        if envelope.status == "blocked":
            self._store.update_task_status(task_id, "blocked")
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
                payload={"summary": envelope.summary, "round_id": round_id},
            )
            return result
        if (
            envelope.status == "passed" or (envelope.status == "waiting" and envelope.handoff_to)
        ) and result.next_role:
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
            handoff_artifact = self._store.save_handoff_packet(
                task_id,
                from_role=role,
                to_role=next_role,
                packet=handoff,
            )
            self._store.update_task_status(task_id, "running")
            self._reset_required_roles_after_handoff(
                task_id,
                detail=detail,
                next_role=next_role,
            )
            self._store.update_role_status(task_id, next_role, "queued")
            self._events.emit(
                task_id,
                "handoff.created",
                role=role,
                payload={
                    **handoff.to_json_dict(),
                    "round_id": round_id,
                    "artifact_id": int(getattr(handoff_artifact, "id", 0) or 0),
                },
            )
            self._events.emit(
                task_id,
                "role.queued",
                role=next_role,
                payload={"role": next_role, "round_id": round_id},
            )
            if dispatch_next:
                await self.dispatch_role(task_id, next_role)
            return result
        if envelope.status == "waiting":
            self._store.update_task_status(task_id, "waiting_user")
            return result
        return result

    def _ensure_followup_routing_decision(
        self,
        task_id: int,
        *,
        detail: RelayTaskDetail,
        envelope_payload: dict[str, Any],
    ) -> bool:
        current_round_id = int(getattr(detail, "current_round_id", 1) or 1)
        target_role = str(envelope_payload.get("handoff_to") or "").strip()
        if target_role not in RELAY_ROLE_IDS or target_role == "director":
            return False
        previous_decision: dict[str, Any] = {}
        for artifact in reversed(detail.artifacts):
            if str(artifact.get("artifact_type") or "") == "routing_decision":
                previous_decision = dict(artifact)
                break
        required_roles = _clean_required_roles(previous_decision.get("required_roles"))
        if target_role not in required_roles:
            required_roles = ["director", target_role]
            if target_role != "auditor":
                required_roles.append("auditor")
        required_roles = _ordered_required_roles(required_roles, route="core_relay")
        route = str(previous_decision.get("route") or "core_relay")
        if route == "director_only":
            route = "core_relay"
        artifact_payload = {
            "role": "director",
            "artifact_type": "routing_decision",
            "status": "passed",
            "reason": "接续回合沿用当前任务接力链路。",
            "summary": str(
                envelope_payload.get("summary")
                or envelope_payload.get("reason")
                or previous_decision.get("summary")
                or "接续回合进入接力处理。"
            ),
            "handoff_to": target_role,
            "next_action": str(
                envelope_payload.get("next_action") or previous_decision.get("next_action") or ""
            ),
            "evidence_refs": list(envelope_payload.get("evidence_refs") or []),
            "open_questions": list(envelope_payload.get("open_questions") or []),
            "complexity": str(previous_decision.get("complexity") or "medium"),
            "risk": str(previous_decision.get("risk") or "medium"),
            "route": route,
            "required_roles": required_roles,
            "acceptance_criteria": _clean_string_list(previous_decision.get("acceptance_criteria")),
            "stop_conditions": _clean_string_list(previous_decision.get("stop_conditions")),
            "requires_user_approval": False,
            "round_id": current_round_id,
            "output": json.dumps(envelope_payload, ensure_ascii=False),
        }
        self._store.save_artifact(
            task_id,
            "director",
            "routing_decision",
            artifact_payload,
            summary=str(artifact_payload.get("summary") or "接续回合进入接力处理。"),
        )
        self._events.emit(
            task_id,
            "routing.decision",
            role="director",
            payload=artifact_payload,
        )
        return True

    def _reset_required_roles_after_handoff(
        self,
        task_id: int,
        *,
        detail: RelayTaskDetail,
        next_role: str,
    ) -> None:
        if next_role == "director":
            return
        decision = detail.routing_decision or {}
        route = str(decision.get("route") or "")
        ordered_roles = _ordered_required_roles(
            _clean_required_roles(decision.get("required_roles")),
            route=route,
        )
        if next_role not in ordered_roles:
            return
        for relay_role in ordered_roles[ordered_roles.index(next_role) + 1 :]:
            if relay_role == "director":
                continue
            self._store.update_role_status(task_id, relay_role, "idle")

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

        round_id = self._store.current_round_id(task_id)
        artifact_payload = {
            **envelope.to_json_dict(),
            **decision,
            "round_id": round_id,
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
                payload={"status": "blocked", "round_id": round_id},
            )
            return result
        if route == "waiting_user" or decision["requires_user_approval"]:
            self._store.update_role_status(task_id, role, "waiting")
            self._store.update_task_status(task_id, "waiting_user")
            self._events.emit(
                task_id,
                "role.status",
                role=role,
                payload={"status": "waiting", "round_id": round_id},
            )
            return result

        self._store.update_role_status(task_id, role, "passed")
        self._store.update_task_status(task_id, "running")
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": "passed", "round_id": round_id},
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
                payload={
                    "role": role,
                    "reason": "director_only_final_summary",
                    "round_id": round_id,
                },
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
            handoff_artifact = self._store.save_handoff_packet(
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
                payload={
                    **handoff.to_json_dict(),
                    "round_id": round_id,
                    "artifact_id": int(getattr(handoff_artifact, "id", 0) or 0),
                },
            )
            self._events.emit(
                task_id,
                "role.queued",
                role=next_role,
                payload={"role": next_role, "round_id": round_id},
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
        raw_required_roles = _clean_required_roles(payload.get("required_roles"))
        required_roles = _normalize_required_roles_for_route(
            route,
            payload.get("required_roles"),
        )
        if not raw_required_roles:
            return {}, "routing_decision requires at least one role"
        if route == "director_only" and raw_required_roles != ["director"]:
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
            "acceptance_criteria": _clean_string_list(payload.get("acceptance_criteria")),
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
            str(job.role) for job in detail.role_jobs if str(job.status) in {"passed", "completed"}
        }
        missing_roles = [
            role for role in required_roles if role != "director" and role not in completed_roles
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
            if not explicit:
                if next_required:
                    return next_required, ""
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
        round_id = self._store.current_round_id(task_id)
        self._store.save_artifact(
            task_id,
            role,
            "role_error",
            {
                "error": reason,
                "output": output,
                "round_id": round_id,
            },
            summary=reason,
        )
        self._store.update_role_status(task_id, role, "blocked")
        self._store.update_task_status(task_id, "blocked")
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": "blocked", "error": reason, "round_id": round_id},
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
            payload={
                "role": role,
                "provider": job.provider,
                "native_session_id": native_session_id,
            },
        )
        self._events.emit(
            task_id,
            "dispatch.verified",
            role=role,
            payload={
                "provider": job.provider,
                "native_session_id": native_session_id,
            },
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

    async def _apply_native_completion_output(
        self,
        task_id: int,
        role: str,
        *,
        runtime_event_id: int,
        output: str,
        agent_run_id: int | None = None,
        completed_event: Any | None = None,
    ) -> bool:
        text = output.strip()
        if not text:
            if completed_event is not None and _is_agent_run_completed_event(completed_event):
                self._mark_native_agent_run_failed(
                    agent_run_id,
                    "native provider completed without assistant output",
                )
                self._block_role_with_error(
                    task_id,
                    role,
                    "native provider completed without assistant output",
                )
                return True
            return False
        parse_result = parse_role_envelope(text)
        if (
            not parse_result.ok
            and self._should_accept_plain_followup_response(task_id, role)
            and not _looks_like_relay_protocol_attempt(text)
        ):
            self._mark_native_agent_run_done(agent_run_id, text)
            await self._handle_plain_followup_response(
                task_id,
                role,
                runtime_event_id=runtime_event_id,
                text=text,
            )
            return True
        if not parse_result.ok and await self._recover_director_routing_decision(
            task_id,
            role,
            error=parse_result.error or "invalid role envelope",
            output=text,
        ):
            self._mark_native_agent_run_done(agent_run_id, text)
            return True
        if not parse_result.ok and _looks_like_relay_protocol_attempt(text):
            self._mark_native_agent_run_done(agent_run_id, text)
            self._block_role_with_error(
                task_id,
                role,
                parse_result.error or "invalid role envelope",
                output=text,
            )
            return True
        if not parse_result.ok:
            return False
        self._mark_native_agent_run_done(agent_run_id, text)
        try:
            await self.handle_role_completion_event(
                task_id,
                role,
                runtime_event_id=runtime_event_id,
                output=text,
            )
            return True
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
            return True

    def _mark_native_agent_run_done(
        self,
        agent_run_id: int | None,
        completion_summary: str,
    ) -> None:
        if not agent_run_id:
            return
        try:
            current = self._store._ledger.get_agent_run(int(agent_run_id))
        except KeyError:
            return
        self._store._ledger.update_agent_run_status(
            int(agent_run_id),
            "done",
            token_input=current.token_input,
            token_output=current.token_output,
            completion_summary=completion_summary[:2000],
        )

    def _mark_native_agent_run_failed(
        self,
        agent_run_id: int | None,
        reason: str,
    ) -> None:
        if not agent_run_id:
            return
        try:
            current = self._store._ledger.get_agent_run(int(agent_run_id))
        except KeyError:
            return
        self._store._ledger.update_agent_run_status(
            int(agent_run_id),
            "failed",
            token_input=current.token_input,
            token_output=current.token_output,
            completion_summary=reason[:2000],
        )

    async def add_user_message(
        self,
        task_id: int,
        text: str,
        *,
        images: list[dict[str, Any]] | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> None:
        detail = self._store.get_task_detail(task_id)
        clean_images = _relay_clean_image_attachments(images)
        clean_files = _relay_clean_text_file_attachments(files)
        prompt_text = _relay_user_input_with_attachments(
            text,
            images=clean_images,
            files=clean_files,
        )
        starts_new_visible_turn = detail.task.status in {
            "waiting_user",
            "completed",
            "blocked",
            "failed",
            "interrupted",
        }
        round_id = self._store.next_round_id(task_id)
        board = detail.board
        self._store.save_artifact(
            task_id,
            "director",
            "relay_board",
            {
                **board.to_json_dict(),
                "round_id": round_id,
                "latest_user_input": prompt_text,
                "current_dispatch": "director",
                "next_step": "director review latest user input",
            },
            summary="User follow-up routed to director",
        )
        if starts_new_visible_turn:
            self._store.update_task_status(task_id, "running")
            for job in detail.role_jobs:
                if job.role == "director":
                    continue
                if job.status != "idle":
                    self._store.update_role_status(task_id, job.role, "idle")
                    self._events.emit(
                        task_id,
                        "role.status",
                        role=job.role,
                        payload={
                            "status": "idle",
                            "reason": "new_followup_turn",
                            "round_id": round_id,
                        },
                    )
        self._store.update_role_status(task_id, "director", "queued")
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
        followup = self._store.save_artifact(
            task_id,
            "director",
            "user_followup",
            {
                "text": text,
                "target_role": "director",
                "context_packet_id": int(getattr(context_record, "id", 0) or 0),
                "round_id": round_id,
                **_relay_attachment_payload(images=clean_images, files=clean_files),
            },
            summary=text,
        )
        self._events.emit(
            task_id,
            "user.followup",
            role="user",
            payload={
                "role": "user",
                "text": text,
                "target_role": "director",
                **_relay_attachment_payload(images=clean_images, files=clean_files),
                "artifact_id": int(getattr(followup, "id", 0) or 0),
                "context_packet_id": int(getattr(context_record, "id", 0) or 0),
                "round_id": round_id,
            },
        )
        self._events.emit(
            task_id,
            "role.queued",
            role="director",
            payload={
                "latest_user_input": prompt_text,
                "reason": "new_followup_turn",
                "round_id": round_id,
            },
        )
        director = next(
            (job for job in detail.role_jobs if job.role == "director"),
            None,
        )
        if director and director.native_session_id and director.provider:
            try:
                provider = self._registry.get(director.provider)
            except KeyError:
                await self.dispatch_role(task_id, "director", prefer_continue=False)
                return
            capabilities = provider.capabilities()
            can_steer = bool(getattr(capabilities, "can_steer_active_turn", False))
            active_turn_id = director.active_turn_id or director.turn_id
            can_continue = bool(getattr(capabilities, "can_continue_session", False))
            provider_kwargs = {"images": clean_images} if clean_images else {}
            if (can_steer and director.turn_running and active_turn_id) or can_continue:
                try:
                    if can_steer and director.turn_running and active_turn_id:
                        result = await provider.steer_session(
                            director.native_session_id,
                            active_turn_id,
                            context_record.prompt_text,
                            **provider_kwargs,
                        )
                    else:
                        result = await provider.continue_session(
                            director.native_session_id,
                            context_record.prompt_text,
                            **provider_kwargs,
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
                            "fallback_action": "start_session",
                        },
                    )
                    await self.dispatch_role(
                        task_id,
                        "director",
                        prefer_continue=False,
                    )
                    return
                if not _control_result_verified(result):
                    self._events.emit(
                        task_id,
                        "dispatch.fallback",
                        role="director",
                        payload={
                            "fallback_reason": _control_result_failure_reason(result),
                            "provider": director.provider,
                            "fallback_action": "start_session",
                        },
                    )
                    await self.dispatch_role(
                        task_id,
                        "director",
                        prefer_continue=False,
                    )
                    return
                native_session_id = str(
                    getattr(result, "native_session_id", "") or director.native_session_id
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
                        "round_id": round_id,
                    },
                )
                self._events.emit(
                    task_id,
                    "dispatch.verified",
                    role="director",
                    payload={
                        "provider": director.provider,
                        "native_session_id": native_session_id,
                        "round_id": round_id,
                    },
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
        if not self._runtime_event_matches_current_role_turn(
            runtime_event,
            task_id,
            role,
        ):
            return None
        if self._project_runtime_delta(runtime_event, task_id=task_id, role=role):
            return None
        if _is_runtime_failure(runtime_event):
            self._mark_native_agent_run_failed(
                agent_run_id,
                _runtime_failure_reason(runtime_event),
            )
            self._block_role_with_error(
                task_id,
                role,
                _runtime_failure_reason(runtime_event),
            )
            return None
        if not self._is_runtime_completion(runtime_event):
            return None
        text = await self._runtime_completion_text_from_native_session(
            task_id,
            role,
            runtime_event,
        )
        if not text:
            text = self._runtime_completion_text(runtime_event)
        await self._apply_native_completion_output(
            task_id,
            role,
            runtime_event_id=event_id,
            output=text,
            agent_run_id=int(agent_run_id),
            completed_event=runtime_event,
        )
        return None

    def _should_accept_plain_followup_response(self, task_id: int, role: str) -> bool:
        if role != "director":
            return False
        detail = self._store.get_task_detail(task_id)
        latest_followup_id = _latest_artifact_id(detail.artifacts, "user_followup")
        if latest_followup_id <= 0:
            return False
        latest_response_id = _latest_artifact_id(
            detail.artifacts,
            "followup_response",
        )
        return latest_followup_id > latest_response_id

    def _runtime_event_matches_current_role_turn(
        self,
        runtime_event: Any,
        task_id: int,
        role: str,
    ) -> bool:
        try:
            detail = self._store.get_task_detail(task_id)
        except KeyError:
            return True
        job = next(
            (candidate for candidate in detail.role_jobs if candidate.role == role),
            None,
        )
        if job is None:
            return True
        return _runtime_event_matches_turn(
            runtime_event,
            job.active_turn_id or job.turn_id,
        )

    async def _handle_plain_followup_response(
        self,
        task_id: int,
        role: str,
        *,
        runtime_event_id: int,
        text: str,
    ):
        if runtime_event_id > 0 and runtime_event_id in self._handled_runtime_completion_ids:
            return None
        if runtime_event_id > 0:
            self._handled_runtime_completion_ids.add(runtime_event_id)
        clean_text = _plain_followup_visible_text(text.strip())
        round_id = self._store.current_round_id(task_id)
        response_payload = {
            "text": clean_text,
            "target_role": "user",
            "status": "passed",
            "runtime_event_id": runtime_event_id,
            "round_id": round_id,
        }
        if runtime_event_id > 0:
            try:
                runtime_store = RuntimeEventStore(self._store._ledger._conn)
                runtime_event = runtime_store.get_by_id(runtime_event_id)
                turn_id = _runtime_event_turn_id(runtime_event)
            except KeyError:
                turn_id = ""
            if turn_id:
                response_payload["native_turn_id"] = turn_id
        response_artifact = self._store.save_artifact(
            task_id,
            role,
            "followup_response",
            response_payload,
            summary=clean_text,
        )
        self._store.update_role_status(task_id, role, "passed")
        self._store.update_task_status(task_id, "completed")
        self._events.emit(
            task_id,
            "role.followup_response",
            role=role,
            payload={
                "role": role,
                "text": clean_text,
                "status": "passed",
                "artifact_id": int(getattr(response_artifact, "id", 0) or 0),
                "round_id": round_id,
            },
        )
        self._events.emit(
            task_id,
            "role.status",
            role=role,
            payload={"status": "passed", "round_id": round_id},
        )
        self._events.emit(
            task_id,
            "task.completed",
            role=role,
            payload={"summary": clean_text, "round_id": round_id},
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
        for task_detail in self._active_native_task_details():
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
                current_turn_id = job.active_turn_id or job.turn_id
                for event in events:
                    self._runtime_projection_cursors[agent_run_id] = int(
                        getattr(event, "id", 0) or 0
                    )
                    if not _runtime_event_matches_turn(event, current_turn_id):
                        continue
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
        projected += self._reconcile_finished_native_agent_runs(runtime_store)
        return projected

    def _reconcile_finished_native_agent_runs(
        self,
        runtime_store: RuntimeEventStore,
    ) -> int:
        changed = 0
        for summary in self._store.list_tasks():
            try:
                task_detail = self._store.get_task_detail(summary.task_id)
            except KeyError:
                continue
            for job in task_detail.role_jobs:
                if job.status in {"running", "streaming"} or not job.agent_run_id:
                    continue
                agent_run_id = int(job.agent_run_id)
                try:
                    agent_run = self._store._ledger.get_agent_run(agent_run_id)
                except KeyError:
                    continue
                if str(agent_run.status) in {
                    "done",
                    "failed",
                    "cancelled",
                    "canceled",
                    "interrupted",
                }:
                    continue
                events = runtime_store.list_by_agent_run_tail(agent_run_id, limit=500)
                current_turn_id = job.active_turn_id or job.turn_id
                if current_turn_id:
                    events = [
                        event
                        for event in events
                        if _runtime_event_matches_turn(event, current_turn_id)
                    ]
                if not events:
                    continue
                failure = next(
                    (event for event in reversed(events) if _is_runtime_failure(event)),
                    None,
                )
                if failure is not None:
                    self._mark_native_agent_run_failed(
                        agent_run_id,
                        _runtime_failure_reason(failure),
                    )
                    changed += 1
                    continue
                if not any(self._is_runtime_completion(event) for event in events):
                    continue
                protocol_delta = _complete_protocol_delta_event(events)
                summary_text = (
                    _completed_role_envelope_text(events)
                    or (_runtime_event_text(protocol_delta) if protocol_delta else "")
                    or "".join(
                        _runtime_event_text(event)
                        for event in events
                        if _is_runtime_model_text_delta(event)
                    )
                    or _runtime_event_text(events[-1])
                    or "native provider completed"
                )
                self._mark_native_agent_run_done(agent_run_id, summary_text)
                changed += 1
        return changed

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
        for task_detail in self._active_native_task_details():
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
                    (candidate for candidate in refreshed.role_jobs if candidate.role == job.role),
                    None,
                )
                if refreshed_job is None or refreshed_job.status not in {
                    "running",
                    "streaming",
                }:
                    continue
                pending_followup_response = self._should_accept_plain_followup_response(
                    task_detail.task.id,
                    job.role,
                )
                if await self._complete_stale_native_delta_if_ready(
                    runtime_store,
                    task_id=task_detail.task.id,
                    role=job.role,
                    agent_run_id=int(job.agent_run_id),
                    after_id=last_event_id,
                    provider_name=job.provider,
                    native_session_id=job.native_session_id,
                    allow_read_session=not pending_followup_response,
                    current_turn_id=job.active_turn_id or job.turn_id,
                ):
                    changed += 1
                    continue
                if pending_followup_response:
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

    def _active_native_task_details(self) -> list[Any]:
        details: list[Any] = []
        seen: set[int] = set()
        for summary in self._store.list_tasks(status="running"):
            try:
                detail = self._store.get_task_detail(summary.task_id)
            except KeyError:
                continue
            seen.add(int(detail.task.id))
            details.append(detail)
        for summary in self._store.list_tasks(status="interrupted"):
            if int(summary.task_id) in seen:
                continue
            try:
                detail = self._store.get_task_detail(summary.task_id)
            except KeyError:
                continue
            has_active_native_role = any(
                job.status in {"running", "streaming"} and job.agent_run_id
                for job in detail.role_jobs
            )
            if not has_active_native_role:
                continue
            self._store.update_task_status(detail.task.id, "running")
            details.append(self._store.get_task_detail(detail.task.id))
        return details

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
            if job is not None and job.status in {
                "passed",
                "failed",
                "blocked",
                "interrupted",
                "idle",
            }:
                return
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
        if detail.task.status not in {"queued", "running", "streaming", "waiting_user"}:
            return
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

    def _runtime_event_round_id(
        self,
        task_id: int,
        role: str,
        agent_run_id: int | None,
    ) -> int:
        try:
            fallback_round = self._store.current_round_id(task_id)
        except Exception:
            fallback_round = 1
        if agent_run_id is None:
            return fallback_round
        try:
            artifacts = self._store.get_task_detail(task_id).artifacts
        except Exception:
            return fallback_round
        for artifact in reversed(artifacts):
            if str(artifact.get("artifact_type") or "") != "role_dispatch_metadata":
                continue
            if str(artifact.get("relay_role") or artifact.get("role") or "") != str(role):
                continue
            try:
                artifact_agent_run_id = int(artifact.get("agent_run_id") or 0)
            except (TypeError, ValueError):
                continue
            if artifact_agent_run_id != int(agent_run_id):
                continue
            try:
                round_id = int(artifact.get("round_id") or fallback_round)
            except (TypeError, ValueError):
                return fallback_round
            return round_id if round_id > 0 else fallback_round
        return fallback_round

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
        round_id = self._runtime_event_round_id(int(task_id), role, agent_run_id)
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
                "round_id": round_id,
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
        round_id = self._runtime_event_round_id(int(task_id), role, agent_run_id)
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
                "round_id": round_id,
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
        current_turn_id = _runtime_event_turn_id(runtime_event)
        if current_turn_id:
            events = [event for event in events if _runtime_event_turn_id(event) == current_turn_id]
        completed = _completed_role_envelope_text(events)
        if completed is not None:
            return completed
        return "".join(
            _runtime_event_text(event) for event in events if _is_runtime_model_text_delta(event)
        )

    async def _runtime_completion_text_from_native_session(
        self,
        task_id: int,
        role: str,
        runtime_event: Any,
    ) -> str:
        try:
            detail = self._store.get_task_detail(task_id)
        except KeyError:
            return ""
        job = next(
            (candidate for candidate in detail.role_jobs if candidate.role == role),
            None,
        )
        if job is None or not job.provider or not job.native_session_id:
            return ""
        expected_turn_id = _runtime_event_turn_id(runtime_event)
        if self._should_accept_plain_followup_response(task_id, role) and not expected_turn_id:
            return ""
        try:
            provider = self._registry.get(job.provider)
        except KeyError:
            return ""
        if getattr(provider, "read_session", None) is None:
            return ""
        await self._sync_stale_native_role(job)
        return await self._read_native_session_completion_text(
            provider_name=job.provider,
            native_session_id=job.native_session_id,
            expected_turn_id=expected_turn_id,
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
        allow_read_session: bool = True,
        current_turn_id: str = "",
    ) -> bool:
        if after_id > 0:
            events = runtime_store.list_by_agent_run_after(
                agent_run_id,
                after_id=after_id,
                limit=500,
            )
        else:
            events = runtime_store.list_by_agent_run_tail(agent_run_id, limit=5000)
        if current_turn_id:
            events = [
                event for event in events if _runtime_event_matches_turn(event, current_turn_id)
            ]
        if current_turn_id and not events:
            tail_events = runtime_store.list_by_agent_run_tail(agent_run_id, limit=5000)
            current_turn_events = [
                event
                for event in tail_events
                if _runtime_event_matches_turn(event, current_turn_id)
            ]
            if any(_is_runtime_completion_event(event) for event in current_turn_events):
                events = current_turn_events
        if allow_read_session:
            read_session_output = await self._read_native_session_completion_text(
                provider_name=provider_name,
                native_session_id=native_session_id,
                expected_turn_id=current_turn_id,
            )
            if read_session_output:
                return await self._apply_native_completion_output(
                    task_id,
                    role,
                    runtime_event_id=0,
                    output=read_session_output,
                    agent_run_id=agent_run_id,
                )
        completed = _completed_role_envelope_event(events)
        if completed is not None:
            return await self._apply_native_completion_output(
                task_id,
                role,
                runtime_event_id=int(getattr(completed, "id", 0) or 0),
                output=_runtime_event_text(completed),
                agent_run_id=agent_run_id,
                completed_event=completed,
            )
        delta_events = [event for event in events if _is_runtime_model_text_delta(event)]
        if not delta_events:
            return False
        protocol_delta_event = _complete_protocol_delta_event(delta_events)
        if protocol_delta_event is not None:
            output = _runtime_event_text(protocol_delta_event)
            runtime_event_id = int(getattr(protocol_delta_event, "id", 0) or 0)
        else:
            output = "".join(_runtime_event_text(event) for event in delta_events)
            runtime_event_id = int(getattr(delta_events[-1], "id", 0) or 0)
        return await self._apply_native_completion_output(
            task_id,
            role,
            runtime_event_id=runtime_event_id,
            output=output,
            agent_run_id=agent_run_id,
        )

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

    async def _read_native_session_completion_text(
        self,
        *,
        provider_name: str,
        native_session_id: str,
        expected_turn_id: str = "",
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
        return _session_assistant_completion_text(
            payload,
            expected_turn_id=expected_turn_id,
        )

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


def _normalize_required_roles_for_route(route: str, values: Any) -> list[str]:
    required_roles = _clean_required_roles(values)
    if "director" not in required_roles:
        required_roles.insert(0, "director")
    if route == "director_only":
        return ["director"]
    if route == "core_relay":
        if not any(role for role in required_roles if role != "director"):
            required_roles.append("implementer")
        if "implementer" in required_roles and "auditor" not in required_roles:
            required_roles.append("auditor")
    return required_roles


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
        str(job.role) for job in detail.role_jobs if str(job.status) in {"passed", "completed"}
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
    payload = _latest_json_object(output)
    if payload is not None:
        recovered = _recover_routing_decision_from_payload(prompt, payload)
        if recovered is not None:
            return recovered
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
    if "core_relay" in text or "core relay" in text:
        return {
            "status": "passed",
            "reason": "总工程师输出的路由语义明确为核心接力，格式恢复后继续调度开发角色。",
            "role": "director",
            "artifact_type": "routing_decision",
            "handoff_to": "",
            "summary": "按核心接力处理：先由总工程师确认方向，再交给开发角色实现，随后由审核角色复核。",
            "evidence_refs": ["recovered malformed director routing output"],
            "open_questions": [],
            "next_action": "调度开发角色继续接力，完成后进入审核",
            "complexity": "medium",
            "risk": "medium",
            "route": "core_relay",
            "required_roles": ["director", "implementer", "auditor"],
            "acceptance_criteria": ["完成用户请求并给出中文结果"],
            "stop_conditions": ["发现需要用户确认范围或高风险操作时暂停"],
            "requires_user_approval": False,
        }
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


def _recover_routing_decision_from_payload(
    prompt: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if str(payload.get("artifact_type") or "") != "routing_decision":
        return None
    route = str(payload.get("route") or "").strip()
    if route not in _ROUTING_DECISION_ROUTES:
        if "core_relay" in route or "core" in route:
            route = "core_relay"
        else:
            return None
    if route == "director_only" and _prompt_looks_high_risk(prompt):
        return None
    required_roles = _normalize_required_roles_for_route(
        route,
        payload.get("required_roles"),
    )
    complexity = str(payload.get("complexity") or "medium").strip() or "medium"
    risk = str(payload.get("risk") or "medium").strip().lower() or "medium"
    if risk not in _ROUTING_DECISION_RISKS:
        risk = "medium"
    return {
        "status": "passed",
        "reason": str(
            payload.get("reason") or "总工程师路由语义明确，已恢复为合法 routing_decision。"
        ),
        "role": "director",
        "artifact_type": "routing_decision",
        "handoff_to": "",
        "summary": str(payload.get("summary") or "总工程师已完成路由判断。"),
        "evidence_refs": _clean_string_list(payload.get("evidence_refs"))
        or ["recovered malformed director routing output"],
        "open_questions": _clean_string_list(payload.get("open_questions")),
        "next_action": str(payload.get("next_action") or "继续接力"),
        "complexity": complexity,
        "risk": risk,
        "route": route,
        "required_roles": required_roles,
        "acceptance_criteria": _clean_string_list(payload.get("acceptance_criteria"))
        or ["完成用户请求并给出中文结果"],
        "stop_conditions": _clean_string_list(payload.get("stop_conditions"))
        or ["发现需要用户确认范围或高风险操作时暂停"],
        "requires_user_approval": bool(payload.get("requires_user_approval")),
    }


def _latest_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _index = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        return None
    return candidates[-1]


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


def _is_runtime_completion_event(runtime_event: Any) -> bool:
    return (
        _is_runtime_model_message_completed(runtime_event)
        or _is_turn_completed_activity(runtime_event)
        or _is_agent_run_completed_event(runtime_event)
    )


def _completed_role_envelope_event(events: list[Any]) -> Any | None:
    for event in reversed(events):
        if not (_is_runtime_model_message_completed(event) or _is_runtime_model_text_delta(event)):
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


def _complete_protocol_delta_event(events: list[Any]) -> Any | None:
    for event in reversed(events):
        if not _is_runtime_model_text_delta(event):
            continue
        text = _runtime_event_text(event).strip()
        if not text.startswith("{") or not text.endswith("}"):
            continue
        if parse_role_envelope(text).ok or _looks_like_relay_protocol_attempt(text):
            return event
    return None


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


def _runtime_event_turn_id(runtime_event: Any) -> str:
    payload = dict(getattr(runtime_event, "payload", {}) or {})
    return str(
        payload.get("native_turn_id")
        or payload.get("turnId")
        or payload.get("turn_id")
        or payload.get("active_turn_id")
        or ""
    ).strip()


def _runtime_event_matches_turn(runtime_event: Any, turn_id: str) -> bool:
    expected_turn_id = str(turn_id or "").strip()
    if not expected_turn_id:
        return True
    return _runtime_event_turn_id(runtime_event) == expected_turn_id


def _looks_like_relay_protocol_attempt(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    protocol_markers = (
        "artifact_type",
        "role_envelope",
        "routing_decision",
        "final_summary",
        "handoff_to",
        "required_roles",
        "evidence_refs",
        "acceptance_criteria",
        "stop_conditions",
        "requires_user_approval",
    )
    if any(marker in lowered for marker in protocol_markers):
        return True
    fused_markers = (
        "routing_decisioncomplexity",
        "routecore_relay",
        "routefull_relay",
        "routedirector_only",
        "statuspassedsummary",
        "roledirectorstatus",
        "handoff_toimplementer",
        "required_rolesdirector",
    )
    return any(marker in lowered for marker in fused_markers)


def _plain_followup_visible_text(text: str) -> str:
    value = text.strip()
    if not value:
        return ""
    parsed: Any = None
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        for field in ("summary", "reason", "message", "text", "output"):
            field_value = str(parsed.get(field) or "").strip()
            if field_value:
                return _trim_protocol_field_value(field_value)
        fused = " ".join(str(key) for key in parsed.keys())
        extracted = _extract_protocol_field_value(fused, "summary")
        if extracted:
            return extracted
        extracted = _extract_protocol_field_value(fused, "reason")
        if extracted:
            return extracted
    prefix_text = _readable_text_before_protocol_payload(value)
    if prefix_text:
        return prefix_text
    for field in ("summary", "reason"):
        extracted = _extract_protocol_field_value(value, field)
        if extracted:
            return extracted
    return value


def _readable_text_before_protocol_payload(text: str) -> str:
    protocol_index = text.find('{"artifact_type"')
    if protocol_index <= 0:
        protocol_index = text.find('{"role_envelope"')
    if protocol_index <= 0:
        return ""
    prefix = text[:protocol_index].strip()
    if len(prefix) < 12 or not re.search(r"[\u4e00-\u9fff]", prefix):
        return ""
    result_tail = _readable_result_tail(prefix)
    if result_tail:
        return result_tail
    sentences = re.findall(r"[^。！？!?]+[。！？!?]", prefix)
    if sentences:
        return sentences[-1].strip()
    return prefix


def _readable_result_tail(text: str) -> str:
    css_result_index = text.rfind("CSS 已经")
    if css_result_index >= 0:
        return text[css_result_index:].strip()
    markers = (
        "验证通过",
        "编译确认",
        "已修复",
        "已完成",
        "已更新",
        "已收口",
        "已经收口",
        "完成",
    )
    positions = [text.rfind(marker) for marker in markers]
    marker_index = max(positions)
    if marker_index < 0:
        return ""
    start = max(
        text.rfind("。", 0, marker_index),
        text.rfind("！", 0, marker_index),
        text.rfind("？", 0, marker_index),
        text.rfind("\n", 0, marker_index),
    )
    if start >= 0:
        start += 1
    else:
        start = marker_index
        css_index = text.rfind("CSS ", 0, marker_index + 8)
        if css_index >= 0:
            start = css_index
    return text[start:].strip()


def _extract_protocol_field_value(text: str, field: str) -> str:
    match = re.search(rf"(?:^|[{{,\s\"]){field}(?:[\"\s:=：]*)(.+)", text)
    if match:
        return _trim_protocol_field_value(match.group(1))
    marker_index = text.rfind(field)
    if marker_index < 0:
        return ""
    return _trim_protocol_field_value(text[marker_index + len(field) :])


def _trim_protocol_field_value(text: str) -> str:
    cut_at = len(text)
    for marker in (
        "role",
        "status",
        "handoff_to",
        "next_action",
        "open_questions",
        "evidence_refs",
        "artifact_type",
        "summary",
        "reason",
    ):
        marker_index = text.find(marker)
        if marker_index > 0:
            cut_at = min(cut_at, marker_index)
    return text[:cut_at].strip(' ,，。"{}')


def _latest_artifact_id(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    artifact_type: str,
) -> int:
    latest = 0
    for artifact in artifacts:
        if str(artifact.get("artifact_type") or "") != artifact_type:
            continue
        try:
            artifact_id = int(artifact.get("id") or 0)
        except (TypeError, ValueError):
            artifact_id = 0
        latest = max(latest, artifact_id)
    return latest


def _session_assistant_completion_text(
    payload: Any,
    *,
    expected_turn_id: str = "",
) -> str:
    if not isinstance(payload, dict):
        return ""
    turns = payload.get("turns")
    if not isinstance(turns, list):
        return ""
    expected = str(expected_turn_id or "").strip()
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role") or "").strip().lower() != "assistant":
            continue
        if expected and _native_session_turn_id(turn) != expected:
            continue
        text = str(
            turn.get("text")
            or turn.get("message")
            or turn.get("content")
            or turn.get("output")
            or ""
        )
        if text.strip():
            return text
    return ""


def _native_session_turn_id(turn: dict[str, Any]) -> str:
    return str(
        turn.get("native_turn_id")
        or turn.get("turnId")
        or turn.get("turn_id")
        or turn.get("id")
        or ""
    ).strip()


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
