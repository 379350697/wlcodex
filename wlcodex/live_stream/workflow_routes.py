"""Native workflow handoff route contract.

The server injects transport and authorization primitives so the route remains
independent of the socket server's lifecycle state.
"""

from __future__ import annotations

from typing import Any


async def handle_workflow_route(
    reader: Any,
    writer: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    *,
    workflow_service: Any | None,
    authorized: Any,
    require_token: bool,
    send_json: Any,
    read_request_json: Any,
    json_object: Any,
) -> None:
    if workflow_service is None:
        await send_json(writer, 503, {"error": "workflow service unavailable"})
        return
    if not authorized(writer, headers, require_token=require_token):
        await send_json(writer, 401, {"error": "unauthorized"})
        return
    if path not in {
        "/api/native/workflows/handoffs/preview",
        "/api/native/workflows/handoffs/execute",
    }:
        await send_json(writer, 404, {"error": "not found"})
        return
    if method != "POST":
        await send_json(writer, 405, {"error": "method not allowed"})
        return
    body = await read_request_json(writer, reader, headers)
    if body is None:
        return
    try:
        if path.endswith("/preview"):
            result = await workflow_service.preview_handoff(
                source_provider=str(body.get("source_provider") or body.get("sourceProvider") or ""),
                source_thread_id=str(body.get("source_thread_id") or body.get("sourceThreadId") or ""),
                source_turn_id=str(body.get("source_turn_id") or body.get("sourceTurnId") or ""),
                target_provider=str(body.get("target_provider") or body.get("targetProvider") or ""),
                cwd=str(body.get("cwd") or ""),
                intent=str(body.get("intent") or ""),
                user_note=str(body.get("user_note") or body.get("userNote") or ""),
            )
        else:
            result = await workflow_service.execute_handoff(
                workflow_run_id=str(body.get("workflow_run_id") or body.get("workflowRunId") or ""),
                preview_id=str(body.get("preview_id") or body.get("previewId") or ""),
                target_provider=str(body.get("target_provider") or body.get("targetProvider") or ""),
                cwd=str(body.get("cwd") or ""),
                prompt=str(body.get("prompt") or ""),
            )
    except KeyError as exc:
        await send_json(writer, 404, {"error": str(exc)})
        return
    except ValueError as exc:
        await send_json(writer, 409, {"error": str(exc)})
        return
    await send_json(writer, 200, json_object(result))
