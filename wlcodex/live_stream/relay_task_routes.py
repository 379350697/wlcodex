"""Relay task detail, input, and lifecycle action routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class RelayTaskRouteDependencies:
    send_json: Callable[..., Awaitable[None]]
    read_request_json: Callable[..., Awaitable[dict[str, Any] | None]]
    task_api_parts: Callable[[str], tuple[int | None, str]]
    task_detail: Callable[[int], Awaitable[Any]]
    task_detail_json: Callable[[Any, Any], dict[str, Any]]
    task_events: Callable[..., Awaitable[bool]]
    safe_int: Callable[..., int]
    safe_images: Callable[[Any], list[dict[str, Any]]]
    safe_files: Callable[[Any], list[dict[str, Any]]]
    optional_nonempty_string: Callable[[Any], str]
    first_blocked_role: Callable[[list[Any]], str]
    schedule_dispatch: Callable[[int, str], Any]
    schedule_reconcile: Callable[[int], bool]
    reject_if_maintenance_frozen: Callable[[Any], Awaitable[bool]]
    begin_mutation: Callable[..., Awaitable[Any]]
    finish_mutation: Callable[..., Awaitable[None]]
    abandon_mutation: Callable[[Any], None]
    maintenance_error_type: type[Exception]


async def handle_relay_task_route(
    *,
    deps: RelayTaskRouteDependencies,
    normalized_path: str,
    method: str,
    query: dict[str, list[str]],
    reader: Any,
    writer: Any,
    headers: dict[str, str],
    service: Any,
    hub: Any,
    send_sse: Any,
) -> bool:
    """Handle a task-scoped Relay API route and report response ownership."""
    task_id, suffix = deps.task_api_parts(normalized_path)
    if task_id is None:
        await deps.send_json(writer, 404, {"error": "not found"})
        return True

    async def require_presentation_action(action: str) -> Any | None:
        """Authorize mutations from the same read-only presentation contract.

        A raw lifecycle status is transport detail, not a user permission.  In
        particular a stale or provider-recovery task can still have a raw
        ``running`` row while only refresh is safe.  Keep every mutation on
        this guard so a stale DOM/SSE snapshot cannot issue a provider control.
        """

        try:
            detail = service.get_task_readonly(task_id)
        except KeyError:
            await deps.send_json(writer, 404, {"error": "relay task not found"})
            return None
        presentation = getattr(detail, "presentation", None)
        allowed = list(getattr(presentation, "allowed_actions", []) or [])
        if action not in allowed:
            freshness = getattr(presentation, "freshness", {}) or {}
            recovery_required = bool(
                freshness.get("recovery_required") if isinstance(freshness, dict) else False
            )
            await deps.send_json(
                writer,
                409,
                {
                    # Preserve the historic machine-readable recovery wording
                    # for old clients while the presentation explains the
                    # user-facing reason and exact safe next action.
                    "error": (
                        "native approval recovery is pending; wait for the lifecycle worker"
                        if recovery_required
                        else "该任务当前状态不允许此操作；请按下一步指引处理。"
                    ),
                    "state": str(getattr(presentation, "state", "stale") or "stale"),
                    "allowed_actions": allowed,
                    "presentation": presentation.to_dict() if hasattr(presentation, "to_dict") else {},
                },
            )
            return None
        return detail
    if suffix == "":
        if method != "GET":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        detail = await deps.task_detail(task_id)
        await deps.send_json(writer, 200, deps.task_detail_json(detail, service))
        return True
    if await deps.task_events(
        suffix=suffix, task_id=task_id, method=method, query=query, headers=headers,
        writer=writer, service=service, hub=hub, safe_int=deps.safe_int,
        send_json=deps.send_json, send_sse=send_sse,
    ):
        return True
    if suffix == "/inputs":
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("add_input") is None:
            return True
        body = await deps.read_request_json(writer, reader, headers)
        if body is None:
            return True
        mutation = await deps.begin_mutation("relay.input.queue", task_id, body)
        if mutation is None:
            return True
        try:
            result = await service.queue_or_followup_user_input(
                task_id, str(body.get("text") or body.get("prompt") or ""),
                images=deps.safe_images(body.get("images")),
                files=deps.safe_files(body.get("files")),
            )
        except (KeyError, deps.maintenance_error_type, ValueError) as exc:
            deps.abandon_mutation(mutation)
            await deps.send_json(
                writer, 423 if isinstance(exc, deps.maintenance_error_type) else 400,
                {"error": str(exc)},
            )
            return True
        await deps.finish_mutation(mutation, 200, result)
        return True
    if suffix.startswith("/inputs/"):
        parts = [part for part in suffix.strip("/").split("/") if part]
        if len(parts) != 3 or parts[0] != "inputs" or not parts[1].isdigit():
            await deps.send_json(writer, 404, {"error": "not found"})
            return True
        pending_id, action = int(parts[1]), parts[2]
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("add_input") is None:
            return True
        mutation = await deps.begin_mutation(
            f"relay.input.{action}", task_id, {"pending_id": pending_id, "action": action}
        )
        if mutation is None:
            return True
        if action == "steer":
            try:
                payload = await service.steer_active_attempt_payload(task_id, pending_id)
            except (KeyError, deps.maintenance_error_type, ValueError, RuntimeError) as exc:
                deps.abandon_mutation(mutation)
                await deps.send_json(
                    writer, 423 if isinstance(exc, deps.maintenance_error_type) else 400,
                    {"error": str(exc)},
                )
                return True
            await deps.finish_mutation(mutation, 200, {"pending_input": payload})
            return True
        if action == "cancel":
            try:
                pending = service.cancel_pending_input(task_id, pending_id)
            except (KeyError, ValueError) as exc:
                deps.abandon_mutation(mutation)
                await deps.send_json(writer, 400, {"error": str(exc)})
                return True
            await deps.finish_mutation(mutation, 200, {"pending_input": pending.to_dict()})
            return True
        deps.abandon_mutation(mutation)
        await deps.send_json(writer, 404, {"error": "not found"})
        return True
    if suffix.startswith("/rounds/"):
        parts = [part for part in suffix.strip("/").split("/") if part]
        if len(parts) != 3 or parts[0] != "rounds" or not parts[1].isdigit() or parts[2] != "control":
            await deps.send_json(writer, 404, {"error": "not found"})
            return True
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("resolve") is None:
            return True
        body = await deps.read_request_json(writer, reader, headers)
        if body is None:
            return True
        if str(body.get("decision") or "").strip() != "cancel_plan":
            if await deps.reject_if_maintenance_frozen(writer):
                return True
        mutation = await deps.begin_mutation(
            "relay.round.control", task_id, {"round_id": int(parts[1]), "control": body}
        )
        if mutation is None:
            return True
        try:
            result = await service.apply_round_control(
                task_id, int(parts[1]), decision=str(body.get("decision") or ""),
                artifact_id=deps.safe_int(str(body.get("artifact_id") or "0"), default=0),
                comment=str(body.get("comment") or ""),
                selected_option_id=str(body.get("selected_option_id") or ""),
                selected_option_label=str(body.get("selected_option_label") or ""),
                selected_option_instruction=str(body.get("selected_option_instruction") or ""),
                dispatch_next=False,
            )
        except (KeyError, deps.maintenance_error_type, ValueError) as exc:
            deps.abandon_mutation(mutation)
            await deps.send_json(
                writer, 423 if isinstance(exc, deps.maintenance_error_type) else 400,
                {"error": str(exc)},
            )
            return True
        next_role = str(result.get("next_role") or result.get("role") or "").strip()
        if next_role:
            deps.schedule_dispatch(task_id, next_role)
        await deps.finish_mutation(mutation, 200, {"control": result})
        return True
    if suffix == "/sessions":
        if method != "GET":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        detail = await deps.task_detail(task_id)
        await deps.send_json(writer, 200, {"sessions": [link.to_dict() for link in detail.session_links]})
        return True
    if suffix == "/message":
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("add_input") is None:
            return True
        body = await deps.read_request_json(writer, reader, headers)
        if body is None:
            return True
        mutation = await deps.begin_mutation("relay.message.add", task_id, body)
        if mutation is None:
            return True
        try:
            await service.add_user_message(
                task_id, str(body.get("text") or body.get("prompt") or ""),
                images=deps.safe_images(body.get("images")), files=deps.safe_files(body.get("files")),
            )
        except (KeyError, deps.maintenance_error_type, ValueError) as exc:
            deps.abandon_mutation(mutation)
            await deps.send_json(
                writer, 423 if isinstance(exc, deps.maintenance_error_type) else 400,
                {"error": str(exc)},
            )
            return True
        detail = await deps.task_detail(task_id)
        await deps.finish_mutation(mutation, 200, deps.task_detail_json(detail, service))
        return True
    if suffix == "/resume":
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("resume") is None:
            return True
        body = await deps.read_request_json(writer, reader, headers)
        if body is None:
            return True
        mutation = await deps.begin_mutation("relay.role.resume", task_id, body)
        if mutation is None:
            return True
        role = deps.optional_nonempty_string(body.get("role"))
        if not role:
            detail = service.get_task_readonly(task_id)
            role = deps.first_blocked_role(detail.role_jobs)
        if not role:
            deps.abandon_mutation(mutation)
            await deps.send_json(writer, 400, {"error": "relay task has no blocked role to resume"})
            return True
        try:
            await service.resume_role(
                task_id, role, force=bool(body.get("force")),
                override_indeterminate_provider_state=bool(body.get("override_indeterminate_provider_state")),
            )
        except (KeyError, deps.maintenance_error_type, ValueError) as exc:
            deps.abandon_mutation(mutation)
            await deps.send_json(
                writer, 423 if isinstance(exc, deps.maintenance_error_type) else 400,
                {"error": str(exc)},
            )
            return True
        detail = await deps.task_detail(task_id)
        await deps.finish_mutation(mutation, 200, deps.task_detail_json(detail, service))
        return True
    if suffix == "/archive":
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("archive") is None:
            return True
        body = await deps.read_request_json(writer, reader, headers)
        if body is None:
            return True
        mutation = await deps.begin_mutation("relay.task.archive", task_id, body)
        if mutation is None:
            return True
        try:
            detail = service.get_task_readonly(task_id)
        except KeyError:
            deps.abandon_mutation(mutation)
            await deps.send_json(writer, 404, {"error": "relay task not found"})
            return True
        state = str(getattr(detail.presentation, "state", "") or "")
        freshness = getattr(detail.presentation, "freshness", {})
        recovery_required = bool(freshness.get("recovery_required") if isinstance(freshness, dict) else False)
        if recovery_required:
            deps.abandon_mutation(mutation)
            await deps.send_json(writer, 409, {"error": "native approval recovery is pending; wait for the lifecycle worker before archiving", "state": state})
            return True
        if state in {"running", "waiting_user", "waiting_approval"}:
            deps.abandon_mutation(mutation)
            await deps.send_json(writer, 409, {"error": "active Relay tasks must be interrupted or completed before archiving", "state": state})
            return True
        mutation_store, _claim = mutation
        mutation_store.archive_task(task_id, reason=str(body.get("reason") or "").strip())
        await deps.finish_mutation(mutation, 200, {"task_id": task_id, "archived": True, "state": state})
        return True
    if suffix == "/refresh":
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("refresh") is None:
            return True
        body = await deps.read_request_json(writer, reader, headers)
        if body is None:
            return True
        mutation = await deps.begin_mutation("relay.task.refresh", task_id, body)
        if mutation is None:
            return True
        try:
            service.get_task_readonly(task_id)
        except KeyError:
            deps.abandon_mutation(mutation)
            await deps.send_json(writer, 404, {"error": "relay task not found"})
            return True
        scheduled = deps.schedule_reconcile(task_id)
        await deps.finish_mutation(mutation, 202, {"task_id": task_id, "scheduled": scheduled})
        return True
    if suffix == "/interrupt":
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if await require_presentation_action("interrupt") is None:
            return True
        body = await deps.read_request_json(writer, reader, headers)
        if body is None:
            return True
        mutation = await deps.begin_mutation("relay.task.interrupt", task_id, body)
        if mutation is None:
            return True
        try:
            await service.interrupt(task_id, role=deps.optional_nonempty_string(body.get("role")))
        except (KeyError, ValueError, RuntimeError) as exc:
            deps.abandon_mutation(mutation)
            await deps.send_json(
                writer, 503 if isinstance(exc, RuntimeError) else 400,
                {"error": str(exc), "retryable": isinstance(exc, RuntimeError)},
            )
            return True
        detail = await deps.task_detail(task_id)
        await deps.finish_mutation(mutation, 200, deps.task_detail_json(detail, service))
        return True
    await deps.send_json(writer, 404, {"error": "not found"})
    return True
