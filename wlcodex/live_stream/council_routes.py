"""Council HTTP routes, separated from the live-stream transport."""

from __future__ import annotations

from typing import Any

from wlcodex.council import CouncilReviewService, NativeProviderCouncilReviewer


async def handle_council_route(
    server: Any,
    reader: Any,
    writer: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, list[str]],
    *,
    run_id_from_path: Any,
    projects_payload: Any,
    packet_from_body: Any,
    public_run_payload: Any,
    provider_resolver: Any,
) -> None:
    """Handle Council reads and starts while retaining server-owned run state."""
    if not server._is_authorized(
        writer,
        headers,
        query,
        require_token=(server._native_registry is not None or server._native_controller is not None),
    ):
        await server._send_json(writer, 401, {"error": "unauthorized"})
        return
    if path == "/api/council/config/default":
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        await server._send_json(writer, 200, await server._default_council_config_payload())
        return
    if path == "/api/council/projects":
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        await server._send_json(
            writer, 200, projects_payload(workspaces=server._workspace_catalog)
        )
        return
    if path == "/api/council/runs":
        if method != "POST":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        body = await server._read_request_json(writer, reader, headers)
        if body is None:
            return
        try:
            packet = packet_from_body(body)
            config = await server._council_config_from_body(body)
            if bool(body.get("async")):
                await server._send_json(
                    writer,
                    200,
                    server._start_async_council_run(
                        packet=packet, config=config, cwd=str(body.get("cwd") or "")
                    ),
                )
                return
            reviewer = NativeProviderCouncilReviewer(
                provider_resolver=provider_resolver(server), default_cwd=str(body.get("cwd") or "")
            )
            board = await CouncilReviewService(reviewer=reviewer).review_packet(
                packet=packet, config=config
            )
        except ValueError as exc:
            await server._send_json(writer, 400, {"error": str(exc)})
            return
        await server._send_json(writer, 200, board.to_json_dict())
        return
    run_id = run_id_from_path(path)
    if run_id:
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        run = server._council_runs.get(run_id)
        if run is None:
            await server._send_json(writer, 404, {"error": "council run not found"})
            return
        await server._send_json(writer, 200, public_run_payload(run))
        return
    await server._send_json(writer, 404, {"error": "not found"})
