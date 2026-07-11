"""Native provider API routes.

Transport-level request parsing remains in :mod:`server`; this module keeps
provider actions together so their read/mutation semantics can be audited as a
single boundary.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote


async def handle_native_agent_route(
    server: Any,
    reader: Any,
    writer: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, list[str]],
    *,
    provider_route_parts: Any,
    json_object: Any,
    optional_nonempty_string: Any,
    safe_images: Any,
    permission_kwargs_from_body: Any,
    collaboration_kwargs_from_body: Any,
    disabled_reason: Any,
    login_ticket_ttl_seconds: int,
) -> None:
    provider_name, provider_suffix = provider_route_parts(path)
    provider = server._native_provider(provider_name)
    if provider is None:
        if provider_name == "codex" and server._native_registry is None:
            if not server._is_authorized(writer, headers, query, require_token=False):
                await server._send_json(writer, 401, {"error": "unauthorized"})
                return
            await server._send_json(writer, 503, {"error": "native controller unavailable"})
            return
        await server._send_json(writer, 404, {"error": "unknown native provider"})
        return
    legacy_codex_controller = (
        provider_name == "codex"
        and server._native_registry is None
        and server._native_controller is not None
    )
    if not server._is_authorized(writer, headers, query, require_token=provider is not None):
        await server._send_json(writer, 401, {"error": "unauthorized"})
        return
    target = server._native_controller if legacy_codex_controller else provider
    route = f"/{provider_suffix}" if provider_suffix else ""
    if route == "/login-ticket":
        if not server._access_token:
            await server._send_json(writer, 404, {"error": "not found"})
            return
        if method != "POST":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        ticket = server._mint_login_ticket()
        await server._send_json(
            writer,
            200,
            {
                "ticket": ticket,
                "path": f"/native/{quote(provider_name, safe='')}/login?ticket={quote(ticket, safe='')}",
                "expires_in": login_ticket_ttl_seconds,
            },
        )
        return
    if route == "/status":
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        await server._send_json(writer, 200, json_object(await target.status()))
        return
    if route == "/capabilities":
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        await server._send_json(writer, 200, provider.capabilities().to_json_dict())
        return
    if route == "/sessions/stream":
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        await server._send_native_sessions_sse(
            writer, provider_name, target, legacy_codex_controller=legacy_codex_controller
        )
        return
    if route == "/sessions":
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        payload = await server._native_sessions_payload(
            provider_name, target, legacy_codex_controller=legacy_codex_controller,
            fresh=False, schedule_refresh=False,
        )
        await server._send_json(writer, 200, payload)
        return
    if route == "/models":
        if method != "GET":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        provider_label = {"codex": "Codex", "claude": "Claude", "antigravity": "Antigravity"}.get(
            provider_name, provider_name or "Native"
        )
        try:
            models = await target.list_models()
        except Exception:
            await server._send_json(
                writer,
                503,
                {
                    "error": f"无法同步 {provider_label} 模型目录，请检查提供方二进制和 app-server 配置后重试。",
                    "models": [],
                    "freshness": {"source": "unavailable", "updated_at": "", "is_stale": True, "reason": f"{provider_label} 模型目录同步失败"},
                    "recovery": "检查提供方二进制与 app-server 配置，然后重试。",
                },
            )
            return
        if not models:
            await server._send_json(
                writer,
                503,
                {
                    "error": f"{provider_label} 未返回可用模型，请检查 app-server 配置后重试。",
                    "models": [],
                    "freshness": {"source": "unavailable", "updated_at": "", "is_stale": True, "reason": f"{provider_label} 未返回可用模型"},
                    "recovery": "检查提供方二进制与 app-server 配置，然后重试。",
                },
            )
            return
        await server._send_json(writer, 200, {"models": [json_object(model) for model in models]})
        return
    if route == "/sessions/start":
        if method != "POST":
            await server._send_json(writer, 405, {"error": "method not allowed"})
            return
        if await server._reject_if_maintenance_frozen(writer):
            return
        body = await server._read_request_json(writer, reader, headers)
        if body is None:
            return
        mutation = await server._begin_native_mutation(
            writer, headers=headers, operation=f"native.{provider_name}.sessions.start",
            payload={"provider": provider_name, "body": body},
        )
        if mutation is None:
            return
        prompt = str(body.get("prompt", ""))
        model = optional_nonempty_string(body.get("model"))
        images = safe_images(body.get("images"))
        permission_kwargs = permission_kwargs_from_body(provider_name, body)
        collaboration_kwargs = collaboration_kwargs_from_body(provider_name, body)
        service_tier = optional_nonempty_string(body.get("service_tier") or body.get("serviceTier"))
        try:
            if prompt.strip() or images:
                result = await target.start_session(
                    str(body.get("cwd", "")), prompt, model=model,
                    effort=optional_nonempty_string(body.get("effort")), service_tier=service_tier,
                    images=images, **permission_kwargs, **collaboration_kwargs,
                )
            else:
                create_permission_kwargs = dict(permission_kwargs)
                create_permission_kwargs.pop("sandbox_policy", None)
                result = await target.create_session(
                    str(body.get("cwd", "")), model=model, service_tier=service_tier,
                    **create_permission_kwargs,
                )
        except ValueError as exc:
            server._abandon_native_mutation(mutation)
            await server._send_json(writer, 400, {"error": str(exc)})
            return
        await server._finish_native_mutation(writer, mutation, status=200, payload=json_object(result))
        return
    if route.startswith("/approvals/"):
        parts = [unquote(part) for part in route[len("/approvals/") :].split("/") if part]
        if len(parts) == 2 and parts[1] == "resolve" and method == "POST":
            body = await server._read_request_json(writer, reader, headers)
            if body is None:
                return
            mutation = await server._begin_native_mutation(
                writer, headers=headers, operation=f"native.{provider_name}.approvals.resolve",
                payload={"provider": provider_name, "approval_id": parts[0], "body": body},
            )
            if mutation is None:
                return
            try:
                result = await target.resolve_approval(parts[0], body)
            except KeyError:
                server._abandon_native_mutation(mutation)
                await server._send_json(writer, 404, {"error": "approval request not found"})
                return
            except ValueError as exc:
                server._abandon_native_mutation(mutation)
                await server._send_json(writer, 400, {"error": str(exc)})
                return
            await server._finish_native_mutation(writer, mutation, status=200, payload=json_object(result))
            return
        await server._send_json(writer, 404, {"error": "not found"})
        return
    if not route.startswith("/sessions/"):
        await server._send_json(writer, 404, {"error": "not found"})
        return
    parts = [unquote(part) for part in route[len("/sessions/") :].split("/") if part]
    if not parts:
        await server._send_json(writer, 404, {"error": "not found"})
        return
    thread_id, action = parts[0], parts[1] if len(parts) > 1 else ""
    if method == "GET" and action == "" and len(parts) == 1:
        try:
            session = await server._native_session_payload(provider_name, target, thread_id)
        except KeyError:
            await server._send_json(writer, 404, {"error": "native session not found"})
            return
        await server._send_json(writer, 200, json_object(session))
        return
    if method == "POST" and action in {"attach", "sync"} and len(parts) == 2:
        mutation = await server._begin_native_mutation(
            writer, headers=headers, operation=f"native.{provider_name}.sessions.{action}",
            payload={"provider": provider_name, "thread_id": thread_id},
        )
        if mutation is None:
            return
        try:
            session = await (target.attach_session(thread_id) if action == "attach" else target.sync_session(thread_id))
        except KeyError:
            server._abandon_native_mutation(mutation)
            await server._send_json(writer, 404, {"error": "native session not found"})
            return
        await server._finish_native_mutation(writer, mutation, status=200, payload=json_object(session))
        return
    if method == "POST" and action in {"continue", "steer"} and len(parts) == 2:
        if await server._reject_if_maintenance_frozen(writer):
            return
        capabilities = provider.capabilities()
        capability_name = "can_continue_session" if action == "continue" else "can_steer_active_turn"
        if not getattr(capabilities, capability_name):
            await server._send_json(writer, 409, {"error": disabled_reason(capabilities, capability_name)})
            return
        body = await server._read_request_json(writer, reader, headers)
        if body is None:
            return
        mutation = await server._begin_native_mutation(
            writer, headers=headers, operation=f"native.{provider_name}.sessions.{action}",
            payload={"provider": provider_name, "thread_id": thread_id, "body": body},
        )
        if mutation is None:
            return
        permission_kwargs = permission_kwargs_from_body(provider_name, body)
        if provider_name.strip().lower() != "antigravity":
            permission_kwargs.pop("sandbox", None)
        try:
            if action == "continue":
                collaboration_kwargs = collaboration_kwargs_from_body(provider_name, body)
                continue_kwargs: dict[str, Any] = {}
                if body.get("force_new_turn") is True or body.get("forceNewTurn") is True:
                    continue_kwargs["force_new_turn"] = True
                result = await target.continue_session(
                    thread_id, str(body.get("prompt", "")),
                    model=optional_nonempty_string(body.get("model")),
                    effort=optional_nonempty_string(body.get("effort")),
                    service_tier=optional_nonempty_string(body.get("service_tier") or body.get("serviceTier")),
                    images=safe_images(body.get("images")), **permission_kwargs,
                    **collaboration_kwargs, **continue_kwargs,
                )
            else:
                result = await target.steer_session(
                    thread_id, str(body.get("expected_turn_id") or body.get("turn_id") or ""),
                    str(body.get("prompt", "")), model=optional_nonempty_string(body.get("model")),
                    effort=optional_nonempty_string(body.get("effort")),
                    service_tier=optional_nonempty_string(body.get("service_tier") or body.get("serviceTier")),
                    images=safe_images(body.get("images")), **permission_kwargs,
                )
        except KeyError:
            server._abandon_native_mutation(mutation)
            await server._send_json(writer, 404, {"error": "native session not found"})
            return
        await server._finish_native_mutation(writer, mutation, status=200, payload=json_object(result))
        return
    if method == "POST" and action == "interrupt" and len(parts) == 2:
        capabilities = provider.capabilities()
        if not capabilities.can_interrupt:
            await server._send_json(writer, 409, {"error": disabled_reason(capabilities, "can_interrupt")})
            return
        body = await server._read_request_json(writer, reader, headers)
        if body is None:
            return
        mutation = await server._begin_native_mutation(
            writer, headers=headers, operation=f"native.{provider_name}.sessions.interrupt",
            payload={"provider": provider_name, "thread_id": thread_id, "body": body},
        )
        if mutation is None:
            return
        try:
            result = await target.interrupt_session(thread_id, str(body.get("turn_id", "")))
        except KeyError:
            server._abandon_native_mutation(mutation)
            await server._send_json(writer, 404, {"error": "native session not found"})
            return
        await server._finish_native_mutation(writer, mutation, status=200, payload=json_object(result))
        return
    await server._send_json(writer, 404, {"error": "not found"})
