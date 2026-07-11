"""Relay API route composition and idempotent mutation boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from wlcodex.live_stream.relay_collection_routes import handle_relay_collection_route
from wlcodex.live_stream.relay_event_route import handle_relay_task_events_route
from wlcodex.live_stream.relay_task_routes import (
    RelayTaskRouteDependencies,
    handle_relay_task_route,
)
from wlcodex.relay.mutations import RelayMutationClaim, RelayMutationStore


@dataclass(frozen=True)
class RelayApiRouteDependencies:
    is_authorized: Callable[..., bool]
    send_json: Callable[..., Awaitable[None]]
    read_request_json: Callable[..., Awaitable[dict[str, Any]]]
    normalize_path: Callable[[str], str]
    presentation_state_filter: Callable[[str], str]
    page_number: Callable[[str], int]
    safe_int: Callable[..., int]
    optional_nonempty_string: Callable[[Any], str | None]
    safe_images: Callable[[Any], list[Any]]
    safe_files: Callable[[Any], list[Any]]
    acceptance_criteria: Callable[[dict[str, Any]], list[str]]
    task_detail: Callable[[int], Awaitable[Any]]
    task_api_parts: Callable[[str], tuple[int, str] | None]
    task_detail_json: Callable[[Any], dict[str, Any]]
    first_blocked_role: Callable[[list[Any]], str]
    schedule_dispatch: Callable[[int, str], None]
    schedule_reconcile: Callable[[int], bool]
    reject_if_maintenance_frozen: Callable[..., Awaitable[bool]]
    maintenance_error_type: type[Exception]


async def handle_relay_api_route(
    *,
    deps: RelayApiRouteDependencies,
    reader: Any,
    writer: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, list[str]],
    service: Any,
    hub: Any,
    send_sse: Callable[..., Awaitable[None]],
) -> None:
    """Dispatch Relay API routes; all mutations are durably idempotent."""

    if service is None:
        await deps.send_json(writer, 503, {"error": "relay service unavailable"})
        return
    if not deps.is_authorized(writer, headers, query, require_token=True):
        await deps.send_json(writer, 401, {"error": "unauthorized"})
        return
    normalized_path = deps.normalize_path(path)

    async def begin_mutation(
        operation: str,
        task_id: int | None,
        payload: dict[str, Any],
    ) -> tuple[RelayMutationStore, RelayMutationClaim | None] | None:
        mutation_store = RelayMutationStore.from_relay_service(service)
        try:
            claim = mutation_store.claim(
                key=headers.get("idempotency-key", ""),
                operation=operation,
                task_id=task_id,
                payload=payload,
            )
        except ValueError as exc:
            await deps.send_json(writer, 400, {"error": str(exc)})
            return None
        if claim is None:
            return mutation_store, None
        if claim.is_replay:
            await deps.send_json(
                writer,
                int(claim.response_status or 200),
                dict(claim.response_payload or {}),
            )
            return None
        if (
            claim.status == "in_progress"
            and claim.operation == "relay.task.create"
            and claim.task_id is None
        ):
            recovered_task_id = mutation_store.recover_task_create_binding(claim.key)
            if recovered_task_id is not None:
                claim = RelayMutationClaim(
                    key=claim.key,
                    status="in_progress",
                    operation=claim.operation,
                    task_id=recovered_task_id,
                )
        if claim.can_resume_task_creation:
            return mutation_store, claim
        if not claim.should_execute:
            await deps.send_json(
                writer,
                409,
                {
                    "error": claim.error or "mutation is already in progress",
                    "task_id": claim.task_id,
                    "retryable": claim.status == "in_progress",
                },
            )
            return None
        return mutation_store, claim

    async def finish_mutation(
        mutation: tuple[RelayMutationStore, RelayMutationClaim | None],
        status: int,
        payload: dict[str, Any],
    ) -> None:
        mutation_store, claim = mutation
        if claim is not None:
            mutation_store.complete(claim.key, status=status, payload=payload)
        await deps.send_json(writer, status, payload)

    def abandon_mutation(mutation: tuple[RelayMutationStore, RelayMutationClaim | None]) -> None:
        mutation_store, claim = mutation
        if claim is not None:
            mutation_store.abandon(claim.key)

    try:
        if await handle_relay_collection_route(
            normalized_path=normalized_path,
            method=method,
            query=query,
            reader=reader,
            writer=writer,
            headers=headers,
            service=service,
            send_json=deps.send_json,
            read_request_json=deps.read_request_json,
            begin_mutation=begin_mutation,
            finish_mutation=finish_mutation,
            abandon_mutation=abandon_mutation,
            presentation_state_filter=deps.presentation_state_filter,
            page_number=deps.page_number,
            safe_int=deps.safe_int,
            optional_nonempty_string=deps.optional_nonempty_string,
            safe_images=deps.safe_images,
            safe_files=deps.safe_files,
            acceptance_criteria=deps.acceptance_criteria,
            task_detail=deps.task_detail,
            maintenance_error_type=deps.maintenance_error_type,
        ):
            return
        await handle_relay_task_route(
            deps=RelayTaskRouteDependencies(
                send_json=deps.send_json,
                read_request_json=deps.read_request_json,
                task_api_parts=deps.task_api_parts,
                task_detail=deps.task_detail,
                task_detail_json=deps.task_detail_json,
                task_events=handle_relay_task_events_route,
                safe_int=deps.safe_int,
                safe_images=deps.safe_images,
                safe_files=deps.safe_files,
                optional_nonempty_string=deps.optional_nonempty_string,
                first_blocked_role=deps.first_blocked_role,
                schedule_dispatch=deps.schedule_dispatch,
                schedule_reconcile=deps.schedule_reconcile,
                reject_if_maintenance_frozen=deps.reject_if_maintenance_frozen,
                begin_mutation=begin_mutation,
                finish_mutation=finish_mutation,
                abandon_mutation=abandon_mutation,
                maintenance_error_type=deps.maintenance_error_type,
            ),
            normalized_path=normalized_path,
            method=method,
            query=query,
            reader=reader,
            writer=writer,
            headers=headers,
            service=service,
            hub=hub,
            send_sse=send_sse,
        )
    except KeyError:
        await deps.send_json(writer, 404, {"error": "relay task not found"})
