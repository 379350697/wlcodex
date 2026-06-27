from __future__ import annotations

import asyncio
import base64
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4

from wlcodex.auto_digest_llm import DigestClient
from wlcodex.claude_permissions import (
    CLAUDE_PERMISSION_MODE_DESCRIPTIONS,
    CLAUDE_PERMISSION_MODE_LABELS,
    CLAUDE_PERMISSION_MODE_ORDER,
    normalize_claude_permission_mode,
)
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
from wlcodex.jsonrpc import JsonRpcError, JsonRpcTimeout
from wlcodex.relay.envelopes import parse_role_envelope
from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES, RELAY_ROLE_IDS


_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_HEADER_BYTES = 16 * 1024
_MAX_BODY_BYTES = 24 * 1024 * 1024
_MAX_NATIVE_IMAGE_ATTACHMENTS = 8
_MAX_PLUGIN_ICON_BYTES = 128 * 1024
_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS = 0.05
_NATIVE_SESSION_WATCH_INTERVAL_SECONDS = 1.0
_NATIVE_TRANSCRIPT_WATCH_INTERVAL_SECONDS = 0.5
_CODEX_PERMISSION_PRESETS: dict[str, dict[str, object]] = {
    "default": {},
    "read_only": {
        "approval_policy": "on-request",
        "sandbox": "read-only",
        "sandbox_policy": {"type": "readOnly", "networkAccess": False},
    },
    "on_request": {"approval_policy": "on-request"},
    "auto_review": {
        "approval_policy": "on-request",
        "approvals_reviewer": "auto_review",
    },
    "never": {"approval_policy": "never"},
    "full_access": {
        "approval_policy": "never",
        "sandbox": "danger-full-access",
        "sandbox_policy": {"type": "dangerFullAccess"},
    },
}
_CODEX_PERMISSION_PRESETS_UI = [
    {"value": "default", "label": "默认权限", "description": "在沙盒中运行命令"},
    {
        "value": "auto_review",
        "label": "自动审核",
        "description": "自动审查提权请求",
    },
    {"value": "read_only", "label": "只读", "description": "编辑文件或运行命令需要批准"},
    {
        "value": "full_access",
        "label": "完全访问权限",
        "description": "完全访问计算机（风险较高）",
    },
]
_CLAUDE_PERMISSION_PRESETS = [
    {
        "value": mode,
        "label": CLAUDE_PERMISSION_MODE_LABELS[mode],
        "description": CLAUDE_PERMISSION_MODE_DESCRIPTIONS[mode],
    }
    for mode in CLAUDE_PERMISSION_MODE_ORDER
]
_ANTIGRAVITY_PERMISSION_PRESETS = [
    {
        "value": "default",
        "label": "默认",
        "description": "按默认权限提示执行命令。",
        "dangerously_skip_permissions": False,
        "sandbox": False,
    },
    {
        "value": "sandbox",
        "label": "沙盒",
        "description": "在沙盒环境中运行，仍保留权限确认。",
        "dangerously_skip_permissions": False,
        "sandbox": True,
    },
    {
        "value": "skip_permissions",
        "label": "跳过权限",
        "description": "跳过权限提示直接执行，风险较高。",
        "dangerously_skip_permissions": True,
        "sandbox": False,
    },
    {
        "value": "skip_permissions_sandbox",
        "label": "沙盒 + 跳过权限",
        "description": "沙盒运行且跳过权限提示，适合高度受控环境。",
        "dangerously_skip_permissions": True,
        "sandbox": True,
    },
]
_ANTIGRAVITY_PERMISSION_PRESET_MAP = {
    preset["value"]: preset for preset in _ANTIGRAVITY_PERMISSION_PRESETS
}
_LOGIN_TICKET_TTL_SECONDS = 5 * 60
_LOGIN_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_COUNCIL_PROJECTS_ROOT = Path.home() / "projects"
_STATIC_ASSET_DIR = Path(__file__).with_name("static")
_STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_RELAY_MARVIS_CSS_HREF = "/static/relay_marvis.css?v=20260628-s25-header-icons"
_RELAY_ACTIVITY_DISPLAY_TZ = timezone(timedelta(hours=8))

_NATIVE_APP_HEAD = """  <link rel="manifest" href="/native/manifest.webmanifest">
  <meta name="theme-color" content="#000000">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="WLCodex">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">"""

# Inline SVG icons — 24×24 viewBox, currentColor, Lucide-style (stroke-width 2)
_ICON_ATTRS = 'width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
_ICON_SVG = {
    # Navigation
    "back": f'<svg {_ICON_ATTRS}><path d="M15 18l-6-6 6-6"/></svg>',
    "chevron": f'<svg {_ICON_ATTRS}><path d="M9 18l6-6-6-6"/></svg>',
    # Actions
    "menu": '<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg>',
    "attach": f'<svg {_ICON_ATTRS}><path d="M12 5v14"/><path d="M5 12h14"/></svg>',
    "remove": f'<svg {_ICON_ATTRS}><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>',
    "send": f'<svg {_ICON_ATTRS}><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
    "stop": '<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
    "check": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
    "copy": f'<svg {_ICON_ATTRS}><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4c0-1.1.9-2 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    "download": f'<svg {_ICON_ATTRS}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>',
    "plan": f'<svg {_ICON_ATTRS}><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/><path d="M7 9h4"/><path d="M7 15h2"/></svg>',
    "pin": f'<svg {_ICON_ATTRS}><line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14"/><path d="M7 10l5-7 5 7"/><path d="M8 10h8l1 7H7z"/></svg>',
    "pencil": f'<svg {_ICON_ATTRS}><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
    "archive": f'<svg {_ICON_ATTRS}><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>',
    "info": f'<svg {_ICON_ATTRS}><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
    # Extended icon set
    "settings": f'<svg {_ICON_ATTRS}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    "folder": f'<svg {_ICON_ATTRS}><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
    "terminal": f'<svg {_ICON_ATTRS}><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    "zap": f'<svg {_ICON_ATTRS}><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
    "shield": f'<svg {_ICON_ATTRS}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    "eye": f'<svg {_ICON_ATTRS}><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
    "refresh": f'<svg {_ICON_ATTRS}><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
    "clock": f'<svg {_ICON_ATTRS}><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
}

# Unicode → SVG key mapping for HTML template replacements
_UNICODE_ICON_MAP = {
    "‹": _ICON_SVG["back"],
    "⋮": _ICON_SVG["menu"],
    "›": _ICON_SVG["chevron"],
    "＋": _ICON_SVG["attach"],
    "×": _ICON_SVG["remove"],
    "↑": _ICON_SVG["send"],
    "■": _ICON_SVG["stop"],
    "✓": _ICON_SVG["check"],
}

# ICONS object injected into <script> blocks for dynamic JS icon use
_ICONS_JS_LITERAL = (
    "const ICONS={"
    + ",".join(f"{key}:{json.dumps(svg)}" for key, svg in _ICON_SVG.items())
    + "};"
)


def _replace_html_icons(html: str) -> str:
    """Replace Unicode icon characters with inline SVGs in HTML context."""
    result = html
    for char, svg in _UNICODE_ICON_MAP.items():
        result = result.replace(char, svg)
    return result


def _token_suffix(access_token: str = "") -> str:
    token = str(access_token or "")
    return f"?token={quote(token, safe='')}" if token else ""


def _relay_task_detail_view(value: str) -> str:
    return "board" if str(value or "").strip().lower() == "board" else "conversation"


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
        workflow_service: Any = None,
        relay_service: Any = None,
        native_sync_timeout_seconds: float = 3.0,
        native_sessions_timeout_seconds: float = 3.0,
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
        self._workflow_service = workflow_service
        self._relay_service = relay_service
        self._native_sync_timeout_seconds = max(0.1, float(native_sync_timeout_seconds))
        self._native_sessions_timeout_seconds = max(
            0.1,
            float(native_sessions_timeout_seconds),
        )
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._native_background_tasks: dict[tuple[str, ...], asyncio.Task[None]] = {}
        self._native_background_errors: dict[tuple[str, ...], str] = {}
        self._native_session_streams: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._native_session_file_signatures: dict[str, str] = {}
        self._native_transcript_file_signatures: dict[tuple[str, str], str] = {}
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
        tasks = [task for task in self._client_tasks if task is not asyncio.current_task()]
        tasks.extend(
            task
            for task in self._native_background_tasks.values()
            if task is not asyncio.current_task()
        )
        tasks.extend(
            task
            for task in self._council_run_tasks
            if task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

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

            if parsed.path.startswith("/static/"):
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await self._send_static_asset(writer, parsed.path)
                return

            if parsed.path == "/native/manifest.webmanifest":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await _send_response(
                    writer,
                    200,
                    "application/manifest+json; charset=utf-8",
                    _native_app_manifest().encode("utf-8"),
                    extra_headers={
                        "Cache-Control": "public, max-age=300, stale-while-revalidate=60"
                    },
                )
                return

            if parsed.path == "/native/icon.svg":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await _send_response(
                    writer,
                    200,
                    "image/svg+xml; charset=utf-8",
                    _native_app_icon_svg().encode("utf-8"),
                    extra_headers={
                        "Cache-Control": "public, max-age=86400, stale-while-revalidate=3600"
                    },
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

            if parsed.path in (
                "/native/workflows",
                "/native/workflows/relay",
                "/native/workflows/relay/chat",
                "/native/workflows/relay/config",
                "/native/workflows/relay/office",
            ) or parsed.path.startswith("/native/workflows/relay/tasks/"):
                await self._handle_relay_ui_route(
                    writer,
                    method,
                    parsed.path,
                    headers,
                    query,
                )
                return

            native_provider = _native_page_provider_from_path(parsed.path)
            if native_provider:
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await self._send_native_page(writer, native_provider, headers, query)
                return

            if parsed.path.startswith("/api/native/workflows/"):
                await self._handle_workflow_route(
                    reader,
                    writer,
                    method,
                    parsed.path,
                    headers,
                    query,
                )
                return

            if parsed.path.startswith("/api/relay/"):
                await self._handle_relay_route(
                    reader,
                    writer,
                    method,
                    parsed.path,
                    headers,
                    query,
                )
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
                native_provider = _optional_nonempty_string(
                    query.get("native_provider", [""])[0]
                ) or "codex"
                native_provider_key = native_provider.strip().lower() or "codex"
                native_turn_id = _optional_nonempty_string(
                    query.get("native_turn_id", [""])[0]
                ) or ""
                native_sync_error = self._native_background_errors.get(
                    ("native_transcript", native_provider_key, native_thread_id),
                    "",
                )
                native_sync_pending = False
                should_sync_native = bool(native_thread_id) and (
                    "tail" in query or "before" in query
                )
                if should_sync_native:
                    native_sync_pending = self._schedule_native_transcript_sync(
                        native_thread_id,
                        native_provider=native_provider_key,
                    )
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
                        "native_sync_pending": native_sync_pending,
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
                theme = _optional_nonempty_string(
                    query.get("theme", [""])[0]
                ) or ""
                await self._send_html(
                    writer,
                    200,
                    _live_page(agent_id, native_provider=native_provider, theme=theme),
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
                native_thread_id = _optional_nonempty_string(
                    query.get("native_thread_id", [""])[0]
                ) or ""
                native_provider = _optional_nonempty_string(
                    query.get("native_provider", [""])[0]
                ) or "codex"
                await self._send_sse(
                    writer,
                    agent_id,
                    after,
                    native_thread_id=native_thread_id,
                    native_provider=native_provider,
                )
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

    async def _sync_native_transcript(
        self,
        native_thread_id: str,
        *,
        native_provider: str = "codex",
    ) -> str:
        if not native_thread_id:
            return ""
        errors: list[str] = []
        if self._native_transcript_mirror is not None:
            try:
                self._native_transcript_mirror.sync_thread(native_thread_id)
            except Exception as exc:
                errors.append(str(exc) or type(exc).__name__)
        provider_name = native_provider.strip().lower() or "codex"
        if provider_name == "codex":
            provider = self._native_provider("codex")
            if provider is not None:
                try:
                    await provider.sync_session(native_thread_id)
                except Exception as exc:
                    errors.append(str(exc) or type(exc).__name__)
        return "; ".join(error for error in errors if error)

    async def _read_json_body(
        self,
        reader: asyncio.StreamReader,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        raw = await self._read_request_body_bytes(reader, headers)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    async def _read_request_body_bytes(
        self,
        reader: asyncio.StreamReader,
        headers: dict[str, str],
    ) -> bytes:
        if _uses_chunked_transfer(headers):
            return await _read_chunked_body(reader)
        content_length = _safe_int(headers.get("content-length", "0"), default=0)
        if content_length == 0:
            return b""
        if content_length > _MAX_BODY_BYTES:
            raise RequestBodyTooLarge("request body too large")
        return await asyncio.wait_for(
            reader.readexactly(content_length),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    async def _list_native_sessions(
        self,
        target: Any,
        *,
        legacy_codex_controller: bool,
    ) -> list[Any]:
        if legacy_codex_controller:
            return await target.list_sessions()
        return await target.list_sessions(50)

    async def _list_cached_native_sessions(self, target: Any) -> list[Any]:
        cached = getattr(target, "list_cached_sessions", None)
        if cached is None:
            return []
        result = cached(50)
        if asyncio.iscoroutine(result):
            return await result
        return list(result)

    async def _native_sessions_payload(
        self,
        provider_name: str,
        target: Any,
        *,
        legacy_codex_controller: bool,
        fresh: bool,
        schedule_refresh: bool,
    ) -> dict[str, Any]:
        key = ("native_sessions", provider_name)
        native_sync_error = self._native_background_errors.get(key, "")
        native_refresh_pending = False
        if not fresh and getattr(target, "list_cached_sessions", None) is not None:
            sessions = await self._list_cached_native_sessions(target)
            if schedule_refresh:
                native_refresh_pending = self._schedule_native_sessions_refresh(
                    provider_name,
                    target,
                    legacy_codex_controller=legacy_codex_controller,
                )
            native_session_source = "cache"
        else:
            try:
                await self._index_native_jsonl_sessions(provider_name)
                sessions = await asyncio.wait_for(
                    self._list_native_sessions(
                        target,
                        legacy_codex_controller=legacy_codex_controller,
                    ),
                    timeout=self._native_sessions_timeout_seconds,
                )
                native_session_source = "daemon"
                self._native_background_errors.pop(key, None)
            except (asyncio.TimeoutError, JsonRpcTimeout) as exc:
                native_sync_error = str(exc) or "native sessions sync timed out"
                sessions = await self._list_cached_native_sessions(target)
                native_session_source = "cache"
        payload: dict[str, Any] = {
            "sessions": [_json_object(session) for session in sessions],
            "native_refresh_pending": native_refresh_pending,
            "native_session_source": native_session_source,
        }
        if native_sync_error:
            payload["native_sync_error"] = native_sync_error
        return payload

    async def _index_native_jsonl_sessions(self, provider_name: str) -> int:
        if provider_name != "codex" or self._native_transcript_mirror is None:
            return 0
        index_recent = getattr(
            self._native_transcript_mirror,
            "index_recent_sessions",
            None,
        )
        if index_recent is None:
            return 0
        result = index_recent(limit=100)
        if asyncio.iscoroutine(result):
            result = await result
        return int(result or 0)

    def _schedule_native_sessions_refresh(
        self,
        provider_name: str,
        target: Any,
        *,
        legacy_codex_controller: bool,
    ) -> bool:
        key = ("native_sessions", provider_name)
        existing = self._native_background_tasks.get(key)
        if existing is not None and not existing.done():
            return True

        async def refresh() -> None:
            try:
                await asyncio.sleep(_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS)
                payload = await self._native_sessions_payload(
                    provider_name,
                    target,
                    legacy_codex_controller=legacy_codex_controller,
                    fresh=True,
                    schedule_refresh=False,
                )
                self._publish_native_sessions(provider_name, payload)
                self._native_background_errors.pop(key, None)
            except Exception as exc:
                self._native_background_errors[key] = (
                    str(exc) or "native sessions sync failed"
                )
            finally:
                if self._native_background_tasks.get(key) is task:
                    self._native_background_tasks.pop(key, None)

        task = asyncio.create_task(refresh())
        self._native_background_tasks[key] = task
        return True

    async def _send_native_sessions_sse(
        self,
        writer: asyncio.StreamWriter,
        provider_name: str,
        target: Any,
        *,
        legacy_codex_controller: bool,
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

        queue = self._subscribe_native_sessions(provider_name)
        self._ensure_native_sessions_watcher(
            provider_name,
            target,
            legacy_codex_controller=legacy_codex_controller,
        )
        initial_payload = await self._native_sessions_payload(
            provider_name,
            target,
            legacy_codex_controller=legacy_codex_controller,
            fresh=False,
            schedule_refresh=True,
        )
        await _write_json_sse(writer, "native_sessions", initial_payload)

        try:
            while not writer.is_closing():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    writer.write(b": heartbeat\n\n")
                    await writer.drain()
                    continue
                await _write_json_sse(writer, "native_sessions", payload)
        finally:
            self._unsubscribe_native_sessions(provider_name, queue)
            if not self._native_session_streams.get(provider_name):
                key = ("native_sessions_watch", provider_name)
                task = self._native_background_tasks.get(key)
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    def _subscribe_native_sessions(
        self,
        provider_name: str,
    ) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=20)
        self._native_session_streams.setdefault(provider_name, set()).add(queue)
        return queue

    def _unsubscribe_native_sessions(
        self,
        provider_name: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        queues = self._native_session_streams.get(provider_name)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._native_session_streams.pop(provider_name, None)

    def _publish_native_sessions(
        self,
        provider_name: str,
        payload: dict[str, Any],
    ) -> None:
        for queue in list(self._native_session_streams.get(provider_name, ())):
            _offer_json_queue(queue, payload)

    def _ensure_native_sessions_watcher(
        self,
        provider_name: str,
        target: Any,
        *,
        legacy_codex_controller: bool,
    ) -> None:
        key = ("native_sessions_watch", provider_name)
        existing = self._native_background_tasks.get(key)
        if existing is not None and not existing.done():
            return

        async def watch() -> None:
            try:
                self._native_session_file_signatures[provider_name] = (
                    self._native_sessions_file_signature(provider_name)
                )
                while self._native_session_streams.get(provider_name):
                    await asyncio.sleep(_NATIVE_SESSION_WATCH_INTERVAL_SECONDS)
                    signature = self._native_sessions_file_signature(provider_name)
                    if not signature:
                        continue
                    if signature == self._native_session_file_signatures.get(
                        provider_name
                    ):
                        continue
                    self._native_session_file_signatures[provider_name] = signature
                    payload = await self._native_sessions_payload(
                        provider_name,
                        target,
                        legacy_codex_controller=legacy_codex_controller,
                        fresh=True,
                        schedule_refresh=False,
                    )
                    self._publish_native_sessions(provider_name, payload)
            finally:
                if self._native_background_tasks.get(key) is task:
                    self._native_background_tasks.pop(key, None)

        task = asyncio.create_task(watch())
        self._native_background_tasks[key] = task

    def _native_sessions_file_signature(self, provider_name: str) -> str:
        if provider_name != "codex" or self._native_transcript_mirror is None:
            return ""
        signature = getattr(
            self._native_transcript_mirror,
            "session_index_signature",
            None,
        )
        if signature is None:
            return ""
        result = signature(limit=100)
        return str(result or "")

    def _ensure_native_transcript_watcher(
        self,
        agent_run_id: int,
        native_thread_id: str,
        *,
        native_provider: str = "codex",
    ) -> None:
        if not native_thread_id:
            return
        provider_name = native_provider.strip().lower() or "codex"
        key = (
            "native_transcript_watch",
            provider_name,
            native_thread_id,
            str(agent_run_id),
        )
        existing = self._native_background_tasks.get(key)
        if existing is not None and not existing.done():
            return

        async def watch() -> None:
            signature_key = (provider_name, native_thread_id)
            try:
                self._native_transcript_file_signatures[signature_key] = (
                    self._native_transcript_file_signature(
                        provider_name,
                        native_thread_id,
                    )
                )
                while self._hub.subscriber_count(agent_run_id=agent_run_id) > 0:
                    await asyncio.sleep(_NATIVE_TRANSCRIPT_WATCH_INTERVAL_SECONDS)
                    signature = self._native_transcript_file_signature(
                        provider_name,
                        native_thread_id,
                    )
                    if not signature:
                        continue
                    if signature == self._native_transcript_file_signatures.get(
                        signature_key
                    ):
                        continue
                    self._native_transcript_file_signatures[signature_key] = signature
                    sync_error = await self._sync_native_transcript(
                        native_thread_id,
                        native_provider=provider_name,
                    )
                    error_key = (
                        "native_transcript",
                        provider_name,
                        native_thread_id,
                    )
                    if sync_error:
                        self._native_background_errors[error_key] = sync_error
                    else:
                        self._native_background_errors.pop(error_key, None)
            finally:
                if self._native_background_tasks.get(key) is task:
                    self._native_background_tasks.pop(key, None)

        task = asyncio.create_task(watch())
        self._native_background_tasks[key] = task

    def _native_transcript_file_signature(
        self,
        provider_name: str,
        native_thread_id: str,
    ) -> str:
        if provider_name != "codex" or self._native_transcript_mirror is None:
            return ""
        signature = getattr(
            self._native_transcript_mirror,
            "thread_file_signature",
            None,
        )
        if signature is None:
            return ""
        result = signature(native_thread_id)
        return str(result or "")

    def _schedule_native_transcript_sync(
        self,
        native_thread_id: str,
        *,
        native_provider: str = "codex",
    ) -> bool:
        if not native_thread_id:
            return False
        provider_name = native_provider.strip().lower() or "codex"
        key = ("native_transcript", provider_name, native_thread_id)
        existing = self._native_background_tasks.get(key)
        if existing is not None and not existing.done():
            return True

        async def sync() -> None:
            try:
                await asyncio.sleep(_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS)
                sync_error = await self._sync_native_transcript(
                    native_thread_id,
                    native_provider=provider_name,
                )
                if sync_error:
                    self._native_background_errors[key] = sync_error
                else:
                    self._native_background_errors.pop(key, None)
            except Exception as exc:
                self._native_background_errors[key] = (
                    str(exc) or "native transcript sync failed"
                )
            finally:
                if self._native_background_tasks.get(key) is task:
                    self._native_background_tasks.pop(key, None)

        task = asyncio.create_task(sync())
        self._native_background_tasks[key] = task
        return True

    async def _handle_relay_ui_route(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
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
                or self._relay_service is not None
            ),
        ):
            await self._send_html(writer, 401, _native_token_entry_page(path))
            return
        token = str((query.get("token") or [""])[0] or "")
        if path == "/native/workflows":
            await self._send_html(writer, 200, _native_workflows_page(access_token=token))
            return
        if path == "/native/workflows/relay/office":
            relay_config = (
                self._relay_service.config() if self._relay_service is not None else {}
            )
            token_stats = (
                self._relay_service.today_token_stats()
                if self._relay_service is not None
                else {}
            )
            await self._send_html(
                writer,
                200,
                _marvis_relay_office_page(
                    access_token=token,
                    relay_config=relay_config,
                    token_stats=token_stats,
                ),
            )
            return
        if path in (
            "/native/workflows/relay",
            "/native/workflows/relay/chat",
            "/native/workflows/relay/config",
        ):
            project_rows = _relay_project_rows()
            selected_workspace = _relay_selected_workspace(
                str((query.get("workspace") or [""])[0] or ""),
                project_rows,
            )
        if path == "/native/workflows/relay/chat":
            await self._send_html(
                writer,
                200,
                _relay_chat_home_page(
                    selected_workspace=selected_workspace,
                    access_token=token,
                ),
            )
            return
        if path == "/native/workflows/relay":
            summaries = (
                self._relay_service.list_tasks(
                    workspace=selected_workspace
                )
                if self._relay_service is not None
                else []
            )
            providers = (
                self._native_registry.list_provider_summaries()
                if self._native_registry is not None
                else []
            )
            relay_config = (
                self._relay_service.config() if self._relay_service is not None else {}
            )
            await self._send_html(
                writer,
                200,
                _relay_task_list_page(
                    summaries,
                    providers=providers,
                    relay_config=relay_config,
                    projects=project_rows,
                    selected_workspace=selected_workspace,
                    access_token=token,
                ),
            )
            return
        if path == "/native/workflows/relay/config":
            providers = (
                self._native_registry.list_provider_summaries()
                if self._native_registry is not None
                else []
            )
            relay_config = (
                self._relay_service.config() if self._relay_service is not None else {}
            )
            await self._send_html(
                writer,
                200,
                _relay_config_page(
                    providers=providers,
                    relay_config=relay_config,
                    selected_workspace=selected_workspace,
                    access_token=token,
                ),
            )
            return
        task_id = _relay_task_id_from_ui_path(path)
        if task_id is None:
            await self._send_json(writer, 404, {"error": "not found"})
            return
        if self._relay_service is None:
            await self._send_json(writer, 503, {"error": "relay service unavailable"})
            return
        try:
            detail = self._relay_service.get_task(task_id)
        except KeyError:
            await self._send_json(writer, 404, {"error": "relay task not found"})
            return
        events = self._relay_service.events_for_task(task_id)
        detail_view = _relay_task_detail_view(
            str((query.get("view") or ["conversation"])[0] or "conversation")
        )
        await self._send_html(
            writer,
            200,
            _relay_task_detail_page(
                detail,
                access_token=token,
                view=detail_view,
                events=events,
                hub=self._hub,
            ),
        )

    async def _handle_relay_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        if self._relay_service is None:
            await self._send_json(writer, 503, {"error": "relay service unavailable"})
            return
        if not self._is_authorized(
            writer,
            headers,
            query,
            require_token=self._relay_service is not None,
        ):
            await self._send_json(writer, 401, {"error": "unauthorized"})
            return
        normalized_path = _normalize_relay_api_path(path)
        try:
            if normalized_path == "/api/relay/token-stats":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                await self._send_json(writer, 200, self._relay_service.today_token_stats())
                return
            if normalized_path == "/api/relay/config":
                if method == "GET":
                    await self._send_json(writer, 200, self._relay_service.config())
                    return
                if method == "POST":
                    body = await self._read_request_json(writer, reader, headers)
                    if body is None:
                        return
                    assignments = body.get("assignments", body)
                    if not isinstance(assignments, dict):
                        await self._send_json(
                            writer,
                            400,
                            {"error": "assignments must be an object"},
                        )
                        return
                    try:
                        config = self._relay_service.save_config(
                            {
                                str(role): str(provider)
                                for role, provider in assignments.items()
                            }
                        )
                    except ValueError as exc:
                        await self._send_json(writer, 400, {"error": str(exc)})
                        return
                    await self._send_json(writer, 200, config)
                    return
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            if normalized_path == "/api/relay/tasks":
                if method == "GET":
                    summaries = self._relay_service.list_tasks(
                        workspace=_optional_nonempty_string(
                            (query.get("workspace") or [""])[0]
                        ),
                        status=_optional_nonempty_string(
                            (query.get("status") or [""])[0]
                        ),
                    )
                    await self._send_json(
                        writer,
                        200,
                        {"tasks": [summary.to_dict() for summary in summaries]},
                    )
                    return
                if method == "POST":
                    body = await self._read_request_json(writer, reader, headers)
                    if body is None:
                        return
                    workspace = str(body.get("workspace") or "").strip()
                    if not workspace:
                        await self._send_json(
                            writer,
                            400,
                            {"error": "relay task workspace is required"},
                        )
                        return
                    task = self._relay_service.create_task(
                        title=str(body.get("title") or body.get("prompt") or "Relay Task"),
                        prompt=str(body.get("prompt") or ""),
                        workspace=workspace,
                        provider=str(body.get("provider") or ""),
                        role_providers=(
                            {
                                str(role): str(provider)
                                for role, provider in body.get("role_providers", {}).items()
                            }
                            if isinstance(body.get("role_providers"), dict)
                            else None
                        ),
                    )
                    await self._relay_service.dispatch_role(task.id, "director")
                    await self._send_json(
                        writer,
                        200,
                        {"task": self._relay_service.get_task(task.id).task.to_dict()},
                    )
                    return
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return

            task_id, suffix = _relay_task_api_parts(normalized_path)
            if task_id is None:
                await self._send_json(writer, 404, {"error": "not found"})
                return

            if suffix == "":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                detail = self._relay_service.get_task(task_id)
                await self._send_json(writer, 200, detail.to_dict())
                return
            if suffix == "/events":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                after = _safe_int(query.get("after", ["0"])[0], default=0)
                live = "text/event-stream" in headers.get("accept", "").lower()
                relay_event_queue = (
                    self._relay_service.subscribe_events(task_id) if live else None
                )
                try:
                    events = self._relay_service.events_for_task(task_id, after=after)
                    detail = self._relay_service.get_task(task_id)
                except Exception:
                    if relay_event_queue is not None:
                        self._relay_service.unsubscribe_events(task_id, relay_event_queue)
                    raise
                await _send_relay_sse(
                    writer,
                    events,
                    detail=detail,
                    hub=self._hub,
                    relay_service=self._relay_service,
                    relay_event_queue=relay_event_queue,
                    live=live,
                )
                return
            if suffix == "/sessions":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                detail = self._relay_service.get_task(task_id)
                await self._send_json(
                    writer,
                    200,
                    {"sessions": [link.to_dict() for link in detail.session_links]},
                )
                return
            if suffix == "/message":
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                await self._relay_service.add_user_message(
                    task_id,
                    str(body.get("text") or body.get("prompt") or ""),
                )
                await self._send_json(
                    writer,
                    200,
                    self._relay_service.get_task(task_id).to_dict(),
                )
                return
            if suffix == "/resume":
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                role = _optional_nonempty_string(body.get("role"))
                if not role:
                    detail = self._relay_service.get_task(task_id)
                    role = _relay_first_blocked_role(detail.role_jobs)
                if not role:
                    await self._send_json(
                        writer,
                        400,
                        {"error": "relay task has no blocked role to resume"},
                    )
                    return
                await self._relay_service.resume_role(
                    task_id,
                    role,
                    force=bool(body.get("force")),
                )
                await self._send_json(
                    writer,
                    200,
                    self._relay_service.get_task(task_id).to_dict(),
                )
                return
            if suffix == "/interrupt":
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                await self._relay_service.interrupt(
                    task_id,
                    role=_optional_nonempty_string(body.get("role")),
                )
                await self._send_json(
                    writer,
                    200,
                    self._relay_service.get_task(task_id).to_dict(),
                )
                return
            await self._send_json(writer, 404, {"error": "not found"})
        except KeyError:
            await self._send_json(writer, 404, {"error": "relay task not found"})

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

        if route == "/sessions/stream":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            await self._send_native_sessions_sse(
                writer,
                provider_name,
                target,
                legacy_codex_controller=legacy_codex_controller,
            )
            return

        if route == "/sessions":
            if method != "GET":
                await self._send_json(writer, 405, {"error": "method not allowed"})
                return
            fresh = query.get("fresh", [""])[0].lower() in ("1", "true", "yes")
            payload = await self._native_sessions_payload(
                provider_name,
                target,
                legacy_codex_controller=legacy_codex_controller,
                fresh=fresh,
                schedule_refresh=True,
            )
            await self._send_json(
                writer,
                200,
                payload,
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
            images = _safe_image_attachments(body.get("images"))
            permission_kwargs = _native_permission_kwargs_from_body(provider_name, body)
            collaboration_kwargs = _codex_collaboration_kwargs_from_body(
                provider_name,
                body,
            )
            service_tier = _optional_nonempty_string(
                body.get("service_tier") or body.get("serviceTier")
            )
            if prompt.strip() or images:
                result = await target.start_session(
                    str(body.get("cwd", "")),
                    prompt,
                    model=model,
                    effort=_optional_nonempty_string(body.get("effort")),
                    service_tier=service_tier,
                    images=images,
                    **permission_kwargs,
                    **collaboration_kwargs,
                )
            else:
                create_permission_kwargs = dict(permission_kwargs)
                create_permission_kwargs.pop("sandbox_policy", None)
                result = await target.create_session(
                    str(body.get("cwd", "")),
                    model=model,
                    service_tier=service_tier,
                    **create_permission_kwargs,
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
            try:
                session = await target.read_session(thread_id)
            except KeyError:
                await self._send_json(writer, 404, {"error": "native session not found"})
                return
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "attach" and len(parts) == 2:
            try:
                session = await target.attach_session(thread_id)
            except KeyError:
                await self._send_json(writer, 404, {"error": "native session not found"})
                return
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "sync" and len(parts) == 2:
            try:
                session = await target.sync_session(thread_id)
            except KeyError:
                await self._send_json(writer, 404, {"error": "native session not found"})
                return
            await self._send_json(writer, 200, _json_object(session))
            return
        if method == "POST" and action == "continue" and len(parts) == 2:
            capabilities = provider.capabilities()
            if not capabilities.can_continue_session:
                await self._send_json(
                    writer,
                    409,
                    {"error": _native_disabled_reason(capabilities, "can_continue_session")},
                )
                return
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            permission_kwargs = _native_permission_kwargs_from_body(provider_name, body)
            collaboration_kwargs = _codex_collaboration_kwargs_from_body(
                provider_name,
                body,
            )
            if provider_name.strip().lower() != "antigravity":
                permission_kwargs.pop("sandbox", None)
            force_new_turn = (
                body.get("force_new_turn") is True
                or body.get("forceNewTurn") is True
            )
            continue_kwargs: dict[str, Any] = {}
            if force_new_turn:
                continue_kwargs["force_new_turn"] = True
            try:
                result = await target.continue_session(
                    thread_id,
                    str(body.get("prompt", "")),
                    model=_optional_nonempty_string(body.get("model")),
                    effort=_optional_nonempty_string(body.get("effort")),
                    service_tier=_optional_nonempty_string(
                        body.get("service_tier") or body.get("serviceTier")
                    ),
                    images=_safe_image_attachments(body.get("images")),
                    **permission_kwargs,
                    **collaboration_kwargs,
                    **continue_kwargs,
                )
            except KeyError:
                await self._send_json(writer, 404, {"error": "native session not found"})
                return
            await self._send_json(writer, 200, _json_object(result))
            return
        if method == "POST" and action == "steer" and len(parts) == 2:
            capabilities = provider.capabilities()
            if not capabilities.can_steer_active_turn:
                await self._send_json(
                    writer,
                    409,
                    {"error": _native_disabled_reason(capabilities, "can_steer_active_turn")},
                )
                return
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            expected_turn_id = str(
                body.get("expected_turn_id") or body.get("turn_id") or ""
            )
            permission_kwargs = _native_permission_kwargs_from_body(provider_name, body)
            if provider_name.strip().lower() != "antigravity":
                permission_kwargs.pop("sandbox", None)
            try:
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
                    **permission_kwargs,
                )
            except KeyError:
                await self._send_json(writer, 404, {"error": "native session not found"})
                return
            await self._send_json(writer, 200, _json_object(result))
            return
        if method == "POST" and action == "interrupt" and len(parts) == 2:
            capabilities = provider.capabilities()
            if not capabilities.can_interrupt:
                await self._send_json(
                    writer,
                    409,
                    {"error": _native_disabled_reason(capabilities, "can_interrupt")},
                )
                return
            body = await self._read_request_json(writer, reader, headers)
            if body is None:
                return
            try:
                result = await target.interrupt_session(
                    thread_id,
                    str(body.get("turn_id", "")),
                )
            except KeyError:
                await self._send_json(writer, 404, {"error": "native session not found"})
                return
            await self._send_json(writer, 200, _json_object(result))
            return
        await self._send_json(writer, 404, {"error": "not found"})

    async def _handle_workflow_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        if self._workflow_service is None:
            await self._send_json(
                writer,
                503,
                {"error": "workflow service unavailable"},
            )
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
            await self._send_json(writer, 401, {"error": "unauthorized"})
            return

        if path not in (
            "/api/native/workflows/handoffs/preview",
            "/api/native/workflows/handoffs/execute",
        ):
            await self._send_json(writer, 404, {"error": "not found"})
            return
        if method != "POST":
            await self._send_json(writer, 405, {"error": "method not allowed"})
            return
        body = await self._read_request_json(writer, reader, headers)
        if body is None:
            return

        try:
            if path == "/api/native/workflows/handoffs/preview":
                result = await self._workflow_service.preview_handoff(
                    source_provider=str(
                        body.get("source_provider") or body.get("sourceProvider") or ""
                    ),
                    source_thread_id=str(
                        body.get("source_thread_id") or body.get("sourceThreadId") or ""
                    ),
                    source_turn_id=str(
                        body.get("source_turn_id") or body.get("sourceTurnId") or ""
                    ),
                    target_provider=str(
                        body.get("target_provider") or body.get("targetProvider") or ""
                    ),
                    cwd=str(body.get("cwd") or ""),
                    intent=str(body.get("intent") or ""),
                    user_note=str(body.get("user_note") or body.get("userNote") or ""),
                )
            else:
                result = await self._workflow_service.execute_handoff(
                    workflow_run_id=str(
                        body.get("workflow_run_id") or body.get("workflowRunId") or ""
                    ),
                    preview_id=str(body.get("preview_id") or body.get("previewId") or ""),
                    target_provider=str(
                        body.get("target_provider") or body.get("targetProvider") or ""
                    ),
                    cwd=str(body.get("cwd") or ""),
                    prompt=str(body.get("prompt") or ""),
                )
        except KeyError as exc:
            await self._send_json(writer, 404, {"error": str(exc)})
            return
        except ValueError as exc:
            await self._send_json(writer, 409, {"error": str(exc)})
            return
        await self._send_json(writer, 200, _json_object(result))

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
        theme = _optional_nonempty_string(query.get("theme", [""])[0]) or ""
        await self._send_html(writer, 200, _native_codex_page(provider_name, theme=theme))

    async def _read_request_json(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        try:
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/x-www-form-urlencoded":
                raw = await self._read_request_body_bytes(reader, headers)
                parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                return {
                    key: values[-1] if values else ""
                    for key, values in parsed.items()
                }
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
            extra_headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    async def _send_static_asset(
        self,
        writer: asyncio.StreamWriter,
        path: str,
    ) -> None:
        relative = unquote(path.removeprefix("/static/")).strip("/")
        if not relative:
            await self._send_json(writer, 404, {"error": "not found"})
            return
        content_type = _STATIC_CONTENT_TYPES.get(Path(relative).suffix)
        if content_type is None:
            await self._send_json(writer, 404, {"error": "not found"})
            return
        asset_path = (_STATIC_ASSET_DIR / relative).resolve()
        static_root = _STATIC_ASSET_DIR.resolve()
        try:
            asset_path.relative_to(static_root)
        except ValueError:
            await self._send_json(writer, 404, {"error": "not found"})
            return
        if not asset_path.is_file():
            await self._send_json(writer, 404, {"error": "not found"})
            return
        await _send_response(
            writer,
            200,
            content_type,
            asset_path.read_bytes(),
            extra_headers={
                "Cache-Control": "public, max-age=300, stale-while-revalidate=60"
            },
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
        *,
        native_thread_id: str = "",
        native_provider: str = "codex",
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
        self._ensure_native_transcript_watcher(
            agent_run_id,
            native_thread_id,
            native_provider=native_provider,
        )
        try:
            while not writer.is_closing():
                event = await queue.get()
                if event.id <= latest:
                    continue
                latest = event.id
                await _write_sse(writer, event)
        finally:
            self._hub.unsubscribe(agent_run_id=agent_run_id, queue=queue)
            if native_thread_id and self._hub.subscriber_count(
                agent_run_id=agent_run_id
            ) == 0:
                provider_name = native_provider.strip().lower() or "codex"
                key = (
                    "native_transcript_watch",
                    provider_name,
                    native_thread_id,
                    str(agent_run_id),
                )
                task = self._native_background_tasks.get(key)
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


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


async def _write_json_sse(
    writer: asyncio.StreamWriter,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    writer.write(f"event: {event_type}\n".encode("utf-8"))
    writer.write(f"data: {raw}\n\n".encode("utf-8"))
    await writer.drain()


def _offer_json_queue(
    queue: asyncio.Queue[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    try:
        queue.put_nowait(payload)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        pass


async def _send_relay_sse(
    writer: asyncio.StreamWriter,
    events: list[Any],
    *,
    detail: Any | None = None,
    hub: WorkerLiveStreamHub | None = None,
    relay_service: Any | None = None,
    relay_event_queue: asyncio.Queue[Any] | None = None,
    live: bool = False,
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
    seen_relay_sequences: set[int] = set()
    seen_worker_events: set[tuple[str, int, str]] = set()
    for event in events:
        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        sequence = int(payload.get("sequence") or 0)
        if sequence:
            seen_relay_sequences.add(sequence)
        event_type = str(payload.get("event_type") or "message")
        event_payload = dict(payload.get("payload") or {})
        runtime_event_id = int(event_payload.get("runtime_event_id") or 0)
        if runtime_event_id > 0:
            seen_worker_events.add(
                (
                    str(payload.get("role") or event_payload.get("role") or ""),
                    runtime_event_id,
                    event_type,
                )
            )
        await _write_relay_sse_payload(
            writer,
            event_id=str(sequence),
            event_type=event_type,
            payload=payload,
        )
    role_jobs = [
        job
        for job in getattr(detail, "role_jobs", [])
        if getattr(job, "agent_run_id", None) is not None
    ]
    task_id = int(getattr(getattr(detail, "task", None), "id", 0) or 0)
    latest_by_agent: dict[int, int] = {}
    if hub is not None:
        for job in role_jobs:
            agent_run_id = int(job.agent_run_id)
            latest_by_agent[agent_run_id] = 0
            for worker_event in hub.snapshot(agent_run_id=agent_run_id, after_id=0):
                latest_by_agent[agent_run_id] = max(
                    latest_by_agent[agent_run_id],
                    int(worker_event.id),
                )
                relay_event = _relay_worker_payload(
                    int(detail.task.id),
                    str(job.role),
                    worker_event,
                )
                if relay_event is None:
                    continue
                event_type, payload = relay_event
                if (
                    str(job.role),
                    int(worker_event.id),
                    event_type,
                ) in seen_worker_events:
                    continue
                await _write_relay_sse_payload(
                    writer,
                    event_id=f"native-{worker_event.id}",
                    event_type=event_type,
                    payload=payload,
                )
    if live and relay_service is not None and task_id:
        queue = relay_event_queue or relay_service.subscribe_events(task_id)
        worker_subscriptions: list[tuple[Any, asyncio.Queue[WorkerStreamEvent]]] = []
        pending: dict[asyncio.Task[Any], tuple[str, Any, Any]] = {
            asyncio.create_task(queue.get()): ("relay", None, queue)
        }
        if hub is not None:
            for job in role_jobs:
                agent_run_id = int(job.agent_run_id)
                worker_queue = hub.subscribe(agent_run_id=agent_run_id)
                worker_subscriptions.append((job, worker_queue))
                pending[asyncio.create_task(worker_queue.get())] = (
                    "worker",
                    job,
                    worker_queue,
                )
        try:
            while not writer.is_closing():
                done, _pending = await asyncio.wait(
                    pending,
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                for task in done:
                    source_kind, job, source_queue = pending.pop(task)
                    if source_kind == "relay":
                        event = task.result()
                        payload = (
                            event.to_dict()
                            if hasattr(event, "to_dict")
                            else dict(event)
                        )
                        sequence = int(payload.get("sequence") or 0)
                        if sequence not in seen_relay_sequences:
                            if sequence:
                                seen_relay_sequences.add(sequence)
                            event_type = str(payload.get("event_type") or "message")
                            event_payload = dict(payload.get("payload") or {})
                            runtime_event_id = int(
                                event_payload.get("runtime_event_id")
                                or payload.get("runtime_event_id")
                                or 0
                            )
                            if runtime_event_id > 0:
                                seen_worker_events.add(
                                    (
                                        str(
                                            payload.get("role")
                                            or event_payload.get("role")
                                            or ""
                                        ),
                                        runtime_event_id,
                                        event_type,
                                    )
                                )
                            await _write_relay_sse_payload(
                                writer,
                                event_id=str(sequence or ""),
                                event_type=event_type,
                                payload=payload,
                            )
                        pending[asyncio.create_task(source_queue.get())] = (
                            "relay",
                            None,
                            source_queue,
                        )
                        continue
                    worker_event = task.result()
                    agent_run_id = int(job.agent_run_id)
                    worker_key = (str(job.role), int(worker_event.id), "role.native_event")
                    if (
                        worker_event.id > latest_by_agent.get(agent_run_id, 0)
                        and worker_key not in seen_worker_events
                    ):
                        latest_by_agent[agent_run_id] = int(worker_event.id)
                        seen_worker_events.add(worker_key)
                        await _write_relay_worker_event(
                            writer,
                            task_id=int(detail.task.id),
                            role=str(job.role),
                            worker_event=worker_event,
                        )
                    pending[asyncio.create_task(source_queue.get())] = (
                        "worker",
                        job,
                        source_queue,
                    )
        finally:
            for task in pending:
                task.cancel()
            for job, worker_queue in worker_subscriptions:
                hub.unsubscribe(agent_run_id=int(job.agent_run_id), queue=worker_queue)
            relay_service.unsubscribe_events(task_id, queue)
    elif live and hub is not None and role_jobs:
        subscriptions: list[tuple[Any, asyncio.Queue[WorkerStreamEvent]]] = []
        pending: dict[asyncio.Task[WorkerStreamEvent], tuple[Any, asyncio.Queue[WorkerStreamEvent]]] = {}
        for job in role_jobs:
            queue = hub.subscribe(agent_run_id=int(job.agent_run_id))
            subscriptions.append((job, queue))
            pending[asyncio.create_task(queue.get())] = (job, queue)
        try:
            while not writer.is_closing():
                done, _pending = await asyncio.wait(
                    pending,
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                for task in done:
                    job, queue = pending.pop(task)
                    worker_event = task.result()
                    agent_run_id = int(job.agent_run_id)
                    if worker_event.id > latest_by_agent.get(agent_run_id, 0):
                        latest_by_agent[agent_run_id] = int(worker_event.id)
                        await _write_relay_worker_event(
                            writer,
                            task_id=int(detail.task.id),
                            role=str(job.role),
                            worker_event=worker_event,
                        )
                    pending[asyncio.create_task(queue.get())] = (job, queue)
        finally:
            for task in pending:
                task.cancel()
            for job, queue in subscriptions:
                hub.unsubscribe(agent_run_id=int(job.agent_run_id), queue=queue)
    try:
        await writer.drain()
    except (ConnectionError, RuntimeError):
        pass
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
    except (asyncio.TimeoutError, ConnectionError, RuntimeError):
        pass


async def _write_relay_sse_payload(
    writer: asyncio.StreamWriter,
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    writer.write(f"id: {event_id}\n".encode("utf-8"))
    writer.write(f"event: {event_type}\n".encode("utf-8"))
    writer.write(
        ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode(
            "utf-8"
        )
    )
    await writer.drain()


async def _write_relay_worker_event(
    writer: asyncio.StreamWriter,
    *,
    task_id: int,
    role: str,
    worker_event: WorkerStreamEvent,
) -> None:
    relay_event = _relay_worker_payload(task_id, role, worker_event)
    if relay_event is None:
        return
    event_type, payload = relay_event
    await _write_relay_sse_payload(
        writer,
        event_id=f"native-{worker_event.id}",
        event_type=event_type,
        payload=payload,
    )


def _relay_worker_payload(
    task_id: int,
    role: str,
    worker_event: WorkerStreamEvent,
) -> tuple[str, dict[str, Any]] | None:
    payload = dict(worker_event.payload)
    return "role.native_event", {
        "event_type": "role.native_event",
        "task_id": task_id,
        "role": role,
        "runtime_event_id": worker_event.id,
        "agent_run_id": worker_event.agent_run_id,
        "kind": worker_event.kind,
        "payload": payload,
        "native_event": worker_event.to_json_dict(),
    }


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


def _relay_task_id_from_ui_path(path: str) -> int | None:
    prefix = "/native/workflows/relay/tasks/"
    if not path.startswith(prefix):
        return None
    raw = path.removeprefix(prefix).strip("/")
    return int(raw) if raw.isdigit() else None


def _normalize_relay_api_path(path: str) -> str:
    if path == "/api/relay/runs":
        return "/api/relay/tasks"
    prefix = "/api/relay/runs/"
    if path.startswith(prefix):
        return "/api/relay/tasks/" + path.removeprefix(prefix)
    return path


def _relay_task_api_parts(path: str) -> tuple[int | None, str]:
    prefix = "/api/relay/tasks/"
    if not path.startswith(prefix):
        return None, ""
    raw = path.removeprefix(prefix)
    task_raw, _, suffix_raw = raw.partition("/")
    if not task_raw.isdigit():
        return None, ""
    suffix = f"/{suffix_raw}" if suffix_raw else ""
    return int(task_raw), suffix


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


def _native_permission_presets(provider: str) -> list[dict[str, object]]:
    normalized = str(provider or "").strip().lower()
    if normalized == "claude":
        return list(_CLAUDE_PERMISSION_PRESETS)
    if normalized == "antigravity":
        return [
            {key: value for key, value in preset.items() if key not in {"dangerously_skip_permissions", "sandbox"}}
            for preset in _ANTIGRAVITY_PERMISSION_PRESETS
        ]
    return list(_CODEX_PERMISSION_PRESETS_UI)


def _native_permission_kwargs_from_body(
    provider: str,
    body: dict[str, Any],
) -> dict[str, object]:
    mode = body.get("permission_mode") if body is not None else None
    if mode is None:
        mode = body.get("permissionMode") if body is not None else None
    if mode is None:
        return {}
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider == "claude":
        return {"permission_mode": normalize_claude_permission_mode(str(mode))}
    if normalized_provider == "antigravity":
        normalized_mode = str(mode).strip().lower()
        preset = _ANTIGRAVITY_PERMISSION_PRESET_MAP.get(
            normalized_mode,
            _ANTIGRAVITY_PERMISSION_PRESET_MAP["default"],
        )
        return {
            "dangerously_skip_permissions": bool(preset["dangerously_skip_permissions"]),
            "sandbox": bool(preset["sandbox"]),
        }
    preset = _CODEX_PERMISSION_PRESETS.get(str(mode).strip(), {})
    return dict(preset)


def _codex_permission_kwargs_from_body(body: dict[str, Any]) -> dict[str, object]:
    mode = str(body.get("permission_mode") or body.get("permissionMode") or "default")
    preset = _CODEX_PERMISSION_PRESETS.get(mode.strip(), {})
    return dict(preset)


def _codex_collaboration_kwargs_from_body(
    provider: str,
    body: dict[str, Any],
) -> dict[str, object]:
    if str(provider or "").strip().lower() != "codex":
        return {}
    raw = body.get("collaboration_mode")
    if raw is None:
        raw = body.get("collaborationMode")
    if not isinstance(raw, dict):
        return {}
    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in {"default", "plan"}:
        return {}
    clean: dict[str, object] = {"mode": mode}
    settings = raw.get("settings")
    clean_settings = dict(settings) if isinstance(settings, dict) else {}
    existing_model = clean_settings.get("model")
    if mode == "plan" and not (
        isinstance(existing_model, str) and existing_model.strip()
    ):
        body_model = body.get("model")
        if isinstance(body_model, str) and body_model.strip():
            clean_settings["model"] = body_model.strip()
    clean["settings"] = clean_settings
    return {"collaboration_mode": clean}


def _plugin_icon_data_url(manifest: Path, icon_path: object) -> str:
    if not isinstance(icon_path, str) or not icon_path.strip():
        return ""
    raw_path = icon_path.strip()
    plugin_root = manifest.parent.parent
    candidates = [
        (plugin_root / raw_path).resolve(),
        (manifest.parent / raw_path).resolve(),
    ]
    path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    try:
        if not path.is_file() or path.stat().st_size > _MAX_PLUGIN_ICON_BYTES:
            return ""
        data = path.read_bytes()
    except OSError:
        return ""
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }.get(suffix, "application/octet-stream")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _native_app_manifest() -> str:
    payload = {
        "name": "WLCodex Native",
        "short_name": "WLCodex",
        "description": "Native mobile workspace for WLCodex sessions.",
        "start_url": "/native/codex",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "fullscreen", "browser"],
        "orientation": "portrait",
        "theme_color": "#000000",
        "background_color": "#000000",
        "icons": [
            {
                "src": "/native/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ": "))


def _native_app_icon_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#000000"/>
  <rect x="72" y="76" width="368" height="360" rx="72" fill="#111214"/>
  <path d="M312 160 216 256l96 96" fill="none" stroke="#f4f4f5" stroke-width="42" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="370" cy="136" r="24" fill="#58a6ff"/>
</svg>"""


def _codex_plugin_menu_items() -> list[dict[str, str]]:
    cache_root = Path.home() / ".codex" / "plugins" / "cache"
    if not cache_root.exists():
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for manifest in sorted(cache_root.glob("*/*/*/.codex-plugin/plugin.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        interface = data.get("interface")
        if not isinstance(interface, dict):
            interface = {}
        name = interface.get("displayName") or data.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        description = interface.get("shortDescription") or data.get("description") or ""
        brand_color = interface.get("brandColor") or ""
        icon = _plugin_icon_data_url(
            manifest,
            interface.get("composerIcon") or interface.get("logo") or interface.get("icon"),
        )
        items.append(
            {
                "name": name.strip(),
                "description": str(description).strip(),
                "brand_color": str(brand_color).strip(),
                "icon": icon,
            }
        )
    return items[:12]


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


def _native_disabled_reason(capabilities: Any, key: str) -> str:
    reasons = getattr(capabilities, "disabled_reasons", {})
    if isinstance(reasons, dict):
        reason = str(reasons.get(key) or "").strip()
        if reason:
            return reason
    return "native provider capability is disabled"


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
    token_suffix = _token_suffix(access_token)
    council_links = """
      <a class="provider council" href="/council__TOKEN_SUFFIX__">
        <span>议会审核</span>
        <small>提交方案并运行五席审核</small>
      </a>
      <a class="provider relay" data-native-entry="marvis-relay" href="/native/workflows/relay__TOKEN_SUFFIX__">
        <span>Marvis 接力</span>
        <small>像 Marvis 一样用对话流调度五角色接力</small>
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
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Native Agents</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <link rel="stylesheet" href="/static/native_index.css">
</head>
<body class="aurora-bg noise-overlay">
  <main>
    <div class="native-index-topbar">
      <button class="circle native-back" id="back" aria-label="back" aria-disabled="true" disabled>‹</button>
      <h1>Native Agents</h1>
      <span class="native-back-spacer" aria-hidden="true"></span>
    </div>
    {council_links}
    {links}
  </main>
</body>
</html>""")


def _native_workflows_page(*, access_token: str = "") -> str:
    token_suffix = _token_suffix(access_token)
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>工作流</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <link rel="stylesheet" href="/static/native_index.css">
</head>
<body class="aurora-bg noise-overlay">
  <main>
    <div class="native-index-topbar">
      <a class="circle native-back" href="/native{token_suffix}" aria-label="back">‹</a>
      <h1>工作流</h1>
      <span class="native-back-spacer" aria-hidden="true"></span>
    </div>
    <a class="provider workflow" data-native-entry="marvis-relay" href="/native/workflows/relay{token_suffix}">
      <span>Marvis 接力</span>
      <small>对话式发布任务，总工程师调度多角色实时协作。</small>
    </a>
    <a class="provider council" href="/council{token_suffix}">
      <span>议会审核</span>
      <small>沿用现有五席审核入口。</small>
    </a>
    <div class="provider">
      <span>Dev Flow</span>
      <small>工作流类入口预留，稳定 UI 接入后开放。</small>
    </div>
  </main>
</body>
</html>""")


def _relay_task_list_page(
    summaries: list[Any],
    *,
    providers: list[dict[str, str]],
    relay_config: dict[str, Any] | None = None,
    projects: list[Any] | None = None,
    selected_workspace: str = "",
    access_token: str = "",
) -> str:
    token_suffix = _token_suffix(access_token)
    relay_config = relay_config or {}
    selected_workspace = str(selected_workspace or "")
    sorted_summaries = sorted(
        summaries,
        key=lambda summary: str(getattr(summary, "last_activity_at", "") or ""),
        reverse=True,
    )
    filters = ["running", "waiting_user", "blocked", "completed", "interrupted"]
    counts = {status: 0 for status in filters}
    for summary in sorted_summaries:
        status = str(getattr(summary, "status", "") or "")
        if status in counts:
            counts[status] += 1
    filter_html = "\n".join(
        '<button class="relay-filter-chip" type="button" '
        f'data-filter="{escape(status)}">'
        f"{escape(_relay_task_status_label(status))} "
        f'<span>{counts.get(status, 0)}</span></button>'
        for status in filters
    )
    if sorted_summaries:
        task_list_html = "\n".join(
            _relay_task_card_html(summary, token_suffix)
            for summary in sorted_summaries
        )
    else:
        task_list_html = """
          <section class="relay-empty-state">
            <h2>还没有接力任务</h2>
            <p>创建一个大任务后，总工程师会先接收并调度架构、开发、测试和审计角色。</p>
            <p>当前工作区还没有接力任务，可以从底部导航的对话开始第一个任务。</p>
          </section>
        """
    task_count = len(sorted_summaries)
    active_count = sum(
        1
        for summary in sorted_summaries
        if str(getattr(summary, "status", "") or "") in {"running", "waiting_user", "blocked"}
    )
    workspace_nav = _relay_workspace_nav_html(
        projects or [],
        selected_workspace=selected_workspace,
        access_token=access_token,
    )
    workspace_label = Path(selected_workspace).name or selected_workspace or "wlcodex"
    topbar_html = _marvis_relay_topbar(
        title="Marvis",
        subtitle=workspace_label,
        back_href=f"/native{token_suffix}",
        right_html=f"""
          <a class="marvis-relay-icon-button" href="/native/workflows/relay/office{token_suffix}" aria-label="Marvis办公室">
            <span class="marvis-relay-icon-devices" aria-hidden="true"></span>
          </a>
        """,
    )
    bottom_nav_html = _marvis_relay_bottom_nav(
        "tasks",
        access_token=access_token,
        selected_workspace=selected_workspace,
    )
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light only">
  <title>流式接力</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="{_RELAY_MARVIS_CSS_HREF}">
  <style>
    html {{ background: var(--bg-canvas); }}
    body {{ margin: 0; color: var(--text-primary); background: transparent; }}
    header {{ position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 48px 1fr auto; gap: 12px; align-items: center; padding: 12px 18px; background: rgba(5,5,8,.88); border-bottom: 1px solid var(--border-header); }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 22px; }}
    main {{ display: grid; gap: 18px; width: min(1120px, 100%); margin: 0 auto; padding: 18px; box-sizing: border-box; }}
    .relay-shell {{ display: grid; gap: 18px; align-items: start; }}
    .relay-toolbar {{ display: flex; gap: 8px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }}
    .relay-secondary {{ min-height: 38px; border: 1px solid var(--border-subtle); border-radius: 6px; background: transparent; color: var(--text-primary); padding: 0 12px; text-decoration: none; display: inline-grid; place-items: center; }}
    .relay-workspace-nav {{ display: grid; gap: 8px; border: 1px solid var(--border-card); border-radius: 8px; background: var(--bg-surface); padding: 12px; }}
    .relay-workspace-row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .relay-workspace-link {{ min-height: 34px; display: inline-grid; place-items: center; border: 1px solid var(--border-subtle); border-radius: 999px; padding: 0 11px; color: var(--text-muted); text-decoration: none; }}
    .relay-workspace-link.active {{ border-color: var(--color-link); color: var(--text-primary); background: rgba(88, 166, 255, .1); }}
    .relay-form {{ display: grid; gap: 10px; }}
    .relay-form label {{ display: grid; gap: 6px; color: var(--text-muted); font-size: 13px; }}
    .relay-form input, .relay-form textarea, .relay-form select {{ width: 100%; box-sizing: border-box; border: 1px solid var(--border-subtle); border-radius: 6px; padding: 10px; background: rgba(255,255,255,.04); color: var(--text-primary); }}
    .relay-form textarea {{ min-height: 130px; resize: vertical; }}
    .relay-form button, .relay-open, .relay-primary {{ min-height: 38px; border: 1px solid var(--color-link); border-radius: 6px; background: transparent; color: var(--text-primary); text-decoration: none; display: inline-grid; place-items: center; padding: 0 12px; }}
    .relay-form-section {{ display: grid; gap: 8px; border: 1px solid var(--border-subtle); border-radius: 8px; padding: 12px; }}
    .relay-form-section h3 {{ font-size: 14px; }}
    .relay-config-copy {{ margin: 0; color: var(--text-muted); font-size: 13px; line-height: 1.5; }}
    .relay-config-panel {{ display: grid; gap: 12px; border: 1px solid var(--border-card); border-radius: 8px; background: var(--bg-surface); padding: 14px; }}
    .relay-config-grid {{ display: grid; gap: 8px; }}
    .relay-config-row {{ display: grid; grid-template-columns: minmax(120px, 160px) minmax(150px, 220px) minmax(0, 1fr); gap: 10px; align-items: center; border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px; min-width: 0; }}
    .relay-provider-select {{ width: 100%; min-height: 38px; border: 1px solid var(--border-subtle); border-radius: 6px; padding: 8px 34px 8px 10px; background: #15161d; color: #f4f4f5; color-scheme: dark; appearance: none; background-image: linear-gradient(45deg, transparent 50%, #c9d1d9 50%), linear-gradient(135deg, #c9d1d9 50%, transparent 50%); background-position: calc(100% - 17px) 16px, calc(100% - 12px) 16px; background-size: 5px 5px, 5px 5px; background-repeat: no-repeat; }}
    .relay-provider-select:focus {{ outline: 2px solid rgba(88,166,255,.55); outline-offset: 2px; border-color: var(--color-link); }}
    .relay-provider-select option {{ background: #15161d; color: #f4f4f5; }}
    .relay-config-tools {{ display: flex; gap: 6px; flex-wrap: wrap; min-width: 0; }}
    .relay-primary {{ background: rgba(88, 166, 255, .12); font-weight: var(--weight-bold); }}
    .relay-primary:hover, .relay-open:hover, .relay-filter-chip:hover {{ background: rgba(255,255,255,.07); }}
    .relay-history-list {{ display: grid; gap: 14px; min-width: 0; }}
    .relay-history-head {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }}
    .relay-history-title {{ display: grid; gap: 4px; min-width: 0; }}
    .relay-filter-row {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .relay-filter-chip {{ min-height: 32px; border: 1px solid var(--border-subtle); border-radius: 999px; background: transparent; color: var(--text-muted); padding: 0 10px; }}
    .relay-filter-chip.active {{ border-color: var(--color-link); color: var(--text-primary); background: rgba(88, 166, 255, .1); }}
    .relay-task-list {{ display: grid; gap: 10px; min-width: 0; }}
    .relay-task-card {{ display: grid; gap: 10px; border: 1px solid var(--border-card); border-radius: 8px; padding: 14px; background: var(--bg-surface); min-width: 0; overflow-wrap: anywhere; }}
    .relay-card-head {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: start; }}
    .relay-card-meta {{ display: flex; gap: 8px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }}
    .relay-card-open {{ white-space: nowrap; }}
    .relay-title {{ font-size: 17px; font-weight: var(--weight-bold); }}
    .relay-muted {{ color: var(--text-muted); font-size: 13px; }}
    .relay-summary {{ color: var(--text-primary); font-size: 14px; line-height: 1.45; }}
    .relay-status-badge {{ border: 1px solid var(--border-subtle); border-radius: 999px; padding: 4px 8px; font-size: 12px; color: var(--text-primary); background: rgba(255,255,255,.05); white-space: nowrap; }}
    .relay-empty-state {{ display: grid; justify-items: start; gap: 10px; border: 1px dashed var(--border-subtle); border-radius: 8px; padding: 18px; color: var(--text-muted); }}
    .relay-empty-state h2 {{ color: var(--text-primary); font-size: 18px; }}
    .relay-empty-state p {{ margin: 0; max-width: 58ch; line-height: 1.55; }}
    @media (max-width: 760px) {{ header {{ grid-template-columns: 48px 1fr; }} .relay-toolbar {{ grid-column: 1 / -1; justify-content: stretch; }} .relay-primary, .relay-secondary {{ width: 100%; }} .relay-card-head {{ grid-template-columns: 1fr; }} .relay-card-meta {{ justify-content: flex-start; }} .relay-card-open {{ width: auto; }} .relay-config-row {{ grid-template-columns: 1fr; }} main {{ padding: 12px; }} .relay-modal-head {{ padding: 12px; }} .relay-modal-body {{ padding: 14px 12px 28px; }} }}
  </style>
</head>
<body data-marvis-relay-view="tasks">
  <div class="marvis-relay-phone">
    {topbar_html}
  <main>
    <div class="relay-shell">
      {workspace_nav}
      <section class="relay-history-list" aria-label="relay task history">
        <div class="relay-history-head">
          <div class="relay-history-title">
            <h2>任务历史</h2>
            <span class="relay-muted">共 {task_count} 个任务，{active_count} 个需要跟进</span>
          </div>
          <div class="relay-filter-row" aria-label="relay task status filters">
            <button class="relay-filter-chip active" type="button" data-filter="all">全部 <span>{task_count}</span></button>
            {filter_html}
          </div>
        </div>
        <div class="relay-task-list" aria-label="relay tasks">
          {task_list_html}
        </div>
      </section>
    </div>
  </main>
  <nav class="marvis-relay-bottom-nav" aria-label="Marvis relay navigation">
    {bottom_nav_html}
  </nav>
  </div>
  <script>
    document.querySelectorAll("[data-filter]").forEach((button) => {{
      button.addEventListener("click", () => {{
        const filter = button.dataset.filter || "all";
        document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("active", item === button));
        document.querySelectorAll(".relay-task-card").forEach((card) => {{
          card.hidden = filter !== "all" && card.dataset.status !== filter;
        }});
      }});
    }});
  </script>
</body>
</html>""")


def _relay_chat_home_page(
    *,
    selected_workspace: str = "",
    access_token: str = "",
) -> str:
    token_suffix = _token_suffix(access_token)
    selected_workspace = str(selected_workspace or "")
    workspace_label = Path(selected_workspace).name or selected_workspace or "wlcodex"
    topbar_html = _marvis_relay_topbar(
        title="Marvis",
        subtitle=workspace_label,
        back_href=f"/native{token_suffix}",
        right_html=f"""
          <a class="marvis-relay-icon-button" href="/native/workflows/relay/office{token_suffix}" aria-label="Marvis办公室">
            <span class="marvis-relay-icon-devices" aria-hidden="true"></span>
          </a>
          <a class="marvis-relay-icon-button" href="{escape(_relay_workspace_href(selected_workspace, access_token))}" aria-label="任务">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
          </a>
        """,
    )
    bottom_nav_html = _marvis_relay_bottom_nav(
        "chat",
        access_token=access_token,
        selected_workspace=selected_workspace,
    )
    composer_html = _marvis_relay_task_composer(
        token_suffix=token_suffix,
        selected_workspace=selected_workspace,
        access_token=access_token,
        placeholder="请在此输入任务",
    )
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light only">
  <title>Marvis 对话</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="{_RELAY_MARVIS_CSS_HREF}">
</head>
<body data-marvis-relay-view="chat">
  <div class="marvis-relay-phone">
    {topbar_html}
    <main class="marvis-relay-chat-home">
      <span class="marvis-relay-avatar marvis-relay-avatar-marvis marvis-relay-hero-avatar" aria-hidden="true"></span>
      <h2>你好，今天想做什么？</h2>
    </main>
    {composer_html}
    <nav class="marvis-relay-bottom-nav" aria-label="Marvis relay navigation">
      {bottom_nav_html}
    </nav>
  </div>
  <script>
    const TOKEN_SUFFIX = {json.dumps(token_suffix)};
    const marvisComposer = document.querySelector("[data-marvis-task-composer]");
    marvisComposer?.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const data = Object.fromEntries(new FormData(marvisComposer).entries());
      const title = String(data.title || "").trim();
      if (!title) return;
      data.title = title;
      data.prompt = title;
      const response = await fetch(`/api/relay/tasks${{TOKEN_SUFFIX}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(data),
      }});
      const payload = await response.json();
      if (payload?.task?.id) {{
        window.location.href = `/native/workflows/relay/tasks/${{encodeURIComponent(payload.task.id)}}${{TOKEN_SUFFIX}}`;
      }}
    }});
  </script>
</body>
</html>""")


def _relay_config_page(
    *,
    providers: list[dict[str, str]],
    relay_config: dict[str, Any] | None = None,
    selected_workspace: str = "",
    access_token: str = "",
) -> str:
    token_suffix = _token_suffix(access_token)
    relay_config = relay_config or {}
    config_providers = relay_config.get("providers")
    provider_rows = (
        config_providers if isinstance(config_providers, list) and config_providers else providers
    )
    role_config_html = _relay_role_config_html(relay_config, provider_rows)
    back_href = _relay_workspace_href(selected_workspace, access_token)
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>流式接力配置</title>
  <link rel="stylesheet" href="/static/base.css">
  <style>
    html {{ background: var(--bg-canvas); }}
    body {{ margin: 0; color: var(--text-primary); background: transparent; }}
    header {{ position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 48px 1fr auto; gap: 12px; align-items: center; padding: 12px 18px; background: rgba(5,5,8,.88); border-bottom: 1px solid var(--border-header); }}
    h1, h2 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 22px; }}
    main {{ display: grid; gap: 14px; width: min(960px, 100%); margin: 0 auto; padding: 18px; box-sizing: border-box; }}
    .relay-config-panel {{ display: grid; gap: 12px; border: 1px solid var(--border-card); border-radius: 8px; background: var(--bg-surface); padding: 14px; min-width: 0; }}
    .relay-config-copy, .relay-muted {{ margin: 0; color: var(--text-muted); font-size: 13px; line-height: 1.5; }}
    .relay-config-grid {{ display: grid; gap: 8px; }}
    .relay-config-row {{ display: grid; grid-template-columns: minmax(130px, 180px) minmax(150px, 220px) minmax(0, 1fr); gap: 10px; align-items: center; border: 1px solid var(--border-subtle); border-radius: 8px; padding: 10px; min-width: 0; }}
    .relay-provider-select {{ width: 100%; min-height: 38px; border: 1px solid var(--border-subtle); border-radius: 6px; padding: 8px 34px 8px 10px; background: #15161d; color: #f4f4f5; color-scheme: dark; appearance: none; background-image: linear-gradient(45deg, transparent 50%, #c9d1d9 50%), linear-gradient(135deg, #c9d1d9 50%, transparent 50%); background-position: calc(100% - 17px) 16px, calc(100% - 12px) 16px; background-size: 5px 5px, 5px 5px; background-repeat: no-repeat; }}
    .relay-provider-select:focus {{ outline: 2px solid rgba(88,166,255,.55); outline-offset: 2px; border-color: var(--color-link); }}
    .relay-provider-select option {{ background: #15161d; color: #f4f4f5; }}
    .relay-config-tools, .relay-role-chips {{ display: flex; gap: 6px; flex-wrap: wrap; min-width: 0; }}
    .relay-chip {{ border: 1px solid var(--border-subtle); border-radius: 999px; padding: 4px 8px; font-size: 12px; white-space: nowrap; color: var(--text-muted); }}
    .relay-primary, .relay-secondary {{ min-height: 38px; border-radius: 6px; padding: 0 12px; color: var(--text-primary); text-decoration: none; display: inline-grid; place-items: center; }}
    .relay-primary {{ border: 1px solid var(--color-link); background: rgba(88, 166, 255, .12); font-weight: var(--weight-bold); }}
    .relay-secondary {{ border: 1px solid var(--border-subtle); background: transparent; }}
    .relay-actions {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    @media (max-width: 760px) {{ header {{ grid-template-columns: 48px 1fr; }} .relay-actions {{ grid-column: 1 / -1; }} .relay-primary, .relay-secondary {{ width: 100%; }} .relay-config-row {{ grid-template-columns: 1fr; }} main {{ padding: 12px; }} }}
  </style>
</head>
<body>
  <header>
    <a class="circle" href="{escape(back_href)}" aria-label="back">‹</a>
    <h1>流式接力</h1>
  </header>
  <main>
    <section class="relay-config-panel" aria-label="relay role provider configuration">
      <h2>角色配置</h2>
      <p class="relay-config-copy">为固定五角色选择 agent provider；工具和能力来自团队配置，只读展示。新任务会快照保存后的配置。</p>
      <div class="relay-config-grid">{role_config_html}</div>
      <div class="relay-actions">
        <button class="relay-primary" id="save-relay-config" type="button">保存配置</button>
        <span class="relay-muted" id="relay-config-status">等待保存</span>
      </div>
    </section>
  </main>
  <script>
    const TOKEN_SUFFIX = {json.dumps(token_suffix)};
    const RELAY_HISTORY_HREF = {json.dumps(back_href)};
    const statusNode = document.getElementById("relay-config-status");
    document.getElementById("save-relay-config")?.addEventListener("click", async () => {{
      const assignments = {{}};
      document.querySelectorAll("[data-role-provider]").forEach((select) => {{
        assignments[select.dataset.roleProvider] = select.value;
      }});
      const response = await fetch(`/api/relay/config${{TOKEN_SUFFIX}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{assignments}}),
      }});
      if (!response.ok) {{
        const payload = await response.json().catch(() => ({{}}));
        statusNode.textContent = payload.error || "保存失败";
        return;
      }}
      statusNode.textContent = "配置已保存";
      window.location.href = RELAY_HISTORY_HREF;
    }});
  </script>
</body>
</html>""")


def _relay_workspace_nav_html(
    projects: list[Any],
    *,
    selected_workspace: str,
    access_token: str,
) -> str:
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for project in projects:
        cwd = str(project.get("cwd", "") or "")
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        rows.append((
            cwd,
            str(project.get("name") or Path(cwd).name or cwd),
            cwd,
        ))
    if selected_workspace and selected_workspace not in seen:
        rows.insert(0, (
            selected_workspace,
            Path(selected_workspace).name or selected_workspace,
            selected_workspace,
        ))
    links = "\n".join(
        '<a class="relay-workspace-link'
        f'{" active" if workspace == selected_workspace else ""}" '
        f'data-workspace-value="{escape(workspace)}" '
        f'href="{escape(_relay_workspace_href(workspace, access_token))}">'
        f'{escape(label)}</a>'
        for workspace, label, _path in rows
    )
    current = selected_workspace
    return f"""
      <section class="relay-workspace-nav" aria-label="relay workspace picker">
        <div class="relay-history-head">
          <div class="relay-history-title">
            <h2>工作区</h2>
            <span class="relay-muted">当前：{escape(Path(current).name or current)}</span>
          </div>
        </div>
        <div class="relay-workspace-row">{links}</div>
      </section>
    """


def _relay_workspace_href(workspace: str, access_token: str) -> str:
    params = []
    if access_token:
        params.append(f"token={quote(access_token)}")
    if workspace:
        params.append(f"workspace={quote(workspace)}")
    suffix = "?" + "&".join(params) if params else ""
    return f"/native/workflows/relay{suffix}"


def _relay_task_view_href(task_id: int, access_token: str, view: str) -> str:
    params = []
    if access_token:
        params.append(f"token={quote(access_token, safe='')}")
    params.append(f"view={quote(_relay_task_detail_view(view), safe='')}")
    suffix = "?" + "&".join(params)
    return f"/native/workflows/relay/tasks/{task_id}{suffix}"


def _relay_task_events_suffix(access_token: str, after: int) -> str:
    params = []
    if access_token:
        params.append(f"token={quote(access_token, safe='')}")
    if after > 0:
        params.append(f"after={after}")
    return "?" + "&".join(params) if params else ""


def _relay_config_href(workspace: str, access_token: str) -> str:
    params = []
    if access_token:
        params.append(f"token={quote(access_token)}")
    if workspace:
        params.append(f"workspace={quote(workspace)}")
    suffix = "?" + "&".join(params) if params else ""
    return f"/native/workflows/relay/config{suffix}"


def _relay_project_rows() -> list[dict[str, str]]:
    payload = _council_projects_payload()
    return [
        project
        for project in payload.get("projects", [])
        if str(project.get("cwd", "") or "")
    ]


def _relay_default_workspace(projects: list[Any]) -> str:
    preferred = str(_COUNCIL_PROJECTS_ROOT / "wlcodex")
    for project in projects:
        cwd = str(project.get("cwd", "") or "")
        if cwd == preferred:
            return cwd
    for project in projects:
        cwd = str(project.get("cwd", "") or "")
        if cwd:
            return cwd
    return preferred


def _relay_selected_workspace(requested_workspace: str, projects: list[Any]) -> str:
    requested = str(requested_workspace or "").strip()
    return requested or _relay_default_workspace(projects)


def _relay_role_summary_html(
    relay_config: dict[str, Any],
    providers: list[Any],
) -> str:
    assignments = relay_config.get("assignments")
    assignment_map = assignments if isinstance(assignments, dict) else {}
    provider_rows = providers or [{"provider": "codex", "provider_engine": ""}]
    fallback = str(provider_rows[0].get("provider") or "codex")
    return "\n".join(
        f'<span class="relay-chip">{escape(_relay_role_label(role))} · '
        f'{escape(_native_provider_display_name(str(assignment_map.get(role) or fallback)))}</span>'
        for role in RELAY_ROLE_IDS
    )


def _marvis_relay_avatar_html(role: str, *, label: str = "") -> str:
    role_name = str(role or "marvis").strip() or "marvis"
    alt = label or _relay_role_label(role_name)
    return (
        f'<span class="marvis-relay-avatar marvis-relay-avatar-{escape(role_name)}" '
        f'aria-label="{escape(alt)}"></span>'
    )


_MARVIS_RELAY_ROLE_PERSONAS: dict[str, tuple[str, str]] = {
    "director": ("marvis", "Marvis"),
    "implementer": ("implementer", "开发工程师"),
    "architect": ("architect", "架构工程师"),
    "tester": ("tester", "测试工程师"),
    "auditor": ("auditor", "审核工程师"),
}

_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS: dict[str, tuple[tuple[str, str], ...]] = {
    "implementer": (("App", "Agent"),),
    "architect": (("Computer", "Agent"),),
    "tester": (("Search", "Agent"),),
    "auditor": (("File", "Agent"), ("Browser", "Agent")),
}

_MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS: dict[str, tuple[tuple[str, str], ...]] = {
    "implementer": (("app", "agent"),),
    "architect": (("computer", "agent"),),
    "tester": (("search", "agent"),),
    "auditor": (("file", "agent"), ("browser", "agent")),
}


def _marvis_relay_public_role(role: str) -> tuple[str, str]:
    return _MARVIS_RELAY_ROLE_PERSONAS.get(
        str(role or "").strip(),
        ("marvis", "Marvis"),
    )


def _marvis_relay_handoff_role_label(role: str) -> str:
    return _marvis_relay_public_role(role)[1]


def _marvis_relay_handoff_text(from_role: str, to_role: str) -> str:
    to_name = _marvis_relay_handoff_role_label(to_role)
    if from_role == "director":
        return f"Marvis拍了拍 {to_name}，说开干吧"
    from_name = _marvis_relay_handoff_role_label(from_role)
    if to_role == "auditor":
        return f"{from_name}交给{to_name}复核"
    if from_role == "auditor":
        return f"{from_name}退回{to_name}继续处理"
    return f"{from_name}交给{to_name}继续处理"


def _marvis_relay_legacy_persona_label(role: str) -> str:
    labels = _MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS.get(str(role or "").strip(), ())
    return " ".join(labels[0]) if labels else ""


def _relay_replace_legacy_role_display_names(text: str) -> str:
    value = str(text or "")
    for role in RELAY_ROLE_IDS:
        current_label = _marvis_relay_public_role(role)[1]
        for label_parts in _MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS.get(role, ()):
            legacy_label = " ".join(label_parts)
            if legacy_label and legacy_label != current_label:
                value = value.replace(legacy_label, current_label)
    return value


def _relay_replace_legacy_role_identifiers(text: str) -> str:
    value = _relay_replace_legacy_role_display_names(text)
    for role in RELAY_ROLE_IDS:
        current_slug = _marvis_relay_public_role(role)[0]
        for slug_parts in _MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS.get(role, ()):
            legacy_slug = "-".join(slug_parts)
            if legacy_slug and legacy_slug != current_slug:
                value = value.replace(legacy_slug, current_slug)
    return value


def _marvis_relay_role_status_label(status: str) -> str:
    value = str(status or "").strip()
    if value in {"passed", "completed", "success", "succeeded"}:
        return "已完成"
    if value in {"failed", "blocked", "error"}:
        return "调用失败"
    if value in {"queued", "streaming", "running", "started", "progress"}:
        return "进行中"
    if value == "waiting_user":
        return "等待中"
    if value == "interrupted":
        return "已中断"
    return _relay_role_status_label(value) if value else "进行中"


def _marvis_relay_action_label(role: str, payload: dict[str, Any] | None = None) -> str:
    artifact_type = str((payload or {}).get("artifact_type") or "").strip()
    if role == "director":
        return "任务分配"
    if artifact_type:
        return _marvis_relay_role_status_label(artifact_type) if artifact_type in {"passed", "failed", "blocked", "completed"} else artifact_type.replace("_", " ")
    return "任务"


def _marvis_relay_topbar(
    *,
    title: str = "Marvis",
    subtitle: str = "",
    back_href: str = "",
    right_html: str = "",
) -> str:
    left = (
        f'<a class="marvis-relay-menu is-back" href="{escape(back_href)}" aria-label="返回上一级">'
        '<span></span><span></span><span></span></a>'
        if back_href
        else '<button class="marvis-relay-menu" type="button" aria-label="菜单"><span></span><span></span><span></span></button>'
    )
    subtitle_html = (
        f'<div class="marvis-relay-device"><span class="marvis-relay-dot"></span>{escape(subtitle)}</div>'
        if subtitle
        else ""
    )
    actions = right_html or """
      <button class="marvis-relay-icon-button" type="button" aria-label="设备">
        <span class="marvis-relay-icon-devices" aria-hidden="true"></span>
      </button>
      <button class="marvis-relay-icon-button" type="button" aria-label="任务">
        <span class="marvis-relay-icon-list" aria-hidden="true"></span>
      </button>
    """
    return f"""
    <header class="marvis-relay-topbar">
      {left}
      <div class="marvis-relay-brand">
        <h1>{escape(title)}</h1>
        {subtitle_html}
      </div>
      <div class="marvis-relay-actions">{actions}</div>
    </header>
    """


def _marvis_relay_bottom_nav(
    active: str = "chat",
    *,
    access_token: str = "",
    selected_workspace: str = "",
) -> str:
    token_suffix = _token_suffix(access_token)
    workspace_query = ""
    if selected_workspace:
        joiner = "&" if token_suffix else "?"
        workspace_query = f"{joiner}workspace={quote(selected_workspace, safe='/')}"
    hrefs = {
        "chat": f"/native/workflows/relay/chat{token_suffix}{workspace_query}",
        "tasks": f"/native/workflows/relay{token_suffix}{workspace_query}",
    }
    items = [
        ("chat", "对话", "chat"),
        ("tasks", "任务", "clock"),
        ("skills", "技能", "tool"),
        ("profile", "我的", "person"),
    ]
    rows = []
    for key, label, icon in items:
        class_name = f"marvis-relay-nav-item{' active' if key == active else ''}"
        current = ' aria-current="page"' if key == active else ""
        icon_html = _marvis_relay_nav_icon_html(icon)
        if key in hrefs:
            rows.append(
                f"""
        <a class="{class_name}" href="{escape(hrefs[key])}" data-marvis-nav="{escape(key)}"{current}>
          {icon_html}
          <span>{escape(label)}</span>
        </a>
        """
            )
        else:
            rows.append(
                f"""
        <button class="{class_name}" type="button" data-marvis-nav="{escape(key)}"{current}>
          {icon_html}
          <span>{escape(label)}</span>
        </button>
        """
            )
    return "\n".join(rows)


def _marvis_relay_nav_icon_html(icon: str) -> str:
    icons = {
        "chat": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
        "clock": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
        "tool": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
        "person": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
    }
    return f'<span class="marvis-relay-nav-icon" aria-hidden="true">{icons.get(icon, icons["chat"])}</span>'


def _marvis_relay_task_composer(
    *,
    token_suffix: str,
    selected_workspace: str,
    access_token: str = "",
    placeholder: str = "请输入任务",
) -> str:
    workspace_dock = _marvis_relay_workspace_dock(
        selected_workspace,
        access_token=access_token,
    )
    return f"""
    {workspace_dock}
    <form class="marvis-relay-composer" data-marvis-task-composer action="/api/relay/tasks{token_suffix}">
      <button class="marvis-relay-plus" type="button" aria-label="添加">+</button>
      <input name="title" autocomplete="off" placeholder="{escape(placeholder)}">
      <input type="hidden" name="prompt" value="">
      <input type="hidden" name="workspace" value="{escape(selected_workspace)}">
      <button class="marvis-relay-submit" type="submit" aria-label="发送任务" data-marvis-submit>
        <span class="marvis-relay-submit-arrow" aria-hidden="true">↑</span>
        <span class="marvis-relay-submit-stop" aria-hidden="true">■</span>
      </button>
    </form>
    """


def _marvis_relay_workspace_dock(workspace: str, *, access_token: str = "") -> str:
    workspace = str(workspace or "")
    label = Path(workspace).name or workspace or "选择工作区"
    href = _relay_workspace_href(workspace, access_token)
    return f"""
    <div class="marvis-relay-workspace-dock" aria-label="当前工作区">
      <span class="marvis-relay-workspace-folder" aria-hidden="true"></span>
      <span class="marvis-relay-workspace-label">工作区</span>
      <a class="marvis-relay-workspace-chip" href="{escape(href)}" title="{escape(workspace or label)}">
        <span class="marvis-relay-workspace-name">{escape(label)}</span>
        <span class="marvis-relay-workspace-action">选择</span>
      </a>
    </div>
    """


def _marvis_relay_office_roles(relay_config: dict[str, Any] | None) -> list[dict[str, str]]:
    config = relay_config if isinstance(relay_config, dict) else {}
    raw_roles = config.get("configured_roles") or config.get("roles")
    role_entries: list[Any]
    if isinstance(raw_roles, list) and raw_roles:
        role_entries = raw_roles
    else:
        assignments = config.get("assignments")
        if isinstance(assignments, dict) and assignments:
            role_entries = [{"role": role} for role in assignments]
        else:
            role_entries = [{"role": role} for role in RELAY_ROLE_IDS]

    roles: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in role_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue
        role = str(entry.get("role") or "").strip()
        if role not in RELAY_ROLE_IDS or role in seen:
            continue
        seen.add(role)
        roles.append(
            {
                "role": role,
                "display_name": str(
                    entry.get("display_name") or _relay_role_label(role)
                ),
            }
        )
    return roles[:6]


_MARVIS_OFFICE_PERSONAS: dict[str, dict[str, Any]] = {
    "director": {
        "title": "总工程师",
        "intro": "团队队长，全盘统筹，负责拆任务、派角色、收结果，并把接力过程汇成你能直接看的结论。",
        "skills": ("分派任务", "汇总结果呈现", "调度接力", "读写文档", "写代码"),
    },
    "architect": {
        "title": "架构工程师",
        "intro": "负责把需求拆成结构、边界和风险点，先看清怎么做，再把可执行方案交给后续角色。",
        "skills": ("方案设计", "影响分析", "模块边界", "技术取舍"),
    },
    "implementer": {
        "title": "开发工程师",
        "intro": "负责把方案落到代码里，按现有工程风格实现功能、修复问题，并尽量保持改动范围收敛。",
        "skills": ("写代码", "改页面", "接 API", "修复缺陷"),
    },
    "tester": {
        "title": "测试工程师",
        "intro": "负责验证行为是不是符合预期，补关键测试，复查手机端、路由、流式状态这些容易回归的地方。",
        "skills": ("跑测试", "回归验证", "边界用例", "复现问题"),
    },
    "auditor": {
        "title": "审核工程师",
        "intro": "负责最后审一遍风险、遗漏和上线边界，确认改动没有影响 Codex、Claude、Antigravity 等其他页面。",
        "skills": ("代码审查", "风险检查", "上线把关", "回滚判断"),
    },
}

_MARVIS_OFFICE_AVATARS: dict[str, str] = {
    "director": "marvis",
    "architect": "architect",
    "implementer": "implementer",
    "tester": "tester",
    "auditor": "auditor",
}


def _marvis_relay_office_persona(
    role: str,
    *,
    display_name: str,
    provider: str = "",
) -> dict[str, Any]:
    persona = _MARVIS_OFFICE_PERSONAS.get(role, {})
    title = str(persona.get("title") or display_name or "接力角色")
    provider_id = provider.strip()
    provider_label = _native_provider_display_name(provider_id)
    return {
        "role": role,
        "display_name": display_name,
        "title": title,
        "provider": provider_id,
        "provider_label": provider_label,
        "intro": str(persona.get("intro") or f"{display_name}正在处理分配给自己的接力任务。"),
        "skills": list(persona.get("skills") or (display_name,)),
        "avatar": _MARVIS_OFFICE_AVATARS.get(role, role),
    }


def _marvis_relay_office_page(
    *,
    access_token: str = "",
    relay_config: dict[str, Any] | None = None,
    token_stats: dict[str, Any] | None = None,
) -> str:
    token_suffix = _token_suffix(access_token)
    config = relay_config if isinstance(relay_config, dict) else {}
    stats = token_stats if isinstance(token_stats, dict) else {}
    consumed_tokens = _marvis_token_int(stats.get("consumed_tokens"))
    total_consumed_tokens = _marvis_token_int(stats.get("total_consumed_tokens"))
    consumed_label = _format_marvis_token_count(consumed_tokens)
    total_consumed_label = _format_marvis_token_count(total_consumed_tokens)
    assignment_map = config.get("assignments")
    assignments = assignment_map if isinstance(assignment_map, dict) else {}
    assignment_payload = {
        role: str(assignments.get(role) or "")
        for role in RELAY_ROLE_IDS
    }
    provider_rows = config.get("providers")
    provider_source = provider_rows if isinstance(provider_rows, list) else []
    provider_options: list[dict[str, str]] = []
    seen_providers: set[str] = set()
    for provider in provider_source:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("provider") or "").strip()
        if not provider_id or provider_id in seen_providers:
            continue
        seen_providers.add(provider_id)
        provider_options.append(
            {
                "provider": provider_id,
                "label": _native_provider_display_name(provider_id),
            }
        )
    for provider_id in assignment_payload.values():
        provider_id = str(provider_id or "").strip()
        if provider_id and provider_id not in seen_providers:
            seen_providers.add(provider_id)
            provider_options.append(
                {
                    "provider": provider_id,
                    "label": _native_provider_display_name(provider_id),
                }
            )
    if not provider_options:
        provider_options.append({"provider": "codex", "label": "Codex"})
        assignment_payload = {role: "codex" for role in RELAY_ROLE_IDS}
    provider_options_html = "\n".join(
        '<button class="marvis-persona-model-option" type="button" '
        f'data-provider-option="{escape(option["provider"])}">'
        f'{escape(option["label"])}</button>'
        for option in provider_options
    )
    provider_options_json = json.dumps(
        provider_options,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    assignment_json = json.dumps(
        assignment_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    active_roles = _marvis_relay_office_roles(relay_config)
    role_personas = {
        role["role"]: _marvis_relay_office_persona(
            role["role"],
            display_name=role["display_name"],
            provider=str(assignment_payload.get(role["role"]) or ""),
        )
        for role in active_roles
    }
    role_personas_json = json.dumps(
        role_personas,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    role_count = min(len(active_roles), 6)
    office_slots: list[str] = []
    for index in range(6):
        slot_class = f"marvis-office-hotspot marvis-office-hotspot-{index + 1}"
        if index < len(active_roles):
            role = active_roles[index]
            office_slots.append(
                f"""
        <button class="{slot_class}" type="button" data-marvis-office-role="{escape(role['role'])}" data-marvis-persona-open="{escape(role['role'])}" aria-label="打开{escape(role['display_name'])}人设">
          <span>{escape(role['display_name'])}</span>
        </button>
        """
            )
        else:
            office_slots.append(
                f"""
        <div class="{slot_class} marvis-office-empty" aria-hidden="true"></div>
        """
            )
    office_slots_html = "\n".join(office_slots)
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light only">
  <title>Marvis办公室</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="{_RELAY_MARVIS_CSS_HREF}">
</head>
<body data-marvis-relay-view="office">
  <div class="marvis-relay-phone marvis-office-shell">
    <header class="marvis-office-topbar">
      <a class="marvis-office-back" href="/native/workflows/relay{token_suffix}" aria-label="返回"></a>
      <h1>Marvis办公室</h1>
      <span aria-hidden="true"></span>
    </header>
    <main class="marvis-office-main">
      <section class="marvis-office-scene" aria-label="Marvis办公室工位">
        <img class="marvis-office-scene-img" src="/static/marvis/office-scene-roles-{role_count}.png?v=20260627-red-director" alt="" loading="eager">
        <div class="marvis-office-layer">
          {office_slots_html}
        </div>
      </section>
      <section class="marvis-office-token-row" aria-label="Token统计" data-marvis-token-stats data-token-endpoint="/api/relay/token-stats{token_suffix}">
        <div class="marvis-office-token-card">
          <span>今日消耗Token</span>
          <strong data-token-consumed="{consumed_tokens}"><b data-token-consumed-label>{escape(consumed_label)}</b> <span class="marvis-token-beans" aria-hidden="true"><span></span><span></span></span></strong>
        </div>
        <div class="marvis-office-token-card">
          <span>总消耗Token</span>
          <strong data-token-total="{total_consumed_tokens}"><b data-token-total-label>{escape(total_consumed_label)}</b> <span class="marvis-token-beans" aria-hidden="true"><span></span><span></span></span></strong>
        </div>
      </section>
    </main>
    <div class="marvis-office-backdrop" data-marvis-persona-backdrop hidden></div>
    <section class="marvis-persona-modal" data-marvis-persona-modal hidden aria-modal="true" role="dialog" aria-label="Marvis（马维斯）人设">
      <button class="marvis-persona-close" type="button" data-marvis-persona-close aria-label="关闭"></button>
      <header class="marvis-persona-head">
        <span class="marvis-persona-avatar marvis-relay-avatar marvis-relay-avatar-marvis" data-persona-avatar aria-hidden="true"></span>
        <div>
          <h2 data-persona-name>Marvis（马维斯）</h2>
          <p data-persona-title>总工程师</p>
          <small>◷ 空闲中</small>
        </div>
      </header>
      <div class="marvis-persona-divider"></div>
      <div class="marvis-persona-content">
        <p class="marvis-persona-label">简介:</p>
        <p class="marvis-persona-intro" data-persona-intro></p>
        <p class="marvis-persona-label">技能:</p>
        <div class="marvis-persona-skills" data-persona-skills></div>
        <div class="marvis-persona-actions">
          <button class="marvis-persona-model-button" type="button" data-persona-model-toggle>设置大脑</button>
          <span class="marvis-persona-model-status" data-persona-model-status></span>
        </div>
        <section class="marvis-persona-model-panel" data-persona-model-panel hidden aria-label="设置角色大脑">
          <p class="marvis-persona-label">大脑:</p>
          <div class="marvis-persona-model-options" data-persona-model-options>
            {provider_options_html}
          </div>
        </section>
      </div>
    </section>
  </div>
  <script>
    (() => {{
      const TOKEN_SUFFIX = {json.dumps(token_suffix)};
      const personas = {role_personas_json};
      const providers = {provider_options_json};
      let assignments = {assignment_json};
      const openButtons = document.querySelectorAll("[data-marvis-persona-open]");
      const modal = document.querySelector("[data-marvis-persona-modal]");
      const backdrop = document.querySelector("[data-marvis-persona-backdrop]");
      const closeButtons = document.querySelectorAll("[data-marvis-persona-close]");
      const avatar = document.querySelector("[data-persona-avatar]");
      const name = document.querySelector("[data-persona-name]");
      const title = document.querySelector("[data-persona-title]");
      const intro = document.querySelector("[data-persona-intro]");
      const skills = document.querySelector("[data-persona-skills]");
      const modelToggle = document.querySelector("[data-persona-model-toggle]");
      const modelPanel = document.querySelector("[data-persona-model-panel]");
      const modelStatus = document.querySelector("[data-persona-model-status]");
      const modelOptions = document.querySelector("[data-persona-model-options]");
      let activeRole = "";
      const providerLabel = (provider) => {{
        const found = providers.find((item) => item.provider === provider);
        return found ? found.label : (provider || "Native");
      }};
      const updatePersonaProvider = (role, provider) => {{
        const persona = personas[role];
        if (!persona) return;
        persona.provider = provider;
        persona.provider_label = providerLabel(provider);
      }};
      const renderProviderOptions = () => {{
        if (!modelOptions || !activeRole) return;
        const selected = assignments[activeRole] || "";
        modelOptions.querySelectorAll("[data-provider-option]").forEach((button) => {{
          const provider = button.getAttribute("data-provider-option") || "";
          button.classList.toggle("selected", provider === selected);
          button.setAttribute("aria-pressed", provider === selected ? "true" : "false");
        }});
        if (modelStatus) modelStatus.textContent = selected ? `当前：${{providerLabel(selected)}}` : "当前：未设置";
      }};
      const renderPersona = (role) => {{
        const persona = personas[role];
        if (!persona) return false;
        activeRole = role;
        if (avatar) avatar.className = `marvis-persona-avatar marvis-relay-avatar marvis-relay-avatar-${{persona.avatar || role}}`;
        if (name) name.textContent = persona.display_name || role;
        if (title) title.textContent = persona.title || "Relay Agent";
        if (intro) intro.textContent = persona.intro || "";
        if (skills) {{
          skills.textContent = "";
          (persona.skills || []).forEach((skill) => {{
            const chip = document.createElement("span");
            chip.textContent = skill;
            skills.appendChild(chip);
          }});
        }}
        if (modelPanel) modelPanel.hidden = true;
        renderProviderOptions();
        return true;
      }};
      const setOpen = (isOpen) => {{
        if (!modal || !backdrop) return;
        modal.hidden = !isOpen;
        backdrop.hidden = !isOpen;
        document.body.classList.toggle("marvis-office-modal-open", isOpen);
      }};
      openButtons.forEach((button) => button.addEventListener("click", () => {{
        const role = button.getAttribute("data-marvis-persona-open") || "";
        if (renderPersona(role)) setOpen(true);
      }}));
      backdrop?.addEventListener("click", () => setOpen(false));
      closeButtons.forEach((button) => button.addEventListener("click", () => setOpen(false)));
      window.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") setOpen(false);
      }});
      modelToggle?.addEventListener("click", () => {{
        if (!modelPanel) return;
        modelPanel.hidden = !modelPanel.hidden;
        renderProviderOptions();
      }});
      modelOptions?.addEventListener("click", async (event) => {{
        const target = event.target instanceof HTMLElement ? event.target.closest("[data-provider-option]") : null;
        if (!target || !activeRole) return;
        const provider = target.getAttribute("data-provider-option") || "";
        if (!provider) return;
        const nextAssignments = {{...assignments, [activeRole]: provider}};
        if (modelStatus) modelStatus.textContent = "保存中...";
        try {{
          const response = await fetch(`/api/relay/config${{TOKEN_SUFFIX}}`, {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{assignments: nextAssignments}}),
          }});
          if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
          const payload = await response.json();
          assignments = payload.assignments || nextAssignments;
          updatePersonaProvider(activeRole, assignments[activeRole]);
          if (title) title.textContent = personas[activeRole]?.title || "Relay Agent";
          renderProviderOptions();
        }} catch (error) {{
          if (modelStatus) modelStatus.textContent = "保存失败，请重试";
        }}
      }});
      const tokenStats = document.querySelector("[data-marvis-token-stats]");
      const consumed = document.querySelector("[data-token-consumed]");
      const consumedLabel = document.querySelector("[data-token-consumed-label]");
      const total = document.querySelector("[data-token-total]");
      const totalLabel = document.querySelector("[data-token-total-label]");
      const formatToken = (value) => {{
        const number = Number(value || 0);
        if (!Number.isFinite(number) || number <= 0) return "0";
        if (number >= 100000000) return `${{(number / 100000000).toFixed(1).replace(/\\.0$/, "")}}亿`;
        if (number >= 10000) return `${{(number / 10000).toFixed(1).replace(/\\.0$/, "")}}万`;
        return Math.round(number).toLocaleString("en-US");
      }};
      const applyTokenStats = (stats) => {{
        const used = Number(stats && stats.consumed_tokens || 0);
        const totalUsed = Number(stats && stats.total_consumed_tokens || used || 0);
        if (consumed) {{
          consumed.dataset.tokenConsumed = String(Math.max(0, Math.round(used)));
        }}
        if (total) {{
          total.dataset.tokenTotal = String(Math.max(0, Math.round(totalUsed)));
        }}
        if (consumedLabel) consumedLabel.textContent = formatToken(used);
        if (totalLabel) totalLabel.textContent = formatToken(totalUsed);
      }};
      const refreshTokenStats = async () => {{
        if (!tokenStats) return;
        const endpoint = tokenStats.getAttribute("data-token-endpoint");
        if (!endpoint) return;
        try {{
          const response = await fetch(endpoint, {{headers: {{"Accept": "application/json"}}}});
          if (!response.ok) return;
          applyTokenStats(await response.json());
        }} catch (_error) {{
          // Keep the last good values; the office should stay quiet if stats lag.
        }}
      }};
      refreshTokenStats();
      window.setInterval(refreshTokenStats, 2000);
    }})();
  </script>
</body>
</html>""")


def _marvis_token_int(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _format_marvis_token_count(value: int) -> str:
    count = max(0, int(value))
    if count >= 100_000_000:
        return f"{count / 100_000_000:.1f}".removesuffix(".0") + "亿"
    if count >= 10_000:
        return f"{count / 10_000:.1f}".removesuffix(".0") + "万"
    return f"{count:,}"


def _format_marvis_relay_token_count(value: int) -> str:
    count = max(0, int(value))
    if count >= 1000:
        return f"{round(count / 1000)}K"
    return str(count)


def _marvis_relay_followup_composer(
    *,
    task_id: int,
    placeholder: str = "请在此输入任务",
    workspace: str = "",
    access_token: str = "",
    task_status: str = "",
) -> str:
    workspace_dock = _marvis_relay_workspace_dock(
        workspace,
        access_token=access_token,
    )
    return f"""
    {workspace_dock}
    <form class="marvis-relay-composer" data-marvis-followup-composer data-task-status-value="{escape(task_status)}" data-interrupt-url="/api/relay/tasks/{task_id}/interrupt" method="post" action="/api/relay/tasks/{task_id}/message" onsubmit="return false">
      <button class="marvis-relay-plus" type="button" aria-label="添加">+</button>
      <textarea name="text" placeholder="{escape(placeholder)}" aria-label="继续补充给总工程师"></textarea>
      <button class="marvis-relay-submit" type="submit" aria-label="发送补充" data-marvis-submit>
        <span class="marvis-relay-submit-arrow" aria-hidden="true">↑</span>
        <span class="marvis-relay-submit-stop" aria-hidden="true">■</span>
      </button>
    </form>
    """


@dataclass
class WorkLogEntry:
    kind: str
    key: str
    text: str = ""
    chip: str = ""
    output: str = ""
    failed: bool = False
    replace_text: bool = False


@dataclass
class WorkLogSegment:
    role: str
    persona: str
    display_name: str
    entries: list[WorkLogEntry]


def _marvis_relay_work_log_html(
    *,
    body_html: str,
    token_text: str = "0",
    token_total: int = 0,
    max_event_id: int = 0,
) -> str:
    return f"""
    <div class="marvis-relay-backdrop" data-marvis-work-log-backdrop hidden></div>
    <section class="marvis-work-log" data-marvis-work-log data-marvis-work-log-max-event-id="{max(0, int(max_event_id))}" aria-label="工作日志">
      <button class="marvis-work-log-close" type="button" data-marvis-close-log aria-label="关闭">×</button>
      <div class="marvis-work-log-tabs">
        <button class="marvis-work-log-tab active" type="button">工作日志</button>
        <button class="marvis-work-log-tab" type="button">产出物</button>
      </div>
      <div class="marvis-work-log-hero">
        <div class="marvis-work-log-desks" aria-hidden="true">
          <img src="/static/marvis/office-desk-worker-1.png" alt="">
          <img src="/static/marvis/office-desk-empty-slot.png" alt="">
          <img src="/static/marvis/office-desk-empty-slot.png" alt="">
        </div>
        <div class="marvis-work-log-metrics">
          <span>空闲中...</span>
          <strong data-marvis-work-log-token-value data-token-total="{max(0, int(token_total))}">{escape(token_text)} ☕</strong>
        </div>
      </div>
      <div class="marvis-work-log-body" data-marvis-work-log-body>{body_html}</div>
    </section>
    """


def _marvis_relay_token_total_from_events(
    role_jobs: list[Any],
    *,
    hub: WorkerLiveStreamHub | None,
) -> int:
    total = 0
    for _occurred_at, _event_id, _role, _display_name, worker_event in _relay_worker_events_for_roles(
        role_jobs,
        hub=hub,
    ):
        total += _marvis_relay_usage_event_total(dict(worker_event.payload or {}))
    return total


def _marvis_relay_max_event_id_from_events(
    role_jobs: list[Any],
    *,
    hub: WorkerLiveStreamHub | None,
) -> int:
    max_event_id = 0
    for _occurred_at, event_id, _role, _display_name, _worker_event in _relay_worker_events_for_roles(
        role_jobs,
        hub=hub,
    ):
        max_event_id = max(max_event_id, int(event_id))
    return max_event_id


def _marvis_relay_token_text_from_events(
    role_jobs: list[Any],
    *,
    hub: WorkerLiveStreamHub | None,
) -> str:
    return _format_marvis_relay_token_count(
        _marvis_relay_token_total_from_events(role_jobs, hub=hub)
    )


def _marvis_relay_usage_event_total(payload: dict[str, Any]) -> int:
    usage = payload.get("usage")
    total_usage = payload.get("total")
    candidates = [payload]
    if isinstance(usage, dict):
        candidates.append(usage)
        nested_total = usage.get("total")
        if isinstance(nested_total, dict):
            candidates.append(nested_total)
    if isinstance(total_usage, dict):
        candidates.append(total_usage)

    for candidate in candidates:
        for key in ("total_tokens", "tokens", "consumed_tokens"):
            value = candidate.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                return int(value)

    fallback_total = 0
    for candidate in candidates:
        for key in ("input_tokens", "output_tokens", "reasoning_output_tokens"):
            value = candidate.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                fallback_total += int(value)
        if fallback_total > 0:
            return fallback_total
    return 0


def _marvis_relay_work_log_body_html(
    detail: Any,
    *,
    hub: WorkerLiveStreamHub | None,
    canonical_payloads: dict[str, dict[str, Any]] | None = None,
) -> str:
    segments = _marvis_relay_work_log_segments(
        detail,
        hub=hub,
        canonical_payloads=canonical_payloads,
    )
    rows: list[str] = []
    for index, segment in enumerate(segments):
        rows.append(_marvis_relay_work_log_segment_html(segment, index=index))
    if not rows:
        return '<p class="marvis-work-log-empty">暂无工作日志</p>'
    rows.append(
        """
      <section class="marvis-work-log-artifacts" data-marvis-work-log-artifacts>
        <h3>产出物</h3>
        <p>暂无产出物</p>
      </section>
        """
    )
    return "\n".join(rows)


def _marvis_relay_work_log_segments(
    detail: Any,
    *,
    hub: WorkerLiveStreamHub | None,
    canonical_payloads: dict[str, dict[str, Any]] | None = None,
) -> list[WorkLogSegment]:
    canonical_payloads = canonical_payloads or {}
    role_errors = _marvis_relay_role_error_payloads_by_role(
        getattr(detail, "artifacts", []) or []
    )
    artifact_payloads = _marvis_relay_summary_payloads_by_role(
        getattr(detail, "artifacts", []) or []
    )
    events = _relay_worker_events_for_roles(detail.role_jobs, hub=hub)
    protocol_completed_keys = {
        _relay_native_message_key(role, worker_event, bucket="assistant")
        for _occurred_at, _event_id, role, _display_name, worker_event in events
        if worker_event.kind == "message_completed"
        and _marvis_relay_work_log_text_is_protocol_noise(
            _relay_native_event_text(worker_event)
        )
    }
    segments: list[WorkLogSegment] = []
    entry_maps: list[dict[str, WorkLogEntry]] = []

    def append_segment(role: str) -> WorkLogSegment:
        persona, display_name = _marvis_relay_public_role(role)
        segment = WorkLogSegment(
            role=role,
            persona=persona,
            display_name=display_name,
            entries=[],
        )
        segments.append(segment)
        entry_maps.append({})
        return segment

    def append_entry(role: str, entry: WorkLogEntry) -> None:
        if not role or role not in RELAY_ROLE_IDS:
            return
        segment = segments[-1] if segments and segments[-1].role == role else append_segment(role)
        key_map = entry_maps[-1]
        existing = key_map.get(entry.key) if entry.key else None
        if existing is None:
            segment.entries.append(entry)
            if entry.key:
                key_map[entry.key] = entry
            return
        _marvis_relay_merge_work_log_entry(existing, entry)

    for _occurred_at, _event_id, role, _display_name, worker_event in events:
        if (
            worker_event.kind == "text_delta"
            and _relay_native_message_key(role, worker_event, bucket="assistant")
            in protocol_completed_keys
        ):
            continue
        entry = _marvis_relay_work_log_entry_from_event(role, worker_event)
        if entry is not None:
            append_entry(role, entry)

    _marvis_relay_finalize_work_log_segments(segments)
    existing_roles = {segment.role for segment in segments}
    for job in detail.role_jobs:
        role = str(getattr(job, "role", "") or "")
        if role not in RELAY_ROLE_IDS:
            continue
        if role in existing_roles:
            payload = None
        else:
            payload = canonical_payloads.get(role) or artifact_payloads.get(role)
            fallback_payload = artifact_payloads.get(role)
            if (
                payload is not None
                and fallback_payload is not None
                and _marvis_relay_work_log_text_is_protocol_noise(
                    str(payload.get("summary") or payload.get("output") or "")
                )
            ):
                payload = fallback_payload
        role_error = role_errors.get(role) or {}
        error_message = str(
            getattr(job, "error_message", "")
            or role_error.get("error")
            or role_error.get("summary")
            or role_error.get("message")
            or ""
        ).strip()
        status = str(getattr(job, "status", "") or "")
        if payload is not None:
            append_entry(
                role,
                WorkLogEntry(
                    kind="artifact",
                    key=f"artifact:{role}",
                    text=_relay_humanize_role_envelope(payload),
                    chip=f"{_marvis_relay_action_label(role, payload)} {_marvis_relay_role_status_label(str(payload.get('status') or status or 'passed'))}",
                ),
            )
            existing_roles.add(role)
        if error_message:
            append_entry(
                role,
                WorkLogEntry(
                    kind="error",
                    key=f"error:{role}",
                    text=f"{_relay_role_label(role)}执行问题：{_relay_humanize_display_text(error_message)}",
                    chip="调用失败",
                    failed=True,
                ),
            )
            existing_roles.add(role)
        if (
            role not in existing_roles
            and status
            and status not in {"idle", "passed", "completed"}
        ):
            _persona, display_name = _marvis_relay_public_role(role)
            append_entry(
                role,
                WorkLogEntry(
                    kind="status",
                    key=f"status:{role}:{status}",
                    text=f"{display_name} {_marvis_relay_role_status_label(status)}",
                ),
            )
            existing_roles.add(role)

    _marvis_relay_finalize_work_log_segments(segments)
    return segments


def _marvis_relay_role_error_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    errors: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        payload = dict(artifact or {})
        if str(payload.get("artifact_type") or "") != "role_error":
            continue
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if role:
            errors[role] = payload
    return errors


def _marvis_relay_summary_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        payload = dict(artifact or {})
        artifact_type = str(payload.get("artifact_type") or "").strip()
        if artifact_type not in {
            "routing_decision",
            "architecture_plan",
            "implementation_report",
            "test_report",
            "audit_report",
            "final_summary",
        }:
            continue
        role = str(payload.get("role") or payload.get("relay_role") or "")
        summary = str(
            payload.get("summary")
            or payload.get("output")
            or payload.get("reason")
            or ""
        ).strip()
        summary = _marvis_relay_clean_artifact_summary(summary)
        if role and summary:
            payloads[role] = {
                "role": role,
                "artifact_type": artifact_type,
                "status": str(payload.get("status") or "passed"),
                "summary": summary,
                "next_action": str(payload.get("next_action") or ""),
                "open_questions": payload.get("open_questions") or [],
                "acceptance_criteria": payload.get("acceptance_criteria") or [],
                "route": str(payload.get("route") or ""),
                "risk": str(payload.get("risk") or ""),
            }
    return payloads


def _marvis_relay_merge_work_log_entry(existing: WorkLogEntry, entry: WorkLogEntry) -> None:
    if entry.chip:
        existing.chip = entry.chip
    if entry.text:
        if entry.replace_text:
            existing.text = entry.text
        else:
            existing.text += entry.text if existing.text else entry.text
    if entry.output:
        if existing.output and entry.output not in existing.output:
            existing.output = f"{existing.output}\n{entry.output}"
        elif not existing.output:
            existing.output = entry.output
    existing.failed = existing.failed or entry.failed


def _marvis_relay_finalize_work_log_segments(segments: list[WorkLogSegment]) -> None:
    kept_segments: list[WorkLogSegment] = []
    for segment in segments:
        entries: list[WorkLogEntry] = []
        for entry in segment.entries:
            if entry.kind == "message":
                cleaned = _marvis_relay_clean_artifact_summary(entry.text)
                if cleaned:
                    entry.text = cleaned
                elif _marvis_relay_work_log_text_is_protocol_noise(entry.text):
                    continue
            if entry.text or entry.chip or entry.output:
                entries.append(entry)
        segment.entries = entries
        if segment.entries:
            kept_segments.append(segment)
    segments[:] = kept_segments


def _marvis_relay_work_log_segment_html(segment: WorkLogSegment, *, index: int = 0) -> str:
    timeline_items = "".join(
        _marvis_relay_work_log_entry_html(entry)
        for entry in segment.entries
        if entry.text or entry.chip or entry.output
    )
    if not timeline_items:
        return ""
    return f"""
      <section class="marvis-work-log-role marvis-work-log-segment" data-marvis-work-log-role="{escape(segment.role)}" data-marvis-work-log-segment="{escape(segment.role)}" data-marvis-work-log-segment-index="{index}">
        {_marvis_relay_avatar_html(segment.persona, label=segment.display_name)}
        <div class="marvis-work-log-role-main">
          <h3>{escape(segment.display_name)}</h3>
          <div class="marvis-work-log-line">{timeline_items}</div>
        </div>
      </section>
    """


def _marvis_relay_work_log_entry_html(entry: WorkLogEntry) -> str:
    classes = ["marvis-work-log-entry"]
    if entry.failed:
        classes.append("is-failed")
    key_attr = f' data-marvis-work-log-entry-key="{escape(entry.key)}"' if entry.key else ""
    chip = _relay_replace_legacy_role_identifiers(entry.chip)
    text = _relay_replace_legacy_role_identifiers(entry.text)
    output = _relay_replace_legacy_role_identifiers(entry.output)
    chip_html = (
        f'<span class="marvis-work-log-tool-chip">{escape(chip)}</span>' if chip else ""
    )
    output_html = ""
    if output:
        output_html = (
            '<details class="marvis-work-log-output" data-marvis-work-log-output>'
            "<summary>查看输出</summary>"
            f"<pre>{escape(output)}</pre>"
            "</details>"
        )
    return f"""
      <div class="{' '.join(classes)}" data-marvis-work-log-entry="{escape(entry.kind)}"{key_attr}>
        {chip_html}
        <p>{escape(text)}</p>
        {output_html}
      </div>
    """


def _marvis_relay_work_log_text_item(text: str, *, chip: str = "") -> str:
    return _marvis_relay_work_log_entry_html(
        WorkLogEntry(kind="text", key="", text=text, chip=chip)
    )


def _marvis_relay_work_log_event_item(worker_event: WorkerStreamEvent) -> str:
    entry = _marvis_relay_work_log_entry_from_event("", worker_event)
    return _marvis_relay_work_log_entry_html(entry) if entry is not None else ""


def _marvis_relay_work_log_entry_from_event(
    role: str,
    worker_event: WorkerStreamEvent,
) -> WorkLogEntry | None:
    kind = str(worker_event.kind or "")
    event_type = str(worker_event.type or "")
    payload = dict(worker_event.payload or {})
    if kind == "user_message" or kind == "reasoning_delta":
        return None
    if event_type == "model.usage.updated":
        return None
    if kind == "text_delta":
        text = _relay_native_event_text(worker_event)
        if not text or _marvis_relay_work_log_text_is_protocol_noise(text):
            return None
        compact_text, output = _marvis_relay_compact_work_log_text(text)
        return WorkLogEntry(
            kind="message",
            key=_relay_native_message_key(role, worker_event, bucket="assistant"),
            text=_relay_sanitize_protocol_leak_text(role, compact_text),
            output=output,
        )
    if kind == "message_completed":
        text = _relay_native_event_text(worker_event).strip()
        if not text or _marvis_relay_work_log_text_is_protocol_noise(text):
            return None
        compact_text, output = _marvis_relay_compact_work_log_text(text)
        return WorkLogEntry(
            kind="message",
            key=_relay_native_message_key(role, worker_event, bucket="assistant"),
            text=_relay_sanitize_protocol_leak_text(role, compact_text),
            output=output,
            replace_text=True,
        )
    if kind.startswith("tool_call"):
        label = _marvis_relay_tool_label(payload) or "tool"
        return WorkLogEntry(
            kind="tool",
            key=_marvis_relay_tool_or_command_key("tool", worker_event, label),
            chip=f"{label} {_marvis_relay_event_status_label(kind)}",
            output=_marvis_relay_event_output_text(payload),
            failed=kind.endswith("_failed"),
        )
    if kind.startswith("command"):
        label = _marvis_relay_command_label(payload) or "command"
        return WorkLogEntry(
            kind="command",
            key=_marvis_relay_tool_or_command_key("command", worker_event, label),
            chip=f"{label} {_marvis_relay_event_status_label(kind)}",
            output=_marvis_relay_event_output_text(payload),
            failed=kind.endswith("_failed"),
        )
    if kind in {"approval_requested", "approval_resolved"}:
        text = _relay_native_event_text(worker_event).strip()
        status = "等待审批" if kind == "approval_requested" else "审批已处理"
        return WorkLogEntry(
            kind="approval",
            key=_marvis_relay_tool_or_command_key("approval", worker_event, status),
            text=text,
            chip=status,
        )
    if kind in {"file_changed", "diff_updated"}:
        label = "文件变更" if kind == "file_changed" else "差异更新"
        text = _marvis_relay_file_change_summary(payload)
        return WorkLogEntry(
            kind="file",
            key=_marvis_relay_tool_or_command_key("file", worker_event, label),
            text=text,
            chip=f"{label} 已记录",
        )
    if kind in {"activity", "lifecycle", "completed", "failed"}:
        text = _relay_native_event_text(worker_event).strip()
        if not text and kind == "failed":
            text = str(
                payload.get("error")
                or payload.get("reason")
                or payload.get("status")
                or "调用失败"
            ).strip()
        if not text:
            return None
        return WorkLogEntry(
            kind=kind,
            key=_marvis_relay_tool_or_command_key(kind, worker_event, kind),
            text=text,
            chip="调用失败" if kind == "failed" else "",
            failed=kind == "failed",
        )
    return None


def _marvis_relay_compact_work_log_text(text: str) -> tuple[str, str]:
    value = str(text or "").strip()
    if not value:
        return "", ""
    if not _marvis_relay_should_fold_work_log_text(value):
        return value, ""
    return "输出较长，已折叠。", value


def _marvis_relay_should_fold_work_log_text(text: str) -> bool:
    value = str(text or "")
    if len(value) > 600:
        return True
    if "```" in value:
        return True
    stripped = value.lstrip()
    if stripped.startswith(("{", "[")) and len(value) > 240:
        return True
    if re.search(r"(?is)<(?:!doctype|html|body|script|style|pre|div|section)\\b", value):
        return True
    return any(len(line) > 220 for line in value.splitlines())


def _marvis_relay_work_log_text_is_protocol_noise(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if _relay_text_looks_like_role_envelope(value):
        return True
    markers = (
        "artifact_type",
        "relay_role",
        "final_summary",
        "routing_decision",
        "acceptance_criteria",
        "evidence_refs",
        "handoff_to",
        "required_roles",
    )
    if any(marker in value for marker in markers) and (
        "{" in value
        or "}" in value
        or '",' in value
        or '":' in value
        or value.startswith(('"', "["))
    ):
        return True
    return False


def _marvis_relay_clean_artifact_summary(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if not _marvis_relay_work_log_text_is_protocol_noise(value):
        return value
    match = re.search(r'"summary"\s*:\s*"((?:\\.|[^"\\])*)"', value)
    if match is None and '\\"summary\\"' in value:
        normalized = value.replace('\\"', '"')
        match = re.search(r'"summary"\s*:\s*"((?:\\.|[^"\\])*)"', normalized)
    if match:
        raw_summary = match.group(1)
        try:
            decoded = json.loads(f'"{raw_summary}"')
        except json.JSONDecodeError:
            decoded = raw_summary
        cleaned = str(decoded or "").strip()
        if cleaned and not _marvis_relay_work_log_text_is_protocol_noise(cleaned):
            return cleaned
    return ""


def _marvis_relay_tool_or_command_key(
    prefix: str,
    worker_event: WorkerStreamEvent,
    label: str,
) -> str:
    payload = dict(worker_event.payload or {})
    stable = (
        payload.get("itemId")
        or payload.get("item_id")
        or payload.get("call_id")
        or payload.get("tool_call_id")
        or payload.get("native_turn_id")
        or payload.get("turnId")
        or label
        or worker_event.id
    )
    return f"{prefix}:{stable}"


def _marvis_relay_event_output_text(payload: dict[str, Any]) -> str:
    for key in ("output", "stderr", "stdout", "result", "message", "error", "delta", "chunk"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        text = str(value).strip()
        if text:
            return text
    return ""


def _marvis_relay_file_change_summary(payload: dict[str, Any]) -> str:
    for key in ("path", "file", "filename", "summary", "message"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    files = payload.get("files")
    if isinstance(files, list):
        return "、".join(str(item) for item in files[:5] if str(item).strip())
    return ""


def _marvis_relay_event_status_label(kind: str) -> str:
    if kind.endswith("_completed") or kind == "completed":
        return "已完成"
    if kind.endswith("_failed") or kind == "failed":
        return "调用失败"
    return "进行中"


def _marvis_relay_tool_label(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "name", "tool", "display_name", "action"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _marvis_relay_command_label(payload: dict[str, Any]) -> str:
    command = payload.get("command")
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command[:3])
    value = str(command or payload.get("cmd") or payload.get("name") or "").strip()
    return value


def _marvis_relay_static_work_log_body_html(body_html: str) -> str:
    live_markers = (
        "data-role-output",
        "data-activity-log",
        "data-routing-card",
        "data-routing-summary",
        "data-routing-route",
        "data-routing-complexity",
        "data-routing-risk",
        "data-routing-path",
        "data-routing-roles",
        "data-routing-acceptance",
        "data-routing-stops",
        "data-board-current-goal",
        "data-board-phase",
        "data-board-dispatch",
        "data-board-next-step",
        "data-board-latest-user",
        "data-board-director-summary",
    )
    for marker in live_markers:
        body_html = body_html.replace(marker, marker.replace("data-", "data-marvis-snapshot-"))
    return body_html


def _relay_role_config_html(
    relay_config: dict[str, Any],
    providers: list[Any],
) -> str:
    assignments = relay_config.get("assignments")
    assignment_map = assignments if isinstance(assignments, dict) else {}
    roles = relay_config.get("roles")
    role_rows = roles if isinstance(roles, list) and roles else [
        {"role": role, "display_name": _relay_role_label(role)}
        for role in RELAY_ROLE_IDS
    ]
    provider_rows = providers or [{"provider": "codex", "provider_engine": ""}]
    rows = []
    for role_entry in role_rows:
        role = str(role_entry.get("role") or "")
        if role not in RELAY_ROLE_IDS:
            continue
        selected = str(assignment_map.get(role) or provider_rows[0].get("provider") or "codex")
        options = "\n".join(
            f'<option value="{escape(str(provider.get("provider", "")))}"'
            f'{" selected" if str(provider.get("provider", "")) == selected else ""}>'
            f'{escape(_native_provider_display_name(str(provider.get("provider", ""))))}</option>'
            for provider in provider_rows
            if str(provider.get("provider", "")).strip()
        )
        tool_chips = "".join(
            f'<span class="relay-chip">{escape(str(item))}</span>'
            for item in [
                *list(role_entry.get("skills") or []),
                *list(role_entry.get("capabilities") or []),
            ]
        ) or '<span class="relay-chip">默认能力</span>'
        rows.append(
            f"""
            <div class="relay-config-row">
              <div>
                <strong>{escape(str(role_entry.get("display_name") or _relay_role_label(role)))}</strong>
                <div class="relay-muted">当前：{escape(_native_provider_display_name(selected))}</div>
              </div>
              <select class="relay-provider-select" data-role-provider="{escape(role)}" aria-label="{escape(_relay_role_label(role))} Provider">
                {options}
              </select>
              <div class="relay-config-tools">{tool_chips}</div>
            </div>
            """
        )
    return "\n".join(rows)


def _relay_task_card_html(summary: Any, token_suffix: str) -> str:
    status = str(summary.status)
    status_label = _relay_task_status_label(status)
    activity = _relay_activity_label(summary.last_activity_at)
    workspace = str(summary.workspace or "未指定工作目录")
    phase = _relay_phase_label(str(summary.phase or "director"))
    return f"""
      <article class="relay-task-card marvis-relay-task-card" data-status="{escape(status)}">
        <div class="relay-card-head">
          {_marvis_relay_avatar_html("marvis", label="Marvis")}
          <div>
            <div class="relay-title">{escape(summary.title)}</div>
            <div class="relay-muted">{escape(workspace)} · 当前阶段：{escape(phase)}</div>
          </div>
        </div>
        <div class="marvis-relay-task-card-footer">
          <div class="relay-card-meta">
            <span class="relay-status-badge">{escape(status_label)}</span>
            <span class="relay-muted">{escape(activity)}</span>
          </div>
          <a class="relay-open relay-card-open" href="/native/workflows/relay/tasks/{int(summary.task_id)}{token_suffix}">打开任务</a>
        </div>
      </article>
    """


def _relay_task_status_label(status: str) -> str:
    return {
        "queued": "排队中",
        "running": "进行中",
        "waiting_user": "等待你",
        "blocked": "已阻塞",
        "failed": "失败",
        "completed": "已完成",
        "interrupted": "已中断",
    }.get(status, status or "未知")


def _relay_role_label(role: str) -> str:
    return RELAY_ROLE_DISPLAY_NAMES.get(role, role)


def _relay_role_status_label(status: str) -> str:
    return {
        "idle": "未调度",
        "queued": "待启动",
        "streaming": "输出中",
        "waiting": "等待",
        "passed": "已完成",
        "failed": "失败",
        "blocked": "阻塞",
        "interrupted": "中断",
    }.get(status, status or "未知")


def _relay_routing_route_label(route: str) -> str:
    return {
        "director_only": "总工程师直接完成",
        "core_relay": "核心接力",
        "full_relay": "完整五角色接力",
        "audit_first": "审计优先",
        "waiting_user": "等待用户确认",
        "blocked": "已阻塞",
    }.get(route, route or "等待总工程师判断")


def _relay_humanize_display_text(text: str, *, english_fallback: str = "") -> str:
    value = str(text or "")
    value = re.sub(
        r"(?:Marvis/)?(?:App|File|Search|Computer|Browser)(?:/(?:App|File|Search|Computer|Browser))+ Agent",
        "英文角色名",
        value,
    )
    value = _relay_replace_legacy_role_identifiers(value)
    replacements = (
        ("路由为director_only", "由总工程师直接处理"),
        ("director_only", "总工程师直接处理"),
        ("core_relay", "核心角色接力"),
        ("full_relay", "五角色完整接力"),
        ("audit_first", "先审计再推进"),
        ("waiting_user", "等待你补充"),
        ("complete directly after routing by checking current market sources and returning the latest available gold price", "由总工程师核验最新行情来源并给出结果"),
        ("complete directly after routing", "由总工程师直接处理"),
        ("complete directly", "直接处理"),
        ("dispatch next role", "交给下一位角色处理"),
        ("dispatch task", "任务分配"),
        ("safe-area-inset-top", "顶部安全区"),
        ("safe-area", "安全区"),
    )
    for source, target in replacements:
        value = value.replace(source, target)
    if english_fallback and _relay_text_needs_chinese_fallback(value):
        return english_fallback
    return value


def _relay_text_needs_chinese_fallback(text: str) -> bool:
    value = _relay_replace_legacy_role_identifiers(text).strip()
    if not value:
        return False
    normalized = value
    for public_name in (
        "开发工程师",
        "架构工程师",
        "测试工程师",
        "审核工程师",
        "Marvis",
    ):
        normalized = normalized.replace(public_name, "")
    if not re.search(r"[A-Za-z]{3,}", normalized):
        return False
    if not re.search(r"[\u4e00-\u9fff]", normalized):
        return True
    return bool(re.search(r"[A-Za-z]{3,}(?:[ -]+[A-Za-z]{2,}){1,}", normalized))


def _relay_routing_risk_label(risk: str) -> str:
    return {
        "low": "低",
        "medium": "中",
        "high": "高",
        "critical": "关键",
    }.get(risk, risk or "待判断")


def _relay_phase_label(phase: str) -> str:
    return {
        "director": "总工程师接收",
        "architect": "架构设计",
        "implementer": "开发实现",
        "tester": "测试验证",
        "auditor": "审计复核",
        "complete": "完成总结",
    }.get(phase, phase or "总工程师接收")


def _relay_activity_label(value: Any) -> str:
    if not value:
        return "暂无活动"
    if isinstance(value, datetime):
        parsed = value
    else:
        timestamp = str(value).strip()
        if not timestamp:
            return "暂无活动"
        normalized = re.sub(
            r"(\.\d{6})\d+([+-]\d\d:\d\d|Z)?$",
            r"\1\2",
            timestamp,
        )
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return "最近活动未知"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_RELAY_ACTIVITY_DISPLAY_TZ)
    return f"最近活动 {parsed.astimezone(_RELAY_ACTIVITY_DISPLAY_TZ):%m-%d %H:%M}"


def _relay_current_dispatch_label(board: Any) -> str:
    current = str(
        getattr(board, "current_dispatch", "") or getattr(board, "phase", "") or "director"
    )
    return _relay_role_label(current) if current in RELAY_ROLE_IDS else _relay_phase_label(current)


def _relay_director_summary_text(
    role_jobs: list[Any],
    canonical_payloads: dict[str, dict[str, Any]],
) -> str:
    for job in role_jobs:
        if str(getattr(job, "role", "") or "") != "director":
            continue
        display = _relay_role_display_from_job(
            job,
            canonical_payload=canonical_payloads.get("director"),
        )
        summary = str(display.get("summary_text") or "").strip()
        if summary:
            return summary
        error = str(getattr(job, "error_message", "") or "").strip()
        if error:
            return f"执行问题：{error}"
    return "等待总工程师接收并形成决策摘要"


def _relay_role_progress_html(role_jobs: list[Any]) -> str:
    jobs_by_role = {str(job.role): job for job in role_jobs}
    rows = []
    for role in RELAY_ROLE_IDS:
        status = str(getattr(jobs_by_role.get(role), "status", "idle") or "idle")
        rows.append(
            f"""
            <div class="relay-progress-step" data-progress-role="{escape(role)}" data-progress-status="{escape(status)}">
              <span class="relay-progress-dot" aria-hidden="true"></span>
              <div>
                <strong>{escape(_relay_role_label(role))}</strong>
                <span class="relay-progress-status">{escape(_relay_role_status_label(status))}</span>
              </div>
            </div>
            """
        )
    return "\n".join(rows)


def _relay_initial_activity_html(detail: Any) -> str:
    items = [f"任务已创建，等待总工程师接收：{detail.task.title}"]
    decision = getattr(detail, "routing_decision", None) or {}
    if decision:
        items.append(
            "总工程师调度决策："
            f"{_relay_routing_route_label(str(decision.get('route') or ''))}"
            f" · 风险{_relay_routing_risk_label(str(decision.get('risk') or ''))}"
        )
    for job in detail.role_jobs:
        status = str(job.status or "idle")
        if status == "idle":
            continue
        provider = _native_provider_display_name(str(job.provider or detail.task.provider))
        items.append(f"{job.display_name} · {_relay_role_status_label(status)} · {provider}")
        if job.latest_handoff_summary:
            items.append(f"{job.display_name}交接摘要：{job.latest_handoff_summary}")
        if job.fallback_reason:
            items.append(f"{job.display_name}调度降级：{job.fallback_reason}")
        if getattr(job, "error_message", ""):
            items.append(f"{job.display_name}执行问题：{job.error_message}")
    if getattr(detail, "latest_handoff", None):
        handoff = detail.latest_handoff
        items.append(
            f"{_relay_role_label(handoff.from_role)} 已交接给 "
            f"{_relay_role_label(handoff.to_role)}：{handoff.summary}"
        )
    return "\n".join(
        '<li><span class="relay-activity-dot" aria-hidden="true"></span>'
        f"<span>{escape(item)}</span></li>"
        for item in items
    )


def _relay_routing_decision_html(detail: Any) -> str:
    decision = getattr(detail, "routing_decision", None) or {}
    if not decision:
        decision = _relay_missing_routing_decision(detail)
    required_roles = [
        _relay_role_label(str(role))
        for role in decision.get("required_roles", [])
        if str(role)
    ]
    acceptance = [
        str(item)
        for item in decision.get("acceptance_criteria", [])
        if str(item).strip()
    ]
    stop_conditions = [
        str(item)
        for item in decision.get("stop_conditions", [])
        if str(item).strip()
    ]
    required_text = "、".join(required_roles) or "等待总工程师判断"
    acceptance_text = "、".join(acceptance) or "等待总工程师给出验收依据"
    stop_text = "、".join(stop_conditions) or "暂无额外停止条件"
    approval_text = "需要用户确认" if bool(decision.get("requires_user_approval")) else "无需额外确认"
    route = str(decision.get("route") or "")
    summary = _relay_humanize_display_text(
        str(decision.get("summary") or "等待总工程师接收任务并形成调度决策。")
    )
    return f"""
    <section class="relay-routing" aria-label="调度决策" data-routing-card>
      <div class="relay-board-head">
        <div>
          <h2>调度决策</h2>
          <p class="relay-muted" data-routing-summary>{escape(summary)}</p>
        </div>
        <span class="relay-status-badge" data-routing-route>{escape(_relay_routing_route_label(route))}</span>
      </div>
      <div class="relay-board-grid">
        <div class="relay-board-item"><strong>任务难度</strong><p data-routing-complexity>{escape(str(decision.get("complexity") or "待判断"))}</p></div>
        <div class="relay-board-item"><strong>风险等级</strong><p data-routing-risk>{escape(_relay_routing_risk_label(str(decision.get("risk") or "")))}</p></div>
        <div class="relay-board-item"><strong>执行路径</strong><p data-routing-path>{escape(_relay_routing_route_label(route))}</p></div>
        <div class="relay-board-item"><strong>本轮角色</strong><p data-routing-roles>{escape(required_text)}</p></div>
        <div class="relay-board-item"><strong>验收依据</strong><p data-routing-acceptance>{escape(acceptance_text)}</p></div>
        <div class="relay-board-item"><strong>停止条件</strong><p data-routing-stops>{escape(stop_text)} · {escape(approval_text)}</p></div>
      </div>
    </section>
    """


def _relay_missing_routing_decision(detail: Any) -> dict[str, Any]:
    task_status = str(getattr(getattr(detail, "task", None), "status", "") or "")
    director_error = next(
        (
            str(getattr(job, "error_message", "") or "").strip()
            for job in getattr(detail, "role_jobs", [])
            if str(getattr(job, "role", "") or "") == "director"
            and str(getattr(job, "error_message", "") or "").strip()
        ),
        "",
    )
    if task_status == "blocked":
        summary = "调度决策未生成。"
        if director_error:
            summary = f"调度决策未生成。总工程师输出协议错误：{director_error}"
        return {
            "summary": summary,
            "complexity": "未生成",
            "risk": "high",
            "route": "blocked",
            "required_roles": [],
            "acceptance_criteria": [],
            "stop_conditions": ["需要重新发起或补充后生成调度决策"],
            "requires_user_approval": True,
        }
    if task_status == "waiting_user":
        return {
            "summary": "等待用户补充后形成调度决策。",
            "complexity": "待判断",
            "risk": "",
            "route": "waiting_user",
            "required_roles": [],
            "acceptance_criteria": [],
            "stop_conditions": [],
            "requires_user_approval": True,
        }
    return {
        "summary": "等待总工程师接收任务并形成调度决策。",
        "complexity": "待判断",
        "risk": "",
        "route": "",
        "required_roles": [],
        "acceptance_criteria": [],
        "stop_conditions": [],
        "requires_user_approval": False,
    }


def _relay_conversation_message_html(
    *,
    kind: str,
    role: str,
    speaker: str,
    body: str,
    meta: str = "",
) -> str:
    meta_html = (
        f'<span class="relay-message-meta">{escape(meta)}</span>' if meta else ""
    )
    return f"""
      <article class="relay-message" data-conversation-kind="{escape(kind)}" data-conversation-role="{escape(role)}">
        <div class="relay-message-head">
          <strong>{escape(speaker)}</strong>
          {meta_html}
        </div>
        <div class="relay-message-body" data-conversation-body>{escape(body)}</div>
      </article>
    """


def _relay_conversation_event_html(text: str, *, role: str = "system") -> str:
    return _relay_conversation_message_html(
        kind="event",
        role=role,
        speaker="系统",
        body=text,
    )


def _relay_event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_dict"):
        return dict(event.to_dict())
    return dict(event or {})


def _relay_latest_event_sequence(events: list[Any] | tuple[Any, ...]) -> int:
    latest = 0
    for event in events:
        try:
            latest = max(latest, int(_relay_event_dict(event).get("sequence") or 0))
        except (TypeError, ValueError):
            continue
    return latest


def _relay_event_payload(event: Any) -> tuple[str, dict[str, Any]]:
    raw = _relay_event_dict(event)
    nested = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    payload = dict(nested or {})
    for key, value in raw.items():
        if key == "payload":
            continue
        if value not in (None, ""):
            payload[key] = value
    event_type = str(payload.get("event_type") or "")
    role = str(payload.get("role") or nested.get("role") or "")
    if role:
        payload["role"] = role
    return event_type, payload


def _relay_conversation_html_from_events(events: list[Any] | tuple[Any, ...]) -> str:
    items: list[str] = []
    active_role = ""
    active_body: list[str] = []

    def flush_delta() -> None:
        nonlocal active_role, active_body
        body = "".join(active_body)
        if body:
            items.append(
                _relay_conversation_message_html(
                    kind="role",
                    role=active_role,
                    speaker=_relay_role_label(active_role),
                    body=body,
                )
            )
        active_role = ""
        active_body = []

    def append_event(text: str, *, role: str = "system") -> None:
        if not str(text or "").strip():
            return
        flush_delta()
        items.append(_relay_conversation_event_html(str(text), role=role))

    def append_user(text: str) -> None:
        if not str(text or "").strip():
            return
        flush_delta()
        items.append(
            _relay_conversation_message_html(
                kind="user",
                role="user",
                speaker="你",
                body=str(text).strip(),
            )
        )

    def append_delta(role: str, text: str) -> None:
        nonlocal active_role, active_body
        delta = str(text or "")
        if not delta:
            return
        if active_role != role:
            flush_delta()
            active_role = role
            active_body = []
        active_body.append(delta)

    for event in sorted(events, key=lambda item: _relay_latest_event_sequence([item])):
        event_type, payload = _relay_event_payload(event)
        role = str(payload.get("role") or "")
        if event_type == "task.created":
            append_event(f"任务已创建：{payload.get('title') or '接力任务'}")
        elif event_type == "role.queued":
            if payload.get("latest_user_input"):
                append_user(str(payload.get("latest_user_input") or ""))
            append_event(f"{_relay_role_label(role)} 已进入队列，等待启动。", role=role)
        elif event_type == "role.streaming":
            append_event(f"{_relay_role_label(role)} 开始执行。", role=role)
        elif event_type == "dispatch.verified":
            append_event(f"{_relay_role_label(role)} 的原生会话已启动。", role=role)
        elif event_type == "dispatch.fallback":
            reason = str(payload.get("reason") or "使用可用 provider 继续")
            append_event(f"{_relay_role_label(role)} 调度降级：{reason}", role=role)
        elif event_type == "role.output_delta":
            append_delta(role, str(payload.get("delta") or payload.get("text") or ""))
        elif event_type == "routing.decision":
            route = _relay_routing_route_label(str(payload.get("route") or ""))
            summary = str(payload.get("summary") or "")
            append_event(f"总工程师调度决策：{route}。{summary}", role="director")
        elif event_type == "role.envelope":
            summary = str(payload.get("summary") or "")
            if summary:
                append_event(f"{_relay_role_label(role)} 产出摘要：{summary}", role=role)
        elif event_type == "handoff.created":
            from_role = str(payload.get("from_role") or role)
            to_role = str(payload.get("to_role") or payload.get("handoff_to") or "")
            summary = str(payload.get("summary") or "等待下一角色处理")
            append_event(
                f"{_relay_role_label(from_role)} 已交接给 {_relay_role_label(to_role)}：{summary}",
                role=to_role or from_role,
            )
        elif event_type == "role.status":
            status = str(payload.get("status") or "")
            append_event(
                f"{_relay_role_label(role)} 状态更新为 {_relay_role_status_label(status)}。",
                role=role,
            )
        elif event_type == "task.completed":
            append_event("任务已完成，可以继续补充给总工程师进行追问或追加验收。")
        elif event_type == "task.interrupted":
            append_event("任务已中断。")
    flush_delta()
    return "\n".join(items)


def _relay_initial_conversation_html(
    detail: Any,
    *,
    events: list[Any] | tuple[Any, ...] | None = None,
) -> str:
    if events:
        event_html = _relay_conversation_html_from_events(events)
        if event_html:
            return event_html

    task = getattr(detail, "task", None)
    board = getattr(detail, "board", None)
    items: list[str] = []
    latest_user_input = str(getattr(board, "latest_user_input", "") or "").strip()
    if latest_user_input:
        items.append(
            _relay_conversation_message_html(
                kind="user",
                role="user",
                speaker="你",
                body=latest_user_input,
            )
        )

    decision = getattr(detail, "routing_decision", None) or {}
    if decision:
        route = _relay_routing_route_label(str(decision.get("route") or ""))
        summary = str(decision.get("summary") or "总工程师已形成调度决策。")
        items.append(_relay_conversation_event_html(f"调度决策：{route}。{summary}"))
    elif str(getattr(task, "status", "") or "") in ("blocked", "waiting_user"):
        fallback = _relay_missing_routing_decision(detail)
        items.append(_relay_conversation_event_html(str(fallback.get("summary") or "")))

    for job in getattr(detail, "role_jobs", []):
        role = str(getattr(job, "role", "") or "")
        output = str(getattr(job, "output", "") or "").strip()
        if output:
            items.append(
                _relay_conversation_message_html(
                    kind="role",
                    role=role,
                    speaker=str(getattr(job, "display_name", "") or _relay_role_label(role)),
                    body=output,
                    meta=_relay_role_status_label(str(getattr(job, "status", "") or "idle")),
                )
            )
        error_message = str(getattr(job, "error_message", "") or "").strip()
        if error_message:
            items.append(
                _relay_conversation_event_html(
                    f"{_relay_role_label(role)} 执行问题：{error_message}",
                    role=role,
                )
            )
        handoff_summary = str(getattr(job, "latest_handoff_summary", "") or "").strip()
        if handoff_summary:
            items.append(
                _relay_conversation_event_html(
                    f"{_relay_role_label(role)} 交接摘要：{handoff_summary}",
                    role=role,
                )
            )

    if not items:
        title = str(getattr(task, "title", "") or "接力任务")
        items.append(_relay_conversation_event_html(f"{title} 已创建，等待总工程师接收。"))
    return "\n".join(items)


def _relay_task_detail_page(
    detail: Any,
    *,
    access_token: str = "",
    view: str = "conversation",
    events: list[Any] | tuple[Any, ...] | None = None,
    hub: WorkerLiveStreamHub | None = None,
) -> str:
    view = _relay_task_detail_view(view)
    token_suffix = _token_suffix(access_token)
    event_history = list(events or [])
    event_after = _relay_latest_event_sequence(event_history)
    events_suffix = _relay_task_events_suffix(access_token, event_after)
    canonical_payloads = _relay_role_canonical_payloads_by_role(
        getattr(detail, "artifacts", []) or []
    )
    native_conversation_html = _marvis_relay_conversation_html(
        detail.role_jobs,
        hub=hub,
        canonical_payloads=canonical_payloads,
        canonical_payload_sequence=_relay_role_canonical_payload_sequence(
            getattr(detail, "artifacts", []) or []
        ),
        artifacts=getattr(detail, "artifacts", []) or [],
    )
    task = detail.task
    back_href = _relay_workspace_href(str(task.workspace or ""), access_token)
    device_label = "wanglin的Mac mini"
    topbar_html = _marvis_relay_topbar(
        title="Marvis",
        subtitle=device_label,
        back_href=back_href,
        right_html=f"""
          <a class="marvis-relay-icon-button" href="/native/workflows/relay/office{token_suffix}" aria-label="Marvis办公室">
            <span class="marvis-relay-icon-devices" aria-hidden="true"></span>
          </a>
          <button class="marvis-relay-icon-button" type="button" data-marvis-open-log aria-label="工作日志">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
          </button>
        """,
    )
    bottom_nav_html = _marvis_relay_bottom_nav(
        "chat",
        access_token=access_token,
        selected_workspace=str(task.workspace or ""),
    )
    token_total = _marvis_relay_token_total_from_events(detail.role_jobs, hub=hub)
    token_text = _format_marvis_relay_token_count(token_total)
    work_log_html = _marvis_relay_work_log_html(
        body_html=_marvis_relay_work_log_body_html(
            detail,
            hub=hub,
            canonical_payloads=canonical_payloads,
        ),
        token_text=token_text,
        token_total=token_total,
        max_event_id=_marvis_relay_max_event_id_from_events(detail.role_jobs, hub=hub),
    )
    followup_composer_html = _marvis_relay_followup_composer(
        task_id=int(task.id),
        workspace=str(task.workspace or ""),
        access_token=access_token,
        task_status=str(task.status or ""),
    )
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light only">
  <title>{escape(task.title)} · Relay</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="{_RELAY_MARVIS_CSS_HREF}">
  <style>
    html {{ background: #f6f6f6; }}
    body {{ margin: 0; color: #111; background: #f6f6f6; }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
  </style>
</head>
<body data-relay-view="{escape(view)}" data-marvis-relay-view="{escape(view)}">
  <div class="marvis-relay-phone">
  {topbar_html}
  <main class="marvis-relay-task-main">
    <section class="relay-view relay-conversation-panel" data-view-panel="conversation" aria-label="会话">
      <div class="marvis-relay-chat-thread relay-conversation" data-native-conversation-timeline>
        {native_conversation_html}
      </div>
    </section>
  </main>
  {followup_composer_html}
  <nav class="marvis-relay-bottom-nav" aria-label="Marvis relay navigation">
    {bottom_nav_html}
  </nav>
  </div>
  {work_log_html}
  <script>
    const TASK_ID = {json.dumps(str(task.id))};
    const TOKEN_SUFFIX = {json.dumps(token_suffix)};
    const EVENTS_SUFFIX = {json.dumps(events_suffix)};
    const ROLE_LABELS = {json.dumps({role: _relay_role_label(role) for role in RELAY_ROLE_IDS}, ensure_ascii=False)};
    const MARVIS_WORK_LOG_ROLE_LABELS = {json.dumps({role: _marvis_relay_public_role(role)[1] for role in RELAY_ROLE_IDS}, ensure_ascii=False)};
    const MARVIS_WORK_LOG_ROLE_PERSONAS = {json.dumps({role: _marvis_relay_public_role(role)[0] for role in RELAY_ROLE_IDS}, ensure_ascii=False)};
    const MARVIS_HANDOFF_ROLE_LABELS = {json.dumps({role: _marvis_relay_handoff_role_label(role) for role in RELAY_ROLE_IDS}, ensure_ascii=False)};
    const MARVIS_LEGACY_ROLE_LABEL_PARTS = {json.dumps(_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS, ensure_ascii=False)};
    const MARVIS_LEGACY_ROLE_SLUG_PARTS = {json.dumps(_MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS, ensure_ascii=False)};
    const STATUS_LABELS = {json.dumps({
        "idle": "未调度",
        "queued": "排队中",
        "streaming": "执行中",
        "waiting": "等待中",
        "passed": "已完成",
        "failed": "失败",
        "blocked": "阻塞",
        "interrupted": "已中断",
        "completed": "已完成",
    }, ensure_ascii=False)};
    const TASK_STATUS_LABELS = {json.dumps({
        "queued": "排队中",
        "running": "进行中",
        "waiting_user": "等待你",
        "blocked": "已阻塞",
        "failed": "失败",
        "completed": "已完成",
        "interrupted": "已中断",
    }, ensure_ascii=False)};
    const roleOutputs = {{}};
    const marvisWorkLog = document.querySelector("[data-marvis-work-log]");
    const marvisWorkLogBackdrop = document.querySelector("[data-marvis-work-log-backdrop]");
    function marvisWorkLogIsDesktop() {{
      return window.matchMedia("(min-width: 980px)").matches;
    }}
    function openMarvisWorkLog() {{
      if (!marvisWorkLog) return;
      marvisWorkLog.hidden = false;
      if (marvisWorkLogBackdrop && !marvisWorkLogIsDesktop()) marvisWorkLogBackdrop.hidden = false;
      requestAnimationFrame(() => {{
        marvisWorkLog.classList.add("open");
        if (!marvisWorkLogIsDesktop()) marvisWorkLogBackdrop?.classList.add("visible");
      }});
    }}
    function closeMarvisWorkLog() {{
      if (!marvisWorkLog) return;
      if (marvisWorkLogIsDesktop()) return;
      marvisWorkLog.classList.remove("open");
      marvisWorkLogBackdrop?.classList.remove("visible");
      setTimeout(() => {{
        marvisWorkLog.hidden = true;
        if (marvisWorkLogBackdrop) marvisWorkLogBackdrop.hidden = true;
      }}, 240);
    }}
    if (marvisWorkLog && marvisWorkLogIsDesktop()) {{
      marvisWorkLog.hidden = false;
      marvisWorkLog.classList.add("open");
    }}
    document.querySelectorAll("[data-marvis-open-log]").forEach((button) => {{
      button.addEventListener("click", openMarvisWorkLog);
    }});
    document.querySelectorAll("[data-marvis-close-log], [data-marvis-work-log-backdrop]").forEach((button) => {{
      button.addEventListener("click", closeMarvisWorkLog);
    }});
    document.querySelectorAll("[data-role-output]").forEach((node) => {{
      roleOutputs[node.dataset.roleOutput] = node;
    }});
    const rolePreviews = {{}};
    document.querySelectorAll("[data-role-preview]").forEach((node) => {{
      rolePreviews[node.dataset.rolePreview] = node;
    }});
    function labelForRole(role) {{
      return ROLE_LABELS[role] || role || "角色";
    }}
    function marvisHandoffRoleLabel(role) {{
      return MARVIS_HANDOFF_ROLE_LABELS[role] || labelForRole(role);
    }}
    function marvisHandoffText(fromRole, toRole) {{
      const toName = marvisHandoffRoleLabel(toRole);
      if (fromRole === "director") return `Marvis拍了拍 ${{toName}}，说开干吧`;
      const fromName = marvisHandoffRoleLabel(fromRole);
      if (toRole === "auditor") return `${{fromName}}交给${{toName}}复核`;
      if (fromRole === "auditor") return `${{fromName}}退回${{toName}}继续处理`;
      return `${{fromName}}交给${{toName}}继续处理`;
    }}
    function marvisLegacyPersonaLabels(role) {{
      return (MARVIS_LEGACY_ROLE_LABEL_PARTS[role] || []).map((parts) => parts.join(" "));
    }}
    function relayReplaceLegacyRoleDisplayNames(text) {{
      let value = String(text || "");
      Object.keys(MARVIS_WORK_LOG_ROLE_PERSONAS).forEach((role) => {{
        const currentLabel = MARVIS_WORK_LOG_ROLE_LABELS[role] || "";
        marvisLegacyPersonaLabels(role).forEach((legacyLabel) => {{
          if (!legacyLabel || !currentLabel || legacyLabel === currentLabel) return;
          value = value.split(legacyLabel).join(currentLabel);
        }});
      }});
      Object.keys(MARVIS_LEGACY_ROLE_SLUG_PARTS).forEach((role) => {{
        const currentSlug = MARVIS_WORK_LOG_ROLE_PERSONAS[role] || "";
        (MARVIS_LEGACY_ROLE_SLUG_PARTS[role] || []).forEach((parts) => {{
          const legacySlug = parts.join("-");
          if (!legacySlug || !currentSlug || legacySlug === currentSlug) return;
          value = value.split(legacySlug).join(currentSlug);
        }});
      }});
      return value;
    }}
    function labelForStatus(status) {{
      return STATUS_LABELS[status] || status || "未知";
    }}
    function normalizeRelayPayload(raw) {{
      const source = raw && typeof raw === "object" ? raw : {{}};
      const nested = source.payload && typeof source.payload === "object" && !Array.isArray(source.payload) ? source.payload : {{}};
      const normalized = {{ ...nested }};
      Object.entries(source).forEach(([key, value]) => {{
        if (key === "payload") return;
        if (value === undefined || value === null || value === "") return;
        normalized[key] = value;
      }});
      if (!normalized.role && nested.role) normalized.role = nested.role;
      return normalized;
    }}
    function parseRelayEvent(event) {{
      return normalizeRelayPayload(JSON.parse(event.data || "{{}}"));
    }}
    const conversationTimeline = document.querySelector("[data-native-conversation-timeline]");
    const nativeTranscriptNodes = new Map();
    const nativeEnvelopeBuffers = new Map();
    const conversationUserBodies = new Set();
    const seenPreviewEventKeys = new Set();
    function scrollNativeConversationToEnd() {{
      if (conversationTimeline) conversationTimeline.scrollTop = conversationTimeline.scrollHeight;
    }}
    function relayNormalizeConversationText(text) {{
      return String(text || "").replace(/\\s+/g, " ").trim();
    }}
    function relayUserMessageIsRetryOrContext(text) {{
      const value = String(text || "");
      return value.includes("系统已要求当前角色重新输出合法结构化结果。") || value.includes("expected_output_envelope:") || value.includes("你刚才作为");
    }}
    function nativeEventPayload(nativeEvent) {{
      return nativeEvent && nativeEvent.payload && typeof nativeEvent.payload === "object" ? nativeEvent.payload : {{}};
    }}
    function nativeEventText(nativeEvent) {{
      const payload = nativeEventPayload(nativeEvent);
      const value = payload.text ?? payload.delta ?? payload.summary ?? payload.content ?? payload.message ?? payload.output ?? payload.chunk ?? "";
      return String(value || "");
    }}
    function relayExtractContextField(text, field) {{
      const prefix = `${{field}}:`;
      const lines = String(text || "").split(/\\r?\\n/);
      const labels = ["task_id:", "role:", "workspace:", "goal:", "latest_user_input:", "handoff_summaries:", "constraints:", "expected_output_envelope:"];
      for (let index = 0; index < lines.length; index += 1) {{
        const line = lines[index].trimEnd();
        if (!line.startsWith(prefix)) continue;
        const inline = line.slice(prefix.length).trim();
        if (inline) return inline;
        const collected = [];
        for (const nextLine of lines.slice(index + 1)) {{
          const trimmed = nextLine.trim();
          if (labels.some((label) => trimmed.startsWith(label))) break;
          if (trimmed) collected.push(trimmed);
        }}
        return collected.join("\\n").trim();
      }}
      return "";
    }}
    function relayHumanizeUserMessage(text) {{
      const value = String(text || "");
      if (value.includes("你刚才作为") && value.includes("expected_output_envelope:")) {{
        return "";
      }}
      if (!value.includes("latest_user_input:") && !value.includes("expected_output_envelope:")) return value;
      return relayExtractContextField(value, "latest_user_input") || relayExtractContextField(value, "goal") || value;
    }}
    function relayTextLooksLikeEnvelope(text) {{
      const value = String(text || "").trim();
      if (!value.startsWith("{{")) return false;
      return [
        "artifact_type",
        "relay_role",
        "routing_decision",
        "acceptance_criteria",
        "handoff_to",
        "required_roles",
        "open_questions",
        "next_action",
      ].some((marker) => value.includes(marker));
    }}
    function relayProtocolOutputHiddenText(role) {{
      return `${{labelForRole(role)}} 的结构化输出已由系统处理，原始协议内容不在主会话展示。`;
    }}
    function relayDictLooksLikeEnvelope(payload) {{
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
      return [
        "artifact_type",
        "relay_role",
        "summary",
        "next_action",
        "open_questions",
        "required_roles",
        "acceptance_criteria",
      ].some((key) => Object.prototype.hasOwnProperty.call(payload, key));
    }}
    function relayParseEnvelope(text) {{
      const value = String(text || "").trim();
      try {{
        const parsed = JSON.parse(value);
        return relayDictLooksLikeEnvelope(parsed) ? parsed : null;
      }} catch (_error) {{
        for (let index = 0; index < value.length; index += 1) {{
          if (value[index] !== "{{") continue;
          try {{
            const parsed = JSON.parse(value.slice(index));
            return relayDictLooksLikeEnvelope(parsed) ? parsed : null;
          }} catch (_nestedError) {{
            continue;
          }}
        }}
      }}
      return null;
    }}
    function relayJoinTextList(value) {{
      if (Array.isArray(value)) return value.map((item) => String(item || "").trim()).filter(Boolean).join("；");
      return String(value || "").trim();
    }}
    function relayRouteLabel(route) {{
      const labels = {{
        director_only: "总工程师直接完成",
        core_relay: "核心接力",
        full_relay: "完整五角色接力",
        audit_first: "审计优先",
        waiting_user: "等待用户确认",
        blocked: "已阻塞",
      }};
      return labels[route] || route || "";
    }}
    function relayTextNeedsChineseFallback(text) {{
      const value = relayReplaceLegacyRoleDisplayNames(text).trim();
      if (!/[A-Za-z]{{3,}}/.test(value)) return false;
      if (!/[一-龥]/.test(value)) return true;
      return /[A-Za-z]{{3,}}(?:[ -]+[A-Za-z]{{2,}}){{1,}}/.test(value);
    }}
    function relayHumanizeDisplayText(text, englishFallback = "") {{
      let value = relayReplaceLegacyRoleDisplayNames(text);
      const replacements = [
        [/路由为director_only/g, "由总工程师直接处理"],
        [/director_only/g, "总工程师直接处理"],
        [/core_relay/g, "核心角色接力"],
        [/full_relay/g, "五角色完整接力"],
        [/audit_first/g, "先审计再推进"],
        [/waiting_user/g, "等待你补充"],
        [/complete directly after routing by checking current market sources and returning the latest available gold price/g, "由总工程师核验最新行情来源并给出结果"],
        [/complete directly after routing/g, "由总工程师直接处理"],
        [/complete directly/g, "直接处理"],
        [/dispatch next role/g, "交给下一位角色处理"],
      ];
      replacements.forEach(([pattern, label]) => {{
        value = value.replace(pattern, label);
      }});
      if (englishFallback && relayTextNeedsChineseFallback(value)) return englishFallback;
      return value;
    }}
    function relaySanitizeProtocolLeakText(role, text) {{
      let value = relayHumanizeDisplayText(text);
      const sentinel = "原始结构化输出不在主会话展示。";
      if (value.includes(sentinel)) return value.split(sentinel, 1)[0] + sentinel;
      const markers = ["artifact_type", "expected_output_envelope", "routing_decisioncomplexity", "required_roles", "handoff_to"];
      if (markers.some((marker) => value.includes(marker)) && value.includes("{{")) {{
        return relayProtocolOutputHiddenText(role);
      }}
      return value;
    }}
    const marvisWorkLogBody = document.querySelector("[data-marvis-work-log-body]");
    const marvisWorkLogTokenValue = document.querySelector("[data-marvis-work-log-token-value]");
    const marvisWorkLogSeenRuntimeIds = new Set();
    const marvisWorkLogInitialMaxEventId = Number(marvisWorkLog?.dataset.marvisWorkLogMaxEventId || "0");
    let marvisWorkLogTokenTotal = Number(marvisWorkLogTokenValue?.dataset.tokenTotal || "0");
    function marvisWorkLogRoleLabel(role) {{
      return MARVIS_WORK_LOG_ROLE_LABELS[role] || labelForRole(role) || "Marvis";
    }}
    function marvisWorkLogRolePersona(role) {{
      return MARVIS_WORK_LOG_ROLE_PERSONAS[role] || "marvis";
    }}
    function marvisWorkLogFormatTokens(value) {{
      const count = Math.max(0, Math.round(Number(value || 0)));
      if (count >= 1000) return `${{Math.round(count / 1000)}}K`;
      return String(count);
    }}
    function marvisWorkLogUsageTotal(nativeEvent) {{
      const payload = nativeEventPayload(nativeEvent);
      const usage = payload.usage && typeof payload.usage === "object" ? payload.usage : null;
      const total = payload.total && typeof payload.total === "object" ? payload.total : null;
      const candidates = [payload];
      if (usage) {{
        candidates.push(usage);
        if (usage.total && typeof usage.total === "object") candidates.push(usage.total);
      }}
      if (total) candidates.push(total);
      for (const candidate of candidates) {{
        for (const key of ["total_tokens", "tokens", "consumed_tokens"]) {{
          const value = Number(candidate[key] || 0);
          if (Number.isFinite(value) && value > 0) return Math.round(value);
        }}
      }}
      for (const candidate of candidates) {{
        let subtotal = 0;
        for (const key of ["input_tokens", "output_tokens", "reasoning_output_tokens"]) {{
          const value = Number(candidate[key] || 0);
          if (Number.isFinite(value) && value > 0) subtotal += Math.round(value);
        }}
        if (subtotal > 0) return subtotal;
      }}
      return 0;
    }}
    function updateMarvisWorkLogTokenTotal(nativeEvent, runtimeEventId = "") {{
      const numericId = Number(runtimeEventId || nativeEvent?.id || 0);
      if (numericId && numericId <= marvisWorkLogInitialMaxEventId) return;
      const usageTotal = marvisWorkLogUsageTotal(nativeEvent);
      if (!usageTotal || !marvisWorkLogTokenValue) return;
      marvisWorkLogTokenTotal += usageTotal;
      marvisWorkLogTokenValue.dataset.tokenTotal = String(marvisWorkLogTokenTotal);
      marvisWorkLogTokenValue.textContent = `${{marvisWorkLogFormatTokens(marvisWorkLogTokenTotal)}} ☕`;
    }}
    function marvisWorkLogNativeKind(nativeEvent) {{
      if (nativeEvent?.kind) return nativeEvent.kind;
      const type = nativeEvent?.type || "";
      const map = {{
        "model.text.delta": "text_delta",
        "model.message.completed": "message_completed",
        "model.reasoning.delta": "reasoning_delta",
        "model.usage.updated": "usage_updated",
        "tool.call.started": "tool_call_started",
        "tool.call.progress": "tool_call_progress",
        "tool.call.completed": "tool_call_completed",
        "tool.call.failed": "tool_call_failed",
        "command.started": "command_started",
        "command.output.delta": "command_output",
        "command.completed": "command_completed",
        "command.failed": "command_failed",
        "file.changed": "file_changed",
        "diff.updated": "diff_updated",
        "approval.requested": "approval_requested",
        "approval.resolved": "approval_resolved",
        "agent.run.activity": "activity",
        "agent.run.started": "lifecycle",
        "agent.run.heartbeat": "lifecycle",
        "agent.run.completed": "completed",
        "agent.run.failed": "failed",
      }};
      return map[type] || type || "event";
    }}
    function marvisWorkLogEventStatus(kind) {{
      if (kind.endsWith("_completed") || kind === "completed") return "已完成";
      if (kind.endsWith("_failed") || kind === "failed") return "调用失败";
      return "进行中";
    }}
    function marvisWorkLogToolLabel(payload) {{
      for (const key of ["tool_name", "name", "tool", "display_name", "action"]) {{
        const value = String(payload[key] || "").trim();
        if (value) return value;
      }}
      return "";
    }}
    function marvisWorkLogCommandLabel(payload) {{
      const command = payload.command;
      if (Array.isArray(command)) return command.slice(0, 3).map(String).join(" ");
      return String(command || payload.cmd || payload.name || "").trim();
    }}
    function marvisWorkLogOutputText(payload) {{
      for (const key of ["output", "stderr", "stdout", "result", "message", "error", "delta", "chunk"]) {{
        const value = payload[key];
        if (value === undefined || value === null) continue;
        if (typeof value === "object") return JSON.stringify(value, null, 2);
        const text = String(value || "").trim();
        if (text) return text;
      }}
      return "";
    }}
    function marvisWorkLogStableKey(prefix, nativeEvent, label) {{
      const payload = nativeEventPayload(nativeEvent);
      const stable = payload.itemId || payload.item_id || payload.call_id || payload.tool_call_id || payload.message_id || payload.native_message_id || payload.native_turn_id || payload.turnId || label || nativeEvent?.id || `${{Date.now()}}:${{Math.random()}}`;
      return `${{prefix}}:${{stable}}`;
    }}
    function marvisWorkLogTextIsProtocolNoise(text) {{
      const value = String(text || "").trim();
      if (!value) return true;
      if (relayTextLooksLikeEnvelope(value)) return true;
      const markers = ["artifact_type", "relay_role", "final_summary", "routing_decision", "acceptance_criteria", "evidence_refs", "handoff_to", "required_roles"];
      if (markers.some((marker) => value.includes(marker)) && (value.includes("{{") || value.includes("}}") || value.includes('",') || value.includes('":') || value.startsWith('"') || value.startsWith("["))) {{
        return true;
      }}
      return false;
    }}
    function marvisWorkLogCleanProtocolSummary(text) {{
      const value = String(text || "").trim();
      if (!value) return "";
      if (!marvisWorkLogTextIsProtocolNoise(value)) return value;
      let source = value;
      let match = source.match(/"summary"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"/);
      if (!match && source.includes('\\\\"summary\\\\"')) {{
        source = source.replace(/\\\\"/g, '"');
        match = source.match(/"summary"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"/);
      }}
      if (!match) return "";
      let cleaned = match[1] || "";
      try {{
        cleaned = JSON.parse(`"${{cleaned}}"`);
      }} catch (_error) {{}}
      cleaned = String(cleaned || "").trim();
      return cleaned && !marvisWorkLogTextIsProtocolNoise(cleaned) ? cleaned : "";
    }}
    function marvisWorkLogShouldFoldText(text) {{
      const value = String(text || "");
      if (value.length > 600) return true;
      if (value.includes("```")) return true;
      const stripped = value.trimStart();
      if ((stripped.startsWith("{{") || stripped.startsWith("[")) && value.length > 240) return true;
      if (/<(?:!doctype|html|body|script|style|pre|div|section)\\b/i.test(value)) return true;
      return value.split(/\\r?\\n/).some((line) => line.length > 220);
    }}
    function marvisWorkLogCompactText(text) {{
      const value = String(text || "").trim();
      if (!value) return {{ text: "", output: "" }};
      if (!marvisWorkLogShouldFoldText(value)) return {{ text: value, output: "" }};
      return {{ text: "输出较长，已折叠。", output: value }};
    }}
    function marvisWorkLogEntryFromNativeEvent(role, nativeEvent) {{
      const kind = marvisWorkLogNativeKind(nativeEvent);
      const type = nativeEvent?.type || "";
      const payload = nativeEventPayload(nativeEvent);
      if (kind === "usage_updated" || type === "model.usage.updated") return {{ usage: true }};
      if (kind === "user_message" || kind === "reasoning_delta") return null;
      if (kind === "text_delta" || kind === "message_completed") {{
        const text = nativeEventText(nativeEvent);
        const key = marvisWorkLogStableKey(`message:${{role || ""}}`, nativeEvent, "assistant");
        if (marvisWorkLogTextIsProtocolNoise(text)) {{
          if (kind === "message_completed") return {{ removeKey: key }};
          return null;
        }}
        const compact = marvisWorkLogCompactText(text);
        return {{
          kind: "message",
          key,
          text: relaySanitizeProtocolLeakText(role, compact.text),
          output: compact.output,
          replaceText: kind === "message_completed",
        }};
      }}
      if (kind.startsWith("tool_call")) {{
        const label = marvisWorkLogToolLabel(payload) || "tool";
        return {{
          kind: "tool",
          key: marvisWorkLogStableKey("tool", nativeEvent, label),
          chip: `${{label}} ${{marvisWorkLogEventStatus(kind)}}`,
          output: marvisWorkLogOutputText(payload),
          failed: kind.endsWith("_failed"),
        }};
      }}
      if (kind.startsWith("command")) {{
        const label = marvisWorkLogCommandLabel(payload) || "command";
        return {{
          kind: "command",
          key: marvisWorkLogStableKey("command", nativeEvent, label),
          chip: `${{label}} ${{marvisWorkLogEventStatus(kind)}}`,
          output: marvisWorkLogOutputText(payload),
          failed: kind.endsWith("_failed"),
        }};
      }}
      if (kind === "approval_requested" || kind === "approval_resolved") {{
        return {{
          kind: "approval",
          key: marvisWorkLogStableKey("approval", nativeEvent, kind),
          chip: kind === "approval_requested" ? "等待审批" : "审批已处理",
          text: nativeEventText(nativeEvent),
        }};
      }}
      if (kind === "file_changed" || kind === "diff_updated") {{
        const label = kind === "file_changed" ? "文件变更" : "差异更新";
        const fileText = payload.path || payload.file || payload.filename || payload.summary || payload.message || "";
        return {{
          kind: "file",
          key: marvisWorkLogStableKey("file", nativeEvent, label),
          chip: `${{label}} 已记录`,
          text: String(fileText || ""),
        }};
      }}
      if (["activity", "lifecycle", "completed", "failed"].includes(kind)) {{
        let text = nativeEventText(nativeEvent);
        if (!text && kind === "failed") text = String(payload.error || payload.reason || payload.status || "调用失败");
        if (!text) return null;
        return {{
          kind,
          key: marvisWorkLogStableKey(kind, nativeEvent, kind),
          text,
          chip: kind === "failed" ? "调用失败" : "",
          failed: kind === "failed",
        }};
      }}
      return null;
    }}
    function createMarvisWorkLogAvatar(role) {{
      const avatar = document.createElement("span");
      avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{marvisWorkLogRolePersona(role)}}`;
      avatar.setAttribute("aria-label", marvisWorkLogRoleLabel(role));
      return avatar;
    }}
    function ensureMarvisWorkLogSegment(role) {{
      if (!marvisWorkLogBody) return null;
      const empty = marvisWorkLogBody.querySelector(".marvis-work-log-empty");
      if (empty) empty.remove();
      const segments = Array.from(marvisWorkLogBody.querySelectorAll("[data-marvis-work-log-segment]"));
      const artifact = marvisWorkLogBody.querySelector("[data-marvis-work-log-artifacts]");
      const lastSegment = segments[segments.length - 1];
      if (lastSegment && lastSegment.dataset.marvisWorkLogSegment === role) return lastSegment;
      const section = document.createElement("section");
      section.className = "marvis-work-log-role marvis-work-log-segment";
      section.dataset.marvisWorkLogRole = role || "";
      section.dataset.marvisWorkLogSegment = role || "";
      section.dataset.marvisWorkLogSegmentIndex = String(segments.length);
      const main = document.createElement("div");
      main.className = "marvis-work-log-role-main";
      const title = document.createElement("h3");
      title.textContent = marvisWorkLogRoleLabel(role);
      const line = document.createElement("div");
      line.className = "marvis-work-log-line";
      main.append(title, line);
      section.append(createMarvisWorkLogAvatar(role), main);
      if (artifact) marvisWorkLogBody.insertBefore(section, artifact);
      else marvisWorkLogBody.appendChild(section);
      return section;
    }}
    function renderMarvisWorkLogEntry(segment, entry) {{
      if (!entry || entry.usage) return;
      if (entry.removeKey) {{
        document.querySelectorAll(`[data-marvis-work-log-entry-key="${{CSS.escape(entry.removeKey)}}"]`).forEach((node) => node.remove());
        return;
      }}
      if (!segment) return;
      const line = segment.querySelector(".marvis-work-log-line");
      if (!line) return;
      let node = entry.key ? line.querySelector(`[data-marvis-work-log-entry-key="${{CSS.escape(entry.key)}}"]`) : null;
      if (!node) {{
        node = document.createElement("div");
        node.className = "marvis-work-log-entry";
        node.dataset.marvisWorkLogEntry = entry.kind || "event";
        if (entry.key) node.dataset.marvisWorkLogEntryKey = entry.key;
        const paragraph = document.createElement("p");
        node.appendChild(paragraph);
        line.appendChild(node);
      }}
      node.classList.toggle("is-failed", Boolean(entry.failed));
      if (entry.chip) {{
        const chipText = relayReplaceLegacyRoleDisplayNames(entry.chip);
        let chip = node.querySelector(".marvis-work-log-tool-chip");
        if (!chip) {{
          chip = document.createElement("span");
          chip.className = "marvis-work-log-tool-chip";
          node.insertBefore(chip, node.firstChild);
        }}
        chip.textContent = chipText;
      }}
      const paragraph = node.querySelector("p") || node.appendChild(document.createElement("p"));
      if (entry.text) {{
        const entryText = relayReplaceLegacyRoleDisplayNames(entry.text);
        paragraph.textContent = entry.replaceText ? entryText : `${{paragraph.textContent || ""}}${{entryText}}`;
        const cleaned = marvisWorkLogCleanProtocolSummary(paragraph.textContent);
        if (cleaned && cleaned !== paragraph.textContent) {{
          paragraph.textContent = cleaned;
        }} else if (entry.replaceText && marvisWorkLogTextIsProtocolNoise(paragraph.textContent)) {{
          node.remove();
          return;
        }}
      }}
      if (entry.output) {{
        let details = node.querySelector("[data-marvis-work-log-output]");
        if (!details) {{
          details = document.createElement("details");
          details.className = "marvis-work-log-output";
          details.dataset.marvisWorkLogOutput = "";
          const summary = document.createElement("summary");
          summary.textContent = "查看输出";
          const pre = document.createElement("pre");
          details.append(summary, pre);
          node.appendChild(details);
        }}
        const pre = details.querySelector("pre");
        const entryOutput = relayReplaceLegacyRoleDisplayNames(entry.output);
        if (pre && !pre.textContent.includes(entryOutput)) {{
          pre.textContent = pre.textContent ? `${{pre.textContent}}\\n${{entryOutput}}` : entryOutput;
        }}
      }}
      marvisWorkLogBody?.scrollTo({{ top: marvisWorkLogBody.scrollHeight, behavior: "smooth" }});
    }}
    function renderMarvisWorkLogNativeEvent(role, nativeEvent, runtimeEventId = "") {{
      if (!marvisWorkLogBody || !nativeEvent) return;
      const numericId = Number(runtimeEventId || nativeEvent?.id || 0);
      if (numericId && numericId <= marvisWorkLogInitialMaxEventId) return;
      if (numericId && marvisWorkLogSeenRuntimeIds.has(numericId)) return;
      if (numericId) marvisWorkLogSeenRuntimeIds.add(numericId);
      const entry = marvisWorkLogEntryFromNativeEvent(role, nativeEvent);
      if (!entry) return;
      if (entry.usage) {{
        updateMarvisWorkLogTokenTotal(nativeEvent, runtimeEventId);
        return;
      }}
      if (entry.removeKey) {{
        renderMarvisWorkLogEntry(null, entry);
        return;
      }}
      const segment = ensureMarvisWorkLogSegment(role || "");
      renderMarvisWorkLogEntry(segment, entry);
    }}
    function relayRiskLabel(risk) {{
      const labels = {{ low: "低", medium: "中", high: "高", critical: "关键" }};
      return labels[risk] || risk || "";
    }}
    function relayHumanizeEnvelope(envelope) {{
      const lines = [];
      const summary = relayHumanizeDisplayText(
        envelope.summary || envelope.output || envelope.reason || "",
        "该角色已返回结构化结果，详情见结构化数据。"
      ).trim();
      if (summary) lines.push(`结论：${{summary}}`);
      const nextAction = relayHumanizeDisplayText(
        envelope.next_action || "",
        "下一步见结构化数据。"
      ).trim();
      if (nextAction) lines.push(`下一步：${{nextAction}}`);
      const questions = relayHumanizeDisplayText(
        relayJoinTextList(envelope.open_questions),
        "待确认内容见结构化数据。"
      );
      if (questions) lines.push(`待确认：${{questions}}`);
      const route = String(envelope.route || "").trim();
      const risk = String(envelope.risk || "").trim();
      if (route || risk) {{
        const parts = [];
        if (route) parts.push(`路径：${{relayRouteLabel(route)}}`);
        if (risk) parts.push(`风险：${{relayRiskLabel(risk)}}`);
        lines.push(parts.join(" · "));
      }}
      const acceptance = relayHumanizeDisplayText(
        relayJoinTextList(envelope.acceptance_criteria),
        "验收依据见结构化数据。"
      );
      if (acceptance) lines.push(`验收依据：${{acceptance}}`);
      return lines.length ? lines.join("\\n") : "角色已返回结构化结果。";
    }}
    function nativeMessageKey(role, nativeEvent, bucket = "") {{
      const payload = nativeEventPayload(nativeEvent);
      const stable = payload.itemId || payload.item_id || payload.message_id || payload.native_message_id || payload.native_turn_id || payload.turnId || nativeEvent?.id || `${{Date.now()}}:${{Math.random()}}`;
      return `${{bucket}}:${{role || ""}}:${{stable}}`;
    }}
    function setNativeBodyText(node, text, append = false) {{
      const body = node?.querySelector("[data-native-message-body]");
      if (!body) return;
      if (append) body.textContent += text;
      else body.textContent = text;
      scrollNativeConversationToEnd();
    }}
    function createNativeMessage(role, kind, speaker, meta, key) {{
      if (!conversationTimeline) return null;
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const block = document.createElement("article");
      block.className = "relay-message";
      block.dataset.nativeRole = role || "system";
      block.dataset.nativeKind = kind || "status";
      if (key) block.dataset.nativeKey = key;
      const avatar = document.createElement("span");
      avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{role || "system"}}`;
      avatar.setAttribute("aria-label", speaker || labelForRole(role));
      const head = document.createElement("div");
      head.className = "relay-message-head";
      const title = document.createElement("strong");
      title.textContent = speaker || labelForRole(role);
      head.appendChild(title);
      if (meta) {{
        const metaNode = document.createElement("span");
        metaNode.className = "relay-message-meta";
        metaNode.textContent = meta;
        head.appendChild(metaNode);
      }}
      const body = document.createElement("div");
      body.className = "relay-message-body";
      body.dataset.nativeMessageBody = "";
      block.appendChild(avatar);
      block.appendChild(head);
      block.appendChild(body);
      conversationTimeline.appendChild(block);
      scrollNativeConversationToEnd();
      return block;
    }}
    function marvisConversationPersona(role) {{
      if (role === "architect") return "computer";
      if (role === "implementer") return "app";
      if (role === "tester") return "search";
      if (role === "auditor") return "file";
      return "marvis";
    }}
    function appendMarvisConversationUser(text, key = "", pending = false) {{
      if (!conversationTimeline) return null;
      const body = relayHumanizeUserMessage(text);
      const normalizedBody = relayNormalizeConversationText(body);
      if (!normalizedBody || relayUserMessageIsRetryOrContext(body)) return null;
      if (conversationUserBodies.has(normalizedBody)) return null;
      conversationUserBodies.add(normalizedBody);
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const node = document.createElement("article");
      node.className = "marvis-relay-user-message";
      node.dataset.nativeRole = "user";
      node.dataset.nativeKind = "user_message";
      if (key) node.dataset.nativeKey = key;
      if (pending) node.dataset.pendingFollowup = "true";
      const bubble = document.createElement("div");
      bubble.className = "marvis-relay-user-bubble";
      bubble.dataset.nativeMessageBody = "";
      bubble.textContent = body;
      node.appendChild(bubble);
      conversationTimeline.appendChild(node);
      if (key) nativeTranscriptNodes.set(key, node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function markMarvisConversationUserFailed(key) {{
      if (!key) return;
      const node = nativeTranscriptNodes.get(key) || conversationTimeline?.querySelector(`[data-native-key='${{CSS.escape(key)}}']`);
      if (!node) return;
      node.classList.add("is-failed");
      node.dataset.pendingFollowup = "failed";
      node.title = "发送失败";
    }}
    function appendMarvisConversationWaiting() {{
      if (!conversationTimeline) return null;
      let node = conversationTimeline.querySelector("[data-marvis-followup-waiting]");
      if (node) return node;
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      node = document.createElement("article");
      node.className = "marvis-relay-agent-step marvis-relay-waiting";
      node.dataset.nativeRole = "director";
      node.dataset.nativeKind = "waiting";
      node.dataset.marvisFollowupWaiting = "true";
      const avatar = document.createElement("span");
      avatar.className = "marvis-relay-avatar marvis-relay-avatar-marvis";
      avatar.setAttribute("aria-label", "Marvis");
      const content = document.createElement("div");
      content.className = "marvis-relay-agent-content";
      const head = document.createElement("div");
      head.className = "marvis-relay-agent-head";
      const title = document.createElement("strong");
      title.textContent = "Marvis";
      const action = document.createElement("span");
      action.className = "marvis-relay-agent-action";
      action.textContent = "| 任务分配 进行中";
      head.append(title, document.createTextNode(" "), action);
      const bubble = document.createElement("div");
      bubble.className = "marvis-relay-agent-bubble";
      bubble.dataset.nativeMessageBody = "";
      bubble.textContent = "...";
      content.append(head, bubble);
      node.append(avatar, content);
      conversationTimeline.appendChild(node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function clearMarvisConversationWaiting() {{
      conversationTimeline?.querySelectorAll("[data-marvis-followup-waiting]").forEach((node) => node.remove());
    }}
    function appendMarvisConversationAssistant(role, text, kind = "followup_response", key = "", status = "passed") {{
      if (!conversationTimeline || !text) return null;
      clearMarvisConversationWaiting();
      const existing = key ? nativeTranscriptNodes.get(key) || conversationTimeline.querySelector(`[data-native-key='${{CSS.escape(key)}}']`) : null;
      const node = existing || document.createElement("article");
      node.className = "marvis-relay-agent-step";
      node.dataset.nativeRole = role || "director";
      node.dataset.nativeKind = kind || "followup_response";
      if (key) node.dataset.nativeKey = key;
      if (!existing) {{
        const avatar = document.createElement("span");
        avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{marvisConversationPersona(role)}}`;
        avatar.setAttribute("aria-label", labelForRole(role));
        const content = document.createElement("div");
        content.className = "marvis-relay-agent-content";
        const head = document.createElement("div");
        head.className = "marvis-relay-agent-head";
        const title = document.createElement("strong");
        title.textContent = labelForRole(role);
        const action = document.createElement("span");
        action.className = "marvis-relay-agent-action";
        action.textContent = `| 任务分配 ${{labelForStatus(status) || "已完成"}}`;
        head.append(title, document.createTextNode(" "), action);
        const bubble = document.createElement("div");
        bubble.className = "marvis-relay-agent-bubble";
        bubble.dataset.nativeMessageBody = "";
        content.append(head, bubble);
        node.append(avatar, content);
        conversationTimeline.appendChild(node);
      }}
      setNativeBodyText(node, text);
      if (key) nativeTranscriptNodes.set(key, node);
      return node;
    }}
    function appendMarvisConversationHandoff(toRole, key = "", fromRole = "") {{
      if (!conversationTimeline || !toRole) return null;
      if (!fromRole || fromRole === toRole || toRole === "director") return null;
      const existingPair = conversationTimeline.querySelector(
        `[data-marvis-handoff][data-native-from-role='${{CSS.escape(fromRole)}}'][data-native-to-role='${{CSS.escape(toRole)}}']`
      );
      if (existingPair) return existingPair;
      const handoffKey = key || `handoff:${{toRole}}`;
      let node = conversationTimeline.querySelector(`[data-native-key='${{CSS.escape(handoffKey)}}']`);
      if (node) return node;
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      node = document.createElement("div");
      node.className = "marvis-relay-handoff";
      node.dataset.marvisHandoff = "";
      node.dataset.nativeKind = "handoff";
      node.dataset.nativeRole = toRole;
      node.dataset.nativeFromRole = fromRole;
      node.dataset.nativeToRole = toRole;
      node.dataset.nativeKey = handoffKey;
      node.textContent = marvisHandoffText(fromRole, toRole);
      conversationTimeline.appendChild(node);
      nativeTranscriptNodes.set(handoffKey, node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function renderRelayNativeEvent(role, nativeEvent, runtimeEventId = "") {{
      if (!conversationTimeline || !nativeEvent) return;
      const kind = nativeEvent.kind || "event";
      const payload = nativeEventPayload(nativeEvent);
      const text = nativeEventText(nativeEvent);
      const provider = nativeEvent.source || payload.provider || "";
      const roleLabel = labelForRole(role);
      if (kind === "user_message") {{
        const body = relayHumanizeUserMessage(text);
        const normalizedBody = relayNormalizeConversationText(body);
        if (!normalizedBody || relayUserMessageIsRetryOrContext(body) || conversationUserBodies.has(normalizedBody)) return;
        conversationUserBodies.add(normalizedBody);
        const key = nativeMessageKey(role, nativeEvent, "user");
        let node = nativeTranscriptNodes.get(key);
        if (!node) {{
          node = createNativeMessage(role, "user_message", "你", roleLabel, key);
          nativeTranscriptNodes.set(key, node);
        }}
        setNativeBodyText(node, body);
        return;
      }}
      if (kind === "text_delta") {{
        appendRolePreview(role, text, runtimeEventId || nativeEvent?.id);
        setRoleStatus(role, "streaming");
        return;
      }}
      if (kind === "text_delta" || kind === "message_completed") {{
        const key = nativeMessageKey(role, nativeEvent, "assistant");
        const bufferedEnvelope = nativeEnvelopeBuffers.get(key) || "";
        if (bufferedEnvelope || relayTextLooksLikeEnvelope(text)) {{
          const candidate = kind === "text_delta" ? bufferedEnvelope + text : text || bufferedEnvelope;
          if (kind === "text_delta") nativeEnvelopeBuffers.set(key, candidate);
          const envelope = relayParseEnvelope(candidate);
          if (!envelope) {{
            if (kind === "message_completed") {{
              nativeEnvelopeBuffers.delete(key);
            }}
            return;
          }}
          nativeEnvelopeBuffers.delete(key);
          renderRoleEnvelope(role, envelope);
          setRoleStatus(role, "streaming");
          return;
        }}
        return;
      }}
    }}
    document.querySelectorAll("[data-native-key]").forEach((node) => {{
      if (node.dataset.nativeKey) nativeTranscriptNodes.set(node.dataset.nativeKey, node);
    }});
    document.querySelectorAll('[data-native-kind="user_message"] [data-native-message-body]').forEach((node) => {{
      const normalizedBody = relayNormalizeConversationText(node.textContent || "");
      if (normalizedBody) conversationUserBodies.add(normalizedBody);
    }});
    function previewEventKey(role, eventId) {{
      const value = String(eventId || "").trim();
      return value ? `${{role || ""}}:${{value}}` : "";
    }}
    function relayPreviewDisplayText(role, text) {{
      return `${{labelForRole(role)}}正在处理任务，完成后展示结果。`;
    }}
    function appendRolePreview(role, text, eventId = "") {{
      if (!role || !text) return;
      if (TERMINAL_ROLE_STATUSES.has(currentRoleStatus(role))) return;
      const eventKey = previewEventKey(role, eventId);
      if (eventKey && seenPreviewEventKeys.has(eventKey)) return;
      if (eventKey) seenPreviewEventKeys.add(eventKey);
      const preview = rolePreviews[role];
      if (preview) {{
        preview.classList.remove("is-idle");
        preview.textContent = relayPreviewDisplayText(role, text);
        preview.scrollTop = preview.scrollHeight;
      }}
      if (!conversationTimeline) return;
      if (conversationTimeline.querySelector(`[data-conversation-role-final="${{role}}"]`)) return;
      let node = conversationTimeline.querySelector(`[data-conversation-role-preview="${{role}}"]`);
      if (!node) {{
        node = createNativeMessage(role, "text_delta", labelForRole(role), "实时预览", `preview:${{role}}`);
        if (!node) return;
        node.dataset.conversationRolePreview = role;
      }}
      setNativeBodyText(node, relayPreviewDisplayText(role, text));
    }}
    function clearRolePreview(role) {{
      const preview = rolePreviews[role];
      if (preview) {{
        preview.textContent = "";
        preview.classList.add("is-idle");
      }}
      conversationTimeline?.querySelector(`[data-conversation-role-preview="${{role}}"]`)?.remove();
    }}
    function clearAllRolePreviews() {{
      Object.keys(rolePreviews).forEach(clearRolePreview);
    }}
    function canonicalEnvelopeJson(envelope) {{
      return JSON.stringify(envelope || {{}}, null, 2);
    }}
    function ensureCanonicalDetails(container, role, envelope) {{
      if (!container || !envelope) return;
      let details = container.querySelector(".role-canonical-json");
      if (!details) {{
        details = document.createElement("details");
        details.className = "role-canonical-json";
        const summary = document.createElement("summary");
        summary.textContent = "查看结构化数据";
        const pre = document.createElement("pre");
        pre.dataset.roleCanonicalJson = role;
        details.append(summary, pre);
        container.appendChild(details);
      }}
      const pre = details.querySelector("[data-role-canonical-json]");
      if (pre) pre.textContent = canonicalEnvelopeJson(envelope);
    }}
    function renderRoleEnvelope(role, envelope) {{
      if (!role || !envelope) return;
      clearRolePreview(role);
      const summaryText = relayHumanizeEnvelope(envelope);
      const output = roleOutputs[role];
      if (output) {{
        output.classList.remove("is-idle");
        output.textContent = summaryText;
        ensureCanonicalDetails(output.parentElement, role, envelope);
      }}
      if (conversationTimeline) {{
        const key = `canonical:${{role}}:${{envelope.artifact_type || "role_envelope"}}`;
        let node = conversationTimeline.querySelector(`[data-conversation-role-final="${{role}}"]`);
        if (!node) {{
          node = createNativeMessage(role, "role_envelope", labelForRole(role), labelForStatus(envelope.status || "passed"), key);
          if (!node) return;
          node.dataset.conversationRoleFinal = role;
        }}
        node.dataset.nativeKind = "role_envelope";
        setNativeBodyText(node, summaryText);
        ensureCanonicalDetails(node, role, envelope);
      }}
    }}
    document.querySelectorAll("[data-conversation-role-preview]").forEach((node) => {{
      const role = node.dataset.conversationRolePreview;
      const rawPreview = node.dataset.rawPreview || "";
      const preview = rolePreviews[role];
      if (preview && rawPreview) {{
        preview.dataset.rawPreview = rawPreview;
        preview.textContent = relayPreviewDisplayText(role, rawPreview);
        preview.classList.remove("is-idle");
      }}
      (node.dataset.previewEventIds || "").split(",").filter(Boolean).forEach((eventId) => {{
        const eventKey = previewEventKey(role, eventId);
        if (eventKey) seenPreviewEventKeys.add(eventKey);
      }});
    }});
    const TERMINAL_ROLE_STATUSES = new Set(["passed", "completed", "blocked", "failed", "interrupted"]);
    function currentRoleStatus(role) {{
      const lane = document.querySelector(`[data-role="${{role}}"]`);
      const statusNode = lane?.querySelector(".role-status");
      return statusNode?.dataset.status || "";
    }}
    function canApplyRoleStatus(role, status) {{
      if (!status) return false;
      const currentStatus = currentRoleStatus(role);
      if (TERMINAL_ROLE_STATUSES.has(currentStatus) && !TERMINAL_ROLE_STATUSES.has(status)) return false;
      return true;
    }}
    function setRoleStatus(role, status) {{
      if (!canApplyRoleStatus(role, status)) return;
      const lane = document.querySelector(`[data-role="${{role}}"]`);
      const statusNode = lane?.querySelector(".role-status");
      if (statusNode && status) {{
        statusNode.textContent = labelForStatus(status);
        statusNode.dataset.status = status;
      }}
      const progress = document.querySelector(`[data-progress-role="${{role}}"]`);
      if (progress && status) {{
        progress.dataset.progressStatus = status;
        const textNode = progress.querySelector(".relay-progress-status");
        if (textNode) textNode.textContent = labelForStatus(status);
      }}
      const output = roleOutputs[role];
      if (output && status && status !== "idle") output.classList.remove("is-idle");
    }}
    function appendActivity(text) {{
      const log = document.querySelector("[data-activity-log]");
      if (!log || !text) return;
      const item = document.createElement("li");
      item.innerHTML = '<span class="relay-activity-dot" aria-hidden="true"></span><span></span>';
      item.querySelector("span:last-child").textContent = text;
      log.prepend(item);
    }}
    const followupComposer = document.querySelector("[data-marvis-followup-composer]");
    const followupTextInput = followupComposer?.querySelector("textarea[name='text']");
    const followupSubmitButton = followupComposer?.querySelector("[data-marvis-submit]");
    let relayTaskStatus = followupComposer?.dataset.taskStatusValue || "";
    function relayTaskIsRunning() {{
      return ["queued", "running", "streaming"].includes(String(relayTaskStatus || "").trim());
    }}
    function relayFollowupHasText() {{
      return Boolean(String(followupTextInput?.value || "").trim());
    }}
    function updateRelayComposerAction() {{
      if (!followupSubmitButton) return;
      const showStop = relayTaskIsRunning() && !relayFollowupHasText();
      followupSubmitButton.classList.toggle("is-stop", showStop);
      followupSubmitButton.setAttribute("aria-label", showStop ? "中断任务" : "发送补充");
    }}
    function updateTaskStatus(status) {{
      if (!status) return;
      relayTaskStatus = String(status || "");
      if (followupComposer) followupComposer.dataset.taskStatusValue = relayTaskStatus;
      document.querySelectorAll("[data-task-status]").forEach((node) => {{
        node.textContent = TASK_STATUS_LABELS[status] || status;
      }});
      updateRelayComposerAction();
    }}
    function updateNativeLink(role, provider, nativeSessionId) {{
      const lane = document.querySelector(`[data-role="${{role}}"]`);
      const linkWrap = lane?.querySelector("[data-native-link]");
      const linkProvider = provider || lane?.dataset.roleProvider || "";
      if (!linkWrap || !linkProvider || !nativeSessionId) return;
      const separator = TOKEN_SUFFIX ? `&${{TOKEN_SUFFIX.slice(1)}}` : "";
      linkWrap.innerHTML = `<a class="role-link" href="/native/${{encodeURIComponent(linkProvider)}}?native_thread_id=${{encodeURIComponent(nativeSessionId)}}${{separator}}">打开原生会话</a>`;
    }}
    const source = new EventSource(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/events${{EVENTS_SUFFIX}}`);
    source.addEventListener("role.queued", (event) => {{
      const payload = parseRelayEvent(event);
      setRoleStatus(payload.role, "queued");
      appendActivity(`${{labelForRole(payload.role)}} 已进入队列，等待启动。`);
    }});
    source.addEventListener("role.streaming", (event) => {{
      const payload = parseRelayEvent(event);
      setRoleStatus(payload.role, "streaming");
      updateNativeLink(payload.role, payload.provider, payload.native_session_id);
      appendActivity(`${{labelForRole(payload.role)}} 开始执行。`);
    }});
    source.addEventListener("dispatch.verified", (event) => {{
      const payload = parseRelayEvent(event);
      setRoleStatus(payload.role, "streaming");
      updateNativeLink(payload.role, payload.provider, payload.native_session_id);
      appendActivity(`${{labelForRole(payload.role)}} 的原生会话已启动。`);
    }});
    source.addEventListener("dispatch.fallback", (event) => {{
      const payload = parseRelayEvent(event);
      appendActivity(`${{labelForRole(payload.role)}} 调度降级：${{payload.reason || "使用可用 provider 继续"}}`);
    }});
    source.addEventListener("user.followup", (event) => {{
      const payload = parseRelayEvent(event);
      const key = payload.artifact_id ? `user_followup:${{payload.artifact_id}}` : `user_followup:${{payload.context_packet_id || Date.now()}}`;
      appendMarvisConversationUser(payload.text || payload.latest_user_input || "", key, false);
      appendMarvisConversationWaiting();
      updateTaskStatus("running");
      setRoleStatus("director", "queued");
    }});
    source.addEventListener("role.native_event", (event) => {{
      const payload = parseRelayEvent(event);
      renderRelayNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);
      renderMarvisWorkLogNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);
    }});
    source.addEventListener("role.output_delta", (event) => {{
      const payload = parseRelayEvent(event);
      appendRolePreview(payload.role, payload.delta || payload.text || "", payload.runtime_event_id);
      setRoleStatus(payload.role, "streaming");
    }});
    source.addEventListener("role.followup_response", (event) => {{
      const payload = parseRelayEvent(event);
      appendMarvisConversationAssistant(
        payload.role || "director",
        payload.text || payload.summary || "",
        "followup_response",
        payload.artifact_id ? `followup_response:${{payload.artifact_id}}` : "",
        payload.status || "passed"
      );
      setRoleStatus(payload.role || "director", payload.status || "passed");
    }});
    source.addEventListener("routing.decision", (event) => {{
      const payload = parseRelayEvent(event);
      const routeLabels = {{
        director_only: "总工程师直接完成",
        core_relay: "核心接力",
        full_relay: "完整五角色接力",
        audit_first: "审计优先",
        waiting_user: "等待用户确认",
        blocked: "已阻塞",
      }};
      const riskLabels = {{ low: "低", medium: "中", high: "高", critical: "关键" }};
      const routeText = routeLabels[payload.route] || payload.route || "等待总工程师判断";
      const stops = relayHumanizeDisplayText((payload.stop_conditions || []).join("、")) || "暂无额外停止条件";
      const routingSummary = document.querySelector("[data-routing-summary]");
      const routingRoute = document.querySelector("[data-routing-route]");
      const routingComplexity = document.querySelector("[data-routing-complexity]");
      const routingRisk = document.querySelector("[data-routing-risk]");
      const routingPath = document.querySelector("[data-routing-path]");
      const routingRoles = document.querySelector("[data-routing-roles]");
      const routingAcceptance = document.querySelector("[data-routing-acceptance]");
      const routingStops = document.querySelector("[data-routing-stops]");
      if (routingSummary) routingSummary.textContent = relayHumanizeDisplayText(payload.summary || "总工程师已形成调度决策。");
      if (routingRoute) routingRoute.textContent = routeText;
      if (routingComplexity) routingComplexity.textContent = payload.complexity || "待判断";
      if (routingRisk) routingRisk.textContent = riskLabels[payload.risk] || payload.risk || "待判断";
      if (routingPath) routingPath.textContent = routeText;
      if (routingRoles) routingRoles.textContent = (payload.required_roles || []).map(labelForRole).join("、") || "等待总工程师判断";
      if (routingAcceptance) routingAcceptance.textContent = relayHumanizeDisplayText((payload.acceptance_criteria || []).join("、")) || "等待总工程师给出验收依据";
      if (routingStops) routingStops.textContent = `${{stops}} · ${{payload.requires_user_approval ? "需要用户确认" : "无需额外确认"}}`;
      appendActivity(`总工程师调度决策：${{routeText}}。`);
    }});
    source.addEventListener("role.envelope", (event) => {{
      const payload = parseRelayEvent(event);
      const envelope = payload.envelope || payload;
      const role = payload.role || envelope.role;
      renderRoleEnvelope(role, envelope);
      if (envelope.summary) {{
        const summaryText = relayHumanizeDisplayText(envelope.summary);
        appendActivity(`${{labelForRole(role)}} 产出摘要：${{summaryText}}`);
        if (role === "director") {{
          const summary = document.querySelector("[data-board-director-summary]");
          if (summary) summary.textContent = summaryText;
        }}
      }}
      if (envelope.next_action) {{
        const nextActionText = relayHumanizeDisplayText(envelope.next_action);
        const next = document.querySelector("[data-board-next-step]");
        if (next) next.textContent = nextActionText;
      }}
      if (envelope.status) setRoleStatus(role, envelope.status);
    }});
    source.addEventListener("handoff.created", (event) => {{
      const payload = parseRelayEvent(event);
      const toRole = payload.to_role || payload.handoff_to;
      const fromRole = payload.from_role || "";
      if (toRole) setRoleStatus(toRole, "queued");
      const handoffKey = `handoff:${{fromRole}}:${{toRole || ""}}:${{payload.artifact_id || payload.summary || event.lastEventId || ""}}`;
      appendMarvisConversationHandoff(toRole, handoffKey, fromRole);
      appendActivity(`${{labelForRole(payload.from_role)}} 已交接给 ${{labelForRole(toRole)}}：${{payload.summary || "等待下一角色处理"}}`);
    }});
    source.addEventListener("role.status", (event) => {{
      const payload = parseRelayEvent(event);
      setRoleStatus(payload.role, payload.status);
      if (TERMINAL_ROLE_STATUSES.has(payload.status)) clearRolePreview(payload.role);
      appendActivity(`${{labelForRole(payload.role)}} 状态更新为 ${{labelForStatus(payload.status)}}。`);
    }});
    source.addEventListener("task.completed", () => {{
      updateTaskStatus("completed");
      clearAllRolePreviews();
      appendActivity("任务已完成，可以继续补充给总工程师进行追问或追加验收。");
    }});
    source.addEventListener("task.interrupted", () => {{
      updateTaskStatus("interrupted");
      clearAllRolePreviews();
      appendActivity("任务已中断。");
    }});
    followupTextInput?.addEventListener("input", updateRelayComposerAction);
    updateRelayComposerAction();
    followupComposer?.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const form = event.currentTarget;
      const data = Object.fromEntries(new FormData(form).entries());
      if (!String(data.text || "").trim()) {{
        if (!followupSubmitButton?.classList.contains("is-stop") || !relayTaskIsRunning()) return;
        const response = await fetch(`${{form.dataset.interruptUrl}}${{TOKEN_SUFFIX}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{}}),
        }});
        if (response.ok) {{
          if (relayTaskIsRunning()) {{
            updateTaskStatus("interrupted");
            clearAllRolePreviews();
            appendActivity("你已中断任务。");
          }}
        }} else {{
          appendActivity("中断失败，请稍后重试。");
        }}
        return;
      }}
      const localKey = `local-followup:${{Date.now()}}`;
      appendMarvisConversationUser(data.text, localKey, true);
      appendMarvisConversationWaiting();
      updateTaskStatus("running");
      setRoleStatus("director", "queued");
      appendActivity("你已补充需求，已发送给总工程师。");
      const response = await fetch(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/message${{TOKEN_SUFFIX}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(data),
      }});
      if (!response.ok) {{
        markMarvisConversationUserFailed(localKey);
        clearMarvisConversationWaiting();
        appendActivity("补充发送失败，请稍后重试。");
        return;
      }}
      form.reset();
      updateRelayComposerAction();
    }});
    document.querySelector("[data-interrupt-url]")?.addEventListener("click", async (event) => {{
      const target = event.currentTarget;
      const response = await fetch(`${{target.dataset.interruptUrl}}${{TOKEN_SUFFIX}}`, {{ method: "POST" }});
      if (response.ok) {{
        updateTaskStatus("interrupted");
        appendActivity("你已中断任务。");
      }} else {{
        appendActivity("中断失败，请稍后重试。");
      }}
    }});
  </script>
</body>
</html>""")


def _relay_role_panel_html(
    job: Any,
    *,
    canonical_payload: dict[str, Any] | None = None,
) -> str:
    link = (
        f'<a class="role-link" href="{escape(_native_session_path(job.provider, job.native_session_id))}">打开原生会话</a>'
        if job.provider and job.native_session_id
        else '<span class="relay-muted">原生会话未启动</span>'
    )
    fallback = (
        f'<div class="relay-muted">调度降级：{escape(job.fallback_reason)}</div>'
        if job.fallback_reason
        else ""
    )
    error_message = str(getattr(job, "error_message", "") or "")
    error_html = (
        f'<div class="relay-muted relay-error">执行问题：{escape(error_message)}</div>'
        if error_message
        else ""
    )
    questions = "、".join(str(item) for item in job.open_questions) or "无"
    status = str(job.status or "idle")
    provider = _native_provider_display_name(str(job.provider or ""))
    display = _relay_role_display_from_job(job, canonical_payload=canonical_payload)
    output_text = str(display.get("summary_text") or "")
    output_class = "role-output is-idle" if status == "idle" else "role-output"
    canonical_json = str(display.get("canonical_json") or "")
    canonical_html = (
        '<details class="role-canonical-json">'
        "<summary>查看结构化数据</summary>"
        f'<pre data-role-canonical-json="{escape(job.role)}">{escape(canonical_json)}</pre>'
        "</details>"
        if canonical_json
        else ""
    )
    handoff = str(job.latest_handoff_summary or "暂无交接摘要")
    return f"""
      <article class="role-lane" data-role="{escape(job.role)}" data-role-provider="{escape(str(job.provider or ""))}">
        <div class="role-head">
          <h3>{escape(job.display_name)}</h3>
          <span class="role-status" data-status="{escape(status)}">{escape(_relay_role_status_label(status))}</span>
        </div>
        <div class="role-meta">
          <span class="role-provider">Provider：{escape(provider or "未配置")}</span>
          <span class="relay-muted">{escape(job.model or "默认模型")}</span>
        </div>
        {fallback}
        {error_html}
        <div class="{output_class}" data-role-output="{escape(job.role)}">{escape(output_text)}</div>
        {canonical_html}
        <div class="role-preview is-idle" data-role-preview="{escape(job.role)}"></div>
        <div class="role-notes">
          <div class="relay-muted">交接摘要：{escape(handoff)}</div>
          <div class="relay-muted">待确认问题：{escape(questions)}</div>
          <div data-native-link>{link}</div>
        </div>
      </article>
    """


def _relay_worker_events_for_roles(
    role_jobs: list[Any],
    *,
    hub: WorkerLiveStreamHub | None,
) -> list[tuple[str, int, str, str, WorkerStreamEvent]]:
    events: list[tuple[str, int, str, str, WorkerStreamEvent]] = []
    if hub is None:
        return events
    for job in role_jobs:
        agent_run_id = getattr(job, "agent_run_id", None)
        if agent_run_id is None:
            continue
        role = str(getattr(job, "role", "") or "")
        display_name = str(getattr(job, "display_name", "") or _relay_role_label(role))
        for worker_event in hub.snapshot(agent_run_id=int(agent_run_id), after_id=0, limit=500):
            events.append(
                (
                    str(worker_event.occurred_at or ""),
                    int(worker_event.id),
                    role,
                    display_name,
                    worker_event,
                )
            )
    events.sort(key=lambda item: (item[0], item[1]))
    return events


def _relay_projected_conversation_rows(
    role_jobs: list[Any],
    *,
    hub: WorkerLiveStreamHub | None,
    canonical_payloads: dict[str, dict[str, Any]] | None = None,
    canonical_payload_sequence: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> list[dict[str, str]]:
    canonical_payloads = canonical_payloads or {}
    canonical_payload_sequence = canonical_payload_sequence or list(canonical_payloads.values())
    events = _relay_worker_events_for_roles(role_jobs, hub=hub)
    job_by_role = {str(getattr(job, "role", "") or ""): job for job in role_jobs}
    completed_keys = {
        _relay_native_message_key(role, worker_event, bucket="assistant")
        for _occurred_at, _event_id, role, _display_name, worker_event in events
        if worker_event.kind == "message_completed"
        and _relay_native_event_text(worker_event).strip()
    }
    rows: list[dict[str, str]] = []
    row_by_key: dict[str, dict[str, str]] = {}
    for _occurred_at, _event_id, role, display_name, worker_event in events:
        kind = str(worker_event.kind or "event")
        text = _relay_native_event_text(worker_event)
        if kind in {"text_delta", "message_completed"} and role in canonical_payloads:
            continue
        if kind == "text_delta":
            key = _relay_native_message_key(role, worker_event, bucket="assistant")
            if key in completed_keys:
                continue
            if key not in row_by_key:
                row = {
                    "role": role,
                    "kind": kind,
                    "speaker": display_name,
                    "meta": str(worker_event.source or ""),
                    "body": "",
                    "key": key,
                    "preview_event_ids": str(worker_event.id),
                }
                rows.append(row)
                row_by_key[key] = row
            event_ids = set(
                filter(None, row_by_key[key].get("preview_event_ids", "").split(","))
            )
            event_ids.add(str(worker_event.id))
            row_by_key[key]["preview_event_ids"] = ",".join(sorted(event_ids, key=int))
            row_by_key[key]["body"] += text
            continue
        row = _relay_native_event_row(role, display_name, worker_event)
        if row is not None:
            rows.append(row)

    projected_rows: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    seen_user_bodies: set[str] = set()
    for row in rows:
        projected = _relay_project_native_conversation_row(
            row,
            job=job_by_role.get(str(row.get("role") or "")),
        )
        if projected is None and str(row.get("kind") or "") == "message_completed":
            role = str(row.get("role") or "")
            body = _relay_sanitize_protocol_leak_text(role, str(row.get("body") or ""))
            if body and not _relay_conversation_row_is_task_status_noise(
                {"kind": "message_completed", "body": body}
            ):
                projected = {**row, "body": body}
        if projected is None:
            continue
        if _relay_conversation_row_is_task_status_noise(projected):
            continue
        if str(projected.get("kind") or "") == "user_message":
            body = str(projected.get("body") or "").strip()
            if not body or body in seen_user_bodies:
                continue
            seen_user_bodies.add(body)
        key = str(projected.get("key") or "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        projected_rows.append(projected)

    if artifacts is not None:
        user_followup_texts = {
            str(artifact.get("text") or "").strip()
            for artifact in artifacts
            if str(artifact.get("artifact_type") or "") == "user_followup"
            and str(artifact.get("text") or "").strip()
        }
        for index, artifact in enumerate(artifacts):
            artifact_row = _relay_conversation_row_from_artifact(
                artifact,
                index=index,
                job_by_role=job_by_role,
                user_followup_texts=user_followup_texts,
            )
            if artifact_row is None:
                continue
            if str(artifact_row.get("kind") or "") == "user_message":
                body = str(artifact_row.get("body") or "").strip()
                if not body or body in seen_user_bodies:
                    continue
                seen_user_bodies.add(body)
            key = str(artifact_row.get("key") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            projected_rows.append(artifact_row)
    else:
        for index, payload in enumerate(canonical_payload_sequence):
            role = str(payload.get("role") or payload.get("relay_role") or "")
            if not role:
                continue
            key = (
                f"canonical:{role}:{payload.get('artifact_type') or 'role_envelope'}:"
                f"{payload.get('_relay_artifact_key') or index}"
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            projected_rows.append(
                {
                    "role": role,
                    "kind": "role_envelope",
                    "speaker": _relay_role_label(role),
                    "meta": _marvis_relay_role_status_label(
                        str(getattr(job_by_role.get(role), "status", "") or "passed")
                    ),
                    "body": _relay_humanize_role_envelope(payload),
                    "key": key,
                    "artifact_type": str(payload.get("artifact_type") or ""),
                    "status": str(payload.get("status") or ""),
                    "handoff_to": str(payload.get("handoff_to") or ""),
                    "display_summary": _relay_concrete_payload_summary(payload),
                }
            )
    _relay_enrich_generic_final_summary_rows(projected_rows)
    blocked_role = _relay_first_blocked_role(role_jobs)
    if blocked_role:
        projected_rows.append(
            {
                "role": blocked_role,
                "kind": "status",
                "speaker": "系统",
                "meta": "",
                "body": f"接力暂停在{_relay_role_label(blocked_role)}，详情见工作日志。",
                "key": f"relay-paused:{blocked_role}",
            }
        )
    return projected_rows


def _relay_conversation_row_from_artifact(
    artifact: dict[str, Any],
    *,
    index: int,
    job_by_role: dict[str, Any],
    user_followup_texts: set[str],
) -> dict[str, str] | None:
    artifact_type = str(artifact.get("artifact_type") or "")
    artifact_key = str(artifact.get("id") or artifact.get("created_at") or index)
    if artifact_type == "user_followup":
        text = str(artifact.get("text") or artifact.get("summary") or "").strip()
        if not text:
            return None
        return {
            "role": "user",
            "kind": "user_message",
            "speaker": "你",
            "meta": "",
            "body": _relay_humanize_user_message(text),
            "key": f"user_followup:{artifact_key}",
        }
    if artifact_type == "relay_board":
        text = str(artifact.get("latest_user_input") or "").strip()
        summary = str(artifact.get("summary") or "")
        next_step = str(artifact.get("next_step") or "")
        is_followup_board = (
            summary == "User follow-up routed to director"
            or next_step == "director review latest user input"
        )
        if not is_followup_board or not text or text in user_followup_texts:
            return None
        return {
            "role": "user",
            "kind": "user_message",
            "speaker": "你",
            "meta": "",
            "body": _relay_humanize_user_message(text),
            "key": f"relay_board_followup:{artifact_key}",
        }
    if artifact_type == "handoff_packet":
        from_role = str(artifact.get("from_role") or artifact.get("relay_role") or "")
        to_role = str(artifact.get("to_role") or artifact.get("handoff_to") or "")
        if not from_role or not to_role:
            return None
        return {
            "role": to_role,
            "kind": "handoff",
            "speaker": _relay_role_label(from_role),
            "meta": "",
            "body": str(artifact.get("summary") or ""),
            "key": f"handoff:{from_role}:{to_role}:{artifact_key}",
            "from_role": from_role,
            "to_role": to_role,
        }
    if artifact_type in {"role_dispatch_metadata", "role_error"}:
        return None
    if artifact_type == "followup_response":
        text = str(artifact.get("text") or artifact.get("summary") or "").strip()
        if not text:
            return None
        role = str(artifact.get("role") or artifact.get("relay_role") or "director")
        return {
            "role": role,
            "kind": "followup_response",
            "speaker": _relay_role_label(role),
            "meta": "passed",
            "body": _relay_followup_response_display_text(role, text),
            "key": f"followup_response:{artifact_key}",
            "status": "passed",
        }
    payload = _relay_canonical_payload_from_artifact(artifact)
    if payload is None:
        return None
    role = str(payload.get("role") or payload.get("relay_role") or "")
    if not role:
        return None
    return {
        "role": role,
        "kind": "role_envelope",
        "speaker": _relay_role_label(role),
        "meta": _marvis_relay_role_status_label(
            str(getattr(job_by_role.get(role), "status", "") or "passed")
        ),
        "body": _relay_humanize_role_envelope(payload),
        "key": f"canonical:{role}:{payload.get('artifact_type') or 'role_envelope'}:{artifact_key}",
        "artifact_type": str(payload.get("artifact_type") or ""),
        "status": str(payload.get("status") or ""),
        "handoff_to": str(payload.get("handoff_to") or ""),
        "display_summary": _relay_concrete_payload_summary(payload),
    }


def _relay_enrich_generic_final_summary_rows(rows: list[dict[str, str]]) -> None:
    prior_rows: list[dict[str, str]] = []
    for row in rows:
        is_director_final = (
            str(row.get("kind") or "") == "role_envelope"
            and str(row.get("role") or "") == "director"
            and str(row.get("artifact_type") or "") == "final_summary"
        )
        if is_director_final and _relay_summary_text_is_generic(str(row.get("body") or "")):
            replacement = _relay_synthesize_final_summary_from_role_rows(prior_rows)
            if replacement:
                row["body"] = replacement
        prior_rows.append(row)


def _relay_synthesize_final_summary_from_role_rows(rows: list[dict[str, str]]) -> str:
    concrete_by_role: dict[str, str] = {}
    role_order: list[str] = []
    for row in rows:
        if str(row.get("kind") or "") != "role_envelope":
            continue
        role = str(row.get("role") or "")
        if not role or role == "director":
            continue
        candidate = str(row.get("display_summary") or "").strip()
        if not candidate:
            candidate = _relay_conclusion_from_humanized_body(str(row.get("body") or ""))
        if _relay_summary_text_is_generic(candidate):
            continue
        if role not in role_order:
            role_order.append(role)
        concrete_by_role[role] = candidate

    if not concrete_by_role:
        return "结论：任务已完成，但最终摘要缺少可展示的具体变更；详细过程见工作日志。"

    parts: list[str] = []
    for role in role_order:
        candidate = concrete_by_role.get(role, "")
        if not candidate:
            continue
        if role == "auditor" and not candidate.startswith(("审核", "审计")):
            parts.append(f"审核工程师确认：{candidate}")
        else:
            parts.append(candidate)
    if not parts:
        return ""
    return "结论：" + "；".join(parts[:3])


def _relay_concrete_payload_summary(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    for field in ("summary", "reason"):
        value = str(payload.get(field) or "").strip()
        if value:
            candidates.append(value)
    acceptance = _relay_join_text_list(payload.get("acceptance_criteria"))
    if acceptance:
        candidates.append(acceptance)

    for candidate in candidates:
        value = _relay_humanize_display_text(candidate).strip()
        value = _relay_sanitize_protocol_leak_text(str(payload.get("role") or ""), value)
        if not _relay_summary_text_is_generic(value):
            return value
    return ""


def _relay_conclusion_from_humanized_body(body: str) -> str:
    for line in str(body or "").splitlines():
        value = line.strip()
        if not value.startswith("结论："):
            continue
        value = value.removeprefix("结论：").strip()
        if not _relay_summary_text_is_generic(value):
            return value
    return ""


def _relay_summary_text_is_generic(text: str) -> bool:
    value = _relay_humanize_display_text(str(text or "")).strip()
    if not value:
        return True
    value = re.sub(r"\s+", "", value).strip("。；;,.，")
    if not value:
        return True
    if _relay_text_needs_chinese_fallback(value):
        return True
    exact = {
        "completed",
        "done",
        "passed",
        "implemented",
        "success",
        "结论：已完成任务",
        "结论：任务已完成",
        "结论：该角色已返回结构化结果，详情见结构化数据",
        "角色已返回结构化结果",
        "该角色已返回结构化结果，详情见结构化数据",
        "已完成任务",
        "任务已完成",
        "已完成",
        "搞定，有请下一位",
        "修复完成，交给审核",
        "交给审核",
        "交回总工程师收尾",
        "无需返工；可以进入用户验收或收尾",
        "下一步见结构化数据",
        "验收依据见结构化数据",
    }
    if value.lower() in exact or value in exact:
        return True
    generic_fragments = (
        "详情见结构化数据",
        "有请下一位",
        "进入用户验收或收尾",
    )
    return any(fragment in value for fragment in generic_fragments)


def _marvis_relay_conversation_html(
    role_jobs: list[Any],
    *,
    hub: WorkerLiveStreamHub | None,
    canonical_payloads: dict[str, dict[str, Any]] | None = None,
    canonical_payload_sequence: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> str:
    rows = _relay_projected_conversation_rows(
        role_jobs,
        hub=hub,
        canonical_payloads=canonical_payloads,
        canonical_payload_sequence=canonical_payload_sequence,
        artifacts=artifacts,
    )
    if not rows:
        if any(str(getattr(job, "status", "") or "") in {"queued", "streaming"} for job in role_jobs):
            return _marvis_relay_waiting_message_html()
        return _marvis_relay_empty_conversation_html()
    html_rows: list[str] = []
    previous_role = ""
    handoffs_by_role: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if str(row.get("kind") or "") != "handoff":
            continue
        to_role = str(row.get("to_role") or row.get("role") or "")
        if to_role:
            handoffs_by_role.setdefault(to_role, []).append(row)
    rendered_handoffs: set[str] = set()
    rendered_handoff_pairs: set[tuple[str, str]] = set()

    def append_handoff_once(handoff: dict[str, str]) -> bool:
        key = str(handoff.get("key") or "")
        pair = _marvis_relay_handoff_pair(handoff)
        if pair is None:
            return False
        if pair in rendered_handoff_pairs:
            return True
        if key and key in rendered_handoffs:
            return True
        html = _marvis_relay_handoff_html(handoff)
        if not html:
            return False
        html_rows.append(html)
        rendered_handoff_pairs.add(pair)
        if key:
            rendered_handoffs.add(key)
        return True

    for row in rows:
        role = str(row.get("role") or "")
        kind = str(row.get("kind") or "")
        if kind == "handoff":
            continue
        if (
            kind == "role_envelope"
            and previous_role
            and role
            and role != previous_role
        ):
            for handoff in handoffs_by_role.get(role, []):
                if _marvis_relay_handoff_pair(handoff) == (previous_role, role):
                    append_handoff_once(handoff)
        if (
            kind == "role_envelope"
            and previous_role == "director"
            and role
            and role != "director"
            and (previous_role, role) not in rendered_handoff_pairs
        ):
            synthetic_key = f"synthetic-handoff:{previous_role}:{role}"
            append_handoff_once(
                {
                    "from_role": previous_role,
                    "to_role": role,
                    "role": role,
                    "key": synthetic_key,
                }
            )
        html_rows.append(_marvis_relay_message_html(row))
        if kind == "role_envelope":
            previous_role = role
    return "\n".join(html_rows)


def _marvis_relay_handoff_html(row: dict[str, str]) -> str:
    pair = _marvis_relay_handoff_pair(row)
    if pair is None:
        return ""
    from_role, to_role = pair
    key = str(row.get("key") or "")
    text = _marvis_relay_handoff_text(from_role, to_role)
    return (
        '<div class="marvis-relay-handoff" data-marvis-handoff '
        f'data-native-kind="handoff" data-native-from-role="{escape(from_role)}" '
        f'data-native-to-role="{escape(to_role)}" data-native-role="{escape(to_role)}" '
        f'data-native-key="{escape(key)}">'
        f"{escape(text)}"
        "</div>"
    )


def _marvis_relay_handoff_pair(row: dict[str, str]) -> tuple[str, str] | None:
    from_role = str(row.get("from_role") or "").strip()
    to_role = str(row.get("to_role") or row.get("role") or "").strip()
    if not from_role or not to_role:
        return None
    if from_role == to_role or to_role == "director":
        return None
    return from_role, to_role


def _marvis_relay_empty_conversation_html() -> str:
    return """
      <article class="marvis-relay-agent-step marvis-relay-waiting" data-native-role="director" data-native-kind="status" data-native-empty>
        <span class="marvis-relay-avatar marvis-relay-avatar-marvis" aria-label="Marvis"></span>
        <div>
          <div class="marvis-relay-agent-head"><strong>Marvis</strong></div>
          <div class="marvis-relay-agent-bubble">等待总工程师接收任务。</div>
        </div>
      </article>
    """


def _marvis_relay_waiting_message_html() -> str:
    return """
      <article class="marvis-relay-agent-step marvis-relay-waiting" data-native-role="director" data-native-kind="waiting">
        <span class="marvis-relay-avatar marvis-relay-avatar-marvis" aria-label="Marvis"></span>
        <div>
          <div class="marvis-relay-agent-head"><strong>Marvis</strong></div>
          <div class="marvis-relay-agent-bubble">...</div>
        </div>
      </article>
    """


def _marvis_relay_message_html(row: dict[str, str]) -> str:
    role = str(row.get("role") or "system")
    kind = str(row.get("kind") or "event")
    body = str(row.get("body") or "")
    key = str(row.get("key") or "")
    if kind == "user_message":
        return f"""
      <article class="marvis-relay-user-message" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(key)}">
        <div class="marvis-relay-user-bubble" data-native-message-body>{escape(body)}</div>
      </article>
        """
    persona, display_name = _marvis_relay_public_role(role)
    meta = str(row.get("meta") or row.get("status") or "")
    status_label = _marvis_relay_role_status_label(meta)
    action = _marvis_relay_action_label(role, row)
    role_final_attr = (
        f' data-conversation-role-final="{escape(role)}"'
        if kind == "role_envelope"
        else ""
    )
    role_preview_attr = (
        f' data-conversation-role-preview="{escape(role)}"'
        if kind == "text_delta"
        else ""
    )
    raw_preview_attr = ""
    preview_event_ids = str(row.get("preview_event_ids") or "")
    preview_event_ids_attr = (
        f' data-preview-event-ids="{escape(preview_event_ids)}"'
        if kind == "text_delta" and preview_event_ids
        else ""
    )
    action_html = (
        f'<span class="marvis-relay-agent-action">| {escape(action)} {escape(status_label)}</span>'
        if kind in {"role_envelope", "text_delta"} or role == "director"
        else ""
    )
    return f"""
      <article class="marvis-relay-agent-step" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(key)}"{role_final_attr}{role_preview_attr}{raw_preview_attr}{preview_event_ids_attr}>
        {_marvis_relay_avatar_html(persona, label=display_name)}
        <div class="marvis-relay-agent-content">
          <div class="marvis-relay-agent-head"><strong>{escape(display_name)}</strong> {action_html}</div>
          <div class="marvis-relay-agent-bubble" data-native-message-body>{escape(body)}</div>
        </div>
      </article>
    """


def _relay_native_conversation_html(
    role_jobs: list[Any],
    *,
    hub: WorkerLiveStreamHub | None,
    canonical_payloads: dict[str, dict[str, Any]] | None = None,
) -> str:
    canonical_payloads = canonical_payloads or {}
    events: list[tuple[str, int, str, str, WorkerStreamEvent]] = []
    job_by_role = {str(getattr(job, "role", "") or ""): job for job in role_jobs}
    if hub is not None:
        for job in role_jobs:
            agent_run_id = getattr(job, "agent_run_id", None)
            if agent_run_id is None:
                continue
            role = str(getattr(job, "role", "") or "")
            display_name = str(
                getattr(job, "display_name", "") or _relay_role_label(role)
            )
            for worker_event in hub.snapshot(
                agent_run_id=int(agent_run_id),
                after_id=0,
                limit=500,
            ):
                events.append(
                    (
                        str(worker_event.occurred_at or ""),
                        int(worker_event.id),
                        role,
                        display_name,
                        worker_event,
                    )
                )
    if not events and not canonical_payloads:
        return _relay_native_empty_conversation_html()

    events.sort(key=lambda item: (item[0], item[1]))
    completed_keys = {
        _relay_native_message_key(role, worker_event, bucket="assistant")
        for _occurred_at, _event_id, role, _display_name, worker_event in events
        if worker_event.kind == "message_completed"
        and _relay_native_event_text(worker_event).strip()
    }
    rows: list[dict[str, str]] = []
    row_by_key: dict[str, dict[str, str]] = {}
    for _occurred_at, _event_id, role, display_name, worker_event in events:
        kind = str(worker_event.kind or "event")
        text = _relay_native_event_text(worker_event)
        if kind in {"text_delta", "message_completed"} and role in canonical_payloads:
            continue
        if kind == "text_delta":
            key = _relay_native_message_key(
                role,
                worker_event,
                bucket="assistant",
            )
            if key in completed_keys:
                continue
            if key not in row_by_key:
                row = {
                    "role": role,
                    "kind": kind,
                    "speaker": display_name,
                    "meta": str(worker_event.source or ""),
                    "body": "",
                    "key": key,
                    "preview_event_ids": str(worker_event.id),
                }
                rows.append(row)
                row_by_key[key] = row
            event_ids = set(filter(None, row_by_key[key].get("preview_event_ids", "").split(",")))
            event_ids.add(str(worker_event.id))
            row_by_key[key]["preview_event_ids"] = ",".join(sorted(event_ids, key=int))
            row_by_key[key]["body"] += text
            continue
        row = _relay_native_event_row(role, display_name, worker_event)
        if row is not None:
            rows.append(row)
    projected_rows: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    seen_user_bodies: set[str] = set()
    for row in rows:
        projected = _relay_project_native_conversation_row(
            row,
            job=job_by_role.get(str(row.get("role") or "")),
        )
        if projected is None:
            continue
        if _relay_conversation_row_is_task_status_noise(projected):
            continue
        if str(projected.get("kind") or "") == "user_message":
            body = str(projected.get("body") or "").strip()
            if not body or body in seen_user_bodies:
                continue
            seen_user_bodies.add(body)
        key = str(projected.get("key") or "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        projected_rows.append(projected)
    for role in RELAY_ROLE_IDS:
        payload = canonical_payloads.get(role)
        if payload is None:
            continue
        key = f"canonical:{role}:{payload.get('artifact_type') or 'role_envelope'}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        projected_rows.append(
            {
                "role": role,
                "kind": "role_envelope",
                "speaker": _relay_role_label(role),
                "meta": _relay_role_status_label(
                    str(getattr(job_by_role.get(role), "status", "") or "passed")
                ),
                "body": _relay_humanize_role_envelope(payload),
                "key": key,
                "canonical_json": _relay_canonical_envelope_json(payload),
            }
        )
    blocked_role = _relay_first_blocked_role(role_jobs)
    if blocked_role:
        projected_rows.append(
            {
                "role": blocked_role,
                "kind": "status",
                "speaker": "系统",
                "meta": "",
                "body": f"接力暂停在{_relay_role_label(blocked_role)}，详情见任务状态。",
                "key": f"relay-paused:{blocked_role}",
            }
        )
    rows = projected_rows
    if not rows:
        return _relay_native_empty_conversation_html()
    return "\n".join(_relay_native_message_html(row) for row in rows)


def _relay_native_empty_conversation_html() -> str:
    return """
      <article class="relay-message" data-native-role="system" data-native-kind="status" data-native-empty>
        <div class="relay-message-head"><strong>系统</strong></div>
        <div class="relay-message-body" data-native-message-body>等待原生会话输出。</div>
      </article>
    """


def _relay_native_event_row(
    role: str,
    display_name: str,
    worker_event: WorkerStreamEvent,
) -> dict[str, str] | None:
    kind = str(worker_event.kind or "event")
    text = _relay_native_event_text(worker_event)
    if kind == "user_message":
        return {
            "role": role,
            "kind": kind,
            "speaker": "你",
            "meta": display_name,
            "body": text,
            "key": _relay_native_message_key(role, worker_event, bucket="user"),
        }
    if kind == "message_completed":
        return {
            "role": role,
            "kind": kind,
            "speaker": display_name,
            "meta": str(worker_event.source or ""),
            "body": text,
            "key": _relay_native_message_key(role, worker_event, bucket="assistant"),
        }
    return None


def _relay_project_native_conversation_row(
    row: dict[str, str],
    *,
    job: Any | None,
) -> dict[str, str] | None:
    kind = str(row.get("kind") or "")
    if kind == "user_message":
        body = _relay_humanize_user_message(str(row.get("body") or ""))
        if not body:
            return None
        return {**row, "body": body}
    if kind not in {"text_delta", "message_completed"}:
        return None
    body = str(row.get("body") or "").strip()
    if not body:
        return None
    humanized = _relay_humanized_role_output_row(row, body, job=job)
    if humanized is not None and str(humanized.get("kind") or "") == "role_envelope":
        return humanized
    if kind == "text_delta" and _relay_role_job_is_live_preview(job):
        role = str(row.get("role") or "")
        return {
            **row,
            "kind": "text_delta",
            "meta": "实时预览",
            "body": _relay_preview_display_text(role, body),
            "raw_preview": body,
        }
    return None


def _relay_role_job_is_live_preview(job: Any | None) -> bool:
    if job is None:
        return False
    status = str(getattr(job, "status", "") or "")
    if status in {"blocked", "failed", "interrupted", "passed", "completed"}:
        return False
    return status == "streaming" or bool(getattr(job, "turn_running", False))


def _relay_preview_display_text(role: str, text: str) -> str:
    return f"{_relay_role_label(role)}正在处理任务，完成后展示结果。"


def _relay_conversation_row_is_task_status_noise(row: dict[str, str]) -> bool:
    kind = str(row.get("kind") or "")
    body = str(row.get("body") or "")
    if kind == "role_error":
        return True
    if kind == "user_message" and _relay_user_message_is_retry_or_context(body):
        return True
    status_markers = (
        "<tool_use_error>",
        "Directory does not exist",
        "Found 1 file",
        "No files found",
        "Task #",
        "The file /",
        "Tool permission request failed",
        "Updated task #",
        "输出格式异常",
        "任务已阻塞",
        "invalid json",
        "请补充确认后重新调度",
        "原始结构化输出不在主会话展示",
    )
    if any(marker in body for marker in status_markers):
        return True
    return False


def _relay_user_message_is_retry_or_context(text: str) -> bool:
    return (
        "系统已要求当前角色重新输出合法结构化结果。" in text
        or "expected_output_envelope:" in text
        or "你刚才作为" in text
    )


def _relay_first_blocked_role(role_jobs: list[Any]) -> str:
    statuses = {"blocked", "failed", "interrupted"}
    jobs_by_role = {str(getattr(job, "role", "") or ""): job for job in role_jobs}
    for role in RELAY_ROLE_IDS:
        job = jobs_by_role.get(role)
        if job is None:
            continue
        if str(getattr(job, "status", "") or "") in statuses:
            return role
    return ""


def _relay_humanized_role_output_row(
    row: dict[str, str],
    body: str,
    *,
    job: Any | None,
) -> dict[str, str] | None:
    parsed_payload = _relay_parse_role_envelope_payload(body)
    if parsed_payload is not None:
        return {
            **row,
            "kind": "role_envelope",
            "body": _relay_humanize_role_envelope(parsed_payload),
            "canonical_json": _relay_canonical_envelope_json(parsed_payload),
        }

    if not _relay_text_looks_like_role_envelope(body):
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and _relay_dict_looks_like_role_envelope(parsed):
        return {
            **row,
            "kind": "role_envelope",
            "body": _relay_humanize_role_envelope(parsed),
            "canonical_json": _relay_canonical_envelope_json(parsed),
        }

    error = str(getattr(job, "error_message", "") or "").strip() if job else ""
    status = str(getattr(job, "status", "") or "").strip() if job else ""
    if error or status in {"blocked", "failed"}:
        return {
            **row,
            "kind": "role_error",
            "body": _relay_role_output_error_text(str(row.get("role") or ""), error),
        }
    return {
        **row,
        "kind": "role_error",
        "body": _relay_protocol_output_hidden_text(str(row.get("role") or "")),
    }


def _relay_text_looks_like_role_envelope(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    markers = (
        "artifact_type",
        "relay_role",
        "routing_decision",
        "acceptance_criteria",
        "handoff_to",
        "required_roles",
        "open_questions",
        "next_action",
    )
    return any(marker in stripped for marker in markers)


def _relay_humanize_user_message(text: str) -> str:
    if "你刚才作为" in text and "expected_output_envelope:" in text:
        return ""
    if "latest_user_input:" not in text and "expected_output_envelope:" not in text:
        return text
    return (
        _relay_extract_context_field(text, "latest_user_input")
        or _relay_extract_context_field(text, "goal")
        or text
    )


def _relay_extract_context_field(text: str, field: str) -> str:
    prefix = f"{field}:"
    labels = (
        "task_id:",
        "role:",
        "workspace:",
        "goal:",
        "latest_user_input:",
        "handoff_summaries:",
        "constraints:",
        "expected_output_envelope:",
    )
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.rstrip()
        if not stripped.startswith(prefix):
            continue
        inline = stripped[len(prefix) :].strip()
        if inline:
            return inline
        collected: list[str] = []
        for next_line in lines[index + 1 :]:
            next_stripped = next_line.strip()
            if any(next_stripped.startswith(label) for label in labels):
                break
            if next_stripped:
                collected.append(next_stripped)
        return "\n".join(collected).strip()
    return ""


def _relay_sanitize_protocol_leak_text(role: str, text: str) -> str:
    value = _relay_humanize_display_text(text)
    sentinel = "原始结构化输出不在主会话展示。"
    if sentinel in value:
        return value.split(sentinel, 1)[0] + sentinel
    markers = (
        "artifact_type",
        "expected_output_envelope",
        "routing_decisioncomplexity",
        "required_roles",
        "handoff_to",
    )
    if "{" in value and any(marker in value for marker in markers):
        return _relay_protocol_output_hidden_text(role)
    return value


def _relay_followup_response_display_text(role: str, text: str) -> str:
    value = text.strip()
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and _relay_dict_looks_like_role_envelope(parsed):
        display_text = _relay_humanize_role_envelope(parsed)
        extracted = _relay_extract_pseudo_envelope_human_text(value)
        if display_text == "角色已返回结构化结果。" and extracted:
            return extracted
        return display_text

    humanized = _relay_humanize_display_text(value)
    extracted = _relay_extract_pseudo_envelope_human_text(humanized)
    if extracted:
        return extracted
    return _relay_sanitize_protocol_leak_text(role, value)


def _relay_extract_pseudo_envelope_human_text(text: str) -> str:
    if not text or "{" not in text:
        return ""
    if not any(marker in text for marker in ("artifact_type", "summary", "reason")):
        return ""

    normalized = re.sub(r"\s+", " ", text).strip()
    candidates: list[str] = []
    for field in ("summary", "reason"):
        match = re.search(
            rf"(?:^|[{{,\\s\"]){field}(?:[\"\\s:=：]*)(.+)",
            normalized,
        )
        if match:
            candidates.append(match.group(1))
            continue
        marker_index = normalized.rfind(field)
        if marker_index >= 0:
            candidates.append(normalized[marker_index + len(field) :])

    for candidate in candidates:
        cut_at = len(candidate)
        for marker in (
            "role",
            "status",
            "handoff_to",
            "next_action",
            "open_questions",
            "evidence_refs",
            "artifact_type",
        ):
            marker_index = candidate.find(marker)
            if marker_index > 0:
                cut_at = min(cut_at, marker_index)
        cleaned = candidate[:cut_at]
        cleaned = cleaned.strip(" ,，。\"{}")
        if not cleaned:
            continue
        cleaned = _relay_humanize_display_text(cleaned)
        if _relay_text_needs_chinese_fallback(cleaned):
            continue
        if len(cleaned) >= 2:
            return cleaned
    return ""


def _relay_dict_looks_like_role_envelope(payload: dict[str, Any]) -> bool:
    return any(
        key in payload
        for key in (
            "artifact_type",
            "relay_role",
            "summary",
            "next_action",
            "open_questions",
            "required_roles",
            "acceptance_criteria",
        )
    )


def _relay_humanize_role_envelope(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = str(
        payload.get("summary") or payload.get("output") or payload.get("reason") or ""
    ).strip()
    if summary:
        summary = _relay_humanize_display_text(
            summary,
            english_fallback="该角色已返回结构化结果，详情见结构化数据。",
        )
        lines.append(f"结论：{summary}")
    next_action = str(payload.get("next_action") or "").strip()
    if next_action:
        next_action = _relay_humanize_display_text(
            next_action,
            english_fallback="下一步见结构化数据。",
        )
        lines.append(f"下一步：{next_action}")
    questions = _relay_join_text_list(payload.get("open_questions"))
    if questions:
        questions = _relay_humanize_display_text(
            questions,
            english_fallback="待确认内容见结构化数据。",
        )
        lines.append(f"待确认：{questions}")
    route = str(payload.get("route") or "").strip()
    risk = str(payload.get("risk") or "").strip()
    if route or risk:
        parts: list[str] = []
        if route:
            parts.append(f"路径：{_relay_routing_route_label(route)}")
        if risk:
            parts.append(f"风险：{_relay_routing_risk_label(risk)}")
        lines.append(" · ".join(parts))
    acceptance = _relay_join_text_list(payload.get("acceptance_criteria"))
    if acceptance:
        acceptance = _relay_humanize_display_text(
            acceptance,
            english_fallback="验收依据见结构化数据。",
        )
        lines.append(f"验收依据：{acceptance}")
    if not lines:
        lines.append("角色已返回结构化结果。")
    return "\n".join(lines)


def _relay_join_text_list(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _relay_role_output_error_text(role: str, error: str) -> str:
    role_label = _relay_role_label(role)
    lines = [f"{role_label}输出格式异常，任务已阻塞。"]
    if error:
        lines.append(f"错误：{_relay_humanize_display_text(error)}")
    lines.append("请补充确认后重新调度，原始结构化输出不在主会话展示。")
    return "\n".join(lines)


def _relay_protocol_output_hidden_text(role: str) -> str:
    role_label = _relay_role_label(role)
    return f"{role_label}的结构化输出已由系统处理，原始协议内容不在主会话展示。"


def _relay_native_message_html(row: dict[str, str]) -> str:
    meta = str(row.get("meta") or "")
    meta_html = (
        f'<span class="relay-message-meta">{escape(meta)}</span>' if meta else ""
    )
    role = str(row.get("role", "") or "system")
    kind = str(row.get("kind", "") or "event")
    role_final_attr = (
        f' data-conversation-role-final="{escape(role)}"'
        if kind == "role_envelope"
        else ""
    )
    role_preview_attr = (
        f' data-conversation-role-preview="{escape(role)}"'
        if kind == "text_delta"
        else ""
    )
    raw_preview = str(row.get("raw_preview") or "")
    raw_preview_attr = (
        f' data-raw-preview="{escape(raw_preview)}"'
        if kind == "text_delta" and raw_preview
        else ""
    )
    preview_event_ids = str(row.get("preview_event_ids") or "")
    preview_event_ids_attr = (
        f' data-preview-event-ids="{escape(preview_event_ids)}"'
        if kind == "text_delta" and preview_event_ids
        else ""
    )
    canonical_json = str(row.get("canonical_json") or "")
    canonical_html = (
        '<details class="role-canonical-json">'
        "<summary>查看结构化数据</summary>"
        f'<pre data-role-canonical-json="{escape(role)}">{escape(canonical_json)}</pre>'
        "</details>"
        if canonical_json
        else ""
    )
    return f"""
      <article class="relay-message" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(row.get("key", "") or "")}"{role_final_attr}{role_preview_attr}{raw_preview_attr}{preview_event_ids_attr}>
        {_marvis_relay_avatar_html(role, label=str(row.get("speaker", "") or "系统"))}
        <div class="relay-message-head">
          <strong>{escape(row.get("speaker", "") or "系统")}</strong>
          {meta_html}
        </div>
        <div class="relay-message-body" data-native-message-body>{escape(row.get("body", "") or "")}</div>
        {canonical_html}
      </article>
    """


def _relay_native_event_text(worker_event: WorkerStreamEvent) -> str:
    payload = dict(worker_event.payload or {})
    return str(
        payload.get("text")
        or payload.get("delta")
        or payload.get("summary")
        or payload.get("content")
        or payload.get("message")
        or payload.get("output")
        or payload.get("chunk")
        or ""
    )


def _relay_role_display_from_job(
    job: Any,
    *,
    canonical_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = str(getattr(job, "status", "") or "idle")
    output = str(getattr(job, "output", "") or "").strip()
    error_message = str(getattr(job, "error_message", "") or "").strip()
    payload = canonical_payload or _relay_parse_role_envelope_payload(output)
    if payload is not None:
        return {
            "summary_text": _relay_humanize_role_envelope(payload),
            "canonical_json": _relay_canonical_envelope_json(payload),
            "is_preview": False,
            "debug_raw": output if output and output != _relay_canonical_envelope_json(payload) else "",
        }
    if error_message and status in {"blocked", "failed"}:
        return {
            "summary_text": _relay_role_output_error_text(
                str(getattr(job, "role", "") or ""),
                error_message,
            ),
            "canonical_json": "",
            "is_preview": False,
            "debug_raw": output,
        }
    if output:
        return {
            "summary_text": _relay_sanitize_protocol_leak_text(
                str(getattr(job, "role", "") or ""),
                output,
            ),
            "canonical_json": "",
            "is_preview": status == "streaming",
            "debug_raw": output,
        }
    if error_message:
        return {
            "summary_text": _relay_role_output_error_text(
                str(getattr(job, "role", "") or ""),
                error_message,
            ),
            "canonical_json": "",
            "is_preview": False,
            "debug_raw": output,
        }
    idle_output = "未调度，等待总工程师分配或上一角色交接。"
    if getattr(job, "idle_reason", ""):
        idle_output = f"未调度，{job.idle_reason}。"
    return {
        "summary_text": idle_output if status == "idle" else "已启动，等待角色输出。",
        "canonical_json": "",
        "is_preview": status not in {"idle", "passed", "completed", "blocked", "failed"},
        "debug_raw": "",
    }


def _relay_role_canonical_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        payload = _relay_canonical_payload_from_artifact(artifact)
        if payload is None:
            continue
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if role:
            payloads[role] = payload
    return payloads


def _relay_role_canonical_payload_sequence(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        payload = _relay_canonical_payload_from_artifact(artifact)
        if payload is None:
            continue
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if not role:
            continue
        payloads.append(
            {
                **payload,
                "_relay_artifact_key": str(
                    artifact.get("id") or artifact.get("created_at") or index
                ),
            }
        )
    return payloads


def _relay_canonical_payload_from_artifact(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    payload = {
        key: value
        for key, value in dict(artifact or {}).items()
        if key not in {"id", "created_at", "output"} and value is not None
    }
    if "role" not in payload and payload.get("relay_role"):
        payload["role"] = payload["relay_role"]
    parsed = _relay_parse_role_envelope_payload(payload)
    return parsed


def _relay_parse_role_envelope_payload(text_or_payload: str | dict[str, Any]) -> dict[str, Any] | None:
    result = parse_role_envelope(text_or_payload)
    if result.ok and result.payload:
        return dict(result.payload)
    return None


def _relay_canonical_envelope_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _relay_native_message_key(
    role: str,
    worker_event: WorkerStreamEvent,
    *,
    bucket: str,
) -> str:
    payload = dict(worker_event.payload or {})
    stable = (
        payload.get("itemId")
        or payload.get("item_id")
        or payload.get("message_id")
        or payload.get("native_message_id")
        or payload.get("native_turn_id")
        or payload.get("turnId")
        or worker_event.id
    )
    return f"{bucket}:{role}:{stable}"


def _council_review_page() -> str:
    return _replace_html_icons("""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>议会审核</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    html { background: var(--bg-canvas); }
    body { background: transparent; }
    header { position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 52px 1fr auto; gap: 12px; align-items: center; min-height: 72px; padding: 10px 18px; background: rgba(5,5,8,.82); backdrop-filter: blur(20px) saturate(1.4); -webkit-backdrop-filter: blur(20px) saturate(1.4); border-bottom: 1px solid var(--border-header); }
    .circle { width: 46px; height: 46px; font-size: 28px; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .config-link { min-height: 42px; padding: 0 14px; border-radius: 21px; border: 1px solid #34363d; color: var(--text-primary); display: inline-grid; place-items: center; text-decoration: none; font-weight: var(--weight-bold); }
    main { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 420px); gap: 18px; width: min(1180px, 100%); margin: 0 auto; padding: 18px; }
    section { min-width: 0; }
    .panel { border: 1px solid var(--border-card); background: var(--bg-surface); border-radius: 8px; padding: 14px; }
    .stack { display: grid; gap: 12px; }
    label { display: grid; gap: 6px; color: var(--text-secondary); font-size: 14px; font-weight: var(--weight-bold); }
    input, textarea, select { border: 1px solid var(--border-input-alt); border-radius: 8px; background: #1b1d24; }
    textarea { min-height: 124px; resize: vertical; line-height: 1.48; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .run { min-height: 48px; border: 0; border-radius: 8px; background: linear-gradient(135deg, #f4f4f5 0%, #e0e7ff 50%, #f4f4f5 100%); background-size: 200% 100%; color: var(--bg-canvas); font-weight: var(--weight-black); font-size: 16px; box-shadow: 0 4px 20px rgba(244, 244, 245, 0.1); transition: background-position 400ms ease, box-shadow 300ms ease; }
    .run:not(:disabled):hover { background-position: 100% 0; box-shadow: 0 4px 28px rgba(244, 244, 245, 0.18); }
    .run:disabled { opacity: .55; cursor: progress; }
    .muted { color: var(--text-muted); font-size: 13px; line-height: 1.45; }
    .seat-list, .results { display: grid; gap: 10px; }
    .seat, .result { position: relative; border: 1px solid var(--border-card); border-radius: 8px; padding: 12px 12px 12px 18px; background: var(--bg-elevated); animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }
    .seat::before { content: ""; position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px; border-radius: 2px; background: var(--seat-accent, var(--color-link)); }
    .seat:nth-child(1) { --seat-accent: #ef4444; }
    .seat:nth-child(2) { --seat-accent: #3b82f6; }
    .seat:nth-child(3) { --seat-accent: #f59e0b; }
    .seat:nth-child(4) { --seat-accent: #a855f7; }
    .seat:nth-child(5) { --seat-accent: #22c55e; }
    .seat-head, .result-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .seat-title, .result-title { font-weight: var(--weight-black); }
    .badge { border-radius: 999px; padding: 4px 8px; background: #22252e; color: var(--text-secondary); font-size: 12px; white-space: nowrap; }
    .summary { margin-top: 8px; color: #d9dde6; line-height: 1.5; white-space: pre-wrap; }
    .session-link { display: inline-grid; place-items: center; min-height: 32px; margin-top: 10px; padding: 0 10px; border: 1px solid #3b3f49; border-radius: 8px; color: var(--text-primary); text-decoration: none; font-size: 13px; font-weight: var(--weight-bold); }
    .error { color: var(--color-error-text); }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; padding-bottom: 96px; }
      header { grid-template-columns: 46px 1fr; }
      .config-link { grid-column: 1 / -1; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body class="aurora-bg noise-overlay">
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
</html>""")


def _council_seats_page() -> str:
    return _replace_html_icons("""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>议会席位配置</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    html { background: var(--bg-canvas); }
    body { background: transparent; }
    header { position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 52px 1fr auto; gap: 12px; align-items: center; min-height: 72px; padding: 10px 18px; background: rgba(5,5,8,.82); backdrop-filter: blur(20px) saturate(1.4); -webkit-backdrop-filter: blur(20px) saturate(1.4); border-bottom: 1px solid var(--border-header); }
    .circle { width: 46px; height: 46px; font-size: 28px; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .review-link, button.save { min-height: 42px; padding: 0 14px; border-radius: 21px; border: 1px solid #34363d; color: var(--text-primary); background: #202126; display: inline-grid; place-items: center; text-decoration: none; font-weight: var(--weight-bold); }
    main { display: grid; gap: 14px; width: min(980px, 100%); margin: 0 auto; padding: 18px; }
    .toolbar { display: flex; justify-content: space-between; gap: 12px; align-items: center; border: 1px solid var(--border-card); background: var(--bg-surface); border-radius: 8px; padding: 14px; }
    .muted { color: var(--text-muted); font-size: 13px; line-height: 1.45; }
    .seat-grid { display: grid; gap: 12px; }
    .seat { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(140px, .7fr) minmax(160px, .8fr) auto; gap: 10px; align-items: center; border: 1px solid var(--border-card); border-radius: 8px; padding: 12px; background: var(--bg-elevated); }
    .seat-title { display: grid; gap: 5px; min-width: 0; }
    .role { font-weight: var(--weight-black); font-size: 17px; }
    .mission { color: var(--text-placeholder); font-size: 13px; line-height: 1.45; }
    label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 12px; font-weight: var(--weight-bold); }
    input, select { border: 1px solid var(--border-input-alt); border-radius: 8px; background: #1b1d24; padding: 10px 11px; }
    .switch { width: 54px; height: 32px; }
    @media (max-width: 760px) {
      header { grid-template-columns: 46px 1fr; }
      .review-link { grid-column: 1 / -1; }
      .toolbar { display: grid; }
      .seat { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body class="aurora-bg noise-overlay">
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
</html>""")


def _native_token_entry_page(return_to: str = "/native/codex") -> str:
    return_to_json = json.dumps(_safe_native_return_path(return_to))
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WLCodex</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    body { display: grid; place-items: center; padding: 26px; min-height: 100vh; position: relative; }
    main { width: min(420px, 100%); display: grid; gap: 20px; padding: 32px 28px; border-radius: 20px; background: rgba(17, 18, 23, 0.65); border: 1px solid rgba(255, 255, 255, 0.08); backdrop-filter: blur(16px); box-shadow: var(--shadow-lg), var(--shadow-glow); z-index: 1; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }
    h1 { margin: 0; font-size: 32px; font-weight: var(--weight-black); background: var(--gradient-accent); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
    p { margin: 0; color: var(--text-placeholder); line-height: 1.5; }
    form { display: grid; gap: 12px; }
    input { width: 100%; height: 54px; border-radius: 14px; border: 1px solid var(--border-popover); background: #14161d; color: var(--text-primary); padding: 0 14px; font-size: 16px; }
    button { height: 52px; border: 0; border-radius: 14px; background: linear-gradient(135deg, #f4f4f5 0%, #e0e7ff 50%, #f4f4f5 100%); background-size: 200% 100%; color: var(--bg-canvas); font-size: 16px; font-weight: var(--weight-black); box-shadow: 0 4px 20px rgba(244, 244, 245, 0.1); transition: background-position 400ms ease, box-shadow 300ms ease, transform 150ms ease; }
    button:not(:disabled):hover { background-position: 100% 0; box-shadow: 0 4px 28px rgba(244, 244, 245, 0.18); transform: translateY(-1px); }
    button:active { transform: translateY(0); }
    .status { min-height: 20px; color: var(--color-error-light); font-size: 14px; }
  </style>
</head>
<body class="aurora-bg noise-overlay">
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
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    body {{ display: grid; place-items: center; padding: 26px; min-height: 100vh; position: relative; }}
    main {{ width: min(420px, 100%); display: grid; gap: 20px; padding: 32px 28px; border-radius: 20px; background: rgba(17, 18, 23, 0.65); border: 1px solid rgba(255, 255, 255, 0.08); backdrop-filter: blur(16px); box-shadow: var(--shadow-lg), var(--shadow-glow); z-index: 1; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }}
    h1 {{ margin: 0; font-size: 32px; font-weight: var(--weight-black); background: var(--gradient-accent); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }}
    p {{ margin: 0; color: var(--text-placeholder); line-height: 1.5; }}
    form {{ display: grid; gap: 12px; }}
    button {{ height: 52px; border: 0; border-radius: 14px; background: linear-gradient(135deg, #f4f4f5 0%, #e0e7ff 50%, #f4f4f5 100%); background-size: 200% 100%; color: var(--bg-canvas); font-size: 16px; font-weight: var(--weight-black); box-shadow: 0 4px 20px rgba(244, 244, 245, 0.1); transition: background-position 400ms ease, box-shadow 300ms ease, transform 150ms ease; }}
    button:not(:disabled):hover {{ background-position: 100% 0; box-shadow: 0 4px 28px rgba(244, 244, 245, 0.18); transform: translateY(-1px); }}
    button:active {{ transform: translateY(0); }}
  </style>
</head>
<body class="aurora-bg noise-overlay">
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


def _native_codex_page(provider_name: str = "codex", *, theme: str = "") -> str:
    provider_name = provider_name.strip() or "codex"
    provider_label = _native_provider_display_name(provider_name)
    api_base = f"/api/native/{quote(provider_name, safe='')}"
    supports_plan_mode = provider_name in {"codex", "claude"}
    supports_plugin_menu = provider_name == "codex"
    uses_claude_plan_permission_mode = provider_name == "claude"
    plan_mode_action_hidden = "" if supports_plan_mode else " hidden"
    plugin_menu_hidden = "" if supports_plugin_menu else " hidden"
    marvis_css_link = ""
    marvis_body_attr = ""
    marvis_title = escape(provider_label)
    template = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__MARVIS_TITLE__</title>
__NATIVE_APP_HEAD__
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <link rel="stylesheet" href="/static/components.css">
__MARVIS_CSS_LINK__  <style>
    :root { --native-remote-blue: #58a6ff; --native-remote-red: #ff3b4f; }
    body { background: #000; }
    body { scrollbar-width: none; }
    body::-webkit-scrollbar { display: none; }
    .aurora-bg { background: #000 !important; }
    .noise-overlay::before { display: none !important; }
    header { position: sticky; top: 0; z-index: 2; padding: 18px 20px 8px; background: #000; border-bottom: 0; }
    .topbar { position: relative; display: grid; grid-template-columns: 54px 1fr 54px; align-items: center; min-height: 56px; }
    h1 { margin: 0; text-align: center; font-size: 22px; font-weight: var(--weight-black); letter-spacing: 0; }
    .circle { width: 54px; min-height: 54px; border-radius: 50%; border-color: #343434; background: #202022; color: #f5f5f5; font-size: 34px; }
    .menu { font-size: 25px; }
    .topbar-spacer { display: block; width: 54px; min-height: 54px; }
    .theme-toggle { font-size: 22px; font-weight: var(--weight-black); line-height: 1; }
    .theme-toggle[aria-pressed="true"] { background: #fff4df; border-color: #e2c48e; color: #4b3820; }
    .theme-toggle-icon { display: inline-flex; align-items: center; justify-content: center; width: 1em; height: 1em; }
    .devices { display: flex; gap: 10px; overflow-x: auto; padding: 14px 6px 4px; scrollbar-width: none; }
    .title-stack { min-width: 0; display: grid; gap: 3px; text-align: center; }
    .page-subtitle { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #cfcfd5; font-size: 13px; font-weight: var(--weight-bold); }
    .page-subtitle[hidden] { display: none; }
    .device-chip { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 8px; max-width: 82vw; min-height: 44px; border-radius: 22px; padding: 0 17px; border: 0; background: #fff; color: #111; font-size: 15px; font-weight: var(--weight-bold); }
    .device-chip.off { background: var(--bg-chip-off); color: #d8d8dc; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--color-success); }
    .off .dot { background: #8c8f98; }
    .laptop { width: 20px; height: 14px; border: 2px solid currentColor; border-radius: 2px; position: relative; display: inline-block; }
    .laptop:after { content: ""; position: absolute; left: -4px; right: -4px; bottom: -6px; height: 2px; background: currentColor; border-radius: 2px; }
    main { overflow-x: hidden; padding: 6px 26px calc(124px + env(safe-area-inset-bottom)); }
    body[data-native-view="compose"] main { min-height: calc(100vh - 190px); display: grid; align-content: center; padding-bottom: calc(170px + env(safe-area-inset-bottom)); }
    body[data-native-view="history"] #chat,
    body[data-native-view="history"] #projects,
    body[data-native-view="history"] #projectNewChat,
    body[data-native-view="compose"] #chat,
    body[data-native-view="compose"] #projects,
    body[data-native-view="compose"] #projectNewChat,
    body[data-native-view="compose"] .section-title,
    body[data-native-view="compose"] #sessions { display: none; }
    .nav-row, .project, .recent { position: relative; display: grid; grid-template-columns: 40px minmax(0, 1fr) auto; align-items: center; min-width: 0; min-height: 56px; overflow: hidden; color: var(--text-primary); background: transparent; border: 0; border-radius: 12px; width: 100%; padding: 0; text-align: left; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }
    .nav-row[hidden], .project-new-chat[hidden] { display: none; }
    .nav-row > span:nth-child(2), .project > span:nth-child(2) { min-width: 0; }
    .icon-folder { width: 26px; height: 20px; border: 2.4px solid var(--text-primary); border-radius: 4px; position: relative; }
    .icon-folder:before { content: ""; position: absolute; left: 2px; top: -7px; width: 12px; height: 7px; border: 2.4px solid var(--text-primary); border-bottom: 0; border-radius: 4px 4px 0 0; background: #000; }
    .icon-chat { width: 27px; height: 27px; border: 2.4px solid var(--text-primary); border-radius: 50%; position: relative; color: var(--text-primary); }
    .chat-chevron { position: absolute; left: 7px; top: 9px; width: 8px; height: 8px; }
    .chat-chevron:before, .chat-chevron:after { content: ""; position: absolute; right: 0; width: 8px; height: 2.4px; border-radius: 999px; background: currentColor; transform-origin: right center; }
    .chat-chevron:before { top: 1px; transform: rotate(38deg); }
    .chat-chevron:after { bottom: 1px; transform: rotate(-38deg); }
    .chat-prompt-dot { position: absolute; left: 16px; bottom: 8px; width: 4.5px; height: 4.5px; border-radius: 50%; background: currentColor; }
    .nav-row.active .label, .project.active .label { color: #fff; }
    .project .label { color: #f5f5f5; }
    .project .icon-folder { border-color: #f5f5f5; }
    .project .icon-folder:before { border-color: #f5f5f5; }
    .nav-row.active .icon-chat, .project.active .icon-folder { border-color: #fff; }
    .project.active .icon-folder:before { border-color: #fff; }
    button.nav-row::before,
    button.project::before {
      content: "";
      position: absolute;
      top: 10px;
      bottom: 10px;
      left: 0;
      width: 3px;
      border-radius: 999px;
      background: transparent;
    }
    button.nav-row:not(.secondary):not(.warn):not(:disabled):hover,
    button.project:not(.secondary):not(.warn):not(:disabled):hover,
    button.recent:not(.secondary):not(.warn):not(:disabled):hover {
      background: rgba(255, 255, 255, 0.04);
      filter: none;
    }
    button.nav-row:disabled,
    button.project:disabled {
      opacity: 1;
    }
    button.nav-row.active,
    button.project.active {
      background: transparent;
    }
    button.nav-row.active::before,
    button.project.active::before {
      background: transparent;
    }
    button.nav-row.active:not(.secondary):not(.warn):not(:disabled):hover,
    button.project.active:not(.secondary):not(.warn):not(:disabled):hover {
      background: rgba(147, 197, 253, 0.14);
      filter: none;
    }
    button.nav-row:not(.secondary):not(.warn):not(:disabled):active,
    button.project:not(.secondary):not(.warn):not(:disabled):active,
    button.recent:not(.secondary):not(.warn):not(:disabled):active {
      background: rgba(255, 255, 255, 0.07);
      transform: none;
    }
    button.nav-row.active:not(.secondary):not(.warn):not(:disabled):active,
    button.project.active:not(.secondary):not(.warn):not(:disabled):active {
      background: rgba(147, 197, 253, 0.16);
      transform: none;
    }
    .label { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 17px; font-weight: var(--weight-medium); }
    .section-title { margin: 30px 0 12px; color: #f3f3f3; font-size: 16px; font-weight: var(--weight-medium); }
    .recent { grid-template-columns: minmax(0, 1fr) 32px; gap: 10px; align-items: start; min-height: 58px; padding: 6px 0; }
    .recent-copy { min-width: 0; overflow: hidden; }
    .recent-title { display: -webkit-box; max-height: 2.56em; white-space: normal; line-height: 1.28; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
    .recent.active .label { color: #fff; }
    .recent.loading { background: rgba(147, 197, 253, 0.12); }
    .recent-status { position: relative; display: grid; place-items: center; width: 34px; min-height: 28px; color: #b8b8bd; font-size: 14px; line-height: 1; }
    .recent-status.running::before { content: ""; width: 18px; height: 18px; border: 2px solid transparent; border-top-color: var(--native-remote-blue); border-right-color: var(--native-remote-blue); border-radius: 50%; animation: nativeRemoteSpin .85s linear infinite; }
    .recent-status.finished::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--native-remote-red); box-shadow: 0 0 10px rgba(255,59,79,.35); }
    .recent-status.running .status-time, .recent-status.finished .status-time { display: none; }
    .recent.loading .recent-status::before { content: ""; width: 18px; height: 18px; border: 2px solid transparent; border-top-color: var(--native-remote-blue); border-right-color: var(--native-remote-blue); border-radius: 50%; animation: nativeRemoteSpin .85s linear infinite; }
    .recent.loading .recent-status .status-time { display: none; }
    @keyframes nativeRemoteSpin { to { transform: rotate(360deg); } }
    .more-sessions { border: 0; border-top: 1px solid var(--border-subtle); margin-top: 8px; padding-top: 8px; }
    .more-sessions summary { min-height: 44px; list-style: none; cursor: pointer; color: var(--text-dim); font-size: 15px; }
    .more-sessions summary::-webkit-details-marker { display: none; }
    .more-sessions-body { display: grid; gap: 0; }
    .time { max-width: 66px; overflow: hidden; color: var(--text-meta); font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
    .meta { margin-top: 3px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-meta); font-size: 12px; }
    .empty { color: var(--text-meta); padding: 16px 0; }
    .compose-hero { display: none; justify-items: center; gap: 18px; color: var(--text-primary); text-align: center; }
    body[data-native-view="compose"] .compose-hero { display: grid; }
    .compose-hero[hidden] { display: none; }
    .compose-hero h2 { margin: 0; font-size: 24px; line-height: 1.18; font-weight: var(--weight-black); }
    .compose-project-button { display: inline-flex; align-items: center; gap: 8px; max-width: 86vw; min-height: 36px; border: 0; border-radius: 18px; background: transparent; color: #f4f4f5; padding: 0 4px; font-size: 23px; font-weight: var(--weight-black); }
    .compose-project-button .icon-folder { width: 20px; height: 15px; border-width: 2px; flex: 0 0 auto; }
    .compose-project-button .icon-folder:before { top: -6px; width: 10px; height: 6px; border-width: 2px; }
    .compose-project-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .compose-project-chevron { color: #d7d7dc; font-size: 16px; line-height: 1; }
    .compose-mode-toggle { display: grid; grid-template-columns: 1fr 1fr; width: 208px; min-height: 68px; overflow: hidden; border: 1px solid #303036; border-radius: 34px; background: #000; }
    .compose-mode-toggle button { border: 0; border-radius: 0; background: transparent; color: #fff; font-size: 19px; font-weight: var(--weight-black); }
    .compose-mode-toggle button.selected { background: #4a4a4d; color: #fff; }
    .project-picker { position: fixed; inset: 0; z-index: 7; display: grid; align-content: start; overflow-y: auto; padding: calc(180px + env(safe-area-inset-top)) 72px calc(158px + env(safe-area-inset-bottom)); background: rgba(0,0,0,.9); color: var(--text-primary); }
    .project-picker[hidden] { display: none; }
    .project-picker-panel { display: grid; gap: 26px; min-width: 0; }
    .project-picker h2 { margin: 0 0 20px; font-size: 29px; line-height: 1.16; font-weight: var(--weight-medium); letter-spacing: 0; }
    .project-picker-list { display: grid; gap: 18px; }
    .project-picker-section { margin: 10px 0 -4px; color: #d9d9de; font-size: 15px; font-weight: var(--weight-bold); }
    .project-picker-row { display: grid; grid-template-columns: 48px minmax(0, 1fr) 24px; gap: 15px; align-items: center; width: 100%; min-height: 66px; padding: 0; border: 0; border-radius: 12px; background: transparent; color: var(--text-primary); text-align: left; box-shadow: none; -webkit-tap-highlight-color: transparent; }
    button.project-picker-row:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }
    button.project-picker-row:not(.secondary):not(.warn):not(:disabled):active { background: transparent; filter: none; transform: none; }
    .project-picker-row .icon-folder { width: 27px; height: 21px; border-color: #e8e8ed; }
    .project-picker-row .icon-folder:before { border-color: #e8e8ed; }
    .project-picker-row .icon-chat { width: 29px; height: 29px; border-color: #e8e8ed; color: #e8e8ed; }
    .project-picker-copy { min-width: 0; display: grid; gap: 3px; }
    .project-picker-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #f5f5f5; font-size: 20px; font-weight: var(--weight-medium); }
    .project-picker-path { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #c8c8ce; font-size: 14px; line-height: 1.25; }
    .project-picker-check { color: #fff; font-size: 22px; font-weight: var(--weight-black); }
    .project-picker-cancel { justify-self: end; margin-top: 22px; min-height: 44px; border: 0; border-radius: 22px; background: transparent; color: #fff; padding: 0; font-size: 18px; font-weight: var(--weight-extrabold); }
    .controls { position: fixed; left: 0; right: 0; bottom: 0; display: grid; gap: 9px; padding: 12px 26px 18px; background: linear-gradient(to top, rgba(0,0,0,.98) 55%, rgba(0,0,0,.85) 78%, rgba(0,0,0,0)); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
    body[data-native-view="home"] .composer-tools,
    body[data-native-view="history"] .composer-tools,
    body[data-native-view="home"] .mode-chip-row,
    body[data-native-view="history"] .mode-chip-row,
    body[data-native-view="home"] .attachment-strip,
    body[data-native-view="history"] .attachment-strip,
    body[data-native-view="home"] .composer-action-menu,
    body[data-native-view="history"] .composer-action-menu,
    body[data-native-view="home"] .attach-button,
    body[data-native-view="history"] .attach-button { display: none; }
    .composer-tools { display: flex; gap: 8px; align-items: center; min-width: 0; }
    .composer-settings { position: relative; flex: 1; display: flex; gap: 8px; min-width: 0; }
    .setting-pill { min-height: 38px; max-width: 100%; border-radius: 19px; padding: 0 14px; overflow: hidden; background: var(--bg-pill); color: var(--btn-primary-bg); border: 1px solid transparent; font-size: 14px; font-weight: var(--weight-extrabold); text-overflow: ellipsis; white-space: nowrap; transition: background var(--duration-fast) ease, border-color var(--duration-fast) ease; }
    .setting-pill.modified { border-color: rgba(147, 197, 253, 0.35); background: var(--bg-pill-modified); }
    button.setting-pill:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-pill-hover); filter: none; }
    .mode-chip-row { display: flex; gap: 8px; align-items: center; min-height: 0; }
    .mode-chip { display: inline-flex; align-items: center; gap: 8px; min-height: 38px; max-width: 100%; padding: 0 13px; border: 0; border-radius: 19px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 14px; font-weight: var(--weight-extrabold); }
    .mode-chip[hidden] { display: none; }
    .mode-chip-cancel { display: inline-grid; place-items: center; width: 18px; min-height: 18px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--btn-primary-bg); font-size: 16px; line-height: 1; }
    button.mode-chip-cancel:not(.secondary):not(.warn):not(:disabled):hover { background: rgba(255,255,255,.1); filter: none; }
    .model-popover { position: absolute; left: 0; bottom: 48px; width: min(330px, calc(100vw - 52px)); border: 1px solid var(--border-popover); border-radius: 22px; background: var(--bg-popover); box-shadow: 0 20px 54px rgba(0,0,0,.55); overflow: hidden; z-index: 6; opacity: 1; transform: translateY(0) scale(1); transform-origin: bottom left; transition: opacity 180ms var(--ease-default), transform 180ms var(--ease-default); }
    .model-popover.closed { opacity: 0; transform: translateY(8px) scale(0.96); pointer-events: none; }
    .setting-row { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr) auto; gap: 12px; align-items: center; min-height: 76px; padding: 12px 18px; border-bottom: 1px solid var(--border-section); color: var(--btn-primary-bg); }
    .setting-row[hidden] { display: none; }
    .setting-row:last-child { border-bottom: 0; }
    .setting-label { display: grid; gap: 5px; min-width: 0; font-size: 16px; font-weight: var(--weight-extrabold); }
    .setting-value { color: var(--text-dim); font-size: 14px; font-weight: var(--weight-medium); }
    .setting-chevron { color: var(--btn-primary-bg); font-size: 28px; line-height: 1; }
    .model-selector, .setting-selector { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; pointer-events: none; }
    .setting-options { display: grid; gap: 6px; padding: 0 12px 12px; border-bottom: 1px solid var(--border-section); background: var(--bg-setting-options); }
    .setting-options[hidden] { display: none; }
    .permission-popover .setting-options { padding: 12px; border-bottom: 0; }
    .setting-option { display: flex; justify-content: space-between; gap: 10px; align-items: center; min-height: 38px; border-radius: 13px; padding: 7px 11px; background: transparent; color: var(--text-secondary); font-size: 15px; text-align: left; }
    .setting-option-copy { display: grid; gap: 3px; min-width: 0; }
    .setting-option-title { color: var(--btn-primary-bg); font-size: 15px; font-weight: var(--weight-bold); }
    .setting-option-desc { color: var(--text-dim); font-size: 13px; line-height: 1.35; }
    .setting-option.selected { background: var(--bg-option-selected); color: #fff; }
    button.setting-option:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .setting-option-check { color: var(--btn-primary-bg); font-weight: var(--weight-black); }
    .attach-button { position: relative; width: 56px; height: 56px; padding: 0; border-radius: 28px; border: 1px solid #3d3d42; background: #202022; color: var(--text-primary); font-size: 0; line-height: 1; }
    .attach-button:before, .attach-button:after { content: ""; position: absolute; left: 50%; top: 50%; width: 24px; height: 2.6px; border-radius: 999px; background: currentColor; transform: translate(-50%, -50%); }
    .attach-button:after { width: 2.6px; height: 24px; }
    button.attach-button:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .composer-action-menu { position: absolute; left: 26px; right: 26px; bottom: 92px; max-height: min(58vh, 520px); overflow-y: auto; border: 1px solid var(--border-popover); border-radius: 22px; background: var(--bg-popover); box-shadow: 0 20px 54px rgba(0,0,0,.55); padding: 14px; z-index: 8; opacity: 1; transform: translateY(0) scale(1); transform-origin: bottom left; transition: opacity 180ms var(--ease-default), transform 180ms var(--ease-default); }
    .composer-action-menu.closed { opacity: 0; transform: translateY(8px) scale(0.96); pointer-events: none; }
    .composer-menu-item { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 12px; align-items: center; width: 100%; min-height: 58px; padding: 8px 10px; border: 0; border-radius: 14px; background: transparent; color: var(--btn-primary-bg); text-align: left; }
    button.composer-menu-item:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .composer-menu-icon { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 10px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 20px; font-weight: var(--weight-black); }
    .composer-menu-title { min-width: 0; color: var(--btn-primary-bg); font-size: 17px; font-weight: var(--weight-extrabold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-desc { margin-top: 3px; min-width: 0; color: var(--text-dim); font-size: 13px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-check { color: var(--btn-primary-bg); font-size: 18px; font-weight: var(--weight-black); }
    .composer-menu-section { margin: 12px 2px 8px; padding-top: 12px; border-top: 1px solid var(--border-section); color: var(--text-dim); font-size: 14px; font-weight: var(--weight-medium); }
    .plugin-list { display: grid; gap: 4px; }
    .plugin-dot { width: 30px; height: 30px; border-radius: 9px; background: var(--bg-pill); color: var(--btn-primary-bg); display: grid; place-items: center; font-size: 13px; font-weight: var(--weight-black); overflow: hidden; }
    .plugin-dot img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .selected-plugin-strip { display: flex; gap: 8px; min-height: 38px; align-items: center; overflow-x: auto; }
    .selected-plugin-strip[hidden] { display: none; }
    .selected-plugin-chip { display: inline-flex; align-items: center; gap: 7px; min-height: 38px; max-width: 180px; padding: 0 12px; border: 0; border-radius: 19px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 14px; font-weight: var(--weight-extrabold); }
    .selected-plugin-chip .plugin-dot { width: 20px; height: 20px; border-radius: 6px; font-size: 9px; }
    .selected-plugin-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .plugin-autocomplete { position: absolute; left: 26px; right: 26px; bottom: 92px; display: grid; gap: 6px; padding: 8px; border: 1px solid var(--border-popover); border-radius: 22px; background: var(--bg-popover); box-shadow: 0 20px 54px rgba(0,0,0,.55); z-index: 9; }
    .plugin-autocomplete[hidden] { display: none; }
    .plugin-suggestion { display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 12px; align-items: center; min-height: 64px; padding: 10px 12px; border: 0; border-radius: 16px; background: transparent; color: var(--btn-primary-bg); text-align: left; }
    button.plugin-suggestion:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .plugin-suggestion-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--btn-primary-bg); font-size: 17px; font-weight: var(--weight-extrabold); }
    .plugin-suggestion-desc { margin-top: 3px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-dim); font-size: 13px; line-height: 1.35; }
    .attachment-strip { display: flex; gap: 8px; min-height: 48px; overflow-x: auto; padding-bottom: 1px; }
    .attachment-strip[hidden] { display: none; }
    .attachment-chip { position: relative; flex: 0 0 auto; display: grid; grid-template-columns: 42px minmax(76px, 1fr) 26px; align-items: center; gap: 7px; max-width: 220px; min-height: 46px; border: 1px solid var(--border-default); border-radius: 12px; background: var(--bg-attachment); padding: 4px; color: var(--btn-primary-bg); }
    .attachment-chip img { width: 42px; height: 38px; border-radius: 8px; object-fit: cover; background: var(--bg-canvas); }
    .attachment-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); font-size: 12px; }
    .attachment-remove { width: 26px; min-height: 26px; padding: 0; border-radius: 50%; background: var(--bg-remove-btn); color: var(--btn-primary-bg); font-size: 15px; }
    .workspace-bar { display: flex; align-items: center; gap: 8px; min-height: 36px; }
    .workspace-bar[hidden] { display: none; }
    .workspace-bar-icon { width: 18px; height: 14px; flex: 0 0 auto; border: 2px solid var(--text-dim); border-radius: 3px 3px 0 0; border-bottom: 0; position: relative; }
    .workspace-bar-icon:before { content: ""; position: absolute; top: -5px; left: 50%; width: 8px; height: 5px; border: 2px solid var(--text-dim); border-bottom: 0; border-radius: 3px 3px 0 0; transform: translateX(-50%); }
    .workspace-bar-label { color: var(--text-dim); font-size: 12px; font-weight: var(--weight-extrabold); white-space: nowrap; }
    .workspace-bar-chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; min-height: 32px; padding: 0 12px; border: 1px solid var(--border-subtle); border-radius: 16px; background: var(--bg-pill); color: var(--text-primary); font-size: 14px; font-weight: var(--weight-extrabold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; transition: background var(--duration-fast) ease, border-color var(--duration-fast) ease; }
    button.workspace-bar-chip:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-pill-hover); border-color: var(--border-default); }
    .workspace-bar-chip-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .workspace-bar-chip-chevron { color: var(--text-dim); font-size: 11px; flex: 0 0 auto; }
    .workspace-bar-none { color: var(--text-dim); font-size: 13px; font-style: italic; }
    .start-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 12px; align-items: center; }
    body[data-native-view="home"] .start-row,
    body[data-native-view="history"] .start-row { grid-template-columns: minmax(0, 1fr) auto; }
    body[data-native-view="compose"] .start-row { position: relative; grid-template-columns: auto minmax(0, 1fr); }
    .search-wrap { position: relative; min-width: 0; }
    .search-wrap input { width: 100%; min-width: 0; height: 56px; border-radius: 28px; border: 1px solid #49494f; background: #222224; color: var(--text-primary); padding: 0 20px 0 56px; font-size: 17px; }
    body[data-native-view="compose"] .search-wrap input { padding-left: 24px; padding-right: 54px; }
    .search-wrap input::placeholder { color: #bfc0c6; opacity: 1; }
    .search-icon { position: absolute; left: 20px; top: 50%; width: 22px; height: 22px; color: #d9d9de; transform: translateY(-50%); pointer-events: none; }
    body[data-native-view="compose"] .search-icon { display: none; }
    .search-icon:before { content: ""; position: absolute; left: 1px; top: 1px; width: 14px; height: 14px; border: 2.5px solid currentColor; border-radius: 50%; }
    .search-icon:after { content: ""; position: absolute; right: 2px; bottom: 2px; width: 9px; height: 2.5px; border-radius: 999px; background: currentColor; transform: rotate(45deg); transform-origin: center; }
    .mic-icon { display: none; position: absolute; right: 20px; top: 50%; width: 20px; height: 24px; color: #d9d9de; transform: translateY(-50%); pointer-events: none; }
    body[data-native-view="compose"] .mic-icon { display: block; }
    .mic-icon:before { content: ""; position: absolute; left: 5px; top: 0; width: 10px; height: 16px; border: 2.4px solid currentColor; border-radius: 8px; }
    .mic-icon:after { content: ""; position: absolute; left: 1px; bottom: 0; width: 18px; height: 12px; border: 2.4px solid currentColor; border-top: 0; border-radius: 0 0 10px 10px; }
    button.chat { display: inline-flex; align-items: center; justify-content: center; gap: 9px; height: 56px; min-width: 118px; border-radius: 28px; border: 0; background: #fff; color: #000; font-size: 17px; font-weight: var(--weight-extrabold); transition: background var(--duration-fast) ease, color var(--duration-fast) ease, opacity var(--duration-fast) ease; }
    body[data-native-view="compose"] button.chat { display: none; }
    body[data-native-view="compose"] .controls.has-draft button.chat { display: grid; position: absolute; right: 8px; top: 50%; z-index: 2; place-items: center; width: 44px; min-width: 44px; height: 44px; min-height: 44px; padding: 0; border-radius: 50%; background: #fff; color: #000; transform: translateY(-50%); }
    body[data-native-view="compose"] .controls.has-draft button.chat svg { width: 28px; height: 28px; stroke-width: 2.4; }
    body[data-native-view="compose"] .controls.has-draft button.chat:not(:disabled):active { transform: translateY(-50%) scale(.96); }
    body[data-native-view="compose"] .controls.has-draft .mic-icon { display: none; }
    .compose-icon { position: relative; width: 22px; height: 22px; flex: 0 0 22px; }
    .compose-icon:before { content: ""; position: absolute; left: 3px; top: 8px; width: 15px; height: 8px; border: 2.5px solid currentColor; border-top: 0; border-radius: 0 0 5px 5px; transform: rotate(-45deg); }
    .compose-icon:after { content: ""; position: absolute; right: 2px; top: 2px; width: 10px; height: 2.5px; border-radius: 999px; background: currentColor; transform: rotate(-45deg); transform-origin: center; }
    button.chat:disabled { background: var(--bg-pill); color: var(--text-dim); opacity: 1; cursor: default; }
  </style>
</head>
<body class="aurora-bg noise-overlay" data-native-view="home"__MARVIS_BODY_ATTR__>
  <header>
    <div class="topbar">
      <button class="circle" id="back" aria-label="back">‹</button>
      <div class="title-stack">
        <template><h1>__PROVIDER_LABEL__</h1></template>
        <h1 id="pageTitle">__PROVIDER_LABEL__</h1>
        <div class="page-subtitle" id="pageSubtitle" hidden></div>
      </div>
      <span class="topbar-spacer" aria-hidden="true"></span>
    </div>
    <div class="devices" id="devices">
      <button class="device-chip off"><span class="dot"></span><span class="laptop"></span><span>connecting</span></button>
    </div>
  </header>
  <main>
    <button class="nav-row" id="chat">
      <span class="icon-chat"><span class="chat-chevron"></span><span class="chat-prompt-dot"></span></span>
      <span class="label">聊天</span>
      <span></span>
    </button>
    <div id="projects"></div>
    <button class="nav-row project-new-chat" id="projectNewChat" hidden>
      <span class="icon-chat"><span class="chat-chevron"></span><span class="chat-prompt-dot"></span></span>
      <span><span class="label">聊天</span><span class="meta" id="projectNewChatMeta"></span></span>
      <span></span>
    </button>
    <section class="compose-hero" id="composeHero" hidden>
      <h2>开始处理</h2>
      <button class="compose-project-button" id="composeProjectButton" type="button">
        <span class="icon-folder"></span>
        <span class="compose-project-label" id="composeProjectLabel">选择项目</span>
        <span class="compose-project-chevron">⌄</span>
      </button>
      <div class="compose-mode-toggle" aria-label="工作区模式">
        <button class="selected" type="button"><span>工作区</span></button>
        <button type="button"><span>工作树</span></button>
      </div>
    </section>
    <section class="project-picker" id="projectPicker" hidden aria-label="选择项目">
      <div class="project-picker-panel">
        <h2>选择项目</h2>
        <div class="project-picker-list">
          <button class="project-picker-row" id="projectPickerCurrent" type="button"></button>
          <button class="project-picker-row" id="projectPickerNone" type="button"></button>
          <div class="project-picker-section">最近的项目</div>
          <div class="project-picker-list" id="projectPickerRecent"></div>
        </div>
        <button class="project-picker-cancel" id="projectPickerCancel" type="button">取消</button>
      </div>
    </section>
    <div class="section-title">最近</div>
    <div id="sessions"></div>
  </main>
  <section class="controls">
    <div class="composer-tools">
      <div class="composer-settings">
        <button class="setting-pill" id="modelSettingsButton" type="button">加载模型</button>
        <button class="setting-pill permissions" id="permissionSettingsButton" type="button">自动审核</button>
        <div class="model-popover permission-popover closed" id="permissionPopover">
          <select id="permissionSelector" class="setting-selector" aria-label="选择权限模式" hidden>
            <option value="default">默认权限</option>
          </select>
          <div class="setting-options" id="permissionOptions" hidden></div>
        </div>
        <div class="model-popover closed" id="modelPopover">
          <div class="setting-row" id="modelSettingRow" role="button" tabindex="0">
            <span class="setting-label">模型<span class="setting-value" id="modelSettingValue">加载模型</span></span>
            <span></span>
            <span class="setting-chevron">›</span>
            <select id="modelSelector" class="model-selector" aria-label="选择模型">
              <option value="">加载模型</option>
            </select>
          </div>
          <div class="setting-options" id="modelOptions" hidden></div>
          <div class="setting-row" id="serviceTierSettingRow" role="button" tabindex="0">
            <span class="setting-label">速度<span class="setting-value" id="serviceTierSettingValue">正常</span></span>
            <span></span>
            <span class="setting-chevron">›</span>
            <select id="serviceTierSelector" class="setting-selector" aria-label="选择速度">
              <option value="">速度</option>
            </select>
          </div>
          <div class="setting-options" id="serviceTierOptions" hidden></div>
          <div class="setting-row" id="reasoningSettingRow" role="button" tabindex="0">
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
    </div>
    <div class="attachment-strip" id="attachmentStrip" hidden></div>
    <div class="selected-plugin-strip" id="selectedPluginStrip" hidden></div>
    <div class="mode-chip-row">
      <div class="mode-chip plan-mode-chip" id="planModeChip" hidden>
        <span>☷ 计划</span>
        <button class="mode-chip-cancel" id="planModeChipCancel" type="button" aria-label="取消计划模式">×</button>
      </div>
    </div>
    <div class="composer-action-menu closed" id="composerActionMenu" role="menu" aria-label="输入操作">
      <button class="composer-menu-item" id="menuUploadPhoto" type="button" role="menuitem">
        <span class="composer-menu-icon">▧</span>
        <span>
          <span class="composer-menu-title">上传照片</span>
          <span class="composer-menu-desc">添加图片到下一条消息</span>
        </span>
        <span></span>
      </button>
      <button class="composer-menu-item" id="menuPlanMode" type="button" role="menuitem"__PLAN_MODE_ACTION_HIDDEN__ aria-pressed="false">
        <span class="composer-menu-icon">☷</span>
        <span>
          <span class="composer-menu-title">计划模式</span>
          <span class="composer-menu-desc">下一轮先规划再执行</span>
        </span>
        <span class="composer-menu-check" id="planModeCheck"></span>
      </button>
      <div class="composer-menu-section" id="pluginMenuSection"__PLUGIN_MENU_HIDDEN__>插件</div>
      <div class="plugin-list" id="pluginList"__PLUGIN_MENU_HIDDEN__></div>
    </div>
    <div class="plugin-autocomplete" id="pluginAutocomplete" hidden></div>
    <div class="workspace-bar" id="workspaceBar">
      <span class="workspace-bar-icon" aria-hidden="true"></span>
      <span class="workspace-bar-label">工作区</span>
      <button class="workspace-bar-chip" id="workspaceBarChip" type="button">
        <span class="workspace-bar-chip-name" id="workspaceBarChipName">选择项目</span>
        <span class="workspace-bar-chip-chevron">⌄</span>
      </button>
    </div>
    <div class="start-row">
      <button class="attach-button" id="attachmentButton" type="button" aria-label="上传照片"></button>
      <input id="imageInput" type="file" accept="image/*" multiple hidden>
      <label class="search-wrap" for="prompt">
        <span class="search-icon" aria-hidden="true"></span>
        <span class="mic-icon" aria-hidden="true"></span>
        <input id="prompt" placeholder="搜索聊天">
      </label>
      <button class="chat" id="send"><span class="compose-icon" aria-hidden="true"></span><span>聊天</span></button>
    </div>
  </section>
  <script>
    const PROVIDER = __PROVIDER_JSON__;
__ICONS_JS__
    const PROVIDER_LABEL = __PROVIDER_LABEL_JSON__;
    const API_BASE = __API_BASE_JSON__;
    const SUPPORTS_PLAN_MODE = __SUPPORTS_PLAN_MODE_JSON__;
    const SUPPORTS_PLUGIN_MENU = __SUPPORTS_PLUGIN_MENU_JSON__;
    const USES_CLAUDE_PLAN_PERMISSION_MODE = __USES_CLAUDE_PLAN_PERMISSION_MODE_JSON__;
    const PROJECTS_URL = "/api/council/projects";
    const nativePageParams = new URLSearchParams(location.search);
    const token = nativePageParams.get("token") || "";
    const initialComposeCwd = nativePageParams.get("cwd") || "";
    let initialComposeCwdApplied = false;
    if (token) {
      try { localStorage.setItem("wlcodexToken", token); } catch (_error) {}
      document.cookie = "wlcodex_token=" + encodeURIComponent(token) + "; Path=/; Max-Age=2592000; SameSite=Lax";
    }
    const headers = token ? {"Authorization": "Bearer " + token} : {};
    let selected = null;
    let selectedProjectCwd = "";
    let noProjectSelected = false;
    let viewMode = "home";
    let historyTitle = PROVIDER_LABEL;
    let deviceStatusText = "";
    let sessions = [];
    let sessionsRefreshTimer = null;
    let sessionsEventSource = null;
    let projectRoot = "";
    let projectCatalog = [];
    let renderedHomeDataSignature = "";
    const SESSION_PREVIEW_LIMIT = 10;
    const LIVE_PREFETCH_LIMIT = 4;
    const prefetchedLiveUrls = new Set();
    const devicesEl = document.getElementById("devices");
    const pageTitle = document.getElementById("pageTitle");
    const pageSubtitle = document.getElementById("pageSubtitle");
    const sessionsEl = document.getElementById("sessions");
    const projectsEl = document.getElementById("projects");
    const promptEl = document.getElementById("prompt");
    const sendButton = document.getElementById("send");
    const controlsEl = document.querySelector(".controls");
    const attachmentButton = document.getElementById("attachmentButton");
    const imageInput = document.getElementById("imageInput");
    const attachmentStrip = document.getElementById("attachmentStrip");
    const chatRow = document.getElementById("chat");
    const composeHero = document.getElementById("composeHero");
    const composeProjectButton = document.getElementById("composeProjectButton");
    const composeProjectLabel = document.getElementById("composeProjectLabel");
    const projectPicker = document.getElementById("projectPicker");
    const projectPickerCurrent = document.getElementById("projectPickerCurrent");
    const projectPickerNone = document.getElementById("projectPickerNone");
    const projectPickerRecent = document.getElementById("projectPickerRecent");
    const projectPickerCancel = document.getElementById("projectPickerCancel");
    const projectNewChat = document.getElementById("projectNewChat");
    const projectNewChatMeta = document.getElementById("projectNewChatMeta");
    const modelSettingsButton = document.getElementById("modelSettingsButton");
    const permissionSettingsButton = document.getElementById("permissionSettingsButton");
    const modelPopover = document.getElementById("modelPopover");
    const permissionPopover = document.getElementById("permissionPopover");
    const modelSelector = document.getElementById("modelSelector");
    const permissionSelector = document.getElementById("permissionSelector");
    const reasoningSelector = document.getElementById("reasoningSelector");
    const serviceTierSelector = document.getElementById("serviceTierSelector");
    const modelOptions = document.getElementById("modelOptions");
    const permissionOptions = document.getElementById("permissionOptions");
    const reasoningOptions = document.getElementById("reasoningOptions");
    const serviceTierOptions = document.getElementById("serviceTierOptions");
    const modelSettingValue = document.getElementById("modelSettingValue");
    const reasoningSettingValue = document.getElementById("reasoningSettingValue");
    const serviceTierSettingValue = document.getElementById("serviceTierSettingValue");
    const modelSettingRow = document.getElementById("modelSettingRow");
    const reasoningSettingRow = document.getElementById("reasoningSettingRow");
    const serviceTierSettingRow = document.getElementById("serviceTierSettingRow");
    const MODEL_SETTINGS_STORAGE_KEY = "wlcodexNativeModelSettings";
    const MODEL_SETTINGS_STORAGE_VERSION = 2;
    const PERMISSION_SETTINGS_STORAGE_KEY = "wlcodexNativePermissionSettings";
    const COLLABORATION_MODE_STORAGE_KEY = "wlcodexNativeCollaborationMode";
    const DEFAULT_PERMISSION_MODE = "auto_review";
    const PERMISSION_SETTINGS_STORAGE_VERSION = 2;
    const PERMISSION_PRESETS = __PERMISSION_PRESETS_JSON__;
    const PLUGIN_MENU_ITEMS = __PLUGIN_MENU_ITEMS_JSON__;
    let modelCatalog = [];
    let savedModelSettings = loadSavedModelSettings();
    let savedPermissionSettings = loadSavedPermissionSettings();
    let selectedCollaborationMode = loadSavedCollaborationMode();
    let modelSettingsDirty = false;
    let permissionSettingsDirty = false;
    let imageAttachments = [];
    const MAX_IMAGE_DATA_URL_CHARS = 2500000;
    const IMAGE_RESIZE_MAX_SIDE = 1280;
    const IMAGE_RESIZE_MIN_SIDE = 640;
    let selectedPlugins = [];
    const composerActionMenu = document.getElementById("composerActionMenu");
    const menuUploadPhoto = document.getElementById("menuUploadPhoto");
    const menuPlanMode = document.getElementById("menuPlanMode");
    const pluginMenuSection = document.getElementById("pluginMenuSection");
    const pluginList = document.getElementById("pluginList");
    const selectedPluginStrip = document.getElementById("selectedPluginStrip");
    const pluginAutocomplete = document.getElementById("pluginAutocomplete");
    const planModeCheck = document.getElementById("planModeCheck");
    const planModeChip = document.getElementById("planModeChip");
    const planModeChipCancel = document.getElementById("planModeChipCancel");
    const workspaceBar = document.getElementById("workspaceBar");
    const workspaceBarChip = document.getElementById("workspaceBarChip");
    const workspaceBarChipName = document.getElementById("workspaceBarChipName");
    let startingChat = false;

    async function api(path, options = {}) {
      const res = await fetch(path, {
        ...options,
        headers: {"Content-Type": "application/json", ...headers, ...(options.headers || {})}
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }

    function tokenizedPath(path) {
      if (!token) return path;
      const params = new URLSearchParams();
      params.set("token", token);
      return `${path}?${params.toString()}`;
    }

    async function loadStatus() {
      try {
        const status = await api(`${API_BASE}/status`);
        const name = status.server_name || PROVIDER_LABEL;
        deviceStatusText = `${name} · ${status.connected ? "已连接" : "未连接"}`;
        devicesEl.innerHTML = `<button class="device-chip${status.connected ? "" : " off"}"><span class="dot"></span><span class="laptop"></span><span>${escapeHtml(name)}</span></button>`;
        updateNativeChrome();
      } catch (error) {
        deviceStatusText = `${PROVIDER_LABEL} · 未连接`;
        devicesEl.innerHTML = `<button class="device-chip off"><span class="dot"></span><span class="laptop"></span><span>${escapeHtml(error.message)}</span></button>`;
        updateNativeChrome();
      }
    }

    function applySessionsPayload(data, render = true) {
      sessions = data.sessions || [];
      if (data.native_refresh_pending && sessionsRefreshTimer === null) {
        sessionsRefreshTimer = setTimeout(() => {
          sessionsRefreshTimer = null;
          loadSessions(true);
        }, 800);
      }
      if (selected && !sessions.some(session => session.native_thread_id === selected.native_thread_id)) selected = null;
      if (render) {
        renderNativePageIfHomeDataChanged();
      }
    }

    async function loadSessions(render = true) {
      try {
        const data = await api(`${API_BASE}/sessions`);
        applySessionsPayload(data, render);
      } catch (error) {
        sessionsEl.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      }
    }

    function sessionsStreamPath() {
      return tokenizedPath(`${API_BASE}/sessions/stream`);
    }

    function startSessionsStream() {
      if (sessionsEventSource) return;
      try {
        const source = new EventSource(sessionsStreamPath());
        sessionsEventSource = source;
        source.addEventListener("native_sessions", message => {
          const data = JSON.parse(message.data || "{}");
          sessions = data.sessions || [];
          applySessionsPayload(data, true);
        });
        source.onerror = () => {
          if (sessionsEventSource !== source) return;
          source.close();
          sessionsEventSource = null;
          window.setTimeout(startSessionsStream, 3000);
        };
      } catch (_error) {
        sessionsEventSource = null;
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

    function homeDataSignature() {
      return JSON.stringify({
        projectRoot,
        projectCatalog: projectCatalog.map(project => ({
          cwd: String(project.cwd || ""),
          name: String(project.name || ""),
        })),
        sessions: sessions.map(session => ({
          native_thread_id: String(session.native_thread_id || ""),
          agent_run_id: session.agent_run_id || 0,
          title: String(session.title || ""),
          cwd: String(session.cwd || ""),
          status: String(session.status || ""),
          model: String(((session.metadata || {}).model) || ""),
          effort: String(((session.metadata || {}).effort) || ""),
          service_tier: String(((session.metadata || {}).service_tier) || ""),
        })),
      });
    }

    function renderNativePageIfHomeDataChanged() {
      const signature = homeDataSignature();
      if (signature === renderedHomeDataSignature) return false;
      renderedHomeDataSignature = signature;
      renderNativePage({silentSessions: true});
      return true;
    }

    async function loadHomeData() {
      await loadProjects();
      await loadSessions(false);
      renderNativePageIfHomeDataChanged();
      if (initialComposeCwd && !initialComposeCwdApplied) {
        initialComposeCwdApplied = true;
        selectComposeProject(initialComposeCwd);
        openCompose(initialComposeCwd);
      }
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
        updateSettingVisibility();
        updateSettingSummary();
      }
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

    function renderPermissionSettings() {
      permissionSelector.innerHTML = "";
      for (const preset of PERMISSION_PRESETS) {
        const option = document.createElement("option");
        option.value = preset.value;
        option.textContent = preset.label;
        option.dataset.description = preset.description || "";
        permissionSelector.append(option);
      }
      if (optionValueExists(permissionSelector, savedPermissionSettings.permission_mode)) {
        permissionSelector.value = savedPermissionSettings.permission_mode;
      }
      renderSettingOptions(permissionOptions, permissionSelector, updatePermissionSummary);
      updatePermissionSummary();
      savedPermissionSettings = readSelectedPermissionSettings();
      permissionSettingsDirty = false;
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
        preferredReasoningEffortDefault(model, efforts),
        "推理"
      );
      fillServiceTierSelector(tiers, preferredServiceTierDefault(model, tiers));
      if (
        shouldApplyPreferredEffort(preferredSettings, model)
        && optionValueExists(reasoningSelector, preferredSettings.effort)
      ) {
        reasoningSelector.value = preferredSettings.effort;
      }
      if (Object.prototype.hasOwnProperty.call(preferredSettings, "service_tier")
        && optionValueExists(serviceTierSelector, preferredSettings.service_tier)) {
        serviceTierSelector.value = preferredSettings.service_tier || "";
      }
      renderSettingOptions(reasoningOptions, reasoningSelector, updateSettingSummary);
      renderSettingOptions(serviceTierOptions, serviceTierSelector, updateSettingSummary, {includeEmpty: true});
      updateSettingVisibility();
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
        const description = option.dataset.description || "";
        if (description) {
          const copy = document.createElement("span");
          copy.className = "setting-option-copy";
          const title = document.createElement("span");
          title.className = "setting-option-title";
          title.textContent = option.textContent || option.value;
          const desc = document.createElement("span");
          desc.className = "setting-option-desc";
          desc.textContent = description;
          copy.append(title, desc);
          button.append(copy);
        } else {
          button.textContent = option.textContent || option.value;
        }
        button.disabled = select.disabled;
        if (option.selected) {
          const check = document.createElement("span");
          check.className = "setting-option-check";
          check.innerHTML = ICONS.check;
          button.append(check);
        }
        button.onclick = () => {
          const previousValue = select.value;
          select.value = option.value;
          if (select.value !== previousValue) {
            if (select === permissionSelector) markPermissionSettingsDirty();
            else markModelSettingsDirty();
          }
          syncSettingOptionsSelection(container, select);
          container.hidden = true;
          if (onChoose) onChoose();
          if (select === permissionSelector) {
            savePermissionSettingsIfChanged();
            permissionPopover.classList.add("closed");
          }
        };
        container.append(button);
      });
    }

    function syncSettingOptionsSelection(container, select) {
      for (const button of Array.from(container.querySelectorAll(".setting-option"))) {
        const selectedOption = button.dataset.value === select.value;
        button.classList.toggle("selected", selectedOption);
        const existingCheck = button.querySelector(".setting-option-check");
        if (selectedOption && !existingCheck) {
          const check = document.createElement("span");
          check.className = "setting-option-check";
          check.innerHTML = ICONS.check;
          button.append(check);
        } else if (!selectedOption && existingCheck) {
          existingCheck.remove();
        }
      }
    }

    function toggleSettingOptions(container) {
      for (const node of [modelOptions, permissionOptions, serviceTierOptions, reasoningOptions]) {
        if (node !== container) node.hidden = true;
      }
      container.hidden = !container.hidden;
    }

    function updateSettingVisibility() {
      reasoningSettingRow.hidden = reasoningSelector.options.length <= 1;
      serviceTierSettingRow.hidden = serviceTierSelector.options.length <= 1;
      if (reasoningSettingRow.hidden) reasoningOptions.hidden = true;
      if (serviceTierSettingRow.hidden) serviceTierOptions.hidden = true;
    }

    function preferredServiceTierDefault(model, tiers) {
      const defaultValue = String((model && model.defaultServiceTier) || "").toLowerCase();
      if (!defaultValue || ["fast", "priority"].includes(defaultValue)) return "";
      const match = tiers.find(tier => {
        return String(tier.id || tier.serviceTier || tier.name || "").toLowerCase() === defaultValue;
      });
      return match ? match.id || match.serviceTier || match.name || "" : "";
    }
    function preferredReasoningEffortDefault(model, efforts) {
      return highestReasoningEffort(efforts) || String((model && model.defaultReasoningEffort) || "");
    }
    function highestReasoningEffort(efforts) {
      const ranked = (Array.isArray(efforts) ? efforts : [])
        .map(item => String((item && (item.reasoningEffort || item.id)) || item || "").trim())
        .filter(Boolean)
        .sort((left, right) => reasoningEffortRank(right) - reasoningEffortRank(left));
      return ranked[0] || "";
    }
    function reasoningEffortRank(value) {
      const key = String(value || "").trim().toLowerCase();
      if (key === "max" || key === "maximum") return 6;
      if (key === "xhigh" || key === "extra_high") return 5;
      if (key === "high") return 4;
      if (key === "medium" || key === "normal" || key === "default") return 3;
      if (key === "low") return 2;
      if (key === "minimal") return 1;
      if (key === "none") return 0;
      return -1;
    }
    function shouldApplyPreferredEffort(preferredSettings, model) {
      const effort = String((preferredSettings && preferredSettings.effort) || "");
      if (!effort) return false;
      const defaultEffort = preferredReasoningEffortDefault(model, Array.isArray(model && model.supportedReasoningEfforts) ? model.supportedReasoningEfforts : []);
      const catalogDefault = String((model && model.defaultReasoningEffort) || "");
      if ((preferredSettings.version || 0) < MODEL_SETTINGS_STORAGE_VERSION
        && catalogDefault
        && effort === catalogDefault
        && effort !== defaultEffort) {
        return false;
      }
      return true;
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
        effort: reasoningSettingRow.hidden ? "" : reasoningSelector.value,
        service_tier: serviceTierSettingRow.hidden ? "" : serviceTierSelector.value,
        version: MODEL_SETTINGS_STORAGE_VERSION
      });
    }

    function normalizeModelSettings(settings = {}) {
      return {
        model: typeof settings.model === "string" ? settings.model : "",
        effort: typeof settings.effort === "string" ? settings.effort : "",
        service_tier: typeof settings.service_tier === "string" ? settings.service_tier : "",
        version: Number(settings.version || 0)
      };
    }

    function optionValueExists(select, value) {
      const normalized = String(value || "");
      return Array.from(select.options || []).some(option => option.value === normalized);
    }

    function modelSettingsEqual(left, right) {
      return left.model === right.model
        && left.effort === right.effort
        && left.service_tier === right.service_tier
        && left.version === right.version;
    }

    function saveModelSettingsIfChanged() {
      const nextSettings = readSelectedModelSettings();
      const changed = modelSettingsDirty || !modelSettingsEqual(savedModelSettings, nextSettings);
      savedModelSettings = nextSettings;
      if (changed) {
        try {
          localStorage.setItem(MODEL_SETTINGS_STORAGE_KEY, JSON.stringify(savedModelSettings));
        } catch (_error) {}
      }
      modelSettingsDirty = false;
    }

    function markModelSettingsDirty() {
      modelSettingsDirty = true;
    }

    function loadSavedPermissionSettings() {
      try {
        return normalizePermissionSettings(JSON.parse(localStorage.getItem(PERMISSION_SETTINGS_STORAGE_KEY) || "{}"));
      } catch (_error) {
        return normalizePermissionSettings({});
      }
    }

    function readSelectedPermissionSettings() {
      return normalizePermissionSettings({
        permission_mode: permissionSelector.value,
        version: PERMISSION_SETTINGS_STORAGE_VERSION
      });
    }

    function normalizePermissionSettings(settings = {}) {
      const storedVersion = Number(settings.version || 0);
      const mode = typeof settings.permission_mode === "string" ? settings.permission_mode : DEFAULT_PERMISSION_MODE;
      const known = PERMISSION_PRESETS.some(preset => preset.value === mode) ? mode : DEFAULT_PERMISSION_MODE;
      const migrated = storedVersion < PERMISSION_SETTINGS_STORAGE_VERSION && known === "default"
        ? DEFAULT_PERMISSION_MODE
        : known;
      return {
        permission_mode: migrated,
        version: storedVersion
      };
    }

    function permissionSettingsEqual(left, right) {
      return left.permission_mode === right.permission_mode && left.version === right.version;
    }

    function savePermissionSettingsIfChanged() {
      const nextSettings = readSelectedPermissionSettings();
      const changed = permissionSettingsDirty || !permissionSettingsEqual(savedPermissionSettings, nextSettings);
      savedPermissionSettings = nextSettings;
      if (changed) {
        try {
          localStorage.setItem(PERMISSION_SETTINGS_STORAGE_KEY, JSON.stringify(savedPermissionSettings));
        } catch (_error) {}
      }
      permissionSettingsDirty = false;
    }

    function markPermissionSettingsDirty() {
      permissionSettingsDirty = true;
    }

    function loadSavedCollaborationMode() {
      if (!SUPPORTS_PLAN_MODE) return "default";
      try {
        const stored = String(localStorage.getItem(COLLABORATION_MODE_STORAGE_KEY) || "default").toLowerCase();
        return stored === "plan" ? "plan" : "default";
      } catch (_error) {
        return "default";
      }
    }

    function saveSelectedCollaborationMode() {
      try { localStorage.setItem(COLLABORATION_MODE_STORAGE_KEY, selectedCollaborationMode); } catch (_error) {}
    }

    function readSelectedCollaborationMode() {
      if (!SUPPORTS_PLAN_MODE) return null;
      if (USES_CLAUDE_PLAN_PERMISSION_MODE) return null;
      const settings = readSelectedModelSettings();
      return {"mode": selectedCollaborationMode === "plan" ? "plan" : "default", "settings": {"model": settings.model}};
    }

    function setSelectedCollaborationMode(mode) {
      selectedCollaborationMode = SUPPORTS_PLAN_MODE && mode === "plan" ? "plan" : "default";
      saveSelectedCollaborationMode();
      updateCollaborationMenu();
      updateStartControls();
    }

    function updateCollaborationMenu() {
      pluginMenuSection.hidden = !SUPPORTS_PLUGIN_MENU;
      pluginList.hidden = !SUPPORTS_PLUGIN_MENU;
      if (!SUPPORTS_PLAN_MODE) {
        menuPlanMode.hidden = true;
        planModeCheck.innerHTML = "";
        planModeChip.hidden = true;
        return;
      }
      menuPlanMode.hidden = false;
      const enabled = selectedCollaborationMode === "plan";
      menuPlanMode.classList.toggle("selected", enabled);
      menuPlanMode.setAttribute("aria-pressed", enabled ? "true" : "false");
      planModeCheck.innerHTML = enabled ? ICONS.check : "";
      planModeChip.hidden = !enabled;
    }

    function pluginKey(item) {
      return String((item && (item.id || item.name)) || "").trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    }

    function pluginMention(item) {
      const key = pluginKey(item);
      return key ? "@" + key : "";
    }

    function createPluginIcon(item, sizeClass = "") {
      const dot = document.createElement("span");
      dot.className = sizeClass ? "plugin-dot " + sizeClass : "plugin-dot";
      if (item && item.brand_color) dot.style.background = item.brand_color;
      if (item && item.icon) {
        const image = document.createElement("img");
        image.src = item.icon;
        image.alt = "";
        dot.append(image);
      } else {
        dot.textContent = String((item && item.name) || "?").trim().slice(0, 1).toUpperCase() || "?";
      }
      return dot;
    }

    function availablePluginItems() {
      return SUPPORTS_PLUGIN_MENU && Array.isArray(PLUGIN_MENU_ITEMS) ? PLUGIN_MENU_ITEMS : [];
    }

    function renderSelectedPlugins() {
      selectedPluginStrip.innerHTML = "";
      selectedPluginStrip.hidden = !selectedPlugins.length;
      for (const item of selectedPlugins) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "selected-plugin-chip";
        chip.title = item.description || item.name || "Plugin";
        chip.append(createPluginIcon(item));
        const label = document.createElement("span");
        label.className = "selected-plugin-name";
        label.textContent = item.name || "Plugin";
        chip.append(label);
        chip.onclick = () => {
          selectedPlugins = selectedPlugins.filter(plugin => pluginKey(plugin) !== pluginKey(item));
          renderSelectedPlugins();
          updateStartControls();
        };
        selectedPluginStrip.append(chip);
      }
    }

    function currentPluginQuery() {
      if (viewMode !== "compose" || !SUPPORTS_PLUGIN_MENU) return null;
      const cursor = Number.isFinite(promptEl.selectionStart) ? promptEl.selectionStart : promptEl.value.length;
      const before = promptEl.value.slice(0, cursor);
      const match = before.match(/(?:^|\\s)@([a-zA-Z0-9_-]*)$/);
      return match ? match[1].toLowerCase() : null;
    }

    function pluginAutocompleteMatches(query) {
      if (query === null) return [];
      const needle = String(query || "").toLowerCase();
      return availablePluginItems()
        .filter(item => {
          const name = String(item.name || "").toLowerCase();
          const key = pluginKey(item);
          return !needle || name.includes(needle) || key.includes(needle);
        })
        .slice(0, 4);
    }

    function promptHasPluginMention(value, mention) {
      return String(value || "").toLowerCase().split(/\\s+/).includes(String(mention || "").toLowerCase());
    }

    function replacePromptPluginQuery(item) {
      const mention = pluginMention(item);
      if (!mention) return;
      const value = promptEl.value;
      const cursor = Number.isFinite(promptEl.selectionStart) ? promptEl.selectionStart : value.length;
      const before = value.slice(0, cursor);
      const match = before.match(/(^|\\s)@([a-zA-Z0-9_-]*)$/);
      if (match) {
        const prefix = match[1] || "";
        const start = cursor - match[0].length;
        const nextCursor = start + prefix.length + mention.length + 1;
        promptEl.value = value.slice(0, start) + prefix + mention + " " + value.slice(cursor);
        promptEl.setSelectionRange(nextCursor, nextCursor);
        return;
      }
      if (promptHasPluginMention(value, mention)) return;
      const separator = value && !value.endsWith(" ") ? " " : "";
      promptEl.value = value + separator + mention + " ";
      const nextCursor = promptEl.value.length;
      promptEl.setSelectionRange(nextCursor, nextCursor);
    }

    function selectComposerPlugin(item) {
      if (!item || !pluginKey(item)) return;
      if (!selectedPlugins.some(plugin => pluginKey(plugin) === pluginKey(item))) {
        selectedPlugins.push(item);
      }
      replacePromptPluginQuery(item);
      renderSelectedPlugins();
      updatePluginAutocomplete();
      closeComposerActionMenu();
      updateStartControls();
      promptEl.focus({preventScroll: true});
    }

    function updatePluginAutocomplete() {
      const matches = pluginAutocompleteMatches(currentPluginQuery());
      pluginAutocomplete.innerHTML = "";
      pluginAutocomplete.hidden = !matches.length;
      for (const item of matches) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "plugin-suggestion";
        row.append(createPluginIcon(item));
        const copy = document.createElement("span");
        const title = document.createElement("span");
        title.className = "plugin-suggestion-title";
        title.textContent = pluginMention(item);
        const desc = document.createElement("span");
        desc.className = "plugin-suggestion-desc";
        desc.textContent = `${item.name || "Plugin"} · ${item.description || "本机插件"}`;
        copy.append(title, desc);
        row.append(copy);
        row.onclick = () => selectComposerPlugin(item);
        pluginAutocomplete.append(row);
      }
    }

    function resetComposerPlugins() {
      selectedPlugins = [];
      renderSelectedPlugins();
      updatePluginAutocomplete();
    }

    function renderPluginList() {
      if (!SUPPORTS_PLUGIN_MENU) return;
      pluginList.innerHTML = "";
      const items = availablePluginItems();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "composer-menu-item";
        empty.innerHTML = `<span class="plugin-dot">+</span><span><span class="composer-menu-title">未检测到插件</span><span class="composer-menu-desc">安装后会显示在这里</span></span><span></span>`;
        pluginList.append(empty);
        return;
      }
      for (const item of items) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "composer-menu-item";
        row.onclick = () => selectComposerPlugin(item);
        const dot = createPluginIcon(item);
        const copy = document.createElement("span");
        const title = document.createElement("span");
        title.className = "composer-menu-title";
        title.textContent = item.name || "Plugin";
        const desc = document.createElement("span");
        desc.className = "composer-menu-desc";
        desc.textContent = item.description || "本机插件";
        copy.append(title, desc);
        row.append(dot, copy, document.createElement("span"));
        pluginList.append(row);
      }
    }

    function closeComposerActionMenu() {
      composerActionMenu.classList.add("closed");
    }

    function toggleComposerActionMenu() {
      const willClose = !composerActionMenu.classList.contains("closed");
      composerActionMenu.classList.toggle("closed", willClose);
      if (!willClose) {
        modelPopover.classList.add("closed");
        permissionPopover.classList.add("closed");
        modelOptions.hidden = true;
        serviceTierOptions.hidden = true;
        reasoningOptions.hidden = true;
        permissionOptions.hidden = true;
      }
    }

    function updatePermissionSummary() {
      const label = selectedOptionText(permissionSelector, "默认权限");
      permissionSettingsButton.textContent = label;
      permissionSettingsButton.classList.toggle("modified", permissionSelector.value !== "default");
      syncSettingOptionsSelection(permissionOptions, permissionSelector);
    }

    function updateSettingSummary() {
      const modelText = selectedOptionText(modelSelector, "模型");
      const effortText = selectedOptionText(reasoningSelector, "默认");
      const tierText = selectedOptionText(serviceTierSelector, "正常");
      modelSettingValue.textContent = modelText;
      reasoningSettingValue.textContent = effortText;
      serviceTierSettingValue.textContent = tierText;
      const summaryParts = [modelText];
      if (!reasoningSettingRow.hidden) summaryParts.push(effortText);
      if (!serviceTierSettingRow.hidden) summaryParts.push(tierText);
      modelSettingsButton.textContent = summaryParts.join(" ");
      syncSettingOptionsSelection(modelOptions, modelSelector);
      syncSettingOptionsSelection(reasoningOptions, reasoningSelector);
      syncSettingOptionsSelection(serviceTierOptions, serviceTierSelector);
      const defaultModel = modelCatalog.find(model => model.isDefault) || modelCatalog[0] || null;
      const currentModel = selectedModelCatalogEntry();
      const modelChanged = currentModel && defaultModel && (currentModel.model || currentModel.id || "") !== (defaultModel.model || defaultModel.id || "");
      const effortChanged = currentModel && !reasoningSettingRow.hidden && reasoningSelector.value && reasoningSelector.value !== preferredReasoningEffortDefault(currentModel, Array.isArray(currentModel.supportedReasoningEfforts) ? currentModel.supportedReasoningEfforts : []);
      const tierChanged = !serviceTierSettingRow.hidden && serviceTierSelector.value && serviceTierSelector.value !== (String(currentModel ? currentModel.defaultServiceTier || "" : "").toLowerCase());
      modelSettingsButton.classList.toggle("modified", modelChanged || effortChanged || tierChanged);
    }

    function selectedOptionText(select, fallback) {
      const option = select && select.options ? select.options[select.selectedIndex] : null;
      return (option && option.textContent ? option.textContent : fallback) || fallback;
    }

    function renderNativePage(options = {}) {
      updateNativeChrome();
      renderProjects();
      renderSessions({silent: Boolean(options.silentSessions)});
    }

    function updateNativeChrome() {
      document.body.dataset.nativeView = viewMode;
      controlsEl.dataset.view = viewMode;
      const title = viewMode === "home" ? PROVIDER_LABEL : (viewMode === "compose" ? "新聊天" : historyTitle);
      pageTitle.textContent = title || PROVIDER_LABEL;
      const showSubtitle = viewMode !== "home" && Boolean(deviceStatusText);
      pageSubtitle.hidden = !showSubtitle;
      if (showSubtitle) pageSubtitle.textContent = deviceStatusText;
      devicesEl.hidden = viewMode !== "home";
      composeHero.hidden = viewMode !== "compose";
      promptEl.placeholder = viewMode === "compose" ? "接下来我们该写什么代码？" : "搜索聊天";
      renderComposeProject();
      renderWorkspaceBar();
    }

    function renderWorkspaceBar() {
      if (viewMode === "home") {
        workspaceBar.hidden = true;
        return;
      }
      workspaceBar.hidden = false;
      if (selectedProjectCwd) {
        workspaceBarChipName.textContent = lastPath(selectedProjectCwd);
        workspaceBarChip.style.color = "";
      } else if (noProjectSelected) {
        workspaceBarChipName.textContent = "无项目";
        workspaceBarChip.style.color = "var(--text-dim)";
      } else {
        workspaceBarChipName.textContent = "选择项目";
        workspaceBarChip.style.color = "var(--text-dim)";
      }
    }

    function renderComposeProject() {
      const label = selectedProjectCwd ? lastPath(selectedProjectCwd) : (noProjectSelected ? "无项目" : "选择项目");
      composeProjectLabel.textContent = label;
    }

    function openProjectPicker() {
      if (viewMode !== "compose") return;
      renderProjectPicker();
      projectPicker.hidden = false;
    }

    function closeProjectPicker() {
      projectPicker.hidden = true;
    }

    function renderProjectPicker() {
      const current = currentDirectoryProject();
      renderProjectPickerRow(projectPickerCurrent, "当前目录", current.cwd || "", current.cwd || "", "folder");
      renderProjectPickerRow(projectPickerNone, "无项目", "", "", "chat");
      projectPickerRecent.innerHTML = "";
      const seen = new Set();
      for (const project of projectCatalog) {
        const cwd = String(project.cwd || "");
        if (!cwd || seen.has(cwd)) continue;
        seen.add(cwd);
        const row = document.createElement("button");
        row.className = "project-picker-row";
        row.type = "button";
        renderProjectPickerRow(row, project.name || lastPath(cwd), cwd, cwd, "folder");
        projectPickerRecent.appendChild(row);
      }
    }

    function renderProjectPickerRow(row, title, path, cwd, icon) {
      const selectedMark = isProjectPickerRowSelected(cwd) ? "\\u2713" : "";
      const iconMarkup = icon === "chat"
        ? '<span class="icon-chat"><span class="chat-chevron"></span><span class="chat-prompt-dot"></span></span>'
        : '<span class="icon-folder"></span>';
      row.innerHTML = `${iconMarkup}<span class="project-picker-copy"><span class="project-picker-title">${escapeHtml(title)}</span><span class="project-picker-path">${escapeHtml(path)}</span></span><span class="project-picker-check">${selectedMark}</span>`;
      row.onclick = () => selectComposeProject(cwd);
    }

    function isProjectPickerRowSelected(cwd) {
      const value = String(cwd || "");
      if (!value) return noProjectSelected;
      return value === selectedProjectCwd;
    }

    function currentDirectoryProject() {
      const current = projectCatalog.find(project => lastPath(project.cwd || "") === "wlcodex");
      if (current) return {cwd: String(current.cwd || ""), name: current.name || lastPath(current.cwd || "")};
      return {cwd: projectRoot, name: lastPath(projectRoot || "")};
    }

    function selectComposeProject(cwd) {
      selectedProjectCwd = String(cwd || "");
      noProjectSelected = !selectedProjectCwd;
      closeProjectPicker();
      renderComposeProject();
      renderWorkspaceBar();
      updateContextHint();
      updateStartControls();
    }

    function showHome() {
      viewMode = "home";
      historyTitle = PROVIDER_LABEL;
      selectedProjectCwd = "";
      noProjectSelected = false;
      selected = null;
      promptEl.value = "";
      closeProjectPicker();
      closeComposerActionMenu();
      resetComposerPlugins();
      renderNativePage();
    }

    function openHistory(cwd, label) {
      viewMode = "history";
      selectedProjectCwd = String(cwd || "");
      noProjectSelected = false;
      historyTitle = String(label || (selectedProjectCwd ? lastPath(selectedProjectCwd) : "聊天"));
      selected = null;
      promptEl.value = "";
      closeProjectPicker();
      closeComposerActionMenu();
      resetComposerPlugins();
      renderNativePage();
    }

    function openCompose(cwd) {
      viewMode = "compose";
      selectedProjectCwd = String(cwd || selectedProjectCwd || "");
      noProjectSelected = false;
      selected = null;
      promptEl.value = "";
      closeProjectPicker();
      closeComposerActionMenu();
      resetComposerPlugins();
      renderNativePage();
      window.setTimeout(() => promptEl.focus({preventScroll: true}), 0);
    }

    function renderProjects() {
      const seen = new Set();
      projectsEl.innerHTML = "";
      chatRow.className = "nav-row";
      function addProjectOption(cwd, label) {
        cwd = String(cwd || "");
        if (!cwd || seen.has(cwd)) return;
        seen.add(cwd);
        const btn = document.createElement("button");
        btn.className = "project";
        btn.innerHTML = `<span class="icon-folder"></span><span class="label">${escapeHtml(label || lastPath(cwd))}</span><span></span>`;
        btn.onclick = () => openHistory(cwd, label || lastPath(cwd));
        projectsEl.appendChild(btn);
      }
      for (const project of projectCatalog) {
        addProjectOption(project.cwd, project.name);
      }
      for (const session of sessions) {
        if (!isKnownProjectWorkspace(session.cwd)) continue;
        addProjectOption(session.cwd || "", lastPath(session.cwd || ""));
      }
      projectNewChat.hidden = true;
      renderProjectAction();
    }

    function selectProject(cwd) {
      const project = projectCatalog.find(item => String(item.cwd || "") === String(cwd || ""));
      openHistory(cwd, project ? project.name : lastPath(cwd));
    }

    function renderProjectAction() {
      projectNewChat.hidden = true;
      projectNewChatMeta.textContent = selectedProjectCwd ? `在 ${lastPath(selectedProjectCwd)} 新建会话` : "";
    }

    function renderSessions(options = {}) {
      const silent = Boolean(options.silent);
      if (viewMode === "compose") {
        sessionsEl.innerHTML = "";
        return;
      }
      const needle = promptEl.value.trim().toLowerCase();
      const filtered = sortedSessions().filter(session => {
        if (selectedProjectCwd && !(sessionProjectKey(session) === selectedProjectCwd)) return false;
        if (!needle) return true;
        return `${session.title || ""} ${session.cwd || ""}`.toLowerCase().includes(needle);
      });
      if (silent && filtered.length && !sessionsEl.querySelector(":scope > .empty")) {
        scheduleLivePrefetch(filtered.slice(0, LIVE_PREFETCH_LIMIT));
        syncSessionList(filtered.slice(0, SESSION_PREVIEW_LIMIT), sessionsEl);
        const overflow = filtered.slice(SESSION_PREVIEW_LIMIT);
        let details = sessionsEl.querySelector(":scope > details.more-sessions");
        if (!overflow.length) {
          if (details) details.remove();
          return;
        }
        if (!details) {
          details = document.createElement("details");
          details.className = "more-sessions";
          details.append(document.createElement("summary"), document.createElement("div"));
          sessionsEl.append(details);
        }
        const summary = details.querySelector("summary") || document.createElement("summary");
        const body = details.querySelector(".more-sessions-body") || document.createElement("div");
        if (!summary.parentElement) details.prepend(summary);
        if (!body.parentElement) details.append(body);
        body.className = "more-sessions-body";
        summary.textContent = `更多聊天 ${overflow.length}`;
        sessionsEl.append(details);
        syncSessionList(filtered.slice(SESSION_PREVIEW_LIMIT), body);
        return;
      }
      sessionsEl.innerHTML = "";
      if (!filtered.length) {
        sessionsEl.innerHTML = `<div class="empty">没有最近聊天</div>`;
        return;
      }
      scheduleLivePrefetch(filtered.slice(0, LIVE_PREFETCH_LIMIT));
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
        target.appendChild(createSessionButton(session));
      }
    }

    function syncSessionList(source, target) {
      const existing = new Map();
      for (const child of Array.from(target.children)) {
        if (child.matches && child.matches("button.recent[data-session-id]")) {
          existing.set(child.dataset.sessionId || "", child);
        }
      }
      const desired = new Set();
      for (const session of source) {
        const id = sessionDomId(session);
        if (!id) continue;
        desired.add(id);
        const btn = existing.get(id) || createSessionButton(session);
        updateSessionButton(btn, session);
        target.appendChild(btn);
      }
      for (const [id, btn] of existing.entries()) {
        if (!desired.has(id)) btn.remove();
      }
    }

    function createSessionButton(session) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.innerHTML = '<span class="recent-copy"><span class="label recent-title"></span><span class="meta"></span></span><span class="recent-status"><span class="status-time"></span></span>';
      updateSessionButton(btn, session);
      return btn;
    }

    function updateSessionButton(btn, session) {
      const id = sessionDomId(session);
      const isLoading = btn.classList.contains("loading");
      btn.className = "recent" + (selected && selected.native_thread_id === session.native_thread_id ? " active" : "") + (isLoading ? " loading" : "");
      btn.dataset.sessionId = id;
      const titleEl = btn.querySelector(".recent-title");
      const metaEl = btn.querySelector(".meta");
      const statusEl = btn.querySelector(".recent-status");
      const timeEl = btn.querySelector(".status-time");
      if (titleEl) titleEl.textContent = session.title || session.native_thread_id || "";
      if (metaEl) metaEl.textContent = sessionMetaText(session);
      if (statusEl) statusEl.className = `recent-status ${sessionVisualStateClass(session)}`;
      if (timeEl && !isLoading) timeEl.textContent = relativeTime(sessionActivityAt(session));
      btn.onpointerenter = () => prefetchLive(session);
      btn.onpointerdown = () => prefetchLive(session);
      btn.onclick = () => {
        selected = session;
        markSessionViewed(session);
        btn.classList.add("loading");
        const currentTimeEl = btn.querySelector(".status-time");
        if (currentTimeEl) currentTimeEl.textContent = "打开中";
        openLive(session);
      };
    }

    function sessionDomId(session) {
      return String((session && session.native_thread_id) || "");
    }
    function sessionVisualStateClass(session) {
      const status = String((session && session.status) || "").trim().toLowerCase();
      if (status === "running" || status === "in_progress" || status === "queued") return "running";
      if (isUnreadCompletedSession(session)) return "finished";
      return "idle";
    }
    function isUnreadCompletedSession(session) {
      const status = String((session && session.status) || "").trim().toLowerCase();
      const completed = (
        status === "completed" ||
        status === "complete" ||
        status === "done" ||
        status === "succeeded" ||
        status === "success" ||
        status === "failed" ||
        status === "error" ||
        status === "cancelled" ||
        status === "canceled" ||
        status === "interrupted" ||
        status === "aborted" ||
        (status === "idle" && hasReviewableTurn(session))
      );
      return completed && !hasViewedSession(session);
    }
    function hasReviewableTurn(session) {
      return Boolean(session && (session.last_turn_id || session.lastTurnId));
    }
    function hasViewedSession(session) {
      const threadId = String((session && session.native_thread_id) || "");
      if (!threadId) return false;
      try {
        return localStorage.getItem(sessionViewedStorageKey(threadId)) === "1";
      } catch (error) {
        return false;
      }
    }
    function markSessionViewed(session) {
      const threadId = String((session && session.native_thread_id) || "");
      if (!threadId) return;
      try {
        localStorage.setItem(sessionViewedStorageKey(threadId), "1");
      } catch (error) {}
    }
    function sessionViewedStorageKey(threadId) {
      return "wlcodex:native-session-viewed:" + PROVIDER + ":" + threadId;
    }
    function liveUrlForSession(session) {
      if (!session || !session.agent_run_id || !session.native_thread_id) return "";
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      params.set("native_provider", PROVIDER);
      params.set("native_thread_id", session.native_thread_id);
      return `/workers/${session.agent_run_id}/live?${params.toString()}`;
    }
    function prefetchLive(session) {
      const url = liveUrlForSession(session);
      if (!url || prefetchedLiveUrls.has(url)) return;
      prefetchedLiveUrls.add(url);
      const link = document.createElement("link");
      link.rel = "prefetch";
      link.as = "document";
      link.href = url;
      document.head.appendChild(link);
    }
    function scheduleLivePrefetch(source) {
      const sessionsToPrefetch = source.filter(session => session && session.agent_run_id && session.native_thread_id);
      if (!sessionsToPrefetch.length) return;
      const run = () => sessionsToPrefetch.forEach(prefetchLive);
      if ("requestIdleCallback" in window) {
        window.requestIdleCallback(run, {timeout: 1200});
      } else {
        window.setTimeout(run, 250);
      }
    }
    function sessionMetaText(session) {
      const parts = [];
      const workspace = lastPath(session.cwd || "");
      const settings = sessionModelSettingsLabel(session);
      if (workspace) parts.push(workspace);
      if (settings) parts.push(settings);
      if (session.status) parts.push(session.status);
      return parts.join(" · ");
    }
    function sessionModelSettingsLabel(session) {
      const metadata = (session && session.metadata) || {};
      const parts = [];
      if (metadata.model) parts.push(String(metadata.model));
      if (metadata.effort) parts.push(reasoningEffortLabel(metadata.effort));
      if (metadata.service_tier) parts.push(serviceTierLabel(metadata.service_tier));
      return parts.join(" · ");
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
      if (startingChat) return;
      const promptText = String(prompt || "");
      startingChat = true;
      updateStartControls();
      saveModelSettingsIfChanged();
      savePermissionSettingsIfChanged();
      const settings = readSelectedModelSettings();
      const permissionSettings = readSelectedPermissionSettings();
      const attachmentsForSend = imageAttachments.map(image => ({...image}));
      const composerSnapshot = {
        prompt: promptText,
        imageAttachments: attachmentsForSend,
        selectedPlugins: selectedPlugins.map(plugin => ({...plugin}))
      };
      const body = {cwd: selectedProjectCwd, prompt: promptText};
      if (settings.model) body.model = settings.model;
      if (settings.effort) body.effort = settings.effort;
      if (settings.service_tier) body.service_tier = settings.service_tier;
      let permissionMode = permissionSettings.permission_mode;
      if (USES_CLAUDE_PLAN_PERMISSION_MODE && selectedCollaborationMode === "plan") {
        permissionMode = "plan";
      }
      body.permission_mode = permissionMode;
      const collaborationMode = readSelectedCollaborationMode();
      if (collaborationMode) {
        body.collaboration_mode = collaborationMode;
      }
      if (attachmentsForSend.length) {
        body.images = attachmentsForSend.map(image => ({
          url: image.url,
          filename: image.filename,
          mime_type: image.mime_type
        }));
      }
      promptEl.value = "";
      imageAttachments = [];
      renderAttachments();
      resetComposerPlugins();
      try {
        const result = await api(`${API_BASE}/sessions/start`, {
          method: "POST",
          body: JSON.stringify(body)
        });
        openLive(result);
      } catch (error) {
        promptEl.value = composerSnapshot.prompt;
        imageAttachments = composerSnapshot.imageAttachments.map(image => ({...image}));
        selectedPlugins = composerSnapshot.selectedPlugins.map(plugin => ({...plugin}));
        renderAttachments();
        renderSelectedPlugins();
        throw error;
      } finally {
        startingChat = false;
        updateStartControls();
      }
    }

    async function handleProjectNewChat() {
      if (!selectedProjectCwd) return;
      openCompose(selectedProjectCwd);
    }

    function composerHasDraft() {
      return Boolean(promptEl.value.trim() || imageAttachments.length);
    }

    function updateStartControls() {
      const hasDraft = composerHasDraft();
      controlsEl.classList.toggle("has-draft", viewMode === "compose" && hasDraft);
      sendButton.innerHTML = viewMode === "compose" ? ICONS.send : '<span class="compose-icon" aria-hidden="true"></span><span>聊天</span>';
      sendButton.setAttribute("aria-label", viewMode === "compose" ? "发送" : "聊天");
      sendButton.disabled = startingChat || (viewMode === "compose" && !hasDraft);
    }

    async function openLive(session = selected) {
      if (!session) return;
      const url = liveUrlForSession(session);
      if (!url) return;
      location.href = url;
    }
    sendButton.onclick = async () => {
      if (viewMode !== "compose") {
        openCompose(selectedProjectCwd);
        return;
      }
      await startNewChat(promptEl.value.trim());
    };
    window.addEventListener("pageshow", () => {
      renderNativePage();
    });
    chatRow.onclick = () => openHistory("", "聊天");
    composeProjectButton.onclick = openProjectPicker;
    workspaceBarChip.onclick = () => {
      if (viewMode !== "compose") {
        openCompose(selectedProjectCwd);
        return;
      }
      openProjectPicker();
    };
    projectPickerCancel.onclick = closeProjectPicker;
    projectNewChat.onclick = handleProjectNewChat;
    attachmentButton.onclick = toggleComposerActionMenu;
    menuUploadPhoto.onclick = () => {
      closeComposerActionMenu();
      imageInput.click();
    };
    menuPlanMode.onclick = () => {
      if (!SUPPORTS_PLAN_MODE) return;
      setSelectedCollaborationMode(selectedCollaborationMode === "plan" ? "default" : "plan");
      closeComposerActionMenu();
    };
    planModeChipCancel.onclick = () => setSelectedCollaborationMode("default");
    imageInput.onchange = async () => {
      const files = Array.from(imageInput.files || []);
      imageInput.value = "";
      for (const file of files) {
        try {
          imageAttachments.push(await readImageAttachment(file));
        } catch (error) {
          projectNewChatMeta.textContent = error.message || "图片读取失败";
        }
      }
      renderAttachments();
      updateStartControls();
    };
    modelSettingsButton.onclick = () => {
      const willClose = !modelPopover.classList.contains("closed");
      if (willClose) saveModelSettingsIfChanged();
      modelPopover.classList.toggle("closed", willClose);
      if (!willClose) permissionPopover.classList.add("closed");
      if (!willClose) closeComposerActionMenu();
      if (willClose) {
        modelOptions.hidden = true;
        serviceTierOptions.hidden = true;
        reasoningOptions.hidden = true;
      }
    };
    permissionSettingsButton.onclick = () => {
      const willClose = !permissionPopover.classList.contains("closed");
      if (willClose) savePermissionSettingsIfChanged();
      permissionPopover.classList.toggle("closed", willClose);
      if (!willClose) {
        modelPopover.classList.add("closed");
        closeComposerActionMenu();
        permissionOptions.hidden = false;
      } else {
        permissionOptions.hidden = true;
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
    permissionSelector.onchange = () => {
      renderSettingOptions(permissionOptions, permissionSelector, updatePermissionSummary);
      updatePermissionSummary();
      markPermissionSettingsDirty();
    };
    modelSettingRow.onclick = event => {
      if (event.target === modelSelector) return;
      toggleSettingOptions(modelOptions);
    };
    serviceTierSettingRow.onclick = event => {
      if (event.target === serviceTierSelector) return;
      toggleSettingOptions(serviceTierOptions);
    };
    reasoningSettingRow.onclick = event => {
      if (event.target === reasoningSelector) return;
      toggleSettingOptions(reasoningOptions);
    };
    document.getElementById("back").onclick = () => {
      if (!projectPicker.hidden) {
        closeProjectPicker();
        return;
      }
      if (viewMode !== "home") {
        showHome();
        return;
      }
      location.href = tokenizedPath("/native");
    };
    promptEl.addEventListener("input", () => {
      updatePluginAutocomplete();
      if (viewMode === "compose") updateStartControls();
      else renderSessions();
      updateStartControls();
    });
    promptEl.addEventListener("keydown", async event => {
      if (viewMode !== "compose" || event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      await startNewChat(promptEl.value.trim());
    });
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
      const project = selectedProjectCwd ? lastPath(selectedProjectCwd) : (noProjectSelected ? "无项目" : "");
      controlsEl.dataset.project = project;
      promptEl.placeholder = viewMode === "compose" ? "接下来我们该写什么代码？" : "搜索聊天";
    }
    async function readImageAttachment(file) {
      const dataUrl = await readFileAsDataUrl(file);
      try {
        return await resizeImageAttachment(file, dataUrl);
      } catch (error) {
        if (dataUrl.length > MAX_IMAGE_DATA_URL_CHARS) {
          throw new Error(error.message || "图片过大，请换成 JPG 或 PNG 后重试");
        }
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
          try {
            const initialScale = Math.min(1, IMAGE_RESIZE_MAX_SIDE / Math.max(image.width, image.height));
            if (!Number.isFinite(initialScale) || initialScale <= 0) {
              reject(new Error("图片尺寸无效"));
              return;
            }
            if (
              dataUrl.length <= MAX_IMAGE_DATA_URL_CHARS &&
              file.size <= 900000 &&
              image.width <= IMAGE_RESIZE_MAX_SIDE &&
              image.height <= IMAGE_RESIZE_MAX_SIDE
            ) {
              resolve({
                url: dataUrl,
                filename: file.name || "image",
                mime_type: file.type || "image/*"
              });
              return;
            }
            const canvas = document.createElement("canvas");
            let width = Math.max(1, Math.round(image.width * initialScale));
            let height = Math.max(1, Math.round(image.height * initialScale));
            const draw = () => {
              canvas.width = width;
              canvas.height = height;
              const context = canvas.getContext("2d");
              if (!context) throw new Error("图片处理失败");
              context.drawImage(image, 0, 0, width, height);
            };
            draw();
            let quality = .82;
            let url = canvas.toDataURL("image/jpeg", quality);
            while (url.length > MAX_IMAGE_DATA_URL_CHARS && quality > .58) {
              quality = Math.max(.58, quality - .08);
              url = canvas.toDataURL("image/jpeg", quality);
            }
            while (url.length > MAX_IMAGE_DATA_URL_CHARS && Math.max(width, height) > IMAGE_RESIZE_MIN_SIDE) {
              width = Math.max(1, Math.round(width * .82));
              height = Math.max(1, Math.round(height * .82));
              draw();
              quality = .72;
              url = canvas.toDataURL("image/jpeg", quality);
            }
            if (url.length > MAX_IMAGE_DATA_URL_CHARS) {
              reject(new Error("图片压缩后仍过大，请换一张较小的图片"));
              return;
            }
            resolve({
              url,
              filename: file.name || "image",
              mime_type: "image/jpeg"
            });
          } catch (error) {
            reject(error);
          }
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
        remove.innerHTML = ICONS.remove;
        remove.onclick = () => {
          imageAttachments.splice(index, 1);
          renderAttachments();
          updateStartControls();
        };
        chip.append(preview, name, remove);
        attachmentStrip.append(chip);
      });
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
    renderPermissionSettings();
    renderPluginList();
    updateCollaborationMenu();
    updateStartControls();
    loadStatus();
    loadModelCatalog();
    loadHomeData();
    startSessionsStream();
    setInterval(loadHomeData, 15000);
  </script>
__MARVIS_EXTRA_HTML__
</body>
</html>"""
    return _replace_html_icons(
        template.replace("__PROVIDER_LABEL__", escape(provider_label))
        .replace("__MARVIS_TITLE__", marvis_title)
        .replace("__NATIVE_APP_HEAD__", _NATIVE_APP_HEAD)
        .replace("__MARVIS_CSS_LINK__", marvis_css_link)
        .replace("__MARVIS_BODY_ATTR__", marvis_body_attr)
        .replace("__MARVIS_EXTRA_HTML__", "")
        .replace("__PROVIDER_JSON__", json.dumps(provider_name, ensure_ascii=False))
        .replace("__PROVIDER_LABEL_JSON__", json.dumps(provider_label, ensure_ascii=False))
        .replace("__API_BASE_JSON__", json.dumps(api_base, ensure_ascii=False))
        .replace(
            "__SUPPORTS_PLAN_MODE_JSON__",
            json.dumps(supports_plan_mode),
        )
        .replace(
            "__SUPPORTS_PLUGIN_MENU_JSON__",
            json.dumps(supports_plugin_menu),
        )
        .replace(
            "__USES_CLAUDE_PLAN_PERMISSION_MODE_JSON__",
            json.dumps(uses_claude_plan_permission_mode),
        )
        .replace(
            "__PLAN_MODE_ACTION_HIDDEN__",
            plan_mode_action_hidden,
        )
        .replace(
            "__PLUGIN_MENU_HIDDEN__",
            plugin_menu_hidden,
        )
        .replace(
            "__PERMISSION_PRESETS_JSON__",
            json.dumps(_native_permission_presets(provider_name), ensure_ascii=False),
        )
        .replace(
            "__PLUGIN_MENU_ITEMS_JSON__",
            json.dumps(
                _codex_plugin_menu_items()
                if supports_plugin_menu
                else [],
                ensure_ascii=False,
            ),
        )
        .replace("__ICONS_JS__", _ICONS_JS_LITERAL)
    )


def _marvis_extra_html() -> str:
    """Return Marvis-specific HTML: bottom nav, work log panel, persona page, backdrop."""
    return """
  <!-- Marvis Bottom Navigation Bar -->
  <nav class="marvis-bottom-nav" id="marvisBottomNav">
    <button class="marvis-nav-item active" id="marvisNavChat" type="button">
      <span class="marvis-nav-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span>
      <span class="marvis-nav-label">对话</span>
    </button>
    <button class="marvis-nav-item" id="marvisNavTasks" type="button">
      <span class="marvis-nav-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></span>
      <span class="marvis-nav-label">任务</span>
    </button>
    <button class="marvis-nav-item" id="marvisNavSkills" type="button">
      <span class="marvis-nav-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg></span>
      <span class="marvis-nav-label">技能</span>
    </button>
    <button class="marvis-nav-item" id="marvisNavProfile" type="button">
      <span class="marvis-nav-icon"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></span>
      <span class="marvis-nav-label">我的</span>
    </button>
  </nav>

  <!-- Marvis Backdrop Overlay -->
  <div class="marvis-backdrop" id="marvisBackdrop"></div>

  <!-- Marvis Work Log Panel -->
  <section class="marvis-work-log" id="marvisWorkLog" hidden>
    <div class="marvis-work-log-handle"><span></span></div>
    <button class="marvis-work-log-close" id="marvisWorkLogClose" type="button" aria-label="关闭">×</button>
    <div class="marvis-work-log-tabs">
      <button class="marvis-work-log-tab active" type="button">工作日志</button>
      <button class="marvis-work-log-tab" type="button">产出物</button>
    </div>
    <div class="marvis-work-log-body">
      <div class="marvis-work-log-avatar">
        <span class="marvis-work-log-avatar-name">Marvis</span>
      </div>
      <div class="marvis-work-log-screenshots" id="marvisScreenshots">
        <div class="marvis-work-log-screenshot"></div>
        <div class="marvis-work-log-screenshot"></div>
        <div class="marvis-work-log-screenshot"></div>
        <div class="marvis-work-log-screenshot"></div>
      </div>
      <div class="marvis-work-log-status">
        <span class="marvis-work-log-status-text">空闲中...</span>
        <span class="marvis-work-log-status-link">工作状态 ›</span>
      </div>
      <div class="marvis-work-log-status">
        <span class="marvis-work-log-tokens">消耗Token ›</span>
        <span>0 🔥</span>
      </div>
    </div>
  </section>

  <!-- Marvis Persona Page -->
  <section class="marvis-persona-page" id="marvisPersonaPage" hidden>
    <button class="marvis-persona-back" id="marvisPersonaBack" type="button" aria-label="返回">‹</button>
    <div class="marvis-persona-hero">
      <div class="marvis-persona-avatar">
        <svg viewBox="0 0 160 160" xmlns="http://www.w3.org/2000/svg">
          <!-- Marvis character - stylized black mascot with red scarf -->
          <g transform="translate(30,10)">
            <!-- Body -->
            <ellipse cx="50" cy="110" rx="38" ry="30" fill="#1A1A1A"/>
            <!-- Head -->
            <circle cx="50" cy="55" r="32" fill="#1A1A1A"/>
            <!-- Ears/Horns -->
            <path d="M28 28 L22 8 L36 22Z" fill="#1A1A1A"/>
            <path d="M72 28 L78 8 L64 22Z" fill="#1A1A1A"/>
            <!-- Eyes -->
            <rect x="33" y="46" width="14" height="8" rx="2" fill="#FFFFFF"/>
            <rect x="53" y="46" width="14" height="8" rx="2" fill="#FFFFFF"/>
            <rect x="37" y="48" width="6" height="4" rx="1" fill="#E53935"/>
            <rect x="57" y="48" width="6" height="4" rx="1" fill="#E53935"/>
            <!-- Scarf -->
            <path d="M22 72 Q50 85 78 72 Q78 92 62 90 L58 105 L50 95 L42 105 L38 90 Q22 92 22 72Z" fill="#E53935"/>
            <!-- Arms -->
            <ellipse cx="16" cy="100" rx="10" ry="18" fill="#1A1A1A" transform="rotate(-15 16 100)"/>
            <ellipse cx="84" cy="95" rx="10" ry="18" fill="#1A1A1A" transform="rotate(15 84 95)"/>
            <!-- Legs -->
            <ellipse cx="35" cy="135" rx="12" ry="10" fill="#1A1A1A"/>
            <ellipse cx="65" cy="135" rx="12" ry="10" fill="#1A1A1A"/>
          </g>
        </svg>
      </div>
      <h2 class="marvis-persona-greeting">Hi，我是 Marvis</h2>
      <div class="marvis-persona-tags">
        <span class="marvis-persona-tag">理智高效</span>
        <span class="marvis-persona-tag">极简办公</span>
        <span class="marvis-persona-tag">默默干活</span>
      </div>
    </div>
    <div class="marvis-persona-body">
      <div class="marvis-persona-section">
        <div class="marvis-persona-section-title">👋 自我介绍</div>
        <div class="marvis-persona-section-content">
          老板，我是理智高效版 Marvis。我 24 小时在线，有问题随时来找我。
        </div>
      </div>
      <div class="marvis-persona-section">
        <div class="marvis-persona-section-title">📋 人设特征</div>
        <div class="marvis-persona-section-content">
          <dl>
            <dt>性格关键词：理智高效</dt>
            <dd>说话风格：不做多余寒暄，直奔问题核心，用最短路径帮你解决需求</dd>
            <dt>适用场景：</dt>
            <dd>适合办公需求处理：文档整理、信息查询、方案梳理等高效场景</dd>
          </dl>
        </div>
      </div>
      <button class="marvis-persona-edit" type="button">✏️ 人设调整</button>
    </div>
    <button class="marvis-persona-cta" id="marvisPersonaCta" type="button">设定我的Marvis</button>
  </section>

  <!-- Marvis UI JavaScript -->
  <script>
  (function() {
    if (!document.body.dataset.theme || document.body.dataset.theme !== 'marvis') return;

    const bottomNav = document.getElementById('marvisBottomNav');
    const backdrop = document.getElementById('marvisBackdrop');
    const workLog = document.getElementById('marvisWorkLog');
    const workLogClose = document.getElementById('marvisWorkLogClose');
    const personaPage = document.getElementById('marvisPersonaPage');
    const personaBack = document.getElementById('marvisPersonaBack');
    const personaCta = document.getElementById('marvisPersonaCta');
    const navChat = document.getElementById('marvisNavChat');
    const navTasks = document.getElementById('marvisNavTasks');
    const navSkills = document.getElementById('marvisNavSkills');
    const navProfile = document.getElementById('marvisNavProfile');

    if (!bottomNav) return;

    // Tab switching
    const navItems = [navChat, navTasks, navSkills, navProfile];
    function activateTab(item) {
      navItems.forEach(n => { if (n) n.classList.remove('active'); });
      if (item) item.classList.add('active');
    }

    if (navChat) navChat.addEventListener('click', function() {
      activateTab(navChat);
      closeWorkLog();
      closePersonaPage();
    });

    if (navTasks) navTasks.addEventListener('click', function() {
      activateTab(navTasks);
      openWorkLog();
    });

    if (navSkills) navSkills.addEventListener('click', function() {
      activateTab(navSkills);
    });

    if (navProfile) navProfile.addEventListener('click', function() {
      activateTab(navProfile);
      openPersonaPage();
    });

    // Work Log panel
    function openWorkLog() {
      if (!workLog) return;
      workLog.hidden = false;
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          workLog.classList.add('open');
          if (backdrop) { backdrop.classList.add('visible'); }
        });
      });
    }

    function closeWorkLog() {
      if (!workLog) return;
      workLog.classList.remove('open');
      if (backdrop) backdrop.classList.remove('visible');
      setTimeout(function() { workLog.hidden = true; }, 350);
      activateTab(navChat);
    }

    if (workLogClose) workLogClose.addEventListener('click', closeWorkLog);

    // Work Log tabs
    var wlTabs = workLog ? workLog.querySelectorAll('.marvis-work-log-tab') : [];
    wlTabs.forEach(function(tab) {
      tab.addEventListener('click', function() {
        wlTabs.forEach(function(t) { t.classList.remove('active'); });
        tab.classList.add('active');
      });
    });

    // Persona Page
    function openPersonaPage() {
      if (!personaPage) return;
      personaPage.hidden = false;
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          personaPage.classList.add('open');
        });
      });
    }

    function closePersonaPage() {
      if (!personaPage) return;
      personaPage.classList.remove('open');
      setTimeout(function() { personaPage.hidden = true; }, 300);
      activateTab(navChat);
    }

    if (personaBack) personaBack.addEventListener('click', closePersonaPage);
    if (personaCta) personaCta.addEventListener('click', closePersonaPage);

    // Backdrop click closes work log
    if (backdrop) backdrop.addEventListener('click', function() {
      closeWorkLog();
    });
  })();
  </script>
"""


def _live_page(agent_run_id: int, *, native_provider: str = "codex", theme: str = "") -> str:
    stream_path = f"/api/workers/{agent_run_id}/stream"
    native_provider = native_provider.strip() or "codex"
    provider_label = _native_provider_display_name(native_provider)
    api_base = f"/api/native/{quote(native_provider, safe='')}"
    safe_title = escape(provider_label)
    supports_plan_mode = native_provider in {"codex", "claude"}
    supports_plugin_menu = native_provider == "codex"
    uses_claude_plan_permission_mode = native_provider == "claude"
    plan_mode_action_hidden = "" if supports_plan_mode else " hidden"
    plugin_menu_hidden = "" if supports_plugin_menu else " hidden"
    marvis_css_link = ""
    marvis_body_attr = ""
    template = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
  <title>__SAFE_TITLE__</title>
__NATIVE_APP_HEAD__
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <link rel="stylesheet" href="/static/components.css">
__MARVIS_CSS_LINK__  <style>
    :root { --native-remote-blue: #58a6ff; --native-remote-red: #ff3b4f; }
    html, body, .native-mobile-shell, .codex-run-shell, .codex-transcript, .transcript-body, .codex-input-dock, input { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
    body { background: #000; }
    body { scrollbar-width: none; }
    body::-webkit-scrollbar { display: none; }
    .aurora-bg { background: #000 !important; }
    .noise-overlay::before { display: none !important; }
    .native-mobile-shell, .codex-run-shell { min-height: 100vh; background: #000; }
    .viewport-debug { position: fixed; left: 12px; right: 12px; bottom: calc(var(--codex-dock-height, 150px) + 14px + env(safe-area-inset-bottom)); z-index: 40; max-height: 34vh; margin: 0; padding: 10px 12px; overflow: auto; border: 1px solid rgba(88,166,255,.55); border-radius: 12px; background: rgba(0,0,0,.88); color: #dbeafe; font: 11px/1.45 var(--font-mono); white-space: pre-wrap; box-shadow: 0 14px 36px rgba(0,0,0,.5); }
    .viewport-debug[hidden] { display: none; }
    header { position: sticky; top: 0; z-index: 3; display: grid; grid-template-columns: 54px 1fr 54px; align-items: center; gap: 8px; min-height: 72px; padding: 10px 20px 8px; background: #000; border-bottom: 0; }
    .circle { width: 54px; min-height: 54px; border-radius: 50%; border-color: #343434; background: #202022; color: #f5f5f5; font-size: 34px; }
    .session-float { position: fixed; top: calc(24px + env(safe-area-inset-top)); left: 90px; right: 146px; z-index: 5; display: grid; grid-template-columns: minmax(0, 1fr); align-items: center; min-height: 50px; padding: 0 15px; border: 1px solid #343434; border-radius: 25px; background: #242426; color: #f4f4f5; box-shadow: 0 12px 30px rgba(0,0,0,.38); }
    .session-float-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; line-height: 1.15; font-weight: var(--weight-black); }
    .session-float-meta { display: flex; gap: 7px; align-items: center; min-width: 0; margin-top: 4px; color: #d0d0d4; font-size: 11px; line-height: 1; overflow: hidden; white-space: nowrap; }
    .session-float-meta .laptop { width: 13px; height: 9px; border: 1.6px solid currentColor; border-radius: 2px; position: relative; display: inline-block; }
    .session-float-meta .laptop:after { content: ""; position: absolute; left: -3px; right: -3px; bottom: -5px; height: 2px; background: currentColor; border-radius: 2px; }
    .header-run-indicator { position: fixed; top: calc(24px + env(safe-area-inset-top)); right: 20px; z-index: 6; display: grid; grid-template-columns: 34px 34px; gap: 10px; align-items: center; justify-content: center; width: 96px; min-height: 58px; border: 1px solid #343434; border-radius: 30px; background: #242426; color: #f4f4f5; box-shadow: 0 12px 30px rgba(0,0,0,.38); }
    .header-run-button { width: 34px; min-height: 34px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; color: inherit; -webkit-tap-highlight-color: transparent; }
    button.header-run-button:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }
    .header-run-status { display: grid; place-items: center; width: 34px; min-height: 34px; }
    .header-run-spinner { width: 28px; height: 28px; border: 3px solid #5a5b60; border-right-color: transparent; border-radius: 50%; opacity: .72; }
    .header-run-menu { display: grid; place-items: center; width: 24px; height: 34px; line-height: 1; color: #f4f4f5; font-size: 30px; font-weight: var(--weight-extrabold); transform: translateY(-1px); }
    .header-run-menu svg { width: 24px; height: 24px; }
    .header-run-dot { display: none; width: 8px; height: 8px; border-radius: 50%; background: var(--native-remote-red); box-shadow: 0 0 10px rgba(255,59,79,.35); }
    .header-run-indicator.running .header-run-spinner { border-color: transparent; border-top-color: var(--native-remote-blue); border-right-color: var(--native-remote-blue); opacity: 1; animation: nativeRemoteSpin .85s linear infinite; }
    .header-run-indicator.finished .header-run-spinner { display: none; }
    .header-run-indicator.finished .header-run-dot { display: block; }
    .native-header-popover { position: fixed; top: calc(86px + env(safe-area-inset-top)); right: 20px; z-index: 10; width: min(326px, calc(100vw - 40px)); border: 1px solid #343434; border-radius: 24px; background: #242426; color: #f4f4f5; box-shadow: 0 18px 44px rgba(0,0,0,.55); overflow: hidden; }
    .native-header-popover[hidden] { display: none; }
    .context-info-sheet { position: fixed; left: 0; right: 0; bottom: 0; z-index: 30; display: grid; padding: 13px 38px calc(42px + env(safe-area-inset-bottom)); border-radius: 30px 30px 0 0; background: #000; color: #f4f4f5; border-top: 1px solid #111114; box-shadow: 0 -22px 54px rgba(0,0,0,.78); }
    .context-info-sheet[hidden] { display: none; }
    .context-sheet-handle { justify-self: center; width: 58px; height: 5px; margin: 0 0 28px; border-radius: 999px; background: #252529; }
    .context-sheet-header { position: relative; display: grid; place-items: center; min-height: 42px; margin-bottom: 24px; }
    .context-info-title { margin: 0; color: #f4f4f5; font-size: 24px; line-height: 1.15; font-weight: var(--weight-black); text-align: center; letter-spacing: 0; }
    .context-info-close { position: absolute; top: -2px; right: -10px; width: 42px; min-height: 42px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; color: #f4f4f5; font-size: 34px; line-height: 1; font-weight: var(--weight-medium); -webkit-tap-highlight-color: transparent; }
    button.context-info-close:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }
    .context-info-grid { display: grid; gap: 15px; }
    .context-info-row { display: grid; grid-template-columns: 136px minmax(0, 1fr); gap: 36px; align-items: start; min-height: 28px; }
    .context-info-label { color: #d9d9dd; font-size: 18px; line-height: 1.35; font-weight: var(--weight-medium); white-space: nowrap; }
    .context-info-value { min-width: 0; color: #f4f4f5; font-size: 19px; line-height: 1.35; font-weight: var(--weight-extrabold); overflow-wrap: anywhere; white-space: normal; }
    .context-info-value-wrap { min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) 42px; gap: 10px; align-items: start; }
    .context-info-copy { width: 42px; min-height: 42px; display: grid; place-items: center; padding: 0; margin-top: -6px; border: 0; border-radius: 50%; background: transparent; color: #f4f4f5; -webkit-tap-highlight-color: transparent; }
    .context-info-copy svg { width: 32px; height: 32px; }
    button.context-info-copy:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }
    .session-action-menu { padding: 18px 0; }
    .session-action-title { min-width: 0; margin: 0; padding: 0 38px 12px; color: #d7d7dc; font-size: 18px; line-height: 1.2; font-weight: var(--weight-extrabold); text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .session-action-item { width: 100%; min-height: 64px; display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 18px; align-items: center; padding: 0 42px; border: 0; border-radius: 0; background: transparent; color: #f4f4f5; text-align: left; font-size: 20px; line-height: 1.2; font-weight: var(--weight-medium); -webkit-tap-highlight-color: transparent; }
    button.session-action-item:not(.secondary):not(.warn):not(:disabled):hover { background: rgba(255,255,255,.06); filter: none; }
    .session-action-item.danger { color: #ff4b55; }
    .session-action-icon { display: grid; place-items: center; width: 36px; height: 36px; }
    .session-action-icon svg { width: 30px; height: 30px; }
    @keyframes nativeRemoteSpin { to { transform: rotate(360deg); } }
    .screen-title { min-width: 0; text-align: center; visibility: hidden; }
    header > button:last-child { visibility: hidden; pointer-events: none; }
    h1 { margin: 0; font-size: 22px; font-weight: var(--weight-extrabold); letter-spacing: 0; }
    .subtitle { margin-top: 5px; color: var(--text-muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 7px; background: var(--color-warning); vertical-align: 1px; transition: background 300ms ease; }
    .connected .status-dot { background: var(--color-success); animation: breathe 2s ease-in-out infinite; }
    .reconnecting .status-dot { background: var(--color-error); animation: breathe 1s ease-in-out infinite; }
    main { padding: 12px 20px calc(var(--codex-dock-height, 150px) + 32px + env(safe-area-inset-bottom)); }
    .codex-status-flow { position: sticky; top: 78px; z-index: 2; display: grid; grid-template-columns: 12px 1fr auto; gap: 10px; align-items: center; min-height: 42px; margin: 0 -20px 8px; padding: 10px 20px; background: rgba(0,0,0,.94); border-bottom: 1px solid #17181c; color: var(--text-dim); font-size: 14px; }
    .run-pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--color-success); box-shadow: 0 0 16px rgba(34,197,94,.7); transition: background 300ms ease, box-shadow 300ms ease; }
    .run-state.busy .run-pulse { background: var(--color-warning); box-shadow: 0 0 16px rgba(245,158,11,.7); animation: statusPulse 2s ease-in-out infinite; }
    .run-state.failed .run-pulse { background: var(--color-error); box-shadow: 0 0 16px rgba(239,68,68,.7); }
    @keyframes statusPulse { 0%, 100% { box-shadow: 0 0 8px rgba(245,158,11,.3); } 50% { box-shadow: 0 0 20px rgba(245,158,11,.7); } }
    .event-cursor { color: #777b86; font-size: 12px; }
    .codex-transcript { display: grid; gap: 18px; padding-top: 8px; }
    .transcript-item { display: grid; gap: 7px; min-width: 0; padding: 0; }
    .transcript-meta { color: #9aa0aa; font-size: 12px; }
    .transcript-body { min-width: 0; max-width: 100%; white-space: normal; overflow-wrap: anywhere; color: var(--btn-primary-bg); font-size: 15px; line-height: 1.55; letter-spacing: 0; }
    .transcript-body p { margin: 0 0 13px; overflow-wrap: anywhere; word-break: break-word; }
    .transcript-body p:last-child { margin-bottom: 0; }
    .transcript-body h3 { margin: 18px 0 8px; color: var(--text-heading); font-size: 16px; line-height: 1.35; }
    .transcript-body h3:first-child { margin-top: 0; }
    .transcript-body ul, .transcript-body ol { margin: 0 0 13px 1.3em; padding: 0; display: grid; gap: 6px; white-space: normal; }
    .transcript-body li { padding-left: 2px; white-space: normal; }
    .transcript-body strong { color: var(--text-heading); font-weight: var(--weight-extrabold); }
    .transcript-body a { color: var(--color-link); text-decoration: none; border-bottom: 1px solid rgba(147, 197, 253, .45); transition: border-color 150ms ease; }
    .transcript-body a:hover { border-bottom-color: rgba(147, 197, 253, .7); }
    .transcript-body code { white-space: normal; overflow-wrap: anywhere; word-break: break-word; padding: 1px 5px; border-radius: 5px; border: 1px solid rgba(255,255,255,0.06); background: var(--bg-code); color: var(--text-code); font: .88em var(--font-mono); }
    .transcript-body pre { margin: 0 0 13px; overflow: auto; padding: 14px 16px; border: 1px solid var(--border-code); border-radius: 8px; background: linear-gradient(145deg, #0c0e14, #101420); box-shadow: inset 0 1px 0 rgba(255,255,255,0.04); white-space: pre; scrollbar-width: thin; scrollbar-color: #383c46 transparent; }
    .transcript-body pre code { white-space: pre; overflow-wrap: normal; word-break: normal; padding: 0; border-radius: 0; background: transparent; font-size: 12px; line-height: 1.5; }
    .transcript-item.user { justify-self: end; justify-items: end; max-width: min(82%, 520px); }
    .transcript-item.user .transcript-meta { display: none; }
    .transcript-item.user .transcript-body { white-space: pre-wrap; padding: 10px 13px; border: 1px solid #333842; border-radius: 20px 20px 4px 20px; background: var(--bg-user-bubble); line-height: 1.5; }
    .transcript-item.local-pending .transcript-body { opacity: .86; }
    .transcript-item.assistant { justify-self: start; max-width: 100%; padding-left: 22px; border-left: 2px solid var(--border-default); }
    .transcript-item.prompt-message { justify-self: stretch; max-width: 100%; margin-left: 6px; margin-right: 6px; padding-left: 0; border-left: 0; }
    .transcript-item.prompt-message .transcript-meta { display: none; }
    .transcript-item.prompt-message .transcript-body { display: grid; gap: 14px; }
    .transcript-item { animation: messageEnter 250ms var(--ease-default) forwards; }
    .transcript-item.user { animation-name: userMessageEnter; }
    .transcript-item.no-animate { animation: none; }
    @keyframes messageEnter { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    @keyframes userMessageEnter { from { opacity: 0; transform: translateX(12px); } to { opacity: 1; transform: translateX(0); } }
    .transcript-images { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
    .transcript-image { width: min(180px, 52vw); max-height: 180px; border-radius: 12px; object-fit: cover; border: 1px solid var(--border-input); background: var(--bg-canvas); }
    .status-event { display: grid; grid-template-columns: 18px 1fr; gap: 10px; align-items: start; color: var(--text-placeholder); font-size: 14px; line-height: 1.5; }
    .status-event:before { content: ""; width: 8px; height: 8px; margin-top: 7px; border-radius: 50%; background: #6b7280; }
    .status-event.busy:before { background: var(--color-warning); }
    .status-event.done:before { background: var(--color-success); }
    .status-event.failed:before { background: var(--color-error); }
    .status-title { display: block; color: var(--text-secondary); font-weight: var(--weight-bold); }
    .status-detail { display: block; white-space: pre-wrap; overflow-wrap: anywhere; }
    .plan-item { justify-self: stretch; margin: 2px 6px 20px; }
    .plan-card { position: relative; display: grid; gap: 26px; height: 432px; min-height: 432px; padding: 26px 26px 30px; border: 1px solid #333333; border-radius: 22px; background: #242424; color: #f8fafc; box-shadow: none; overflow: hidden; cursor: pointer; }
    .plan-card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 34px; color: #d4d4d8; }
    .plan-card-label { display: inline-flex; align-items: center; gap: 12px; min-width: 0; color: #d4d4d8; font-size: 21px; line-height: 1.2; }
    .plan-card-label svg { width: 28px; height: 28px; flex: 0 0 auto; }
    .plan-card-actions { display: inline-flex; gap: 12px; align-items: center; flex: 0 0 auto; }
    .plan-card-action { width: 34px; min-height: 34px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 10px; background: transparent; color: #f4f4f5; }
    .plan-card-action svg { width: 31px; height: 31px; }
    .plan-card-title { margin: 0; color: #ffffff; font-size: 33px; line-height: 1.16; font-weight: var(--weight-black); letter-spacing: 0; overflow-wrap: anywhere; }
    .plan-card-summary-title { margin: 0; color: #f5f5f5; font-size: 25px; line-height: 1.22; font-weight: var(--weight-black); letter-spacing: 0; }
    .plan-card-summary { display: grid; gap: 12px; color: #f4f4f5; font-size: 20px; line-height: 1.58; }
    .plan-card-summary p { margin: 0; }
    .plan-card:not(.expanded)::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 72px; background: linear-gradient(to bottom, rgba(36,36,36,0), #242424 86%); pointer-events: none; }
    .plan-card-execute { min-height: 44px; align-self: end; justify-self: start; padding: 0 18px; border-radius: 22px; border: 1px solid rgba(255,255,255,.14); background: #f4f4f5; color: #111; font-size: 16px; font-weight: var(--weight-extrabold); z-index: 1; }
    .plan-card-execute:disabled { opacity: .48; }
    .plan-card-readonly { min-height: 44px; align-self: end; justify-self: start; display: inline-flex; align-items: center; padding: 0 18px; border-radius: 22px; border: 1px solid rgba(255,255,255,.14); color: #d4d4d8; font-size: 15px; font-weight: var(--weight-bold); z-index: 1; }
    .plan-page-backdrop { position: fixed; inset: 0; z-index: 12; overflow-y: auto; overflow-x: hidden; max-width: 100vw; background: #000; color: #fff; }
    .plan-page-backdrop[hidden] { display: none; }
    .plan-page-shell { box-sizing: border-box; width: 100%; max-width: 100vw; min-height: 100vh; min-width: 0; display: grid; grid-template-rows: auto 1fr; overflow-x: hidden; background: #000; }
    .plan-page-top { position: sticky; top: 0; z-index: 1; box-sizing: border-box; width: 100%; max-width: 100vw; min-width: 0; display: grid; grid-template-columns: 64px minmax(0, 1fr) 104px; align-items: center; min-height: 92px; padding: 12px 20px 8px; background: rgba(0,0,0,.96); }
    .plan-page-close { width: 54px; min-height: 54px; border-radius: 50%; border: 1px solid #2d2d2f; background: #242426; color: #f5f5f5; font-size: 39px; line-height: 1; }
    .plan-page-heading-label { min-width: 0; text-align: center; color: #fff; font-size: 22px; line-height: 1.2; font-weight: var(--weight-black); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .plan-page-actions { display: flex; justify-content: flex-end; gap: 12px; }
    .plan-page-icon { width: 38px; min-height: 38px; display: grid; place-items: center; padding: 0; border: 0; background: transparent; color: #fff; }
    .plan-page-icon svg { width: 31px; height: 31px; }
    .plan-page-content { box-sizing: border-box; width: 100%; max-width: 100vw; min-width: 0; display: grid; gap: 34px; align-content: start; padding: 34px 26px 96px; overflow-x: hidden; }
    .plan-page-content > * { min-width: 0; max-width: 100%; }
    .plan-page-title { margin: 0; color: #fff; font-size: 41px; line-height: 1.16; font-weight: var(--weight-black); letter-spacing: 0; overflow-wrap: anywhere; }
    .plan-page-section-title { margin: 0; color: #fff; font-size: 31px; line-height: 1.22; font-weight: var(--weight-black); letter-spacing: 0; }
    .plan-page-title, .plan-page-summary, .plan-page-body { overflow-wrap: anywhere; word-break: break-word; }
    .plan-page-summary, .plan-page-body { color: #f4f4f5; font-size: 25px; line-height: 1.56; letter-spacing: 0; }
    .plan-page-summary p, .plan-page-body p { margin: 0 0 22px; }
    .plan-page-body h3 { margin: 34px 0 14px; color: #fff; font-size: 29px; line-height: 1.2; }
    .plan-page-body ul, .plan-page-body ol { margin: 0 0 22px 1.25em; padding: 0; display: grid; gap: 10px; }
    .plan-page-body li { min-width: 0; }
    .plan-page-body pre { max-width: 100%; overflow-x: auto; }
    .plan-page-body code, .plan-page-summary code { white-space: normal; overflow-wrap: anywhere; word-break: break-word; padding: 1px 5px; border-radius: 5px; background: #111; color: #f4f4f5; font: .88em var(--font-mono); }
    .plan-page-execute { min-height: 54px; border-radius: 27px; border: 0; background: #f4f4f5; color: #111; font-size: 18px; font-weight: var(--weight-black); }
    .plan-page-execute:disabled { opacity: .48; }
    .composer-activity { display: flex; gap: 5px; align-items: center; height: 20px; margin: 8px 0 14px 2px; opacity: 0; transition: opacity 200ms ease; }
    .composer-activity.active { opacity: 1; }
    .composer-activity-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted); animation: typingBounce 1.4s ease-in-out infinite; }
    .composer-activity-dot:nth-child(2) { animation-delay: 0.15s; }
    .composer-activity-dot:nth-child(3) { animation-delay: 0.30s; }
    @keyframes typingBounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-6px); opacity: 1; } }
    .history-fold { width: 100%; min-height: 38px; margin: 2px 0 10px; border: 0; border-bottom: 1px solid var(--border-subtle); border-radius: 0; background: transparent; color: var(--text-dim); text-align: left; font-size: 15px; }
    .history-fold[hidden] { display: none; }
    .turn-fold { border: 0; border-bottom: 1px solid var(--border-subtle); border-radius: 0; background: transparent; overflow: visible; }
    .turn-fold summary { display: grid; gap: 8px; min-height: 42px; padding: 0 0 8px; cursor: pointer; list-style: none; color: var(--text-secondary); }
    .turn-fold summary::-webkit-details-marker { display: none; }
    .turn-fold-row { display: flex; gap: 6px; align-items: center; min-width: 0; }
    .turn-fold-title { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 16px; }
    .turn-fold-chevron { flex: 0 0 auto; color: var(--text-placeholder); font-size: 18px; transition: transform .16s ease; }
    .turn-fold:not(.collapsed) .turn-fold-chevron { transform: rotate(90deg); }
    .turn-fold-preview { display: grid; grid-template-rows: 1fr; gap: 8px; padding: 0 0 8px; opacity: 1; transition: grid-template-rows 200ms ease, opacity 150ms ease; }
    .turn-fold:not(.collapsed) .turn-fold-preview { grid-template-rows: 0fr; opacity: 0; overflow: hidden; padding: 0; }
    .turn-fold-preview-line { min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; line-height: 1.42; }
    .turn-fold-preview-user { justify-self: end; max-width: min(82%, 520px); padding: 8px 11px; border: 1px solid #333842; border-radius: 15px 15px 4px 15px; background: var(--bg-interact); color: var(--btn-primary-bg); }
    .turn-fold-preview-assistant { justify-self: start; padding-left: 18px; border-left: 2px solid var(--border-default); color: var(--text-secondary); }
    .turn-fold-body { display: grid; grid-template-rows: 0fr; overflow: hidden; opacity: 0; transition: grid-template-rows 200ms ease, opacity 200ms ease 50ms; }
    .turn-fold-body-inner { min-height: 0; overflow: hidden; }
    .turn-fold:not(.collapsed) .turn-fold-body { grid-template-rows: 1fr; opacity: 1; padding: 12px 0 18px; }
    .codex-tool-call, .approval-card { position: relative; border: 1px solid var(--border-default); background: #0f1014; border-radius: 10px; overflow: hidden; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }
    .codex-tool-call.failed { border-color: #7f1d1d; }
    .tool-head, .approval-head { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 11px 12px; border-bottom: 1px solid var(--border-header); }
    .tool-title, .approval-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--btn-primary-bg); font-size: 14px; font-weight: var(--weight-bold); }
    .tool-state { color: var(--text-muted); font-size: 12px; }
    .tool-output { margin: 0; max-height: 260px; overflow: auto; padding: 11px 12px; color: var(--text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.45; }
    .approval-card { border-color: #854d0e; background: #171107; }
    .approval-card::after { content: ""; position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px; border-radius: 2px; background: var(--color-warning); }
    .approval-card.resolving { border-color: #a16207; }
    .approval-card.resolved { border-color: #166534; background: #07130b; }
    .approval-card.resolved::after { background: var(--color-success); }
    .approval-card.failed { border-color: #7f1d1d; background: #17090a; }
    .approval-card.failed::after { background: var(--color-error); }
    .file-change-summary { display: flex; justify-content: center; margin: 4px 0 10px; }
    .file-change-summary-pill { display: inline-flex; align-items: center; gap: 14px; min-height: 40px; max-width: 100%; padding: 0 18px; border: 1px solid #343434; border-radius: 20px; background: #242426; color: #f4f4f5; box-shadow: 0 12px 30px rgba(0,0,0,.32); font-size: 16px; line-height: 1; font-weight: var(--weight-extrabold); }
    .file-change-summary-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .file-change-summary-add { color: #22c55e; }
    .file-change-summary-del { color: #ff3b4f; }
    .approval-body { padding: 0 12px 12px; color: #fde68a; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.5; }
    .approval-state { padding: 0 12px 10px; color: #d6d3d1; font-size: 13px; }
    .approval-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 0 12px 12px; }
    .approval-action { min-height: 46px; border: 1px solid transparent; transition: opacity .16s ease, background .16s ease, border-color .16s ease; }
    .approval-action.approve { background: #14532d; color: #ecfdf5; border-color: var(--color-success); }
    .approval-action.danger { background: #7f1d1d; color: #fff1f2; border-color: var(--color-error); }
    .approval-action.selected { opacity: 1; box-shadow: inset 0 0 0 2px rgba(255,255,255,.38); }
    .approval-action.muted { background: var(--bg-pill); color: #8e929b; border-color: #34363d; opacity: .62; box-shadow: none; }
    .codex-input-dock { position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; display: grid; gap: 8px; padding: 10px 16px 16px; background: linear-gradient(to top, rgba(0,0,0,.98) 55%, rgba(0,0,0,.85) 78%, rgba(0,0,0,0)); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 1px solid #272930; }
    .composer-tools { display: flex; gap: 8px; align-items: center; min-width: 0; }
    .composer-settings { position: relative; flex: 1; display: flex; gap: 8px; min-width: 0; }
    .setting-pill { min-height: 38px; border-radius: 19px; padding: 0 14px; background: var(--bg-pill); color: var(--btn-primary-bg); border: 1px solid transparent; font-size: 14px; font-weight: var(--weight-extrabold); white-space: nowrap; transition: background var(--duration-fast) ease, border-color var(--duration-fast) ease; }
    .setting-pill.modified { border-color: rgba(147, 197, 253, 0.35); background: var(--bg-pill-modified); }
    .setting-pill:not(:disabled):hover { background: var(--bg-pill-hover); }
    .setting-pill.permissions { flex: 0 0 auto; }
    .setting-pill.handoff { flex: 0 0 auto; background: #1f2937; border: 1px solid var(--border-input); }
    .mode-chip-row { display: flex; gap: 8px; align-items: center; min-height: 0; }
    .mode-chip { display: inline-flex; align-items: center; gap: 8px; min-height: 38px; max-width: 100%; padding: 0 13px; border: 0; border-radius: 19px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 14px; font-weight: var(--weight-extrabold); }
    .mode-chip[hidden] { display: none; }
    .mode-chip-cancel { display: inline-grid; place-items: center; width: 18px; min-height: 18px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--btn-primary-bg); font-size: 16px; line-height: 1; }
    button.mode-chip-cancel:not(.secondary):not(.warn):not(:disabled):hover { background: rgba(255,255,255,.1); filter: none; }
    .model-popover { position: absolute; left: 0; bottom: 48px; width: min(330px, calc(100vw - 32px)); border: 1px solid var(--border-popover); border-radius: 22px; background: var(--bg-popover); box-shadow: 0 20px 54px rgba(0,0,0,.55); overflow: hidden; z-index: 6; opacity: 1; transform: translateY(0) scale(1); transform-origin: bottom left; transition: opacity 180ms var(--ease-default), transform 180ms var(--ease-default); }
    .model-popover.closed { opacity: 0; transform: translateY(8px) scale(0.96); pointer-events: none; }
    .handoff-panel { position: fixed; left: 26px; right: 26px; bottom: 112px; width: auto; max-height: min(76vh, 760px); display: grid; gap: 10px; padding: 0; border: 0; background: transparent; box-shadow: none; z-index: 7; }
    .handoff-panel[hidden] { display: none; }
    .handoff-targets, .handoff-actions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .handoff-target, .handoff-action { min-height: 38px; border-radius: 12px; border: 1px solid var(--border-input); background: var(--bg-interact); color: var(--btn-primary-bg); padding: 7px 8px; font-size: 14px; }
    .handoff-target.selected, .handoff-action.primary { background: var(--btn-primary-bg); color: var(--btn-primary-color); border-color: transparent; }
    .handoff-intent, .handoff-note { width: 100%; border: 1px solid var(--border-input); border-radius: 12px; background: var(--bg-attachment); color: var(--btn-primary-bg); font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .handoff-intent { min-height: 38px; padding: 0 10px; }
    .handoff-note { min-height: 58px; padding: 9px 10px; resize: vertical; }
    .handoff-preview { max-height: min(48vh, 620px); min-height: 260px; display: grid; grid-template-rows: auto auto minmax(0, 1fr); overflow: hidden; border: 1px solid rgba(255,255,255,.08); border-radius: 30px; background: #303033; color: #f4f4f5; box-shadow: 0 22px 64px rgba(0,0,0,.5); }
    .handoff-preview[hidden] { display: none; }
    .handoff-preview-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 62px; padding: 20px 26px 10px; }
    .handoff-preview-title { color: #f8fafc; font-size: 20px; font-weight: var(--weight-extrabold); line-height: 1.2; }
    .handoff-copy { width: 44px; min-height: 44px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 14px; background: transparent; color: #d4d4d8; font-size: 30px; }
    .handoff-copy:disabled { opacity: .38; }
    .handoff-copy.copied { color: #bbf7d0; }
    .handoff-copy svg { width: 32px; height: 32px; }
    .handoff-preview-status { padding: 0 26px 10px; color: #d1d5db; font-size: 13px; line-height: 1.42; white-space: pre-wrap; overflow-wrap: anywhere; }
    .handoff-preview.error { border-color: #7f1d1d; color: var(--color-error-light); }
    .handoff-preview.ok { border-color: rgba(255,255,255,.08); color: #f4f4f5; }
    .handoff-prompt-body { min-height: 0; overflow: auto; margin: 0; padding: 18px 26px 28px; white-space: pre-wrap; overflow-wrap: anywhere; font: 18px/1.42 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #f4f4f5; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.22) transparent; }
    .handoff-prompt-body[hidden] { display: none; }
    .prompt-preface { font-size: 24px; line-height: 1.45; color: #f4f4f5; }
    .prompt-card { position: relative; width: auto; height: min(48vh, 620px); min-height: 300px; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; border: 0; border-radius: 30px; background: #303030 !important; background-color: #303030 !important; color: #f4f4f5; box-shadow: none; -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
    .prompt-card.collapsed { height: 250px; min-height: 250px; cursor: pointer; }
    .prompt-card.collapsed::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 78px; background: linear-gradient(to bottom, rgba(48,48,48,0), #303030 78%); pointer-events: none; }
    .prompt-card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 0; padding: 20px 26px 0; background: #303030; }
    .prompt-card-title { color: #f8fafc; font-size: 25px; font-weight: 400; line-height: 1.45; letter-spacing: 0; }
    .prompt-card-copy svg { width: 32px; height: 32px; }
    .transcript-body .prompt-card-body { min-height: 0; overflow: auto; margin: 0; padding: 16px 26px 28px; border: 0; border-radius: 0; background: #303030; white-space: pre-wrap; overflow-wrap: break-word; word-break: normal; font: 22px/1.34 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #f4f4f5; letter-spacing: 0; -webkit-text-size-adjust: 100%; text-size-adjust: 100%; scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.22) transparent; }
    .handoff-actions { grid-template-columns: 1fr 1fr; }
    .setting-row { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr) auto; gap: 12px; align-items: center; min-height: 76px; padding: 12px 18px; border-bottom: 1px solid var(--border-section); color: var(--btn-primary-bg); }
    .setting-row:last-child { border-bottom: 0; }
    .setting-label { display: grid; gap: 5px; min-width: 0; font-size: 16px; font-weight: var(--weight-extrabold); }
    .setting-value { color: var(--text-dim); font-size: 14px; font-weight: var(--weight-medium); }
    .setting-chevron { color: var(--btn-primary-bg); font-size: 28px; line-height: 1; }
    .model-selector, .setting-selector { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; pointer-events: none; }
    .setting-options { display: grid; gap: 6px; padding: 0 12px 12px; border-bottom: 1px solid var(--border-section); background: var(--bg-setting-options); }
    .setting-options[hidden] { display: none; }
    .setting-option { display: flex; justify-content: space-between; gap: 10px; align-items: center; min-height: 38px; border-radius: 13px; padding: 7px 11px; background: transparent; color: var(--text-secondary); font-size: 15px; text-align: left; }
    .permission-popover .setting-options { padding: 12px; border-bottom: 0; }
    .setting-option-copy { display: grid; gap: 3px; min-width: 0; }
    .setting-option-title { color: var(--btn-primary-bg); font-size: 15px; font-weight: var(--weight-bold); }
    .setting-option-desc { color: var(--text-dim); font-size: 13px; line-height: 1.35; }
    .setting-option.selected { background: var(--bg-option-selected); color: #fff; }
    .setting-option-check { color: var(--btn-primary-bg); font-weight: var(--weight-black); }
    .attach-button { width: 40px; min-height: 38px; padding: 0; border-radius: 11px; background: var(--bg-interact); color: var(--btn-primary-bg); border: 1px solid var(--border-input); font-size: 24px; line-height: 1; }
    .composer-action-menu { position: absolute; left: 16px; right: 16px; bottom: 104px; max-height: min(58vh, 520px); overflow-y: auto; border: 1px solid var(--border-popover); border-radius: 22px; background: var(--bg-popover); box-shadow: 0 20px 54px rgba(0,0,0,.55); padding: 14px; z-index: 8; opacity: 1; transform: translateY(0) scale(1); transform-origin: bottom left; transition: opacity 180ms var(--ease-default), transform 180ms var(--ease-default); }
    .composer-action-menu.closed { opacity: 0; transform: translateY(8px) scale(0.96); pointer-events: none; }
    .composer-menu-item { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 12px; align-items: center; width: 100%; min-height: 58px; padding: 8px 10px; border: 0; border-radius: 14px; background: transparent; color: var(--btn-primary-bg); text-align: left; }
    button.composer-menu-item:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .composer-menu-item:disabled { opacity: .82; }
    .composer-menu-icon { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 10px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 20px; font-weight: var(--weight-black); }
    .composer-menu-title { display: block; min-width: 0; color: var(--btn-primary-bg); font-size: 17px; font-weight: var(--weight-extrabold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-desc { display: block; margin-top: 3px; min-width: 0; color: var(--text-dim); font-size: 13px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-check { color: var(--btn-primary-bg); font-size: 18px; font-weight: var(--weight-black); }
    .composer-menu-section { margin: 12px 2px 8px; padding-top: 12px; border-top: 1px solid var(--border-section); color: var(--text-dim); font-size: 14px; font-weight: var(--weight-medium); }
    .plugin-list { display: grid; gap: 4px; }
    .plugin-dot { width: 30px; height: 30px; border-radius: 9px; background: var(--bg-pill); color: var(--btn-primary-bg); display: grid; place-items: center; font-size: 13px; font-weight: var(--weight-black); overflow: hidden; }
    .plugin-dot img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .selected-plugin-strip { display: flex; gap: 8px; min-height: 38px; align-items: center; overflow-x: auto; }
    .selected-plugin-strip[hidden] { display: none; }
    .selected-plugin-chip { display: inline-flex; align-items: center; gap: 7px; min-height: 38px; max-width: 180px; padding: 0 12px; border: 0; border-radius: 19px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 14px; font-weight: var(--weight-extrabold); }
    .selected-plugin-chip .plugin-dot { width: 20px; height: 20px; border-radius: 6px; font-size: 9px; }
    .selected-plugin-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .plugin-autocomplete { position: absolute; left: 16px; right: 16px; bottom: 104px; display: grid; gap: 6px; padding: 8px; border: 1px solid var(--border-popover); border-radius: 22px; background: var(--bg-popover); box-shadow: 0 20px 54px rgba(0,0,0,.55); z-index: 9; }
    .plugin-autocomplete[hidden] { display: none; }
    .plugin-suggestion { display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 12px; align-items: center; min-height: 64px; padding: 10px 12px; border: 0; border-radius: 16px; background: transparent; color: var(--btn-primary-bg); text-align: left; }
    button.plugin-suggestion:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .plugin-suggestion-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--btn-primary-bg); font-size: 17px; font-weight: var(--weight-extrabold); }
    .plugin-suggestion-desc { margin-top: 3px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-dim); font-size: 13px; line-height: 1.35; }
    .send-status { min-width: 66px; color: var(--text-muted); font-size: 12px; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: color 300ms ease, opacity 300ms ease; }
    .send-status.error { color: var(--color-error-light); }
    .send-status.ok { color: var(--color-success); }
    .attachment-strip { display: flex; gap: 8px; min-height: 54px; overflow-x: auto; padding-bottom: 2px; }
    .attachment-strip[hidden] { display: none; }
    .attachment-chip { position: relative; flex: 0 0 auto; display: grid; grid-template-columns: 46px minmax(80px, 1fr) 28px; align-items: center; gap: 8px; max-width: 230px; min-height: 50px; border: 1px solid var(--border-default); border-radius: 10px; background: var(--bg-attachment); padding: 4px; color: var(--btn-primary-bg); }
    .attachment-chip img { width: 46px; height: 42px; border-radius: 7px; object-fit: cover; background: var(--bg-canvas); }
    .attachment-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); font-size: 12px; }
    .attachment-remove { width: 28px; min-height: 28px; padding: 0; border-radius: 50%; background: var(--bg-remove-btn); color: var(--btn-primary-bg); font-size: 16px; }
    .live-workspace-bar { display: flex; align-items: center; gap: 8px; min-height: 34px; min-width: 0; }
    .live-workspace-icon { width: 18px; height: 14px; flex: 0 0 auto; border: 2px solid var(--text-dim); border-radius: 3px 3px 0 0; border-bottom: 0; position: relative; }
    .live-workspace-icon:before { content: ""; position: absolute; top: -5px; left: 50%; width: 8px; height: 5px; border: 2px solid var(--text-dim); border-bottom: 0; border-radius: 3px 3px 0 0; transform: translateX(-50%); }
    .live-workspace-label { color: var(--text-dim); font-size: 12px; font-weight: var(--weight-extrabold); white-space: nowrap; }
    .live-workspace-chip { display: inline-flex; align-items: center; gap: 6px; min-width: 0; max-width: 100%; min-height: 30px; padding: 0 11px; border: 1px solid var(--border-subtle); border-radius: 15px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 13px; font-weight: var(--weight-extrabold); overflow: hidden; cursor: pointer; }
    button.live-workspace-chip:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-pill-hover); border-color: var(--border-default); filter: none; }
    .live-workspace-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .live-workspace-action { color: var(--text-dim); font-size: 12px; flex: 0 0 auto; }
    .interruption-choice { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 2px 0; }
    .interruption-choice[hidden] { display: none; }
    .choice-action { min-height: 42px; border-radius: 12px; background: var(--bg-interact); color: var(--btn-primary-bg); border: 1px solid var(--border-input); }
    .choice-action.primary { background: var(--btn-primary-bg); color: var(--btn-primary-color); border: 0; }
    .dock-row { display: flex; gap: 10px; min-width: 0; align-items: center; }
    .dock-actions { display: flex; gap: 10px; min-width: 0; }
    .dock-actions[hidden] { display: none; }
    input { flex: 1; min-width: 0; min-height: 54px; border-radius: var(--radius-lg); border: 1px solid var(--border-input); background: var(--bg-input); color: var(--btn-primary-bg); padding: 0 14px; font-size: 15px; }
    .primary-action { flex: 0 0 52px; width: 52px; min-height: 52px; border-radius: 26px; padding: 0; display: grid; place-items: center; background: #f4f4f5; color: #050505; font-size: 24px; line-height: 1; }
    .primary-action:disabled { background: #f4f4f5; color: #050505; opacity: .48; }
    .primary-action.stop { background: #f4f4f5; color: #050505; font-size: 24px; }
    @media (min-width: 820px) {
      main { max-width: 780px; margin: 0 auto; }
      .codex-input-dock { left: 50%; transform: translateX(-50%); width: min(780px, 100%); }
    }
  </style>
</head>
<body class="aurora-bg noise-overlay"__MARVIS_BODY_ATTR__>
  <div class="native-mobile-shell codex-run-shell">
    <header id="header">
      <button class="circle" id="back" aria-label="返回">‹</button>
      <div class="screen-title">
        <template><h1>__PROVIDER_LABEL_TEXT__</h1></template>
        <template>连接 __PROVIDER_LABEL_TEXT__ 会话</template>
        <template>输入消息开始 __PROVIDER_LABEL_TEXT__ 会话</template>
        <h1></h1>
        <div class="subtitle"><span class="status-dot"></span><span id="state">connecting</span></div>
      </div>
      <button class="circle" aria-label="菜单">⋮</button>
    </header>
    <div class="session-float" id="sessionFloat" aria-label="当前会话">
      <span class="session-float-title" id="sessionFloatTitle"></span>
      <span class="session-float-meta"><span class="laptop"></span><span id="sessionFloatMeta">wlcodex</span></span>
    </div>
    <div class="header-run-indicator neutral" id="headerRunIndicator">
      <button class="header-run-button header-context-button" id="headerContextButton" type="button" aria-label="状态">
        <span class="header-run-status" aria-hidden="true"><span class="header-run-spinner"></span><span class="header-run-dot"></span></span>
      </button>
      <button class="header-run-button header-session-menu-button" id="headerSessionMenuButton" type="button" aria-label="会话操作">
        <span class="header-run-menu" aria-hidden="true">⋮</span>
      </button>
    </div>
    <section class="context-info-sheet" id="contextInfoPopover" aria-label="状态" hidden>
      <span class="context-sheet-handle" aria-hidden="true"></span>
      <div class="context-sheet-header">
        <h2 class="context-info-title">状态</h2>
        <button class="context-info-close" id="contextInfoClose" type="button" aria-label="关闭">×</button>
      </div>
      <div class="context-info-grid">
        <div class="context-info-row">
          <span class="context-info-label">对话线程:</span>
          <span class="context-info-value-wrap">
            <span class="context-info-value" id="contextThreadValue">未连接</span>
            <button class="context-info-copy" id="contextThreadCopyButton" type="button" aria-label="复制会话 ID"></button>
          </span>
        </div>
        <div class="context-info-row"><span class="context-info-label">目录:</span><span class="context-info-value" id="contextDirectoryValue">wlcodex</span></div>
        <div class="context-info-row"><span class="context-info-label">上下文:</span><span class="context-info-value" id="contextUsageValue">等待同步</span></div>
        <div class="context-info-row"><span class="context-info-label">5 小时限制:</span><span class="context-info-value" id="contextFiveHourValue">等待同步</span></div>
        <div class="context-info-row"><span class="context-info-label">7 天限制:</span><span class="context-info-value" id="contextSevenDayValue">等待同步</span></div>
      </div>
    </section>
    <section class="native-header-popover session-action-menu" id="sessionActionMenu" role="menu" aria-label="会话操作" hidden>
      <h2 class="session-action-title" id="sessionActionTitle" hidden></h2>
      <button class="session-action-item" id="pinSessionButton" type="button" role="menuitem">
        <span class="session-action-icon"></span><span>置顶</span>
      </button>
      <button class="session-action-item" id="copySessionIdButton" type="button" role="menuitem">
        <span class="session-action-icon"></span><span>复制会话 ID</span>
      </button>
      <button class="session-action-item" id="renameSessionButton" type="button" role="menuitem">
        <span class="session-action-icon"></span><span>重命名</span>
      </button>
      <button class="session-action-item danger" id="archiveSessionButton" type="button" role="menuitem">
        <span class="session-action-icon"></span><span>归档</span>
      </button>
    </section>
    <main>
      <section class="codex-status-flow run-state" id="runStatus">
        <span class="run-pulse"></span>
        <span id="runStateLabel">连接会话</span>
        <span class="event-cursor" id="cursor"></span>
      </section>
      <button class="history-fold" id="historyFold" hidden>更早的消息</button>
      <section class="codex-transcript" id="events"><div class="empty" id="empty">输入消息开始新会话</div></section>
      <div class="composer-activity" id="composerActivity" aria-hidden="true">
        <span class="composer-activity-dot"></span>
        <span class="composer-activity-dot"></span>
        <span class="composer-activity-dot"></span>
      </div>
    </main>
    <section class="codex-input-dock">
      <div class="composer-tools">
        <div class="composer-settings">
          <button class="setting-pill" id="modelSettingsButton" type="button">加载模型</button>
          <button class="setting-pill permissions" id="permissionSettingsButton" type="button">自动审核</button>
          <button class="setting-pill handoff" id="handoffButton" type="button">接棒执行</button>
          <div class="model-popover permission-popover closed" id="permissionPopover">
            <select id="permissionSelector" class="setting-selector" aria-label="选择权限模式" hidden>
              <option value="default">默认权限</option>
            </select>
            <div class="setting-options" id="permissionOptions" hidden></div>
          </div>
          <div class="handoff-panel" id="handoffPanel" hidden>
            <div class="handoff-targets" id="handoffTargets">
              <button class="handoff-target" type="button" data-provider="codex">Codex</button>
              <button class="handoff-target" type="button" data-provider="claude">Claude</button>
              <button class="handoff-target" type="button" data-provider="antigravity">Antigravity</button>
            </div>
            <select class="handoff-intent" id="handoffIntent" aria-label="接棒意图">
              <option value="auto">自动判断</option>
              <option value="execute_plan">执行计划</option>
              <option value="fix_bug">修复 Bug</option>
              <option value="implement_feature">做小功能</option>
              <option value="continue_work">继续工作</option>
              <option value="custom">自定义</option>
            </select>
            <textarea class="handoff-note" id="handoffNote" rows="2" placeholder="补充给下一个智能体"></textarea>
            <div class="handoff-preview" id="handoffPreview" hidden>
              <div class="handoff-preview-head">
                <span class="handoff-preview-title">Plain text</span>
                <button class="handoff-copy" id="handoffCopyButton" type="button" aria-label="复制提示词" disabled></button>
              </div>
              <div class="handoff-preview-status" id="handoffPreviewStatus"></div>
              <pre class="handoff-prompt-body" id="handoffPromptBody" hidden></pre>
            </div>
            <div class="handoff-actions">
              <button class="handoff-action" id="handoffPreviewButton" type="button">预览</button>
              <button class="handoff-action primary" id="handoffExecuteButton" type="button" disabled>执行</button>
            </div>
          </div>
          <div class="model-popover closed" id="modelPopover">
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
        <div class="composer-action-menu closed" id="composerActionMenu" role="menu" aria-label="输入操作">
          <button class="composer-menu-item" id="menuUploadPhoto" type="button" role="menuitem">
            <span class="composer-menu-icon">▧</span>
            <span>
              <span class="composer-menu-title">上传照片</span>
              <span class="composer-menu-desc">添加图片到下一条消息</span>
            </span>
            <span></span>
          </button>
          <button class="composer-menu-item" id="menuPlanMode" type="button" role="menuitem"__PLAN_MODE_ACTION_HIDDEN__ aria-pressed="false">
            <span class="composer-menu-icon">☷</span>
            <span>
              <span class="composer-menu-title">计划模式</span>
              <span class="composer-menu-desc">下一轮先规划再执行</span>
            </span>
            <span class="composer-menu-check" id="planModeCheck"></span>
          </button>
          <div class="composer-menu-section" id="pluginMenuSection"__PLUGIN_MENU_HIDDEN__>插件</div>
          <div class="plugin-list" id="pluginList"__PLUGIN_MENU_HIDDEN__></div>
        </div>
        <div class="plugin-autocomplete" id="pluginAutocomplete" hidden></div>
        <button class="attach-button" id="attachmentButton" type="button" aria-label="上传照片">＋</button>
        <input id="imageInput" type="file" accept="image/*" multiple hidden>
        <span class="send-status" id="sendStatus"></span>
      </div>
      <div class="attachment-strip" id="attachmentStrip" hidden></div>
      <div class="selected-plugin-strip" id="selectedPluginStrip" hidden></div>
      <div class="mode-chip-row">
        <div class="mode-chip plan-mode-chip" id="planModeChip" hidden>
          <span>☷ 计划</span>
          <button class="mode-chip-cancel" id="planModeChipCancel" type="button" aria-label="取消计划模式">×</button>
        </div>
      </div>
      <div class="live-workspace-bar" id="liveWorkspaceBar">
        <span class="live-workspace-icon" aria-hidden="true"></span>
        <span class="live-workspace-label">工作区</span>
        <button class="live-workspace-chip" id="liveWorkspaceChip" type="button">
          <span class="live-workspace-name" id="liveWorkspaceName">同步中</span>
          <span class="live-workspace-action">切换</span>
        </button>
      </div>
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
    <pre class="viewport-debug" id="viewportDebug" hidden></pre>
  </div>
  <div class="plan-page-backdrop" id="planPage" role="dialog" aria-modal="true" aria-label="计划" hidden>
    <div class="plan-page-shell">
      <div class="plan-page-top">
        <button class="plan-page-close" id="planPageClose" type="button" aria-label="关闭">×</button>
        <div class="plan-page-heading-label">计划</div>
        <div class="plan-page-actions">
          <button class="plan-page-icon" id="planPageDownload" type="button" aria-label="下载计划"></button>
          <button class="plan-page-icon" id="planPageCopy" type="button" aria-label="复制计划"></button>
        </div>
      </div>
      <section class="plan-page-content" id="planPageContent">
        <h2 class="plan-page-title" id="planPageTitle"></h2>
        <h3 class="plan-page-section-title">Summary</h3>
        <div class="plan-page-summary" id="planPageSummary"></div>
        <div class="plan-page-body" id="planPageBody"></div>
        <button class="plan-page-execute" id="planPageExecute" type="button">执行计划</button>
      </section>
    </div>
  </div>
  <script>
    const state = document.getElementById("state");
    const cursor = document.getElementById("cursor");
    const events = document.getElementById("events");
    const header = document.getElementById("header");
    const empty = document.getElementById("empty");
    const runStatus = document.getElementById("runStatus");
    const runStateLabel = document.getElementById("runStateLabel");
    const headerRunIndicator = document.getElementById("headerRunIndicator");
    const headerContextButton = document.getElementById("headerContextButton");
    const headerSessionMenuButton = document.getElementById("headerSessionMenuButton");
    const contextInfoPopover = document.getElementById("contextInfoPopover");
    const contextInfoClose = document.getElementById("contextInfoClose");
    const sessionActionMenu = document.getElementById("sessionActionMenu");
    const contextThreadValue = document.getElementById("contextThreadValue");
    const contextDirectoryValue = document.getElementById("contextDirectoryValue");
    const contextUsageValue = document.getElementById("contextUsageValue");
    const contextFiveHourValue = document.getElementById("contextFiveHourValue");
    const contextSevenDayValue = document.getElementById("contextSevenDayValue");
    const contextThreadCopyButton = document.getElementById("contextThreadCopyButton");
    const sessionActionTitle = document.getElementById("sessionActionTitle");
    const pinSessionButton = document.getElementById("pinSessionButton");
    const copySessionIdButton = document.getElementById("copySessionIdButton");
    const renameSessionButton = document.getElementById("renameSessionButton");
    const archiveSessionButton = document.getElementById("archiveSessionButton");
    const sessionFloatTitle = document.getElementById("sessionFloatTitle");
    const sessionFloatMeta = document.getElementById("sessionFloatMeta");
    const historyFold = document.getElementById("historyFold");
    const composerActivity = document.getElementById("composerActivity");
    const inputDock = document.querySelector(".codex-input-dock");
    const params = new URLSearchParams(location.search);
    const viewportDebug = document.getElementById("viewportDebug");
    const debugViewport = params.get("debug_viewport") === "1";
    const token = params.get("token") || "";
    const PROVIDER = __PROVIDER_JSON__;
__ICONS_JS__
    const PROVIDER_LABEL = __PROVIDER_LABEL_JSON__;
    const API_BASE = __API_BASE_JSON__;
    const SUPPORTS_PLAN_MODE = __SUPPORTS_PLAN_MODE_JSON__;
    const SUPPORTS_PLUGIN_MENU = __SUPPORTS_PLUGIN_MENU_JSON__;
    const USES_CLAUDE_PLAN_PERMISSION_MODE = __USES_CLAUDE_PLAN_PERMISSION_MODE_JSON__;
    let nativeThreadId = params.get("native_thread_id") || "";
    let invalidNativeThreadId = Boolean(nativeThreadId && !isValidNativeThreadId(nativeThreadId));
    if (invalidNativeThreadId) nativeThreadId = "";
    markNativeSessionViewed(nativeThreadId);
    let nativeTurnId = "";
    let activeTurnId = "";
    let attached = false;
    let loadedEvents = [];
    let oldestEventId = 0;
    let latestEventId = 0;
    let previousEventCount = 0;
    let source = null;
    let pollInFlight = false;
    let nativeSyncInFlight = false;
    let nativeTranscriptSyncTimer = null;
    const terminalTranscriptSyncTurns = new Set();
    const authHeaders = token ? {"Authorization": "Bearer " + token} : {};
    const promptInput = document.getElementById("prompt");
    const continueButton = document.getElementById("continue");
    const steerButton = document.getElementById("steer");
    const interruptButton = document.getElementById("interrupt");
    const modelSettingsButton = document.getElementById("modelSettingsButton");
    const permissionSettingsButton = document.getElementById("permissionSettingsButton");
    const modelPopover = document.getElementById("modelPopover");
    const permissionPopover = document.getElementById("permissionPopover");
    const modelSettingValue = document.getElementById("modelSettingValue");
    const reasoningSettingValue = document.getElementById("reasoningSettingValue");
    const serviceTierSettingValue = document.getElementById("serviceTierSettingValue");
    const modelSettingRow = modelSettingValue.closest(".setting-row");
    const reasoningSettingRow = reasoningSettingValue.closest(".setting-row");
    const serviceTierSettingRow = serviceTierSettingValue.closest(".setting-row");
    const modelSelector = document.getElementById("modelSelector");
    const permissionSelector = document.getElementById("permissionSelector");
    const reasoningSelector = document.getElementById("reasoningSelector");
    const serviceTierSelector = document.getElementById("serviceTierSelector");
    const modelOptions = document.getElementById("modelOptions");
    const permissionOptions = document.getElementById("permissionOptions");
    const reasoningOptions = document.getElementById("reasoningOptions");
    const serviceTierOptions = document.getElementById("serviceTierOptions");
    const attachmentButton = document.getElementById("attachmentButton");
    const imageInput = document.getElementById("imageInput");
    const attachmentStrip = document.getElementById("attachmentStrip");
    const interruptionChoice = document.getElementById("interruptionChoice");
    const steerChoice = document.getElementById("steerChoice");
    const queueChoice = document.getElementById("queueChoice");

    function markNativeSessionViewed(threadId) {
      threadId = String(threadId || "");
      if (!threadId) return;
      try {
        localStorage.setItem("wlcodex:native-session-viewed:" + PROVIDER + ":" + threadId, "1");
      } catch (error) {}
    }
    function syncDockHeight() {
      if (!inputDock) return;
      const rect = inputDock.getBoundingClientRect();
      document.documentElement.style.setProperty("--codex-dock-height", `${Math.ceil(rect.height)}px`);
    }
    syncDockHeight();
    if ("ResizeObserver" in window && inputDock) {
      const dockResizeObserver = new ResizeObserver(syncDockHeight);
      dockResizeObserver.observe(inputDock);
    }
    window.addEventListener("resize", syncDockHeight);
    function computedSize(element) {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      const styles = getComputedStyle(element);
      return {
        width: Math.round(rect.width * 100) / 100,
        height: Math.round(rect.height * 100) / 100,
        fontSize: styles.fontSize,
        lineHeight: styles.lineHeight,
        webkitTextSizeAdjust: styles.webkitTextSizeAdjust || styles.textSizeAdjust || ""
      };
    }
    function collectViewportDebugMetrics() {
      return {
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        clientWidth: document.documentElement.clientWidth,
        clientHeight: document.documentElement.clientHeight,
        devicePixelRatio: window.devicePixelRatio,
        visualViewport: window.visualViewport ? {
          width: Math.round(window.visualViewport.width * 100) / 100,
          height: Math.round(window.visualViewport.height * 100) / 100,
          scale: window.visualViewport.scale,
          offsetLeft: Math.round(window.visualViewport.offsetLeft * 100) / 100,
          offsetTop: Math.round(window.visualViewport.offsetTop * 100) / 100
        } : null,
        computedCircleSize: computedSize(document.querySelector(".circle")),
        computedTranscriptSize: computedSize(document.querySelector(".transcript-body")),
        computedDockSize: computedSize(inputDock)
      };
    }
    function updateViewportDebug() {
      if (!debugViewport || !viewportDebug) return;
      const metrics = collectViewportDebugMetrics();
      viewportDebug.hidden = false;
      viewportDebug.textContent = JSON.stringify(metrics, null, 2);
      console.info("wlcodex viewport debug", metrics);
    }
    if (debugViewport) {
      updateViewportDebug();
      window.addEventListener("resize", updateViewportDebug);
      if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", updateViewportDebug);
        window.visualViewport.addEventListener("scroll", updateViewportDebug);
      }
    }
    const sendStatus = document.getElementById("sendStatus");
    const composerActionMenu = document.getElementById("composerActionMenu");
    const menuUploadPhoto = document.getElementById("menuUploadPhoto");
    const menuPlanMode = document.getElementById("menuPlanMode");
    const pluginMenuSection = document.getElementById("pluginMenuSection");
    const pluginList = document.getElementById("pluginList");
    const selectedPluginStrip = document.getElementById("selectedPluginStrip");
    const pluginAutocomplete = document.getElementById("pluginAutocomplete");
    const planModeCheck = document.getElementById("planModeCheck");
    const planModeChip = document.getElementById("planModeChip");
    const planModeChipCancel = document.getElementById("planModeChipCancel");
    const liveWorkspaceChip = document.getElementById("liveWorkspaceChip");
    const liveWorkspaceName = document.getElementById("liveWorkspaceName");
    const handoffButton = document.getElementById("handoffButton");
    const handoffPanel = document.getElementById("handoffPanel");
    const handoffTargets = document.getElementById("handoffTargets");
    const handoffTargetButtons = Array.from(handoffTargets.querySelectorAll(".handoff-target"));
    const handoffIntent = document.getElementById("handoffIntent");
    const handoffNote = document.getElementById("handoffNote");
    const handoffPreviewEl = document.getElementById("handoffPreview");
    const handoffPreviewStatus = document.getElementById("handoffPreviewStatus");
    const handoffPromptBody = document.getElementById("handoffPromptBody");
    const handoffCopyButton = document.getElementById("handoffCopyButton");
    const handoffPreviewButton = document.getElementById("handoffPreviewButton");
    const handoffExecuteButton = document.getElementById("handoffExecuteButton");
    const planPage = document.getElementById("planPage");
    const planPageClose = document.getElementById("planPageClose");
    const planPageDownload = document.getElementById("planPageDownload");
    const planPageCopy = document.getElementById("planPageCopy");
    const planPageTitle = document.getElementById("planPageTitle");
    const planPageSummary = document.getElementById("planPageSummary");
    const planPageBody = document.getElementById("planPageBody");
    const planPageExecute = document.getElementById("planPageExecute");
    const streamPathBase = "__STREAM_PATH__";
    const agentRunId = __AGENT_RUN_ID__;
    const CURRENT_TURN_EVENT_LIMIT = 5000;
    const RECENT_EVENT_LIMIT = 80;
    const OLDER_EVENT_LIMIT = 80;
    const MODEL_SETTINGS_STORAGE_KEY = "wlcodexNativeModelSettings";
    const MODEL_SETTINGS_STORAGE_VERSION = 2;
    const PERMISSION_SETTINGS_STORAGE_KEY = "wlcodexNativePermissionSettings";
    const COLLABORATION_MODE_STORAGE_KEY = "wlcodexNativeCollaborationMode";
    const DEFAULT_PERMISSION_MODE = "auto_review";
    const PERMISSION_SETTINGS_STORAGE_VERSION = 2;
    const PERMISSION_PRESETS = __PERMISSION_PRESETS_JSON__;
    const PLUGIN_MENU_ITEMS = __PLUGIN_MENU_ITEMS_JSON__;
    const transcriptNodes = new Map();
    const statusNodes = new Map();
    const commandNodes = new Map();
    const fileChangeSummaryNodes = new Map();
    const fileChangeSummaryStates = new Map();
    let renderTarget = events;
    let imageAttachments = [];
    const MAX_IMAGE_DATA_URL_CHARS = 2500000;
    const IMAGE_RESIZE_MAX_SIDE = 1280;
    const IMAGE_RESIZE_MIN_SIDE = 640;
    let pendingUserEcho = null;
    let selectedPlugins = [];
    let currentSessionInfo = {};
    let sendingPrompt = false;
    let nativeTurnRunning = false;
    let modelCatalog = [];
    let providerCapabilities = {};
    let savedModelSettings = loadSavedModelSettings();
    let savedPermissionSettings = loadSavedPermissionSettings();
    let selectedCollaborationMode = loadSavedCollaborationMode();
    let modelSettingsDirty = false;
    let permissionSettingsDirty = false;
    let handoffTargetProvider = "";
    let handoffPreviewPayload = null;
    let handoffBusy = false;
    let handoffCopyStateTimer = 0;
    let activePlan = null;
    handoffCopyButton.innerHTML = ICONS.copy;
    planPageDownload.innerHTML = ICONS.download;
    planPageCopy.innerHTML = ICONS.copy;
    pinSessionButton.querySelector(".session-action-icon").innerHTML = ICONS.pin;
    contextThreadCopyButton.innerHTML = ICONS.copy;
    copySessionIdButton.querySelector(".session-action-icon").innerHTML = ICONS.copy;
    renameSessionButton.querySelector(".session-action-icon").innerHTML = ICONS.pencil;
    archiveSessionButton.querySelector(".session-action-icon").innerHTML = ICONS.archive;
    historyFold.onclick = loadOlderEvents;
    function isValidNativeThreadId(value) {
      return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(value || ""));
    }
    function nativeErrorMessage(message) {
      const text = String(message || "");
      if (text === "native session not found" || text === "KeyError") {
        return "会话不存在或已被清理";
      }
      return text;
    }
    function isFetchNetworkError(error) {
      const text = String((error && error.message) || error || "");
      return Boolean(error && error.name === "TypeError") || /failed to fetch|network/i.test(text);
    }
    function delay(ms) {
      return new Promise(resolve => window.setTimeout(resolve, ms));
    }
    function snapshotNativeTurnControl() {
      return {
        nativeTurnId,
        activeTurnId,
        nativeTurnRunning
      };
    }
    function nativeTurnAdvancedSince(snapshot) {
      const before = snapshot || {};
      return Boolean(
        (nativeTurnId && nativeTurnId !== before.nativeTurnId) ||
        (activeTurnId && activeTurnId !== before.activeTurnId) ||
        (nativeTurnRunning && !before.nativeTurnRunning)
      );
    }
    async function recoverNativeControlAfterFetchFailure(error, snapshot) {
      if (!isFetchNetworkError(error)) return false;
      await delay(700);
      await syncNativeTranscript();
      await pollEvents();
      return nativeTurnAdvancedSince(snapshot);
    }
    function clearComposerDraft() {
      promptInput.value = "";
      imageAttachments = [];
      renderAttachments();
      resetComposerPlugins();
    }
    function clearComposerDraftToSnapshot(snapshot) {
      if (!snapshot) {
        clearComposerDraft();
        return;
      }
      promptInput.value = String(snapshot.prompt || "");
      imageAttachments = (snapshot.imageAttachments || []).map(image => ({...image}));
      renderAttachments();
      resetComposerPlugins();
      updateComposerDisabled();
    }
    async function api(path, options = {}) {
      const {timeoutMs = 0, ...fetchOptions} = options;
      let timeoutId = 0;
      if (timeoutMs > 0 && !fetchOptions.signal) {
        const controller = new AbortController();
        fetchOptions.signal = controller.signal;
        timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
      }
      try {
        const response = await fetch(path, {
          ...fetchOptions,
          headers: {"Content-Type": "application/json", ...authHeaders, ...(fetchOptions.headers || {})}
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(nativeErrorMessage(body.error || response.statusText));
        }
        return response.json().catch(() => ({}));
      } catch (error) {
        if (error && error.name === "AbortError") throw new Error("请求超时");
        throw error;
      } finally {
        if (timeoutId) window.clearTimeout(timeoutId);
      }
    }
    async function loadProviderCapabilities() {
      try {
        providerCapabilities = await api(`${API_BASE}/capabilities`);
      } catch (_error) {
        providerCapabilities = {};
      }
      updateComposerDisabled();
    }
    function canSteerActiveTurn() {
      return providerCapabilities.can_steer_active_turn !== false;
    }
    function canInterruptActiveTurn() {
      return providerCapabilities.can_interrupt !== false;
    }
    async function nativeControl(action, body) {
      if (!nativeThreadId) throw new Error("会话未连接");
      return api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/${action}`, {
        method: "POST",
        body: JSON.stringify(body)
      });
    }
    async function loadNativeSessionInfo() {
      if (!nativeThreadId || invalidNativeThreadId) {
        updateNativeSessionInfo({});
        return;
      }
      try {
        const session = await api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}`, {timeoutMs: 2500});
        updateNativeSessionInfo(session || {});
      } catch (error) {
        updateNativeSessionInfo({status: error.message || "不可用"});
      }
    }
    function updateNativeSessionInfo(session) {
      currentSessionInfo = {...currentSessionInfo, ...(session || {})};
      updateNativeHeaderContext();
    }
    function updateNativeHeaderContext() {
      const title = nativeSessionTitle();
      const directory = nativeSessionDirectory();
      const project = nativeSessionProjectLabel(directory);
      writeCompactText(sessionFloatTitle, title);
      writeCompactText(sessionFloatMeta, project || "wlcodex");
      writeCompactText(contextThreadValue, nativeThreadId || "未连接");
      writeCompactText(contextDirectoryValue, directory || project || "wlcodex");
      writeCompactText(contextUsageValue, nativeContextUsageSummary());
      writeCompactText(contextFiveHourValue, nativeLimitSummary("fiveHour"));
      writeCompactText(contextSevenDayValue, nativeLimitSummary("sevenDay"));
      writeCompactText(sessionActionTitle, title);
      renderLiveWorkspaceBar(directory, project);
    }
    function renderLiveWorkspaceBar(directory, project) {
      const workspace = directory || currentWorkspaceCwd() || "";
      const label = workspace ? lastPathComponent(workspace) : (project || "未指定");
      writeCompactText(liveWorkspaceName, label);
      liveWorkspaceChip.title = workspace || label;
      liveWorkspaceChip.disabled = false;
    }
    function openWorkspaceSwitcher() {
      const cwd = nativeSessionDirectory() || currentWorkspaceCwd();
      const target = new URL(`/native/${encodeURIComponent(PROVIDER)}`, location.origin);
      if (token) target.searchParams.set("token", token);
      if (cwd) target.searchParams.set("cwd", cwd);
      location.href = target.pathname + "?" + target.searchParams.toString();
    }
    function nativeSessionTitle() {
      const thread = nativeSessionThread();
      return String(
        currentSessionInfo.title ||
        currentSessionInfo.name ||
        currentSessionInfo.summary ||
        thread.title ||
        thread.name ||
        thread.preview ||
        "会话"
      ).trim() || "会话";
    }
    function nativeSessionProjectLabel(directory = nativeSessionDirectory()) {
      if (directory) return lastPathComponent(directory);
      const thread = nativeSessionThread();
      return String(currentSessionInfo.project || thread.project || "wlcodex");
    }
    function nativeSessionDirectory() {
      const thread = nativeSessionThread();
      const direct = firstStringFromSources([currentSessionInfo, thread], [
        "cwd",
        "workdir",
        "working_directory",
        "directory",
        "workspace_dir",
        "repo_path",
        "project_path"
      ]);
      if (direct) return direct;
      const workspace = currentSessionInfo.workspace;
      if (workspace && typeof workspace === "object") {
        const workspacePath = firstStringFromSources([workspace], ["cwd", "path", "root", "directory", "workdir"]);
        if (workspacePath) return workspacePath;
      }
      const metadata = nativeSessionMetadata();
      return firstStringFromSources([metadata], [
        "cwd",
        "workdir",
        "working_directory",
        "directory",
        "workspace",
        "workspace_dir",
        "repo_path",
        "project_path"
      ]) || currentWorkspaceCwd() || "";
    }
    function nativeContextUsageSummary() {
      const metadata = nativeSessionMetadata();
      const thread = nativeSessionThread();
      const contextUsage = firstObjectFromSources([currentSessionInfo, thread, metadata], [
        "context",
        "context_window",
        "context_window_usage",
        "contextUsage",
        "token_usage",
        "tokenUsage"
      ]) || {};
      const sources = [contextUsage, metadata, thread, currentSessionInfo];
      const remaining = firstNumberFromSources(sources, [
        "remaining_percent",
        "remaining_percentage",
        "remainingPct",
        "remaining_context_percent",
        "context_remaining_percent",
        "contextRemainingPercent"
      ]);
      const used = firstNumberFromSources(sources, [
        "used",
        "used_tokens",
        "usedTokens",
        "tokens_used",
        "context_used",
        "context_used_tokens",
        "contextUsedTokens"
      ]);
      const total = firstNumberFromSources(sources, [
        "total",
        "total_tokens",
        "totalTokens",
        "limit",
        "max_tokens",
        "context_window",
        "contextWindow",
        "context_total",
        "context_total_tokens",
        "contextTotalTokens"
      ]);
      const computedRemaining = remaining === null && used !== null && total ? Math.max(0, (total - used) / total * 100) : remaining;
      if (computedRemaining === null && used === null && total === null) return "等待同步";
      const prefix = computedRemaining === null ? "剩余 --%" : `剩余 ${formatNativePercent(computedRemaining)}`;
      if (used !== null && total !== null) {
        return `${prefix}（已用 ${formatNativeTokenCount(used)} / ${formatNativeTokenCount(total)}）`;
      }
      return `${prefix}（等待同步）`;
    }
    function nativeLimitSummary(kind) {
      const metadata = nativeSessionMetadata();
      const thread = nativeSessionThread();
      const groupKeys = kind === "fiveHour"
        ? ["five_hour", "fiveHour", "five_hour_limit", "fiveHourLimit", "five_hours", "fiveHours"]
        : ["seven_day", "sevenDay", "weekly", "week", "seven_day_limit", "sevenDayLimit"];
      const limitRoot = firstObjectFromSources([currentSessionInfo, thread, metadata], ["rate_limits", "rateLimits", "limits", "quota", "quotas"]) || {};
      const limit = firstObjectFromSources([currentSessionInfo, thread, metadata, limitRoot], groupKeys) || {};
      const remainingKeys = kind === "fiveHour"
        ? ["remaining_percent", "remaining_percentage", "remainingPct", "five_hour_remaining_percent", "fiveHourRemainingPercent", "five_hour_limit_remaining_percent"]
        : ["remaining_percent", "remaining_percentage", "remainingPct", "seven_day_remaining_percent", "sevenDayRemainingPercent", "weekly_remaining_percent"];
      const resetKeys = kind === "fiveHour"
        ? ["reset_at", "resetAt", "resets_at", "resetsAt", "reset_time", "resetTime", "five_hour_reset_at", "fiveHourResetAt"]
        : ["reset_at", "resetAt", "resets_at", "resetsAt", "reset_time", "resetTime", "seven_day_reset_at", "sevenDayResetAt", "weekly_reset_at"];
      const sources = [limit, limitRoot, metadata, thread, currentSessionInfo];
      const remaining = firstNumberFromSources(sources, remainingKeys);
      const reset = firstStringFromSources(sources, resetKeys) || firstNumberFromSources(sources, resetKeys);
      if (remaining === null && reset === null) return "等待同步";
      const prefix = remaining === null ? "剩余 --%" : `剩余 ${formatNativePercent(remaining)}`;
      if (reset === null) return `${prefix}（等待同步）`;
      return `${prefix}（将于 ${formatNativeResetTime(reset, kind)} 重置）`;
    }
    function nativeSessionMetadata() {
      const thread = nativeSessionThread();
      if (currentSessionInfo.metadata && typeof currentSessionInfo.metadata === "object") return currentSessionInfo.metadata;
      if (thread.metadata && typeof thread.metadata === "object") return thread.metadata;
      return {};
    }
    function nativeSessionThread() {
      return currentSessionInfo.thread && typeof currentSessionInfo.thread === "object" ? currentSessionInfo.thread : {};
    }
    function firstObjectFromSources(sources, keys) {
      for (const source of sources) {
        if (!source || typeof source !== "object") continue;
        for (const key of keys) {
          const value = source[key];
          if (value && typeof value === "object" && !Array.isArray(value)) return value;
        }
      }
      return null;
    }
    function firstStringFromSources(sources, keys) {
      for (const source of sources) {
        if (!source || typeof source !== "object") continue;
        for (const key of keys) {
          const value = source[key];
          if (typeof value === "string" && value.trim()) return value.trim();
        }
      }
      return "";
    }
    function firstNumberFromSources(sources, keys) {
      for (const source of sources) {
        if (!source || typeof source !== "object") continue;
        for (const key of keys) {
          const value = source[key];
          if (typeof value === "number" && Number.isFinite(value)) return value;
          if (typeof value === "string" && value.trim()) {
            const parsed = Number.parseFloat(value);
            if (Number.isFinite(parsed)) return parsed;
          }
        }
      }
      return null;
    }
    function formatNativePercent(value) {
      const normalized = value >= 0 && value <= 1 ? value * 100 : value;
      return `${Math.round(normalized)}%`;
    }
    function formatNativeTokenCount(value) {
      const rounded = Math.max(0, Math.round(value));
      if (rounded >= 10000) {
        const wan = rounded / 10000;
        return `${Number.isInteger(wan) ? wan : Math.round(wan * 10) / 10}万`;
      }
      return String(rounded);
    }
    function formatNativeResetTime(value, kind) {
      let date = null;
      if (typeof value === "number" && Number.isFinite(value)) {
        date = new Date(value > 1000000000000 ? value : value * 1000);
      } else {
        const text = String(value || "").trim();
        const parsed = new Date(text);
        if (!Number.isNaN(parsed.getTime())) date = parsed;
        else return text;
      }
      if (!date || Number.isNaN(date.getTime())) return String(value || "");
      if (kind === "fiveHour") {
        return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
      }
      return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
    }
    function lastPathComponent(path) {
      const parts = String(path || "").split("/").filter(Boolean);
      return parts.length ? parts[parts.length - 1] : "";
    }
    function writeCompactText(node, text) {
      if (!node) return;
      const value = String(text || "");
      node.textContent = value;
      node.title = value;
    }
    function closeHeaderPopovers() {
      contextInfoPopover.hidden = true;
      sessionActionMenu.hidden = true;
    }
    function toggleContextInfoPopover() {
      const willOpen = contextInfoPopover.hidden;
      closeComposerActionMenu();
      modelPopover.classList.add("closed");
      permissionPopover.classList.add("closed");
      handoffPanel.hidden = true;
      closeHeaderPopovers();
      if (willOpen) {
        updateNativeHeaderContext();
        contextInfoPopover.hidden = false;
      }
    }
    function toggleSessionActionMenu() {
      const willOpen = sessionActionMenu.hidden;
      closeComposerActionMenu();
      modelPopover.classList.add("closed");
      permissionPopover.classList.add("closed");
      handoffPanel.hidden = true;
      closeHeaderPopovers();
      if (willOpen) {
        updateNativeHeaderContext();
        sessionActionMenu.hidden = false;
      }
    }
    async function copyNativeSessionId() {
      closeHeaderPopovers();
      if (!nativeThreadId) {
        setSendStatus("没有会话 ID", "error");
        return;
      }
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(nativeThreadId);
        } else {
          fallbackCopyText(nativeThreadId);
        }
        setSendStatus("已复制会话 ID", "ok");
      } catch (_error) {
        try {
          fallbackCopyText(nativeThreadId);
          setSendStatus("已复制会话 ID", "ok");
        } catch (_fallbackError) {
          setSendStatus("复制失败", "error");
        }
      }
    }
    function unavailableSessionAction(label) {
      closeHeaderPopovers();
      setSendStatus(label + "暂未接入", "error");
    }
    function workflowApi(path, body) {
      return api(path, {
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
        updateSettingVisibility();
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
    function renderPermissionSettings() {
      permissionSelector.innerHTML = "";
      for (const preset of PERMISSION_PRESETS) {
        const option = document.createElement("option");
        option.value = preset.value;
        option.textContent = preset.label;
        option.dataset.description = preset.description || "";
        permissionSelector.append(option);
      }
      if (optionValueExists(permissionSelector, savedPermissionSettings.permission_mode)) {
        permissionSelector.value = savedPermissionSettings.permission_mode;
      }
      renderSettingOptions(permissionOptions, permissionSelector, updatePermissionSummary);
      updatePermissionSummary();
      savedPermissionSettings = readSelectedPermissionSettings();
      permissionSettingsDirty = false;
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
        preferredReasoningEffortDefault(model, efforts),
        "推理"
      );
      fillServiceTierSelector(tiers, preferredServiceTierDefault(model, tiers));
      if (
        shouldApplyPreferredEffort(preferredSettings, model)
        && optionValueExists(reasoningSelector, preferredSettings.effort)
      ) {
        reasoningSelector.value = preferredSettings.effort;
      }
      if (Object.prototype.hasOwnProperty.call(preferredSettings, "service_tier")
        && optionValueExists(serviceTierSelector, preferredSettings.service_tier)) {
        serviceTierSelector.value = preferredSettings.service_tier || "";
      }
      renderSettingOptions(reasoningOptions, reasoningSelector, updateSettingSummary);
      renderSettingOptions(serviceTierOptions, serviceTierSelector, updateSettingSummary, {includeEmpty: true});
      updateSettingVisibility();
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
        const description = option.dataset.description || "";
        if (description) {
          const copy = document.createElement("span");
          copy.className = "setting-option-copy";
          const title = document.createElement("span");
          title.className = "setting-option-title";
          title.textContent = option.textContent || option.value;
          const desc = document.createElement("span");
          desc.className = "setting-option-desc";
          desc.textContent = description;
          copy.append(title, desc);
          button.append(copy);
        } else {
          button.textContent = option.textContent || option.value;
        }
        button.disabled = select.disabled;
        if (option.selected) {
          const check = document.createElement("span");
          check.className = "setting-option-check";
          check.innerHTML = ICONS.check;
          button.append(check);
        }
        button.onclick = () => {
          const previousValue = select.value;
          select.value = option.value;
          if (select.value !== previousValue) {
            if (select === permissionSelector) markPermissionSettingsDirty();
            else markModelSettingsDirty();
          }
          syncSettingOptionsSelection(container, select);
          container.hidden = true;
          if (onChoose) onChoose();
          if (select === permissionSelector) {
            savePermissionSettingsIfChanged();
            permissionPopover.classList.add("closed");
          }
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
          check.innerHTML = ICONS.check;
          button.append(check);
        } else if (!selected && existingCheck) {
          existingCheck.remove();
        }
      }
    }
    function syncSettingOptionsDisabled() {
      for (const [container, select] of [
        [modelOptions, modelSelector],
        [permissionOptions, permissionSelector],
        [serviceTierOptions, serviceTierSelector],
        [reasoningOptions, reasoningSelector],
      ]) {
        for (const button of Array.from(container.querySelectorAll(".setting-option"))) {
          button.disabled = select.disabled;
        }
      }
    }
    function toggleSettingOptions(container) {
      for (const node of [modelOptions, permissionOptions, serviceTierOptions, reasoningOptions]) {
        if (node !== container) node.hidden = true;
      }
      container.hidden = !container.hidden;
    }
    function updateSettingVisibility() {
      reasoningSettingRow.hidden = reasoningSelector.options.length <= 1;
      serviceTierSettingRow.hidden = serviceTierSelector.options.length <= 1;
      if (reasoningSettingRow.hidden) reasoningOptions.hidden = true;
      if (serviceTierSettingRow.hidden) serviceTierOptions.hidden = true;
    }
    function preferredServiceTierDefault(model, tiers) {
      const defaultValue = String((model && model.defaultServiceTier) || "").toLowerCase();
      if (!defaultValue || ["fast", "priority"].includes(defaultValue)) return "";
      const match = tiers.find(tier => {
        return String(tier.id || tier.serviceTier || tier.name || "").toLowerCase() === defaultValue;
      });
      return match ? match.id || match.serviceTier || match.name || "" : "";
    }
    function preferredReasoningEffortDefault(model, efforts) {
      return highestReasoningEffort(efforts) || String((model && model.defaultReasoningEffort) || "");
    }
    function highestReasoningEffort(efforts) {
      const ranked = (Array.isArray(efforts) ? efforts : [])
        .map(item => String((item && (item.reasoningEffort || item.id)) || item || "").trim())
        .filter(Boolean)
        .sort((left, right) => reasoningEffortRank(right) - reasoningEffortRank(left));
      return ranked[0] || "";
    }
    function reasoningEffortRank(value) {
      const key = String(value || "").trim().toLowerCase();
      if (key === "max" || key === "maximum") return 6;
      if (key === "xhigh" || key === "extra_high") return 5;
      if (key === "high") return 4;
      if (key === "medium" || key === "normal" || key === "default") return 3;
      if (key === "low") return 2;
      if (key === "minimal") return 1;
      if (key === "none") return 0;
      return -1;
    }
    function shouldApplyPreferredEffort(preferredSettings, model) {
      const effort = String((preferredSettings && preferredSettings.effort) || "");
      if (!effort) return false;
      const defaultEffort = preferredReasoningEffortDefault(model, Array.isArray(model && model.supportedReasoningEfforts) ? model.supportedReasoningEfforts : []);
      const catalogDefault = String((model && model.defaultReasoningEffort) || "");
      if ((preferredSettings.version || 0) < MODEL_SETTINGS_STORAGE_VERSION
        && catalogDefault
        && effort === catalogDefault
        && effort !== defaultEffort) {
        return false;
      }
      return true;
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
        effort: reasoningSettingRow.hidden ? "" : reasoningSelector.value,
        service_tier: serviceTierSettingRow.hidden ? "" : serviceTierSelector.value,
        version: MODEL_SETTINGS_STORAGE_VERSION
      });
    }
    function normalizeModelSettings(settings = {}) {
      return {
        model: typeof settings.model === "string" ? settings.model : "",
        effort: typeof settings.effort === "string" ? settings.effort : "",
        service_tier: typeof settings.service_tier === "string" ? settings.service_tier : "",
        version: Number(settings.version || 0)
      };
    }
    function optionValueExists(select, value) {
      const normalized = String(value || "");
      return Array.from(select.options || []).some(option => option.value === normalized);
    }
    function modelSettingsEqual(left, right) {
      return left.model === right.model
        && left.effort === right.effort
        && left.service_tier === right.service_tier
        && left.version === right.version;
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
    function loadSavedPermissionSettings() {
      try {
        return normalizePermissionSettings(JSON.parse(localStorage.getItem(PERMISSION_SETTINGS_STORAGE_KEY) || "{}"));
      } catch (_error) {
        return normalizePermissionSettings({});
      }
    }
    function readSelectedPermissionSettings() {
      return normalizePermissionSettings({
        permission_mode: permissionSelector.value,
        version: PERMISSION_SETTINGS_STORAGE_VERSION
      });
    }
    function normalizePermissionSettings(settings = {}) {
      const storedVersion = Number(settings.version || 0);
      const mode = typeof settings.permission_mode === "string" ? settings.permission_mode : DEFAULT_PERMISSION_MODE;
      const known = PERMISSION_PRESETS.some(preset => preset.value === mode) ? mode : DEFAULT_PERMISSION_MODE;
      const migrated = storedVersion < PERMISSION_SETTINGS_STORAGE_VERSION && known === "default"
        ? DEFAULT_PERMISSION_MODE
        : known;
      return {
        permission_mode: migrated,
        version: storedVersion
      };
    }
    function permissionSettingsEqual(left, right) {
      return left.permission_mode === right.permission_mode && left.version === right.version;
    }
    function savePermissionSettingsIfChanged() {
      if (!permissionSettingsDirty) return;
      const nextSettings = readSelectedPermissionSettings();
      const changed = !permissionSettingsEqual(savedPermissionSettings, nextSettings);
      savedPermissionSettings = nextSettings;
      if (changed) {
        try {
          localStorage.setItem(PERMISSION_SETTINGS_STORAGE_KEY, JSON.stringify(savedPermissionSettings));
        } catch (_error) {}
      }
      permissionSettingsDirty = false;
    }
    function markPermissionSettingsDirty() {
      permissionSettingsDirty = true;
    }
    function loadSavedCollaborationMode() {
      if (!SUPPORTS_PLAN_MODE) return "default";
      try {
        const stored = String(localStorage.getItem(COLLABORATION_MODE_STORAGE_KEY) || "default").toLowerCase();
        return stored === "plan" ? "plan" : "default";
      } catch (_error) {
        return "default";
      }
    }
    function saveSelectedCollaborationMode() {
      try { localStorage.setItem(COLLABORATION_MODE_STORAGE_KEY, selectedCollaborationMode); } catch (_error) {}
    }
    function readSelectedCollaborationMode() {
      if (!SUPPORTS_PLAN_MODE) return null;
      if (USES_CLAUDE_PLAN_PERMISSION_MODE) return null;
      const settings = readSelectedModelSettings();
      return {"mode": selectedCollaborationMode === "plan" ? "plan" : "default", "settings": {"model": settings.model}};
    }
    function explicitDefaultCollaborationMode() {
      if (!SUPPORTS_PLAN_MODE) return null;
      if (USES_CLAUDE_PLAN_PERMISSION_MODE) return null;
      const settings = readSelectedModelSettings();
      return {"mode": "default", "settings": {"model": settings.model}};
    }
    function setSelectedCollaborationMode(mode) {
      selectedCollaborationMode = SUPPORTS_PLAN_MODE && mode === "plan" ? "plan" : "default";
      saveSelectedCollaborationMode();
      updateCollaborationMenu();
      updateComposerDisabled();
      updateHandoffControls();
    }
    function clearSelectedPlanModeForExecution() {
      if (selectedCollaborationMode !== "plan") return;
      setSelectedCollaborationMode("default");
    }
    function updateCollaborationMenu() {
      pluginMenuSection.hidden = !SUPPORTS_PLUGIN_MENU;
      pluginList.hidden = !SUPPORTS_PLUGIN_MENU;
      if (!SUPPORTS_PLAN_MODE) {
        menuPlanMode.hidden = true;
        planModeCheck.innerHTML = "";
        planModeChip.hidden = true;
        return;
      }
      menuPlanMode.hidden = false;
      const enabled = selectedCollaborationMode === "plan";
      menuPlanMode.classList.toggle("selected", enabled);
      menuPlanMode.setAttribute("aria-pressed", enabled ? "true" : "false");
      planModeCheck.innerHTML = enabled ? ICONS.check : "";
      planModeChip.hidden = !enabled;
    }
    function pluginKey(item) {
      return String((item && (item.id || item.name)) || "").trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    }
    function pluginMention(item) {
      const key = pluginKey(item);
      return key ? "@" + key : "";
    }
    function createPluginIcon(item, sizeClass = "") {
      const dot = document.createElement("span");
      dot.className = sizeClass ? "plugin-dot " + sizeClass : "plugin-dot";
      if (item && item.brand_color) dot.style.background = item.brand_color;
      if (item && item.icon) {
        const image = document.createElement("img");
        image.src = item.icon;
        image.alt = "";
        dot.append(image);
      } else {
        dot.textContent = String((item && item.name) || "?").trim().slice(0, 1).toUpperCase() || "?";
      }
      return dot;
    }
    function availablePluginItems() {
      return SUPPORTS_PLUGIN_MENU && Array.isArray(PLUGIN_MENU_ITEMS) ? PLUGIN_MENU_ITEMS : [];
    }
    function renderSelectedPlugins() {
      selectedPluginStrip.innerHTML = "";
      selectedPluginStrip.hidden = !selectedPlugins.length;
      for (const item of selectedPlugins) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "selected-plugin-chip";
        chip.title = item.description || item.name || "Plugin";
        chip.append(createPluginIcon(item));
        const label = document.createElement("span");
        label.className = "selected-plugin-name";
        label.textContent = item.name || "Plugin";
        chip.append(label);
        chip.onclick = () => {
          selectedPlugins = selectedPlugins.filter(plugin => pluginKey(plugin) !== pluginKey(item));
          renderSelectedPlugins();
          updateComposerDisabled();
        };
        selectedPluginStrip.append(chip);
      }
    }
    function currentPluginQuery() {
      if (!SUPPORTS_PLUGIN_MENU) return null;
      const cursor = Number.isFinite(promptInput.selectionStart) ? promptInput.selectionStart : promptInput.value.length;
      const before = promptInput.value.slice(0, cursor);
      const match = before.match(/(?:^|\\s)@([a-zA-Z0-9_-]*)$/);
      return match ? match[1].toLowerCase() : null;
    }
    function pluginAutocompleteMatches(query) {
      if (query === null) return [];
      const needle = String(query || "").toLowerCase();
      return availablePluginItems()
        .filter(item => {
          const name = String(item.name || "").toLowerCase();
          const key = pluginKey(item);
          return !needle || name.includes(needle) || key.includes(needle);
        })
        .slice(0, 4);
    }
    function promptHasPluginMention(value, mention) {
      return String(value || "").toLowerCase().split(/\\s+/).includes(String(mention || "").toLowerCase());
    }
    function replacePromptPluginQuery(item) {
      const mention = pluginMention(item);
      if (!mention) return;
      const value = promptInput.value;
      const cursor = Number.isFinite(promptInput.selectionStart) ? promptInput.selectionStart : value.length;
      const before = value.slice(0, cursor);
      const match = before.match(/(^|\\s)@([a-zA-Z0-9_-]*)$/);
      if (match) {
        const prefix = match[1] || "";
        const start = cursor - match[0].length;
        const nextCursor = start + prefix.length + mention.length + 1;
        promptInput.value = value.slice(0, start) + prefix + mention + " " + value.slice(cursor);
        promptInput.setSelectionRange(nextCursor, nextCursor);
        return;
      }
      if (promptHasPluginMention(value, mention)) return;
      const separator = value && !value.endsWith(" ") ? " " : "";
      promptInput.value = value + separator + mention + " ";
      const nextCursor = promptInput.value.length;
      promptInput.setSelectionRange(nextCursor, nextCursor);
    }
    function selectComposerPlugin(item) {
      if (!item || !pluginKey(item)) return;
      if (!selectedPlugins.some(plugin => pluginKey(plugin) === pluginKey(item))) {
        selectedPlugins.push(item);
      }
      replacePromptPluginQuery(item);
      renderSelectedPlugins();
      updatePluginAutocomplete();
      closeComposerActionMenu();
      updateComposerDisabled();
      promptInput.focus({preventScroll: true});
    }
    function updatePluginAutocomplete() {
      const matches = pluginAutocompleteMatches(currentPluginQuery());
      pluginAutocomplete.innerHTML = "";
      pluginAutocomplete.hidden = !matches.length;
      for (const item of matches) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "plugin-suggestion";
        row.append(createPluginIcon(item));
        const copy = document.createElement("span");
        const title = document.createElement("span");
        title.className = "plugin-suggestion-title";
        title.textContent = pluginMention(item);
        const desc = document.createElement("span");
        desc.className = "plugin-suggestion-desc";
        desc.textContent = `${item.name || "Plugin"} · ${item.description || "本机插件"}`;
        copy.append(title, desc);
        row.append(copy);
        row.onclick = () => selectComposerPlugin(item);
        pluginAutocomplete.append(row);
      }
    }
    function resetComposerPlugins() {
      selectedPlugins = [];
      renderSelectedPlugins();
      updatePluginAutocomplete();
    }
    function renderPluginList() {
      if (!SUPPORTS_PLUGIN_MENU) return;
      pluginList.innerHTML = "";
      const items = availablePluginItems();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "composer-menu-item";
        empty.innerHTML = `<span class="plugin-dot">+</span><span><span class="composer-menu-title">未检测到插件</span><span class="composer-menu-desc">安装后会显示在这里</span></span><span></span>`;
        pluginList.append(empty);
        return;
      }
      for (const item of items) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "composer-menu-item";
        row.onclick = () => selectComposerPlugin(item);
        const dot = createPluginIcon(item);
        const copy = document.createElement("span");
        const title = document.createElement("span");
        title.className = "composer-menu-title";
        title.textContent = item.name || "Plugin";
        const desc = document.createElement("span");
        desc.className = "composer-menu-desc";
        desc.textContent = item.description || "本机插件";
        copy.append(title, desc);
        row.append(dot, copy, document.createElement("span"));
        pluginList.append(row);
      }
    }
    function closeComposerActionMenu() {
      composerActionMenu.classList.add("closed");
    }
    function toggleComposerActionMenu() {
      const willClose = !composerActionMenu.classList.contains("closed");
      composerActionMenu.classList.toggle("closed", willClose);
      if (!willClose) {
        closeHeaderPopovers();
        modelPopover.classList.add("closed");
        permissionPopover.classList.add("closed");
        handoffPanel.hidden = true;
        modelOptions.hidden = true;
        serviceTierOptions.hidden = true;
        reasoningOptions.hidden = true;
        permissionOptions.hidden = true;
      }
    }
    function updatePermissionSummary() {
      const label = selectedOptionText(permissionSelector, "默认权限");
      permissionSettingsButton.textContent = label;
      permissionSettingsButton.classList.toggle("modified", permissionSelector.value !== "default");
      syncSettingOptionsSelection(permissionOptions, permissionSelector);
    }
    async function resolveApproval(requestId, action, card) {
      if (!requestId) return;
      const alreadyResolved = resolvedApprovalEventFor(requestId);
      if (alreadyResolved) {
        setApprovalState(card, approvalResolvedAction(alreadyResolved), "resolved");
        return;
      }
      setApprovalState(card, action, "pending");
      try {
        await api(`${API_BASE}/approvals/${encodeURIComponent(requestId)}/resolve`, {
          method: "POST",
          body: JSON.stringify({action})
        });
        setApprovalState(card, action, "resolved");
        await pollEvents();
      } catch (error) {
        await pollEvents();
        const resolved = resolvedApprovalEventFor(requestId);
        if (resolved) {
          setApprovalState(card, approvalResolvedAction(resolved), "resolved");
          return;
        }
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
      if (payload.scope === "session") return "approve_session";
      if (payload.decision === "acceptForSession" || payload.decision === "approved_for_session") return "approve_session";
      if (payload.decision === "decline" || payload.decision === "denied") return "deny";
      if (payload.decision === "cancel" || payload.decision === "abort") return "cancel";
      if (response.action) return String(response.action);
      if (response.scope === "session") return "approve_session";
      if (response.decision === "acceptForSession" || response.decision === "approved_for_session") return "approve_session";
      if (response.decision === "decline" || response.decision === "denied") return "deny";
      if (response.decision === "cancel" || response.decision === "abort") return "cancel";
      return "approve_once";
    }
    function resolvedApprovalEventFor(requestId) {
      const key = String(requestId || "");
      if (!key) return null;
      for (let index = loadedEvents.length - 1; index >= 0; index--) {
        const event = loadedEvents[index];
        if (!event || event.kind !== "approval_resolved") continue;
        if (approvalRequestKey(event) === key) return event;
      }
      return null;
    }
    function approvalActionLabel(action) {
      if (action === "approve_once") return "批准一次";
      if (action === "approve_session") return "本会话批准";
      if (action === "deny") return "拒绝";
      if (action === "cancel") return "取消";
      return "审批";
    }
    headerContextButton.onclick = toggleContextInfoPopover;
    headerSessionMenuButton.onclick = toggleSessionActionMenu;
    contextInfoClose.onclick = closeHeaderPopovers;
    contextThreadCopyButton.onclick = copyNativeSessionId;
    pinSessionButton.onclick = () => unavailableSessionAction("置顶");
    copySessionIdButton.onclick = copyNativeSessionId;
    renameSessionButton.onclick = () => unavailableSessionAction("重命名");
    archiveSessionButton.onclick = () => unavailableSessionAction("归档");
    continueButton.onclick = () => submitPrompt();
    steerChoice.onclick = () => submitPrompt("steer");
    queueChoice.onclick = () => submitPrompt("continue");
    steerButton.onclick = () => submitPrompt("steer");
    interruptButton.onclick = interruptNativeTurn;
    handoffButton.onclick = toggleHandoffPanel;
    handoffPreviewButton.onclick = previewHandoff;
    handoffExecuteButton.onclick = executeHandoff;
    handoffCopyButton.onclick = copyHandoffPrompt;
    planPageClose.onclick = closePlanPage;
    planPageDownload.onclick = () => {
      if (!activePlan) return;
      downloadPlanText(activePlan.title, activePlan.body);
    };
    planPageCopy.onclick = () => {
      if (!activePlan) return;
      copyPromptCardText(planPageCopy, activePlan.body);
    };
    planPageExecute.onclick = executeActivePlan;
    handoffIntent.onchange = resetHandoffPreview;
    handoffNote.oninput = resetHandoffPreview;
    for (const button of handoffTargetButtons) {
      button.onclick = () => selectHandoffTarget(button.dataset.provider || "");
    }
    modelSettingsButton.onclick = () => {
      const willClose = !modelPopover.classList.contains("closed");
      if (willClose) saveModelSettingsIfChanged();
      modelPopover.classList.toggle("closed", willClose);
      if (!willClose) closeHeaderPopovers();
      if (!willClose) permissionPopover.classList.add("closed");
      if (!willClose) handoffPanel.hidden = true;
      if (!willClose) closeComposerActionMenu();
      if (willClose) {
        modelOptions.hidden = true;
        serviceTierOptions.hidden = true;
        reasoningOptions.hidden = true;
      }
    };
    permissionSettingsButton.onclick = () => {
      const willClose = !permissionPopover.classList.contains("closed");
      if (willClose) savePermissionSettingsIfChanged();
      permissionPopover.classList.toggle("closed", willClose);
      if (!willClose) {
        closeHeaderPopovers();
        modelPopover.classList.add("closed");
        handoffPanel.hidden = true;
        closeComposerActionMenu();
        permissionOptions.hidden = false;
      } else {
        permissionOptions.hidden = true;
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
    permissionSelector.onchange = () => {
      renderSettingOptions(permissionOptions, permissionSelector, updatePermissionSummary);
      updatePermissionSummary();
      markPermissionSettingsDirty();
    };
    modelSettingRow.onclick = event => {
      if (event.target === modelSelector) return;
      toggleSettingOptions(modelOptions);
    };
    serviceTierSettingRow.onclick = event => {
      if (event.target === serviceTierSelector) return;
      toggleSettingOptions(serviceTierOptions);
    };
    reasoningSettingRow.onclick = event => {
      if (event.target === reasoningSelector) return;
      toggleSettingOptions(reasoningOptions);
    };
    attachmentButton.onclick = toggleComposerActionMenu;
    menuUploadPhoto.onclick = () => {
      closeComposerActionMenu();
      imageInput.click();
    };
    menuPlanMode.onclick = () => {
      if (!SUPPORTS_PLAN_MODE) return;
      setSelectedCollaborationMode(selectedCollaborationMode === "plan" ? "default" : "plan");
      closeComposerActionMenu();
    };
    planModeChipCancel.onclick = () => setSelectedCollaborationMode("default");
    liveWorkspaceChip.onclick = openWorkspaceSwitcher;
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
    promptInput.addEventListener("input", () => {
      updatePluginAutocomplete();
      updateComposerDisabled();
    });
    document.addEventListener("click", event => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("#headerRunIndicator") || target.closest(".native-header-popover") || target.closest(".context-info-sheet")) return;
      closeHeaderPopovers();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") closeHeaderPopovers();
    });
    document.getElementById("back").onclick = () => {
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      params.set("t", String(Date.now()));
      const urlTheme = new URLSearchParams(location.search).get("theme");
      if (urlTheme) params.set("theme", urlTheme);
      location.href = `/native/${encodeURIComponent(PROVIDER)}?${params.toString()}`;
    };
    async function submitPrompt(action = primaryComposerAction()) {
      if (sendingPrompt) return;
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
      const attachmentsSnapshot = imageAttachments.map(image => ({...image}));
      const body = buildNativePromptBody(prompt, {includeCollaborationMode: true});
      const composerSnapshot = {
        prompt,
        imageAttachments: attachmentsSnapshot
      };
      if (attachmentsSnapshot.length) {
        body.images = attachmentsSnapshot.map(image => ({
          url: image.url,
          filename: image.filename,
          mime_type: image.mime_type
        }));
      }
      if (action === "steer") body.expected_turn_id = activeTurnId;
      if (action === "continue") body.force_new_turn = true;
      sendingPrompt = true;
      pendingUserEcho = {
        text: normalizeTranscriptText(prompt),
        images: attachmentsSnapshot.length
      };
      closeInterruptionChoice();
      const draftTurnId = activeTurnId || nativeTurnId || "";
      if (action !== "steer") renderLocalUserEcho(prompt, attachmentsSnapshot, draftTurnId);
      clearComposerDraft();
      updateComposerDisabled();
      setSendStatus(action === "steer" ? "修正中" : "发送中", "");
      continueButton.classList.add("loading");
      const controlSnapshot = snapshotNativeTurnControl();
      try {
        const result = await nativeControl(action, body);
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.active_turn_id || (result.turn_running ? result.turn_id || "" : "");
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        updateNativeHeaderContext();
        pendingUserEcho = null;
        setSendStatus("已发送", "ok");
        await pollEvents();
      } catch (error) {
        if (await recoverNativeControlAfterFetchFailure(error, controlSnapshot)) {
          pendingUserEcho = null;
          clearComposerDraft();
          setSendStatus("已发送", "ok");
          return;
        }
        clearComposerDraftToSnapshot(composerSnapshot);
        await pollEvents();
        renderStatus(action + "_failed", error.message || String(error));
        setSendStatus(error.message || "发送失败", "error");
      } finally {
        sendingPrompt = false;
        pendingUserEcho = null;
        continueButton.classList.remove("loading");
        updateComposerDisabled();
      }
    }
    function buildNativePromptBody(prompt, options = {}) {
      saveModelSettingsIfChanged();
      savePermissionSettingsIfChanged();
      const permissionSettings = readSelectedPermissionSettings();
      const body = {prompt};
      if (savedModelSettings.model) body.model = savedModelSettings.model;
      if (savedModelSettings.effort) body.effort = savedModelSettings.effort;
      if (savedModelSettings.service_tier) body.service_tier = savedModelSettings.service_tier;
      let permissionMode = permissionSettings.permission_mode;
      if (USES_CLAUDE_PLAN_PERMISSION_MODE && selectedCollaborationMode === "plan") {
        permissionMode = "plan";
      }
      body.permission_mode = permissionMode;
      if (options.collaborationMode) {
        body.collaboration_mode = options.collaborationMode;
      } else if (options.includeCollaborationMode) {
        const collaborationMode = readSelectedCollaborationMode();
        if (collaborationMode) {
          body.collaboration_mode = collaborationMode;
        }
      }
      return body;
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
        updateNativeHeaderContext();
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
      if (!nativeTurnRunning) return "continue";
      if (canSteerActiveTurn() && composerHasDraft()) return "choose";
      if (canInterruptActiveTurn() && !composerHasDraft()) return "interrupt";
      return "wait";
    }
    function applyNativeTurnState(event, options = {}) {
      const payload = event.payload || {};
      const mirroredTranscript = isMirroredTranscriptEvent(event);
      if (options.historical) return;
      if (!mirroredTranscript && payload.native_turn_id) nativeTurnId = payload.native_turn_id;
      if (isTerminalTurnEvent(event)) {
        if (!payload.native_turn_id || payload.native_turn_id === activeTurnId || payload.native_turn_id === nativeTurnId) activeTurnId = "";
        nativeTurnRunning = false;
      } else if (mirroredTranscript) {
        return;
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
      updateNativeHeaderContext();
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
      if (event.kind === "message_completed" && payload.native_turn_id) return true;
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
      composerActivity.classList.toggle("active", Boolean(active));
    }
    function toggleHandoffPanel() {
      if (handoffPanel.hidden && !handoffTargetProvider) {
        selectHandoffTarget(preferredHandoffTarget());
      }
      modelPopover.classList.add("closed");
      permissionPopover.classList.add("closed");
      modelOptions.hidden = true;
      permissionOptions.hidden = true;
      serviceTierOptions.hidden = true;
      reasoningOptions.hidden = true;
      closeHeaderPopovers();
      closeComposerActionMenu();
      handoffPanel.hidden = !handoffPanel.hidden;
      updateHandoffControls();
    }
    function preferredHandoffTarget() {
      const providers = ["codex", "claude", "antigravity"];
      return providers.find(provider => provider !== PROVIDER) || "codex";
    }
    function selectHandoffTarget(provider) {
      handoffTargetProvider = provider || "codex";
      for (const button of handoffTargetButtons) {
        button.classList.toggle("selected", button.dataset.provider === handoffTargetProvider);
      }
      resetHandoffPreview();
    }
    function resetHandoffPreview() {
      handoffPreviewPayload = null;
      handoffExecuteButton.disabled = true;
      handoffPreviewEl.hidden = true;
      handoffPreviewEl.className = "handoff-preview";
      handoffPreviewStatus.textContent = "";
      handoffPromptBody.innerHTML = "";
      handoffPromptBody.hidden = true;
      handoffCopyButton.disabled = true;
      setHandoffCopyState("");
    }
    function setActivePlanFromEvent(event, planText, titleText, summaryText) {
      const payload = (event && event.payload) || {};
      activePlan = {
        threadId: String(payload.threadId || payload.native_thread_id || nativeThreadId || ""),
        turnId: String(payload.turnId || payload.native_turn_id || ""),
        title: String(titleText || "计划"),
        summary: String(summaryText || ""),
        body: String(planText || ""),
        status: String(payload.status || ""),
        source: "native_plan_event",
        executable: true
      };
      updatePlanActionState();
      return activePlan;
    }
    function openPlanPage(plan = activePlan) {
      if (!plan || !String(plan.body || "").trim()) return;
      activePlan = plan;
      renderPlanPage(plan);
      planPage.hidden = false;
      document.body.style.overflow = "hidden";
      updatePlanActionState();
    }
    function renderPlanPage(plan) {
      planPageTitle.textContent = plan.title || "计划";
      planPageSummary.replaceChildren();
      if (plan.summary) renderMarkdownLite(planPageSummary, plan.summary);
      planPageBody.replaceChildren();
      renderMarkdownLite(planPageBody, planDetailTextFromText(plan.body, plan.summary));
      planPageExecute.textContent = plan.executable ? "执行计划" : "仅文本计划";
      planPageExecute.title = plan.executable ? "" : "仅文本计划，不能一键执行";
    }
    function planDetailTextFromText(text, summaryText) {
      let source = String(text || "").trim();
      source = source.replace(/^#\\s+[^\\n]+\\n+/, "").trimStart();
      source = source.replace(/^##\\s+Summary\\s*\\n+/i, "").trimStart();
      const summary = String(summaryText || "").trim();
      if (summary && source.startsWith(summary)) {
        source = source.slice(summary.length).trimStart();
      }
      return source || String(text || "").trim();
    }
    function closePlanPage() {
      planPage.hidden = true;
      document.body.style.overflow = "";
    }
    function updatePlanActionState() {
      const disabled = !activePlan || !activePlan.executable || !nativeThreadId || sendingPrompt || nativeTurnRunning;
      planPageExecute.disabled = disabled;
      document.querySelectorAll(".plan-card-execute").forEach(button => {
        button.disabled = disabled;
      });
    }
    function planExecutionPrompt(planText) {
      return "PLEASE IMPLEMENT THIS PLAN:\\n\\n" + String(planText || "").trim();
    }
    async function executeActivePlan() {
      if (!activePlan || !String(activePlan.body || "").trim()) {
        setSendStatus("没有可执行计划", "error");
        return;
      }
      if (!activePlan.executable) {
        setSendStatus("仅文本计划，不能一键执行", "error");
        return;
      }
      if (nativeTurnRunning) {
        setSendStatus("等待当前轮结束", "error");
        return;
      }
      const prompt = planExecutionPrompt(activePlan.body);
      clearSelectedPlanModeForExecution();
      const body = buildNativePromptBody(prompt, {collaborationMode: explicitDefaultCollaborationMode()});
      body.force_new_turn = true;
      renderLocalUserEcho(prompt, []);
      closePlanPage();
      sendingPrompt = true;
      updateComposerDisabled();
      setSendStatus("执行计划", "");
      continueButton.classList.add("loading");
      try {
        const result = await nativeControl("continue", body);
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.active_turn_id || (result.turn_running ? result.turn_id || "" : "");
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        setSendStatus("已发送", "ok");
        await pollEvents();
      } catch (error) {
        await pollEvents();
        renderStatus("execute_plan_failed", error.message || String(error));
        setSendStatus(error.message || "执行计划失败", "error");
      } finally {
        sendingPrompt = false;
        continueButton.classList.remove("loading");
        updateComposerDisabled();
      }
    }
    async function previewHandoff() {
      if (!nativeThreadId) {
        setHandoffStatus("会话未连接", "error");
        return;
      }
      if (!handoffTargetProvider) selectHandoffTarget(preferredHandoffTarget());
      const cwd = currentWorkspaceCwd();
      handoffBusy = true;
      updateHandoffControls();
      setHandoffStatus("预览中", "");
      try {
        handoffPreviewPayload = await workflowApi("/api/native/workflows/handoffs/preview", {
          source_provider: PROVIDER,
          source_thread_id: nativeThreadId,
          source_turn_id: activeTurnId || nativeTurnId,
          target_provider: handoffTargetProvider,
          cwd,
          intent: handoffIntent.value,
          user_note: handoffNote.value
        });
        renderHandoffPreview(handoffPreviewPayload);
      } catch (error) {
        handoffPreviewPayload = null;
        setHandoffStatus(error.message || "接棒预览失败", "error");
      } finally {
        handoffBusy = false;
        updateHandoffControls();
      }
    }
    async function executeHandoff() {
      if (!handoffPreviewPayload) await previewHandoff();
      if (!handoffPreviewPayload) return;
      handoffBusy = true;
      updateHandoffControls();
      setHandoffStatus("启动中", "");
      try {
        const result = await workflowApi("/api/native/workflows/handoffs/execute", {
          workflow_run_id: handoffPreviewPayload.workflow_run_id,
          preview_id: handoffPreviewPayload.preview_id,
          target_provider: handoffTargetProvider,
          cwd: handoffPreviewPayload.cwd || currentWorkspaceCwd(),
          prompt: handoffPreviewPayload.prompt || ""
        });
        if (result.target_url) {
          if (token) location.href = handoffUrlWithToken(result.target_url);
          else location.href = result.target_url;
        }
      } catch (error) {
        setHandoffStatus(error.message || "接棒执行失败", "error");
      } finally {
        handoffBusy = false;
        updateHandoffControls();
      }
    }
    function renderHandoffPreview(preview) {
      const warnings = Array.isArray(preview.warnings) && preview.warnings.length
        ? "\\n" + preview.warnings.join("\\n")
        : "";
      handoffPreviewEl.hidden = false;
      handoffPreviewEl.className = "handoff-preview ok";
      handoffPreviewStatus.textContent = `${preview.intent || "auto"}${warnings}`;
      handoffPromptBody.hidden = false;
      handoffPromptBody.innerHTML = escapeHtml(preview.prompt || "");
      handoffCopyButton.disabled = !String(preview.prompt || "").trim();
      setHandoffCopyState("");
      handoffExecuteButton.disabled = false;
    }
    function setHandoffStatus(text, tone) {
      handoffPreviewEl.hidden = !text;
      handoffPreviewEl.className = "handoff-preview" + (tone ? " " + tone : "");
      handoffPreviewStatus.textContent = text || "";
      handoffPromptBody.innerHTML = "";
      handoffPromptBody.hidden = true;
      handoffCopyButton.disabled = true;
      setHandoffCopyState("");
    }
    async function copyHandoffPrompt() {
      const text = String((handoffPreviewPayload && handoffPreviewPayload.prompt) || handoffPromptBody.textContent || "");
      if (!text.trim()) return;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          fallbackCopyText(text);
        }
        setHandoffCopyState("copied");
      } catch (_error) {
        try {
          fallbackCopyText(text);
          setHandoffCopyState("copied");
        } catch (_fallbackError) {
          setHandoffCopyState("failed");
        }
      }
    }
    function fallbackCopyText(text) {
      const node = document.createElement("textarea");
      node.value = text;
      node.setAttribute("readonly", "");
      node.style.position = "fixed";
      node.style.left = "-9999px";
      document.body.append(node);
      node.select();
      document.execCommand("copy");
      node.remove();
    }
    function setHandoffCopyState(state) {
      if (handoffCopyStateTimer) window.clearTimeout(handoffCopyStateTimer);
      const copied = state === "copied";
      const failed = state === "failed";
      handoffCopyButton.classList.toggle("copied", copied);
      handoffCopyButton.setAttribute(
        "aria-label",
        copied ? "已复制提示词" : failed ? "复制失败" : "复制提示词"
      );
      if (copied || failed) {
        handoffCopyStateTimer = window.setTimeout(() => setHandoffCopyState(""), 1400);
      }
    }
    function updateHandoffControls() {
      handoffButton.hidden = false;
      handoffButton.disabled = sendingPrompt || !nativeThreadId;
      handoffPreviewButton.disabled = handoffBusy || !nativeThreadId || !handoffTargetProvider;
      handoffExecuteButton.disabled = handoffBusy || !handoffPreviewPayload;
      for (const button of handoffTargetButtons) button.disabled = handoffBusy;
      handoffIntent.disabled = handoffBusy;
      handoffNote.disabled = handoffBusy;
      updatePlanActionState();
    }
    function currentWorkspaceCwd() {
      const queryCwd = params.get("cwd") || "";
      if (queryCwd) return queryCwd;
      for (let index = loadedEvents.length - 1; index >= 0; index--) {
        const payload = (loadedEvents[index] && loadedEvents[index].payload) || {};
        const cwd = payload.cwd || payload.workdir || payload.working_directory || payload.workspace || "";
        if (typeof cwd === "string" && cwd.startsWith("/")) return cwd;
      }
      return "";
    }
    function handoffUrlWithToken(url) {
      const target = new URL(url, location.origin);
      target.searchParams.set("token", token);
      return target.pathname + "?" + target.searchParams.toString();
    }
    function updateComposerDisabled() {
      const mode = primaryComposerAction();
      const requiresTurn = mode === "interrupt" || mode === "steer";
      steerButton.hidden = !canSteerActiveTurn();
      interruptButton.hidden = !canInterruptActiveTurn();
      continueButton.innerHTML = mode === "interrupt" ? ICONS.stop : ICONS.send;
      continueButton.classList.toggle("stop", mode === "interrupt");
      continueButton.setAttribute(
        "aria-label",
        mode === "interrupt" ? "中断当前轮" : mode === "wait" ? "等待当前轮" : nativeTurnRunning ? "发送到当前轮" : "发送"
      );
      continueButton.disabled = (
        sendingPrompt ||
        !nativeThreadId ||
        mode === "wait" ||
        (requiresTurn && !activeTurnId) ||
        (!nativeTurnRunning && !composerHasDraft())
      );
      steerButton.disabled = sendingPrompt || !canSteerActiveTurn() || !nativeThreadId || !activeTurnId;
      attachmentButton.disabled = sendingPrompt;
      modelSelector.disabled = sendingPrompt || nativeTurnRunning;
      modelSettingsButton.disabled = false;
      permissionSelector.disabled = sendingPrompt;
      permissionSettingsButton.disabled = false;
      reasoningSelector.disabled = sendingPrompt || nativeTurnRunning || reasoningSelector.options.length <= 1;
      serviceTierSelector.disabled = sendingPrompt || nativeTurnRunning || serviceTierSelector.options.length <= 1;
      interruptButton.disabled = sendingPrompt || !canInterruptActiveTurn() || !nativeThreadId || !activeTurnId;
      updateHandoffControls();
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
      const summaryParts = [modelText];
      if (!reasoningSettingRow.hidden) summaryParts.push(effortText);
      if (!serviceTierSettingRow.hidden) summaryParts.push(tierText);
      modelSettingsButton.textContent = summaryParts.join(" ");
      syncSettingOptionsSelection(modelOptions, modelSelector);
      syncSettingOptionsSelection(reasoningOptions, reasoningSelector);
      syncSettingOptionsSelection(serviceTierOptions, serviceTierSelector);
      const defaultModel = modelCatalog.find(model => model.isDefault) || modelCatalog[0] || null;
      const currentModel = selectedModelCatalogEntry();
      const modelChanged = currentModel && defaultModel && (currentModel.model || currentModel.id || "") !== (defaultModel.model || defaultModel.id || "");
      const effortChanged = currentModel && modelSelector.value && !reasoningSettingRow.hidden && reasoningSelector.value && reasoningSelector.value !== preferredReasoningEffortDefault(currentModel, Array.isArray(currentModel.supportedReasoningEfforts) ? currentModel.supportedReasoningEfforts : []);
      const tierChanged = !serviceTierSettingRow.hidden && serviceTierSelector.value && serviceTierSelector.value !== (String(currentModel ? currentModel.defaultServiceTier || "" : "").toLowerCase());
      modelSettingsButton.classList.toggle("modified", modelChanged || effortChanged || tierChanged);
    }
    function selectedOptionText(select, fallback) {
      const option = select && select.options ? select.options[select.selectedIndex] : null;
      return (option && option.textContent ? option.textContent : fallback) || fallback;
    }
    function openInterruptionChoice() {
      if (!nativeTurnRunning || !composerHasDraft() || !canSteerActiveTurn()) return;
      interruptionChoice.hidden = false;
      steerChoice.disabled = sendingPrompt || !canSteerActiveTurn() || !activeTurnId;
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
      } catch (error) {
        if (dataUrl.length > MAX_IMAGE_DATA_URL_CHARS) {
          throw new Error(error.message || "图片过大，请换成 JPG 或 PNG 后重试");
        }
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
          try {
            const initialScale = Math.min(1, IMAGE_RESIZE_MAX_SIDE / Math.max(image.width, image.height));
            if (!Number.isFinite(initialScale) || initialScale <= 0) {
              reject(new Error("图片尺寸无效"));
              return;
            }
            if (
              dataUrl.length <= MAX_IMAGE_DATA_URL_CHARS &&
              file.size <= 900000 &&
              image.width <= IMAGE_RESIZE_MAX_SIDE &&
              image.height <= IMAGE_RESIZE_MAX_SIDE
            ) {
              resolve({
                url: dataUrl,
                filename: file.name || "image",
                mime_type: file.type || "image/*"
              });
              return;
            }
            const canvas = document.createElement("canvas");
            let width = Math.max(1, Math.round(image.width * initialScale));
            let height = Math.max(1, Math.round(image.height * initialScale));
            const draw = () => {
              canvas.width = width;
              canvas.height = height;
              const context = canvas.getContext("2d");
              if (!context) throw new Error("图片处理失败");
              context.drawImage(image, 0, 0, width, height);
            };
            draw();
            let quality = .82;
            let url = canvas.toDataURL("image/jpeg", quality);
            while (url.length > MAX_IMAGE_DATA_URL_CHARS && quality > .58) {
              quality = Math.max(.58, quality - .08);
              url = canvas.toDataURL("image/jpeg", quality);
            }
            while (url.length > MAX_IMAGE_DATA_URL_CHARS && Math.max(width, height) > IMAGE_RESIZE_MIN_SIDE) {
              width = Math.max(1, Math.round(width * .82));
              height = Math.max(1, Math.round(height * .82));
              draw();
              quality = .72;
              url = canvas.toDataURL("image/jpeg", quality);
            }
            if (url.length > MAX_IMAGE_DATA_URL_CHARS) {
              reject(new Error("图片压缩后仍过大，请换一张较小的图片"));
              return;
            }
            resolve({
              url,
              filename: file.name || "image",
              mime_type: "image/jpeg"
            });
          } catch (error) {
            reject(error);
          }
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
        remove.innerHTML = ICONS.remove;
        remove.onclick = () => {
          imageAttachments.splice(index, 1);
          renderAttachments();
        };
        chip.append(preview, name, remove);
        attachmentStrip.append(chip);
      });
    }
    function renderLocalUserEcho(text, images, turnId = "") {
      const event = {
        kind: "user_message",
        payload: {
          text: text || "",
          images: images || [],
          itemId: "local-user-" + Date.now(),
          native_turn_id: turnId
        }
      };
      renderTranscript(event, "user local-pending", "你");
      window.scrollTo(0, document.body.scrollHeight);
    }
    async function attachNative() {
      if (invalidNativeThreadId) return;
      if (!nativeThreadId || attached) return;
      attached = true;
      try {
        const result = await api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/attach`, {
          method: "POST",
          body: "{}",
          timeoutMs: 2500
        });
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.active_turn_id || "";
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        updateComposerDisabled();
        updateNativeHeaderContext();
      } catch (error) {
        renderStatus("attach_failed", error.message || String(error));
      }
    }
    async function syncNativeTranscript() {
      if (!nativeThreadId || invalidNativeThreadId) return;
      try {
        const result = await api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/sync`, {
          method: "POST",
          body: "{}",
          timeoutMs: 2500
        });
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.turn_running ? (result.active_turn_id || result.turn_id || activeTurnId || "") : "";
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        updateComposerDisabled();
        updateNativeHeaderContext();
      } catch (error) {
        renderStatus("native_sync_failed", error.message || String(error));
      }
    }
    function startNativeTranscriptSyncLoop() {
      if (nativeTranscriptSyncTimer) return;
      nativeTranscriptSyncTimer = setInterval(syncNativeTranscriptAndPoll, 2500);
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) syncNativeTranscriptAndPoll();
      });
    }
    async function syncNativeTranscriptAndPoll() {
      if (document.hidden) return;
      if (!nativeThreadId || invalidNativeThreadId) return;
      if (nativeSyncInFlight) return;
      nativeSyncInFlight = true;
      try {
        await syncNativeTranscript();
        await pollEvents();
      } finally {
        nativeSyncInFlight = false;
      }
    }
    function scheduleTerminalTranscriptSync(event) {
      if (!shouldSyncNativeTranscriptAfterTerminalEvent(event)) return;
      const key = eventFoldTurnId(event) || String((event && event.id) || "");
      if (!key || terminalTranscriptSyncTurns.has(key)) return;
      terminalTranscriptSyncTurns.add(key);
      window.setTimeout(async () => {
        await syncNativeTranscript();
        await pollEvents();
      }, 250);
    }
    function shouldSyncNativeTranscriptAfterTerminalEvent(event) {
      if (!nativeThreadId || invalidNativeThreadId) return false;
      if (!isTerminalTurnEvent(event)) return false;
      if (event.kind === "message_completed") return false;
      return !isMirroredTranscriptEvent(event);
    }
    renderPluginList();
    updateCollaborationMenu();
    updateComposerDisabled();
    renderPermissionSettings();
    loadProviderCapabilities();
    loadModelCatalog();
    if (invalidNativeThreadId) renderStatus("native_session_invalid", "会话链接无效，请从最近会话重新打开");
    refreshNativeControlInBackground();
    loadNativeSessionInfo().catch(() => {});
    loadRecentEvents().catch(error => {
      renderStatus("load_recent_failed", error.message || String(error));
    }).then(() => {
      setInterval(pollEvents, 1000);
      startNativeTranscriptSyncLoop();
    });
    function refreshNativeControlInBackground() {
      attachNative().then(syncNativeTranscript).then(loadNativeSessionInfo).catch(error => {
        renderStatus("native_sync_failed", error.message || String(error));
      });
    }
    async function loadRecentEvents() {
      let snapshot = await api(eventsPath("tail=" + CURRENT_TURN_EVENT_LIMIT, {currentTurn: true}));
      if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
      loadedEvents = snapshot.events || [];
      if (
        !loadedEvents.length ||
        !hasLiveDisplayEvents(loadedEvents) ||
        hasUnresolvedApprovalRequests(loadedEvents)
      ) {
        snapshot = await api(eventsPath("tail=" + RECENT_EVENT_LIMIT));
        if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
        loadedEvents = snapshot.events || [];
      }
      if (nativeThreadId && !hasNativePlanEvents(loadedEvents)) {
        snapshot = await api(eventsPath("tail=" + RECENT_EVENT_LIMIT));
        if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
        loadedEvents = mergeDisplayEvents(loadedEvents, snapshot.events || []);
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
    function hasNativePlanEvents(sourceEvents) {
      return sourceEvents.some(event => isNativePlanEvent(event));
    }
    function mergeDisplayEvents(currentEvents, nextEvents) {
      const byId = new Map();
      for (const event of currentEvents || []) {
        if (event && event.id) byId.set(event.id, event);
      }
      for (const event of nextEvents || []) {
        if (event && event.id) byId.set(event.id, event);
      }
      return Array.from(byId.values()).sort((left, right) => (left.id || 0) - (right.id || 0));
    }
    function hasUnresolvedApprovalRequests(sourceEvents) {
      const requested = new Set();
      const resolved = new Set();
      for (const event of sourceEvents) {
        if (event.kind === "approval_requested") {
          const key = approvalRequestKey(event);
          if (key) requested.add(key);
        } else if (event.kind === "approval_resolved") {
          const key = approvalRequestKey(event);
          if (key) resolved.add(key);
        }
      }
      for (const key of requested) {
        if (!resolved.has(key)) return true;
      }
      return false;
    }
    function isInternalEvent(event) {
      return Boolean(
        event && (
          event.type === "model.usage.updated" ||
          isNativeExecutionDetail(event) ||
          isNativeReasoningDetail(event) ||
          (isNativeActivityDetail(event) && !isNativePlanEvent(event))
        )
      );
    }
    function isNativePlanEvent(event) {
      const payload = (event && event.payload) || {};
      return isNativeFeedbackMode(event) && event && event.kind === "activity" && payload.action === "plan_updated";
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
      if (nativeThreadId) params.set("native_thread_id", nativeThreadId);
      if (PROVIDER) params.set("native_provider", PROVIDER);
      const suffix = params.toString();
      return suffix ? streamPathBase + "?" + suffix : streamPathBase;
    }
    function eventsPath(params, options = {}) {
      const search = new URLSearchParams(params);
      if (nativeThreadId) search.set("native_thread_id", nativeThreadId);
      if (PROVIDER) search.set("native_provider", PROVIDER);
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
      fileChangeSummaryNodes.clear();
      fileChangeSummaryStates.clear();
      events.innerHTML = "";
      const groups = foldGroups(dedupeDisplayEvents(loadedEvents)).map(orderTranscriptGroupEvents);
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
      scheduleTerminalTranscriptSync(event);
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
      if (isTranscriptEvent(event) && hasCompletedAssistantMessageForTurn(event)) {
        rebuildStream();
        applyNativeTurnState(event);
        updateComposerDisabled();
        if (event.id) cursor.textContent = "#" + event.id;
        window.scrollTo(0, document.body.scrollHeight);
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
      const canonicalUserTurns = canonicalUserTranscriptTurnSet(sourceEvents);
      const seen = new Set();
      const seenUserMessages = new Map();
      const result = [];
      for (const event of sourceEvents) {
        if (isInternalEvent(event)) continue;
        if (
          event.kind === "user_message" &&
          canonicalUserTurns.has(eventFoldTurnId(event)) &&
          !isCanonicalTranscriptItem(event)
        ) {
          continue;
        }
        if (
          event.kind === "text_delta" &&
          officialAssistantTurns.has(assistantTurnKey(event)) &&
          !isOfficialAssistantTranscriptEvent(event)
        ) {
          continue;
        }
        if (event.kind === "user_message") {
          const userFingerprint = canonicalUserMessageFingerprint(event);
          const previousUserIndex = userFingerprint ? seenUserMessages.get(userFingerprint) : undefined;
          if (previousUserIndex !== undefined) {
            const previousUser = result[previousUserIndex];
            if (isSyntheticUserMessageEvent(event) || isSyntheticUserMessageEvent(previousUser)) {
              if (userMessageDedupePriority(event) > userMessageDedupePriority(previousUser)) {
                result[previousUserIndex] = event;
              }
              continue;
            }
          }
          if (userFingerprint) seenUserMessages.set(userFingerprint, result.length);
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
    function canonicalUserTranscriptTurnSet(sourceEvents) {
      const turns = new Set();
      for (const event of sourceEvents) {
        if (event.kind !== "user_message") continue;
        if (!isCanonicalTranscriptItem(event)) continue;
        const turnId = eventFoldTurnId(event);
        if (turnId) turns.add(turnId);
      }
      return turns;
    }
    function isSyntheticUserMessageEvent(event) {
      if (!event || event.kind !== "user_message") return false;
      const payload = event.payload || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      const turnId = String(payload.native_turn_id || payload.turnId || "");
      return itemId.startsWith("local-user-") || itemId.startsWith("jsonl-user:") || turnId.startsWith("jsonl-turn:");
    }
    function userMessageDedupePriority(event) {
      if (isCanonicalTranscriptItem(event)) return 30;
      if (!isSyntheticUserMessageEvent(event)) return 20;
      const payload = event.payload || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (itemId.startsWith("local-user-")) return 10;
      return 5;
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
    function isTranscriptEvent(event) {
      return event.kind === "user_message" || isAssistantMessageEvent(event);
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
      if (key && previousEvents.some(previous => mirroredDisplayKey(previous) === key)) {
        return true;
      }
      if (event.kind === "user_message") {
        const fingerprint = canonicalUserMessageFingerprint(event);
        if (fingerprint && previousEvents.some(previous => canonicalUserMessageFingerprint(previous) === fingerprint)) {
          return true;
        }
      }
      if (!event.id) return false;
      const eventId = String(event.id);
      return previousEvents.some(previous => String(previous.id || "") === eventId);
    }
    function mirroredDisplayKey(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (!itemId.startsWith("jsonl-")) return "";
      if (event.kind === "message_completed") return completedAssistantMessageKey(event);
      return itemId;
    }
    function isCanonicalTranscriptItem(event) {
      return transcriptItemOrder(event) < Number.MAX_SAFE_INTEGER;
    }
    function orderTranscriptGroupEvents(group) {
      return group.slice().sort((left, right) => {
        return (
          displayEventOrder(left) - displayEventOrder(right) ||
          transcriptItemOrder(left) - transcriptItemOrder(right) ||
          Number(left.id || 0) - Number(right.id || 0)
        );
      });
    }
    function displayEventOrder(event) {
      if (event.kind === "user_message") return 10;
      if (event.kind === "reasoning_delta") return 20;
      if (isCommandEvent(event)) return 30;
      if (isNativePlanEvent(event)) return 35;
      if (event.kind === "text_delta" || event.kind === "message_completed") return 40;
      if (event.kind === "approval_requested" || event.kind === "approval_resolved") return 50;
      return 60;
    }
    function transcriptItemOrder(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      const match = /^item-(\\d+)$/.exec(itemId);
      if (!match) return Number.MAX_SAFE_INTEGER;
      return Number(match[1]);
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
      details.className = "turn-fold collapsed";
      details.setAttribute("open", "");
      const head = document.createElement("summary");
      head.addEventListener("click", (e) => {
        e.preventDefault();
        details.classList.toggle("collapsed");
      });
      const labelRow = document.createElement("div");
      labelRow.className = "turn-fold-row";
      const title = document.createElement("span");
      title.className = "turn-fold-title";
      title.textContent = turnFoldTitle(group);
      const chevron = document.createElement("span");
      chevron.className = "turn-fold-chevron";
      chevron.innerHTML = ICONS.chevron;
      labelRow.append(title, chevron);
      head.append(labelRow);
      renderFoldPreview(head, group);
      const body = document.createElement("div");
      body.className = "turn-fold-body";
      const inner = document.createElement("div");
      inner.className = "turn-fold-body-inner";
      body.append(inner);
      details.append(head, body);
      events.append(details);
      const previousTarget = renderTarget;
      renderTarget = inner;
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
      for (let index = group.length - 1; index >= 0; index--) {
        const event = group[index];
        if (event.kind !== kind) continue;
        const payload = event.payload || {};
        const text = String(payload.text || payload.delta || "").trim();
        if (!text) continue;
        return trimFoldPreview(text);
      }
      return "";
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
      const generatedPrompt = groupHasGeneratedPrompt(group);
      const currentTurnId = activeTurnId || (nativeTurnRunning ? nativeTurnId : "");
      const shouldCollapse = (
        groupHasVisibleContent(group) &&
        nativeTurnId &&
        !failed &&
        !pendingApproval &&
        !generatedPrompt &&
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
    function groupHasGeneratedPrompt(group) {
      return group.some(event => {
        if (!isAssistantMessageEvent(event)) return false;
        const payload = event.payload || {};
        return Boolean(splitGeneratedPromptText(payload.text || payload.delta || payload.summary || ""));
      });
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
      if (event.kind === "lifecycle" || event.kind === "completed") return "";
      if (event.kind === "message_completed") return `assistant:${completedAssistantMessageKey(event)}`;
      if (itemId) return `${event.kind}:${itemId}`;
      if (event.kind === "user_message") return `user:${turnId}:user`;
      if (isNativePlanEvent(event)) return `plan:${turnId}:plan`;
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
      if (event.kind === "user_message") {
        if (!options.historical) clearMatchingLocalUserEcho(event);
        renderTranscript(event, "user", "你", options);
      }
      else if (isNativePlanEvent(event)) renderPlanEvent(event);
      else if (event.kind === "text_delta" || event.kind === "message_completed") renderAssistant(event, options);
      else if (event.kind === "reasoning_delta") renderStatusEvent(event, "思考中", "busy");
      else if (isCommandEvent(event)) renderToolCall(event);
      else if (event.kind === "diff_updated" || event.kind === "file_changed") renderFileChangeSummary(event);
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
    function renderAssistant(event, opts = {}) {
      renderTranscript(event, "assistant", PROVIDER_LABEL, opts);
    }
    function clearMatchingLocalUserEcho(event) {
      const payload = event.payload || {};
      const incomingText = normalizeTranscriptText(
        String(payload.text || payload.delta || payload.summary || payload.content || payload.prompt || "").trim()
      );
      const incomingImages = Array.isArray(payload.images) ? payload.images.length : 0;
      let matched = false;
      for (const [key, node] of Array.from(transcriptNodes.entries())) {
        if (!node || !node.row || !node.row.classList.contains("local-pending")) continue;
        if (!localUserEchoMatchesEvent(node, incomingText, incomingImages)) continue;
        node.row.remove();
        transcriptNodes.delete(key);
        matched = true;
      }
      if (!matched && pendingUserEcho && (incomingText || incomingImages || event.kind === "user_message")) {
        for (const [key, node] of Array.from(transcriptNodes.entries())) {
          if (!node || !node.row || !node.row.classList.contains("local-pending")) continue;
          node.row.remove();
          transcriptNodes.delete(key);
          break;
        }
      }
    }
    function localUserEchoMatchesEvent(node, incomingText, incomingImages) {
      const body = node.body;
      if (!body) return false;
      const text = normalizeTranscriptText(String(body.textContent || ""));
      const images = body.querySelectorAll ? body.querySelectorAll(".transcript-image").length : 0;
      if (!text && !incomingText) {
        return images === incomingImages;
      }
      if (images !== incomingImages) return false;
      return text === incomingText || text.startsWith(incomingText) || incomingText.startsWith(text);
    }
    function normalizeTranscriptText(value) {
      return String(value || "").replace(/\\s+/g, " ").trim();
    }
    function canonicalUserMessageFingerprint(event) {
      if (!event || event.kind !== "user_message") return "";
      const payload = event.payload || {};
      const text = normalizeTranscriptText(
        String(payload.text || payload.delta || payload.summary || payload.content || payload.prompt || "")
      );
      const images = Array.isArray(payload.images) ? payload.images.length : 0;
      const turnId = String(eventFoldTurnId(event) || nativeThreadId || "");
      return `user-message:${turnId}:${text}:${images}`;
    }
    function renderCommand(event) {
      renderToolCall(event);
    }
    function renderPlanEvent(event) {
      const payload = event.payload || {};
      const planText = planTextFromPayload(payload);
      if (!planText.trim()) {
        renderStatusEvent(event, "计划已更新", "busy");
        return;
      }
      const titleText = planTitleFromText(planText, payload.title || "");
      const summaryText = planSummaryFromText(planText);
      setActivePlanFromEvent(event, planText, titleText, summaryText);
      const row = document.createElement("div");
      row.className = "plan-item";
      row.append(createPlanCardElement(planText, titleText, {executable: true}));
      renderTarget.append(row);
      updateRunState("计划已更新", "busy");
    }
    function createPlanCardElement(planText, titleFallback, options = {}) {
      const executable = options.executable === true;
      const titleText = planTitleFromText(planText, titleFallback || "");
      const summaryText = planSummaryFromText(planText);
      const plan = activePlan && activePlan.body === planText && Boolean(activePlan.executable) === executable ? activePlan : {
        title: titleText,
        summary: summaryText,
        body: planText,
        threadId: nativeThreadId,
        turnId: "",
        status: "",
        source: executable ? "native_plan_event" : "text_fallback",
        executable
      };
      const card = document.createElement("section");
      card.className = "plan-card";
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");
      card.onclick = () => openPlanPage(plan);
      card.onkeydown = event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openPlanPage(plan);
        }
      };

      const head = document.createElement("div");
      head.className = "plan-card-head";
      const label = document.createElement("span");
      label.className = "plan-card-label";
      label.innerHTML = `${ICONS.plan}<span>计划</span>`;
      const actions = document.createElement("span");
      actions.className = "plan-card-actions";
      const download = document.createElement("button");
      download.type = "button";
      download.className = "plan-card-action";
      download.setAttribute("aria-label", "下载计划");
      download.innerHTML = ICONS.download;
      download.onclick = click => {
        click.stopPropagation();
        downloadPlanText(titleText, planText);
      };
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "plan-card-action";
      copy.setAttribute("aria-label", "复制计划");
      copy.innerHTML = ICONS.copy;
      copy.onclick = click => {
        click.stopPropagation();
        copyPromptCardText(copy, planText);
      };
      actions.append(download, copy);
      head.append(label, actions);

      const title = document.createElement("h2");
      title.className = "plan-card-title";
      title.textContent = titleText;
      const summaryTitle = document.createElement("h3");
      summaryTitle.className = "plan-card-summary-title";
      summaryTitle.textContent = "Summary";
      const summary = document.createElement("div");
      summary.className = "plan-card-summary";
      if (summaryText) renderMarkdownLite(summary, summaryText);
      if (plan.executable) {
        const execute = document.createElement("button");
        execute.type = "button";
        execute.className = "plan-card-execute";
        execute.textContent = "执行计划";
        execute.onclick = click => {
          click.stopPropagation();
          activePlan = plan;
          executeActivePlan();
        };
        card.append(head, title, summaryTitle, summary, execute);
      } else {
        const readonly = document.createElement("span");
        readonly.className = "plan-card-readonly";
        readonly.textContent = "仅文本计划，不能一键执行";
        card.append(head, title, summaryTitle, summary, readonly);
      }
      updatePlanActionState();
      return card;
    }
    function planTextFromPayload(payload) {
      const source = payload.plan !== undefined ? payload.plan : payload.summary;
      return normalizePlanPayloadValue(source || payload.text || payload.delta || "");
    }
    function normalizePlanPayloadValue(value) {
      if (typeof value === "string") return value.trim();
      if (Array.isArray(value)) {
        return value.map(item => normalizePlanPayloadValue(item)).filter(Boolean).join("\\n");
      }
      if (value && typeof value === "object") {
        const chunks = [];
        if (value.title) chunks.push("# " + String(value.title).trim());
        if (value.summary) chunks.push("## Summary\\n" + normalizePlanPayloadValue(value.summary));
        const steps = value.steps || value.items;
        if (Array.isArray(steps) && steps.length) {
          chunks.push("## Steps\\n" + steps.map(step => "- " + planStepText(step)).join("\\n"));
        }
        if (chunks.length) return chunks.join("\\n\\n").trim();
        return Object.entries(value)
          .filter(([, entryValue]) => typeof entryValue === "string" || typeof entryValue === "number")
          .map(([key, entryValue]) => `${key}: ${entryValue}`)
          .join("\\n")
          .trim();
      }
      return "";
    }
    function planStepText(step) {
      if (typeof step === "string") return step.trim();
      if (!step || typeof step !== "object") return String(step || "").trim();
      const text = step.text || step.title || step.description || step.step || "";
      const status = step.status ? `[${step.status}] ` : "";
      return status + String(text || JSON.stringify(step)).trim();
    }
    function planTitleFromText(text, fallback) {
      if (String(fallback || "").trim()) return String(fallback).trim();
      const normalized = String(text || "").replace(/\\r\\n/g, "\\n");
      const heading = normalized.match(/^#\\s+(.+)$/m);
      if (heading) return heading[1].trim();
      const first = normalized.split("\\n").map(line => line.trim()).find(Boolean) || "计划";
      return first.replace(/^#+\\s*/, "").slice(0, 120);
    }
    function planSummaryFromText(text) {
      const normalized = String(text || "").replace(/\\r\\n/g, "\\n");
      const summary = normalized.match(/^##\\s+Summary\\s*\\n([\\s\\S]*?)(?=\\n##\\s+|$)/im);
      const source = summary ? summary[1] : normalized.replace(/^#\\s+.+$/m, "");
      return trimPlanSummary(source);
    }
    function trimPlanSummary(text) {
      const compact = String(text || "").trim();
      if (compact.length <= 360) return compact;
      return compact.slice(0, 357).trimEnd() + "...";
    }
    function downloadPlanText(title, text) {
      const blob = new Blob([String(text || "")], {type: "text/markdown;charset=utf-8"});
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = safeDownloadName(title || "plan") + ".md";
      document.body.append(link);
      link.click();
      URL.revokeObjectURL(link.href);
      link.remove();
    }
    function safeDownloadName(value) {
      return String(value || "plan").trim().replace(/[\\\\/:*?"<>|]+/g, "-").slice(0, 80) || "plan";
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
    function renderTranscript(event, role, label, opts = {}) {
      const payload = event.payload || {};
      const key = transcriptKey(event, role);
      let node = transcriptNodes.get(key);
      if (!node) {
        const row = document.createElement("article");
        row.className = "transcript-item " + role;
        if (opts.historical) row.classList.add("no-animate");
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
        const renderedPrompt = renderGeneratedPromptTranscript(node.body, node.text, event);
        node.row.classList.toggle("prompt-message", renderedPrompt);
        if (renderedPrompt) return;
        renderMarkdownLite(node.body, node.text);
      } else {
        node.text = String(incomingText);
        const renderedPrompt = renderGeneratedPromptTranscript(node.body, incomingText, event);
        node.row.classList.toggle("prompt-message", renderedPrompt);
        if (renderedPrompt) return;
        node.row.classList.remove("prompt-message");
        renderTranscriptImages(node.body, payload.images || []);
        appendText(node.body, incomingText);
      }
    }
    function renderGeneratedPromptTranscript(target, text, event) {
      const split = splitGeneratedPromptText(text);
      if (!split) return false;
      target.replaceChildren();
      if (split.preface) {
        const preface = document.createElement("div");
        preface.className = "prompt-preface";
        renderMarkdownLite(preface, split.preface);
        target.append(preface);
      }
      if (isPlanExecutionPrompt(split.prompt) && !hasNativePlanEventForTurn(event)) {
        const planText = planTextFromExecutionPrompt(split.prompt);
        if (planText) {
          const plan = document.createElement("div");
          plan.className = "plan-item prompt-plan-fallback";
          plan.append(createPlanCardElement(planText, "", {executable: false}));
          target.append(plan);
        }
      }
      target.append(createPlainPromptCard(split.prompt));
      return true;
    }
    function hasNativePlanEventForTurn(event) {
      const payload = (event && event.payload) || {};
      const turnId = String(payload.native_turn_id || payload.turnId || "");
      if (!turnId) return false;
      return loadedEvents.some(candidate => isNativePlanEvent(candidate) && eventFoldTurnId(candidate) === turnId);
    }
    function planTextFromExecutionPrompt(text) {
      const source = String(text || "").trim();
      if (!isPlanExecutionPrompt(source)) return "";
      return source.replace(/^PLEASE IMPLEMENT THIS PLAN:\\s*/i, "").trim();
    }
    function splitGeneratedPromptText(text) {
      const source = String(text || "").replace(/\\r\\n/g, "\\n");
      const match = generatedPromptStartMatch(source);
      if (!match) return null;
      const promptStart = match.index + (match[1] ? 1 : 0);
      const rawPrompt = stripGeneratedPromptFence(source.slice(promptStart)).trim();
      const prompt = normalizeGeneratedPromptText(rawPrompt);
      if (!isGeneratedPromptBody(prompt)) return null;
      return {
        preface: stripGeneratedPromptFence(source.slice(0, promptStart)).trim(),
        prompt
      };
    }
    function generatedPromptStartMatch(source) {
      return /(^|\\n)(?:你在[\\s\\S]+?工作。|PLEASE IMPLEMENT THIS PLAN:)/m.exec(source);
    }
    function stripGeneratedPromptFence(text) {
      return String(text || "")
        .replace(/(^|\\n)```[\\w-]*\\s*$/g, "$1")
        .replace(/^\\s*```[\\w-]*\\s*\\n?/, "")
        .replace(/\\n?\\s*```\\s*$/, "");
    }
    function normalizeGeneratedPromptText(text) {
      const source = String(text || "").replace(/\\r\\n/g, "\\n").trim();
      if (!source) return "";
      return collapseGeneratedPromptHardWraps(source);
    }
    function collapseGeneratedPromptHardWraps(text) {
      const lines = String(text || "").replace(/\\r\\n/g, "\\n").split("\\n");
      const output = [];
      let paragraph = [];
      function flushParagraph() {
        if (!paragraph.length) return;
        output.push(paragraph.join(" ").replace(/\\s{2,}/g, " ").trim());
        paragraph = [];
      }
      function appendBlankLine() {
        flushParagraph();
        if (output.length && output[output.length - 1] !== "") output.push("");
      }
      for (const rawLine of lines) {
        const trimmed = rawLine.trim();
        if (!trimmed) {
          appendBlankLine();
          continue;
        }
        if (isGeneratedPromptSectionHeading(trimmed) || isGeneratedPromptListItem(rawLine)) {
          flushParagraph();
          if (isGeneratedPromptSectionHeading(trimmed)) {
            appendBlankLine();
            output.push(trimmed);
          } else {
            output.push(normalizeGeneratedPromptListLine(rawLine));
          }
          continue;
        }
        paragraph.push(trimmed);
        if (isGeneratedPromptSentenceBoundary(trimmed)) flushParagraph();
      }
      flushParagraph();
      return output.join("\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
    }
    function isGeneratedPromptSectionHeading(line) {
      return /^(背景|必须阅读的文档|必须阅读并对齐的 V1 源码|V2 当前重点位置|目标|不可变业务规则|实施边界|测试要求，必须 RED first|建议实施方案|验证命令至少运行|交付时必须说明|重点就一句)：$/.test(
        String(line || "").trim()
      );
    }
    function isGeneratedPromptListItem(line) {
      return /^\\s*(?:[-*]\\s+|\\d+\\.\\s+)/.test(String(line || ""));
    }
    function normalizeGeneratedPromptListLine(line) {
      return String(line || "").replace(/\\t/g, "  ").replace(/[ \\t]+$/g, "");
    }
    function isGeneratedPromptSentenceBoundary(line) {
      return /[。！？.!?；;”")）]$/.test(String(line || "").trim());
    }
    function isGeneratedPromptBody(text) {
      const source = String(text || "");
      if (/^PLEASE IMPLEMENT THIS PLAN:/i.test(source)) return true;
      if (!source.startsWith("你在 ")) return false;
      const markers = ["背景：", "必须阅读", "重点就一句"];
      return markers.some(marker => source.includes(marker));
    }
    function createPlainPromptCard(text) {
      const card = document.createElement("section");
      card.className = "prompt-card";
      if (isPlanExecutionPrompt(text)) card.classList.add("collapsed");
      card.onclick = () => {
        if (isPlanExecutionPrompt(text)) card.classList.toggle("collapsed");
      };
      const head = document.createElement("div");
      head.className = "prompt-card-head";
      const title = document.createElement("span");
      title.className = "prompt-card-title";
      title.textContent = promptCardTitle(text);
      const button = document.createElement("button");
      button.className = "handoff-copy prompt-card-copy";
      button.type = "button";
      button.setAttribute("aria-label", "复制提示词");
      button.innerHTML = ICONS.copy;
      button.onclick = event => {
        event.stopPropagation();
        copyPromptCardText(button, text);
      };
      head.append(title, button);
      const body = document.createElement("pre");
      body.className = "prompt-card-body";
      body.textContent = promptCardBodyText(text);
      card.append(head, body);
      return card;
    }
    function isPlanExecutionPrompt(text) {
      return /^PLEASE IMPLEMENT THIS PLAN:/i.test(String(text || "").trim());
    }
    function promptCardTitle(text) {
      if (isPlanExecutionPrompt(text)) return "PLEASE IMPLEMENT THIS PLAN:";
      return "Plain text";
    }
    function promptCardBodyText(text) {
      const source = String(text || "").trim();
      if (!isPlanExecutionPrompt(source)) return source;
      return source.replace(/^PLEASE IMPLEMENT THIS PLAN:\\s*/i, "").trim();
    }
    async function copyPromptCardText(button, text) {
      const value = String(text || "");
      if (!value.trim()) return;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(value);
        } else {
          fallbackCopyText(value);
        }
        setPromptCardCopyState(button, "copied");
      } catch (_error) {
        try {
          fallbackCopyText(value);
          setPromptCardCopyState(button, "copied");
        } catch (_fallbackError) {
          setPromptCardCopyState(button, "failed");
        }
      }
    }
    function setPromptCardCopyState(button, state) {
      const copied = state === "copied";
      const failed = state === "failed";
      button.classList.toggle("copied", copied);
      button.setAttribute(
        "aria-label",
        copied ? "已复制提示词" : failed ? "复制失败" : "复制提示词"
      );
      if (copied || failed) {
        window.setTimeout(() => setPromptCardCopyState(button, ""), 1400);
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
    function renderFileChangeSummary(event) {
      const payload = event.payload || {};
      const key = fileChangeSummaryKey(event);
      const incoming = summarizeDiffPayload(payload);
      const state = mergeFileChangeSummaryState(key, incoming, event.kind);
      let node = fileChangeSummaryNodes.get(key);
      if (!node || !node.row.isConnected) {
        const row = document.createElement("div");
        row.className = "file-change-summary";
        const pill = document.createElement("div");
        pill.className = "file-change-summary-pill";
        const label = document.createElement("span");
        label.className = "file-change-summary-label";
        const add = document.createElement("span");
        add.className = "file-change-summary-add";
        const del = document.createElement("span");
        del.className = "file-change-summary-del";
        pill.append(label, add, del);
        row.append(pill);
        renderTarget.append(row);
        node = {row, pill, label, add, del};
        fileChangeSummaryNodes.set(key, node);
      }
      node.label.textContent = `已更改 ${state.fileCount} 个文件`;
      node.add.textContent = `+${state.additions}`;
      node.del.textContent = `-${state.deletions}`;
      node.pill.title = state.files.length ? state.files.join("\\n") : node.label.textContent;
    }
    function fileChangeSummaryKey(event) {
      const payload = event.payload || {};
      return String(payload.native_turn_id || payload.turnId || nativeTurnId || "global");
    }
    function mergeFileChangeSummaryState(key, incoming, kind) {
      if (kind === "diff_updated") {
        const files = incoming.files.length ? incoming.files : [incoming.fileLabel].filter(Boolean);
        const next = {
          files,
          fileSet: new Set(files),
          additions: incoming.additions,
          deletions: incoming.deletions,
        };
        fileChangeSummaryStates.set(key, next);
        return visibleFileChangeSummaryState(next, incoming);
      }
      const current = fileChangeSummaryStates.get(key) || {
        files: [],
        fileSet: new Set(),
        additions: 0,
        deletions: 0,
      };
      for (const file of incoming.files) {
        if (!current.fileSet.has(file)) {
          current.fileSet.add(file);
          current.files.push(file);
        }
      }
      current.additions += incoming.additions;
      current.deletions += incoming.deletions;
      fileChangeSummaryStates.set(key, current);
      return visibleFileChangeSummaryState(current, incoming);
    }
    function visibleFileChangeSummaryState(state, fallback) {
      const fileCount = state.files.length || fallback.fileCount || 1;
      return {
        files: state.files,
        fileCount,
        additions: state.additions,
        deletions: state.deletions,
      };
    }
    function summarizeDiffPayload(payload) {
      const text = rawDiffText(payload);
      const files = diffFiles(payload, text);
      const counts = diffLineCounts(text);
      return {
        files,
        fileLabel: payload.filePath || payload.path || "",
        fileCount: files.length || (text.trim() ? 1 : 0),
        additions: counts.additions,
        deletions: counts.deletions,
      };
    }
    function rawDiffText(payload) {
      return String((payload && (payload.diff || payload.patch || payload.delta)) || "");
    }
    function diffFiles(payload, text) {
      const files = new Set();
      const direct = payload && (payload.filePath || payload.path);
      if (direct) files.add(String(direct));
      for (const line of String(text || "").split("\\n")) {
        let match = /^diff --git a\\/(.+?) b\\/(.+)$/.exec(line);
        if (match) {
          files.add(match[2]);
          continue;
        }
        match = /^\\+\\+\\+ b\\/(.+)$/.exec(line);
        if (match && match[1] !== "/dev/null") files.add(match[1]);
      }
      return Array.from(files);
    }
    function diffLineCounts(text) {
      let additions = 0;
      let deletions = 0;
      for (const line of String(text || "").split("\\n")) {
        if (line.startsWith("+++") || line.startsWith("---")) continue;
        if (line.startsWith("+")) additions += 1;
        else if (line.startsWith("-")) deletions += 1;
      }
      return {additions, deletions};
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
      const alreadyResolved = resolvedApprovalEventFor(payload.codexRequestId);
      if (alreadyResolved) {
        setApprovalState(card, approvalResolvedAction(alreadyResolved, card), "resolved");
        return;
      }
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
      updateHeaderRunIndicator(tone || "neutral");
      updateNativeHeaderContext();
    }
    function updateHeaderRunIndicator(tone) {
      const visual = tone === "busy" ? "running" : tone === "failed" || tone === "done" ? "finished" : "neutral";
      headerRunIndicator.className = "header-run-indicator " + visual;
    }
    function transcriptKey(event, role) {
      const payload = event.payload || {};
      if (role.includes("assistant")) return ["assistant", assistantMessageKey(event)].join(":");
      return [role, payload.native_turn_id || "", payload.itemId || role].join(":");
    }
    function assistantMessageKey(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (event.kind === "message_completed" && itemId.startsWith("jsonl-assistant")) {
        return completedAssistantMessageKey(event);
      }
      if (itemId.startsWith("jsonl-assistant")) return itemId;
      return `${payload.native_turn_id || payload.turnId || ""}:assistant`;
    }
    function completedAssistantMessageKey(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (itemId) return `${itemId}:${event.id || transcriptTextFingerprint(event)}`;
      return `${payload.native_turn_id || payload.turnId || ""}:completed:${event.id || transcriptTextFingerprint(event)}`;
    }
    function transcriptTextFingerprint(event) {
      const payload = (event && event.payload) || {};
      return String(payload.text || payload.summary || payload.delta || "").slice(0, 160);
    }
    function statusKey(event) {
      const payload = event.payload || {};
      return ["status", payload.native_turn_id || "", payload.itemId || event.kind].join(":");
    }
    function appendText(node, text) {
      if (!text) return;
      node.append(document.createTextNode(String(text)));
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
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
      if (event.kind === "command_completed") return "正在整理回复";
      return "正在处理";
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
      if (event.kind === "lifecycle" && status === "running") return "正在回复";
      if (event.kind === "reasoning_delta") return "Thinking";
      if (event.kind === "completed") return "完成";
      if (event.kind === "failed") return "失败";
      return fallback || event.kind || "状态";
    }
  </script>
__MARVIS_EXTRA_HTML__
</body>
</html>"""
    return _replace_html_icons(
        template
        .replace("__SAFE_TITLE__", safe_title)
        .replace("__NATIVE_APP_HEAD__", _NATIVE_APP_HEAD)
        .replace("__MARVIS_CSS_LINK__", marvis_css_link)
        .replace("__MARVIS_BODY_ATTR__", marvis_body_attr)
        .replace("__MARVIS_EXTRA_HTML__", "")
        .replace("__PROVIDER_LABEL_TEXT__", safe_title)
        .replace("__STREAM_PATH__", stream_path)
        .replace("__AGENT_RUN_ID__", str(agent_run_id))
        .replace("__PROVIDER_JSON__", json.dumps(native_provider, ensure_ascii=False))
        .replace("__PROVIDER_LABEL_JSON__", json.dumps(provider_label, ensure_ascii=False))
        .replace("__API_BASE_JSON__", json.dumps(api_base, ensure_ascii=False))
        .replace(
            "__SUPPORTS_PLAN_MODE_JSON__",
            json.dumps(supports_plan_mode),
        )
        .replace(
            "__SUPPORTS_PLUGIN_MENU_JSON__",
            json.dumps(supports_plugin_menu),
        )
        .replace(
            "__USES_CLAUDE_PLAN_PERMISSION_MODE_JSON__",
            json.dumps(uses_claude_plan_permission_mode),
        )
        .replace(
            "__PLAN_MODE_ACTION_HIDDEN__",
            plan_mode_action_hidden,
        )
        .replace(
            "__PLUGIN_MENU_HIDDEN__",
            plugin_menu_hidden,
        )
        .replace(
            "__PERMISSION_PRESETS_JSON__",
            json.dumps(_native_permission_presets(native_provider), ensure_ascii=False),
        )
        .replace(
            "__PLUGIN_MENU_ITEMS_JSON__",
            json.dumps(
                _codex_plugin_menu_items()
                if supports_plugin_menu
                else [],
                ensure_ascii=False,
            ),
        )
        .replace("__ICONS_JS__", _ICONS_JS_LITERAL)
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
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    body {{ background: var(--btn-primary-color); position: relative; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 12px 16px; background: rgba(25, 27, 32, 0.82); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border-bottom: 1px solid var(--border-default); }}
    main {{ padding: 12px 12px 132px; position: relative; z-index: 1; }}
    .event {{ white-space: pre-wrap; border-bottom: 1px solid var(--border-default); padding: 10px 4px; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }}
    .meta {{ color: #a1a1aa; font-size: 12px; margin-bottom: 4px; }}
    .approval_requested {{ color: #facc15; }}
    .failed {{ color: var(--color-error-light); }}
    .completed {{ color: var(--color-success); }}
    .controls {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; display: grid; gap: 8px; padding: 10px; background: linear-gradient(to top, rgba(0,0,0,.98) 55%, rgba(0,0,0,.85) 78%, rgba(0,0,0,0)); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 1px solid var(--border-default); }}
    .row {{ display: flex; gap: 8px; min-width: 0; }}
    input {{ flex: 1; min-width: 0; border-radius: 8px; border: 1px solid var(--border-input); background: #17191f; color: var(--btn-primary-bg); padding: 11px; font-size: 15px; }}
    button {{ min-height: 40px; }}
    button.secondary {{ background: #1b1e25; color: var(--btn-primary-bg); }}
    button.warn {{ background: var(--color-error-light); color: #1b0707; }}
    .approval-actions {{ display: flex; gap: 8px; margin-top: 8px; }}
  </style>
</head>
<body class="aurora-bg noise-overlay">
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
