"""Relay collection-route handlers (configuration and task collection)."""

from __future__ import annotations

from typing import Any


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
    try:
        task = service.create_task(
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
            allow_subagents=str(body.get("allow_subagents") or "auto"),
            team_strategy=str(body.get("team_strategy") or "none"),
        )
    except (maintenance_error_type, ValueError) as exc:
        abandon_mutation(mutation)
        await send_json(
            writer,
            423 if isinstance(exc, maintenance_error_type) else 400,
            {"error": str(exc)},
        )
        return True
    mutation_store, claim = mutation
    if claim is not None:
        mutation_store.bind_task(claim.key, task.id)
    await service.dispatch_role(task.id, "director")
    detail = await task_detail(task.id)
    await finish_mutation(
        mutation,
        200,
        {"task": detail.task.to_dict(), "presentation": detail.presentation.to_dict()},
    )
    return True
