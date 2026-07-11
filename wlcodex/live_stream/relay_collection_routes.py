"""Relay collection-route handlers (configuration and task collection)."""

from __future__ import annotations

from typing import Any

from wlcodex.relay.errors import ActiveRelayTasksDecisionRequired


async def handle_relay_collection_route(
    *,
    normalized_path: str,
    method: str,
    query: dict[str, list[str]],
    reader: Any,
    writer: Any,
    headers: dict[str, str],
    service: Any,
    send_json: Any,
    read_request_json: Any,
    begin_mutation: Any,
    finish_mutation: Any,
    abandon_mutation: Any,
    presentation_state_filter: Any,
    page_number: Any,
    safe_int: Any,
    optional_nonempty_string: Any,
    safe_images: Any,
    safe_files: Any,
    acceptance_criteria: Any,
    task_detail: Any,
    maintenance_error_type: type[Exception],
) -> bool:
    """Handle a collection route and return whether a response was sent."""
    if normalized_path == "/api/relay/token-stats":
        if method != "GET":
            await send_json(writer, 405, {"error": "method not allowed"})
            return True
        await send_json(writer, 200, service.today_token_stats())
        return True
    if normalized_path == "/api/relay/config":
        if method == "GET":
            await send_json(writer, 200, service.config())
            return True
        if method != "POST":
            await send_json(writer, 405, {"error": "method not allowed"})
            return True
        body = await read_request_json(writer, reader, headers)
        if body is None:
            return True
        assignments = body.get("assignments", body)
        if not isinstance(assignments, dict):
            await send_json(writer, 400, {"error": "assignments must be an object"})
            return True
        mutation = await begin_mutation("relay.config.save", None, body)
        if mutation is None:
            return True
        try:
            config = service.save_config(
                {str(role): str(provider) for role, provider in assignments.items()}
            )
        except (maintenance_error_type, ValueError) as exc:
            abandon_mutation(mutation)
            await send_json(
                writer,
                423 if isinstance(exc, maintenance_error_type) else 400,
                {"error": str(exc)},
            )
            return True
        await finish_mutation(mutation, 200, config)
        return True
    if normalized_path != "/api/relay/tasks":
        return False
    if method == "GET":
        status_filter = presentation_state_filter(str((query.get("status") or [""])[0] or ""))
        page = page_number(str((query.get("page") or ["1"])[0] or "1"))
        page_size = min(100, max(1, safe_int((query.get("page_size") or ["20"])[0], default=20)))
        summaries, total, state_counts = service.list_tasks_page_readonly(
            workspace=optional_nonempty_string((query.get("workspace") or [""])[0]),
            presentation_state=status_filter or None,
            page=page,
            page_size=page_size,
        )
        await send_json(
            writer,
            200,
            {
                "tasks": [summary.to_dict() for summary in summaries],
                "total": total,
                "page": page,
                "page_size": page_size,
                "status": status_filter,
                "state_counts": state_counts,
            },
        )
        return True
    if method != "POST":
        await send_json(writer, 405, {"error": "method not allowed"})
        return True
    body = await read_request_json(writer, reader, headers)
    if body is None:
        return True
    workspace = str(body.get("workspace") or "").strip()
    if not workspace:
        await send_json(writer, 400, {"error": "relay task workspace is required"})
        return True
    mutation = await begin_mutation("relay.task.create", None, body)
    if mutation is None:
        return True
    mutation_store, claim = mutation
    if claim is not None and claim.can_resume_task_creation:
        # The task was committed before a prior response/dispatch could be
        # completed.  Reuse its durable identity and let dispatch's own claim
        # decide whether there is still queued work; never create a second task.
        service.recover_task_creation(
            int(claim.task_id),
            execution_mode=str(body.get("execution_mode") or "standard"),
            execution_goal=str(body.get("execution_goal") or ""),
            acceptance_criteria=acceptance_criteria(body),
            images=safe_images(body.get("images")),
            files=safe_files(body.get("files")),
        )
        detail = await task_detail(int(claim.task_id))
        try:
            dispatched = await service.dispatch_role(detail.task.id, "director")
        except Exception as exc:
            service.record_initial_dispatch_indeterminate(detail.task.id, error=str(exc))
            await finish_mutation(
                mutation,
                202,
                {
                    "task": detail.task.to_dict(),
                    "presentation": detail.presentation.to_dict(),
                    "dispatch_pending": True,
                    "dispatch_status": "recovery_required",
                },
            )
            return True
        await finish_mutation(
            mutation,
            200,
            {
                "task": detail.task.to_dict(),
                "presentation": detail.presentation.to_dict(),
                "dispatch_pending": False,
                "dispatch_status": "started" if dispatched else "not_started",
            },
        )
        return True
    try:
        task = await service.create_task_after_workspace_decision(
            title=str(body.get("title") or body.get("prompt") or "Relay Task"),
            prompt=str(body.get("prompt") or ""),
            workspace=workspace,
            provider=str(body.get("provider") or ""),
            role_providers=(
                {str(role): str(provider) for role, provider in body.get("role_providers", {}).items()}
                if isinstance(body.get("role_providers"), dict)
                else None
            ),
            images=safe_images(body.get("images")),
            files=safe_files(body.get("files")),
            execution_mode=str(body.get("execution_mode") or "standard"),
            execution_goal=str(body.get("execution_goal") or ""),
            acceptance_criteria=acceptance_criteria(body),
            # Kept out of the new API contract.  Historic stored values remain
            # readable but callers cannot disable system-selected subagents.
            allow_subagents="auto",
            team_strategy=str(body.get("team_strategy") or "none"),
            creation_idempotency_key=claim.key if claim is not None else "",
            active_task_policy=str(body.get("active_task_policy") or ""),
        )
    except ActiveRelayTasksDecisionRequired as exc:
        abandon_mutation(mutation)
        await send_json(
            writer,
            409,
            {
                "error": "当前工作区有进行中的任务，请明确选择后台继续或真实中断后新建。",
                "code": "active_tasks_require_decision",
                "active_tasks": [summary.to_dict() for summary in exc.tasks],
                "allowed_policies": ["continue_background", "interrupt_active"],
            },
        )
        return True
    except (maintenance_error_type, ValueError, RuntimeError) as exc:
        abandon_mutation(mutation)
        await send_json(
            writer,
            423
            if isinstance(exc, maintenance_error_type)
            else 409
            if isinstance(exc, RuntimeError)
            else 400,
            {"error": str(exc)},
        )
        return True
    if claim is not None:
        mutation_store.bind_task(claim.key, task.id)
    try:
        dispatched = await service.dispatch_role(task.id, "director")
    except Exception as exc:
        # The task and its durable idempotency binding already exist.  Finish
        # the request as accepted so retrying the same key cannot create a
        # duplicate; the queued role is recoverable by the lifecycle worker.
        service.record_initial_dispatch_indeterminate(task.id, error=str(exc))
        detail = await task_detail(task.id)
        await finish_mutation(
            mutation,
            202,
            {
                "task": detail.task.to_dict(),
                "presentation": detail.presentation.to_dict(),
                "dispatch_pending": True,
                "dispatch_status": "recovery_required",
            },
        )
        return True
    detail = await task_detail(task.id)
    await finish_mutation(
        mutation,
        200 if dispatched else 202,
        {
            "task": detail.task.to_dict(),
            "presentation": detail.presentation.to_dict(),
            "dispatch_pending": False,
            "dispatch_status": "started" if dispatched else "not_started",
        },
    )
    return True
