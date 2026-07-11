"""Native page, message, and timeline route dispatch.

HTTP parsing and transport remain in :mod:`server`; this module owns the
Native URL contract so that page/API routing evolves independently from the
server lifecycle.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class NativeUiRouteDependencies:
    is_authorized: Callable[..., bool]
    send_json: Callable[..., Awaitable[None]]
    send_html: Callable[..., Awaitable[None]]
    send_redirect: Callable[..., Awaitable[None]]
    token_entry_page: Callable[[str], str]
    login_ticket_page: Callable[[str, str], str]
    native_provider: Callable[[str], Any | None]
    send_provider_index: Callable[..., Awaitable[None]]
    send_native_page: Callable[..., Awaitable[None]]
    send_timeline_page: Callable[..., Awaitable[None]]
    send_messages_json: Callable[..., Awaitable[None]]
    send_messages_sse: Callable[..., Awaitable[None]]
    send_timeline_json: Callable[..., Awaitable[None]]
    send_timeline_sse: Callable[..., Awaitable[None]]
    has_login_ticket: Callable[[str], bool]
    consume_login_ticket: Callable[[str], bool]
    login_cookie_header: Callable[[str], str]
    login_provider_from_path: Callable[[str], str]
    page_provider_from_path: Callable[[str], str]
    messages_route_from_path: Callable[[str], tuple[str, str, bool] | None]
    timeline_route_from_path: Callable[[str], tuple[str, str, bool] | None]
    safe_int: Callable[..., int]


async def handle_native_ui_route(
    *,
    deps: NativeUiRouteDependencies,
    writer: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, list[str]],
    native_registry: Any | None,
    native_controller: Any | None,
    access_token: str,
    allow_unauthenticated_loopback: bool,
    is_loopback_peer: Callable[[Any], bool],
) -> bool:
    """Handle a Native URL, returning ``False`` when it is not in scope."""

    native_available = native_controller is not None or native_registry is not None
    if path in ("", "/") and native_available:
        if method != "GET":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        landing_path = "/native" if native_registry is not None else "/native/codex"
        if not access_token or (allow_unauthenticated_loopback and is_loopback_peer(writer)):
            await deps.send_redirect(writer, landing_path)
            return True
        await deps.send_html(writer, 200, deps.token_entry_page(landing_path))
        return True

    if path == "/native":
        if method != "GET":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        await deps.send_provider_index(writer, headers, query)
        return True

    login_provider = deps.login_provider_from_path(path)
    if login_provider:
        if deps.native_provider(login_provider) is None:
            await deps.send_json(writer, 404, {"error": "unknown native provider"})
            return True
        safe_provider = quote(login_provider, safe="")
        if not access_token:
            await deps.send_redirect(writer, f"/native/{safe_provider}")
            return True
        if method == "GET":
            ticket = query.get("ticket", [""])[0]
            if not deps.has_login_ticket(ticket):
                await deps.send_html(writer, 401, deps.token_entry_page("/native/codex"))
                return True
            await deps.send_html(writer, 200, deps.login_ticket_page(ticket, login_provider))
            return True
        if method != "POST":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if not deps.consume_login_ticket(query.get("ticket", [""])[0]):
            await deps.send_html(writer, 401, deps.token_entry_page("/native/codex"))
            return True
        await deps.send_redirect(
            writer,
            f"/native/{safe_provider}",
            headers={"Set-Cookie": deps.login_cookie_header(access_token)},
        )
        return True

    if path in {"/native/codex", "/native/codex-v2"}:
        if method != "GET":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if not deps.is_authorized(
            writer,
            headers,
            query,
            require_token=native_controller is not None,
        ):
            target = "/native/codex-v2" if path.endswith("-v2") else "/native/codex"
            await deps.send_html(writer, 401, deps.token_entry_page(target))
            return True
        if path.endswith("-v2"):
            await deps.send_timeline_page(writer, "codex", headers, query)
        else:
            await deps.send_native_page(writer, "codex", headers, query)
        return True

    # Workflow navigation belongs to Relay's own route module.  It has the
    # same two-segment shape as a provider URL, so protect it before applying
    # the generic provider parser.
    if path == "/native/workflows" or path.startswith("/native/workflows/"):
        return False

    native_provider = deps.page_provider_from_path(path)
    if native_provider:
        if method != "GET":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        await deps.send_native_page(writer, native_provider, headers, query)
        return True

    messages_route = deps.messages_route_from_path(path)
    if messages_route is not None:
        if method != "GET":
            await deps.send_json(writer, 405, {"error": "method not allowed"})
            return True
        if not deps.is_authorized(writer, headers, query):
            await deps.send_json(writer, 401, {"error": "unauthorized"})
            return True
        provider, native_thread_id, stream = messages_route
        if stream:
            after_update = deps.safe_int(
                query.get("after_update", query.get("after", [headers.get("last-event-id", "0")]))[0],
                default=0,
            )
            await deps.send_messages_sse(writer, provider, native_thread_id, after_update)
            return True
        after = deps.safe_int(query.get("after", ["0"])[0], default=0)
        after_update = deps.safe_int(query.get("after_update", ["0"])[0], default=0)
        before_value = query.get("before", [""])[0]
        before = deps.safe_int(before_value, default=0) if str(before_value).strip() else None
        limit = deps.safe_int(query.get("limit", ["100"])[0], default=100)
        await deps.send_messages_json(
            writer,
            provider,
            native_thread_id,
            after=after,
            after_update=after_update,
            before=before,
            limit=limit,
        )
        return True

    timeline_route = deps.timeline_route_from_path(path)
    if timeline_route is None:
        return False
    if method != "GET":
        await deps.send_json(writer, 405, {"error": "method not allowed"})
        return True
    if not deps.is_authorized(writer, headers, query):
        await deps.send_json(writer, 401, {"error": "unauthorized"})
        return True
    provider, native_thread_id, stream = timeline_route
    if stream:
        after = deps.safe_int(query.get("after", [headers.get("last-event-id", "0")])[0], default=0)
        await deps.send_timeline_sse(writer, provider, native_thread_id, after)
        return True
    after = deps.safe_int(query.get("after", ["0"])[0], default=0)
    before_value = query.get("before", [""])[0]
    before = deps.safe_int(before_value, default=0) if str(before_value).strip() else None
    limit = deps.safe_int(query.get("limit", ["100"])[0], default=100)
    await deps.send_timeline_json(
        writer,
        provider,
        native_thread_id,
        after=after,
        before=before,
        limit=limit,
        item_snapshot=True,
    )
    return True
