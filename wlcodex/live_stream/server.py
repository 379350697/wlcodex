from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4

from wlcodex.auto_digest_llm import DigestClient
from wlcodex.council import (
    CouncilConfig,
    CouncilReviewPacket,
    CouncilReviewRequest,
    CouncilReviewResult,
    CouncilReviewService,
    CouncilSeat,
    CouncilSeatAssignment,
    CouncilSynthesis,
    NativeProviderCouncilReviewer,
    build_council_seats,
    council_assignment_diversity,
    default_council_config,
    default_council_seat_definitions,
)
from wlcodex.live_stream.collapse import (
    LiveTurnSummaryConfig,
    summarize_turn_with_sidecar,
)
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.models import WorkerStreamEvent
from wlcodex.jsonrpc import JsonRpcError


_REQUEST_TIMEOUT_SECONDS = 5.0
_MAX_HEADER_BYTES = 16 * 1024
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_NATIVE_IMAGE_ATTACHMENTS = 8
_LOGIN_TICKET_TTL_SECONDS = 5 * 60
_LOGIN_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_COUNCIL_PROJECTS_ROOT = Path.home() / "projects"


class RequestBodyTooLarge(ValueError):
    pass


class WorkerLiveStreamServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        hub: WorkerLiveStreamHub,
        native_controller: Any = None,
        native_registry: Any = None,
        access_token: str | None = None,
        allow_unauthenticated_loopback: bool = True,
        turn_summary_config: LiveTurnSummaryConfig | None = None,
        turn_summary_client: DigestClient | None = None,
        native_transcript_mirror: Any = None,
    ) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError(f"Worker live stream server is loopback-only, got {host!r}")
        self.host = host
        self.port = port
        self._hub = hub
        self._native_controller = native_controller
        self._native_registry = native_registry
        self._access_token = access_token
        self._allow_unauthenticated_loopback = allow_unauthenticated_loopback
        self._turn_summary_config = turn_summary_config or LiveTurnSummaryConfig.from_env()
        self._turn_summary_client = turn_summary_client
        self._native_transcript_mirror = native_transcript_mirror
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._council_runs: dict[str, dict[str, Any]] = {}
        self._council_run_tasks: set[asyncio.Task[None]] = set()
        self._login_tickets: dict[str, float] = {}

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.host,
            port=self.port,
        )
        socket = self._server.sockets[0]
        self.port = int(socket.getsockname()[1])

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        tasks = [task for task in self._client_tasks if task is not asyncio.current_task()]
        tasks.extend(
            task
            for task in self._council_run_tasks
            if task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            request_line = await asyncio.wait_for(
                reader.readline(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return
            method, target, _version = (
                request_line.decode("utf-8", errors="replace").strip().split(" ", 2)
            )
            headers: dict[str, str] = {}
            header_bytes = len(request_line)
            while True:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                header_bytes += len(line)
                if header_bytes > _MAX_HEADER_BYTES:
                    await self._send_json(
                        writer,
                        431,
                        {"error": "request headers too large"},
                    )
                    return
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if ":" in decoded:
                    name, value = decoded.split(":", 1)
                    headers[name.lower()] = value.strip()

            parsed = urlparse(target)
            query = parse_qs(parsed.query)

            if parsed.path == "/health":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await self._send_json(
                    writer,
                    200,
                    {"status": "ok", "service": "worker-live-stream"},
                )
                return

            if parsed.path in ("", "/") and (
                self._native_controller is not None or self._native_registry is not None
            ):
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                native_landing_path = (
                    "/native" if self._native_registry is not None else "/native/codex"
                )
                if not self._access_token or (
                    self._allow_unauthenticated_loopback and _is_loopback_peer(writer)
                ):
                    await self._send_redirect(writer, native_landing_path)
                    return
                await self._send_html(
                    writer,
                    200,
                    _native_token_entry_page(native_landing_path),
                )
                return

            if parsed.path == "/native":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await self._send_native_provider_index(writer, headers, query)
                return

            login_provider = _native_login_provider_from_path(parsed.path)
            if login_provider:
                if self._native_provider(login_provider) is None:
                    await self._send_json(
                        writer,
                        404,
                        {"error": "unknown native provider"},
                    )
                    return
                safe_login_provider = quote(login_provider, safe="")
                if not self._access_token:
                    await self._send_redirect(writer, f"/native/{safe_login_provider}")
                    return
                if method == "GET":
                    ticket = query.get("ticket", [""])[0]
                    if not self._has_login_ticket(ticket):
                        await self._send_html(writer, 401, _native_token_entry_page())
                        return
                    await self._send_html(
                        writer,
                        200,
                        _native_login_ticket_page(ticket, login_provider),
                    )
                    return
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                if not self._consume_login_ticket(query.get("ticket", [""])[0]):
                    await self._send_html(writer, 401, _native_token_entry_page())
                    return
                await self._send_redirect(
                    writer,
                    f"/native/{safe_login_provider}",
                    headers={"Set-Cookie": _login_cookie_header(self._access_token or "")},
                )
                return

            if parsed.path == "/native/codex":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                if not self._is_authorized(
                    writer,
                    headers,
                    query,
                    require_token=self._native_controller is not None,
                ):
                    await self._send_html(writer, 401, _native_token_entry_page())
                    return
                await self._send_native_page(writer, "codex", headers, query)
                return

            native_provider = _native_page_provider_from_path(parsed.path)
            if native_provider:
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await self._send_native_page(writer, native_provider, headers, query)
                return

            if parsed.path.startswith("/api/native/"):
                await self._handle_native_agent_route(
                    reader,
                    writer,
                    method,
                    parsed.path,
                    headers,
                    query,
                )
                return

            if parsed.path in ("/council", "/council/seats"):
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                if not self._is_authorized(
                    writer,
                    headers,
                    query,
                    require_token=(
                        self._native_registry is not None
                        or self._native_controller is not None
                    ),
                ):
                    await self._send_html(writer, 401, _native_token_entry_page())
                    return
                page = (
                    _council_seats_page()
                    if parsed.path == "/council/seats"
                    else _council_review_page()
                )
                await self._send_html(writer, 200, page)
                return

            if parsed.path.startswith("/api/council/"):
                await self._handle_council_route(
                    reader,
                    writer,
                    method,
                    parsed.path,
                    headers,
                    query,
                )
                return

            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return

            if (
                parsed.path.startswith("/api/workers/")
                or parsed.path.startswith("/workers/")
            ) and not self._is_authorized(writer, headers, query):
                await self._send_json(writer, 401, {"error": "unauthorized"})
                return

            agent_id = _agent_id_from_path(
                parsed.path,
                prefix="/api/workers/",
                suffix="/events",
            )
            if agent_id is not None:
                after = _safe_int(query.get("after", ["0"])[0], default=0)
                limit = _safe_int(query.get("limit", ["500"])[0], default=500)
                native_thread_id = _optional_nonempty_string(
                    query.get("native_thread_id", [""])[0]
                ) or ""
                native_turn_id = _optional_nonempty_string(
                    query.get("native_turn_id", [""])[0]
                ) or ""
                native_sync_error = self._sync_native_transcript(native_thread_id)
                previous_event_count = 0
                if "tail" in query:
                    tail_limit = _safe_int(query.get("tail", ["80"])[0], default=80)
                    snapshot = self._hub.snapshot_tail(
                        agent_run_id=agent_id,
                        limit=tail_limit,
                    )
                    previous_event_count = snapshot.previous_event_count
                    events = snapshot.events
                    if native_turn_id:
                        events, extra_previous = _filter_tail_to_native_turn(
                            events,
                            native_turn_id=native_turn_id,
                        )
                        previous_event_count += extra_previous
                elif "before" in query:
                    before = _safe_int(query.get("before", ["0"])[0], default=0)
                    snapshot = self._hub.snapshot_before(
                        agent_run_id=agent_id,
                        before_id=before,
                        limit=limit,
                    )
                    events = snapshot.events
                    previous_event_count = snapshot.previous_event_count
                else:
                    events = self._hub.snapshot(
                        agent_run_id=agent_id,
                        after_id=after,
                        limit=limit,
                    )
                    if native_turn_id:
                        events = _filter_events_for_native_turn(
                            events,
                            native_turn_id=native_turn_id,
                        )
                await self._send_json(
                    writer,
                    200,
                    {
                        "agent_run_id": agent_id,
                        "events": [event.to_json_dict() for event in events],
                        "previous_event_count": previous_event_count,
                        "native_sync_error": native_sync_error,
                    },
                )
                return

            agent_id = _agent_id_from_path(
                parsed.path,
                prefix="/api/workers/",
                suffix="/turn-summary",
            )
            if agent_id is not None:
                native_turn_id = _optional_nonempty_string(
                    query.get("native_turn_id", [""])[0]
                )
                if not native_turn_id:
                    await self._send_json(
                        writer,
                        400,
                        {"error": "native_turn_id is required"},
                    )
                    return
                events = _filter_events_for_native_turn(
                    self._hub.snapshot_tail(agent_run_id=agent_id, limit=2000).events,
                    native_turn_id=native_turn_id,
                )
                summary = await summarize_turn_with_sidecar(
                    events,
                    current_turn_id=_optional_nonempty_string(
                        query.get("current_turn_id", [""])[0]
                    )
                    or "",
                    config=self._turn_summary_config,
                    client=self._turn_summary_client,
                )
                await self._send_json(
                    writer,
                    200,
                    {"agent_run_id": agent_id, "summary": summary.to_json_dict()},
                )
                return

            agent_id = _agent_id_from_path(
                parsed.path,
                prefix="/workers/",
                suffix="/live",
            )
            if agent_id is not None:
                native_provider = (
                    _optional_nonempty_string(
                        query.get("native_provider", query.get("provider", ["codex"]))[0]
                    )
                    or "codex"
                )
                if self._native_provider(native_provider) is None:
                    native_provider = "codex"
                await self._send_html(
                    writer,
                    200,
                    _live_page(agent_id, native_provider=native_provider),
                )
                return

            agent_id = _agent_id_from_path(
                parsed.path,
                prefix="/api/workers/",
                suffix="/stream",
            )
            if agent_id is not None:
                after = _safe_int(
                    query.get("after", [headers.get("last-event-id", "0")])[0],
                    default=0,
                )
                await self._send_sse(writer, agent_id, after)
                return

            await self._send_json(writer, 404, {"error": "not found"})
        except (asyncio.TimeoutError, ValueError):
            if not writer.is_closing():
                await self._send_json(writer, 400, {"error": "bad request"})
        except JsonRpcError as exc:
            if not writer.is_closing():
                await self._send_json(
                    writer,
                    409,
                    {"error": exc.rpc_message or str(exc), "code": exc.code},
                )
        except Exception as exc:
            if not writer.is_closing():
                await self._send_json(writer, 500, {"error": type(exc).__name__})
        finally:
            if task is not None:
                self._client_tasks.discard(task)

    def _sync_native_transcript(self, native_thread_id: str) -> str:
        if not native_thread_id or self._native_transcript_mirror is None:
            return ""
        try:
            self._native_transcript_mirror.sync_thread(native_thread_id)
        except Exception as exc:
            return str(exc) or type(exc).__name__
        return ""

    async def _read_json_body(
        self,
        reader: asyncio.StreamReader,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if _uses_chunked_transfer(headers):
            raw = await _read_chunked_body(reader)
            if not raw:
                return {}
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed
        content_length = _safe_int(headers.get("content-length", "0"), default=0)
        if content_length == 0:
            return {}
        if content_length > _MAX_BODY_BYTES:
            raise RequestBodyTooLarge("request body too large")
        raw = await asyncio.wait_for(
            reader.readexactly(content_length),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    async def _handle_native_agent_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        provider_name, provider_suffix = _native_provider_route_parts(path)
        provider = self._native_provider(provider_name)
        if provider is None:
            if provider_name == "codex" and self._native_registry is None:
                if not self._is_authorized(writer, headers, query, require_token=False):
                    await self._send_json(writer, 401, {"error": "unauthorized"})
                    return
                await self._send_json(
                    writer,
                    503,
                    {"error": "native controller unavailable"},
                )
                return
            await self._send_json(writer, 404, {"error": "unknown native provider"})
            return
        legacy_codex_controller = (
            provider_name == "codex"
            and self._native_registry is None
            and self._native_controller is not None
        )
        if not self._is_authorized(
            writer,
            headers,
            query,
            require_token=provider is not None,
        ):
            await self._send_json(writer, 401, {"error": "unauthorized"})
            return

        target = self._native_controller if legacy_codex_controller else provider
        route = f"/{provider_suffix}" if provider_suffix else ""
        if route == "/login-ticket":
            if not self._access_token:
                await self._send_json(writer, 404, {"error": "not found"})
                return
            if method != "POST":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            ticket = self._mint_login_ticket()
            await self._send_json(
                writer,
                200,
                {
                    "ticket": ticket,
                    "path": (
                        f"/native/{quote(provider_name, safe='')}/login?"
                        f"ticket={quote(ticket, safe='')}"
                    ),
                    "expires_in": _LOGIN_TICKET_TTL_SECONDS,
                },
            )
            return

        if route == "/status":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            status = await target.status()
            await self._send_json(writer, 200, _json_object(status))
            return

        if route == "/capabilities":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            await self._send_json(writer, 200, provider.capabilities().to_json_dict())
            return

        if route == "/sessions":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            if legacy_codex_controller:
                sessions = await target.list_sessions()
            else:
                sessions = await target.list_sessions(50)
            await self._send_json(
                writer,
                200,
                {"sessions": [_json_object(session) for session in sessions]},
            )
            return

        if route == "/models":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            models = await target.list_models()
            await self._send_json(
                writer,
                200,
                {"models": [_json_object(model) for model in models]},
            )
            return

        if route == "/sessions/start":
            if method != "POST":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            prompt = str(body.get("prompt", ""))
            model = _optional_nonempty_string(body.get("model"))
            service_tier = _optional_nonempty_string(
                body.get("service_tier") or body.get("serviceTier")
            )
            if prompt.strip():
                result = await target.start_session(
                    str(body.get("cwd", "")),
                    prompt,
                    model=model,
                    effort=_optional_nonempty_string(body.get("effort")),
                    service_tier=service_tier,
                    images=_safe_image_attachments(body.get("images")),
                )
            else:
                result = await target.create_session(
                    str(body.get("cwd", "")),
                    model=model,
                    service_tier=service_tier,
                )
            await self._send_json(writer, 200, _json_object(result))
            return

        approval_prefix = "/approvals/"
        if route.startswith(approval_prefix):
            parts = [
                unquote(part)
                for part in route[len(approval_prefix) :].split("/")
                if part
            ]
            if len(parts) == 2 and parts[1] == "resolve" and method == "POST":
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                try:
                    result = await target.resolve_approval(parts[0], body)
                except KeyError:
                    await self._send_json(
                        writer,
                        404,
                        {"error": "approval request not found"},
                    )
                    return
                except ValueError as exc:
                    await self._send_json(writer, 400, {"error": str(exc)})
                    return
                await self._send_json(writer, 200, _json_object(result))
                return
            await self._send_json(writer, 404, {"error": "not found"})
            return

        session_prefix = "/sessions/"
        if not route.startswith(session_prefix):
            await self._send_json(writer, 404, {"error": "not found"})
            return
        remainder = route[len(session_prefix) :]
        parts = [unquote(part) for part in remainder.split("/") if part]
        if not parts:
            await self._send_json(writer, 404, {"error": "not found"})
            return
        thread_id = parts[0]
        action = parts[1] if len(parts) > 1 else ""

        if method == "GET" and action == "" and len(parts) == 1:
            session = await target.read_session(thread_id)
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "attach" and len(parts) == 2:
            session = await target.attach_session(thread_id)
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "sync" and len(parts) == 2:
            session = await target.sync_session(thread_id)
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "continue" and len(parts) == 2:
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            result = await target.continue_session(
                thread_id,
                str(body.get("prompt", "")),
                model=_optional_nonempty_string(body.get("model")),
                effort=_optional_nonempty_string(body.get("effort")),
                service_tier=_optional_nonempty_string(
                    body.get("service_tier") or body.get("serviceTier")
                ),
                images=_safe_image_attachments(body.get("images")),
            )
            await self._send_json(writer, 200, _json_object(result))
            return
        if method == "POST" and action == "steer" and len(parts) == 2:
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            expected_turn_id = str(
                body.get("expected_turn_id") or body.get("turn_id") or ""
            )
            result = await target.steer_session(
                thread_id,
                expected_turn_id,
                str(body.get("prompt", "")),
                model=_optional_nonempty_string(body.get("model")),
                effort=_optional_nonempty_string(body.get("effort")),
                service_tier=_optional_nonempty_string(
                    body.get("service_tier") or body.get("serviceTier")
                ),
                images=_safe_image_attachments(body.get("images")),
            )
            await self._send_json(writer, 200, _json_object(result))
            return
        if method == "POST" and action == "interrupt" and len(parts) == 2:
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            result = await target.interrupt_session(
                thread_id,
                str(body.get("turn_id", "")),
            )
            await self._send_json(writer, 200, _json_object(result))
            return
        await self._send_json(writer, 404, {"error": "not found"})

    async def _handle_council_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        if not self._is_authorized(
            writer,
            headers,
            query,
            require_token=(
                self._native_registry is not None
                or self._native_controller is not None
            ),
        ):
            await self._send_json(writer, 401, {"error": "unauthorized"})
            return

        if path == "/api/council/config/default":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            await self._send_json(
                writer,
                200,
                await self._default_council_config_payload(),
            )
            return

        if path == "/api/council/projects":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            await self._send_json(writer, 200, _council_projects_payload())
            return

        if path == "/api/council/runs":
            if method != "POST":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            try:
                packet = _council_packet_from_body(body)
                config = await self._council_config_from_body(body)
                if bool(body.get("async")):
                    await self._send_json(
                        writer,
                        200,
                        self._start_async_council_run(
                            packet=packet,
                            config=config,
                            cwd=str(body.get("cwd") or ""),
                        ),
                    )
                    return
                reviewer = NativeProviderCouncilReviewer(
                    provider_resolver=_ServerNativeProviderResolver(self),
                    default_cwd=str(body.get("cwd") or ""),
                )
                board = await CouncilReviewService(reviewer=reviewer).review_packet(
                    packet=packet,
                    config=config,
                )
            except ValueError as exc:
                await self._send_json(writer, 400, {"error": str(exc)})
                return
            await self._send_json(writer, 200, board.to_json_dict())
            return

        run_id = _council_run_id_from_path(path)
        if run_id:
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            run = self._council_runs.get(run_id)
            if run is None:
                await self._send_json(writer, 404, {"error": "council run not found"})
                return
            await self._send_json(writer, 200, _council_run_public_payload(run))
            return

        await self._send_json(writer, 404, {"error": "not found"})

    def _start_async_council_run(
        self,
        *,
        packet: CouncilReviewPacket,
        config: CouncilConfig,
        cwd: str,
    ) -> dict[str, Any]:
        seats = build_council_seats(config)
        run_id = uuid4().hex
        now = time.time()
        run = {
            "run_id": run_id,
            "mode": "async",
            "status": "queued",
            "packet_fingerprint": packet.fingerprint,
            "round_index": 1,
            "cwd": cwd,
            "created_at": now,
            "updated_at": now,
            "seats": [seat.to_json_dict() for seat in seats],
            "results": [_council_pending_result_payload(seat) for seat in seats],
            "synthesis": CouncilSynthesis.from_results(()).to_json_dict(),
        }
        self._council_runs[run_id] = run
        task = asyncio.create_task(
            self._run_async_council_review(
                run_id=run_id,
                packet=packet,
                seats=seats,
                cwd=cwd,
            )
        )
        self._council_run_tasks.add(task)
        task.add_done_callback(self._council_run_tasks.discard)
        return _council_run_public_payload(run)

    async def _run_async_council_review(
        self,
        *,
        run_id: str,
        packet: CouncilReviewPacket,
        seats: tuple[CouncilSeat, ...],
        cwd: str,
    ) -> None:
        run = self._council_runs.get(run_id)
        if run is None:
            return
        run["status"] = "running"
        _touch_council_run(run)
        reviewer = NativeProviderCouncilReviewer(
            provider_resolver=_ServerNativeProviderResolver(self),
            default_cwd=cwd,
        )
        tasks = [
            self._run_async_council_seat(
                run_id=run_id,
                index=index,
                packet=packet,
                seat=seat,
                reviewer=reviewer,
            )
            for index, seat in enumerate(seats)
        ]
        results = tuple(await asyncio.gather(*tasks))
        run = self._council_runs.get(run_id)
        if run is None:
            return
        run["synthesis"] = CouncilSynthesis.from_results(results).to_json_dict()
        run["status"] = _council_run_status(results)
        _touch_council_run(run)

    async def _run_async_council_seat(
        self,
        *,
        run_id: str,
        index: int,
        packet: CouncilReviewPacket,
        seat: CouncilSeat,
        reviewer: NativeProviderCouncilReviewer,
    ) -> CouncilReviewResult:
        run = self._council_runs.get(run_id)
        if run is not None:
            run["results"][index] = _council_pending_result_payload(
                seat,
                status="running",
                summary="席位审核会话启动中...",
            )
            _touch_council_run(run)
        try:
            result = await reviewer.review(
                CouncilReviewRequest(packet=packet, seat=seat, round_index=1)
            )
        except Exception as exc:
            result = CouncilReviewResult.failed(seat, str(exc))
        run = self._council_runs.get(run_id)
        if run is not None:
            run["results"][index] = _council_result_payload(result)
            _touch_council_run(run)
        return result

    async def _default_council_config_payload(self) -> dict[str, Any]:
        providers = self._council_provider_summaries()
        models = await self._council_provider_models(providers)
        provider_name = str(providers[0]["provider"]) if providers else "codex"
        model_name = _first_model_id(models.get(provider_name, ())) or "default"
        config = default_council_config(provider=provider_name, model=model_name)
        payload = config.to_json_dict()
        payload["providers"] = providers
        payload["models"] = models
        payload["diversity"] = council_assignment_diversity(
            config.assignments
        ).to_json_dict()
        return payload

    async def _council_config_from_body(self, body: dict[str, Any]) -> CouncilConfig:
        raw_config = body.get("config")
        if not isinstance(raw_config, dict):
            default_payload = await self._default_council_config_payload()
            raw_config = default_payload
        assignments = _council_assignments_from_json(raw_config.get("assignments"))
        return CouncilConfig(
            seat_definitions=default_council_seat_definitions(),
            assignments=assignments,
            required_seat_ids=_tuple_from_json(raw_config.get("required_seat_ids")),
            mode=str(raw_config.get("mode") or "council"),
            enabled=bool(raw_config.get("enabled", True)),
        )

    def _council_provider_summaries(self) -> list[dict[str, str]]:
        if self._native_registry is not None:
            return [
                {
                    "provider": str(summary.get("provider") or ""),
                    "provider_engine": str(summary.get("provider_engine") or ""),
                }
                for summary in self._native_registry.list_provider_summaries()
            ]
        if self._native_controller is not None:
            return [{"provider": "codex", "provider_engine": "app-server"}]
        return []

    async def _council_provider_models(
        self,
        providers: list[dict[str, str]],
    ) -> dict[str, list[dict[str, Any]]]:
        models: dict[str, list[dict[str, Any]]] = {}
        for summary in providers:
            provider_name = str(summary.get("provider") or "")
            provider = self._native_provider(provider_name)
            list_models = getattr(provider, "list_models", None)
            if not callable(list_models):
                models[provider_name] = []
                continue
            try:
                raw_models = await list_models()
            except Exception:
                models[provider_name] = []
                continue
            models[provider_name] = [_json_object(model) for model in raw_models]
        return models

    async def _handle_native_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        await self._handle_native_agent_route(
            reader,
            writer,
            method,
            path,
            headers,
            query,
        )

    def _native_provider(self, provider_name: str) -> Any | None:
        if not provider_name:
            return None
        if self._native_registry is not None:
            provider = self._native_registry.maybe_get(provider_name)
            if provider is not None:
                return provider
        if provider_name == "codex" and self._native_controller is not None:
            from wlcodex.native_agents.codex_provider import CodexAppServerProvider

            return CodexAppServerProvider(self._native_controller)
        return None

    async def _send_native_provider_index(
        self,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        if not self._is_authorized(
            writer,
            headers,
            query,
            require_token=(
                self._native_registry is not None
                or self._native_controller is not None
            ),
        ):
            await self._send_html(writer, 401, _native_token_entry_page("/native"))
            return
        if self._native_registry is not None:
            providers = self._native_registry.list_provider_summaries()
        elif self._native_controller is not None:
            providers = [{"provider": "codex", "provider_engine": "app-server"}]
        else:
            providers = []
        query_token = str((query.get("token") or [""])[0] or "")
        await self._send_html(
            writer,
            200,
            _native_provider_index_html(providers, access_token=query_token),
        )

    async def _send_native_page(
        self,
        writer: asyncio.StreamWriter,
        provider_name: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        provider = self._native_provider(provider_name)
        if provider is None:
            await self._send_json(writer, 404, {"error": "unknown native provider"})
            return
        if not self._is_authorized(
            writer,
            headers,
            query,
            require_token=True,
        ):
            safe_provider = quote(provider_name, safe="")
            await self._send_html(
                writer,
                401,
                _native_token_entry_page(f"/native/{safe_provider}"),
            )
            return
        await self._send_html(writer, 200, _native_codex_page(provider_name))

    async def _read_request_json(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        try:
            return await self._read_json_body(reader, headers)
        except RequestBodyTooLarge:
            await self._send_json(writer, 413, {"error": "request body too large"})
            return None
        except (
            ValueError,
            json.JSONDecodeError,
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
        ):
            await self._send_json(writer, 400, {"error": "invalid json body"})
            return None

    def _is_authorized(
        self,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        query: dict[str, list[str]],
        *,
        require_token: bool = False,
    ) -> bool:
        if self._allow_unauthenticated_loopback and _is_loopback_peer(writer):
            return True
        if require_token and not self._access_token:
            return False
        if self._access_token:
            prefix = "Bearer "
            authorization = headers.get("authorization", "")
            if authorization.startswith(prefix):
                candidate = authorization[len(prefix) :]
                if hmac.compare_digest(candidate, self._access_token):
                    return True
            cookie_token = _cookie_value(headers.get("cookie", ""), "wlcodex_token")
            if cookie_token and hmac.compare_digest(cookie_token, self._access_token):
                return True
            query_token = query.get("token", [""])[0]
            return hmac.compare_digest(query_token, self._access_token)
        return False

    def _mint_login_ticket(self) -> str:
        self._purge_login_tickets()
        ticket = secrets.token_urlsafe(32)
        self._login_tickets[ticket] = time.monotonic() + _LOGIN_TICKET_TTL_SECONDS
        return ticket

    def _consume_login_ticket(self, ticket: str) -> bool:
        self._purge_login_tickets()
        expires_at = self._login_tickets.pop(ticket, None)
        return expires_at is not None and expires_at >= time.monotonic()

    def _has_login_ticket(self, ticket: str) -> bool:
        self._purge_login_tickets()
        expires_at = self._login_tickets.get(ticket)
        return expires_at is not None and expires_at >= time.monotonic()

    def _purge_login_tickets(self) -> None:
        now = time.monotonic()
        for ticket, expires_at in list(self._login_tickets.items()):
            if expires_at < now:
                self._login_tickets.pop(ticket, None)

    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await _send_response(writer, status, "application/json; charset=utf-8", body)

    async def _send_html(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: str,
    ) -> None:
        await _send_response(
            writer,
            status,
            "text/html; charset=utf-8",
            body.encode("utf-8"),
        )

    async def _send_redirect(
        self,
        writer: asyncio.StreamWriter,
        location: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        await _send_response(
            writer,
            303,
            "text/plain; charset=utf-8",
            b"",
            extra_headers={"Location": location, **(headers or {})},
        )

    async def _send_sse(
        self,
        writer: asyncio.StreamWriter,
        agent_run_id: int,
        after_id: int,
    ) -> None:
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "X-Accel-Buffering: no\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(header.encode("utf-8"))
        writer.write(b": connected\n\n")
        await writer.drain()
        latest = after_id
        for event in self._hub.snapshot(
            agent_run_id=agent_run_id,
            after_id=after_id,
            limit=500,
        ):
            latest = event.id
            await _write_sse(writer, event)
        queue = self._hub.subscribe(agent_run_id=agent_run_id)
        try:
            while not writer.is_closing():
                event = await queue.get()
                if event.id <= latest:
                    continue
                latest = event.id
                await _write_sse(writer, event)
        finally:
            self._hub.unsubscribe(agent_run_id=agent_run_id, queue=queue)


async def _send_response(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: str,
    body: bytes,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    reason = {
        200: "OK",
        303: "See Other",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        431: "Request Header Fields Too Large",
        503: "Service Unavailable",
    }.get(status, "Error")
    custom_headers = "".join(
        f"{name}: {value}\r\n" for name, value in (extra_headers or {}).items()
    )
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{custom_headers}"
        "Connection: close\r\n"
        "\r\n"
    )
    writer.write(header.encode("utf-8") + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _write_sse(writer: asyncio.StreamWriter, event: WorkerStreamEvent) -> None:
    writer.write(format_sse_event(event))
    await writer.drain()


def _uses_chunked_transfer(headers: dict[str, str]) -> bool:
    transfer_encoding = headers.get("transfer-encoding", "")
    return any(
        part.strip().lower() == "chunked"
        for part in transfer_encoding.split(",")
    )


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        size_line = await asyncio.wait_for(
            reader.readline(),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if not size_line:
            raise asyncio.IncompleteReadError(size_line, None)
        raw_size = size_line.split(b";", 1)[0].strip()
        try:
            chunk_size = int(raw_size, 16)
        except ValueError as exc:
            raise ValueError("invalid chunk size") from exc
        if chunk_size < 0:
            raise ValueError("invalid chunk size")
        if chunk_size == 0:
            await _discard_chunked_trailers(reader)
            break
        total += chunk_size
        if total > _MAX_BODY_BYTES:
            raise RequestBodyTooLarge("request body too large")
        chunk = await asyncio.wait_for(
            reader.readexactly(chunk_size),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        line_end = await asyncio.wait_for(
            reader.readexactly(2),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if line_end != b"\r\n":
            raise ValueError("invalid chunk delimiter")
        chunks.append(chunk)
    return b"".join(chunks)


async def _discard_chunked_trailers(reader: asyncio.StreamReader) -> None:
    while True:
        line = await asyncio.wait_for(
            reader.readline(),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if line in (b"\r\n", b"\n", b""):
            return


def format_sse_event(event: WorkerStreamEvent) -> bytes:
    payload = json.dumps(event.to_json_dict(), ensure_ascii=False)
    return f"id: {event.id}\nevent: {event.kind}\ndata: {payload}\n\n".encode(
        "utf-8"
    )


def _agent_id_from_path(path: str, *, prefix: str, suffix: str) -> int | None:
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    raw = path[len(prefix) : -len(suffix)]
    if not raw.isdigit():
        return None
    return int(raw)


def _native_provider_route_parts(path: str) -> tuple[str, str]:
    prefix = "/api/native/"
    if not path.startswith(prefix):
        return "", ""
    provider, _, suffix = path[len(prefix) :].partition("/")
    return unquote(provider), suffix


def _native_login_provider_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 3 and parts[0] == "native" and parts[2] == "login":
        return unquote(parts[1])
    return ""


def _native_page_provider_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] == "native":
        return unquote(parts[1])
    return ""


def _safe_int(raw: str, *, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _optional_nonempty_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _filter_events_for_native_turn(
    events: list[WorkerStreamEvent],
    *,
    native_turn_id: str,
) -> list[WorkerStreamEvent]:
    return [
        event
        for event in events
        if str(event.payload.get("native_turn_id") or "") == native_turn_id
    ]


def _filter_tail_to_native_turn(
    events: list[WorkerStreamEvent],
    *,
    native_turn_id: str,
) -> tuple[list[WorkerStreamEvent], int]:
    visible = _filter_events_for_native_turn(events, native_turn_id=native_turn_id)
    if not visible:
        return [], len(events)
    first_visible_id = visible[0].id
    extra_previous = sum(1 for event in events if event.id < first_visible_id)
    return visible, extra_previous


def _safe_image_attachments(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    images: list[dict[str, Any]] = []
    for raw in value[:_MAX_NATIVE_IMAGE_ATTACHMENTS]:
        if not isinstance(raw, dict):
            continue
        clean: dict[str, Any] = {}
        url = raw.get("url") or raw.get("data_url")
        if isinstance(url, str) and url.startswith("data:image/") and "," in url:
            clean["url"] = url
        filename = raw.get("filename")
        if isinstance(filename, str) and filename.strip():
            clean["filename"] = filename.strip()[:160]
        mime_type = raw.get("mime_type") or raw.get("mimeType")
        if isinstance(mime_type, str) and mime_type.startswith("image/"):
            clean["mime_type"] = mime_type[:80]
        if "url" in clean:
            images.append(clean)
    return images or None


def _is_loopback_peer(writer: asyncio.StreamWriter) -> bool:
    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer:
        return False
    return peer[0] in ("127.0.0.1", "::1", "localhost")


def _cookie_value(raw: str, name: str) -> str:
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return unquote(value)
    return ""


def _login_cookie_header(token: str) -> str:
    return (
        "wlcodex_token="
        + quote(token, safe="")
        + f"; Path=/; Max-Age={_LOGIN_COOKIE_MAX_AGE_SECONDS}; SameSite=Lax"
    )


def _json_object(value: Any) -> dict[str, Any]:
    to_json_dict = getattr(value, "to_json_dict", None)
    if callable(to_json_dict):
        result = to_json_dict()
        if isinstance(result, dict):
            return result
    if isinstance(value, dict):
        return value
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return dict(raw)
    return {"value": value}


class _ServerNativeProviderResolver:
    def __init__(self, server: WorkerLiveStreamServer) -> None:
        self._server = server

    def get(self, provider: str) -> Any:
        target = self._server._native_provider(provider)
        if target is None:
            raise KeyError(f"unknown native provider: {provider}")
        return target


def _council_packet_from_body(body: dict[str, Any]) -> CouncilReviewPacket:
    return CouncilReviewPacket(
        title=str(body.get("title") or ""),
        proposal=str(body.get("proposal") or ""),
        context=str(body.get("context") or ""),
        success_criteria=_tuple_from_json(body.get("success_criteria")),
        constraints=_tuple_from_json(body.get("constraints")),
        metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
    )


def _council_assignments_from_json(value: Any) -> tuple[CouncilSeatAssignment, ...]:
    if not isinstance(value, list):
        return ()
    assignments: list[CouncilSeatAssignment] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        assignments.append(
            CouncilSeatAssignment(
                seat_id=str(raw.get("seat_id") or ""),
                provider=str(raw.get("provider") or ""),
                model=str(raw.get("model") or ""),
                profile=str(raw.get("profile") or ""),
                enabled=bool(raw.get("enabled", True)),
                metadata=raw.get("metadata")
                if isinstance(raw.get("metadata"), dict)
                else {},
            )
        )
    return tuple(assignments)


def _council_run_id_from_path(path: str) -> str:
    prefix = "/api/council/runs/"
    if not path.startswith(prefix):
        return ""
    remainder = path[len(prefix) :].strip("/")
    if not remainder or "/" in remainder:
        return ""
    return unquote(remainder)


def _council_pending_result_payload(
    seat: CouncilSeat,
    *,
    status: str = "queued",
    summary: str = "等待席位审核启动。",
) -> dict[str, Any]:
    return {
        "seat_id": seat.seat_id,
        "provider": seat.provider,
        "model": seat.model,
        "role": seat.role,
        "verdict": "",
        "confidence": 0.0,
        "summary": summary,
        "risks": [],
        "required_changes": [],
        "open_questions": [],
        "raw_output": "",
        "status": status,
        "error": "",
        "native_session_id": "",
        "provider_engine": "",
        "native_session_path": "",
    }


def _council_result_payload(result: CouncilReviewResult) -> dict[str, Any]:
    payload = result.to_json_dict()
    payload["native_session_path"] = _native_session_path(
        result.provider,
        result.native_session_id,
    )
    return payload


def _native_session_path(provider: str, native_session_id: str) -> str:
    if not provider or not native_session_id:
        return ""
    return (
        f"/native/{quote(provider, safe='')}"
        f"?native_thread_id={quote(native_session_id, safe='')}"
    )


def _council_run_status(results: tuple[CouncilReviewResult, ...]) -> str:
    completed = sum(1 for result in results if result.status == "completed")
    failed = sum(1 for result in results if result.status == "failed")
    if completed == len(results):
        return "completed"
    if failed == len(results):
        return "failed"
    return "partial"


def _touch_council_run(run: dict[str, Any]) -> None:
    run["updated_at"] = time.time()


def _council_run_public_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id", ""),
        "mode": run.get("mode", "async"),
        "status": run.get("status", "queued"),
        "packet_fingerprint": run.get("packet_fingerprint", ""),
        "round_index": run.get("round_index", 1),
        "cwd": run.get("cwd", ""),
        "created_at": run.get("created_at", 0.0),
        "updated_at": run.get("updated_at", 0.0),
        "seats": [dict(seat) for seat in run.get("seats", [])],
        "results": [dict(result) for result in run.get("results", [])],
        "synthesis": dict(run.get("synthesis", {})),
    }


def _tuple_from_json(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _first_model_id(models: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    for model in models:
        for key in ("id", "name", "model"):
            value = str(model.get(key) or "").strip()
            if value:
                return value
    return ""


def _council_projects_payload(
    projects_root: Path | None = None,
) -> dict[str, Any]:
    projects_root = projects_root or _COUNCIL_PROJECTS_ROOT
    projects: list[dict[str, str]] = []
    try:
        entries = sorted(
            projects_root.iterdir(),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        entries = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        projects.append({"name": entry.name, "cwd": str(entry)})
    return {"root": str(projects_root), "projects": projects}


def _native_provider_display_name(provider: str) -> str:
    names = {
        "codex": "Codex",
        "claude": "Claude",
        "antigravity": "Antigravity",
    }
    provider_name = str(provider or "").strip()
    return names.get(provider_name, provider_name.replace("-", " ").title() or "Native")


def _native_provider_index_html(
    providers: list[dict[str, str]],
    *,
    access_token: str = "",
) -> str:
    token_suffix = (
        f"?token={quote(str(access_token), safe='')}" if str(access_token or "") else ""
    )
    council_links = """
      <a class="provider council" href="/council__TOKEN_SUFFIX__">
        <span>议会审核</span>
        <small>提交方案并运行五席审核</small>
      </a>
    """.replace("__TOKEN_SUFFIX__", token_suffix)
    if providers:
        links = "\n".join(
            (
                f'<a class="provider" href="/native/'
                f'{quote(str(provider["provider"]), safe="")}{token_suffix}">'
                f'<span>{escape(_native_provider_display_name(str(provider["provider"])))}</span>'
                f'<small>{escape(str(provider.get("provider_engine", "")))}</small>'
                "</a>"
            )
            for provider in providers
        )
    else:
        links = '<div class="empty">No native providers configured.</div>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Native Agents</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; padding: 28px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #000; color: #f7f7f8; }}
    main {{ display: grid; gap: 14px; max-width: 560px; margin: 0 auto; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    .provider {{ display: grid; gap: 4px; min-height: 64px; align-content: center; padding: 12px 0; border-bottom: 1px solid #24262d; color: inherit; text-decoration: none; }}
    .provider.council {{ border-bottom-color: #334155; }}
    .provider span {{ font-size: 20px; font-weight: 760; }}
    .provider small, .empty {{ color: #9ca3af; }}
  </style>
</head>
<body>
  <main>
    <h1>Native Agents</h1>
    {council_links}
    {links}
  </main>
</body>
</html>"""


def _council_review_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>议会审核</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #050506; color: #f7f7f8; }
    header { position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 52px 1fr auto; gap: 12px; align-items: center; min-height: 72px; padding: 10px 18px; background: rgba(5,5,6,.96); border-bottom: 1px solid #26282f; }
    .circle { display: grid; place-items: center; width: 46px; height: 46px; border-radius: 50%; border: 1px solid #34363d; background: #202126; color: #fff; text-decoration: none; font-size: 28px; line-height: 1; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .config-link { min-height: 42px; padding: 0 14px; border-radius: 21px; border: 1px solid #34363d; color: #f7f7f8; display: inline-grid; place-items: center; text-decoration: none; font-weight: 720; }
    main { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 420px); gap: 18px; width: min(1180px, 100%); margin: 0 auto; padding: 18px; }
    section { min-width: 0; }
    .panel { border: 1px solid #2f3138; background: #111217; border-radius: 8px; padding: 14px; }
    .stack { display: grid; gap: 12px; }
    label { display: grid; gap: 6px; color: #d4d7de; font-size: 14px; font-weight: 680; }
    input, textarea, select { width: 100%; min-width: 0; border: 1px solid #383b43; border-radius: 8px; background: #1b1d24; color: #f7f7f8; font: inherit; padding: 11px 12px; }
    textarea { min-height: 124px; resize: vertical; line-height: 1.48; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .run { min-height: 48px; border: 0; border-radius: 8px; background: #f7f7f8; color: #050506; font-weight: 800; font-size: 16px; }
    .run:disabled { opacity: .55; cursor: progress; }
    .muted { color: #9ca3af; font-size: 13px; line-height: 1.45; }
    .seat-list, .results { display: grid; gap: 10px; }
    .seat, .result { border: 1px solid #2b2d34; border-radius: 8px; padding: 12px; background: #0d0e12; }
    .seat-head, .result-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .seat-title, .result-title { font-weight: 800; }
    .badge { border-radius: 999px; padding: 4px 8px; background: #22252e; color: #cfd3dc; font-size: 12px; white-space: nowrap; }
    .summary { margin-top: 8px; color: #d9dde6; line-height: 1.5; white-space: pre-wrap; }
    .session-link { display: inline-grid; place-items: center; min-height: 32px; margin-top: 10px; padding: 0 10px; border: 1px solid #3b3f49; border-radius: 8px; color: #f7f7f8; text-decoration: none; font-size: 13px; font-weight: 720; }
    .error { color: #fecaca; }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; padding-bottom: 96px; }
      header { grid-template-columns: 46px 1fr; }
      .config-link { grid-column: 1 / -1; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <a class="circle" href="/native" aria-label="back">‹</a>
    <h1>议会审核</h1>
    <a class="config-link" href="/council/seats">席位配置</a>
  </header>
  <main>
    <section class="panel stack">
      <div>
        <strong>Review Packet</strong>
        <div class="muted">把同一份方案锁定后交给五个席位审核。</div>
      </div>
      <label>标题<input id="title" value="方案审核"></label>
      <label>方案<textarea id="proposal" placeholder="粘贴要审核的方案、需求或实现摘要"></textarea></label>
      <label>上下文<textarea id="context" placeholder="可选：相关背景、约束、当前分支、风险"></textarea></label>
      <div class="row">
        <label>成功标准<textarea id="success" placeholder="每行一条"></textarea></label>
        <label>约束<textarea id="constraints" placeholder="每行一条"></textarea></label>
      </div>
      <label>工作目录<select id="cwd"><option value="">正在读取项目...</option></select></label>
      <button class="run" id="run" disabled>启动议会审核</button>
      <div class="muted" id="status">正在读取席位配置...</div>
    </section>
    <aside class="stack">
      <section class="panel stack">
        <div>
          <strong>当前席位</strong>
          <div class="muted" id="diversity">模型多样性等待计算</div>
        </div>
        <div class="seat-list" id="seats"></div>
      </section>
      <section class="panel stack">
        <div>
          <strong>审核结果</strong>
          <div class="muted">每个席位会独立显示状态，启动后可打开原生会话。</div>
        </div>
        <div class="results" id="results"></div>
      </section>
    </aside>
  </main>
  <script>
    const DEFAULT_CONFIG_URL = "/api/council/config/default";
    const PROJECTS_URL = "/api/council/projects";
    const RUN_URL = "/api/council/runs";
    const POLL_INTERVAL_MS = 1200;
    const STORAGE_KEY = "wlcodexCouncilConfig";
    let config = null;
    let projects = [];
    let pollTimer = null;
    let activeRunId = "";

    const $ = (id) => document.getElementById(id);
    const lines = (text) => text.split("\\n").map((item) => item.trim()).filter(Boolean);
    const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[ch]));

    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {"Content-Type": "application/json", ...(options.headers || {})},
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function savedConfig(defaultConfig) {
      try {
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        if (saved && Array.isArray(saved.assignments)) return {...defaultConfig, ...saved};
      } catch (_error) {}
      return defaultConfig;
    }

    function renderSeats() {
      const assignments = (config.assignments || []).filter((seat) => seat.enabled !== false);
      const definitions = Object.fromEntries((config.seat_definitions || []).map((seat) => [seat.seat_id, seat]));
      $("seats").innerHTML = assignments.map((assignment) => {
        const definition = definitions[assignment.seat_id] || {};
        return `<div class="seat"><div class="seat-head"><span class="seat-title">${esc(definition.role || assignment.seat_id)}</span><span class="badge">${esc(assignment.provider)} · ${esc(assignment.model)}</span></div><div class="summary">${esc(definition.mission || "")}</div></div>`;
      }).join("");
      const unique = new Set(assignments.map((item) => `${item.provider}:${item.model}`)).size;
      $("diversity").textContent = `模型多样性 ${unique}/${assignments.length || 0}`;
    }

    function renderProjects(payload) {
      projects = Array.isArray(payload.projects) ? payload.projects : [];
      if (!projects.length) {
        $("cwd").innerHTML = `<option value="">未找到 ${esc(payload.root || "/Users/wl/projects")} 下的项目</option>`;
        return;
      }
      const preferred = projects.find((project) => project.name === "wlcodex") || projects[0];
      $("cwd").innerHTML = projects.map((project) => `<option value="${esc(project.cwd)}">${esc(project.name)}</option>`).join("");
      $("cwd").value = preferred.cwd;
    }

    function boardStatusLabel(status) {
      return ({
        queued: "等待席位启动",
        running: "席位审核中",
        partial: "部分席位已启动，等待输出",
        completed: "审核完成",
        failed: "审核失败",
      })[String(status || "")] || "等待席位输出";
    }

    function seatStatusLabel(status) {
      return ({
        queued: "等待启动",
        running: "启动中",
        started: "已启动，等待输出",
        completed: "已完成",
        failed: "失败",
      })[String(status || "")] || "等待";
    }

    function consensusLabel(consensus) {
      return ({
        no_completed_reviews: "等待席位输出",
        approved: "通过",
        approved_with_changes: "带修改通过",
        rejected: "未通过",
        mixed: "意见不一致",
      })[String(consensus || "")] || "等待汇总";
    }

    function isBoardActive(board) {
      return ["queued", "running"].includes(String((board && board.status) || ""));
    }

    function setRunBusy(isBusy) {
      $("run").disabled = isBusy;
      $("run").textContent = isBusy ? "议会审核中..." : "启动议会审核";
    }

    function setBoardStatus(board) {
      $("status").textContent = board ? boardStatusLabel(board.status) : "席位配置已就绪。";
    }

    function renderBoard(board) {
      const synthesis = board.synthesis || {};
      const results = board.results || [];
      const chairLabel = synthesis.consensus ? consensusLabel(synthesis.consensus) : boardStatusLabel(board.status);
      $("results").innerHTML = [
        `<div class="result"><div class="result-head"><span class="result-title">Chair Synthesis</span><span class="badge">${esc(chairLabel)}</span></div><div class="summary">${esc((synthesis.required_changes || []).join("\\n") || "等待席位输出同步。")}</div></div>`,
        ...results.map((result) => {
          const sessionLink = result.native_session_path ? `<a class="session-link" href="${esc(result.native_session_path)}">打开原生会话</a>` : "";
          const verdict = result.verdict ? ` · ${esc(result.verdict)}` : "";
          return `<div class="result"><div class="result-head"><span class="result-title">${esc(result.seat_id)}</span><span class="badge">${esc(seatStatusLabel(result.status))}${verdict}</span></div><div class="summary">${esc(result.summary || result.error || "")}</div>${sessionLink}</div>`;
        })
      ].join("");
    }

    function stopPolling(resetRun = false) {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      if (resetRun) {
        activeRunId = "";
        setRunBusy(false);
      }
    }

    function startPolling(runId) {
      stopPolling();
      if (!runId) return;
      activeRunId = runId;
      setRunBusy(true);
      pollTimer = setInterval(async () => {
        try {
          const board = await api(`${RUN_URL}/${encodeURIComponent(runId)}`);
          renderBoard(board);
          setBoardStatus(board);
          if (!isBoardActive(board)) stopPolling(true);
        } catch (error) {
          stopPolling(true);
          $("status").innerHTML = `<span class="error">${esc(error.message)}</span>`;
        }
      }, POLL_INTERVAL_MS);
    }

    async function loadConfig() {
      const [defaults, projectPayload] = await Promise.all([
        api(DEFAULT_CONFIG_URL),
        api(PROJECTS_URL),
      ]);
      config = savedConfig(defaults);
      renderProjects(projectPayload);
      renderSeats();
      setBoardStatus(null);
      setRunBusy(false);
    }

    $("run").onclick = async () => {
      if (activeRunId) {
        $("status").textContent = "当前议会还在审核中";
        return;
      }
      activeRunId = "starting";
      setRunBusy(true);
      $("status").textContent = "正在启动席位...";
      $("results").innerHTML = "";
      try {
        const board = await api(RUN_URL, {
          method: "POST",
          body: JSON.stringify({
            async: true,
            title: $("title").value,
            proposal: $("proposal").value,
            context: $("context").value,
            success_criteria: lines($("success").value),
            constraints: lines($("constraints").value),
            cwd: $("cwd").value,
            config,
          }),
        });
        renderBoard(board);
        setBoardStatus(board);
        if (isBoardActive(board)) {
          startPolling(board.run_id);
        } else {
          stopPolling(true);
        }
      } catch (error) {
        stopPolling(true);
        $("status").innerHTML = `<span class="error">${esc(error.message)}</span>`;
      }
    };
    loadConfig().catch((error) => {$("status").innerHTML = `<span class="error">${esc(error.message)}</span>`;});
  </script>
</body>
</html>"""


def _council_seats_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>议会席位配置</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #050506; color: #f7f7f8; }
    header { position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 52px 1fr auto; gap: 12px; align-items: center; min-height: 72px; padding: 10px 18px; background: rgba(5,5,6,.96); border-bottom: 1px solid #26282f; }
    .circle { display: grid; place-items: center; width: 46px; height: 46px; border-radius: 50%; border: 1px solid #34363d; background: #202126; color: #fff; text-decoration: none; font-size: 28px; line-height: 1; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .review-link, button.save { min-height: 42px; padding: 0 14px; border-radius: 21px; border: 1px solid #34363d; color: #f7f7f8; background: #202126; display: inline-grid; place-items: center; text-decoration: none; font-weight: 720; }
    main { display: grid; gap: 14px; width: min(980px, 100%); margin: 0 auto; padding: 18px; }
    .toolbar { display: flex; justify-content: space-between; gap: 12px; align-items: center; border: 1px solid #2f3138; background: #111217; border-radius: 8px; padding: 14px; }
    .muted { color: #9ca3af; font-size: 13px; line-height: 1.45; }
    .seat-grid { display: grid; gap: 12px; }
    .seat { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(140px, .7fr) minmax(160px, .8fr) auto; gap: 10px; align-items: center; border: 1px solid #2b2d34; border-radius: 8px; padding: 12px; background: #0d0e12; }
    .seat-title { display: grid; gap: 5px; min-width: 0; }
    .role { font-weight: 820; font-size: 17px; }
    .mission { color: #aeb4bf; font-size: 13px; line-height: 1.45; }
    label { display: grid; gap: 5px; color: #d4d7de; font-size: 12px; font-weight: 680; }
    input, select { width: 100%; min-width: 0; border: 1px solid #383b43; border-radius: 8px; background: #1b1d24; color: #f7f7f8; font: inherit; padding: 10px 11px; }
    .switch { width: 54px; height: 32px; }
    @media (max-width: 760px) {
      header { grid-template-columns: 46px 1fr; }
      .review-link { grid-column: 1 / -1; }
      .toolbar { display: grid; }
      .seat { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <a class="circle" href="/native" aria-label="back">‹</a>
    <h1>议会席位配置</h1>
    <a class="review-link" href="/council">议会审核</a>
  </header>
  <main>
    <section class="toolbar">
      <div>
        <strong>默认议会</strong>
        <div class="muted">固定五席：唱反调、第一性原理、扩展思路、局外人、执行者</div>
        <div class="muted" id="diversity">读取席位中...</div>
      </div>
      <button class="save" id="save">保存配置</button>
    </section>
    <section class="seat-grid" id="seats"></section>
  </main>
  <script>
    const DEFAULT_CONFIG_URL = "/api/council/config/default";
    const STORAGE_KEY = "wlcodexCouncilConfig";
    let config = null;
    let models = {};
    const $ = (id) => document.getElementById(id);
    const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[ch]));

    async function api(path) {
      const response = await fetch(path);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function savedConfig(defaultConfig) {
      try {
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        if (saved && Array.isArray(saved.assignments)) return {...defaultConfig, ...saved};
      } catch (_error) {}
      return defaultConfig;
    }

    function modelOptions(provider, current) {
      const available = models[provider] || [];
      const ids = available.map((item) => item.id || item.name || item.model).filter(Boolean);
      if (current && !ids.includes(current)) ids.unshift(current);
      return ids.map((id) => `<option value="${esc(id)}"${id === current ? " selected" : ""}>${esc(id)}</option>`).join("");
    }

    function render() {
      const providers = config.providers || [];
      const definitions = Object.fromEntries((config.seat_definitions || []).map((seat) => [seat.seat_id, seat]));
      $("seats").innerHTML = (config.assignments || []).map((assignment, index) => {
        const definition = definitions[assignment.seat_id] || {};
        const providerOptions = providers.map((provider) => {
          const id = provider.provider;
          return `<option value="${esc(id)}"${id === assignment.provider ? " selected" : ""}>${esc(id)}</option>`;
        }).join("");
        return `<div class="seat" data-index="${index}">
          <div class="seat-title"><span class="role">${esc(definition.role || assignment.seat_id)}</span><span class="mission">${esc(definition.mission || "")}</span></div>
          <label>Provider<select data-field="provider">${providerOptions}</select></label>
          <label>Model<select data-field="model">${modelOptions(assignment.provider, assignment.model)}</select></label>
          <label>启用<input class="switch" type="checkbox" data-field="enabled"${assignment.enabled !== false ? " checked" : ""}></label>
        </div>`;
      }).join("");
      updateDiversity();
    }

    function updateDiversity() {
      const enabled = (config.assignments || []).filter((seat) => seat.enabled !== false);
      const unique = new Set(enabled.map((seat) => `${seat.provider}:${seat.model}`)).size;
      $("diversity").textContent = `模型多样性 ${unique}/${enabled.length || 0}，允许同一模型担任多个席位。`;
    }

    $("seats").onchange = (event) => {
      const row = event.target.closest(".seat");
      if (!row) return;
      const assignment = config.assignments[Number(row.dataset.index)];
      const field = event.target.dataset.field;
      if (field === "enabled") assignment.enabled = event.target.checked;
      if (field === "provider") {
        assignment.provider = event.target.value;
        const first = (models[assignment.provider] || [])[0];
        assignment.model = first ? (first.id || first.name || first.model || assignment.model) : assignment.model;
        render();
        return;
      }
      if (field === "model") assignment.model = event.target.value;
      updateDiversity();
    };

    $("save").onclick = () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        mode: config.mode,
        enabled: config.enabled,
        assignments: config.assignments,
        required_seat_ids: config.required_seat_ids,
      }));
      $("diversity").textContent = "配置已保存。";
      setTimeout(updateDiversity, 900);
    };

    api(DEFAULT_CONFIG_URL).then((defaults) => {
      models = defaults.models || {};
      config = savedConfig(defaults);
      render();
    }).catch((error) => {
      $("diversity").textContent = error.message;
    });
  </script>
</body>
</html>"""


def _native_token_entry_page(return_to: str = "/native/codex") -> str:
    return_to_json = json.dumps(_safe_native_return_path(return_to))
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WLCodex</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 26px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #000; color: #f7f7f8; }
    main { width: min(420px, 100%); display: grid; gap: 18px; }
    h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    p { margin: 0; color: #aeb4bf; line-height: 1.5; }
    form { display: grid; gap: 12px; }
    input { width: 100%; height: 54px; border-radius: 14px; border: 1px solid #3a3a40; background: #14161d; color: #f7f7f8; padding: 0 14px; font-size: 16px; }
    button { height: 52px; border: 0; border-radius: 14px; background: #f4f4f5; color: #101114; font-size: 16px; font-weight: 760; }
    .status { min-height: 20px; color: #fca5a5; font-size: 14px; }
  </style>
</head>
<body>
  <main>
    <h1>Codex</h1>
    <p>输入访问令牌后进入手机远程控制页。令牌只保存在此浏览器本地。</p>
    <form id="tokenForm">
      <input id="tokenInput" name="token" placeholder="访问令牌" autocomplete="current-password" autofocus>
      <button type="submit">进入</button>
      <div class="status" id="status"></div>
    </form>
  </main>
  <script>
    function rememberToken(value) {
      try { localStorage.setItem("wlcodexToken", value); } catch (_error) {}
      document.cookie = "wlcodex_token=" + encodeURIComponent(value) + "; Path=/; Max-Age=2592000; SameSite=Lax";
    }
    const params = new URLSearchParams(location.search);
    const queryToken = params.get("token") || "";
    let savedToken = "";
    try { savedToken = localStorage.getItem("wlcodexToken") || ""; } catch (_error) {}
    const token = queryToken || savedToken;
    if (token) {
      rememberToken(token);
      location.replace(__RETURN_TO__);
    }
    const input = document.getElementById("tokenInput");
    const status = document.getElementById("status");
    document.getElementById("tokenForm").onsubmit = event => {
      event.preventDefault();
      const value = input.value.trim();
      if (!value) {
        status.textContent = "请输入访问令牌";
        input.focus();
        return;
      }
      rememberToken(value);
      location.href = __RETURN_TO__;
    };
  </script>
</body>
</html>""".replace("__RETURN_TO__", return_to_json)


def _safe_native_return_path(path: str) -> str:
    value = str(path or "").strip()
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/native/codex"
    if "\r" in value or "\n" in value:
        return "/native/codex"
    return value


def _native_login_ticket_page(ticket: str, provider_name: str = "codex") -> str:
    safe_ticket = escape(ticket, quote=True)
    display_name = escape(_native_provider_display_name(provider_name))
    safe_provider = quote(provider_name, safe="")
    safe_action = f"/native/{safe_provider}/login?ticket={quote(ticket, safe='')}"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex</title>
  <style>
    :root {{ color-scheme: dark; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 26px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #000; color: #f7f7f8; }}
    main {{ width: min(420px, 100%); display: grid; gap: 18px; }}
    h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
    p {{ margin: 0; color: #aeb4bf; line-height: 1.5; }}
    form {{ display: grid; gap: 12px; }}
    button {{ height: 52px; border: 0; border-radius: 14px; background: #f4f4f5; color: #101114; font-size: 16px; font-weight: 760; }}
  </style>
</head>
<body>
  <main>
    <h1>{display_name}</h1>
    <p>点击进入手机远程控制页。此链接只能使用一次。</p>
    <form method="post" action="{safe_action}">
      <input type="hidden" name="ticket" value="{safe_ticket}">
      <button type="submit">进入 {display_name}</button>
    </form>
  </main>
</body>
</html>"""


def _native_codex_page(provider_name: str = "codex") -> str:
    provider_name = provider_name.strip() or "codex"
    provider_label = _native_provider_display_name(provider_name)
    api_base = f"/api/native/{quote(provider_name, safe='')}"
    template = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__PROVIDER_LABEL__</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #000; color: #f7f7f8; }
    header { position: sticky; top: 0; z-index: 2; padding: 18px 20px 10px; background: #000; }
    .topbar { position: relative; display: grid; grid-template-columns: 54px 1fr 54px; align-items: center; min-height: 54px; }
    h1 { margin: 0; text-align: center; font-size: 22px; font-weight: 760; letter-spacing: 0; }
    .circle { width: 50px; height: 50px; border-radius: 50%; border: 1px solid #30333a; background: #1f2024; color: #fff; font-size: 30px; line-height: 1; }
    .menu { font-size: 24px; }
    .devices { display: flex; gap: 10px; overflow-x: auto; padding: 14px 0 4px; scrollbar-width: none; }
    .device-chip { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 9px; max-width: 82vw; min-height: 44px; border-radius: 24px; padding: 0 18px; border: 0; background: #fff; color: #111; font-size: 16px; font-weight: 760; }
    .device-chip.off { background: #2d2d31; color: #d8d8dc; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; }
    .off .dot { background: #8c8f98; }
    .laptop { width: 20px; height: 14px; border: 2px solid currentColor; border-radius: 2px; position: relative; display: inline-block; }
    .laptop:after { content: ""; position: absolute; left: -4px; right: -4px; bottom: -6px; height: 2px; background: currentColor; border-radius: 2px; }
    main { overflow-x: hidden; padding: 8px 22px calc(124px + env(safe-area-inset-bottom)); }
    .nav-row, .project, .recent { display: grid; grid-template-columns: 38px minmax(0, 1fr) auto; align-items: center; min-width: 0; min-height: 62px; color: #f7f7f8; background: transparent; border: 0; width: 100%; padding: 0; text-align: left; }
    .nav-row[hidden], .project-new-chat[hidden] { display: none; }
    .nav-row > span:nth-child(2), .project > span:nth-child(2) { min-width: 0; }
    .icon-folder, .icon-chat { width: 30px; height: 24px; border: 3px solid #f7f7f8; border-radius: 4px; position: relative; }
    .icon-folder:before { content: ""; position: absolute; left: 2px; top: -9px; width: 15px; height: 8px; border: 3px solid #f7f7f8; border-bottom: 0; border-radius: 4px 4px 0 0; background: #000; }
    .icon-chat { width: 28px; height: 28px; border-radius: 50%; }
    .icon-chat:after { content: ""; position: absolute; right: 2px; bottom: 1px; width: 7px; height: 7px; border-right: 3px solid #f7f7f8; border-bottom: 3px solid #f7f7f8; transform: rotate(28deg); background: #000; }
    .nav-row.active .label, .project.active .label { color: #fff; }
    .nav-row.active .icon-chat, .project.active .icon-folder { border-color: #fff; }
    .project.active .icon-folder:before { border-color: #fff; }
    .label { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 18px; font-weight: 650; }
    .section-title { margin: 26px 0 12px; color: #c8c8d0; font-size: 15px; }
    .recent { grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: start; min-height: 64px; padding: 6px 0; }
    .recent-copy { min-width: 0; overflow: hidden; }
    .recent-title { display: -webkit-box; max-height: 2.56em; white-space: normal; line-height: 1.28; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
    .recent.active .label { color: #fff; }
    .more-sessions { border: 0; border-top: 1px solid #24262d; margin-top: 8px; padding-top: 8px; }
    .more-sessions summary { min-height: 44px; list-style: none; cursor: pointer; color: #c8c8d0; font-size: 15px; }
    .more-sessions summary::-webkit-details-marker { display: none; }
    .more-sessions-body { display: grid; gap: 0; }
    .time { max-width: 66px; overflow: hidden; color: #a9a9b2; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
    .meta { margin-top: 3px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #8d93a0; font-size: 12px; }
    .empty { color: #8d93a0; padding: 16px 0; }
    .controls { position: fixed; left: 0; right: 0; bottom: 0; display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 12px 26px 18px; background: linear-gradient(to top, #000 82%, rgba(0,0,0,0)); }
    input { min-width: 0; height: 56px; border-radius: 28px; border: 1px solid #3a3a40; background: #1c1c20; color: #f7f7f8; padding: 0 20px; font-size: 17px; }
    button.chat { height: 56px; min-width: 118px; border-radius: 28px; border: 0; background: #fff; color: #000; font-size: 17px; font-weight: 760; }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <button class="circle" id="back" aria-label="back">‹</button>
      <h1>__PROVIDER_LABEL__</h1>
      <button class="circle menu" aria-label="menu">⋮</button>
    </div>
    <div class="devices" id="devices">
      <button class="device-chip off"><span class="dot"></span><span class="laptop"></span><span>connecting</span></button>
    </div>
  </header>
  <main>
    <button class="nav-row" id="chat">
      <span class="icon-chat"></span>
      <span class="label">聊天</span>
      <span></span>
    </button>
    <div id="projects"></div>
    <button class="nav-row project-new-chat" id="projectNewChat" hidden>
      <span class="icon-chat"></span>
      <span><span class="label">聊天</span><span class="meta" id="projectNewChatMeta"></span></span>
      <span></span>
    </button>
    <div class="section-title">最近</div>
    <div id="sessions"></div>
  </main>
  <section class="controls">
    <input id="prompt" placeholder="搜索聊天或开始新聊天">
    <button class="chat" id="send">聊天</button>
  </section>
  <script>
    const PROVIDER = __PROVIDER_JSON__;
    const PROVIDER_LABEL = __PROVIDER_LABEL_JSON__;
    const API_BASE = __API_BASE_JSON__;
    const PROJECTS_URL = "/api/council/projects";
    const token = new URLSearchParams(location.search).get("token") || "";
    if (token) {
      try { localStorage.setItem("wlcodexToken", token); } catch (_error) {}
      document.cookie = "wlcodex_token=" + encodeURIComponent(token) + "; Path=/; Max-Age=2592000; SameSite=Lax";
    }
    const headers = token ? {"Authorization": "Bearer " + token} : {};
    let selected = null;
    let selectedProjectCwd = "";
    let sessions = [];
    let projectRoot = "";
    let projectCatalog = [];
    const SESSION_PREVIEW_LIMIT = 10;
    const devicesEl = document.getElementById("devices");
    const sessionsEl = document.getElementById("sessions");
    const projectsEl = document.getElementById("projects");
    const promptEl = document.getElementById("prompt");
    const controlsEl = document.querySelector(".controls");
    const chatRow = document.getElementById("chat");
    const projectNewChat = document.getElementById("projectNewChat");
    const projectNewChatMeta = document.getElementById("projectNewChatMeta");

    async function api(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: {"Content-Type": "application/json", ...headers, ...(options.headers || {})}
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }

    async function loadStatus() {
      try {
        const status = await api(`${API_BASE}/status`);
        const name = status.server_name || PROVIDER_LABEL;
        devicesEl.innerHTML = `<button class="device-chip${status.connected ? "" : " off"}"><span class="dot"></span><span class="laptop"></span><span>${escapeHtml(name)}</span></button>`;
      } catch (error) {
        devicesEl.innerHTML = `<button class="device-chip off"><span class="dot"></span><span class="laptop"></span><span>${escapeHtml(error.message)}</span></button>`;
      }
    }

    async function loadSessions() {
      try {
        const data = await api(`${API_BASE}/sessions`);
        sessions = data.sessions || [];
        if (selected && !sessions.some(session => session.native_thread_id === selected.native_thread_id)) selected = null;
        renderProjects();
        renderSessions();
      } catch (error) {
        sessionsEl.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      }
    }

    async function loadProjects() {
      try {
        const data = await api(PROJECTS_URL);
        projectRoot = String(data.root || "");
        projectCatalog = Array.isArray(data.projects) ? data.projects : [];
      } catch (_error) {
        projectRoot = "";
        projectCatalog = [];
      }
    }

    async function loadHomeData() {
      await loadProjects();
      await loadSessions();
    }

    function renderProjects() {
      const seen = new Set();
      let selectedProjectRendered = false;
      projectsEl.innerHTML = "";
      chatRow.className = "nav-row" + (selectedProjectCwd ? "" : " active");
      function addProjectOption(cwd, label) {
        cwd = String(cwd || "");
        if (!cwd || seen.has(cwd)) return;
        seen.add(cwd);
        const btn = document.createElement("button");
        btn.className = "project" + (selectedProjectCwd === cwd ? " active" : "");
        btn.innerHTML = `<span class="icon-folder"></span><span class="label">${escapeHtml(label || lastPath(cwd))}</span><span></span>`;
        btn.onclick = () => selectProject(cwd);
        projectsEl.appendChild(btn);
        if (selectedProjectCwd === cwd) {
          projectsEl.appendChild(projectNewChat);
          selectedProjectRendered = true;
        }
      }
      for (const project of projectCatalog) {
        addProjectOption(project.cwd, project.name);
      }
      for (const session of sessions) {
        if (!isKnownProjectWorkspace(session.cwd)) continue;
        addProjectOption(session.cwd || "", lastPath(session.cwd || ""));
      }
      if (selectedProjectCwd && !selectedProjectRendered) projectsEl.appendChild(projectNewChat);
      updateContextHint();
      renderProjectAction();
    }

    function selectProject(cwd) {
      selectedProjectCwd = String(cwd || "");
      selected = null;
      renderProjects();
      renderSessions();
    }

    function renderProjectAction() {
      projectNewChat.hidden = !selectedProjectCwd;
      projectNewChatMeta.textContent = selectedProjectCwd ? `在 ${lastPath(selectedProjectCwd)} 新建会话` : "";
    }

    function renderSessions() {
      const needle = promptEl.value.trim().toLowerCase();
      const filtered = sortedSessions().filter(session => {
        if (selectedProjectCwd && !(sessionProjectKey(session) === selectedProjectCwd)) return false;
        if (!needle) return true;
        return `${session.title || ""} ${session.cwd || ""}`.toLowerCase().includes(needle);
      });
      sessionsEl.innerHTML = "";
      if (!filtered.length) {
        sessionsEl.innerHTML = `<div class="empty">没有最近聊天</div>`;
        return;
      }
      renderSessionList(filtered.slice(0, SESSION_PREVIEW_LIMIT), sessionsEl);
      if (filtered.length <= SESSION_PREVIEW_LIMIT) return;
      const details = document.createElement("details");
      details.className = "more-sessions";
      const summary = document.createElement("summary");
      summary.textContent = `更多聊天 ${filtered.length - SESSION_PREVIEW_LIMIT}`;
      const body = document.createElement("div");
      body.className = "more-sessions-body";
      details.append(summary, body);
      renderSessionList(filtered.slice(SESSION_PREVIEW_LIMIT), body);
      sessionsEl.append(details);
    }

    function renderSessionList(source, target) {
      for (const session of source) {
        const btn = document.createElement("button");
        btn.className = "recent" + (selected && selected.native_thread_id === session.native_thread_id ? " active" : "");
        btn.innerHTML = `<span class="recent-copy"><span class="label recent-title">${escapeHtml(session.title || session.native_thread_id)}</span><span class="meta">${escapeHtml(lastPath(session.cwd || ""))} · ${escapeHtml(session.status || "")}</span></span><span class="time">${escapeHtml(relativeTime(sessionActivityAt(session)))}</span>`;
        btn.onclick = () => {
          selected = session;
          openLive(session);
        };
        target.appendChild(btn);
      }
    }

    async function control(action, body) {
      if (!selected) return;
      await api(`${API_BASE}/sessions/${encodeURIComponent(selected.native_thread_id)}/${action}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      await loadSessions();
    }

    async function startNewChat(prompt) {
      const result = await api(`${API_BASE}/sessions/start`, {
        method: "POST",
        body: JSON.stringify({cwd: selectedProjectCwd, prompt})
      });
      openLive(result);
    }

    async function openProjectNewChat() {
      if (!selectedProjectCwd) return;
      projectNewChat.disabled = true;
      try {
        await startNewChat("");
      } finally {
        projectNewChat.disabled = false;
      }
    }

    async function openLive(session = selected) {
      if (!session) return;
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      params.set("native_provider", PROVIDER);
      params.set("native_thread_id", session.native_thread_id);
      location.href = `/workers/${session.agent_run_id}/live?${params.toString()}`;
    }
    document.getElementById("send").onclick = async () => {
      const prompt = promptEl.value.trim();
      if (!prompt) {
        promptEl.focus();
        return;
      }
      await startNewChat(prompt);
    };
    document.getElementById("chat").onclick = () => selectProject("");
    projectNewChat.onclick = openProjectNewChat;
    document.getElementById("back").onclick = () => history.back();
    promptEl.addEventListener("input", renderSessions);
    function sessionProjectKey(session) {
      return String((session && session.cwd) || "");
    }
    function isKnownProjectWorkspace(cwd) {
      const value = String(cwd || "");
      if (!value) return false;
      if (projectCatalog.some(project => String(project.cwd || "") === value)) return true;
      if (!projectRoot) return false;
      const normalizedRoot = projectRoot.endsWith("/") ? projectRoot : projectRoot + "/";
      if (!value.startsWith(normalizedRoot)) return false;
      const parts = value.slice(normalizedRoot.length).split("/").filter(Boolean);
      return parts.length === 1;
    }
    function sortedSessions() {
      return [...sessions].sort((left, right) => {
        return Date.parse(sessionActivityAt(right)) - Date.parse(sessionActivityAt(left));
      });
    }
    function sessionActivityAt(session) {
      return session.activity_at || session.updated_at || "";
    }
    function updateContextHint() {
      const project = selectedProjectCwd ? lastPath(selectedProjectCwd) : "";
      controlsEl.dataset.project = project;
      promptEl.placeholder = project ? `在 ${project} 中开始新聊天或搜索` : "搜索聊天或开始新聊天";
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function lastPath(path) {
      const parts = String(path).split("/").filter(Boolean);
      return parts[parts.length - 1] || path || PROVIDER_LABEL;
    }
    function relativeTime(value) {
      const stamp = Date.parse(value);
      if (!Number.isFinite(stamp)) return "";
      const minutes = Math.max(0, Math.round((Date.now() - stamp) / 60000));
      if (minutes < 1) return "刚刚";
      if (minutes < 60) return `${minutes}分钟`;
      const hours = Math.round(minutes / 60);
      if (hours < 48) return `${hours}小时`;
      return `${Math.round(hours / 24)}天`;
    }
    loadStatus();
    loadHomeData();
    setInterval(loadHomeData, 3000);
  </script>
</body>
</html>"""
    return (
        template.replace("__PROVIDER_LABEL__", escape(provider_label))
        .replace("__PROVIDER_JSON__", json.dumps(provider_name, ensure_ascii=False))
        .replace("__PROVIDER_LABEL_JSON__", json.dumps(provider_label, ensure_ascii=False))
        .replace("__API_BASE_JSON__", json.dumps(api_base, ensure_ascii=False))
    )


def _live_page(agent_run_id: int, *, native_provider: str = "codex") -> str:
    stream_path = f"/api/workers/{agent_run_id}/stream"
    native_provider = native_provider.strip() or "codex"
    provider_label = _native_provider_display_name(native_provider)
    api_base = f"/api/native/{quote(native_provider, safe='')}"
    safe_title = escape(provider_label)
    template = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__SAFE_TITLE__</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #000; color: #f7f7f8; }
    .native-mobile-shell, .codex-run-shell { min-height: 100vh; background: #000; }
    header { position: sticky; top: 0; z-index: 3; display: grid; grid-template-columns: 54px 1fr 54px; align-items: center; gap: 8px; min-height: 78px; padding: 10px 20px 8px; background: rgba(0,0,0,.96); border-bottom: 1px solid #25262b; backdrop-filter: blur(16px); }
    .circle { width: 50px; height: 50px; border-radius: 50%; border: 1px solid #34363d; background: #202126; color: #fff; font-size: 30px; line-height: 1; }
    .screen-title { min-width: 0; text-align: center; }
    h1 { margin: 0; font-size: 22px; font-weight: 780; letter-spacing: 0; }
    .subtitle { margin-top: 5px; color: #9ca3af; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 7px; background: #f59e0b; vertical-align: 1px; }
    .connected .status-dot { background: #22c55e; }
    .reconnecting .status-dot { background: #ef4444; }
    main { padding: 12px 20px 150px; }
    .codex-status-flow { position: sticky; top: 78px; z-index: 2; display: grid; grid-template-columns: 12px 1fr auto; gap: 10px; align-items: center; min-height: 42px; margin: 0 -20px 8px; padding: 10px 20px; background: rgba(0,0,0,.94); border-bottom: 1px solid #17181c; color: #c8c8d0; font-size: 14px; }
    .run-pulse { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; box-shadow: 0 0 16px rgba(34,197,94,.7); }
    .run-state.busy .run-pulse { background: #f59e0b; box-shadow: 0 0 16px rgba(245,158,11,.7); }
    .run-state.failed .run-pulse { background: #ef4444; box-shadow: 0 0 16px rgba(239,68,68,.7); }
    .event-cursor { color: #777b86; font-size: 12px; }
    .codex-transcript { display: grid; gap: 18px; padding-top: 8px; }
    .transcript-item { display: grid; gap: 7px; min-width: 0; padding: 0; }
    .transcript-meta { color: #9aa0aa; font-size: 13px; }
    .transcript-body { white-space: pre-wrap; overflow-wrap: anywhere; color: #f4f4f5; font-size: 17px; line-height: 1.62; }
    .transcript-body p { margin: 0 0 13px; }
    .transcript-body p:last-child { margin-bottom: 0; }
    .transcript-body h3 { margin: 18px 0 8px; color: #ffffff; font-size: 18px; line-height: 1.35; }
    .transcript-body h3:first-child { margin-top: 0; }
    .transcript-body ul, .transcript-body ol { margin: 0 0 13px 1.3em; padding: 0; display: grid; gap: 6px; white-space: normal; }
    .transcript-body li { padding-left: 2px; white-space: normal; }
    .transcript-body strong { color: #ffffff; font-weight: 760; }
    .transcript-body a { color: #93c5fd; text-decoration: none; border-bottom: 1px solid rgba(147, 197, 253, .45); }
    .transcript-body code { padding: 1px 5px; border-radius: 5px; background: #1d2027; color: #e5e7eb; font: .92em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .transcript-body pre { margin: 0 0 13px; overflow: auto; padding: 12px 13px; border: 1px solid #2e333d; border-radius: 8px; background: #111318; white-space: pre; }
    .transcript-body pre code { padding: 0; border-radius: 0; background: transparent; font-size: 13px; line-height: 1.5; }
    .transcript-item.user { justify-self: end; justify-items: end; max-width: min(82%, 520px); }
    .transcript-item.user .transcript-meta { display: none; }
    .transcript-item.user .transcript-body { padding: 10px 13px; border: 1px solid #333842; border-radius: 18px 18px 4px 18px; background: #20242d; line-height: 1.5; }
    .transcript-item.local-pending .transcript-body { opacity: .86; }
    .transcript-item.assistant { justify-self: start; max-width: 100%; padding-left: 22px; border-left: 2px solid #30333a; }
    .transcript-images { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
    .transcript-image { width: min(180px, 52vw); max-height: 180px; border-radius: 12px; object-fit: cover; border: 1px solid #3f4550; background: #050506; }
    .status-event { display: grid; grid-template-columns: 18px 1fr; gap: 10px; align-items: start; color: #aeb4bf; font-size: 14px; line-height: 1.5; }
    .status-event:before { content: ""; width: 8px; height: 8px; margin-top: 7px; border-radius: 50%; background: #6b7280; }
    .status-event.busy:before { background: #f59e0b; }
    .status-event.done:before { background: #22c55e; }
    .status-event.failed:before { background: #ef4444; }
    .status-title { display: block; color: #d4d4d8; font-weight: 700; }
    .status-detail { display: block; white-space: pre-wrap; overflow-wrap: anywhere; }
    .composer-activity-dot { width: 7px; height: 7px; margin: 8px 0 14px 2px; border-radius: 50%; background: #f4f4f5; opacity: .72; transition: width .18s ease, height .18s ease, opacity .18s ease, transform .18s ease; }
    .composer-activity-dot.active { width: 13px; height: 13px; opacity: 1; animation: composerPulse 1.15s ease-in-out infinite; }
    @keyframes composerPulse { 0%, 100% { transform: scale(.82); } 50% { transform: scale(1.18); } }
    .history-fold { width: 100%; min-height: 38px; margin: 2px 0 10px; border: 0; border-bottom: 1px solid #24262d; border-radius: 0; background: transparent; color: #b8bdc8; text-align: left; font-size: 15px; }
    .history-fold[hidden] { display: none; }
    .turn-fold { border: 0; border-bottom: 1px solid #24262d; border-radius: 0; background: transparent; overflow: visible; }
    .turn-fold summary { display: grid; gap: 8px; min-height: 42px; padding: 0 0 8px; cursor: pointer; list-style: none; color: #d7dae1; }
    .turn-fold summary::-webkit-details-marker { display: none; }
    .turn-fold-row { display: flex; gap: 6px; align-items: center; min-width: 0; }
    .turn-fold-title { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 16px; }
    .turn-fold-chevron { flex: 0 0 auto; color: #aeb4bf; font-size: 18px; transition: transform .16s ease; }
    .turn-fold[open] .turn-fold-chevron { transform: rotate(90deg); }
    .turn-fold-preview { display: grid; gap: 8px; padding: 0 0 8px; }
    .turn-fold[open] .turn-fold-preview { display: none; }
    .turn-fold-preview-line { min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; line-height: 1.42; }
    .turn-fold-preview-user { justify-self: end; max-width: min(82%, 520px); padding: 8px 11px; border: 1px solid #333842; border-radius: 15px 15px 4px 15px; background: #20242d; color: #f4f4f5; }
    .turn-fold-preview-assistant { justify-self: start; padding-left: 18px; border-left: 2px solid #30333a; color: #cfd3dc; }
    .turn-fold-body { display: grid; gap: 18px; padding: 12px 0 18px; }
    .codex-tool-call, .file-change-card, .approval-card { border: 1px solid #30333a; background: #0f1014; border-radius: 10px; overflow: hidden; }
    .codex-tool-call.failed { border-color: #7f1d1d; }
    .tool-head, .file-head, .approval-head { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 11px 12px; border-bottom: 1px solid #26282f; }
    .tool-title, .file-title, .approval-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #f4f4f5; font-size: 14px; font-weight: 720; }
    .tool-state, .file-state { color: #9ca3af; font-size: 12px; }
    .tool-output, .file-body { margin: 0; max-height: 260px; overflow: auto; padding: 11px 12px; color: #d8dee9; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.45; }
    .approval-card { border-color: #854d0e; background: #171107; }
    .approval-card.resolving { border-color: #a16207; }
    .approval-card.resolved { border-color: #166534; background: #07130b; }
    .approval-card.failed { border-color: #7f1d1d; background: #17090a; }
    .approval-body { padding: 0 12px 12px; color: #fde68a; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.5; }
    .approval-state { padding: 0 12px 10px; color: #d6d3d1; font-size: 13px; }
    .approval-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 0 12px 12px; }
    .approval-action { min-height: 46px; border: 1px solid transparent; transition: opacity .16s ease, background .16s ease, border-color .16s ease; }
    .approval-action.approve { background: #14532d; color: #ecfdf5; border-color: #22c55e; }
    .approval-action.danger { background: #7f1d1d; color: #fff1f2; border-color: #ef4444; }
    .approval-action.selected { opacity: 1; box-shadow: inset 0 0 0 2px rgba(255,255,255,.38); }
    .approval-action.muted { background: #2a2a2d; color: #8e929b; border-color: #34363d; opacity: .62; box-shadow: none; }
    .codex-input-dock { position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; display: grid; gap: 8px; padding: 10px 16px 16px; background: linear-gradient(to top, #000 86%, rgba(0,0,0,0)); border-top: 1px solid #272930; }
    .composer-tools { display: flex; gap: 8px; align-items: center; min-width: 0; }
    .composer-settings { position: relative; flex: 1; display: flex; gap: 8px; min-width: 0; }
    .setting-pill { min-height: 38px; border-radius: 19px; padding: 0 14px; background: #2a2a2d; color: #f4f4f5; border: 0; font-size: 14px; font-weight: 760; white-space: nowrap; }
    .setting-pill.permissions { flex: 0 0 auto; }
    .model-popover { position: absolute; left: 0; bottom: 48px; width: min(330px, calc(100vw - 32px)); border: 1px solid #3a3a40; border-radius: 22px; background: #222225; box-shadow: 0 20px 54px rgba(0,0,0,.55); overflow: hidden; z-index: 6; }
    .model-popover[hidden] { display: none; }
    .setting-row { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr) auto; gap: 12px; align-items: center; min-height: 76px; padding: 12px 18px; border-bottom: 1px solid #343439; color: #f4f4f5; }
    .setting-row:last-child { border-bottom: 0; }
    .setting-label { display: grid; gap: 5px; min-width: 0; font-size: 16px; font-weight: 760; }
    .setting-value { color: #b8bdc8; font-size: 14px; font-weight: 500; }
    .setting-chevron { color: #f4f4f5; font-size: 28px; line-height: 1; }
    .model-selector, .setting-selector { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; pointer-events: none; }
    .setting-options { display: grid; gap: 6px; padding: 0 12px 12px; border-bottom: 1px solid #343439; background: #1d1d20; }
    .setting-options[hidden] { display: none; }
    .setting-option { display: flex; justify-content: space-between; gap: 10px; align-items: center; min-height: 38px; border-radius: 13px; padding: 7px 11px; background: transparent; color: #d4d4d8; font-size: 15px; text-align: left; }
    .setting-option.selected { background: #34343a; color: #fff; }
    .setting-option-check { color: #f4f4f5; font-weight: 800; }
    .attach-button { width: 40px; min-height: 38px; padding: 0; border-radius: 11px; background: #20242e; color: #f4f4f5; border: 1px solid #3f4550; font-size: 24px; line-height: 1; }
    .send-status { min-width: 66px; color: #9ca3af; font-size: 12px; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .send-status.error { color: #fca5a5; }
    .send-status.ok { color: #86efac; }
    .attachment-strip { display: flex; gap: 8px; min-height: 54px; overflow-x: auto; padding-bottom: 2px; }
    .attachment-strip[hidden] { display: none; }
    .attachment-chip { position: relative; flex: 0 0 auto; display: grid; grid-template-columns: 46px minmax(80px, 1fr) 28px; align-items: center; gap: 8px; max-width: 230px; min-height: 50px; border: 1px solid #30333a; border-radius: 10px; background: #11141b; padding: 4px; color: #f4f4f5; }
    .attachment-chip img { width: 46px; height: 42px; border-radius: 7px; object-fit: cover; background: #050506; }
    .attachment-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #d4d4d8; font-size: 12px; }
    .attachment-remove { width: 28px; min-height: 28px; padding: 0; border-radius: 50%; background: #272b35; color: #f4f4f5; font-size: 16px; }
    .interruption-choice { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 2px 0; }
    .interruption-choice[hidden] { display: none; }
    .choice-action { min-height: 42px; border-radius: 12px; background: #20242e; color: #f4f4f5; border: 1px solid #3f4550; }
    .choice-action.primary { background: #f4f4f5; color: #101114; border: 0; }
    .dock-row { display: flex; gap: 10px; min-width: 0; align-items: center; }
    .dock-actions { display: flex; gap: 10px; min-width: 0; }
    .dock-actions[hidden] { display: none; }
    input { flex: 1; min-width: 0; min-height: 54px; border-radius: 13px; border: 1px solid #3f4550; background: #12151d; color: #f4f4f5; padding: 0 14px; font-size: 16px; }
    button { min-height: 44px; border: 0; border-radius: 10px; padding: 8px 14px; background: #f4f4f5; color: #101114; font-weight: 760; font-size: 15px; }
    button:disabled { opacity: .56; }
    .primary-action { flex: 0 0 56px; width: 56px; min-height: 56px; border-radius: 15px; padding: 0; display: grid; place-items: center; font-size: 28px; line-height: 1; }
    .primary-action.stop { font-size: 24px; }
    button.secondary { border: 1px solid #3f4550; background: #1b1f29; color: #f4f4f5; }
    button.warn { background: #b91c1c; color: #fff; }
    .empty { color: #8d93a0; padding: 24px 0; text-align: center; }
    @media (min-width: 820px) {
      main { max-width: 780px; margin: 0 auto; }
      .codex-input-dock { left: 50%; transform: translateX(-50%); width: min(780px, 100%); }
    }
  </style>
</head>
<body>
  <div class="native-mobile-shell codex-run-shell">
    <header id="header">
      <button class="circle" id="back" aria-label="返回">‹</button>
      <div class="screen-title">
        <h1>__PROVIDER_LABEL_TEXT__</h1>
        <div class="subtitle"><span class="status-dot"></span><span id="state">connecting</span></div>
      </div>
      <button class="circle" aria-label="菜单">⋮</button>
    </header>
    <main>
      <section class="codex-status-flow run-state" id="runStatus">
        <span class="run-pulse"></span>
        <span id="runStateLabel">连接 __PROVIDER_LABEL_TEXT__ 会话</span>
        <span class="event-cursor" id="cursor"></span>
      </section>
      <button class="history-fold" id="historyFold" hidden>更早的消息</button>
      <section class="codex-transcript" id="events"><div class="empty" id="empty">等待 __PROVIDER_LABEL_TEXT__ 转录</div></section>
      <div class="composer-activity-dot" id="composerActivityDot" aria-hidden="true"></div>
    </main>
    <section class="codex-input-dock">
      <div class="composer-tools">
        <div class="composer-settings">
          <button class="setting-pill" id="modelSettingsButton" type="button">加载模型</button>
          <button class="setting-pill permissions" type="button">默认权限</button>
          <div class="model-popover" id="modelPopover" hidden>
            <div class="setting-row" role="button" tabindex="0">
              <span class="setting-label">模型<span class="setting-value" id="modelSettingValue">加载模型</span></span>
              <span></span>
              <span class="setting-chevron">›</span>
              <select id="modelSelector" class="model-selector" aria-label="选择模型">
                <option value="">加载模型</option>
              </select>
            </div>
            <div class="setting-options" id="modelOptions" hidden></div>
            <div class="setting-row" role="button" tabindex="0">
              <span class="setting-label">速度<span class="setting-value" id="serviceTierSettingValue">正常</span></span>
              <span></span>
              <span class="setting-chevron">›</span>
              <select id="serviceTierSelector" class="setting-selector" aria-label="选择速度">
                <option value="">速度</option>
              </select>
            </div>
            <div class="setting-options" id="serviceTierOptions" hidden></div>
            <div class="setting-row" role="button" tabindex="0">
              <span class="setting-label">推理<span class="setting-value" id="reasoningSettingValue">默认</span></span>
              <span></span>
              <span class="setting-chevron">›</span>
              <select id="reasoningSelector" class="setting-selector" aria-label="选择推理程度">
                <option value="">推理</option>
              </select>
            </div>
            <div class="setting-options" id="reasoningOptions" hidden></div>
          </div>
        </div>
        <button class="attach-button" id="attachmentButton" type="button" aria-label="上传照片">＋</button>
        <input id="imageInput" type="file" accept="image/*" multiple hidden>
        <span class="send-status" id="sendStatus"></span>
      </div>
      <div class="attachment-strip" id="attachmentStrip" hidden></div>
      <div class="interruption-choice" id="interruptionChoice" hidden>
        <button class="choice-action primary" id="steerChoice" type="button">引导</button>
        <button class="choice-action" id="queueChoice" type="button">排队</button>
      </div>
      <div class="dock-row">
        <input id="prompt" placeholder="继续 __PROVIDER_LABEL_TEXT__ 会话">
        <button class="primary-action" id="continue" aria-label="发送">↑</button>
      </div>
      <div class="dock-actions" hidden>
        <button class="secondary" id="steer">修正当前轮</button>
        <button class="warn" id="interrupt">中断</button>
      </div>
    </section>
  </div>
  <script>
    const state = document.getElementById("state");
    const cursor = document.getElementById("cursor");
    const events = document.getElementById("events");
    const header = document.getElementById("header");
    const empty = document.getElementById("empty");
    const runStatus = document.getElementById("runStatus");
    const runStateLabel = document.getElementById("runStateLabel");
    const historyFold = document.getElementById("historyFold");
    const composerActivityDot = document.getElementById("composerActivityDot");
    const params = new URLSearchParams(location.search);
    const token = params.get("token") || "";
    const PROVIDER = __PROVIDER_JSON__;
    const PROVIDER_LABEL = __PROVIDER_LABEL_JSON__;
    const API_BASE = __API_BASE_JSON__;
    let nativeThreadId = params.get("native_thread_id") || "";
    let nativeTurnId = "";
    let activeTurnId = "";
    let attached = false;
    let loadedEvents = [];
    let oldestEventId = 0;
    let latestEventId = 0;
    let previousEventCount = 0;
    let source = null;
    let pollInFlight = false;
    const authHeaders = token ? {"Authorization": "Bearer " + token} : {};
    const promptInput = document.getElementById("prompt");
    const continueButton = document.getElementById("continue");
    const steerButton = document.getElementById("steer");
    const interruptButton = document.getElementById("interrupt");
    const modelSettingsButton = document.getElementById("modelSettingsButton");
    const modelPopover = document.getElementById("modelPopover");
    const modelSettingValue = document.getElementById("modelSettingValue");
    const reasoningSettingValue = document.getElementById("reasoningSettingValue");
    const serviceTierSettingValue = document.getElementById("serviceTierSettingValue");
    const modelSelector = document.getElementById("modelSelector");
    const reasoningSelector = document.getElementById("reasoningSelector");
    const serviceTierSelector = document.getElementById("serviceTierSelector");
    const modelOptions = document.getElementById("modelOptions");
    const reasoningOptions = document.getElementById("reasoningOptions");
    const serviceTierOptions = document.getElementById("serviceTierOptions");
    const attachmentButton = document.getElementById("attachmentButton");
    const imageInput = document.getElementById("imageInput");
    const attachmentStrip = document.getElementById("attachmentStrip");
    const interruptionChoice = document.getElementById("interruptionChoice");
    const steerChoice = document.getElementById("steerChoice");
    const queueChoice = document.getElementById("queueChoice");
    const sendStatus = document.getElementById("sendStatus");
    const streamPathBase = "__STREAM_PATH__";
    const agentRunId = __AGENT_RUN_ID__;
    const CURRENT_TURN_EVENT_LIMIT = 5000;
    const RECENT_EVENT_LIMIT = 80;
    const OLDER_EVENT_LIMIT = 80;
    const MODEL_SETTINGS_STORAGE_KEY = "wlcodexNativeModelSettings";
    const transcriptNodes = new Map();
    const statusNodes = new Map();
    const commandNodes = new Map();
    let renderTarget = events;
    let imageAttachments = [];
    let sendingPrompt = false;
    let nativeTurnRunning = false;
    let modelCatalog = [];
    let savedModelSettings = loadSavedModelSettings();
    let modelSettingsDirty = false;
    historyFold.onclick = loadOlderEvents;
    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {"Content-Type": "application/json", ...authHeaders, ...(options.headers || {})}
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || response.statusText);
      }
      return response.json().catch(() => ({}));
    }
    async function nativeControl(action, body) {
      if (!nativeThreadId) throw new Error(`${PROVIDER_LABEL} 会话未连接`);
      return api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/${action}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
    }
    async function loadModelCatalog() {
      try {
        const result = await api(`${API_BASE}/models`);
        modelCatalog = Array.isArray(result.models) ? result.models : [];
        renderModelSettings();
      } catch (error) {
        modelCatalog = [];
        modelSelector.innerHTML = `<option value="">${escapeHtml(error.message || "模型不可用")}</option>`;
        reasoningSelector.innerHTML = `<option value="">推理</option>`;
        serviceTierSelector.innerHTML = `<option value="">速度</option>`;
        modelOptions.innerHTML = "";
        reasoningOptions.innerHTML = "";
        serviceTierOptions.innerHTML = "";
        updateSettingSummary();
      }
      updateComposerDisabled();
    }
    function renderModelSettings() {
      const visibleModels = modelCatalog.filter(model => !model.hidden);
      const models = visibleModels.length ? visibleModels : modelCatalog;
      modelSelector.innerHTML = "";
      for (const model of models) {
        const option = document.createElement("option");
        option.value = model.model || model.id || "";
        option.textContent = model.displayName || model.model || model.id || PROVIDER_LABEL;
        option.selected = Boolean(model.isDefault);
        modelSelector.append(option);
      }
      if (savedModelSettings.model && optionValueExists(modelSelector, savedModelSettings.model)) {
        modelSelector.value = savedModelSettings.model;
      } else if (!modelSelector.value && modelSelector.options.length) {
        modelSelector.selectedIndex = 0;
      }
      renderSettingOptions(modelOptions, modelSelector, () => {
        renderReasoningAndSpeed();
        updateSettingSummary();
      });
      renderReasoningAndSpeed(savedModelSettings);
      savedModelSettings = readSelectedModelSettings();
      modelSettingsDirty = false;
    }
    function renderReasoningAndSpeed(preferredSettings = {}) {
      const model = selectedModelCatalogEntry();
      const efforts = Array.isArray(model && model.supportedReasoningEfforts)
        ? model.supportedReasoningEfforts
        : [];
      const tiers = Array.isArray(model && model.serviceTiers) ? model.serviceTiers : [];
      fillSelector(
        reasoningSelector,
        efforts,
        item => item.reasoningEffort || item.id || "",
        item => reasoningEffortLabel(item.reasoningEffort || item.id || ""),
        model && model.defaultReasoningEffort,
        "推理"
      );
      fillServiceTierSelector(tiers, preferredServiceTierDefault(model, tiers));
      if (preferredSettings.effort && optionValueExists(reasoningSelector, preferredSettings.effort)) {
        reasoningSelector.value = preferredSettings.effort;
      }
      if (Object.prototype.hasOwnProperty.call(preferredSettings, "service_tier")
        && optionValueExists(serviceTierSelector, preferredSettings.service_tier)) {
        serviceTierSelector.value = preferredSettings.service_tier || "";
      }
      renderSettingOptions(reasoningOptions, reasoningSelector, updateSettingSummary);
      renderSettingOptions(serviceTierOptions, serviceTierSelector, updateSettingSummary, {includeEmpty: true});
      updateSettingSummary();
    }
    function selectedModelCatalogEntry() {
      return modelCatalog.find(model => {
        return (model.model || model.id || "") === modelSelector.value;
      }) || modelCatalog.find(model => model.isDefault) || modelCatalog[0] || null;
    }
    function fillSelector(select, items, valueFor, labelFor, defaultValue, fallbackLabel) {
      select.innerHTML = "";
      if (!items.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = fallbackLabel;
        select.append(option);
        return;
      }
      for (const item of items) {
        const option = document.createElement("option");
        option.value = valueFor(item) || "";
        option.textContent = labelFor(item) || option.value || fallbackLabel;
        option.selected = option.value === defaultValue;
        select.append(option);
      }
      if (!select.value && select.options.length) select.selectedIndex = 0;
    }
    function fillServiceTierSelector(tiers, defaultValue) {
      serviceTierSelector.innerHTML = "";
      const normalOption = document.createElement("option");
      normalOption.value = "";
      normalOption.textContent = "正常";
      normalOption.selected = !defaultValue;
      serviceTierSelector.append(normalOption);
      for (const tier of tiers) {
        const value = tier.id || tier.serviceTier || tier.name || "";
        if (!value) continue;
        const option = document.createElement("option");
        option.value = value;
        option.textContent = serviceTierLabel(value);
        option.selected = Boolean(defaultValue && value === defaultValue);
        serviceTierSelector.append(option);
      }
    }
    function renderSettingOptions(container, select, onChoose, options = {}) {
      container.innerHTML = "";
      Array.from(select.options || []).forEach(option => {
        if (!option.value && !options.includeEmpty) return;
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.value = option.value;
        button.className = "setting-option" + (option.selected ? " selected" : "");
        button.textContent = option.textContent || option.value;
        button.disabled = select.disabled;
        if (option.selected) {
          const check = document.createElement("span");
          check.className = "setting-option-check";
          check.textContent = "✓";
          button.append(check);
        }
        button.onclick = () => {
          const previousValue = select.value;
          select.value = option.value;
          if (select.value !== previousValue) markModelSettingsDirty();
          syncSettingOptionsSelection(container, select);
          container.hidden = true;
          if (onChoose) onChoose();
          updateComposerDisabled();
        };
        container.append(button);
      });
    }
    function syncSettingOptionsSelection(container, select) {
      for (const button of Array.from(container.querySelectorAll(".setting-option"))) {
        const selected = button.dataset.value === select.value;
        button.classList.toggle("selected", selected);
        const existingCheck = button.querySelector(".setting-option-check");
        if (selected && !existingCheck) {
          const check = document.createElement("span");
          check.className = "setting-option-check";
          check.textContent = "✓";
          button.append(check);
        } else if (!selected && existingCheck) {
          existingCheck.remove();
        }
      }
    }
    function syncSettingOptionsDisabled() {
      for (const [container, select] of [
        [modelOptions, modelSelector],
        [serviceTierOptions, serviceTierSelector],
        [reasoningOptions, reasoningSelector],
      ]) {
        for (const button of Array.from(container.querySelectorAll(".setting-option"))) {
          button.disabled = select.disabled;
        }
      }
    }
    function toggleSettingOptions(container) {
      for (const node of [modelOptions, serviceTierOptions, reasoningOptions]) {
        if (node !== container) node.hidden = true;
      }
      container.hidden = !container.hidden;
    }
    function preferredServiceTierDefault(model, tiers) {
      const defaultValue = String((model && model.defaultServiceTier) || "").toLowerCase();
      if (!defaultValue || ["fast", "priority"].includes(defaultValue)) return "";
      const match = tiers.find(tier => {
        return String(tier.id || tier.serviceTier || tier.name || "").toLowerCase() === defaultValue;
      });
      return match ? match.id || match.serviceTier || match.name || "" : "";
    }
    function serviceTierLabel(value) {
      const key = String(value || "").trim().toLowerCase();
      if (["", "auto", "default", "normal", "standard"].includes(key)) return "正常";
      if (key === "fast" || key === "priority") return "快速";
      if (key === "flex") return "弹性";
      return String(value || "速度");
    }
    function reasoningEffortLabel(value) {
      const key = String(value || "").trim().toLowerCase();
      if (["minimal", "none"].includes(key)) return "最少";
      if (key === "low") return "低";
      if (["medium", "default", "normal", ""].includes(key)) return "正常";
      if (key === "high") return "高";
      if (["xhigh", "extra_high"].includes(key)) return "极高";
      if (["max", "maximum"].includes(key)) return "最大";
      return String(value || "推理");
    }
    function loadSavedModelSettings() {
      try {
        return normalizeModelSettings(JSON.parse(localStorage.getItem(MODEL_SETTINGS_STORAGE_KEY) || "{}"));
      } catch (_error) {
        return normalizeModelSettings({});
      }
    }
    function readSelectedModelSettings() {
      return normalizeModelSettings({
        model: modelSelector.value,
        effort: reasoningSelector.value,
        service_tier: serviceTierSelector.value
      });
    }
    function normalizeModelSettings(settings = {}) {
      return {
        model: typeof settings.model === "string" ? settings.model : "",
        effort: typeof settings.effort === "string" ? settings.effort : "",
        service_tier: typeof settings.service_tier === "string" ? settings.service_tier : ""
      };
    }
    function optionValueExists(select, value) {
      const normalized = String(value || "");
      return Array.from(select.options || []).some(option => option.value === normalized);
    }
    function modelSettingsEqual(left, right) {
      return left.model === right.model
        && left.effort === right.effort
        && left.service_tier === right.service_tier;
    }
    function saveModelSettingsIfChanged() {
      if (!modelSettingsDirty) return;
      const nextSettings = readSelectedModelSettings();
      const changed = !modelSettingsEqual(savedModelSettings, nextSettings);
      savedModelSettings = nextSettings;
      if (changed) {
        try {
          localStorage.setItem(MODEL_SETTINGS_STORAGE_KEY, JSON.stringify(savedModelSettings));
        } catch (_error) {
          // Some mobile browsers can block storage in private contexts; the current page state still applies.
        }
      }
      modelSettingsDirty = false;
    }
    function markModelSettingsDirty() {
      modelSettingsDirty = true;
    }
    async function resolveApproval(requestId, action, card) {
      if (!requestId) return;
      setApprovalState(card, action, "pending");
      try {
        await api(`${API_BASE}/approvals/${encodeURIComponent(requestId)}/resolve`, {
          method: "POST",
          body: JSON.stringify({action})
        });
        setApprovalState(card, action, "resolved");
        await pollEvents();
      } catch (error) {
        setApprovalState(card, action, "failed", error.message || String(error));
      }
    }
    function setApprovalState(card, action, state, message = "") {
      if (!card) return;
      const stateNode = card.querySelector(".approval-state");
      if (action) card.dataset.selectedAction = action;
      card.dataset.approvalState = state;
      card.classList.toggle("resolving", state === "pending");
      card.classList.toggle("resolved", state === "resolved");
      card.classList.toggle("failed", state === "failed");
      setApprovalButtons(card, action, state);
      if (state === "pending" || state === "resolved") stateNode.textContent = approvalStateText(action, state);
      else if (state === "failed") stateNode.textContent = message || "审批失败";
      updateRunState(stateNode.textContent, state === "failed" ? "failed" : "busy");
    }
    function setApprovalButtons(card, action, state) {
      const locked = state === "pending" || state === "resolved";
      for (const button of Array.from(card.querySelectorAll(".approval-action"))) {
        const selected = button.dataset.action === action;
        button.classList.toggle("selected", selected);
        button.classList.toggle("muted", state !== "idle" && !selected);
        button.disabled = locked || (state !== "idle" && !selected);
      }
    }
    function approvalStateText(action, state) {
      if (action === "approve_once") return state === "pending" ? "批准一次处理中" : "已批准一次";
      if (action === "approve_session") return state === "pending" ? "本会话批准处理中" : "本会话已批准";
      if (action === "deny") return state === "pending" ? "拒绝处理中" : "已拒绝";
      if (action === "cancel") return state === "pending" ? "取消处理中" : "已取消";
      return state === "pending" ? "审批处理中" : "审批已处理";
    }
    function approvalResolvedAction(event, card) {
      const payload = (event && event.payload) || {};
      const response = payload.response || {};
      if (card && card.dataset.selectedAction) return card.dataset.selectedAction;
      if (payload.action) return String(payload.action);
      if (response.action) return String(response.action);
      if (response.scope === "session") return "approve_session";
      if (response.decision === "acceptForSession" || response.decision === "approved_for_session") return "approve_session";
      if (response.decision === "decline" || response.decision === "denied") return "deny";
      if (response.decision === "cancel" || response.decision === "abort") return "cancel";
      return "approve_once";
    }
    function approvalActionLabel(action) {
      if (action === "approve_once") return "批准一次";
      if (action === "approve_session") return "本会话批准";
      if (action === "deny") return "拒绝";
      if (action === "cancel") return "取消";
      return "审批";
    }
    continueButton.onclick = () => submitPrompt();
    steerChoice.onclick = () => submitPrompt("steer");
    queueChoice.onclick = () => submitPrompt("continue");
    steerButton.onclick = () => submitPrompt("steer");
    interruptButton.onclick = interruptNativeTurn;
    modelSettingsButton.onclick = () => {
      const willClose = !modelPopover.hidden;
      if (willClose) saveModelSettingsIfChanged();
      modelPopover.hidden = willClose;
      if (willClose) {
        modelOptions.hidden = true;
        serviceTierOptions.hidden = true;
        reasoningOptions.hidden = true;
      }
    };
    modelSelector.onchange = () => {
      renderReasoningAndSpeed();
      updateSettingSummary();
      markModelSettingsDirty();
    };
    reasoningSelector.onchange = () => {
      renderSettingOptions(reasoningOptions, reasoningSelector, updateSettingSummary);
      updateSettingSummary();
      markModelSettingsDirty();
    };
    serviceTierSelector.onchange = () => {
      renderSettingOptions(serviceTierOptions, serviceTierSelector, updateSettingSummary, {includeEmpty: true});
      updateSettingSummary();
      markModelSettingsDirty();
    };
    modelSettingValue.closest(".setting-row").onclick = event => {
      if (event.target === modelSelector) return;
      toggleSettingOptions(modelOptions);
    };
    serviceTierSettingValue.closest(".setting-row").onclick = event => {
      if (event.target === serviceTierSelector) return;
      toggleSettingOptions(serviceTierOptions);
    };
    reasoningSettingValue.closest(".setting-row").onclick = event => {
      if (event.target === reasoningSelector) return;
      toggleSettingOptions(reasoningOptions);
    };
    attachmentButton.onclick = () => imageInput.click();
    imageInput.onchange = async () => {
      const files = Array.from(imageInput.files || []);
      imageInput.value = "";
      for (const file of files) {
        try {
          imageAttachments.push(await readImageAttachment(file));
        } catch (error) {
          renderStatus("image_failed", error.message || String(error));
        }
      }
      renderAttachments();
      updateComposerDisabled();
    };
    promptInput.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submitPrompt();
      }
    });
    promptInput.addEventListener("input", updateComposerDisabled);
    document.getElementById("back").onclick = () => {
      const query = token ? "?token=" + encodeURIComponent(token) : "";
      location.href = `/native/${encodeURIComponent(PROVIDER)}` + query;
    };
    async function submitPrompt(action = primaryComposerAction()) {
      if (action === "interrupt") {
        await interruptNativeTurn();
        return;
      }
      if (action === "choose") {
        openInterruptionChoice();
        return;
      }
      const prompt = promptInput.value;
      if (!prompt.trim() && imageAttachments.length === 0) {
        setSendStatus("请输入内容或照片", "error");
        return;
      }
      saveModelSettingsIfChanged();
      const body = {prompt};
      if (savedModelSettings.model) body.model = savedModelSettings.model;
      if (savedModelSettings.effort) body.effort = savedModelSettings.effort;
      if (savedModelSettings.service_tier) body.service_tier = savedModelSettings.service_tier;
      if (imageAttachments.length) {
        body.images = imageAttachments.map(image => ({
          url: image.url,
          filename: image.filename,
          mime_type: image.mime_type
        }));
      }
      if (action === "steer") body.expected_turn_id = activeTurnId;
      const echoAttachments = imageAttachments.map(image => ({...image}));
      renderLocalUserEcho(prompt, echoAttachments);
      sendingPrompt = true;
      closeInterruptionChoice();
      updateComposerDisabled();
      setSendStatus(action === "steer" ? "修正中" : "发送中", "");
      try {
        const result = await nativeControl(action, body);
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.active_turn_id || (result.turn_running ? result.turn_id || "" : "");
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        promptInput.value = "";
        imageAttachments = [];
        renderAttachments();
        setSendStatus("已发送", "ok");
        await pollEvents();
      } catch (error) {
        await pollEvents();
        renderStatus(action + "_failed", error.message || String(error));
        setSendStatus(error.message || "发送失败", "error");
      } finally {
        sendingPrompt = false;
        updateComposerDisabled();
      }
    }
    async function interruptNativeTurn() {
      if (!activeTurnId) return;
      sendingPrompt = true;
      updateComposerDisabled();
      setSendStatus("中断中", "");
      try {
        await nativeControl("interrupt", {turn_id: activeTurnId});
        activeTurnId = "";
        nativeTurnRunning = false;
        setSendStatus("已中断", "ok");
        await pollEvents();
      } catch (error) {
        await pollEvents();
        renderStatus("interrupt_failed", error.message || String(error));
        setSendStatus(error.message || "中断失败", "error");
      } finally {
        sendingPrompt = false;
        updateComposerDisabled();
      }
    }
    function composerHasDraft() {
      return Boolean(promptInput.value.trim() || imageAttachments.length);
    }
    function primaryComposerAction() {
      if (nativeTurnRunning && !composerHasDraft()) return "interrupt";
      if (nativeTurnRunning) return "choose";
      return "continue";
    }
    function applyNativeTurnState(event, options = {}) {
      const payload = event.payload || {};
      const mirroredTranscript = isMirroredTranscriptEvent(event);
      if (!options.historical && !mirroredTranscript && payload.native_turn_id) nativeTurnId = payload.native_turn_id;
      if (options.historical || mirroredTranscript) return;
      if (isTerminalTurnEvent(event)) {
        if (!payload.native_turn_id || payload.native_turn_id === activeTurnId || payload.native_turn_id === nativeTurnId) activeTurnId = "";
        nativeTurnRunning = false;
      } else if (
        event.kind === "text_delta" ||
        event.kind === "reasoning_delta" ||
        event.kind === "command_started" ||
        event.kind === "command_output" ||
        event.kind === "approval_requested"
      ) {
        if (payload.native_turn_id) activeTurnId = payload.native_turn_id;
        nativeTurnRunning = true;
      }
      setComposerActivity(nativeTurnRunning || sendingPrompt);
    }
    function isMirroredTranscriptEvent(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || "");
      const turnId = String(payload.native_turn_id || payload.turnId || "");
      return itemId.startsWith("jsonl-") || turnId.startsWith("jsonl-turn:");
    }
    function isTerminalTurnEvent(event) {
      const payload = (event && event.payload) || {};
      const status = String(payload.status || "").trim().toLowerCase();
      const action = String(payload.action || "").trim().toLowerCase();
      if (event.kind === "completed" || event.kind === "failed") return true;
      if (action === "turn_completed" || action === "turn_failed") return true;
      return isCompletedStatus(status) || isFailedStatus(status);
    }
    function isCompletedStatus(status) {
      return ["completed", "done", "succeeded", "success"].includes(
        String(status || "").trim().toLowerCase()
      );
    }
    function setComposerActivity(active) {
      composerActivityDot.classList.toggle("active", Boolean(active));
    }
    function updateComposerDisabled() {
      const mode = primaryComposerAction();
      const requiresTurn = mode === "interrupt" || mode === "steer";
      continueButton.textContent = mode === "interrupt" ? "■" : "↑";
      continueButton.classList.toggle("stop", mode === "interrupt");
      continueButton.setAttribute(
        "aria-label",
        mode === "interrupt" ? "中断当前轮" : nativeTurnRunning ? "发送到当前轮" : "发送"
      );
      continueButton.disabled = (
        sendingPrompt ||
        !nativeThreadId ||
        (requiresTurn && !activeTurnId) ||
        (!nativeTurnRunning && !composerHasDraft())
      );
      steerButton.disabled = sendingPrompt || !nativeThreadId || !activeTurnId;
      attachmentButton.disabled = sendingPrompt;
      modelSelector.disabled = sendingPrompt || nativeTurnRunning;
      modelSettingsButton.disabled = false;
      reasoningSelector.disabled = sendingPrompt || nativeTurnRunning || reasoningSelector.options.length <= 1;
      serviceTierSelector.disabled = sendingPrompt || nativeTurnRunning || serviceTierSelector.options.length <= 1;
      interruptButton.disabled = sendingPrompt || !nativeThreadId || !activeTurnId;
      syncSettingOptionsDisabled();
      setComposerActivity(nativeTurnRunning || sendingPrompt);
    }
    function updateSettingSummary() {
      const modelText = selectedOptionText(modelSelector, "模型");
      const effortText = selectedOptionText(reasoningSelector, "默认");
      const tierText = selectedOptionText(serviceTierSelector, "正常");
      modelSettingValue.textContent = modelText;
      reasoningSettingValue.textContent = effortText;
      serviceTierSettingValue.textContent = tierText;
      modelSettingsButton.textContent = `${modelText} ${effortText}`.trim();
      syncSettingOptionsSelection(modelOptions, modelSelector);
      syncSettingOptionsSelection(reasoningOptions, reasoningSelector);
      syncSettingOptionsSelection(serviceTierOptions, serviceTierSelector);
    }
    function selectedOptionText(select, fallback) {
      const option = select && select.options ? select.options[select.selectedIndex] : null;
      return (option && option.textContent ? option.textContent : fallback) || fallback;
    }
    function openInterruptionChoice() {
      if (!nativeTurnRunning || !composerHasDraft()) return;
      interruptionChoice.hidden = false;
      steerChoice.disabled = sendingPrompt || !activeTurnId;
      queueChoice.disabled = sendingPrompt || !nativeThreadId;
    }
    function closeInterruptionChoice() {
      interruptionChoice.hidden = true;
    }
    function setSendStatus(text, tone) {
      sendStatus.textContent = text || "";
      sendStatus.className = "send-status" + (tone ? " " + tone : "");
    }
    async function readImageAttachment(file) {
      const dataUrl = await readFileAsDataUrl(file);
      try {
        return await resizeImageAttachment(file, dataUrl);
      } catch (_error) {
        return {
          url: dataUrl,
          filename: file.name || "image",
          mime_type: file.type || "image/*"
        };
      }
    }
    function readFileAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(reader.error || new Error("图片读取失败"));
        reader.readAsDataURL(file);
      });
    }
    function resizeImageAttachment(file, dataUrl) {
      return new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => {
          const maxSide = 1280;
          const scale = Math.min(1, maxSide / Math.max(image.width, image.height));
          if (!Number.isFinite(scale) || scale <= 0) {
            reject(new Error("图片尺寸无效"));
            return;
          }
          const width = Math.max(1, Math.round(image.width * scale));
          const height = Math.max(1, Math.round(image.height * scale));
          const canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          const context = canvas.getContext("2d");
          if (!context) {
            reject(new Error("图片处理失败"));
            return;
          }
          context.drawImage(image, 0, 0, width, height);
          const mime = file.type === "image/png" && file.size < 900000
            ? "image/png"
            : "image/jpeg";
          const url = scale < 1 || file.size > 1200000
            ? canvas.toDataURL(mime, .86)
            : dataUrl;
          resolve({
            url,
            filename: file.name || "image",
            mime_type: mime
          });
        };
        image.onerror = () => reject(new Error("图片解码失败"));
        image.src = dataUrl;
      });
    }
    function renderAttachments() {
      attachmentStrip.innerHTML = "";
      attachmentStrip.hidden = imageAttachments.length === 0;
      imageAttachments.forEach((attachment, index) => {
        const chip = document.createElement("div");
        chip.className = "attachment-chip";
        const preview = document.createElement("img");
        preview.src = attachment.url;
        preview.alt = "";
        const name = document.createElement("span");
        name.className = "attachment-name";
        name.textContent = attachment.filename || "image";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "attachment-remove";
        remove.setAttribute("aria-label", "移除照片");
        remove.textContent = "×";
        remove.onclick = () => {
          imageAttachments.splice(index, 1);
          renderAttachments();
        };
        chip.append(preview, name, remove);
        attachmentStrip.append(chip);
      });
    }
    function renderLocalUserEcho(text, images) {
      const event = {
        kind: "user_message",
        payload: {
          text: text || "",
          images: images || [],
          itemId: "local-user-" + Date.now()
        }
      };
      renderTranscript(event, "user local-pending", "你");
      window.scrollTo(0, document.body.scrollHeight);
    }
    async function attachNative() {
      if (!nativeThreadId || attached) return;
      attached = true;
      try {
        const result = await api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/attach`, {
          method: "POST",
          body: "{}"
        });
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.active_turn_id || "";
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        updateComposerDisabled();
      } catch (error) {
        renderStatus("attach_failed", error.message || String(error));
      }
    }
    updateComposerDisabled();
    loadModelCatalog();
    attachNative().then(() => {
      return loadRecentEvents();
    }).catch(() => {
      return loadRecentEvents();
    }).then(() => {
      setInterval(pollEvents, 1000);
    });
    async function loadRecentEvents() {
      let snapshot = await api(eventsPath("tail=" + CURRENT_TURN_EVENT_LIMIT, {currentTurn: true}));
      if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
      loadedEvents = snapshot.events || [];
      if ((!loadedEvents.length || !hasLiveDisplayEvents(loadedEvents)) && nativeTurnId) {
        snapshot = await api(eventsPath("tail=" + RECENT_EVENT_LIMIT));
        if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
        loadedEvents = snapshot.events || [];
      }
      previousEventCount = snapshot.previous_event_count || 0;
      oldestEventId = loadedEvents.length ? loadedEvents[0].id : 0;
      latestEventId = loadedEvents.length ? loadedEvents[loadedEvents.length - 1].id : 0;
      rebuildStream();
      updateHistoryFold();
      openStream(latestEventId);
      pollEvents();
    }
    function hasLiveDisplayEvents(sourceEvents) {
      return sourceEvents.some(event => {
        if (!event) return false;
        if (isInternalEvent(event)) return false;
        return event.kind !== "event";
      });
    }
    function isInternalEvent(event) {
      return Boolean(
        event && (
          event.type === "model.usage.updated" ||
          isNativeExecutionDetail(event) ||
          isNativeReasoningDetail(event) ||
          isNativeActivityDetail(event)
        )
      );
    }
    function isNativeFeedbackMode(event) {
      const payload = (event && event.payload) || {};
      return Boolean(nativeThreadId || payload.native_thread_id);
    }
    function isCommandEvent(event) {
      return Boolean(event && (
        event.kind === "command_started" ||
        event.kind === "command_output" ||
        event.kind === "command_completed" ||
        event.kind === "command_failed"
      ));
    }
    function isNativeExecutionDetail(event) {
      return isNativeFeedbackMode(event) && isCommandEvent(event);
    }
    function isNativeReasoningDetail(event) {
      return isNativeFeedbackMode(event) && event && event.kind === "reasoning_delta";
    }
    function isNativeActivityDetail(event) {
      return isNativeFeedbackMode(event) && event && event.kind === "activity";
    }
    async function loadOlderEvents() {
      if (!oldestEventId || !previousEventCount) return;
      historyFold.disabled = true;
      try {
        const snapshot = await api(eventsPath(`before=${oldestEventId}&limit=${OLDER_EVENT_LIMIT}`));
        if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
        const older = snapshot.events || [];
        loadedEvents = older.concat(loadedEvents);
        previousEventCount = snapshot.previous_event_count || 0;
        oldestEventId = loadedEvents.length ? loadedEvents[0].id : 0;
        rebuildStream();
        updateHistoryFold();
      } finally {
        historyFold.disabled = false;
      }
    }
    function openStream(afterId) {
      if (source) source.close();
      source = new EventSource(streamPathWithCursor(afterId));
      source.onopen = () => setConnectionState("connected");
      source.onerror = () => { setConnectionState("reconnecting"); pollEvents(); };
      source.onmessage = (message) => renderLiveEvent(JSON.parse(message.data));
      [
        "lifecycle",
        "activity",
        "user_message",
        "text_delta",
        "message_completed",
        "reasoning_delta",
        "command_started",
        "command_output",
        "command_completed",
        "command_failed",
        "file_changed",
        "diff_updated",
        "approval_requested",
        "approval_resolved",
        "completed",
        "failed",
        "event"
      ].forEach(kind => {
        source.addEventListener(kind, message => renderLiveEvent(JSON.parse(message.data)));
      });
    }
    function streamPathWithCursor(afterId) {
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      if (afterId) params.set("after", String(afterId));
      const suffix = params.toString();
      return suffix ? streamPathBase + "?" + suffix : streamPathBase;
    }
    function eventsPath(params, options = {}) {
      const search = new URLSearchParams(params);
      if (nativeThreadId) search.set("native_thread_id", nativeThreadId);
      if (options.currentTurn && nativeTurnId) search.set("native_turn_id", nativeTurnId);
      return `/api/workers/__AGENT_RUN_ID__/events?${search.toString()}`;
    }
    async function pollEvents() {
      if (pollInFlight) return;
      pollInFlight = true;
      try {
        const snapshot = await api(eventsPath(`after=${latestEventId}&limit=100`));
        if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
        const nextEvents = snapshot.events || [];
        for (const event of nextEvents) renderLiveEvent(event);
        setConnectionState("connected");
      } catch (_error) {
        setConnectionState("reconnecting");
      } finally {
        pollInFlight = false;
      }
    }
    function rebuildStream() {
      transcriptNodes.clear();
      statusNodes.clear();
      commandNodes.clear();
      events.innerHTML = "";
      const groups = foldGroups(dedupeDisplayEvents(loadedEvents));
      const latestTurnId = latestFoldGroupTurnId(groups);
      groups.forEach(group => {
        renderFoldGroup(group, {latestTurnId});
      });
    }
    function renderLiveEvent(event) {
      if (event.id && event.id <= latestEventId) return;
      const previousLatestTurnId = latestFoldGroupTurnId(foldGroups(dedupeDisplayEvents(loadedEvents)));
      if (event.id) latestEventId = event.id;
      const incomingTurnId = eventFoldTurnId(event);
      const duplicateDisplayEvent = isDuplicateDisplayEvent(event, loadedEvents);
      loadedEvents.push(event);
      if (isInternalEvent(event)) {
        if (isNativeExecutionDetail(event)) {
          handleHiddenNativeFeedback(event);
        } else if (isNativeReasoningDetail(event)) {
          handleHiddenNativeFeedback(event);
        } else if (isNativeActivityDetail(event)) {
          handleHiddenNativeFeedback(event);
        } else if (event.id) {
          cursor.textContent = "#" + event.id;
        }
        return;
      }
      if (duplicateDisplayEvent) {
        applyNativeTurnState(event);
        updateComposerDisabled();
        if (event.id) cursor.textContent = "#" + event.id;
        return;
      }
      if (isOfficialAssistantTranscriptEvent(event)) {
        rebuildStream();
        applyNativeTurnState(event);
        updateComposerDisabled();
        if (event.id) cursor.textContent = "#" + event.id;
        window.scrollTo(0, document.body.scrollHeight);
        return;
      }
      if (previousLatestTurnId && incomingTurnId && incomingTurnId !== previousLatestTurnId) {
        rebuildStream();
        applyNativeTurnState(event);
        updateComposerDisabled();
        if (event.id) cursor.textContent = "#" + event.id;
        window.scrollTo(0, document.body.scrollHeight);
        return;
      }
      render(event);
    }
    function updateHistoryFold() {
      historyFold.hidden = previousEventCount <= 0;
      historyFold.textContent = previousEventCount > 0 ? "加载更早的消息" : "更早的消息";
    }
    function foldGroups(sourceEvents) {
      const groupByKey = new Map();
      let syntheticIndex = 0;
      for (const event of sourceEvents) {
        const payload = event.payload || {};
        const key = payload.native_turn_id || payload.turnId || `event:${event.id || syntheticIndex++}`;
        if (!groupByKey.has(key)) groupByKey.set(key, []);
        groupByKey.get(key).push(event);
      }
      return Array.from(groupByKey.values()).sort((left, right) => {
        return eventGroupLastId(left) - eventGroupLastId(right);
      });
    }
    function dedupeDisplayEvents(sourceEvents) {
      const officialAssistantTurns = completedAssistantTurnSet(sourceEvents);
      const seen = new Set();
      const result = [];
      for (const event of sourceEvents) {
        if (isInternalEvent(event)) continue;
        if (
          event.kind === "text_delta" &&
          officialAssistantTurns.has(assistantTurnKey(event)) &&
          !isOfficialAssistantTranscriptEvent(event)
        ) {
          continue;
        }
        const key = mirroredDisplayKey(event);
        if (key) {
          if (seen.has(key)) continue;
          seen.add(key);
        }
        result.push(event);
      }
      return result;
    }
    function completedAssistantTurnSet(sourceEvents) {
      const turns = new Set();
      for (const event of sourceEvents) {
        if (isOfficialAssistantTranscriptEvent(event)) {
          const key = assistantTurnKey(event);
          if (key) turns.add(key);
        }
      }
      return turns;
    }
    function isAssistantMessageEvent(event) {
      return event.kind === "text_delta" || event.kind === "message_completed";
    }
    function isOfficialAssistantTranscriptEvent(event) {
      if (!isAssistantMessageEvent(event)) return false;
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      return event.kind === "message_completed" || itemId.startsWith("jsonl-assistant");
    }
    function hasCompletedAssistantMessageForTurn(event) {
      const key = assistantTurnKey(event);
      if (!key) return false;
      return loadedEvents.some(previous => (
        isOfficialAssistantTranscriptEvent(previous) &&
        assistantTurnKey(previous) === key
      ));
    }
    function assistantTurnKey(event) {
      const payload = (event && event.payload) || {};
      return String(payload.native_turn_id || payload.turnId || "");
    }
    function isDuplicateDisplayEvent(event, previousEvents) {
      const key = mirroredDisplayKey(event);
      if (!key) return false;
      return previousEvents.some(previous => mirroredDisplayKey(previous) === key);
    }
    function mirroredDisplayKey(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (!itemId.startsWith("jsonl-")) return "";
      return itemId;
    }
    function eventGroupLastId(group) {
      return group.reduce((latest, event) => Math.max(latest, Number(event.id || 0)), 0);
    }
    function latestFoldGroupTurnId(groups) {
      for (let index = groups.length - 1; index >= 0; index--) {
        const payload = (groups[index][0] || {}).payload || {};
        const nativeTurnId = String(payload.native_turn_id || payload.turnId || "");
        if (nativeTurnId) return nativeTurnId;
      }
      return "";
    }
    function renderFoldGroup(group, options = {}) {
      const summary = buildFoldSummary(group, options.latestTurnId || "");
      if (!summary.shouldCollapse) {
        for (const event of group) render(event, {scroll: false, historical: true});
        return;
      }
      const details = document.createElement("details");
      details.className = "turn-fold";
      const head = document.createElement("summary");
      const labelRow = document.createElement("div");
      labelRow.className = "turn-fold-row";
      const title = document.createElement("span");
      title.className = "turn-fold-title";
      title.textContent = turnFoldTitle(group);
      const chevron = document.createElement("span");
      chevron.className = "turn-fold-chevron";
      chevron.textContent = "›";
      labelRow.append(title, chevron);
      head.append(labelRow);
      renderFoldPreview(head, group);
      const body = document.createElement("div");
      body.className = "turn-fold-body";
      details.append(head, body);
      events.append(details);
      const previousTarget = renderTarget;
      renderTarget = body;
      try {
        for (const event of group) render(event, {scroll: false, historical: true});
      } finally {
        renderTarget = previousTarget;
      }
    }
    function renderFoldPreview(head, group) {
      const userText = foldTranscriptPreviewText(group, "user_message");
      const assistantText = foldTranscriptPreviewText(group, "text_delta");
      const completedAssistantText = foldTranscriptPreviewText(group, "message_completed");
      if (!userText && !assistantText && !completedAssistantText) return;
      const preview = document.createElement("div");
      preview.className = "turn-fold-preview";
      if (userText) appendFoldPreviewLine(preview, "user", userText);
      if (completedAssistantText || assistantText) {
        appendFoldPreviewLine(preview, "assistant", completedAssistantText || assistantText);
      }
      head.append(preview);
    }
    function appendFoldPreviewLine(preview, role, text) {
      const line = document.createElement("div");
      line.className = "turn-fold-preview-line turn-fold-preview-" + role;
      line.textContent = text;
      preview.append(line);
    }
    function foldTranscriptPreviewText(group, kind) {
      const parts = [];
      for (const event of group) {
        if (event.kind !== kind) continue;
        const payload = event.payload || {};
        const text = String(payload.text || payload.delta || "").trim();
        if (!text) continue;
        parts.push(text);
        if (parts.join(" ").length >= 280) break;
      }
      return trimFoldPreview(parts.join(" "));
    }
    function trimFoldPreview(text) {
      const compact = String(text || "").replace(/\\s+/g, " ").trim();
      if (compact.length <= 220) return compact;
      return compact.slice(0, 217).trimEnd() + "...";
    }
    function buildFoldSummary(group, latestTurnId) {
      const nativeTurnId = String((group[0].payload || {}).native_turn_id || "");
      const failed = group.some(event => isFailedEvent(event));
      const pendingApproval = hasPendingApproval(group);
      const currentTurnId = activeTurnId || (nativeTurnRunning ? nativeTurnId : "");
      const shouldCollapse = (
        groupHasVisibleContent(group) &&
        nativeTurnId &&
        !failed &&
        !pendingApproval &&
        nativeTurnId !== currentTurnId &&
        nativeTurnId !== latestTurnId
      );
      return {
        nativeTurnId,
        shouldCollapse
      };
    }
    function groupHasVisibleContent(group) {
      return group.some(event => !isInternalEvent(event));
    }
    function hasPendingApproval(group) {
      const requested = new Set();
      for (const event of group) {
        if (event.kind !== "approval_requested") continue;
        const key = approvalRequestKey(event);
        if (!key) return true;
        requested.add(key);
      }
      if (!requested.size) return false;
      const resolved = new Set();
      for (const event of loadedEvents) {
        if (event.kind !== "approval_resolved") continue;
        const key = approvalRequestKey(event);
        if (key) resolved.add(key);
      }
      for (const key of requested) {
        if (!resolved.has(key)) return true;
      }
      return false;
    }
    function approvalRequestKey(event) {
      const payload = (event && event.payload) || {};
      return String(payload.codexRequestId || payload.requestId || payload.itemId || payload.item_id || "");
    }
    function eventFoldTurnId(event) {
      const payload = (event && event.payload) || {};
      return String(payload.native_turn_id || payload.turnId || "");
    }
    function turnFoldTitle(group) {
      return `${foldMessageCount(group)} 条以前的消息`;
    }
    function foldMessageCount(group) {
      const keys = new Set();
      for (const event of group) {
        if (isInternalEvent(event)) continue;
        const key = foldMessageKey(event);
        if (key) keys.add(key);
      }
      return Math.max(keys.size, group.length ? 1 : 0);
    }
    function foldMessageKey(event) {
      const payload = (event && event.payload) || {};
      const turnId = payload.native_turn_id || payload.turnId || "";
      const itemId = payload.itemId || payload.item_id || payload.codexRequestId || "";
      if (itemId) return `${event.kind}:${itemId}`;
      if (event.kind === "user_message") return `user:${turnId}:user`;
      if (event.kind === "text_delta") return `assistant:${turnId}:assistant`;
      if (event.kind === "message_completed") return `assistant:${turnId}:completed`;
      if (event.kind === "reasoning_delta") return `reasoning:${turnId}:reasoning`;
      if (
        event.kind === "command_started" ||
        event.kind === "command_output" ||
        event.kind === "command_completed" ||
        event.kind === "command_failed"
      ) {
        return `command:${turnId}:${payload.command || event.id || ""}`;
      }
      return `${event.kind}:${event.id || ""}`;
    }
    function isFailedEvent(event) {
      const payload = event.payload || {};
      return event.kind === "failed" || isFailedStatus(payload.status);
    }
    function isFailedStatus(status) {
      return ["failed", "error", "cancelled", "canceled", "interrupted", "aborted"].includes(
        String(status || "").trim().toLowerCase()
      );
    }
    function setConnectionState(value) {
      state.textContent = value;
      header.classList.remove("connected", "reconnecting");
      header.classList.add(value);
    }
    function render(event, options = {}) {
      if (isInternalEvent(event)) return;
      const payload = event.payload || {};
      if (payload.native_thread_id) nativeThreadId = payload.native_thread_id;
      applyNativeTurnState(event, options);
      updateComposerDisabled();
      cursor.textContent = event.id ? "#" + event.id : "";
      if (empty && empty.isConnected) empty.remove();
      const previousTarget = renderTarget;
      renderTarget = options.target || renderTarget || events;
      try {
      if (event.kind === "user_message") renderTranscript(event, "user", "你");
      else if (event.kind === "text_delta" || event.kind === "message_completed") renderAssistant(event);
      else if (event.kind === "reasoning_delta") renderStatusEvent(event, "思考中", "busy");
      else if (isCommandEvent(event)) renderToolCall(event);
      else if (event.kind === "diff_updated" || event.kind === "file_changed") renderFileChange(event);
      else if (event.kind === "approval_requested") renderApproval(event);
      else if (event.kind === "approval_resolved") {
        markApprovalResolved(event);
        renderStatusEvent(event, "审批已处理", "done");
      }
      else if (shouldRenderStatusEvent(event)) renderStatusEvent(event, statusText(event, payload), statusTone(event));
      else updateRunState(statusTitle(event, statusText(event, payload)), statusTone(event));
      } finally {
        renderTarget = previousTarget;
      }
      if (options.scroll !== false) window.scrollTo(0, document.body.scrollHeight);
    }
    function renderAssistant(event) {
      renderTranscript(event, "assistant", PROVIDER_LABEL);
    }
    function renderCommand(event) {
      renderToolCall(event);
    }
    function handleHiddenNativeFeedback(event) {
      const payload = event.payload || {};
      if (payload.native_thread_id) nativeThreadId = payload.native_thread_id;
      applyNativeTurnState(event);
      updateComposerDisabled();
      if (isNativeExecutionDetail(event)) {
        updateRunState(
          nativeExecutionStatus(event),
          event.kind === "command_failed" ? "failed" : "busy"
        );
      } else if (isNativeReasoningDetail(event)) {
        updateRunState("思考中", "busy");
      } else if (isNativeActivityDetail(event)) {
        updateRunState(statusText(event, payload) || statusTitle(event, "处理中"), statusTone(event));
      }
      if (event.id) cursor.textContent = "#" + event.id;
    }
    function renderTranscript(event, role, label) {
      const payload = event.payload || {};
      const key = transcriptKey(event, role);
      let node = transcriptNodes.get(key);
      if (!node) {
        const row = document.createElement("article");
        row.className = "transcript-item " + role;
        const meta = document.createElement("div");
        meta.className = "transcript-meta";
        meta.textContent = label;
        const body = document.createElement("div");
        body.className = "transcript-body";
        row.append(meta, body);
        renderTarget.append(row);
        node = {row, body, text: ""};
        transcriptNodes.set(key, node);
      }
      const incomingText = payload.text || payload.delta || payload.summary || "";
      if (role.includes("assistant")) {
        if (event.kind === "message_completed") {
          node.text = String(incomingText);
          node.row.dataset.completed = "true";
        } else {
          node.text += String(incomingText);
        }
        renderMarkdownLite(node.body, node.text);
      } else {
        renderTranscriptImages(node.body, payload.images || []);
        appendText(node.body, incomingText);
      }
    }
    function renderTranscriptImages(target, images) {
      if (!Array.isArray(images) || !images.length || target.querySelector(".transcript-images")) return;
      const wrap = document.createElement("div");
      wrap.className = "transcript-images";
      for (const image of images) {
        const preview = document.createElement("img");
        preview.className = "transcript-image";
        preview.src = image.url || image.data_url || "";
        preview.alt = image.filename || "image";
        wrap.append(preview);
      }
      target.prepend(wrap);
    }
    function renderStatusEvent(event, fallback, tone) {
      const payload = event.payload || {};
      const text = payload.delta || payload.text || payload.summary || fallback || "";
      const key = statusKey(event);
      let node = statusNodes.get(key);
      if (!node) {
        const row = document.createElement("div");
        row.className = "status-event " + (tone || "neutral");
        const body = document.createElement("div");
        const title = document.createElement("span");
        title.className = "status-title";
        title.textContent = statusTitle(event, fallback);
        const detail = document.createElement("span");
        detail.className = "status-detail";
        body.append(title, detail);
        row.append(body);
        renderTarget.append(row);
        node = {row, title, detail};
        statusNodes.set(key, node);
      }
      node.row.className = "status-event " + (tone || "neutral");
      appendText(node.detail, text);
      updateRunState(statusTitle(event, fallback), tone || "neutral");
    }
    function renderToolCall(event) {
      const payload = event.payload || {};
      const key = payload.itemId || `${payload.native_turn_id || ""}:command:${event.id}`;
      let node = commandNodes.get(key);
      if (!node) {
        const wrap = document.createElement("div");
        wrap.className = "tool-item";
        const card = document.createElement("section");
        card.className = "codex-tool-call";
        const head = document.createElement("div");
        head.className = "tool-head";
        const title = document.createElement("span");
        title.className = "tool-title";
        const status = document.createElement("span");
        status.className = "tool-state";
        const output = document.createElement("pre");
        output.className = "tool-output";
        head.append(title, status);
        card.append(head, output);
        wrap.append(card);
        renderTarget.append(wrap);
        node = {card, title, status, output};
        commandNodes.set(key, node);
      }
      node.title.textContent = commandTitle(payload);
      node.status.textContent = commandStatus(event.kind);
      node.card.classList.toggle("failed", event.kind === "command_failed");
      appendText(node.output, payload.delta || payload.output || "");
      updateRunState(commandStatus(event.kind), event.kind === "command_failed" ? "failed" : "busy");
    }
    function renderFileChange(event) {
      const payload = event.payload || {};
      const row = document.createElement("div");
      row.className = "file-change-item";
      const card = document.createElement("section");
      card.className = "file-change-card";
      const head = document.createElement("div");
      head.className = "file-head";
      const title = document.createElement("span");
      title.className = "file-title";
      title.textContent = payload.filePath || payload.path || "文件变更";
      const state = document.createElement("span");
      state.className = "file-state";
      state.textContent = event.kind === "diff_updated" ? "diff" : "file";
      const body = document.createElement("pre");
      body.className = "file-body";
      body.textContent = payload.patch || payload.diff || payload.delta || payload.filePath || "";
      head.append(title, state);
      card.append(head, body);
      row.append(card);
      renderTarget.append(row);
    }
    function renderApproval(event) {
      const payload = event.payload || {};
      const row = document.createElement("div");
      row.className = "approval-item";
      const card = document.createElement("section");
      card.className = "approval-card";
      card.dataset.requestId = payload.codexRequestId || "";
      const head = document.createElement("div");
      head.className = "approval-head";
      const title = document.createElement("span");
      title.className = "approval-title";
      title.textContent = "需要审批";
      head.append(title);
      const body = document.createElement("div");
      body.className = "approval-body";
      body.textContent = payload.summary || payload.command || payload.kind || "Approval required";
      const resolution = document.createElement("div");
      resolution.className = "approval-state";
      resolution.textContent = "等待你的确认";
      const actions = document.createElement("div");
      actions.className = "approval-actions";
      for (const [label, action, tone] of [
        ["批准一次", "approve_once", "approve"],
        ["本会话批准", "approve_session", "approve"],
        ["拒绝", "deny", "danger"],
        ["取消", "cancel", "danger"]
      ]) {
        const button = document.createElement("button");
        button.textContent = label;
        button.dataset.action = action;
        button.className = `approval-action ${tone}`;
        button.onclick = () => resolveApproval(payload.codexRequestId, action, card);
        actions.append(button);
      }
      card.append(head, body, resolution, actions);
      row.append(card);
      renderTarget.append(row);
      updateRunState("等待审批", "busy");
    }
    function markApprovalResolved(event) {
      const key = approvalRequestKey(event);
      if (!key) return;
      for (const card of document.querySelectorAll(".approval-card")) {
        if (card.dataset.requestId === key) {
          const action = approvalResolvedAction(event, card);
          setApprovalState(card, action, "resolved");
        }
      }
    }
    function renderStatus(kind, text) {
      renderStatusEvent(
        {kind, payload: {text: text || ""}},
        text || kind,
        kind === "attach_failed" ? "failed" : "neutral"
      );
    }
    function updateRunState(text, tone) {
      if (!text) return;
      runStateLabel.textContent = text;
      runStatus.className = "codex-status-flow run-state " + (tone || "neutral");
    }
    function transcriptKey(event, role) {
      const payload = event.payload || {};
      if (role.includes("assistant")) return ["assistant", assistantMessageKey(event)].join(":");
      return [role, payload.native_turn_id || "", payload.itemId || role].join(":");
    }
    function assistantMessageKey(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (itemId.startsWith("jsonl-assistant")) return itemId;
      return `${payload.native_turn_id || payload.turnId || ""}:assistant`;
    }
    function statusKey(event) {
      const payload = event.payload || {};
      return ["status", payload.native_turn_id || "", payload.itemId || event.kind].join(":");
    }
    function appendText(node, text) {
      if (!text) return;
      node.append(document.createTextNode(String(text)));
    }
    function renderMarkdownLite(target, text) {
      target.replaceChildren();
      const normalized = String(text || "").replace(/\\r\\n/g, "\\n");
      if (!normalized.trim()) return;
      const lines = normalized.split("\\n");
      let paragraph = [];
      let list = null;
      function flushParagraph() {
        if (!paragraph.length) return;
        const block = document.createElement("p");
        appendInlineMarkdown(block, paragraph.join("\\n").trim());
        target.append(block);
        paragraph = [];
      }
      function flushList() {
        if (!list) return;
        target.append(list.node);
        list = null;
      }
      for (let index = 0; index < lines.length; index++) {
        const line = lines[index];
        if (line.trim().startsWith("```")) {
          flushParagraph();
          flushList();
          const codeLines = [];
          index += 1;
          while (index < lines.length && !lines[index].trim().startsWith("```")) {
            codeLines.push(lines[index]);
            index += 1;
          }
          const pre = document.createElement("pre");
          const code = document.createElement("code");
          code.textContent = codeLines.join("\\n");
          pre.append(code);
          target.append(pre);
          continue;
        }
        if (!line.trim()) {
          flushParagraph();
          flushList();
          continue;
        }
        const heading = line.match(/^#{1,3}\\s+(.+)$/);
        if (heading) {
          flushParagraph();
          flushList();
          const block = document.createElement("h3");
          appendInlineMarkdown(block, heading[1].trim());
          target.append(block);
          continue;
        }
        const unordered = line.match(/^\\s*[-*]\\s+(.+)$/);
        const ordered = line.match(/^\\s*\\d+[.)]\\s+(.+)$/);
        if (unordered || ordered) {
          flushParagraph();
          const type = ordered ? "ol" : "ul";
          if (!list || list.type !== type) {
            flushList();
            list = {type, node: document.createElement(type)};
          }
          const item = document.createElement("li");
          appendInlineMarkdown(item, (unordered || ordered)[1].trim());
          list.node.append(item);
          continue;
        }
        paragraph.push(line);
      }
      flushParagraph();
      flushList();
    }
    function appendInlineMarkdown(target, text) {
      const source = String(text || "");
      const pattern = /(\\[([^\\]]+)\\]\\(([^)]+)\\)|`[^`]+`|\\*\\*[^*]+\\*\\*)/g;
      let cursor = 0;
      for (const match of source.matchAll(pattern)) {
        if (match.index > cursor) {
          target.append(document.createTextNode(source.slice(cursor, match.index)));
        }
        const token = match[0];
        if (token.startsWith("[")) {
          appendMarkdownLink(target, match[2] || "", match[3] || "");
        } else if (token.startsWith("`")) {
          const code = document.createElement("code");
          code.textContent = token.slice(1, -1);
          target.append(code);
        } else {
          const strong = document.createElement("strong");
          strong.textContent = token.slice(2, -2);
          target.append(strong);
        }
        cursor = match.index + token.length;
      }
      if (cursor < source.length) {
        target.append(document.createTextNode(source.slice(cursor)));
      }
    }
    function appendMarkdownLink(target, label, href) {
      const anchor = document.createElement("a");
      anchor.textContent = label || href;
      anchor.href = href || "#";
      if (!String(href || "").startsWith("/")) {
        anchor.target = "_blank";
        anchor.rel = "noreferrer";
      }
      target.append(anchor);
    }
    function commandTitle(payload) {
      if (payload.command) return String(payload.command);
      const item = payload.item || {};
      if (item.command) return String(item.command);
      return "命令执行";
    }
    function commandStatus(kind) {
      if (kind === "command_started") return "运行中";
      if (kind === "command_completed") return "完成";
      if (kind === "command_failed") return "失败";
      return "输出";
    }
    function nativeExecutionStatus(event) {
      if (event.kind === "command_failed") return "执行失败";
      if (event.kind === "command_completed") return `${PROVIDER_LABEL} 正在整理回复`;
      return `${PROVIDER_LABEL} 正在处理`;
    }
    function statusTone(event) {
      if (event.kind === "completed") return "done";
      if (event.kind === "failed") return "failed";
      if (event.kind === "lifecycle") return "busy";
      return "neutral";
    }
    function shouldRenderStatusEvent(event) {
      const payload = event.payload || {};
      if (event.kind === "completed") return false;
      if (event.kind === "lifecycle" && !isFailedStatus(payload.status)) return false;
      return true;
    }
    function statusText(event, payload) {
      if (payload.status) return payload.status;
      if (payload.text) return payload.text;
      if (payload.delta) return payload.delta;
      if (event.type) return event.type;
      return "";
    }
    function statusTitle(event, fallback) {
      const payload = event.payload || {};
      const status = String(payload.status || "").trim().toLowerCase();
      if (event.kind === "lifecycle" && status === "running") return `${PROVIDER_LABEL} 正在回复`;
      if (event.kind === "reasoning_delta") return "Thinking";
      if (event.kind === "completed") return "完成";
      if (event.kind === "failed") return "失败";
      return fallback || event.kind || "状态";
    }
  </script>
</body>
</html>"""
    return (
        template
        .replace("__SAFE_TITLE__", safe_title)
        .replace("__PROVIDER_LABEL_TEXT__", safe_title)
        .replace("__STREAM_PATH__", stream_path)
        .replace("__AGENT_RUN_ID__", str(agent_run_id))
        .replace("__PROVIDER_JSON__", json.dumps(native_provider, ensure_ascii=False))
        .replace("__PROVIDER_LABEL_JSON__", json.dumps(provider_label, ensure_ascii=False))
        .replace("__API_BASE_JSON__", json.dumps(api_base, ensure_ascii=False))
    )


def _legacy_live_page(agent_run_id: int) -> str:
    stream_path = f"/api/workers/{agent_run_id}/stream"
    safe_title = escape(f"Worker Live Stream #{agent_run_id}")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; background: #101114; color: #f4f4f5; }}
    header {{ position: sticky; top: 0; padding: 12px 16px; background: #191b20; border-bottom: 1px solid #30333a; }}
    main {{ padding: 12px 12px 132px; }}
    .event {{ white-space: pre-wrap; border-bottom: 1px solid #30333a; padding: 10px 4px; }}
    .meta {{ color: #a1a1aa; font-size: 12px; margin-bottom: 4px; }}
    .approval_requested {{ color: #facc15; }}
    .failed {{ color: #f87171; }}
    .completed {{ color: #86efac; }}
    .controls {{ position: fixed; left: 0; right: 0; bottom: 0; display: grid; gap: 8px; padding: 10px; background: #101114; border-top: 1px solid #30333a; }}
    .row {{ display: flex; gap: 8px; min-width: 0; }}
    input {{ flex: 1; min-width: 0; border-radius: 8px; border: 1px solid #3f4550; background: #17191f; color: #f4f4f5; padding: 11px; font-size: 15px; }}
    button {{ min-height: 40px; border: 0; border-radius: 8px; padding: 9px 12px; background: #f4f4f5; color: #101114; font-weight: 700; }}
    button.secondary {{ border: 1px solid #3f4550; background: #1b1e25; color: #f4f4f5; }}
    button.warn {{ background: #f87171; color: #1b0707; }}
    .approval-actions {{ display: flex; gap: 8px; margin-top: 8px; }}
  </style>
</head>
<body>
  <header>
    <strong>Worker Live Stream</strong>
    <span id="state">connecting</span>
    <span id="cursor"></span>
  </header>
  <main id="events"></main>
  <section class="controls">
    <div class="row">
      <input id="prompt" placeholder="继续官方 Codex 会话">
      <button id="continue">发送</button>
    </div>
    <div class="row">
      <button class="secondary" id="steer">修正当前轮</button>
      <button class="warn" id="interrupt">中断</button>
    </div>
  </section>
  <script>
    const state = document.getElementById("state");
    const cursor = document.getElementById("cursor");
    const events = document.getElementById("events");
    const params = new URLSearchParams(location.search);
    const token = params.get("token") || "";
    let nativeThreadId = params.get("native_thread_id") || "";
    let nativeTurnId = "";
    const authHeaders = token ? {{"Authorization": "Bearer " + token}} : {{}};
    const promptInput = document.getElementById("prompt");
    const streamPath = token ? "{stream_path}?token=" + encodeURIComponent(token) : "{stream_path}";
    const source = new EventSource(streamPath);
    source.onopen = () => {{ state.textContent = "connected"; }};
    source.onerror = () => {{ state.textContent = "reconnecting"; }};
    source.onmessage = (message) => render(JSON.parse(message.data));
    [
      "lifecycle",
      "activity",
      "user_message",
      "text_delta",
      "reasoning_delta",
      "command_started",
      "command_output",
      "command_completed",
      "command_failed",
      "file_changed",
      "diff_updated",
      "approval_requested",
      "approval_resolved",
      "completed",
      "failed",
      "event"
    ].forEach(kind => {{
      source.addEventListener(kind, message => render(JSON.parse(message.data)));
    }});
    async function api(path, options = {{}}) {{
      const response = await fetch(path, {{
        ...options,
        headers: {{"Content-Type": "application/json", ...authHeaders, ...(options.headers || {{}})}}
      }});
      if (!response.ok) {{
        const body = await response.json().catch(() => ({{}}));
        throw new Error(body.error || response.statusText);
      }}
      return response.json().catch(() => ({{}}));
    }}
    async function nativeControl(action, body) {{
      if (!nativeThreadId) return;
      await api(`/api/native/codex/sessions/${{encodeURIComponent(nativeThreadId)}}/${{action}}`, {{
        method: "POST",
        body: JSON.stringify(body)
      }});
    }}
    async function resolveApproval(requestId, action) {{
      await api(`/api/native/codex/approvals/${{encodeURIComponent(requestId)}}/resolve`, {{
        method: "POST",
        body: JSON.stringify({{action}})
      }});
    }}
    document.getElementById("continue").onclick = () => nativeControl("continue", {{prompt: promptInput.value}});
    document.getElementById("steer").onclick = () => nativeControl("steer", {{prompt: promptInput.value, expected_turn_id: nativeTurnId}});
    document.getElementById("interrupt").onclick = () => nativeControl("interrupt", {{turn_id: nativeTurnId}});
    function render(event) {{
      const payload = event.payload || {{}};
      if (payload.native_thread_id) nativeThreadId = payload.native_thread_id;
      if (payload.native_turn_id) nativeTurnId = payload.native_turn_id;
      cursor.textContent = " last event " + event.id;
      const row = document.createElement("div");
      row.className = "event " + event.kind;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = "#" + event.id + " " + event.kind + " " + event.type;
      const body = document.createElement("div");
      body.textContent = payload.delta || payload.text || payload.summary || JSON.stringify(payload, null, 2);
      row.append(meta, body);
      if (event.kind === "approval_requested" && payload.source_kind === "codex_native" && payload.codexRequestId) {{
        const actions = document.createElement("div");
        actions.className = "approval-actions";
        const approveOnce = document.createElement("button");
        approveOnce.textContent = "批准一次";
        approveOnce.onclick = () => resolveApproval(payload.codexRequestId, "approve_once");
        const approveSession = document.createElement("button");
        approveSession.textContent = "本会话批准";
        approveSession.onclick = () => resolveApproval(payload.codexRequestId, "approve_session");
        const deny = document.createElement("button");
        deny.className = "secondary";
        deny.textContent = "拒绝";
        deny.onclick = () => resolveApproval(payload.codexRequestId, "deny");
        const cancel = document.createElement("button");
        cancel.className = "secondary";
        cancel.textContent = "取消";
        cancel.onclick = () => resolveApproval(payload.codexRequestId, "cancel");
        actions.append(approveOnce, approveSession, deny, cancel);
        row.append(actions);
      }}
      events.append(row);
      window.scrollTo(0, document.body.scrollHeight);
    }}
  </script>
</body>
</html>"""
