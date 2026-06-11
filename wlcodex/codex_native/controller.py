from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from wlcodex.codex_backend import (
    build_approval_response,
    build_legacy_approval_response,
    parse_thread_start_response,
)
from wlcodex.codex_native.models import (
    NativeCodexControlResult,
    NativeCodexSession,
    NativeCodexStatus,
)
from wlcodex.codex_native.projector import (
    _METHOD_TO_BACKEND_EVENT,
    _SERVER_REQUEST_TO_APPROVAL_KIND,
    NativeCodexEventProjector,
)
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.jsonrpc import JsonRpcError
from wlcodex.runtime_event_store import RuntimeEventStore


class _StaleActiveTurnMismatch(Exception):
    def __init__(self, stale_turn_id: str) -> None:
        super().__init__(stale_turn_id)
        self.stale_turn_id = stale_turn_id


# The app-server can expose a new thread before its rollout accepts turn/start.
# Keep this race inside the controller so callers do not need a manual prompt path.
_ROLLOUT_NOT_READY_RETRY_DELAYS = (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0)


class _NativeClient(Protocol):
    async def status(self) -> Any: ...

    async def list_sessions(self, limit: int) -> list[dict[str, Any]]: ...

    async def list_models(self) -> list[dict[str, Any]]: ...

    async def start_thread(
        self,
        cwd: str,
        *,
        model: str | None = None,
        service_tier: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox: str | None = None,
    ) -> dict[str, Any]: ...

    async def read_session(
        self,
        native_thread_id: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]: ...

    async def attach_session(self, native_thread_id: str) -> dict[str, Any]: ...

    async def continue_session(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
    ) -> str: ...

    async def start_turn(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
    ) -> str: ...

    async def steer_turn(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
        *,
        images: list[dict[str, Any]] | None = None,
    ) -> None: ...

    async def interrupt_turn(self, native_thread_id: str, turn_id: str) -> None: ...

    def resolve_request(self, request_id: str, result: dict[str, Any]) -> None: ...


NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class _TurnState:
    session: NativeCodexSession
    turn_id: str = ""
    active_turn_id: str = ""

    @property
    def turn_running(self) -> bool:
        return bool(self.active_turn_id)


class CodexNativeController:
    def __init__(
        self,
        client: _NativeClient,
        session_store: NativeCodexSessionStore,
        runtime_store: RuntimeEventStore,
    ) -> None:
        self._client = client
        self._session_store = session_store
        self._projector = NativeCodexEventProjector(session_store, runtime_store)
        self._approval_threads: dict[str, str] = {}
        self._approval_payloads: dict[str, dict[str, Any]] = {}
        self._register_handlers()

    async def status(self) -> Any:
        try:
            return await self._client.status()
        except Exception as exc:
            return NativeCodexStatus(
                enabled=True,
                connected=False,
                remote_control_status="error",
                error=str(exc) or type(exc).__name__,
            )

    async def list_sessions(self, limit: int = 50) -> list[NativeCodexSession]:
        raw_sessions = await self._client.list_sessions(limit)
        return [self._map_thread(raw) for raw in raw_sessions]

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._client.list_models()

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
    ) -> NativeCodexControlResult:
        detail = await self._client.start_thread(
            cwd.strip(),
            model=model,
            service_tier=service_tier,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox=sandbox,
        )
        native_thread_id = parse_thread_start_response(detail)
        self._project_detail_header(native_thread_id, detail)
        return await self._start_new_turn(
            native_thread_id,
            prompt,
            model=model,
            effort=effort,
            service_tier=service_tier,
            images=images,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox_policy=sandbox_policy,
            collaboration_mode=collaboration_mode,
            resume_first=False,
        )

    async def create_session(
        self,
        cwd: str,
        *,
        model: str | None = None,
        service_tier: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox: str | None = None,
    ) -> NativeCodexControlResult:
        detail = await self._client.start_thread(
            cwd.strip(),
            model=model,
            service_tier=service_tier,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox=sandbox,
        )
        native_thread_id = parse_thread_start_response(detail)
        turn_state = self._project_detail_header(native_thread_id, detail)
        session = turn_state.session
        metadata = _model_settings_metadata(
            model=model,
            service_tier=service_tier,
        )
        if metadata:
            session = self._session_store.update_session(
                session.id,
                metadata=metadata,
            )
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_state.turn_id,
            active_turn_id=turn_state.active_turn_id,
            turn_running=turn_state.turn_running,
            status="created",
        )

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        detail = await self._client.read_session(native_thread_id)
        self._project_detail(native_thread_id, detail)
        return detail

    async def attach_session(self, native_thread_id: str) -> NativeCodexControlResult:
        native_thread_id = native_thread_id.strip()
        if not native_thread_id:
            raise ValueError("native_thread_id is required")
        detail = await self._client.attach_session(native_thread_id)
        turn_state = self._project_detail_header(native_thread_id, detail)
        session = turn_state.session
        result = NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_state.turn_id,
            active_turn_id=turn_state.active_turn_id,
            turn_running=turn_state.turn_running,
            status="attached",
        )
        return result

    async def sync_session(self, native_thread_id: str) -> NativeCodexControlResult:
        native_thread_id = native_thread_id.strip()
        if not native_thread_id:
            raise ValueError("native_thread_id is required")
        detail = await self._client.read_session(native_thread_id, include_turns=False)
        turn_state = self._project_detail_header(native_thread_id, detail)
        session = turn_state.session
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_state.turn_id,
            active_turn_id=turn_state.active_turn_id,
            turn_running=turn_state.turn_running,
            status="synced",
        )

    async def continue_session(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
        force_new_turn: bool = False,
    ) -> NativeCodexControlResult:
        native_thread_id = native_thread_id.strip()
        if not native_thread_id:
            raise ValueError("native_thread_id is required")
        turn_state = await self._refresh_turn_state(native_thread_id)
        session = turn_state.session
        active_turn_id = turn_state.active_turn_id
        if active_turn_id and not force_new_turn:
            try:
                active_turn_id = await self._steer_active_turn(
                    native_thread_id,
                    active_turn_id,
                    prompt,
                    images=images,
                )
            except _StaleActiveTurnMismatch as exc:
                await self._client.interrupt_turn(native_thread_id, exc.stale_turn_id)
                return await self._start_new_turn(
                    native_thread_id,
                    prompt,
                    model=model,
                    effort=effort,
                    service_tier=service_tier,
                    images=images,
                    approval_policy=approval_policy,
                    approvals_reviewer=approvals_reviewer,
                    sandbox_policy=sandbox_policy,
                    collaboration_mode=collaboration_mode,
                )
            session = self._session_store.update_session(
                session.id,
                status="running",
                last_turn_id=active_turn_id,
            )
            return NativeCodexControlResult(
                native_thread_id=native_thread_id,
                agent_run_id=session.agent_run_id,
                turn_id=active_turn_id,
                active_turn_id=active_turn_id,
                turn_running=True,
            )
        return await self._start_new_turn(
            native_thread_id,
            prompt,
            model=model,
            effort=effort,
            service_tier=service_tier,
            images=images,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox_policy=sandbox_policy,
            collaboration_mode=collaboration_mode,
        )

    async def _start_new_turn(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
        resume_first: bool = True,
    ) -> NativeCodexControlResult:
        session = self._ensure_session(native_thread_id)
        turn_id = await self._send_turn_when_rollout_is_ready(
            native_thread_id,
            prompt,
            model=model,
            effort=effort,
            service_tier=service_tier,
            images=images,
            approval_policy=approval_policy,
            approvals_reviewer=approvals_reviewer,
            sandbox_policy=sandbox_policy,
            collaboration_mode=collaboration_mode,
            resume_first=resume_first,
        )
        session = self._session_store.update_session(
            session.id,
            status="running",
            last_turn_id=turn_id,
            metadata=_model_settings_metadata(
                model=model,
                effort=effort,
                service_tier=service_tier,
            ),
        )
        self._project_sent_prompt(
            native_thread_id=native_thread_id,
            native_turn_id=turn_id,
            prompt=prompt,
        )
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_id,
            active_turn_id=turn_id,
            turn_running=True,
        )

    async def _send_turn_when_rollout_is_ready(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
        resume_first: bool = True,
    ) -> str:
        for attempt in range(len(_ROLLOUT_NOT_READY_RETRY_DELAYS) + 1):
            try:
                if resume_first:
                    return await self._client.continue_session(
                        native_thread_id,
                        prompt,
                        model=model,
                        effort=effort,
                        service_tier=service_tier,
                        images=images,
                        approval_policy=approval_policy,
                        approvals_reviewer=approvals_reviewer,
                        sandbox_policy=sandbox_policy,
                        collaboration_mode=collaboration_mode,
                    )
                return await self._client.start_turn(
                    native_thread_id,
                    prompt,
                    model=model,
                    effort=effort,
                    service_tier=service_tier,
                    images=images,
                    approval_policy=approval_policy,
                    approvals_reviewer=approvals_reviewer,
                    sandbox_policy=sandbox_policy,
                    collaboration_mode=collaboration_mode,
                )
            except JsonRpcError as exc:
                if (
                    not _is_rollout_not_ready_error(exc)
                    or attempt == len(_ROLLOUT_NOT_READY_RETRY_DELAYS)
                ):
                    raise
                await self._refresh_rollout_state(native_thread_id)
                delay = _ROLLOUT_NOT_READY_RETRY_DELAYS[attempt]
                if delay:
                    await asyncio.sleep(delay)
        raise RuntimeError("unreachable rollout retry state")

    async def _refresh_rollout_state(self, native_thread_id: str) -> None:
        try:
            await self._refresh_turn_state(native_thread_id)
        except JsonRpcError as exc:
            if not _is_rollout_not_ready_error(exc):
                raise

    async def steer_session(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
    ) -> NativeCodexControlResult:
        native_thread_id = native_thread_id.strip()
        if not native_thread_id:
            raise ValueError("native_thread_id is required")
        turn_state = await self._refresh_turn_state(native_thread_id)
        session = turn_state.session
        active_turn_id = turn_state.active_turn_id
        if not active_turn_id:
            return await self._start_new_turn(
                native_thread_id,
                prompt,
                model=model,
                effort=effort,
                service_tier=service_tier,
                images=images,
                approval_policy=approval_policy,
                approvals_reviewer=approvals_reviewer,
                sandbox_policy=sandbox_policy,
            )
        try:
            turn_id = await self._steer_active_turn(
                native_thread_id,
                active_turn_id,
                prompt,
                images=images,
            )
        except _StaleActiveTurnMismatch as exc:
            await self._client.interrupt_turn(native_thread_id, exc.stale_turn_id)
            return await self._start_new_turn(
                native_thread_id,
                prompt,
                model=model,
                effort=effort,
                service_tier=service_tier,
                images=images,
                approval_policy=approval_policy,
                approvals_reviewer=approvals_reviewer,
                sandbox_policy=sandbox_policy,
            )
        session = self._session_store.update_session(
            session.id,
            status="running",
            last_turn_id=turn_id,
        )
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_id,
            active_turn_id=turn_id,
            turn_running=True,
        )

    async def interrupt_session(
        self,
        native_thread_id: str,
        turn_id: str,
    ) -> NativeCodexControlResult:
        session = self._ensure_session(native_thread_id)
        await self._client.interrupt_turn(native_thread_id, turn_id)
        session = self._session_store.update_session(
            session.id,
            status="aborted",
            last_turn_id=turn_id,
        )
        return NativeCodexControlResult(
            native_thread_id=native_thread_id,
            agent_run_id=session.agent_run_id,
            turn_id=turn_id,
            active_turn_id="",
            turn_running=False,
        )

    async def resolve_approval(
        self,
        codex_request_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        if not codex_request_id.strip():
            raise ValueError("codex_request_id is required")
        native_thread_id = self._approval_threads.get(codex_request_id, "")
        approval_payload = self._approval_payloads.get(codex_request_id)
        if not native_thread_id or approval_payload is None:
            raise KeyError(f"unknown native approval request: {codex_request_id}")
        protocol_response = _approval_protocol_response(approval_payload, response)
        self._client.resolve_request(codex_request_id, protocol_response)
        self._projector.project_approval_resolved(
            native_thread_id=native_thread_id,
            native_turn_id=str(
                approval_payload.get("native_turn_id")
                or approval_payload.get("turnId")
                or ""
            ),
            request_id=codex_request_id,
            response=protocol_response,
        )
        self._approval_threads.pop(codex_request_id, None)
        self._approval_payloads.pop(codex_request_id, None)
        return {"codex_request_id": codex_request_id, "status": "resolved"}

    def _ensure_session(self, native_thread_id: str) -> NativeCodexSession:
        native_thread_id = native_thread_id.strip()
        if not native_thread_id:
            raise ValueError("native_thread_id is required")
        existing = self._session_store.get_by_thread_id(native_thread_id)
        if existing is not None:
            return existing
        return self._session_store.get_or_create_session(
            native_thread_id=native_thread_id
        )

    def _project_detail(
        self,
        native_thread_id: str,
        detail: dict[str, Any],
    ) -> NativeCodexSession:
        thread = detail.get("thread")
        if isinstance(thread, dict):
            self._map_thread({"id": native_thread_id, **thread})
            self._projector.project_history(detail)
        return self._ensure_session(native_thread_id)

    def _project_sent_prompt(
        self,
        *,
        native_thread_id: str,
        native_turn_id: str,
        prompt: str,
    ) -> None:
        if not prompt.strip():
            return
        self._projector.project_user_message(
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            text=prompt,
        )

    async def _refresh_turn_state(self, native_thread_id: str) -> _TurnState:
        detail = await self._client.attach_session(native_thread_id)
        return self._project_detail_header(native_thread_id, detail)

    async def _steer_active_turn(
        self,
        native_thread_id: str,
        active_turn_id: str,
        prompt: str,
        *,
        images: list[dict[str, Any]] | None = None,
    ) -> str:
        try:
            await self._client.steer_turn(
                native_thread_id,
                active_turn_id,
                prompt,
                images=images,
            )
            return active_turn_id
        except JsonRpcError as exc:
            current_turn_id = _active_turn_id_from_mismatch(str(exc))
            if not current_turn_id or current_turn_id == active_turn_id:
                raise
            if _is_older_ordered_turn_id(current_turn_id, active_turn_id):
                raise _StaleActiveTurnMismatch(current_turn_id) from exc
            await self._client.steer_turn(
                native_thread_id,
                current_turn_id,
                prompt,
                images=images,
            )
            return current_turn_id

    def _project_detail_header(
        self,
        native_thread_id: str,
        detail: dict[str, Any],
    ) -> _TurnState:
        thread = detail.get("thread")
        if isinstance(thread, dict):
            session = self._map_thread({"id": native_thread_id, **thread})
        else:
            session = self._ensure_session(native_thread_id)
        active_turn_id = _active_turn_id(detail)
        latest_turn_id = _latest_turn_id(detail)
        turn_id = active_turn_id or latest_turn_id
        if turn_id:
            session = self._session_store.update_session(
                session.id,
                last_turn_id=turn_id,
                status="running" if active_turn_id else session.status,
                activity_at=_detail_activity_at(detail) or None,
            )
        return _TurnState(
            session=session,
            turn_id=turn_id,
            active_turn_id=active_turn_id,
        )

    def _map_thread(self, raw: dict[str, Any]) -> NativeCodexSession:
        native_thread_id = _string_value(raw, "threadId") or _string_value(raw, "id")
        if not native_thread_id:
            raise ValueError(f"native Codex thread is missing id/threadId: {raw!r}")
        return self._session_store.get_or_create_session(
            native_thread_id=native_thread_id,
            title=(
                _string_value(raw, "title")
                or _string_value(raw, "name")
                or _string_value(raw, "preview")
            ),
            cwd=_string_value(raw, "cwd"),
            source_kind=(
                _string_value(raw, "sourceKind")
                or _string_value(raw, "source")
                or "unknown"
            ),
            status=_string_value(raw, "status") or "unknown",
            activity_at=_thread_activity_at(raw),
            metadata=_thread_model_settings_metadata(raw),
        )

    def _register_handlers(self) -> None:
        register = getattr(self._client, "register_notification_handler", None)
        if register is not None:
            for method in _METHOD_TO_BACKEND_EVENT:
                register(method, self._build_notification_handler(method))
        register_request = getattr(self._client, "register_server_request_handler", None)
        if register_request is not None:
            for method in _SERVER_REQUEST_TO_APPROVAL_KIND:
                register_request(method, self._build_approval_handler(method))

    def _build_notification_handler(self, method: str) -> NotificationHandler:
        async def handler(payload: dict[str, Any]) -> None:
            self._projector.project_notification(method, payload)

        return handler

    def _build_approval_handler(self, method: str) -> Callable[..., Awaitable[None]]:
        async def handler(payload: dict[str, Any], request_id: str) -> None:
            projected = self._projector.project_approval_request(
                method,
                payload,
                request_id,
            )
            for event in projected:
                native_thread_id = event.payload.get("native_thread_id")
                if native_thread_id:
                    self._approval_threads[request_id] = str(native_thread_id)
                    self._approval_payloads[request_id] = dict(event.payload)
                    break

        return handler


def _string_value(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if isinstance(value, dict) and value.get("type"):
        return str(value["type"])
    return str(value) if value else ""


def _thread_activity_at(raw: dict[str, Any]) -> str:
    for key in (
        "updatedAt",
        "updated_at",
        "lastActivityAt",
        "last_activity_at",
        "lastRunAt",
        "last_run_at",
        "completedAt",
        "createdAt",
    ):
        value = _activity_value(raw, key)
        if value:
            return value
    turns = raw.get("turns")
    if isinstance(turns, list):
        return _turn_activity_at(_latest_turn(turns))
    return ""


def _thread_model_settings_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    return _model_settings_metadata(
        model=(
            _string_value(raw, "model")
            or _string_value(raw, "modelId")
            or _string_value(raw, "model_id")
        ),
        effort=(
            _string_value(raw, "effort")
            or _string_value(raw, "reasoningEffort")
            or _string_value(raw, "reasoning_effort")
        ),
        service_tier=(
            _string_value(raw, "serviceTier")
            or _string_value(raw, "service_tier")
        ),
    )


def _model_settings_metadata(
    *,
    model: str | None = None,
    effort: str | None = None,
    service_tier: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if model:
        metadata["model"] = str(model)
    if effort:
        metadata["effort"] = str(effort)
    if service_tier:
        metadata["service_tier"] = str(service_tier)
    return metadata


def _detail_activity_at(detail: dict[str, Any]) -> str:
    thread = detail.get("thread")
    if isinstance(thread, dict):
        value = _thread_activity_at(thread)
        if value:
            return value
    return _turn_activity_at(_latest_turn(_turns(detail)))


def _turn_activity_at(turn: dict[str, Any] | None) -> str:
    if turn is None:
        return ""
    for key in ("completedAt", "updatedAt", "startedAt", "createdAt"):
        value = _activity_value(turn, key)
        if value:
            return value
    return ""


def _activity_value(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if isinstance(value, int | float):
        return _epoch_to_iso(float(value))
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return _epoch_to_iso(float(text))
        return text
    return ""


def _epoch_to_iso(value: float) -> str:
    if value > 10_000_000_000:
        value = value / 1000
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _active_turn_id(detail: dict[str, Any]) -> str:
    turns = _turns(detail)
    latest_turn = _latest_turn(turns)
    if latest_turn is None:
        return ""
    latest_turn_id = _turn_identifier(latest_turn)
    if latest_turn_id and _is_active_status(_string_value(latest_turn, "status")):
        return latest_turn_id
    if (
        latest_turn_id
        and _is_active_status(_thread_status(detail))
        and not _is_terminal_status(_string_value(latest_turn, "status"))
    ):
        return latest_turn_id
    return ""


def _latest_turn_id(detail: dict[str, Any]) -> str:
    latest_turn = _latest_turn(_turns(detail))
    return _turn_identifier(latest_turn) if latest_turn is not None else ""


def _turns(detail: dict[str, Any]) -> list[Any]:
    turns = detail.get("turns")
    if isinstance(turns, list) and turns:
        return turns
    thread = detail.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("turns"), list):
        return list(thread["turns"])
    return turns if isinstance(turns, list) else []


def _latest_turn(turns: list[Any]) -> dict[str, Any] | None:
    candidates = [turn for turn in turns if isinstance(turn, dict)]
    if not candidates:
        return None
    return max(candidates, key=_turn_sort_key)


def _turn_sort_key(turn: dict[str, Any]) -> tuple[float, str]:
    for key in ("startedAt", "createdAt", "completedAt", "updatedAt"):
        value = turn.get(key)
        if isinstance(value, int | float):
            return (float(value), _turn_identifier(turn))
        if isinstance(value, str):
            try:
                return (float(value), _turn_identifier(turn))
            except ValueError:
                pass
    return (0.0, _turn_identifier(turn))


def _turn_identifier(turn: dict[str, Any]) -> str:
    return _string_value(turn, "turnId") or _string_value(turn, "id")


def _thread_status(detail: dict[str, Any]) -> str:
    thread = detail.get("thread")
    if not isinstance(thread, dict):
        return ""
    return _string_value(thread, "status")


def _is_terminal_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {
        "completed",
        "complete",
        "done",
        "failed",
        "error",
        "cancelled",
        "canceled",
        "interrupted",
        "aborted",
    }


def _is_active_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {
        "active",
        "running",
        "inprogress",
        "in_progress",
        "pending",
        "waitingonapproval",
        "waiting_on_approval",
    }


def _active_turn_id_from_mismatch(message: str) -> str:
    match = re.search(r"found `([^`]+)`", message)
    return match.group(1).strip() if match else ""


def _is_rollout_not_ready_error(exc: JsonRpcError) -> bool:
    message = str(exc.rpc_message or exc)
    return exc.code == -32600 and "no rollout found for thread id" in message


def _is_older_ordered_turn_id(candidate: str, reference: str) -> bool:
    if not (_is_uuid_v7(candidate) and _is_uuid_v7(reference)):
        return False
    return candidate.lower() < reference.lower()


def _is_uuid_v7(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            value.strip(),
            re.IGNORECASE,
        )
    )


def _approval_protocol_response(
    approval_payload: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    action = str(response.get("action", ""))
    if action not in ("approve_once", "approve_session", "deny", "cancel"):
        raise ValueError("approval action must be approve_once, approve_session, deny, or cancel")
    if approval_payload.get("responseSchema") == "legacy_review_decision":
        return dict(build_legacy_approval_response(action=action, allow_session=True))
    requested_permissions = approval_payload.get("requestedPermissions")
    if not isinstance(requested_permissions, dict):
        requested_permissions = approval_payload.get("permissions")
    if not isinstance(requested_permissions, dict):
        requested_permissions = {}
    return dict(
        build_approval_response(
            kind=str(approval_payload.get("kind", "")),
            action=action,
            requested_permissions=requested_permissions,
            allow_session=True,
        )
    )
