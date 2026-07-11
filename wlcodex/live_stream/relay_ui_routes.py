"""Read-only Relay workspace routes.

The web server owns connection parsing and authentication.  This module owns
the user-facing Relay navigation contract: every page is assembled from
read-only projections and no GET can advance a task lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class RelayUiRouteDependencies:
    """Explicit server seams used by the Relay HTML route handler."""

    is_authorized: Callable[..., bool]
    send_json: Callable[..., Awaitable[None]]
    send_html: Callable[..., Awaitable[None]]
    send_redirect: Callable[..., Awaitable[None]]
    token_entry_page: Callable[[str], str]
    native_workflows_page: Callable[..., str]
    selected_workspace: Callable[[str, list[Any]], str]
    project_rows: Callable[[Any], list[dict[str, Any]]]
    settings_href: Callable[[str, str], str]
    chat_home_page: Callable[..., str]
    blocked_inbox_page: Callable[..., str]
    page_number: Callable[[str], int]
    presentation_state_filter: Callable[[str], str]
    task_list_page: Callable[..., str]
    config_page: Callable[..., str]
    task_id_from_path: Callable[[str], int | None]
    task_detail_view: Callable[[str], str]
    task_detail_page: Callable[..., str]
    task_detail: Callable[[int], Awaitable[Any]]
    task_detail_read_model: Callable[[int], Awaitable[Any]] | None = None


async def handle_relay_ui_route(
    *,
    deps: RelayUiRouteDependencies,
    writer: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, list[str]],
    native_registry: Any,
    native_controller: Any,
    relay_service: Any,
    workspace_catalog: Any,
    hub: Any,
) -> None:
    """Render Relay navigation and detail pages without mutating task state."""
    if method != "GET":
        await deps.send_json(writer, 405, {"error": "method not allowed"})
        return
    if not deps.is_authorized(
        writer,
        headers,
        query,
        require_token=(
            native_registry is not None
            or native_controller is not None
            or relay_service is not None
        ),
    ):
        await deps.send_html(writer, 401, deps.token_entry_page(path))
        return
    token = str((query.get("token") or [""])[0] or "")
    if path == "/native/workflows":
        await deps.send_html(writer, 200, deps.native_workflows_page(access_token=token))
        return

    if path == "/native/workflows/relay/office":
        selected_workspace = deps.selected_workspace(
            str((query.get("workspace") or [""])[0] or ""),
            deps.project_rows(workspace_catalog),
        )
        await deps.send_redirect(writer, deps.settings_href(selected_workspace, token))
        return

    relay_roots = (
        "/native/workflows/relay",
        "/native/workflows/relay/inbox",
        "/native/workflows/relay/chat",
        "/native/workflows/relay/config",
        "/native/workflows/relay/skills",
        "/native/workflows/relay/profile",
    )
    if path in relay_roots:
        project_rows = deps.project_rows(workspace_catalog)
        selected_workspace = deps.selected_workspace(
            str((query.get("workspace") or [""])[0] or ""), project_rows
        )
    else:
        project_rows = []
        selected_workspace = ""

    if path in ("/native/workflows/relay/skills", "/native/workflows/relay/profile"):
        await deps.send_redirect(writer, deps.settings_href(selected_workspace, token))
        return
    if path == "/native/workflows/relay/chat":
        await deps.send_html(
            writer,
            200,
            deps.chat_home_page(selected_workspace=selected_workspace, access_token=token),
        )
        return
    if path == "/native/workflows/relay/inbox":
        summaries = (
            relay_service.list_tasks_readonly(workspace=selected_workspace)
            if relay_service is not None
            else []
        )
        await deps.send_html(
            writer,
            200,
            deps.blocked_inbox_page(
                summaries, selected_workspace=selected_workspace, access_token=token
            ),
        )
        return
    if path == "/native/workflows/relay":
        requested_page = deps.page_number(str((query.get("page") or ["1"])[0] or "1"))
        status_filter = deps.presentation_state_filter(
            str((query.get("status") or [""])[0] or "")
        )
        relay_states = (
            "running", "waiting_user", "waiting_approval", "blocked", "failed",
            "completed", "interrupted", "stale",
        )
        page_size = 10
        if relay_service is not None:
            summaries, total, queried_state_counts = relay_service.list_tasks_page_readonly(
                workspace=selected_workspace,
                presentation_state=status_filter or None,
                page=requested_page,
                page_size=page_size,
            )
        else:
            summaries, total, queried_state_counts = [], 0, {}
        state_counts = {
            state: int(queried_state_counts.get(state, 0)) for state in relay_states
        }
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(max(1, requested_page), total_pages)
        if current_page != requested_page and relay_service is not None:
            summaries, _, _ = relay_service.list_tasks_page_readonly(
                workspace=selected_workspace,
                presentation_state=status_filter or None,
                page=current_page,
                page_size=page_size,
            )
        active_count = sum(
            state_counts.get(state, 0)
            for state in ("running", "waiting_user", "waiting_approval", "blocked", "stale")
        )
        providers = native_registry.list_provider_summaries() if native_registry is not None else []
        relay_config = relay_service.config() if relay_service is not None else {}
        await deps.send_html(
            writer,
            200,
            deps.task_list_page(
                summaries,
                providers=providers,
                relay_config=relay_config,
                projects=project_rows,
                selected_workspace=selected_workspace,
                access_token=token,
                page=current_page,
                total=total,
                total_pages=total_pages,
                active_count=active_count,
                state_counts=state_counts,
                status_filter=status_filter,
            ),
        )
        return
    if path == "/native/workflows/relay/config":
        providers = native_registry.list_provider_summaries() if native_registry is not None else []
        relay_config = relay_service.config() if relay_service is not None else {}
        await deps.send_html(
            writer,
            200,
            deps.config_page(
                providers=providers,
                relay_config=relay_config,
                selected_workspace=selected_workspace,
                access_token=token,
            ),
        )
        return

    task_id = deps.task_id_from_path(path)
    if task_id is None:
        await deps.send_json(writer, 404, {"error": "not found"})
        return
    if relay_service is None:
        await deps.send_json(writer, 503, {"error": "relay service unavailable"})
        return
    try:
        read_model = (
            await deps.task_detail_read_model(task_id)
            if deps.task_detail_read_model is not None
            else None
        )
        detail = read_model.detail if read_model is not None else await deps.task_detail(task_id)
    except KeyError:
        await deps.send_json(writer, 404, {"error": "relay task not found"})
        return
    detail_view = deps.task_detail_view(
        str((query.get("view") or ["conversation"])[0] or "conversation")
    )
    await deps.send_html(
        writer,
        200,
        deps.task_detail_page(
            detail,
            access_token=token,
            view=detail_view,
            events=read_model.events if read_model is not None else relay_service.events_for_task(task_id),
            hub=hub,
            token_stats=(
                read_model.token_stats
                if read_model is not None
                else relay_service.task_token_stats(task_id)
            ),
        ),
    )
