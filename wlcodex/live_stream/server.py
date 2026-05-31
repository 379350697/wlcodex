from __future__ import annotations

import asyncio
import hmac
import json
from html import escape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from wlcodex.auto_digest_llm import DigestClient
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
        self._access_token = access_token
        self._allow_unauthenticated_loopback = allow_unauthenticated_loopback
        self._turn_summary_config = turn_summary_config or LiveTurnSummaryConfig.from_env()
        self._turn_summary_client = turn_summary_client
        self._native_transcript_mirror = native_transcript_mirror
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()

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
                    await self._send_json(writer, 401, {"error": "unauthorized"})
                    return
                await self._send_html(writer, 200, _native_codex_page())
                return

            if parsed.path.startswith("/api/native/codex"):
                await self._handle_native_route(
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
                await self._send_html(writer, 200, _live_page(agent_id))
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

    async def _handle_native_route(
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
            require_token=self._native_controller is not None,
        ):
            await self._send_json(writer, 401, {"error": "unauthorized"})
            return
        if self._native_controller is None:
            await self._send_json(writer, 503, {"error": "native controller unavailable"})
            return

        base = "/api/native/codex"
        if path == f"{base}/status":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            status = await self._native_controller.status()
            await self._send_json(writer, 200, _json_object(status))
            return

        if path == f"{base}/sessions":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            sessions = await self._native_controller.list_sessions()
            await self._send_json(
                writer,
                200,
                {"sessions": [_json_object(session) for session in sessions]},
            )
            return

        approval_prefix = f"{base}/approvals/"
        if path.startswith(approval_prefix):
            parts = [unquote(part) for part in path[len(approval_prefix) :].split("/") if part]
            if len(parts) == 2 and parts[1] == "resolve" and method == "POST":
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                try:
                    result = await self._native_controller.resolve_approval(parts[0], body)
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

        session_prefix = f"{base}/sessions/"
        if not path.startswith(session_prefix):
            await self._send_json(writer, 404, {"error": "not found"})
            return
        remainder = path[len(session_prefix) :]
        parts = [unquote(part) for part in remainder.split("/") if part]
        if not parts:
            await self._send_json(writer, 404, {"error": "not found"})
            return
        thread_id = parts[0]
        action = parts[1] if len(parts) > 1 else ""

        if method == "GET" and action == "" and len(parts) == 1:
            session = await self._native_controller.read_session(thread_id)
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "attach" and len(parts) == 2:
            session = await self._native_controller.attach_session(thread_id)
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "sync" and len(parts) == 2:
            session = await self._native_controller.sync_session(thread_id)
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "continue" and len(parts) == 2:
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            result = await self._native_controller.continue_session(
                thread_id,
                str(body.get("prompt", "")),
                model=_optional_nonempty_string(body.get("model")),
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
            result = await self._native_controller.steer_session(
                thread_id,
                expected_turn_id,
                str(body.get("prompt", "")),
                model=_optional_nonempty_string(body.get("model")),
                images=_safe_image_attachments(body.get("images")),
            )
            await self._send_json(writer, 200, _json_object(result))
            return
        if method == "POST" and action == "interrupt" and len(parts) == 2:
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            result = await self._native_controller.interrupt_session(
                thread_id,
                str(body.get("turn_id", "")),
            )
            await self._send_json(writer, 200, _json_object(result))
            return
        await self._send_json(writer, 404, {"error": "not found"})

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
        if require_token and not self._access_token:
            return False
        if self._access_token:
            prefix = "Bearer "
            authorization = headers.get("authorization", "")
            if authorization.startswith(prefix):
                candidate = authorization[len(prefix) :]
                if hmac.compare_digest(candidate, self._access_token):
                    return True
            query_token = query.get("token", [""])[0]
            return hmac.compare_digest(query_token, self._access_token)
        return self._allow_unauthenticated_loopback and _is_loopback_peer(writer)

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
) -> None:
    reason = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        431: "Request Header Fields Too Large",
        503: "Service Unavailable",
    }.get(status, "Error")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
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


def _native_codex_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex</title>
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
    main { padding: 8px 26px 124px; }
    .nav-row, .project, .recent { display: grid; grid-template-columns: 38px 1fr auto; align-items: center; min-height: 62px; color: #f7f7f8; background: transparent; border: 0; width: 100%; padding: 0; text-align: left; }
    .icon-folder, .icon-chat { width: 30px; height: 24px; border: 3px solid #f7f7f8; border-radius: 4px; position: relative; }
    .icon-folder:before { content: ""; position: absolute; left: 2px; top: -9px; width: 15px; height: 8px; border: 3px solid #f7f7f8; border-bottom: 0; border-radius: 4px 4px 0 0; background: #000; }
    .icon-chat { width: 28px; height: 28px; border-radius: 50%; }
    .icon-chat:after { content: ""; position: absolute; right: 2px; bottom: 1px; width: 7px; height: 7px; border-right: 3px solid #f7f7f8; border-bottom: 3px solid #f7f7f8; transform: rotate(28deg); background: #000; }
    .label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 18px; font-weight: 650; }
    .section-title { margin: 26px 0 12px; color: #c8c8d0; font-size: 15px; }
    .recent { grid-template-columns: 1fr auto; gap: 14px; min-height: 54px; }
    .recent.active .label { color: #fff; }
    .time { color: #a9a9b2; font-size: 14px; white-space: nowrap; }
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
      <h1>Codex</h1>
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
    <div class="section-title">最近</div>
    <div id="sessions"></div>
  </main>
  <section class="controls">
    <input id="prompt" placeholder="搜索聊天或继续当前会话">
    <button class="chat" id="send">聊天</button>
  </section>
  <script>
    const token = new URLSearchParams(location.search).get("token") || "";
    const headers = token ? {"Authorization": "Bearer " + token} : {};
    let selected = null;
    let sessions = [];
    const devicesEl = document.getElementById("devices");
    const sessionsEl = document.getElementById("sessions");
    const projectsEl = document.getElementById("projects");
    const promptEl = document.getElementById("prompt");

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
        const status = await api("/api/native/codex/status");
        const name = status.server_name || "wanglindeMac-mini.local";
        devicesEl.innerHTML = `<button class="device-chip${status.connected ? "" : " off"}"><span class="dot"></span><span class="laptop"></span><span>${escapeHtml(name)}</span></button>`;
      } catch (error) {
        devicesEl.innerHTML = `<button class="device-chip off"><span class="dot"></span><span class="laptop"></span><span>${escapeHtml(error.message)}</span></button>`;
      }
    }

    async function loadSessions() {
      try {
        const data = await api("/api/native/codex/sessions");
        sessions = data.sessions || [];
        if (!selected && sessions.length) selected = sessions[0];
        renderProjects();
        renderSessions();
      } catch (error) {
        sessionsEl.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      }
    }

    function renderProjects() {
      const seen = new Set();
      projectsEl.innerHTML = "";
      for (const session of sessions) {
        const cwd = session.cwd || "";
        if (!cwd || seen.has(cwd)) continue;
        seen.add(cwd);
        const btn = document.createElement("button");
        btn.className = "project";
        btn.innerHTML = `<span class="icon-folder"></span><span class="label">${escapeHtml(lastPath(cwd))}</span><span></span>`;
        btn.onclick = () => {
          selected = session;
          renderSessions();
        };
        projectsEl.appendChild(btn);
        if (seen.size >= 4) break;
      }
    }

    function renderSessions() {
      const needle = promptEl.value.trim().toLowerCase();
      const filtered = sessions.filter(session => {
        if (!needle) return true;
        return `${session.title || ""} ${session.cwd || ""}`.toLowerCase().includes(needle);
      });
      sessionsEl.innerHTML = "";
      if (!filtered.length) {
        sessionsEl.innerHTML = `<div class="empty">没有匹配的聊天</div>`;
        return;
      }
      for (const session of filtered.slice(0, 20)) {
        const btn = document.createElement("button");
        btn.className = "recent" + (selected && selected.native_thread_id === session.native_thread_id ? " active" : "");
        btn.innerHTML = `<span><span class="label">${escapeHtml(session.title || session.native_thread_id)}</span><span class="meta">${escapeHtml(lastPath(session.cwd || ""))} · ${escapeHtml(session.status || "")}</span></span><span class="time">${escapeHtml(relativeTime(session.updated_at))}</span>`;
        btn.onclick = () => {
          selected = session;
          openLive();
        };
        sessionsEl.appendChild(btn);
      }
    }

    async function control(action, body) {
      if (!selected) return;
      await api(`/api/native/codex/sessions/${encodeURIComponent(selected.native_thread_id)}/${action}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      await loadSessions();
    }

    async function openLive() {
      if (!selected) return;
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      params.set("native_thread_id", selected.native_thread_id);
      location.href = `/workers/${selected.agent_run_id}/live?${params.toString()}`;
    }
    document.getElementById("send").onclick = async () => {
      if (!selected && sessions.length) selected = sessions[0];
      if (promptEl.value.trim()) await control("continue", {prompt: promptEl.value});
      await openLive();
    };
    document.getElementById("chat").onclick = openLive;
    document.getElementById("back").onclick = () => history.back();
    promptEl.addEventListener("input", renderSessions);
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function lastPath(path) {
      const parts = String(path).split("/").filter(Boolean);
      return parts[parts.length - 1] || path || "Codex";
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
    loadSessions();
    setInterval(loadSessions, 3000);
  </script>
</body>
</html>"""


def _live_page(agent_run_id: int) -> str:
    stream_path = f"/api/workers/{agent_run_id}/stream"
    safe_title = escape("Codex")
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
    .transcript-item.user { justify-self: end; justify-items: end; max-width: min(82%, 520px); }
    .transcript-item.user .transcript-meta { display: none; }
    .transcript-item.user .transcript-body { padding: 10px 13px; border: 1px solid #333842; border-radius: 18px 18px 4px 18px; background: #20242d; line-height: 1.5; }
    .transcript-item.assistant { justify-self: start; max-width: 100%; padding-left: 22px; border-left: 2px solid #30333a; }
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
    .turn-fold summary { display: flex; gap: 6px; align-items: center; min-height: 42px; padding: 0 0 8px; cursor: pointer; list-style: none; color: #d7dae1; }
    .turn-fold summary::-webkit-details-marker { display: none; }
    .turn-fold-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 16px; }
    .turn-fold-chevron { color: #aeb4bf; font-size: 18px; transition: transform .16s ease; }
    .turn-fold[open] .turn-fold-chevron { transform: rotate(90deg); }
    .turn-fold-body { display: grid; gap: 18px; padding: 12px 0 18px; }
    .codex-tool-call, .file-change-card, .approval-card { border: 1px solid #30333a; background: #0f1014; border-radius: 10px; overflow: hidden; }
    .codex-tool-call.failed { border-color: #7f1d1d; }
    .tool-head, .file-head, .approval-head { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 11px 12px; border-bottom: 1px solid #26282f; }
    .tool-title, .file-title, .approval-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #f4f4f5; font-size: 14px; font-weight: 720; }
    .tool-state, .file-state { color: #9ca3af; font-size: 12px; }
    .tool-output, .file-body { margin: 0; max-height: 260px; overflow: auto; padding: 11px 12px; color: #d8dee9; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.45; }
    .approval-card { border-color: #854d0e; background: #171107; }
    .approval-body { padding: 0 12px 12px; color: #fde68a; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.5; }
    .approval-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 0 12px 12px; }
    .codex-input-dock { position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; display: grid; gap: 8px; padding: 10px 16px 16px; background: linear-gradient(to top, #000 86%, rgba(0,0,0,0)); border-top: 1px solid #272930; }
    .composer-tools { display: flex; gap: 8px; align-items: center; min-width: 0; }
    .model-selector { flex: 1; min-width: 0; height: 38px; border-radius: 11px; border: 1px solid #3f4550; background: #11141b; color: #f4f4f5; padding: 0 12px; font-size: 14px; font-weight: 700; }
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
        <h1>Codex</h1>
        <div class="subtitle"><span class="status-dot"></span><span id="state">connecting</span></div>
      </div>
      <button class="circle" aria-label="菜单">⋮</button>
    </header>
    <main>
      <section class="codex-status-flow run-state" id="runStatus">
        <span class="run-pulse"></span>
        <span id="runStateLabel">连接官方 Codex 会话</span>
        <span class="event-cursor" id="cursor"></span>
      </section>
      <button class="history-fold" id="historyFold" hidden>更早的消息</button>
      <section class="codex-transcript" id="events"><div class="empty" id="empty">等待官方 Codex 转录</div></section>
      <div class="composer-activity-dot" id="composerActivityDot" aria-hidden="true"></div>
    </main>
    <section class="codex-input-dock">
      <div class="composer-tools">
        <select id="modelSelector" class="model-selector" aria-label="选择模型">
          <option value="gpt-5.5" selected>GPT-5.5</option>
          <option value="gpt-5.1">GPT-5.1</option>
          <option value="gpt-5">GPT-5</option>
          <option value="gpt-5-codex">GPT-5 Codex</option>
        </select>
        <button class="attach-button" id="attachmentButton" type="button" aria-label="上传照片">＋</button>
        <input id="imageInput" type="file" accept="image/*" multiple hidden>
        <span class="send-status" id="sendStatus"></span>
      </div>
      <div class="attachment-strip" id="attachmentStrip" hidden></div>
      <div class="dock-row">
        <input id="prompt" placeholder="继续官方 Codex 会话">
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
    const modelSelector = document.getElementById("modelSelector");
    const attachmentButton = document.getElementById("attachmentButton");
    const imageInput = document.getElementById("imageInput");
    const attachmentStrip = document.getElementById("attachmentStrip");
    const sendStatus = document.getElementById("sendStatus");
    const streamPathBase = "__STREAM_PATH__";
    const agentRunId = __AGENT_RUN_ID__;
    const RECENT_EVENT_LIMIT = 80;
    const OLDER_EVENT_LIMIT = 80;
    const transcriptNodes = new Map();
    const statusNodes = new Map();
    const commandNodes = new Map();
    let renderTarget = events;
    let imageAttachments = [];
    let sendingPrompt = false;
    let nativeTurnRunning = false;
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
      if (!nativeThreadId) throw new Error("官方 Codex 会话未连接");
      return api(`/api/native/codex/sessions/${encodeURIComponent(nativeThreadId)}/${action}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
    }
    async function resolveApproval(requestId, action) {
      await api(`/api/native/codex/approvals/${encodeURIComponent(requestId)}/resolve`, {
        method: "POST",
        body: JSON.stringify({action})
      });
    }
    continueButton.onclick = () => submitPrompt();
    steerButton.onclick = () => submitPrompt("steer");
    interruptButton.onclick = interruptNativeTurn;
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
      location.href = "/native/codex" + query;
    };
    async function submitPrompt(action = primaryComposerAction()) {
      if (action === "interrupt") {
        await interruptNativeTurn();
        return;
      }
      const prompt = promptInput.value;
      if (!prompt.trim() && imageAttachments.length === 0) {
        setSendStatus("请输入内容或照片", "error");
        return;
      }
      const body = {
        prompt,
        model: modelSelector.value || "gpt-5.5"
      };
      if (imageAttachments.length) {
        body.images = imageAttachments.map(image => ({
          url: image.url,
          filename: image.filename,
          mime_type: image.mime_type
        }));
      }
      if (action === "steer") body.expected_turn_id = activeTurnId;
      sendingPrompt = true;
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
      if (nativeTurnRunning) return "steer";
      return "continue";
    }
    function applyNativeTurnState(event, options = {}) {
      const payload = event.payload || {};
      const mirroredTranscript = isMirroredTranscriptEvent(event);
      if (!options.historical && !mirroredTranscript && payload.native_turn_id) nativeTurnId = payload.native_turn_id;
      if (options.historical || mirroredTranscript) return;
      if (event.kind === "completed" || event.kind === "failed") {
        if (!payload.native_turn_id || payload.native_turn_id === activeTurnId) activeTurnId = "";
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
      interruptButton.disabled = sendingPrompt || !nativeThreadId || !activeTurnId;
      setComposerActivity(nativeTurnRunning || sendingPrompt);
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
    async function attachNative() {
      if (!nativeThreadId || attached) return;
      attached = true;
      try {
        const result = await api(`/api/native/codex/sessions/${encodeURIComponent(nativeThreadId)}/attach`, {
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
    attachNative().then(() => {
      return loadRecentEvents();
    }).catch(() => {
      return loadRecentEvents();
    }).then(() => {
      setInterval(pollEvents, 1000);
    });
    async function loadRecentEvents() {
      let snapshot = await api(eventsPath("tail=" + RECENT_EVENT_LIMIT, {currentTurn: true}));
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
        if (event.type === "model.usage.updated") return false;
        return event.kind !== "event";
      });
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
      if (duplicateDisplayEvent) {
        applyNativeTurnState(event);
        updateComposerDisabled();
        if (event.id) cursor.textContent = "#" + event.id;
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
      const seen = new Set();
      const result = [];
      for (const event of sourceEvents) {
        const key = mirroredDisplayKey(event);
        if (key) {
          if (seen.has(key)) continue;
          seen.add(key);
        }
        result.push(event);
      }
      return result;
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
      const title = document.createElement("span");
      title.className = "turn-fold-title";
      title.textContent = turnFoldTitle(group);
      const chevron = document.createElement("span");
      chevron.className = "turn-fold-chevron";
      chevron.textContent = "›";
      head.append(title, chevron);
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
      return group.some(event => event.type !== "model.usage.updated");
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
        if (event.type === "model.usage.updated") continue;
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
      return ["failed", "error", "cancelled", "canceled"].includes(
        String(status || "").trim().toLowerCase()
      );
    }
    function setConnectionState(value) {
      state.textContent = value;
      header.classList.remove("connected", "reconnecting");
      header.classList.add(value);
    }
    function render(event, options = {}) {
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
      else if (event.kind === "text_delta") renderAssistant(event);
      else if (event.kind === "reasoning_delta") renderStatusEvent(event, "思考中", "busy");
      else if (event.kind === "command_started" || event.kind === "command_output" || event.kind === "command_completed" || event.kind === "command_failed") renderToolCall(event);
      else if (event.kind === "diff_updated" || event.kind === "file_changed") renderFileChange(event);
      else if (event.kind === "approval_requested") renderApproval(event);
      else if (event.kind === "approval_resolved") renderStatusEvent(event, "审批已处理", "done");
      else renderStatusEvent(event, statusText(event, payload), statusTone(event));
      } finally {
        renderTarget = previousTarget;
      }
      if (options.scroll !== false) window.scrollTo(0, document.body.scrollHeight);
    }
    function renderAssistant(event) {
      renderTranscript(event, "assistant", "Codex");
    }
    function renderCommand(event) {
      renderToolCall(event);
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
        node = {row, body};
        transcriptNodes.set(key, node);
      }
      appendText(node.body, payload.text || payload.delta || payload.summary || "");
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
      const head = document.createElement("div");
      head.className = "approval-head";
      const title = document.createElement("span");
      title.className = "approval-title";
      title.textContent = "需要审批";
      head.append(title);
      const body = document.createElement("div");
      body.className = "approval-body";
      body.textContent = payload.summary || payload.command || payload.kind || "Approval required";
      const actions = document.createElement("div");
      actions.className = "approval-actions";
      for (const [label, action, cls] of [
        ["批准一次", "approve_once", ""],
        ["本会话批准", "approve_session", ""],
        ["拒绝", "deny", "secondary"],
        ["取消", "cancel", "secondary"]
      ]) {
        const button = document.createElement("button");
        button.textContent = label;
        if (cls) button.className = cls;
        button.onclick = () => resolveApproval(payload.codexRequestId, action);
        actions.append(button);
      }
      card.append(head, body, actions);
      row.append(card);
      renderTarget.append(row);
      updateRunState("等待审批", "busy");
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
      return [role, payload.native_turn_id || "", payload.itemId || role].join(":");
    }
    function statusKey(event) {
      const payload = event.payload || {};
      return ["status", payload.native_turn_id || "", payload.itemId || event.kind].join(":");
    }
    function appendText(node, text) {
      if (!text) return;
      node.textContent += String(text);
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
    function statusTitle(event, fallback) {
      if (event.kind === "reasoning_delta") return "Thinking";
      if (event.kind === "completed") return "完成";
      if (event.kind === "failed") return "失败";
      return fallback || event.kind || "状态";
    }
    function statusTone(event) {
      if (event.kind === "completed") return "done";
      if (event.kind === "failed") return "failed";
      return "neutral";
    }
    function statusText(event, payload) {
      if (payload.status) return payload.status;
      if (payload.text) return payload.text;
      if (payload.delta) return payload.delta;
      if (event.type) return event.type;
      return "";
    }
  </script>
</body>
</html>"""
    return (
        template
        .replace("__SAFE_TITLE__", safe_title)
        .replace("__STREAM_PATH__", stream_path)
        .replace("__AGENT_RUN_ID__", str(agent_run_id))
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
