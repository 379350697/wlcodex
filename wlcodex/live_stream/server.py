from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import hmac
import json
import re
import secrets
import shlex
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
from wlcodex.live_stream.native_templates.registry import render_native_template
from wlcodex.native_timeline import (
    NativeTimelineEvent,
    NativeTimelineItem,
    NativeTimelineStore,
)
from wlcodex.jsonrpc import JsonRpcError, JsonRpcTimeout
from wlcodex.relay.display import (
    dict_looks_like_role_envelope as relay_dict_looks_like_role_envelope,
)
from wlcodex.relay.display import (
    followup_response_display_text,
    humanize_display_text,
    humanize_role_envelope,
    join_text_list,
    protocol_output_hidden_text,
    replace_legacy_role_identifiers,
    role_output_error_text,
    routing_risk_label,
    routing_route_label,
    sanitize_protocol_leak_text,
    text_contains_relay_protocol_payload,
    text_needs_chinese_fallback,
)
from wlcodex.relay.envelopes import parse_role_envelope
from wlcodex.relay.marvis_interaction import (
    chat_events as marvis_chat_events,
    project_relay_rows_to_marvis_interactions,
)
from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES, RELAY_ROLE_IDS
from wlcodex.relay.work_log_projection import (
    RawWorkLogEntry,
    compress_work_log_entries,
)


_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_HEADER_BYTES = 16 * 1024
_MAX_BODY_BYTES = 24 * 1024 * 1024
_RELAY_SSE_INITIAL_EVENT_LIMIT = 100
_MAX_NATIVE_IMAGE_ATTACHMENTS = 8
_MAX_RELAY_TEXT_ATTACHMENTS = 5
_MAX_RELAY_TEXT_ATTACHMENT_CHARS = 80_000
_MAX_RELAY_TOTAL_TEXT_ATTACHMENT_CHARS = 180_000
_MAX_PLUGIN_ICON_BYTES = 128 * 1024
_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS = 0.05
_NATIVE_STARTUP_WARMUP_LIMIT = 2
_NATIVE_STARTUP_WARMUP_TAIL_LINES = 500
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
    ".svg": "image/svg+xml; charset=utf-8",
}
_STATIC_CSS_BUNDLES = {
    "native_index_bundle.css": (
        "base.css",
        "animations.css",
        "effects.css",
        "native_index.css",
    ),
    "native_app_bundle.css": (
        "base.css",
        "animations.css",
        "effects.css",
        "components.css",
    ),
}
_RELAY_MARVIS_CSS_HREF = "/static/relay_marvis.css?v=20260629-confirmation-provenance"
_RELAY_MOBILE_JS_HREF = "/static/relay_mobile.js?v=20260701-mobile-web"
_RELAY_ACTIVITY_DISPLAY_TZ = timezone(timedelta(hours=8))

_NATIVE_APP_HEAD = """  <link rel="manifest" href="/native/manifest.webmanifest">
  <meta name="theme-color" content="#000000">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="WLCodex">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">"""


def _relay_mobile_web_head(title: str) -> str:
    return f"""  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
  <meta name="color-scheme" content="light only">
  <meta name="theme-color" content="#FAF8F5">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="{_RELAY_MARVIS_CSS_HREF}">
  <script src="{_RELAY_MOBILE_JS_HREF}" defer></script>"""

# Inline SVG icons — 24×24 viewBox, currentColor, Lucide-style (stroke-width 2)
_ICON_ATTRS = 'width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
_ICON_SVG = {
    # Navigation
    "back": f'<svg {_ICON_ATTRS}><path d="M15 18l-6-6 6-6"/></svg>',
    "chevron": f'<svg {_ICON_ATTRS}><path d="M9 18l6-6-6-6"/></svg>',
    # Actions
    "menu": '<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg>',
    "attach": f'<svg {_ICON_ATTRS}><path d="M12 5v14"/><path d="M5 12h14"/></svg>',
    "image": f'<svg {_ICON_ATTRS}><path d="M18 22H4a2 2 0 0 1-2-2V6"/><path d="m22 13-1.3-1.3a2.4 2.4 0 0 0-3.4 0L11 18"/><circle cx="12" cy="8" r="2"/><rect x="6" y="2" width="16" height="16" rx="2"/></svg>',
    "checklist": f'<svg {_ICON_ATTRS}><path d="m3 7 2 2 4-4"/><path d="m3 17 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/></svg>',
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
    "▧": _ICON_SVG["image"],
    "☷": _ICON_SVG["checklist"],
    "×": _ICON_SVG["remove"],
    "↑": _ICON_SVG["send"],
    "■": _ICON_SVG["stop"],
    "✓": _ICON_SVG["check"],
}

# ICONS object injected into <script> blocks for dynamic JS icon use
_ICONS_JS_LITERAL = (
    "const ICONS={" + ",".join(f"{key}:{json.dumps(svg)}" for key, svg in _ICON_SVG.items()) + "};"
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


def _static_css_bundle(relative: str) -> bytes:
    names = _STATIC_CSS_BUNDLES[relative]
    chunks: list[bytes] = []
    for name in names:
        chunks.append(f"/* {name} */\n".encode("utf-8"))
        chunks.append((_STATIC_ASSET_DIR / name).read_bytes())
        chunks.append(b"\n")
    return b"\n".join(chunks)


async def _close_stream_writer(writer: Any, *, timeout: float = 0.5) -> None:
    with suppress(ConnectionError, RuntimeError, OSError):
        writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
    except (asyncio.TimeoutError, ConnectionError, RuntimeError, OSError):
        pass


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
        native_timeline: NativeTimelineStore | None = None,
        workflow_service: Any = None,
        relay_service: Any = None,
        workspace_catalog: Any = None,
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
        self._native_timeline = native_timeline
        self._workflow_service = workflow_service
        self._relay_service = relay_service
        self._workspace_catalog = tuple(workspace_catalog or ())
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
        self._relay_dispatch_tasks: set[asyncio.Task[None]] = set()
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
        self._schedule_native_startup_warmup()

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
        tasks.extend(task for task in self._council_run_tasks if task is not asyncio.current_task())
        tasks.extend(task for task in self._relay_dispatch_tasks if task is not asyncio.current_task())
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
            await self._handle_client_request(reader, writer)
        finally:
            await _close_stream_writer(writer)
            if task is not None:
                self._client_tasks.discard(task)

    async def _handle_client_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task: asyncio.Task[None] | None = None
        try:
            request_line = await asyncio.wait_for(
                reader.readline(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if not request_line:
                await _close_stream_writer(writer)
                return
            method, target, version = (
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

            if parsed.path == "/native/codex-v2":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                if not self._is_authorized(
                    writer,
                    headers,
                    query,
                    require_token=self._native_controller is not None,
                ):
                    await self._send_html(writer, 401, _native_token_entry_page("/native/codex-v2"))
                    return
                await self._send_native_timeline_v2_page(writer, "codex", headers, query)
                return

            if parsed.path in (
                "/native/workflows",
                "/native/workflows/relay",
                "/native/workflows/relay/chat",
                "/native/workflows/relay/config",
                "/native/workflows/relay/office",
                "/native/workflows/relay/skills",
                "/native/workflows/relay/profile",
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

            native_messages_route = _native_messages_route_from_path(parsed.path)
            if native_messages_route is not None:
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                if not self._is_authorized(writer, headers, query):
                    await self._send_json(writer, 401, {"error": "unauthorized"})
                    return
                provider, native_thread_id, stream = native_messages_route
                if stream:
                    after_update = _safe_int(
                        query.get(
                            "after_update",
                            query.get("after", [headers.get("last-event-id", "0")]),
                        )[0],
                        default=0,
                    )
                    await self._send_native_messages_sse(
                        writer,
                        provider,
                        native_thread_id,
                        after_update,
                    )
                    return
                after = _safe_int(query.get("after", ["0"])[0], default=0)
                after_update = _safe_int(
                    query.get("after_update", ["0"])[0],
                    default=0,
                )
                before_value = query.get("before", [""])[0]
                before = (
                    _safe_int(before_value, default=0)
                    if str(before_value).strip()
                    else None
                )
                limit = _safe_int(query.get("limit", ["100"])[0], default=100)
                await self._send_native_messages_json(
                    writer,
                    provider,
                    native_thread_id,
                    after=after,
                    after_update=after_update,
                    before=before,
                    limit=limit,
                )
                return

            native_timeline_route = _native_timeline_route_from_path(parsed.path)
            if native_timeline_route is not None:
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                if not self._is_authorized(writer, headers, query):
                    await self._send_json(writer, 401, {"error": "unauthorized"})
                    return
                provider, native_thread_id, stream = native_timeline_route
                if stream:
                    after = _safe_int(
                        query.get("after", [headers.get("last-event-id", "0")])[0],
                        default=0,
                    )
                    await self._send_native_timeline_sse(
                        writer,
                        provider,
                        native_thread_id,
                        after,
                    )
                    return
                after = _safe_int(query.get("after", ["0"])[0], default=0)
                before_value = query.get("before", [""])[0]
                before = (
                    _safe_int(before_value, default=0)
                    if str(before_value).strip()
                    else None
                )
                limit = _safe_int(query.get("limit", ["100"])[0], default=100)
                await self._send_native_timeline_json(
                    writer,
                    provider,
                    native_thread_id,
                    after=after,
                    before=before,
                    limit=limit,
                    item_snapshot=True,
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
                        self._native_registry is not None or self._native_controller is not None
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
                parsed.path.startswith("/api/workers/") or parsed.path.startswith("/workers/")
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
                native_thread_id = (
                    _optional_nonempty_string(query.get("native_thread_id", [""])[0]) or ""
                )
                native_provider = (
                    _optional_nonempty_string(query.get("native_provider", [""])[0]) or "codex"
                )
                native_provider_key = native_provider.strip().lower() or "codex"
                native_turn_id = (
                    _optional_nonempty_string(query.get("native_turn_id", [""])[0]) or ""
                )
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
                native_turn_id = _optional_nonempty_string(query.get("native_turn_id", [""])[0])
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
                    current_turn_id=_optional_nonempty_string(query.get("current_turn_id", [""])[0])
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
                theme = _optional_nonempty_string(query.get("theme", [""])[0]) or ""
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
                native_thread_id = (
                    _optional_nonempty_string(query.get("native_thread_id", [""])[0]) or ""
                )
                native_provider = (
                    _optional_nonempty_string(query.get("native_provider", [""])[0]) or "codex"
                )
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
        tail_lines: int | None = None,
        include_provider: bool = True,
    ) -> str:
        if not native_thread_id:
            return ""
        errors: list[str] = []
        if self._native_transcript_mirror is not None:
            try:
                if tail_lines is None:
                    self._native_transcript_mirror.sync_thread(native_thread_id)
                else:
                    self._native_transcript_mirror.sync_thread(
                        native_thread_id,
                        tail_lines=tail_lines,
                    )
            except Exception as exc:
                errors.append(str(exc) or type(exc).__name__)
        provider_name = native_provider.strip().lower() or "codex"
        if include_provider and provider_name == "codex":
            provider = self._native_provider("codex")
            if provider is not None:
                try:
                    await provider.sync_session(native_thread_id)
                except Exception as exc:
                    errors.append(str(exc) or type(exc).__name__)
        return "; ".join(error for error in errors if error)

    async def _sync_native_timeline_transcript_if_needed(
        self,
        provider: str,
        native_thread_id: str,
        *,
        force: bool = False,
    ) -> str:
        provider_name = provider.strip().lower() or "codex"
        if provider_name != "codex" or self._native_transcript_mirror is None:
            return ""
        if not native_thread_id:
            return ""
        signature_key = (provider_name, native_thread_id)
        signature = self._native_transcript_file_signature(
            provider_name,
            native_thread_id,
        )
        if (
            not force
            and signature
            and signature == self._native_transcript_file_signatures.get(signature_key)
        ):
            return ""
        sync_error = await self._sync_native_transcript(
            native_thread_id,
            native_provider=provider_name,
            include_provider=False,
        )
        refreshed_signature = self._native_transcript_file_signature(
            provider_name,
            native_thread_id,
        )
        if refreshed_signature or signature:
            self._native_transcript_file_signatures[signature_key] = (
                refreshed_signature or signature
            )
        error_key = ("native_transcript", provider_name, native_thread_id)
        if sync_error:
            self._native_background_errors[error_key] = sync_error
        else:
            self._native_background_errors.pop(error_key, None)
        return sync_error

    def _schedule_native_timeline_transcript_sync_if_needed(
        self,
        provider: str,
        native_thread_id: str,
        *,
        force: bool = False,
    ) -> bool:
        provider_name = provider.strip().lower() or "codex"
        if provider_name != "codex" or self._native_transcript_mirror is None:
            return False
        if not native_thread_id:
            return False
        signature_key = (provider_name, native_thread_id)
        signature = self._native_transcript_file_signature(
            provider_name,
            native_thread_id,
        )
        if (
            not force
            and signature
            and signature == self._native_transcript_file_signatures.get(signature_key)
        ):
            return False
        task_key = ("native_transcript", provider_name, native_thread_id)
        existing = self._native_background_tasks.get(task_key)
        if existing is not None and not existing.done():
            return True

        async def sync() -> None:
            try:
                await asyncio.sleep(_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS)
                await self._sync_native_timeline_transcript_if_needed(
                    provider_name,
                    native_thread_id,
                    force=force,
                )
            except Exception as exc:
                self._native_background_errors[task_key] = (
                    str(exc) or "native transcript sync failed"
                )
            finally:
                if self._native_background_tasks.get(task_key) is task:
                    self._native_background_tasks.pop(task_key, None)

        task = asyncio.create_task(sync())
        self._native_background_tasks[task_key] = task
        return True

    def _schedule_native_startup_warmup(self) -> bool:
        if self._native_transcript_mirror is None:
            return False
        recent_threads = getattr(
            self._native_transcript_mirror,
            "recent_turn_thread_ids",
            None,
        )
        if recent_threads is None:
            return False
        provider_name = "codex"
        key = ("native_transcript_warmup", provider_name)
        existing = self._native_background_tasks.get(key)
        if existing is not None and not existing.done():
            return True

        async def warmup() -> None:
            try:
                await asyncio.sleep(_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS)
                thread_ids = recent_threads(limit=_NATIVE_STARTUP_WARMUP_LIMIT)
                if asyncio.iscoroutine(thread_ids):
                    thread_ids = await thread_ids
                for native_thread_id in list(thread_ids or [])[:_NATIVE_STARTUP_WARMUP_LIMIT]:
                    thread_id = str(native_thread_id or "").strip()
                    if not thread_id:
                        continue
                    if self._native_transcript_task_running(provider_name, thread_id):
                        continue
                    sync_error = await self._sync_native_transcript(
                        thread_id,
                        native_provider=provider_name,
                        tail_lines=_NATIVE_STARTUP_WARMUP_TAIL_LINES,
                        include_provider=False,
                    )
                    error_key = ("native_transcript", provider_name, thread_id)
                    if sync_error:
                        self._native_background_errors[error_key] = sync_error
                    else:
                        self._native_background_errors.pop(error_key, None)
                self._native_background_errors.pop(key, None)
            except Exception as exc:
                self._native_background_errors[key] = str(exc) or "native warmup failed"
            finally:
                if self._native_background_tasks.get(key) is task:
                    self._native_background_tasks.pop(key, None)

        task = asyncio.create_task(warmup())
        self._native_background_tasks[key] = task
        return True

    def _native_transcript_task_running(
        self,
        provider_name: str,
        native_thread_id: str,
    ) -> bool:
        for key, task in self._native_background_tasks.items():
            if task.done() or len(key) < 3:
                continue
            if not str(key[0]).startswith("native_transcript"):
                continue
            if key[1] != provider_name or key[2] != native_thread_id:
                continue
            return True
        return False

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

    async def _read_cached_native_session(
        self,
        target: Any,
        native_thread_id: str,
    ) -> dict[str, Any] | None:
        cached = getattr(target, "read_cached_session", None)
        if cached is not None:
            try:
                result = cached(native_thread_id)
                if asyncio.iscoroutine(result):
                    result = await result
                payload = _json_object(result)
                payload.setdefault("native_session_source", "cache")
                return payload
            except KeyError:
                return None
        for session in await self._list_cached_native_sessions(target):
            payload = _json_object(session)
            session_thread_id = str(
                payload.get("native_thread_id")
                or payload.get("native_session_id")
                or payload.get("id")
                or ""
            )
            if session_thread_id != native_thread_id:
                continue
            return _native_cached_session_detail(payload)
        return None

    def _schedule_native_session_refresh(
        self,
        provider_name: str,
        target: Any,
        native_thread_id: str,
    ) -> bool:
        key = ("native_session", provider_name, native_thread_id)
        existing = self._native_background_tasks.get(key)
        if existing is not None and not existing.done():
            return True

        async def refresh() -> None:
            try:
                await asyncio.sleep(_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS)
                result = target.read_session(native_thread_id)
                if asyncio.iscoroutine(result):
                    await result
                self._native_background_errors.pop(key, None)
            except Exception as exc:
                self._native_background_errors[key] = str(exc) or "native session sync failed"
            finally:
                if self._native_background_tasks.get(key) is task:
                    self._native_background_tasks.pop(key, None)

        task = asyncio.create_task(refresh())
        self._native_background_tasks[key] = task
        return True

    async def _native_session_payload(
        self,
        provider_name: str,
        target: Any,
        native_thread_id: str,
    ) -> dict[str, Any]:
        key = ("native_session", provider_name, native_thread_id)
        native_sync_error = self._native_background_errors.get(key, "")
        cached = await self._read_cached_native_session(target, native_thread_id)
        if cached is not None:
            payload = dict(cached)
            payload["native_sync_pending"] = self._schedule_native_timeline_transcript_sync_if_needed(
                provider_name,
                native_thread_id,
            )
            if native_sync_error:
                payload["native_sync_error"] = native_sync_error
            return payload
        try:
            result = await asyncio.wait_for(
                target.read_session(native_thread_id),
                timeout=self._native_sessions_timeout_seconds,
            )
            payload = _json_object(result)
            payload.setdefault("native_session_source", "daemon")
            payload["native_sync_pending"] = False
            self._native_background_errors.pop(key, None)
            return payload
        except KeyError:
            raise
        except (asyncio.TimeoutError, JsonRpcTimeout) as exc:
            native_sync_error = str(exc) or "native session sync timed out"
        except Exception as exc:
            native_sync_error = str(exc) or "native session sync failed"
        payload = {
            "native_thread_id": native_thread_id,
            "native_session_source": "stub",
            "native_sync_error": native_sync_error,
            "native_sync_pending": self._schedule_native_session_refresh(
                provider_name,
                target,
                native_thread_id,
            ),
            "thread": {"id": native_thread_id, "threadId": native_thread_id},
        }
        return payload

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
                self._native_background_errors[key] = str(exc) or "native sessions sync failed"
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
                    if signature == self._native_session_file_signatures.get(provider_name):
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
                    if signature == self._native_transcript_file_signatures.get(signature_key):
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
                self._native_background_errors[key] = str(exc) or "native transcript sync failed"
            finally:
                if self._native_background_tasks.get(key) is task:
                    self._native_background_tasks.pop(key, None)

        task = asyncio.create_task(sync())
        self._native_background_tasks[key] = task
        return True

    async def _relay_task_detail_after_reconcile(self, task_id: int) -> Any:
        if self._relay_service is None:
            raise KeyError(f"unknown relay task id: {task_id}")
        await self._relay_service.ensure_task_lifecycle_current(task_id)
        return self._relay_service.get_task(task_id)

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
            relay_config = self._relay_service.config() if self._relay_service is not None else {}
            token_stats = (
                self._relay_service.today_token_stats() if self._relay_service is not None else {}
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
            "/native/workflows/relay/skills",
            "/native/workflows/relay/profile",
        ):
            project_rows = _relay_project_rows(self._workspace_catalog)
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
        if path in ("/native/workflows/relay/skills", "/native/workflows/relay/profile"):
            await self._send_html(
                writer,
                200,
                _relay_construction_page(
                    active="skills" if path.endswith("/skills") else "profile",
                    selected_workspace=selected_workspace,
                    access_token=token,
                ),
            )
            return
        if path == "/native/workflows/relay":
            page = _relay_page_number(str((query.get("page") or ["1"])[0] or "1"))
            summaries = (
                self._relay_service.list_tasks(workspace=selected_workspace)
                if self._relay_service is not None
                else []
            )
            providers = (
                self._native_registry.list_provider_summaries()
                if self._native_registry is not None
                else []
            )
            relay_config = self._relay_service.config() if self._relay_service is not None else {}
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
                    page=page,
                ),
            )
            return
        if path == "/native/workflows/relay/config":
            providers = (
                self._native_registry.list_provider_summaries()
                if self._native_registry is not None
                else []
            )
            relay_config = self._relay_service.config() if self._relay_service is not None else {}
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
            detail = await self._relay_task_detail_after_reconcile(task_id)
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
                token_stats=self._relay_service.task_token_stats(task_id),
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
                            {str(role): str(provider) for role, provider in assignments.items()}
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
                        workspace=_optional_nonempty_string((query.get("workspace") or [""])[0]),
                        status=_optional_nonempty_string((query.get("status") or [""])[0]),
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
                        images=_safe_image_attachments(body.get("images")),
                        files=_safe_relay_file_attachments(body.get("files")),
                        execution_mode=str(body.get("execution_mode") or "simple"),
                        execution_goal=str(body.get("execution_goal") or ""),
                        allow_subagents=str(body.get("allow_subagents") or "auto"),
                        team_strategy=str(body.get("team_strategy") or "none"),
                    )
                    await self._relay_service.dispatch_role(task.id, "director")
                    detail = await self._relay_task_detail_after_reconcile(task.id)
                    await self._send_json(
                        writer,
                        200,
                        {"task": detail.task.to_dict()},
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
                detail = await self._relay_task_detail_after_reconcile(task_id)
                await self._send_json(writer, 200, detail.to_dict())
                return
            if suffix == "/events":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                after = _safe_int(query.get("after", ["0"])[0], default=0)
                live = "text/event-stream" in headers.get("accept", "").lower()
                relay_event_queue = self._relay_service.subscribe_events(task_id) if live else None
                try:
                    await self._relay_service.ensure_task_lifecycle_current(task_id)
                    runtime_store = getattr(self._hub, "_store", None)
                    if runtime_store is not None and hasattr(
                        self._relay_service,
                        "scan_active_native_runtime_events",
                    ):
                        await self._relay_service.scan_active_native_runtime_events(runtime_store)
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
            if suffix == "/inputs":
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                await self._relay_service.ensure_task_lifecycle_current(task_id)
                result = await self._relay_service.queue_or_followup_user_input(
                    task_id,
                    str(body.get("text") or body.get("prompt") or ""),
                    images=_safe_image_attachments(body.get("images")),
                    files=_safe_relay_file_attachments(body.get("files")),
                )
                await self._send_json(writer, 200, result)
                return
            if suffix.startswith("/inputs/"):
                parts = [part for part in suffix.strip("/").split("/") if part]
                if len(parts) != 3 or parts[0] != "inputs" or not parts[1].isdigit():
                    await self._send_json(writer, 404, {"error": "not found"})
                    return
                pending_id = int(parts[1])
                action = parts[2]
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                if action == "steer":
                    await self._relay_service.ensure_task_lifecycle_current(task_id)
                    try:
                        payload = await self._relay_service.steer_active_attempt_payload(
                            task_id,
                            pending_id,
                        )
                    except (KeyError, ValueError, RuntimeError) as exc:
                        await self._send_json(writer, 400, {"error": str(exc)})
                        return
                    await self._send_json(writer, 200, {"pending_input": payload})
                    return
                if action == "cancel":
                    try:
                        pending = self._relay_service.cancel_pending_input(task_id, pending_id)
                    except (KeyError, ValueError) as exc:
                        await self._send_json(writer, 400, {"error": str(exc)})
                        return
                    await self._send_json(writer, 200, {"pending_input": pending.to_dict()})
                    return
                await self._send_json(writer, 404, {"error": "not found"})
                return
            if suffix.startswith("/rounds/"):
                parts = [part for part in suffix.strip("/").split("/") if part]
                if len(parts) != 3 or parts[0] != "rounds" or not parts[1].isdigit() or parts[2] != "control":
                    await self._send_json(writer, 404, {"error": "not found"})
                    return
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                await self._relay_service.ensure_task_lifecycle_current(task_id)
                try:
                    result = await self._relay_service.apply_round_control(
                        task_id,
                        int(parts[1]),
                        decision=str(body.get("decision") or ""),
                        artifact_id=_safe_int(str(body.get("artifact_id") or "0"), default=0),
                        comment=str(body.get("comment") or ""),
                        selected_option_id=str(body.get("selected_option_id") or ""),
                        selected_option_label=str(body.get("selected_option_label") or ""),
                        selected_option_instruction=str(
                            body.get("selected_option_instruction") or ""
                        ),
                        dispatch_next=False,
                    )
                except (KeyError, ValueError) as exc:
                    await self._send_json(writer, 400, {"error": str(exc)})
                    return
                next_role = str(result.get("next_role") or result.get("role") or "").strip()
                if next_role:
                    self._schedule_relay_dispatch(task_id, next_role)
                await self._send_json(writer, 200, {"control": result})
                return
            if suffix == "/sessions":
                if method != "GET":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                detail = await self._relay_task_detail_after_reconcile(task_id)
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
                await self._relay_service.ensure_task_lifecycle_current(task_id)
                await self._relay_service.add_user_message(
                    task_id,
                    str(body.get("text") or body.get("prompt") or ""),
                    images=_safe_image_attachments(body.get("images")),
                    files=_safe_relay_file_attachments(body.get("files")),
                )
                await self._send_json(
                    writer,
                    200,
                    (await self._relay_task_detail_after_reconcile(task_id)).to_dict(),
                )
                return
            if suffix == "/resume":
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                await self._relay_service.ensure_task_lifecycle_current(task_id)
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
                    (await self._relay_task_detail_after_reconcile(task_id)).to_dict(),
                )
                return
            if suffix == "/interrupt":
                if method != "POST":
                    await self._send_json(writer, 405, {"error": "method not allowed"})
                    return
                body = await self._read_request_json(writer, reader, headers)
                if body is None:
                    return
                await self._relay_service.ensure_task_lifecycle_current(task_id)
                await self._relay_service.interrupt(
                    task_id,
                    role=_optional_nonempty_string(body.get("role")),
                )
                await self._send_json(
                    writer,
                    200,
                    (await self._relay_task_detail_after_reconcile(task_id)).to_dict(),
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
            parts = [unquote(part) for part in route[len(approval_prefix) :].split("/") if part]
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
                session = await self._native_session_payload(
                    provider_name,
                    target,
                    thread_id,
                )
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
            force_new_turn = body.get("force_new_turn") is True or body.get("forceNewTurn") is True
            continue_kwargs: dict[str, Any] = {}
            if force_new_turn:
                continue_kwargs["force_new_turn"] = True
            images = _safe_image_attachments(body.get("images"))
            try:
                result = await target.continue_session(
                    thread_id,
                    str(body.get("prompt", "")),
                    model=_optional_nonempty_string(body.get("model")),
                    effort=_optional_nonempty_string(body.get("effort")),
                    service_tier=_optional_nonempty_string(
                        body.get("service_tier") or body.get("serviceTier")
                    ),
                    images=images,
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
            expected_turn_id = str(body.get("expected_turn_id") or body.get("turn_id") or "")
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
                self._native_registry is not None or self._native_controller is not None
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
                self._native_registry is not None or self._native_controller is not None
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
            await self._send_json(
                writer,
                200,
                _council_projects_payload(workspaces=self._workspace_catalog),
            )
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

    def _schedule_relay_dispatch(self, task_id: int, role: str) -> None:
        if self._relay_service is None or not role:
            return

        async def dispatch() -> None:
            try:
                await self._relay_service.dispatch_role(task_id, role)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                marker = getattr(self._relay_service, "_mark_role_blocked", None)
                if callable(marker):
                    marker(
                        task_id,
                        role,
                        reason=f"round control dispatch failed: {exc}",
                    )

        task = asyncio.create_task(dispatch())
        self._relay_dispatch_tasks.add(task)
        task.add_done_callback(self._relay_dispatch_tasks.discard)

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
        payload["diversity"] = council_assignment_diversity(config.assignments).to_json_dict()
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
                self._native_registry is not None or self._native_controller is not None
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
        await self._send_html(
            writer,
            200,
            render_native_template(
                provider_name,
                "stable",
                {
                    "theme": theme,
                    "stable_renderer": _native_codex_page,
                },
            ),
        )

    async def _send_native_timeline_v2_page(
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
                _native_token_entry_page(f"/native/{safe_provider}-v2"),
            )
            return
        native_thread_id = _optional_nonempty_string(
            query.get("native_thread_id", [""])[0]
            or query.get("thread_id", [""])[0]
        ) or ""
        initial_events: list[NativeTimelineEvent] = []
        if native_thread_id and self._native_timeline is not None:
            initial_events = self._native_timeline.list_item_events(
                provider_name,
                native_thread_id,
                limit=100,
            )
        await self._send_html(
            writer,
            200,
            render_native_template(
                provider_name,
                "timeline_v2",
                {
                    "native_thread_id": native_thread_id,
                    "initial_events": initial_events,
                },
            ),
        )

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
                return {key: values[-1] if values else "" for key, values in parsed.items()}
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

    async def _send_native_timeline_json(
        self,
        writer: asyncio.StreamWriter,
        provider: str,
        native_thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
        item_snapshot: bool = False,
    ) -> None:
        provider_key = provider.strip().lower() or "codex"
        sync_error = self._native_background_errors.get(
            ("native_transcript", provider_key, native_thread_id),
            "",
        )
        sync_pending = self._schedule_native_timeline_transcript_sync_if_needed(
            provider_key,
            native_thread_id,
        )
        if self._native_timeline is None:
            await self._send_json(
                writer,
                200,
                {
                    "provider": provider,
                    "native_thread_id": native_thread_id,
                    "events": [],
                    "native_sync_error": sync_error,
                    "native_sync_pending": sync_pending,
                },
            )
            return
        if item_snapshot:
            events = self._native_timeline.list_item_events(
                provider,
                native_thread_id,
                after=after,
                before=before,
                limit=limit,
            )
        else:
            events = self._native_timeline.list_events(
                provider,
                native_thread_id,
                after=after,
                before=before,
                limit=limit,
            )
        first_sequence = events[0].sequence if events else int(before or after or 0)
        previous_event_count = (
            (
                self._native_timeline.count_item_events_before(
                    provider,
                    native_thread_id,
                    before=first_sequence,
                )
                if item_snapshot
                else self._native_timeline.count_events_before(
                    provider,
                    native_thread_id,
                    before=first_sequence,
                )
            )
            if first_sequence
            else 0
        )
        display_events = [_native_timeline_display_event(event) for event in events]
        timeline_summary = _native_timeline_display_summary(display_events)
        await self._send_json(
            writer,
            200,
            {
                "provider": provider,
                "native_thread_id": native_thread_id,
                "events": display_events,
                "previous_event_count": previous_event_count,
                "native_sync_error": sync_error,
                "native_sync_pending": sync_pending,
                **timeline_summary,
            },
        )

    async def _send_native_timeline_sse(
        self,
        writer: asyncio.StreamWriter,
        provider: str,
        native_thread_id: str,
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
        if self._native_timeline is None:
            return
        self._schedule_native_timeline_transcript_sync_if_needed(
            provider,
            native_thread_id,
        )
        latest = after_id
        queue = self._native_timeline.subscribe(
            provider=provider,
            native_thread_id=native_thread_id,
        )
        try:
            for event in self._native_timeline.list_item_events(
                provider,
                native_thread_id,
                after=after_id,
                limit=500,
            ):
                if event.sequence <= latest:
                    continue
                latest = event.sequence
                if not _is_visible_native_timeline_event(event):
                    continue
                await _write_native_timeline_sse(writer, event)
            while not writer.is_closing():
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=_NATIVE_TRANSCRIPT_WATCH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await self._sync_native_timeline_transcript_if_needed(
                        provider,
                        native_thread_id,
                    )
                    continue
                if event.sequence <= latest:
                    continue
                latest = event.sequence
                if not _is_visible_native_timeline_event(event):
                    continue
                await _write_native_timeline_sse(writer, event)
        finally:
            self._native_timeline.unsubscribe(
                provider=provider,
                native_thread_id=native_thread_id,
                queue=queue,
            )

    async def _send_native_messages_json(
        self,
        writer: asyncio.StreamWriter,
        provider: str,
        native_thread_id: str,
        *,
        after: int = 0,
        after_update: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> None:
        provider_key = provider.strip().lower() or "codex"
        sync_error = self._native_background_errors.get(
            ("native_transcript", provider_key, native_thread_id),
            "",
        )
        sync_pending = self._schedule_native_timeline_transcript_sync_if_needed(
            provider_key,
            native_thread_id,
        )
        if self._native_timeline is None:
            await self._send_json(
                writer,
                200,
                {
                    "provider": provider_key,
                    "native_thread_id": native_thread_id,
                    "items": [],
                    "cursor": 0,
                    "item_cursor": 0,
                    "update_cursor": 0,
                    "previous_item_count": 0,
                    "run_state": _native_messages_run_state([]),
                    "native_sync_error": sync_error,
                    "native_sync_pending": sync_pending,
                },
            )
            return
        if after_update > 0 and before is None:
            item_events = self._native_timeline.list_item_events(
                provider_key,
                native_thread_id,
                after=after_update,
                limit=limit,
            )
            items = []
            seen_item_ids: set[int] = set()
            for event in item_events:
                item = self._native_timeline.get_conversation_item(event.item_row_id)
                if item is None or item.id in seen_item_ids:
                    continue
                seen_item_ids.add(item.id)
                items.append(item)
        else:
            items = self._native_timeline.list_conversation_items_by_id(
                provider_key,
                native_thread_id,
                after=after,
                before=before,
                limit=limit,
            )
        first_cursor = items[0].id if items else int(before or after or 0)
        previous_item_count = (
            self._native_timeline.count_conversation_items_before_id(
                provider_key,
                native_thread_id,
                before=first_cursor,
            )
            if first_cursor
            else 0
        )
        item_cursor = max([int(after or 0), *(item.id for item in items)])
        update_cursor_values = [int(after_update or 0), *(int(item.cursor or 0) for item in items)]
        if not after_update:
            update_cursor_values.append(
                self._native_timeline.latest_sequence(provider_key, native_thread_id)
            )
        update_cursor = max(update_cursor_values)
        run_state = self._native_timeline.latest_turn_run_state(
            provider_key,
            native_thread_id,
        )
        if not run_state.get("active") and run_state.get("status") == "idle":
            run_state = _native_messages_run_state(items)
        await self._send_json(
            writer,
            200,
            {
                "provider": provider_key,
                "native_thread_id": native_thread_id,
                "items": [_native_conversation_item_json(item) for item in items],
                "cursor": item_cursor,
                "item_cursor": item_cursor,
                "update_cursor": update_cursor,
                "previous_item_count": previous_item_count,
                "run_state": run_state,
                "native_sync_error": sync_error,
                "native_sync_pending": sync_pending,
            },
        )

    async def _send_native_messages_sse(
        self,
        writer: asyncio.StreamWriter,
        provider: str,
        native_thread_id: str,
        after_update: int,
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
        if self._native_timeline is None:
            return
        provider_key = provider.strip().lower() or "codex"
        self._schedule_native_timeline_transcript_sync_if_needed(
            provider_key,
            native_thread_id,
        )
        latest = int(after_update or 0)
        queue = self._native_timeline.subscribe(
            provider=provider_key,
            native_thread_id=native_thread_id,
        )
        try:
            for event in self._native_timeline.list_item_events(
                provider_key,
                native_thread_id,
                after=after_update,
                limit=500,
            ):
                if event.sequence <= latest:
                    continue
                item = self._native_timeline.get_conversation_item(event.item_row_id)
                if item is None:
                    latest = event.sequence
                    continue
                latest = event.sequence
                await _write_native_message_sse(
                    writer,
                    item,
                    replay=True,
                    update_cursor=event.sequence,
                )
            while not writer.is_closing():
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=_NATIVE_TRANSCRIPT_WATCH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await self._sync_native_timeline_transcript_if_needed(
                        provider_key,
                        native_thread_id,
                    )
                    for event in self._native_timeline.list_item_events(
                        provider_key,
                        native_thread_id,
                        after=latest,
                        limit=500,
                    ):
                        if event.sequence <= latest:
                            continue
                        item = self._native_timeline.get_conversation_item(
                            event.item_row_id
                        )
                        if item is None:
                            latest = event.sequence
                            continue
                        latest = event.sequence
                        await _write_native_message_sse(
                            writer,
                            item,
                            replay=True,
                            update_cursor=event.sequence,
                        )
                    continue
                item = self._native_timeline.get_conversation_item(event.item_row_id)
                if event.sequence <= latest:
                    continue
                if item is None:
                    latest = event.sequence
                    continue
                latest = event.sequence
                await _write_native_message_sse(
                    writer,
                    item,
                    replay=False,
                    update_cursor=event.sequence,
                )
        finally:
            self._native_timeline.unsubscribe(
                provider=provider_key,
                native_thread_id=native_thread_id,
                queue=queue,
            )

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
        if relative in _STATIC_CSS_BUNDLES:
            await _send_response(
                writer,
                200,
                "text/css; charset=utf-8",
                _static_css_bundle(relative),
                extra_headers={"Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
            )
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
            extra_headers={"Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
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
            if native_thread_id and self._hub.subscriber_count(agent_run_id=agent_run_id) == 0:
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
    try:
        writer.write(header.encode("utf-8") + body)
        await writer.drain()
    finally:
        await _close_stream_writer(writer)


async def _write_sse(writer: asyncio.StreamWriter, event: WorkerStreamEvent) -> None:
    writer.write(format_sse_event(event))
    await writer.drain()


async def _write_native_timeline_sse(
    writer: asyncio.StreamWriter,
    event: NativeTimelineEvent,
) -> None:
    writer.write(format_native_timeline_sse_event(event))
    await writer.drain()


async def _write_native_message_sse(
    writer: asyncio.StreamWriter,
    item: NativeTimelineItem,
    *,
    replay: bool,
    update_cursor: int | None = None,
) -> None:
    writer.write(
        format_native_message_sse_event(
            item,
            replay=replay,
            update_cursor=update_cursor,
        )
    )
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


def _relay_active_worker_jobs(detail: Any | None) -> list[Any]:
    if detail is None:
        return []
    active_statuses = {"queued", "streaming", "waiting"}
    jobs: list[Any] = []
    for job in getattr(detail, "role_jobs", []) or []:
        if getattr(job, "agent_run_id", None) is None:
            continue
        status = str(getattr(job, "status", "") or "").strip().lower()
        if status in active_statuses or bool(getattr(job, "turn_running", False)):
            jobs.append(job)
    return jobs


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
    initial_events = list(events[:_RELAY_SSE_INITIAL_EVENT_LIMIT])
    remaining_event_count = max(0, len(events) - len(initial_events))
    for event in initial_events:
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
    if remaining_event_count:
        await _write_relay_sse_payload(
            writer,
            event_id=str(max(seen_relay_sequences) if seen_relay_sequences else ""),
            event_type="timeline.truncated",
            payload={
                "event_type": "timeline.truncated",
                "remaining": remaining_event_count,
                "limit": _RELAY_SSE_INITIAL_EVENT_LIMIT,
            },
        )
    role_jobs = _relay_active_worker_jobs(detail)
    task_id = int(getattr(getattr(detail, "task", None), "id", 0) or 0)
    latest_by_agent: dict[int, int] = {}
    for event in initial_events:
        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_payload = dict(payload.get("payload") or {})
        agent_run_id = int(event_payload.get("agent_run_id") or 0)
        runtime_event_id = int(event_payload.get("runtime_event_id") or 0)
        if agent_run_id and runtime_event_id:
            latest_by_agent[agent_run_id] = max(
                latest_by_agent.get(agent_run_id, 0),
                runtime_event_id,
            )
    if hub is not None:
        for job in role_jobs:
            agent_run_id = int(job.agent_run_id)
            after_id = latest_by_agent.get(agent_run_id, 0)
            for worker_event in hub.snapshot(
                agent_run_id=agent_run_id,
                after_id=after_id,
                limit=_RELAY_SSE_INITIAL_EVENT_LIMIT,
            ):
                latest_by_agent[agent_run_id] = max(
                    latest_by_agent.get(agent_run_id, 0),
                    int(worker_event.id),
                )
                relay_event = _relay_worker_payload(
                    int(detail.task.id),
                    str(job.role),
                    worker_event,
                )
                if relay_event is None:
                    continue
                worker_event_type, _payload = relay_event
                worker_key = (
                    str(job.role),
                    int(worker_event.id),
                    worker_event_type,
                )
                if worker_key in seen_worker_events:
                    continue
                seen_worker_events.add(worker_key)
                relay_sequence = await _write_relay_worker_event(
                    writer,
                    task_id=int(detail.task.id),
                    role=str(job.role),
                    worker_event=worker_event,
                    relay_service=relay_service,
                )
                if relay_sequence:
                    seen_relay_sequences.add(relay_sequence)
    if live and relay_service is not None and task_id:
        queue = relay_event_queue or relay_service.subscribe_events(task_id)
        worker_subscriptions: dict[int, tuple[Any, asyncio.Queue[WorkerStreamEvent]]] = {}
        pending: dict[asyncio.Task[Any], tuple[str, Any, Any]] = {
            asyncio.create_task(queue.get()): ("relay", None, queue)
        }

        async def sync_worker_subscriptions(current_detail: Any | None = None) -> None:
            if hub is None:
                return
            refreshed = current_detail
            if refreshed is None:
                try:
                    refreshed = relay_service.get_task(task_id)
                except Exception:
                    refreshed = None
            for refreshed_job in _relay_active_worker_jobs(refreshed):
                agent_run_id_value = getattr(refreshed_job, "agent_run_id", None)
                if agent_run_id_value is None:
                    continue
                agent_run_id = int(agent_run_id_value)
                latest_by_agent.setdefault(agent_run_id, 0)
                for worker_event in hub.snapshot(
                    agent_run_id=agent_run_id,
                    after_id=latest_by_agent.get(agent_run_id, 0),
                    limit=_RELAY_SSE_INITIAL_EVENT_LIMIT,
                ):
                    latest_by_agent[agent_run_id] = max(
                        latest_by_agent.get(agent_run_id, 0),
                        int(worker_event.id),
                    )
                    worker_key = (
                        str(getattr(refreshed_job, "role", "") or ""),
                        int(worker_event.id),
                        "role.native_event",
                    )
                    if worker_key in seen_worker_events:
                        continue
                    seen_worker_events.add(worker_key)
                    relay_sequence = await _write_relay_worker_event(
                        writer,
                        task_id=task_id,
                        role=str(getattr(refreshed_job, "role", "") or ""),
                        worker_event=worker_event,
                        relay_service=relay_service,
                    )
                    if relay_sequence:
                        seen_relay_sequences.add(relay_sequence)
                if agent_run_id in worker_subscriptions:
                    continue
                worker_queue = hub.subscribe(agent_run_id=agent_run_id)
                worker_subscriptions[agent_run_id] = (refreshed_job, worker_queue)
                pending[asyncio.create_task(worker_queue.get())] = (
                    "worker",
                    refreshed_job,
                    worker_queue,
                )

        await sync_worker_subscriptions(detail)
        try:
            while not writer.is_closing():
                done, _pending = await asyncio.wait(
                    pending,
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    await sync_worker_subscriptions()
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    continue
                for task in done:
                    source_kind, job, source_queue = pending.pop(task)
                    if source_kind == "relay":
                        event = task.result()
                        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
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
                                        str(payload.get("role") or event_payload.get("role") or ""),
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
                            await sync_worker_subscriptions()
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
                        relay_sequence = await _write_relay_worker_event(
                            writer,
                            task_id=int(detail.task.id),
                            role=str(job.role),
                            worker_event=worker_event,
                            relay_service=relay_service,
                        )
                        if relay_sequence:
                            seen_relay_sequences.add(relay_sequence)
                    pending[asyncio.create_task(source_queue.get())] = (
                        "worker",
                        job,
                        source_queue,
                    )
        finally:
            for task in pending:
                task.cancel()
            for agent_run_id, (_job, worker_queue) in worker_subscriptions.items():
                hub.unsubscribe(agent_run_id=agent_run_id, queue=worker_queue)
            relay_service.unsubscribe_events(task_id, queue)
    elif live and hub is not None and role_jobs:
        subscriptions: list[tuple[Any, asyncio.Queue[WorkerStreamEvent]]] = []
        pending: dict[
            asyncio.Task[WorkerStreamEvent], tuple[Any, asyncio.Queue[WorkerStreamEvent]]
        ] = {}
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
                            relay_service=relay_service,
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
    await _close_stream_writer(writer)


async def _write_relay_sse_payload(
    writer: asyncio.StreamWriter,
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    payload = _compact_relay_sse_payload(event_type, payload)
    writer.write(f"id: {event_id}\n".encode("utf-8"))
    writer.write(f"event: {event_type}\n".encode("utf-8"))
    writer.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
    await writer.drain()


def _compact_relay_sse_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event_type != "role.native_event":
        return payload
    compacted = dict(payload)
    nested = compacted.get("payload")
    if isinstance(nested, dict) and (
        "native_event" in nested or "payload" in nested or "runtime_event_id" in nested
    ):
        compacted["payload"] = _compact_relay_native_event_payload(nested)
        return compacted
    return _compact_relay_native_event_payload(compacted)


def _compact_relay_native_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    native_event = payload.get("native_event")
    native_payload = native_event.get("payload") if isinstance(native_event, dict) else None
    raw_payload = payload.get("payload")
    source_payload = raw_payload if isinstance(raw_payload, dict) else {}
    if isinstance(native_payload, dict):
        source_payload = {**native_payload, **source_payload}
    for key in (
        "role",
        "agent_run_id",
        "runtime_event_id",
        "round_id",
        "kind",
        "itemId",
        "item_id",
        "stream_key",
        "native_message_id",
        "message_id",
        "native_turn_id",
        "turnId",
        "turn_id",
        "active_turn_id",
    ):
        value = payload.get(key)
        if value in (None, "") and isinstance(native_event, dict):
            if key == "runtime_event_id":
                value = native_event.get("id")
            else:
                value = native_event.get(key)
        if value in (None, ""):
            value = source_payload.get(key)
        if value not in (None, ""):
            compacted[key] = value
    kind = str(compacted.get("kind") or "").strip()
    text = _relay_native_display_text(source_payload)
    if text:
        if kind in {"text_delta", "reasoning_delta", "command_output"}:
            compacted["delta"] = text
        else:
            compacted["text"] = text
    for key in (
        "status",
        "title",
        "command",
        "exit_code",
        "approval_id",
        "request_id",
        "codexRequestId",
        "provider",
    ):
        value = source_payload.get(key)
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            compacted.setdefault(key, value)
    return compacted


def _relay_native_display_text(payload: dict[str, Any]) -> str:
    for key in ("delta", "text", "summary", "content", "message", "output", "chunk", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


async def _write_relay_worker_event(
    writer: asyncio.StreamWriter,
    *,
    task_id: int,
    role: str,
    worker_event: WorkerStreamEvent,
    relay_service: Any | None = None,
) -> int:
    relay_event = _relay_worker_payload(task_id, role, worker_event)
    if relay_event is None:
        return 0
    event_type, payload = relay_event
    if relay_service is not None and hasattr(relay_service, "_events"):
        event = relay_service._events.emit(
            task_id,
            event_type,
            role=role,
            payload=payload,
        )
        relay_payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        sequence = int(relay_payload.get("sequence") or 0)
        await _write_relay_sse_payload(
            writer,
            event_id=str(sequence),
            event_type=event_type,
            payload=relay_payload,
        )
        return sequence
    await _write_relay_sse_payload(
        writer,
        event_id="",
        event_type=event_type,
        payload=payload,
    )
    return 0


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
    return any(part.strip().lower() == "chunked" for part in transfer_encoding.split(","))


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
    return f"id: {event.id}\nevent: {event.kind}\ndata: {payload}\n\n".encode("utf-8")


def format_native_timeline_sse_event(event: NativeTimelineEvent) -> bytes:
    payload = json.dumps(_native_timeline_display_event(event), ensure_ascii=False)
    return (
        f"id: {event.sequence}\nevent: {event.kind}\ndata: {payload}\n\n"
    ).encode("utf-8")


def format_native_message_sse_event(
    item: NativeTimelineItem,
    *,
    replay: bool = False,
    update_cursor: int | None = None,
) -> bytes:
    public_cursor = int(update_cursor or item.cursor or item.id)
    payload = json.dumps(
        {
            "item": _native_conversation_item_json(item),
            "event": _native_conversation_item_display_event(item),
            "cursor": public_cursor,
            "item_cursor": int(item.id),
            "update_cursor": public_cursor,
            "replay": replay,
        },
        ensure_ascii=False,
    )
    return (
        f"id: {public_cursor}\n"
        f"event: {_native_message_sse_event_name(item, replay=replay)}\n"
        f"data: {payload}\n\n"
    ).encode("utf-8")


def _native_message_sse_event_name(item: NativeTimelineItem, *, replay: bool = False) -> str:
    if replay:
        return "message_added"
    if item.kind == "message" and item.status == "completed":
        return "message_completed"
    if item.kind == "message":
        return "message_updated"
    return "message_added"


def _native_conversation_item_json(item: NativeTimelineItem) -> dict[str, Any]:
    data = item.to_json_dict()
    data["sequence_cursor"] = item.cursor
    data["cursor"] = int(item.id)
    payload = dict(data.get("payload") or {})
    payload.pop("delta", None)
    payload["text"] = item.text
    data["payload"] = payload
    return data


def _native_conversation_item_display_event(item: NativeTimelineItem) -> dict[str, Any]:
    payload = dict(item.payload)
    payload["text"] = item.text
    payload.setdefault("itemId", item.item_key)
    payload.setdefault("item_id", item.item_key)
    payload.setdefault("native_turn_id", item.turn_key)
    payload["status"] = item.status
    payload["role"] = item.role
    payload["message_snapshot"] = True
    kind = item.kind
    if item.kind == "message":
        kind = "message_completed" if item.status == "completed" else "text_delta"
    return {
        "id": int(item.id),
        "sequence": item.cursor,
        "type": kind,
        "source_type": "native.conversation.item",
        "kind": kind,
        "role": item.role,
        "visible": True,
        "provider": item.provider,
        "native_thread_id": item.native_thread_id,
        "occurred_at": item.updated_at,
        "payload": payload,
    }


def _native_messages_run_state(items: list[NativeTimelineItem]) -> dict[str, Any]:
    active_statuses = {"queued", "streaming", "waiting", "pending", "running"}
    active_items = [
        item
        for item in items
        if str(item.status or "").strip().lower() in active_statuses
        and item.kind != "user_message"
    ]
    if not active_items:
        return {"active": False, "status": "idle", "active_turn_id": ""}
    latest = active_items[-1]
    return {
        "active": True,
        "status": str(latest.status or "streaming"),
        "active_turn_id": latest.turn_key,
        "item_id": latest.id,
    }


def _native_timeline_display_event(event: NativeTimelineEvent) -> dict[str, Any]:
    if hasattr(event, "to_display_json_dict"):
        return event.to_display_json_dict()
    data = event.to_json_dict()
    data.setdefault("source_type", data.get("type"))
    data["type"] = data.get("kind", data.get("type"))
    return data


def _is_visible_native_timeline_event(event: NativeTimelineEvent) -> bool:
    return bool(_native_timeline_display_event(event).get("visible"))


def _native_timeline_display_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    source_type_counts: dict[str, int] = {}
    hidden_by_reason: dict[str, int] = {}
    latest_sequence = 0
    latest_visible_sequence = 0
    visible_event_count = 0
    for event in events:
        sequence = _safe_int(event.get("sequence", event.get("id", 0)), default=0)
        latest_sequence = max(latest_sequence, sequence)
        source_type = str(event.get("source_type") or event.get("type") or "")
        if source_type:
            source_type_counts[source_type] = source_type_counts.get(source_type, 0) + 1
        if event.get("visible") is False:
            reason = str(event.get("hidden_reason") or "not_visible")
            hidden_by_reason[reason] = hidden_by_reason.get(reason, 0) + 1
            continue
        visible_event_count += 1
        latest_visible_sequence = max(latest_visible_sequence, sequence)
    return {
        "visible_event_count": visible_event_count,
        "hidden_event_count_by_reason": hidden_by_reason,
        "latest_sequence": latest_sequence,
        "latest_visible_sequence": latest_visible_sequence,
        "source_type_counts": source_type_counts,
    }


def _agent_id_from_path(path: str, *, prefix: str, suffix: str) -> int | None:
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    raw = path[len(prefix) : -len(suffix)]
    if not raw.isdigit():
        return None
    return int(raw)


def _native_timeline_route_from_path(path: str) -> tuple[str, str, bool] | None:
    prefix = "/api/native/"
    if not path.startswith(prefix):
        return None
    parts = [unquote(part) for part in path[len(prefix) :].split("/") if part]
    if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "timeline":
        return parts[0], parts[2], False
    if (
        len(parts) == 5
        and parts[1] == "sessions"
        and parts[3] == "timeline"
        and parts[4] == "stream"
    ):
        return parts[0], parts[2], True
    return None


def _native_messages_route_from_path(path: str) -> tuple[str, str, bool] | None:
    prefix = "/api/native/"
    if not path.startswith(prefix):
        return None
    parts = [unquote(part) for part in path[len(prefix) :].split("/") if part]
    if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "messages":
        return parts[0], parts[2], False
    if (
        len(parts) == 5
        and parts[1] == "sessions"
        and parts[3] == "messages"
        and parts[4] == "stream"
    ):
        return parts[0], parts[2], True
    return None


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
            {
                key: value
                for key, value in preset.items()
                if key not in {"dangerously_skip_permissions", "sandbox"}
            }
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
    if mode == "plan" and not (isinstance(existing_model, str) and existing_model.strip()):
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


def _codex_plugin_sort_key(name: str, manifest: Path) -> tuple[int, str, str]:
    preferred_order = {
        "documents": 0,
        "pdf": 1,
        "spreadsheets": 2,
        "presentations": 3,
        "template creator": 4,
        "browser": 5,
        "chrome": 6,
        "computer use": 7,
    }
    key = name.strip().lower()
    if key in preferred_order:
        return (preferred_order[key], key, str(manifest))
    path_parts = set(manifest.parts)
    if "openai-primary-runtime" in path_parts:
        source_rank = 20
    elif "openai-bundled" in path_parts:
        source_rank = 40
    elif "openai-curated" in path_parts or "openai-curated-remote" in path_parts:
        source_rank = 60
    else:
        source_rank = 80
    return (source_rank, key, str(manifest))


def _codex_plugin_menu_items() -> list[dict[str, str]]:
    cache_root = Path.home() / ".codex" / "plugins" / "cache"
    if not cache_root.exists():
        return []
    items: list[tuple[tuple[int, str, str], dict[str, str]]] = []
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
        item = {
            "name": name.strip(),
            "description": str(description).strip(),
            "brand_color": str(brand_color).strip(),
            "icon": icon,
        }
        items.append((_codex_plugin_sort_key(name, manifest), item))
    return [item for _, item in sorted(items, key=lambda pair: pair[0])[:12]]


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


def _safe_relay_file_attachments(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    files: list[dict[str, Any]] = []
    used_chars = 0
    for raw in value[:_MAX_RELAY_TEXT_ATTACHMENTS]:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        if not isinstance(text, str):
            continue
        remaining = _MAX_RELAY_TOTAL_TEXT_ATTACHMENT_CHARS - used_chars
        if remaining <= 0:
            break
        clipped = text[: min(_MAX_RELAY_TEXT_ATTACHMENT_CHARS, remaining)]
        used_chars += len(clipped)
        filename = raw.get("filename")
        clean: dict[str, Any] = {
            "filename": (
                filename.strip()[:160]
                if isinstance(filename, str) and filename.strip()
                else "attachment.txt"
            ),
            "text": clipped,
        }
        mime_type = raw.get("mime_type") or raw.get("mimeType")
        if isinstance(mime_type, str) and mime_type.strip():
            clean["mime_type"] = mime_type.strip()[:120]
        size = raw.get("size")
        if isinstance(size, int) and size >= 0:
            clean["size"] = size
        files.append(clean)
    return files or None


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


def _native_cached_session_detail(session: dict[str, Any]) -> dict[str, Any]:
    native_thread_id = str(
        session.get("native_thread_id")
        or session.get("native_session_id")
        or session.get("id")
        or ""
    )
    thread = session.get("thread")
    if not isinstance(thread, dict):
        thread = {}
    thread = dict(thread)
    if native_thread_id:
        thread.setdefault("id", native_thread_id)
        thread.setdefault("threadId", native_thread_id)
    for source_key, target_key in (
        ("title", "title"),
        ("name", "name"),
        ("cwd", "cwd"),
        ("workdir", "cwd"),
        ("source_kind", "sourceKind"),
        ("sourceKind", "sourceKind"),
        ("status", "status"),
        ("last_turn_id", "last_turn_id"),
        ("activity_at", "activity_at"),
        ("updated_at", "updated_at"),
        ("metadata", "metadata"),
    ):
        if target_key not in thread and source_key in session:
            thread[target_key] = session[source_key]
    return {
        "native_thread_id": native_thread_id,
        "agent_run_id": session.get("agent_run_id"),
        "native_session_source": "cache",
        "thread": thread,
    }


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
                metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
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
        f"/native/{quote(provider, safe='')}?native_thread_id={quote(native_session_id, safe='')}"
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


def _workspace_catalog_payload(workspaces: Any) -> dict[str, Any] | None:
    rows: list[dict[str, Any]] = []
    for workspace in workspaces or ():
        alias = str(getattr(workspace, "alias", "") or "").strip()
        path = getattr(workspace, "path", "")
        cwd = str(path or "").strip()
        if not alias or not cwd:
            continue
        rows.append(
            {
                "alias": alias,
                "name": alias,
                "cwd": cwd,
                "allow_write": bool(getattr(workspace, "allow_write", False)),
            }
        )
    if not rows:
        return None
    return {"root": "", "projects": rows}


def _council_projects_payload(
    projects_root: Path | None = None,
    *,
    workspaces: Any = None,
) -> dict[str, Any]:
    catalog = _workspace_catalog_payload(workspaces)
    if catalog is not None:
        return catalog
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
                f"<span>{escape(_native_provider_display_name(str(provider['provider'])))}</span>"
                f"<small>{escape(str(provider.get('provider_engine', '')))}</small>"
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
  <link rel="stylesheet" href="/static/native_index_bundle.css">
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
  <link rel="stylesheet" href="/static/native_index_bundle.css">
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
    page: int = 1,
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
        f"<span>{counts.get(status, 0)}</span></button>"
        for status in filters
    )
    page_size = 10
    task_count = len(sorted_summaries)
    total_pages = max(1, (task_count + page_size - 1) // page_size)
    current_page = min(max(1, int(page or 1)), total_pages)
    page_start = (current_page - 1) * page_size
    visible_summaries = sorted_summaries[page_start : page_start + page_size]
    pagination_html = _relay_task_pagination_html(
        current_page=current_page,
        total_pages=total_pages,
        selected_workspace=selected_workspace,
        access_token=access_token,
    )
    if visible_summaries:
        task_list_html = "\n".join(
            _relay_task_card_html(summary, token_suffix) for summary in visible_summaries
        )
    else:
        task_list_html = """
          <section class="relay-empty-state">
            <h2>还没有接力任务</h2>
            <p>创建一个大任务后，总工程师会先接收并调度架构、开发、测试和审计角色。</p>
            <p>当前工作区还没有接力任务，可以从底部导航的对话开始第一个任务。</p>
          </section>
        """
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
{_relay_mobile_web_head("流式接力")}
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
    .relay-task-card {{ display: grid; gap: 12px; border: 1px solid var(--border-card); border-radius: 8px; padding: 14px; background: var(--bg-surface); min-width: 0; overflow-wrap: anywhere; }}
    .relay-card-identity {{ display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 12px; align-items: center; min-width: 0; }}
    .relay-card-side {{ display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; color: var(--text-muted); font-size: 13px; line-height: 1.3; }}
    .relay-card-project-pill {{ display: inline-grid; place-items: center; min-height: 24px; border-radius: 999px; padding: 0 9px; color: #315e8d; background: #e8f1fb; border: 1px solid #d8e8f7; font-weight: var(--weight-bold); white-space: nowrap; }}
    .relay-card-activity {{ color: var(--text-muted); min-width: 0; }}
    .relay-card-avatar-row {{ display: flex; align-items: center; min-height: 42px; }}
    .relay-card-meta {{ display: flex; gap: 8px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }}
    .relay-card-open {{ white-space: nowrap; }}
    .relay-title {{ font-size: 17px; font-weight: var(--weight-bold); line-height: 1.35; }}
    .relay-muted {{ color: var(--text-muted); font-size: 13px; }}
    .relay-summary {{ color: var(--text-primary); font-size: 14px; line-height: 1.45; }}
    .relay-status-badge {{ display: inline-grid; place-items: center; min-height: 30px; border: 1px solid var(--border-subtle); border-radius: 999px; padding: 0 12px; font-size: 12px; line-height: 1; color: var(--text-primary); background: rgba(255,255,255,.05); white-space: nowrap; }}
    .relay-status-badge.is-completed {{ background: #f3eded; border-color: #efe3e3; color: #755f5f; }}
    .relay-pagination {{ display: flex; align-items: center; justify-content: center; gap: 10px; padding: 2px 0 0; }}
    .relay-page-link, .relay-page-disabled {{ min-height: 34px; display: inline-grid; place-items: center; border: 1px solid var(--border-subtle); border-radius: 999px; padding: 0 12px; font-size: 13px; text-decoration: none; }}
    .relay-page-link {{ color: #315e8d; background: #e8f1fb; border-color: #d8e8f7; font-weight: var(--weight-bold); }}
    .relay-page-disabled {{ color: #6f7782; background: #f0f2f5; border-color: #e0e4ea; opacity: 1; }}
    .relay-page-indicator {{ color: var(--text-muted); font-size: 13px; }}
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
        {pagination_html}
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
{_relay_mobile_web_head("Marvis 对话")}
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
    {_marvis_relay_attachment_script()}
    const TOKEN_SUFFIX = {json.dumps(token_suffix)};
    const marvisComposer = document.querySelector("[data-marvis-task-composer]");
    marvisComposer?.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const data = Object.fromEntries(new FormData(marvisComposer).entries());
      const attachments = window.marvisRelayAttachments?.payload() || {{}};
      const hasAttachments = Boolean((attachments.images || []).length || (attachments.files || []).length);
      const title = String(data.title || "").trim() || (hasAttachments ? "请查看附件" : "");
      if (!title) return;
      data.title = title;
      data.prompt = title;
      if (String(data.execution_mode || "") === "goal" && !String(data.execution_goal || "").trim()) {{
        data.execution_goal = title;
      }}
      if ((attachments.images || []).length) data.images = attachments.images;
      if ((attachments.files || []).length) data.files = attachments.files;
      const response = await fetch(`/api/relay/tasks${{TOKEN_SUFFIX}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(data),
      }});
      const payload = await response.json();
      if (payload?.task?.id) {{
        window.marvisRelayAttachments?.clear();
        window.location.href = `/native/workflows/relay/tasks/${{encodeURIComponent(payload.task.id)}}${{TOKEN_SUFFIX}}`;
      }}
    }});
  </script>
</body>
</html>""")


def _relay_construction_page(
    *,
    active: str,
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
          <a class="marvis-relay-icon-button" href="/native/workflows/relay{token_suffix}" aria-label="任务">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
          </a>
        """,
    )
    bottom_nav_html = _marvis_relay_bottom_nav(
        active,
        access_token=access_token,
        selected_workspace=selected_workspace,
    )
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
{_relay_mobile_web_head("正在建设中")}
</head>
<body data-marvis-relay-view="construction">
  <div class="marvis-relay-phone">
    {topbar_html}
    <main class="marvis-relay-construction" aria-label="正在建设中">
      <img src="/static/marvis/relay-under-construction.svg" alt="" aria-hidden="true">
      <h2>正在建设中</h2>
    </main>
    <nav class="marvis-relay-bottom-nav" aria-label="Marvis relay navigation">
      {bottom_nav_html}
    </nav>
  </div>
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
        rows.append(
            (
                cwd,
                str(project.get("name") or Path(cwd).name or cwd),
                cwd,
            )
        )
    if selected_workspace and selected_workspace not in seen:
        rows.insert(
            0,
            (
                selected_workspace,
                Path(selected_workspace).name or selected_workspace,
                selected_workspace,
            ),
        )
    links = "\n".join(
        '<a class="relay-workspace-link'
        f'{" active" if workspace == selected_workspace else ""}" '
        f'data-workspace-value="{escape(workspace)}" '
        f'href="{escape(_relay_workspace_href(workspace, access_token))}">'
        f"{escape(label)}</a>"
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


def _relay_page_number(raw_page: str) -> int:
    try:
        return max(1, int(raw_page))
    except (TypeError, ValueError):
        return 1


def _relay_task_list_href(workspace: str, access_token: str, page: int) -> str:
    params = []
    if access_token:
        params.append(f"token={quote(access_token)}")
    if workspace:
        params.append(f"workspace={quote(workspace)}")
    params.append(f"page={max(1, int(page))}")
    return f"/native/workflows/relay?{'&'.join(params)}"


def _relay_task_pagination_html(
    *,
    current_page: int,
    total_pages: int,
    selected_workspace: str,
    access_token: str,
) -> str:
    if total_pages <= 1:
        return ""
    previous_html: str
    next_html: str
    if current_page > 1:
        previous_html = (
            '<a class="relay-page-link" '
            f'href="{escape(_relay_task_list_href(selected_workspace, access_token, current_page - 1))}">'
            "上一页</a>"
        )
    else:
        previous_html = '<span class="relay-page-disabled" aria-disabled="true">上一页</span>'
    if current_page < total_pages:
        next_html = (
            '<a class="relay-page-link" '
            f'href="{escape(_relay_task_list_href(selected_workspace, access_token, current_page + 1))}">'
            "下一页</a>"
        )
    else:
        next_html = '<span class="relay-page-disabled" aria-disabled="true">下一页</span>'
    return f"""
      <nav class="relay-pagination" aria-label="任务分页">
        {previous_html}
        <span class="relay-page-indicator">第 {current_page} / {total_pages} 页</span>
        {next_html}
      </nav>
    """


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


def _relay_project_rows(workspaces: Any = None) -> list[dict[str, Any]]:
    payload = _council_projects_payload(workspaces=workspaces)
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
        f"{escape(_native_provider_display_name(str(assignment_map.get(role) or fallback)))}</span>"
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
        return f"Marvis 拍了拍 {to_name} 说， 别等了，这就开始"
    from_name = _marvis_relay_handoff_role_label(from_role)
    if to_role == "auditor":
        return f"{from_name}交给{to_name}复核"
    if from_role == "auditor" and to_role == "director":
        return f"{from_name}交回Marvis收尾"
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
    return replace_legacy_role_identifiers(text)


def _marvis_relay_role_status_label(status: str) -> str:
    value = str(status or "").strip()
    if value in {"passed", "completed", "success", "succeeded"}:
        return "已完成"
    if value in {"failed", "blocked", "error"}:
        return "调用失败"
    if value in {"queued", "streaming", "started", "progress"}:
        return "进行中"
    if value == "waiting_user":
        return "等待中"
    if value == "interrupted":
        return "已中断"
    return _relay_role_status_label(value) if value else "进行中"


def _marvis_relay_action_label(role: str, payload: dict[str, Any] | None = None) -> str:
    artifact_type = str((payload or {}).get("artifact_type") or "").strip()
    confirmation_source = str((payload or {}).get("confirmation_source") or "").strip()
    confirmation_label = _marvis_relay_confirmation_source_label(
        confirmation_source,
        str((payload or {}).get("provider") or ""),
    )
    if confirmation_label:
        return confirmation_label
    if role == "director":
        kind = str((payload or {}).get("kind") or "").strip()
        handoff_to = str((payload or {}).get("handoff_to") or "").strip()
        if (
            artifact_type == "routing_decision"
            or kind in {"text_delta", "waiting"}
            or (artifact_type == "final_summary" and handoff_to)
        ):
            return "任务分配"
        return ""
    artifact_labels = {
        "architecture_plan": "架构计划",
        "implementation_report": "执行反馈",
        "test_report": "测试反馈",
        "audit_report": "审核反馈",
    }
    if artifact_type in artifact_labels:
        return artifact_labels[artifact_type]
    if artifact_type:
        return (
            _marvis_relay_role_status_label(artifact_type)
            if artifact_type in {"passed", "failed", "blocked", "completed"}
            else artifact_type.replace("_", " ")
        )
    return "任务"


def _marvis_relay_confirmation_source_label(source: str, provider: str = "") -> str:
    clean_source = str(source or "").strip()
    provider_name = str(provider or "").strip().lower()
    if clean_source in {"provider_native_plan", "provider_native_approval"}:
        if provider_name == "codex":
            return "Codex 原生确认"
        if provider_name.startswith("claude"):
            return "Claude 原生确认"
        return "Provider 原生确认"
    if clean_source == "relay_prompt_fallback":
        return "Relay 澄清确认"
    return ""


def _marvis_relay_topbar(
    *,
    title: str = "Marvis",
    subtitle: str = "",
    back_href: str = "",
    right_html: str = "",
) -> str:
    left = (
        f'<a class="marvis-relay-menu is-back" href="{escape(back_href)}" aria-label="返回上一级">'
        "<span></span><span></span><span></span></a>"
        if back_href
        else '<button class="marvis-relay-menu" type="button" aria-label="菜单"><span></span><span></span><span></span></button>'
    )
    subtitle_html = (
        f'<div class="marvis-relay-device"><span class="marvis-relay-dot"></span>{escape(subtitle)}</div>'
        if subtitle
        else ""
    )
    actions = (
        right_html
        or """
      <button class="marvis-relay-icon-button" type="button" aria-label="设备">
        <span class="marvis-relay-icon-devices" aria-hidden="true"></span>
      </button>
      <button class="marvis-relay-icon-button" type="button" aria-label="任务">
        <span class="marvis-relay-icon-list" aria-hidden="true"></span>
      </button>
    """
    )
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
        "skills": f"/native/workflows/relay/skills{token_suffix}{workspace_query}",
        "profile": f"/native/workflows/relay/profile{token_suffix}{workspace_query}",
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
        rows.append(
            f"""
        <a class="{class_name}" href="{escape(hrefs[key])}" data-marvis-nav="{escape(key)}"{current}>
          {icon_html}
          <span>{escape(label)}</span>
        </a>
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
      <div class="marvis-relay-mode-strip" aria-label="接力模式">
        <label><input type="radio" name="execution_mode" value="simple" checked><span>简单</span></label>
        <label><input type="radio" name="execution_mode" value="plan_first"><span>计划</span></label>
        <label><input type="radio" name="execution_mode" value="goal"><span>目标</span></label>
        <label><input type="radio" name="execution_mode" value="auto"><span>自动</span></label>
        <input type="hidden" name="allow_subagents" value="off">
        <label><input type="checkbox" name="allow_subagents" value="auto" checked><span>使用子代理</span></label>
      </div>
      <button class="marvis-relay-plus" type="button" aria-label="添加" data-marvis-attach-open>+</button>
      <input name="title" autocomplete="off" placeholder="{escape(placeholder)}">
      <input type="hidden" name="prompt" value="">
      <input type="hidden" name="execution_goal" value="">
      <input type="hidden" name="workspace" value="{escape(selected_workspace)}">
      <button class="marvis-relay-submit" type="submit" aria-label="发送任务" data-marvis-submit>
        <span class="marvis-relay-submit-arrow" aria-hidden="true">↑</span>
        <span class="marvis-relay-submit-stop" aria-hidden="true">■</span>
      </button>
      <div class="marvis-relay-composer-attachments" data-marvis-attachment-strip hidden></div>
    </form>
    {_marvis_relay_attachment_sheet_html()}
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


def _marvis_relay_attachment_sheet_html() -> str:
    return """
    <div class="marvis-relay-attachment-backdrop" data-marvis-attachment-backdrop hidden></div>
    <section class="marvis-relay-attachment-sheet" data-marvis-attachment-sheet hidden aria-modal="true" role="dialog" aria-label="添加到对话">
      <button class="marvis-relay-attachment-close" type="button" aria-label="关闭" data-marvis-attachment-close>×</button>
      <h2>添加到对话</h2>
      <div class="marvis-relay-attachment-grid" aria-label="附件类型">
        <button class="marvis-relay-attachment-tile" type="button" data-marvis-pick-image>
          <img class="marvis-relay-sheet-icon-native marvis-relay-sheet-icon-native-small" src="/static/marvis/attachment-icon-album-marvis.png" alt="" aria-hidden="true">
          <span>相册</span>
        </button>
        <button class="marvis-relay-attachment-tile" type="button" data-marvis-pick-file>
          <img class="marvis-relay-sheet-icon-native marvis-relay-sheet-icon-native-small" src="/static/marvis/attachment-icon-local-file-marvis.png" alt="" aria-hidden="true">
          <span>本地文件</span>
        </button>
      </div>
      <div class="marvis-relay-skill-section">
        <p>我的技能</p>
        <button class="marvis-relay-skill-row" type="button" aria-label="添加技能">
          <img class="marvis-relay-sheet-icon-native marvis-relay-sheet-icon-native-skill" src="/static/marvis/attachment-icon-skills-marvis.png" alt="" aria-hidden="true">
          <span class="marvis-relay-skill-text">
            <strong>添加技能</strong>
            <small>有200+技能可供使用</small>
          </span>
          <span class="marvis-relay-skill-chevron" aria-hidden="true">›</span>
        </button>
      </div>
      <input type="file" accept="image/*" multiple hidden data-marvis-image-input>
      <input type="file" accept=".txt,.md,.markdown,.json,.jsonl,.log,.csv,.tsv,.yaml,.yml,.toml,.ini,.py,.js,.ts,.tsx,.jsx,.css,.html,.xml,.sh,.zsh,.sql,text/*,application/json" multiple hidden data-marvis-file-input>
    </section>
    """


def _marvis_relay_attachment_script() -> str:
    return r"""
    function setupMarvisRelayAttachments() {
      const composer = document.querySelector("[data-marvis-task-composer], [data-marvis-followup-composer]");
      const sheet = document.querySelector("[data-marvis-attachment-sheet]");
      if (!composer || !sheet) {
        return { payload: () => ({}), clear: () => {}, hasAttachments: () => false };
      }
      const backdrop = document.querySelector("[data-marvis-attachment-backdrop]");
      const openButton = composer.querySelector("[data-marvis-attach-open]");
      const closeButton = sheet.querySelector("[data-marvis-attachment-close]");
      const imageInput = sheet.querySelector("[data-marvis-image-input]");
      const fileInput = sheet.querySelector("[data-marvis-file-input]");
      const strip = composer.querySelector("[data-marvis-attachment-strip]");
      const state = { images: [], files: [] };
      const textFilePattern = /\.(txt|md|markdown|json|jsonl|log|csv|tsv|yaml|yml|toml|ini|py|js|ts|tsx|jsx|css|html|xml|sh|zsh|sql)$/i;
      function openSheet() {
        sheet.hidden = false;
        backdrop.hidden = false;
        requestAnimationFrame(() => {
          sheet.classList.add("open");
          backdrop.classList.add("visible");
        });
      }
      function closeSheet() {
        sheet.classList.remove("open");
        backdrop.classList.remove("visible");
        window.setTimeout(() => {
          sheet.hidden = true;
          backdrop.hidden = true;
        }, 180);
      }
      function readRelayImageAttachment(file) {
        return new Promise((resolve, reject) => {
          if (!file || !String(file.type || "").startsWith("image/")) {
            reject(new Error("请选择图片文件"));
            return;
          }
          const reader = new FileReader();
          reader.onload = () => resolve({
            filename: file.name || "image",
            mime_type: file.type || "image/*",
            size: file.size || 0,
            url: String(reader.result || "")
          });
          reader.onerror = () => reject(new Error("图片读取失败"));
          reader.readAsDataURL(file);
        });
      }
      function readRelayTextAttachment(file) {
        return new Promise((resolve, reject) => {
          if (!file) {
            reject(new Error("请选择文件"));
            return;
          }
          const mime = String(file.type || "");
          const name = String(file.name || "attachment.txt");
          if (mime && !mime.startsWith("text/") && mime !== "application/json" && !textFilePattern.test(name)) {
            reject(new Error(`${name} 暂只支持文本/代码文件`));
            return;
          }
          if (file.size > 1024 * 1024) {
            reject(new Error(`${name} 超过 1MB，请拆小后再上传`));
            return;
          }
          const reader = new FileReader();
          reader.onload = () => resolve({
            filename: name,
            mime_type: mime || "text/plain",
            size: file.size || 0,
            text: String(reader.result || "")
          });
          reader.onerror = () => reject(new Error("文件读取失败"));
          reader.readAsText(file);
        });
      }
      function renderRelayAttachmentStrip() {
        const hasImages = state.images.length > 0;
        const hasFiles = state.files.length > 0;
        composer?.classList.toggle("has-image-attachments", hasImages);
        if (!strip) return;
        strip.innerHTML = "";
        strip.hidden = !hasImages && !hasFiles;
        state.images.forEach((item, index) => {
          const preview = document.createElement("button");
          preview.type = "button";
          preview.className = "marvis-relay-composer-image-preview";
          preview.title = "移除图片";
          const img = document.createElement("img");
          img.src = item.url || "";
          img.alt = "";
          const remove = document.createElement("span");
          remove.className = "marvis-relay-composer-image-remove";
          remove.setAttribute("aria-hidden", "true");
          preview.append(img, remove);
          preview.addEventListener("click", () => {
            state.images.splice(index, 1);
            renderRelayAttachmentStrip();
          });
          strip.appendChild(preview);
        });
        state.files.forEach((item, index) => {
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "marvis-relay-composer-attachment is-file";
          chip.title = item.filename || "文件";
          chip.innerHTML = '<span class="marvis-relay-attachment-icon" aria-hidden="true"></span><span></span><b aria-hidden="true">&#215;</b>';
          chip.querySelector("span:nth-child(2)").textContent = item.filename || "文件";
          chip.addEventListener("click", () => {
            state.files.splice(index, 1);
            renderRelayAttachmentStrip();
          });
          strip.appendChild(chip);
        });
        document.dispatchEvent(new CustomEvent("marvis-relay-attachments-changed"));
      }
      function addErrorChip(message) {
        if (!strip || !message) return;
        strip.hidden = false;
        const chip = document.createElement("span");
        chip.className = "marvis-relay-composer-attachment is-error";
        chip.textContent = message;
        strip.appendChild(chip);
        window.setTimeout(() => {
          chip.remove();
          if (!strip.children.length) strip.hidden = true;
        }, 3500);
      }
      openButton?.addEventListener("click", openSheet);
      closeButton?.addEventListener("click", closeSheet);
      backdrop?.addEventListener("click", closeSheet);
      sheet.querySelector("[data-marvis-pick-image]")?.addEventListener("click", () => imageInput?.click());
      sheet.querySelector("[data-marvis-pick-file]")?.addEventListener("click", () => fileInput?.click());
      imageInput?.addEventListener("change", async () => {
        for (const file of Array.from(imageInput.files || [])) {
          try {
            state.images.push(await readRelayImageAttachment(file));
          } catch (error) {
            addErrorChip(error?.message || "图片读取失败");
          }
        }
        imageInput.value = "";
        renderRelayAttachmentStrip();
        closeSheet();
      });
      fileInput?.addEventListener("change", async () => {
        for (const file of Array.from(fileInput.files || [])) {
          try {
            state.files.push(await readRelayTextAttachment(file));
          } catch (error) {
            addErrorChip(error?.message || "文件读取失败");
          }
        }
        fileInput.value = "";
        renderRelayAttachmentStrip();
        closeSheet();
      });
      const api = {
        payload() {
          return {
            images: state.images.map((item) => ({...item})),
            files: state.files.map((item) => ({...item}))
          };
        },
        clear() {
          state.images = [];
          state.files = [];
          renderRelayAttachmentStrip();
        },
        hasAttachments() {
          return state.images.length > 0 || state.files.length > 0;
        }
      };
      window.readRelayImageAttachment = readRelayImageAttachment;
      window.readRelayTextAttachment = readRelayTextAttachment;
      window.marvisRelayAttachments = api;
      return api;
    }
    function appendMarvisAttachmentList(parent, attachments = {}) {
      if (!parent) return;
      const images = Array.isArray(attachments.images) ? attachments.images : [];
      const files = Array.isArray(attachments.files) ? attachments.files : [];
      if (!images.length && !files.length) return;
      const imageList = document.createElement("div");
      imageList.className = "marvis-relay-message-images";
      images.forEach((item) => {
        const src = String(item?.url || item?.data_url || "");
        if (!src) return;
        const image = document.createElement("img");
        image.className = "marvis-relay-message-image";
        image.src = src;
        image.alt = "";
        image.loading = "lazy";
        imageList.appendChild(image);
      });
      if (imageList.children.length) parent.appendChild(imageList);
      if (!files.length) return;
      const list = document.createElement("div");
      list.className = "marvis-relay-attachment-list";
      const addChip = (item) => {
        const chip = document.createElement("span");
        chip.className = "marvis-relay-attachment-chip marvis-relay-attachment-chip-file";
        chip.innerHTML = '<span class="marvis-relay-attachment-icon" aria-hidden="true"></span><span></span>';
        chip.querySelector("span:last-child").textContent = item.filename || "文件";
        list.appendChild(chip);
      };
      files.forEach((item) => addChip(item));
      parent.appendChild(list);
    }
    setupMarvisRelayAttachments();
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
                "display_name": str(entry.get("display_name") or _relay_role_label(role)),
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


def _marvis_office_token_details_html(stats: dict[str, Any]) -> str:
    agents = stats.get("agents")
    rows = agents if isinstance(agents, list) else []
    if not rows:
        return '<p class="marvis-token-details-empty">还没有记录到 Token 消耗。</p>'
    parts: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent") or "unknown")
        label = _native_provider_display_name(agent)
        today_tokens = _marvis_token_int(item.get("today_tokens"))
        total_tokens = _marvis_token_int(item.get("total_tokens"))
        input_tokens = _marvis_token_int(item.get("input_tokens"))
        cached_input_tokens = _marvis_token_int(item.get("cached_input_tokens"))
        output_tokens = _marvis_token_int(item.get("output_tokens"))
        reasoning_output_tokens = _marvis_token_int(item.get("reasoning_output_tokens"))
        detail_parts = [
            f"输入 {escape(_format_marvis_token_count(input_tokens))}",
        ]
        if cached_input_tokens:
            detail_parts.append(f"缓存 {escape(_format_marvis_token_count(cached_input_tokens))}")
        detail_parts.append(f"输出 {escape(_format_marvis_token_count(output_tokens))}")
        if reasoning_output_tokens:
            detail_parts.append(
                f"推理 {escape(_format_marvis_token_count(reasoning_output_tokens))}"
            )
        parts.append(
            f"""
            <article class="marvis-token-details-row" data-token-agent="{escape(agent)}">
              <strong>{escape(label)}</strong>
              <span>今日 {escape(_format_marvis_token_count(today_tokens))}</span>
              <span>总计 {escape(_format_marvis_token_count(total_tokens))}</span>
              <small>{" · ".join(detail_parts)}</small>
            </article>
            """
        )
    return "\n".join(parts) or '<p class="marvis-token-details-empty">还没有记录到 Token 消耗。</p>'


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
    token_details_html = _marvis_office_token_details_html(stats)
    assignment_map = config.get("assignments")
    assignments = assignment_map if isinstance(assignment_map, dict) else {}
    assignment_payload = {role: str(assignments.get(role) or "") for role in RELAY_ROLE_IDS}
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
        f"{escape(option['label'])}</button>"
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
        <button class="{slot_class}" type="button" data-marvis-office-role="{escape(role["role"])}" data-marvis-persona-open="{escape(role["role"])}" aria-label="打开{escape(role["display_name"])}人设">
          <span>{escape(role["display_name"])}</span>
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
{_relay_mobile_web_head("Marvis办公室")}
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
        <button type="button" class="marvis-office-token-card" data-marvis-token-details-open="today">
          <span>今日消耗Token</span>
          <strong data-token-consumed="{consumed_tokens}"><b data-token-consumed-label>{escape(consumed_label)}</b> <span class="marvis-office-token-coffee" aria-hidden="true">☕</span></strong>
        </button>
        <button type="button" class="marvis-office-token-card" data-marvis-token-details-open="total">
          <span>总消耗Token</span>
          <strong data-token-total="{total_consumed_tokens}"><b data-token-total-label>{escape(total_consumed_label)}</b> <span class="marvis-office-token-coffee" aria-hidden="true">☕</span></strong>
        </button>
      </section>
    </main>
    <div class="marvis-office-backdrop" data-marvis-persona-backdrop hidden></div>
    <div class="marvis-office-backdrop marvis-token-details-backdrop" data-marvis-token-details-backdrop hidden></div>
    <section class="marvis-token-details-sheet" data-marvis-token-details-modal hidden aria-modal="true" role="dialog" aria-label="Token明细">
      <button class="marvis-token-details-close" type="button" data-marvis-token-details-close aria-label="关闭">×</button>
      <h2>Token明细</h2>
      <p class="marvis-token-details-summary">按今天和累计消耗汇总，每条记录来自任务工作日志中的 usage 事件。</p>
      <div class="marvis-token-details-list" data-marvis-token-details-list>
        {token_details_html}
      </div>
    </section>
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
      const tokenDetails = document.querySelector("[data-marvis-token-details-modal]");
      const tokenDetailsBackdrop = document.querySelector("[data-marvis-token-details-backdrop]");
      const tokenDetailsList = document.querySelector("[data-marvis-token-details-list]");
      const tokenDetailsOpen = document.querySelectorAll("[data-marvis-token-details-open]");
      const tokenDetailsClose = document.querySelectorAll("[data-marvis-token-details-close]");
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
        if (tokenDetailsList && stats && Array.isArray(stats.agents)) {{
          tokenDetailsList.innerHTML = stats.agents.length ? stats.agents.map((item) => {{
            const agent = String(item.agent || "unknown");
            const label = providerLabel(agent);
            const today = Number(item.today_tokens || 0);
            const totalTokens = Number(item.total_tokens || 0);
            const input = Number(item.input_tokens || 0);
            const cached = Number(item.cached_input_tokens || 0);
            const output = Number(item.output_tokens || 0);
            const reasoning = Number(item.reasoning_output_tokens || 0);
            const detailParts = [`输入 ${{formatToken(input)}}`];
            if (cached > 0) detailParts.push(`缓存 ${{formatToken(cached)}}`);
            detailParts.push(`输出 ${{formatToken(output)}}`);
            if (reasoning > 0) detailParts.push(`推理 ${{formatToken(reasoning)}}`);
            return `<article class="marvis-token-details-row" data-token-agent="${{agent.replace(/"/g, "&quot;")}}">
              <strong>${{label}}</strong>
              <span>今日 ${{formatToken(today)}}</span>
              <span>总计 ${{formatToken(totalTokens)}}</span>
              <small>${{detailParts.join(" · ")}}</small>
            </article>`;
          }}).join("") : '<p class="marvis-token-details-empty">还没有记录到 Token 消耗。</p>';
        }}
      }};
      const setTokenDetailsOpen = (isOpen) => {{
        if (!tokenDetails || !tokenDetailsBackdrop) return;
        tokenDetails.hidden = !isOpen;
        tokenDetailsBackdrop.hidden = !isOpen;
        document.body.classList.toggle("marvis-token-details-open", isOpen);
      }};
      tokenDetailsOpen.forEach((button) => button.addEventListener("click", () => setTokenDetailsOpen(true)));
      tokenDetailsBackdrop?.addEventListener("click", () => setTokenDetailsOpen(false));
      tokenDetailsClose.forEach((button) => button.addEventListener("click", () => setTokenDetailsOpen(false)));
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
    current_round_id: int = 1,
    pending_inputs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> str:
    workspace_dock = _marvis_relay_workspace_dock(
        workspace,
        access_token=access_token,
    )
    pending_visible = bool(
        [
            item
            for item in (pending_inputs or [])
            if str(item.get("status") or "") in {"pending", "steered"}
        ]
    )
    return f"""
    {workspace_dock}
    <div class="marvis-relay-pending-inputs" data-marvis-pending-inputs{" hidden" if not pending_visible else ""}></div>
    <form class="marvis-relay-composer" data-marvis-followup-composer data-task-status-value="{escape(task_status)}" data-current-round-id="{int(current_round_id)}" data-interrupt-url="/api/relay/tasks/{task_id}/interrupt" method="post" action="/api/relay/tasks/{task_id}/message" onsubmit="return false">
      <button class="marvis-relay-plus" type="button" aria-label="添加" data-marvis-attach-open>+</button>
      <textarea name="text" placeholder="{escape(placeholder)}" aria-label="继续补充给总工程师"></textarea>
      <button class="marvis-relay-submit" type="submit" aria-label="发送补充" data-marvis-submit>
        <span class="marvis-relay-submit-arrow" aria-hidden="true">↑</span>
        <span class="marvis-relay-submit-stop" aria-hidden="true">■</span>
      </button>
      <div class="marvis-relay-composer-attachments" data-marvis-attachment-strip hidden></div>
    </form>
    {_marvis_relay_attachment_sheet_html()}
    """


def _marvis_relay_plan_control_html(detail: Any) -> str:
    if str(getattr(getattr(detail, "task", None), "status", "") or "") != "waiting_user":
        return ""
    round_id = int(getattr(detail, "current_round_id", 1) or 1)
    round_execution = getattr(detail, "round_execution", {}) or {}
    confirmation = round_execution.get("confirmation") if isinstance(round_execution, dict) else {}
    if not isinstance(confirmation, dict):
        confirmation = {}
    waiting_artifact: dict[str, Any] | None = None
    for artifact in reversed(getattr(detail, "artifacts", []) or []):
        if str(artifact.get("artifact_type") or "") != "architecture_plan":
            continue
        if int(artifact.get("round_id") or round_id) != round_id:
            continue
        if str(artifact.get("status") or "") != "waiting":
            continue
        waiting_artifact = artifact
        break
    if waiting_artifact is None:
        for artifact in reversed(getattr(detail, "artifacts", []) or []):
            if int(artifact.get("round_id") or round_id) != round_id:
                continue
            if str(artifact.get("status") or "") != "waiting":
                continue
            waiting_artifact = artifact
            break
    if waiting_artifact is None:
        waiting_artifact = {
            "id": 0,
            "artifact_type": "",
            "status": "waiting",
            "summary": "当前接力需要你确认下一步。",
            "open_questions": [],
            "confirmation_options": [],
        }
    confirmation_source = str(
        waiting_artifact.get("confirmation_source")
        or confirmation.get("source")
        or ""
    )
    confirmation_provider = str(
        waiting_artifact.get("provider")
        or confirmation.get("provider")
        or ""
    )
    confirmation_kind = str(
        waiting_artifact.get("confirmation_kind")
        or confirmation.get("kind")
        or ""
    )
    waiting_reason = str(
        waiting_artifact.get("waiting_reason")
        or round_execution.get("waiting_reason", "")
        if isinstance(round_execution, dict)
        else ""
    )
    source_label = str(waiting_artifact.get("confirmation_source_label") or "").strip()
    if not source_label:
        if confirmation_source in {"provider_native_plan", "provider_native_approval"}:
            if confirmation_provider == "codex":
                source_label = "Codex 原生确认"
            elif confirmation_provider.startswith("claude"):
                source_label = "Claude 原生确认"
            else:
                source_label = "Provider 原生确认"
        elif confirmation_source == "relay_prompt_fallback" or not confirmation_source:
            source_label = "Relay 澄清确认"
    artifact_type = str(waiting_artifact.get("artifact_type") or "")
    is_plan = artifact_type == "architecture_plan"
    artifact_id = int(waiting_artifact.get("id") or 0)
    title = "计划等待确认" if is_plan else "等待确认"
    primary_label = "执行计划" if is_plan else "选择执行"
    primary_decision = "approve_plan" if is_plan else "continue"
    summary = str(waiting_artifact.get("summary") or "当前接力需要你确认下一步。").strip()
    questions = [
        str(item).strip()
        for item in list(waiting_artifact.get("open_questions") or [])
        if str(item).strip()
    ]
    options = _marvis_relay_confirmation_options(
        waiting_artifact.get("confirmation_options")
    )
    option_html = "".join(
        f"""
        <button class="marvis-relay-confirmation-option" type="button"
          data-confirmation-option-id="{escape(option["id"])}"
          data-confirmation-option-label="{escape(option["label"])}"
          data-confirmation-option-instruction="{escape(option["instruction"])}"
          aria-pressed="{'true' if index == 0 else 'false'}">
          <strong>{escape(option["label"])}</strong>
          <span>{escape(option["summary"] or option["instruction"])}</span>
        </button>
        """
        for index, option in enumerate(options)
    )
    question_html = "".join(
        f"<li>{escape(question)}</li>" for question in questions
    )
    detail_body = summary
    if questions:
        detail_body = f"{summary}\n\n待确认：\n" + "\n".join(f"- {item}" for item in questions)
    meta_parts = [
        f"来源：{source_label}",
        f"请求类型：{confirmation_kind or 'relay_question'}",
    ]
    if waiting_reason:
        meta_parts.append(f"等待原因：{waiting_reason}")
    provider_request_id = str(
        waiting_artifact.get("provider_request_id")
        or confirmation.get("provider_request_id")
        or ""
    )
    if provider_request_id:
        meta_parts.append(f"请求 ID：{provider_request_id}")
    meta_text = "\n".join(meta_parts)
    return f"""
    <section class="marvis-relay-confirmation-card" data-marvis-confirmation-card data-marvis-plan-control data-round-id="{round_id}" data-artifact-id="{artifact_id}" aria-label="{escape(title)}">
      <button class="marvis-relay-confirmation-thumb" type="button" data-marvis-confirmation-open aria-label="查看确认详情">
        <em>{escape(source_label)}</em>
        <span>{escape(title)}</span>
        <strong>{escape(summary)}</strong>
      </button>
      <div class="marvis-relay-confirmation-options"{" hidden" if not option_html else ""}>
        {option_html}
      </div>
      <div class="marvis-relay-confirmation-actions">
        <button type="button" data-plan-decision="{escape(primary_decision)}">{escape(primary_label)}</button>
        <button type="button" data-waiting-input>补充内容</button>
        <button type="button" data-plan-decision="cancel_plan">停止</button>
      </div>
    </section>
    <div class="marvis-relay-confirmation-page" data-marvis-confirmation-page hidden>
      <div class="marvis-relay-confirmation-page-shell">
        <header>
          <button type="button" data-marvis-confirmation-close aria-label="返回">‹</button>
          <strong>{escape(title)}</strong>
        </header>
        <main>
          <small>{escape(meta_text)}</small>
          <h2>{escape(summary)}</h2>
          <p>{escape(detail_body)}</p>
          <ul{" hidden" if not question_html else ""}>{question_html}</ul>
        </main>
      </div>
    </div>
    """


def _marvis_relay_confirmation_options(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for index, item in enumerate(raw[:6], start=1):
        if isinstance(item, str):
            label = item.strip()
            summary = ""
            instruction = label
            option_id = f"option_{index}"
        elif isinstance(item, dict):
            option_id = str(item.get("id") or f"option_{index}").strip()
            label = str(
                item.get("label")
                or item.get("title")
                or item.get("name")
                or item.get("summary")
                or option_id
            ).strip()
            summary = str(item.get("summary") or item.get("description") or "").strip()
            instruction = str(
                item.get("instruction")
                or item.get("prompt")
                or item.get("value")
                or item.get("text")
                or label
            ).strip()
        else:
            continue
        if not label and not instruction:
            continue
        options.append(
            {
                "id": option_id or f"option_{index}",
                "label": label or instruction,
                "summary": summary,
                "instruction": instruction or label,
            }
        )
    return options


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
    for (
        _occurred_at,
        _event_id,
        _role,
        _display_name,
        worker_event,
    ) in _relay_worker_events_for_roles(
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
    for (
        _occurred_at,
        event_id,
        _role,
        _display_name,
        _worker_event,
    ) in _relay_worker_events_for_roles(
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
    return "\n".join(rows)


def _marvis_relay_work_log_segments(
    detail: Any,
    *,
    hub: WorkerLiveStreamHub | None,
    canonical_payloads: dict[str, dict[str, Any]] | None = None,
) -> list[WorkLogSegment]:
    canonical_payloads = canonical_payloads or {}
    role_errors = _marvis_relay_role_error_payloads_by_role(getattr(detail, "artifacts", []) or [])
    invalid_artifacts = _marvis_relay_invalid_artifact_payloads_by_role(
        getattr(detail, "artifacts", []) or []
    )
    dispatch_payloads = _marvis_relay_dispatch_payloads_by_role(
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
        and _marvis_relay_work_log_text_is_protocol_noise(_relay_native_event_text(worker_event))
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

    artifact_keys_added: set[str] = set()
    for segment in segments:
        payload = canonical_payloads.get(segment.role) or artifact_payloads.get(segment.role)
        fallback_payload = artifact_payloads.get(segment.role)
        if (
            payload is not None
            and fallback_payload is not None
            and _marvis_relay_work_log_text_is_protocol_noise(
                str(payload.get("summary") or payload.get("output") or "")
            )
        ):
            payload = fallback_payload
        if payload is None:
            continue
        key = f"artifact:{segment.role}"
        if key in artifact_keys_added:
            continue
        segment.entries.append(
            WorkLogEntry(
                kind="artifact",
                key=key,
                text=_relay_humanize_role_envelope(payload),
                chip=f"{_marvis_relay_action_label(segment.role, payload)} {_marvis_relay_role_status_label(str(payload.get('status') or 'passed'))}",
            )
        )
        artifact_keys_added.add(key)

    for role, payload in dispatch_payloads.items():
        decision_text = _marvis_relay_dispatch_decision_text(payload)
        if not decision_text:
            continue
        append_entry(
            role,
            WorkLogEntry(
                kind="dispatch",
                key=f"dispatch:{payload.get('id') or role}",
                text=decision_text,
                chip="调度",
            ),
        )

    round_execution = getattr(detail, "round_execution", {}) or {}
    confirmation = (
        round_execution.get("confirmation")
        if isinstance(round_execution, dict)
        else {}
    )
    if not isinstance(confirmation, dict):
        confirmation = {}
    confirmation_source = str(confirmation.get("source") or "").strip()
    if (
        str(getattr(getattr(detail, "task", None), "status", "") or "") == "waiting_user"
        and confirmation_source
    ):
        role = str(confirmation.get("role") or "").strip()
        if role not in RELAY_ROLE_IDS:
            role = next(
                (
                    str(getattr(job, "role", "") or "")
                    for job in getattr(detail, "role_jobs", []) or []
                    if str(getattr(job, "status", "") or "") == "waiting"
                ),
                "director",
            )
        label = _marvis_relay_confirmation_source_label(
            confirmation_source,
            str(confirmation.get("provider") or ""),
        ) or "等待确认"
        kind = str(confirmation.get("kind") or "relay_question")
        provider_request_id = str(confirmation.get("provider_request_id") or "")
        waiting_reason = str(round_execution.get("waiting_reason") or "")
        text_parts = [f"来源：{label}", f"请求类型：{kind}"]
        if waiting_reason:
            text_parts.append(f"等待原因：{waiting_reason}")
        if provider_request_id:
            text_parts.append(f"请求 ID：{provider_request_id}")
        append_entry(
            role,
            WorkLogEntry(
                kind="confirmation",
                key=f"confirmation:{getattr(detail, 'current_round_id', '')}:{role}:{confirmation_source}:{provider_request_id}",
                text="\n".join(text_parts),
                chip=label,
            ),
        )

    _marvis_relay_finalize_work_log_segments(segments)
    existing_roles = {segment.role for segment in segments}
    for job in detail.role_jobs:
        role = str(getattr(job, "role", "") or "")
        if role not in RELAY_ROLE_IDS:
            continue
        role_success_payload = canonical_payloads.get(role) or artifact_payloads.get(role)
        has_lifecycle_success = _relay_payload_status_is_success(
            str((role_success_payload or {}).get("status") or "")
        )
        if role in existing_roles or f"artifact:{role}" in artifact_keys_added:
            payload = None
        else:
            payload = role_success_payload
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
        if error_message and (role_error or not has_lifecycle_success):
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
        invalid_payload = invalid_artifacts.get(role) or {}
        if invalid_payload:
            append_entry(
                role,
                WorkLogEntry(
                    kind="artifact_invalid",
                    key=f"artifact_invalid:{invalid_payload.get('id') or role}",
                    text="结构化产物未采用，自动流转暂停。已保留 provider 原始可见输出。",
                    chip="等待修正",
                    output=str(invalid_payload.get("error") or ""),
                ),
            )
            existing_roles.add(role)
        if role not in existing_roles and status and status not in {"idle", "passed", "completed"}:
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


def _marvis_relay_dispatch_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        payload = dict(artifact or {})
        if str(payload.get("artifact_type") or "") != "role_dispatch_metadata":
            continue
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if role:
            payloads[role] = payload
    return payloads


def _marvis_relay_dispatch_decision_text(payload: dict[str, Any]) -> str:
    provider_mode = payload.get("provider_mode")
    if not isinstance(provider_mode, dict):
        provider_mode = {}
    provider_mode_key = str(provider_mode.get("provider_mode") or "default")
    decision = provider_mode.get("subagent_decision_json")
    if not isinstance(decision, dict):
        decision = {}
    if provider_mode_key == "default" and not decision:
        return ""
    mode_label = {
        "codex_plan": "Codex plan",
        "claude_plan": "Claude plan",
        "prompt_plan_fallback": "Plan fallback",
        "prompt_goal_contract": "Goal contract",
        "default": "默认调度",
    }.get(provider_mode_key, provider_mode_key)
    allow_subagents = str(provider_mode.get("allow_subagents") or "auto")
    subagent_label = "子代理关闭" if allow_subagents == "off" else "子代理自动"
    capability = str(decision.get("capability") or "").strip()
    provider = str(payload.get("provider") or decision.get("provider") or "").strip()
    parts = [mode_label, subagent_label]
    if capability:
        parts.append(capability)
    if provider:
        parts.append(provider)
    return " · ".join(_relay_humanize_display_text(part) for part in parts if part)


def _marvis_relay_role_error_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    errors: dict[str, dict[str, Any]] = {}
    success_roles_by_round: dict[str, set[str]] = {}
    for artifact in artifacts:
        payload = dict(artifact or {})
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if not role:
            continue
        round_id = str(payload.get("round_id") or "")
        success_roles = success_roles_by_round.setdefault(round_id, set())
        artifact_type = str(payload.get("artifact_type") or "")
        if artifact_type == "role_error":
            errors[role] = payload
            continue
        normalized_status = _relay_lifecycle_status_for_payload(payload, success_roles)
        if _relay_payload_status_is_success(normalized_status):
            success_roles.add(role)
            errors.pop(role, None)
    return errors


def _marvis_relay_invalid_artifact_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    success_roles_by_round: dict[str, set[str]] = {}
    for artifact in artifacts:
        payload = dict(artifact or {})
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if not role:
            continue
        round_id = str(payload.get("round_id") or "")
        success_roles = success_roles_by_round.setdefault(round_id, set())
        artifact_type = str(payload.get("artifact_type") or "")
        if artifact_type == "role_artifact_invalid":
            payloads[role] = payload
            continue
        normalized_status = _relay_lifecycle_status_for_payload(payload, success_roles)
        if _relay_payload_status_is_success(normalized_status):
            success_roles.add(role)
            payloads.pop(role, None)
    return payloads


def _marvis_relay_summary_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    success_roles_by_round: dict[str, set[str]] = {}
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
            payload.get("summary") or payload.get("output") or payload.get("reason") or ""
        ).strip()
        summary = _marvis_relay_clean_artifact_summary(summary)
        if role and summary:
            round_id = str(payload.get("round_id") or "")
            success_roles = success_roles_by_round.setdefault(round_id, set())
            normalized_status = _relay_lifecycle_status_for_payload(payload, success_roles)
            payloads[role] = {
                "role": role,
                "artifact_type": artifact_type,
                "status": normalized_status,
                "summary": summary,
                "next_action": str(payload.get("next_action") or ""),
                "open_questions": payload.get("open_questions") or [],
                "acceptance_criteria": payload.get("acceptance_criteria") or [],
                "route": str(payload.get("route") or ""),
                "risk": str(payload.get("risk") or ""),
                "round_id": round_id,
                "confirmation_source": str(payload.get("confirmation_source") or ""),
                "confirmation_kind": str(payload.get("confirmation_kind") or ""),
                "provider": str(payload.get("provider") or ""),
                "provider_request_id": str(payload.get("provider_request_id") or ""),
            }
            if _relay_payload_status_is_success(normalized_status):
                success_roles.add(role)
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
            if entry.kind == "message" and entry.text:
                cleaned = _marvis_relay_clean_artifact_summary(entry.text)
                if cleaned:
                    entry.text = cleaned
                elif _marvis_relay_work_log_text_is_protocol_noise(entry.text):
                    continue
            if entry.text or entry.chip or entry.output:
                entries.append(entry)
        projected = compress_work_log_entries(
            [
                RawWorkLogEntry(
                    kind=entry.kind,
                    key=entry.key,
                    text=entry.text,
                    chip=entry.chip,
                    output=entry.output,
                    failed=entry.failed,
                    replace_text=entry.replace_text,
                )
                for entry in entries
            ],
            role=segment.role,
            profile="marvis",
        )
        segment.entries = [
            WorkLogEntry(
                kind=entry.kind,
                key=entry.key,
                text=entry.text,
                chip=entry.chip,
                output=entry.output,
                failed=entry.failed,
                replace_text=entry.replace_text,
            )
            for entry in projected
        ]
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
    chip_html = f'<span class="marvis-work-log-tool-chip">{escape(chip)}</span>' if chip else ""
    output_html = ""
    if output:
        output_html = (
            '<details class="marvis-work-log-output" data-marvis-work-log-output>'
            "<summary>查看输出</summary>"
            f"<pre>{escape(output)}</pre>"
            "</details>"
        )
    return f"""
      <div class="{" ".join(classes)}" data-marvis-work-log-entry="{escape(entry.kind)}"{key_attr}>
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


def _marvis_relay_protocol_archive_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return "结构化片段已归档"
    if "artifact_type" in value:
        return "结构化产物已归档"
    cleaned = _marvis_relay_clean_artifact_summary(value)
    if cleaned:
        return cleaned
    markers = [
        marker
        for marker in (
            "final_summary",
            "routing_decision",
            "role_envelope",
            "confirmation_options",
            "evidence_refs",
            "handoff_to",
            "required_roles",
            "next_action",
            "status",
        )
        if marker in value
    ]
    if markers:
        return "结构化片段已归档：" + "、".join(markers[:4])
    return "结构化片段已归档"


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
        if not text:
            return None
        if _marvis_relay_work_log_text_is_protocol_noise(text):
            return WorkLogEntry(
                kind="message",
                key=_relay_native_message_key(role, worker_event, bucket="assistant"),
                text=_marvis_relay_protocol_archive_text(text),
                chip="结构化片段 已归档",
            )
        compact_text, output, chip = _marvis_relay_compact_work_log_text(text)
        return WorkLogEntry(
            kind="message",
            key=_relay_native_message_key(role, worker_event, bucket="assistant"),
            text=_relay_sanitize_protocol_leak_text(role, compact_text),
            chip=chip,
            output=output,
        )
    if kind == "message_completed":
        text = _relay_native_event_text(worker_event).strip()
        if not text:
            return None
        if _marvis_relay_work_log_text_is_protocol_noise(text):
            return WorkLogEntry(
                kind="message",
                key=_relay_native_message_key(role, worker_event, bucket="assistant"),
                text=_marvis_relay_protocol_archive_text(text),
                chip="结构化片段 已归档",
                replace_text=True,
            )
        compact_text, output, chip = _marvis_relay_compact_work_log_text(text)
        return WorkLogEntry(
            kind="message",
            key=_relay_native_message_key(role, worker_event, bucket="assistant"),
            text=_relay_sanitize_protocol_leak_text(role, compact_text),
            chip=chip,
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
                payload.get("error") or payload.get("reason") or payload.get("status") or "调用失败"
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


def _marvis_relay_compact_work_log_text(text: str) -> tuple[str, str, str]:
    value = str(text or "").strip()
    if not value:
        return "", "", ""
    if not _marvis_relay_should_fold_work_log_text(value):
        return value, "", ""
    return "", value, "过程输出 已折叠"


def _marvis_relay_should_fold_work_log_text(text: str) -> bool:
    value = str(text or "")
    if _marvis_relay_text_looks_like_machine_output(value):
        return True
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


def _marvis_relay_text_looks_like_machine_output(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    normalized = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    if re.search(r"(?im)^(?:Task not found|Found \d+ files?|No files found)\b", normalized):
        return True
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) == 1:
        line = lines[0]
        return bool(
            re.match(r"^\d{2,}[:\t ]+\S", line)
            or re.match(r"^(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+$", line)
        )
    path_like = 0
    line_hit_like = 0
    code_like = 0
    for line in lines:
        if re.match(r"^(?:/[^:\s]+|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)$", line):
            path_like += 1
        if re.match(r"^(?:\d{2,}|[^:\s]+:\d{1,5})[:\t ]+\S", line):
            line_hit_like += 1
        if re.search(r"\b(?:def|class|const|let|var|return|import|from)\b|[{}();=]", line):
            code_like += 1
    if path_like >= 2 or line_hit_like >= 2:
        return True
    if len(lines) >= 4 and (path_like + line_hit_like + code_like) / len(lines) >= 0.5:
        return True
    return False


def _marvis_relay_work_log_text_is_protocol_noise(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if _relay_text_looks_like_role_envelope(value):
        return True
    if text_contains_relay_protocol_payload(value):
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
        parts = [str(part) for part in command if str(part).strip()]
        if len(parts) >= 2 and parts[1] == "executor":
            return f"{parts[0]} executor"
        if (
            len(parts) >= 3
            and Path(parts[0]).name in {"bash", "sh", "zsh"}
            and parts[1]
            in {
                "-c",
                "-lc",
            }
        ):
            return _marvis_relay_command_label_from_text(parts[2])
        return Path(parts[0]).name if parts else ""
    value = str(command or payload.get("cmd") or payload.get("name") or "").strip()
    return _marvis_relay_command_label_from_text(value)


def _marvis_relay_command_label_from_text(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.endswith(" executor"):
        return value
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = value.split()
    if (
        len(parts) >= 3
        and Path(parts[0]).name in {"bash", "sh", "zsh"}
        and parts[1]
        in {
            "-c",
            "-lc",
        }
    ):
        return _marvis_relay_command_label_from_text(parts[2])
    return Path(parts[0]).name if parts else value


def _relay_role_config_html(
    relay_config: dict[str, Any],
    providers: list[Any],
) -> str:
    assignments = relay_config.get("assignments")
    assignment_map = assignments if isinstance(assignments, dict) else {}
    roles = relay_config.get("roles")
    role_rows = (
        roles
        if isinstance(roles, list) and roles
        else [{"role": role, "display_name": _relay_role_label(role)} for role in RELAY_ROLE_IDS]
    )
    provider_rows = providers or [{"provider": "codex", "provider_engine": ""}]
    rows = []
    for role_entry in role_rows:
        role = str(role_entry.get("role") or "")
        if role not in RELAY_ROLE_IDS:
            continue
        selected = str(assignment_map.get(role) or provider_rows[0].get("provider") or "codex")
        options = "\n".join(
            f'<option value="{escape(str(provider.get("provider", "")))}"'
            f"{' selected' if str(provider.get('provider', '')) == selected else ''}>"
            f"{escape(_native_provider_display_name(str(provider.get('provider', ''))))}</option>"
            for provider in provider_rows
            if str(provider.get("provider", "")).strip()
        )
        tool_chips = (
            "".join(
                f'<span class="relay-chip">{escape(str(item))}</span>'
                for item in [
                    *list(role_entry.get("skills") or []),
                    *list(role_entry.get("capabilities") or []),
                ]
            )
            or '<span class="relay-chip">默认能力</span>'
        )
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
    workspace = str(summary.workspace or "")
    project_name = Path(workspace).name or workspace or "wlcodex"
    status_class = "relay-status-badge"
    if status:
        status_class += f" is-{_relay_status_class_name(status)}"
    return f"""
      <article class="relay-task-card marvis-relay-task-card" data-status="{escape(status)}">
        <div class="relay-card-identity">
          <div class="relay-card-avatar-row">
            {_marvis_relay_avatar_html("marvis", label="Marvis")}
          </div>
          <div class="relay-card-side">
            <span class="{escape(status_class)}">{escape(status_label)}</span>
            <span class="relay-card-project-pill">{escape(project_name)}</span>
            <span class="relay-card-activity">{escape(activity)}</span>
          </div>
        </div>
        <div class="relay-title">{escape(summary.title)}</div>
        <div class="marvis-relay-task-card-footer">
          <a class="relay-open relay-card-open" href="/native/workflows/relay/tasks/{int(summary.task_id)}{token_suffix}">打开任务</a>
        </div>
      </article>
    """


def _relay_status_class_name(status: str) -> str:
    safe = "".join(char if char.isalnum() else "-" for char in status.lower()).strip("-")
    return safe or "unknown"


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
    return routing_route_label(route)


def _relay_humanize_display_text(text: str, *, english_fallback: str = "") -> str:
    return humanize_display_text(text, english_fallback=english_fallback)


def _relay_text_needs_chinese_fallback(text: str) -> bool:
    return text_needs_chinese_fallback(text)


def _relay_routing_risk_label(risk: str) -> str:
    return routing_risk_label(risk)


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


def _relay_task_detail_page(
    detail: Any,
    *,
    access_token: str = "",
    view: str = "conversation",
    events: list[Any] | tuple[Any, ...] | None = None,
    hub: WorkerLiveStreamHub | None = None,
    token_stats: dict[str, Any] | None = None,
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
    token_total = _marvis_token_int((token_stats or {}).get("total_consumed_tokens"))
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
    plan_control_html = _marvis_relay_plan_control_html(detail)
    followup_composer_html = _marvis_relay_followup_composer(
        task_id=int(task.id),
        workspace=str(task.workspace or ""),
        access_token=access_token,
        task_status=str(task.status or ""),
        current_round_id=int(getattr(detail, "current_round_id", 1) or 1),
        pending_inputs=[
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (getattr(detail, "pending_inputs", []) or [])
        ],
    )
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
{_relay_mobile_web_head(f"{task.title} · Relay")}
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
  {plan_control_html}
  {followup_composer_html}
  <nav class="marvis-relay-bottom-nav" aria-label="Marvis relay navigation">
    {bottom_nav_html}
  </nav>
  </div>
  {work_log_html}
  <script>
    {_marvis_relay_attachment_script()}
    const TASK_ID = {json.dumps(str(task.id))};
    const CURRENT_ROUND_ID = {json.dumps(str(getattr(detail, "current_round_id", 1) or 1))};
    let activeRelayRoundId = CURRENT_ROUND_ID;
    const TOKEN_SUFFIX = {json.dumps(token_suffix)};
    const EVENTS_SUFFIX = {json.dumps(events_suffix)};
    const INITIAL_PENDING_INPUTS = {
        json.dumps(
            [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in (getattr(detail, "pending_inputs", []) or [])
            ],
            ensure_ascii=False,
        )
    };
    const ROLE_LABELS = {
        json.dumps({role: _relay_role_label(role) for role in RELAY_ROLE_IDS}, ensure_ascii=False)
    };
    const MARVIS_WORK_LOG_ROLE_LABELS = {
        json.dumps(
            {role: _marvis_relay_public_role(role)[1] for role in RELAY_ROLE_IDS},
            ensure_ascii=False,
        )
    };
    const MARVIS_WORK_LOG_ROLE_PERSONAS = {
        json.dumps(
            {role: _marvis_relay_public_role(role)[0] for role in RELAY_ROLE_IDS},
            ensure_ascii=False,
        )
    };
    const MARVIS_HANDOFF_ROLE_LABELS = {
        json.dumps(
            {role: _marvis_relay_handoff_role_label(role) for role in RELAY_ROLE_IDS},
            ensure_ascii=False,
        )
    };
    const MARVIS_LEGACY_ROLE_LABEL_PARTS = {
        json.dumps(_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS, ensure_ascii=False)
    };
    const MARVIS_LEGACY_ROLE_SLUG_PARTS = {
        json.dumps(_MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS, ensure_ascii=False)
    };
    const STATUS_LABELS = {
        json.dumps(
            {
                "idle": "未调度",
                "queued": "排队中",
                "streaming": "执行中",
                "waiting": "等待中",
                "passed": "已完成",
                "failed": "失败",
                "blocked": "阻塞",
                "interrupted": "已中断",
                "completed": "已完成",
            },
            ensure_ascii=False,
        )
    };
    const TASK_STATUS_LABELS = {
        json.dumps(
            {
                "queued": "排队中",
                "running": "进行中",
                "waiting_user": "等待你",
                "blocked": "已阻塞",
                "failed": "失败",
                "completed": "已完成",
                "interrupted": "已中断",
            },
            ensure_ascii=False,
        )
    };
    const roleStatuses = {
        json.dumps(
            {
                str(getattr(job, "role", "") or ""): str(
                    getattr(job, "status", "") or "idle"
                )
                for job in detail.role_jobs
            },
            ensure_ascii=False,
        )
    };
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
    function labelForRole(role) {{
      return ROLE_LABELS[role] || role || "角色";
    }}
    function marvisHandoffRoleLabel(role) {{
      return MARVIS_HANDOFF_ROLE_LABELS[role] || labelForRole(role);
    }}
    function marvisHandoffText(fromRole, toRole) {{
      const toName = marvisHandoffRoleLabel(toRole);
      if (fromRole === "director") return `Marvis 拍了拍 ${{toName}} 说， 别等了，这就开始`;
      const fromName = marvisHandoffRoleLabel(fromRole);
      if (toRole === "auditor") return `${{fromName}}交给${{toName}}复核`;
      if (fromRole === "auditor" && toRole === "director") return `${{fromName}}交回Marvis收尾`;
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
      updateRelayEventsCursor(event);
      return normalizeRelayPayload(JSON.parse(event.data || "{{}}"));
    }}
    const conversationTimeline = document.querySelector("[data-native-conversation-timeline]");
    const nativeTranscriptNodes = new Map();
    const nativeEnvelopeBuffers = new Map();
    const conversationUserBodies = new Set();
    const seenStreamEventKeys = new Set();
    const roleStreamBuffers = new Map();
    const hiddenProtocolStreamKeys = new Set();
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
      if (!nativeEvent || typeof nativeEvent !== "object") return {{}};
      if (nativeEvent.payload && typeof nativeEvent.payload === "object") return nativeEvent.payload;
      return nativeEvent;
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
      if (/"artifact_type"\\s*:\\s*"(?:routing_decision|role_envelope|final_summary|architecture_plan|implementation_report|audit_report|test_report|followup_response)"/.test(value)) return true;
      return [
        "relay_role",
        "routing_decision",
        "acceptance_criteria",
        "handoff_to",
        "required_roles",
        "open_questions",
        "next_action",
      ].some((marker) => value.includes(marker));
    }}
    function relayTextHasProtocolFragmentShape(text) {{
      const value = String(text || "").trim();
      return value.includes("{{")
        || value.includes("}}")
        || value.includes('",')
        || value.includes('":')
        || value.includes('\\\\\"')
        || value.includes("],")
        || value.startsWith('"')
        || value.startsWith("[");
    }}
    function marvisConversationTextIsProtocolNoise(text) {{
      const value = String(text || "").trim();
      if (!value) return true;
      if (relayTextLooksLikeEnvelope(value)) return true;
      const artifactTypePattern = /"artifact_type"\\s*:\\s*"(?:routing_decision|role_envelope|final_summary|architecture_plan|implementation_report|audit_report|followup_response)"/;
      if (artifactTypePattern.test(value)) return true;
      if (!relayTextHasProtocolFragmentShape(value)) return false;
      const markers = [
        "relay_role",
        "routing_decision",
        "role_envelope",
        "final_summary",
        "confirmation_options",
        "evidence_refs",
        "handoff_to",
        "required_roles",
        "next_action",
        "open_questions",
        "acceptance_criteria",
        "status",
        "summary",
        "reason",
        "role",
      ];
      const matched = markers.filter((marker) => value.includes(marker));
      if (matched.length >= 2) return true;
      const markerSet = new Set(matched);
      if (markerSet.has("evidence_refs") && markerSet.has("handoff_to")) return true;
      if (markerSet.has("final_summary") && markerSet.has("confirmation_options")) return true;
      if (markerSet.has("acceptance_criteria")) return true;
      if (markerSet.has("confirmation_options")) return true;
      if (markerSet.has("required_roles")) return true;
      if (markerSet.has("handoff_to")) return true;
      if (markerSet.has("next_action")) return true;
      return false;
    }}
    function marvisConversationTextIsPotentialProtocolPrefix(text) {{
      const value = String(text || "").trim();
      if (!value) return false;
      if (!relayTextHasProtocolFragmentShape(value)) return false;
      if (value.startsWith("{{")) return true;
      if (value.startsWith("[") && !/[\\u4e00-\\u9fff]/.test(value)) return true;
      if (value.startsWith('"')) {{
        const compact = value.replace(/\\s+/g, "");
        if (/^"?[A-Za-z_]*$/.test(compact)) return true;
        if (compact.includes('":') || compact.includes('":["') || compact.includes('",')) return true;
        if (!/[\\u4e00-\\u9fff]/.test(value) && compact.length < 240) return true;
      }}
      return false;
    }}
    function marvisConversationTextIsStructuredArtifactPlaceholder(text) {{
      const value = String(text || "").trim();
      if (!value) return true;
      if (marvisConversationTextIsProtocolNoise(value)) return true;
      if (value.startsWith("{{") && (relayParseEnvelope(value) || relayTextLooksLikeEnvelope(value))) return true;
      return [
        "结构化结果缺少",
        "结构化结果不是合法",
        "结构化结果未采用",
        "结构化产物未采用",
        "结构化输出已由系统处理",
        "详情见结构化数据",
        "原始协议内容不在主会话展示",
        "输出格式异常",
        "任务已阻塞",
        "invalid json",
      ].some((marker) => value.includes(marker));
    }}
    function relayDictLooksLikeEnvelope(payload) {{
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
      const artifactType = String(payload.artifact_type || "");
      if ([
        "routing_decision",
        "role_envelope",
        "final_summary",
        "architecture_plan",
        "implementation_report",
        "audit_report",
        "test_report",
        "followup_response",
      ].includes(artifactType)) return true;
      return [
        "relay_role",
        "next_action",
        "open_questions",
        "required_roles",
        "acceptance_criteria",
        "handoff_to",
        "status",
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
        return "";
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
      if (Array.isArray(command)) {{
        const parts = command.map(String).filter((part) => part.trim());
        if (parts.length >= 2 && parts[1] === "executor") return `${{parts[0]}} executor`;
        if (parts.length >= 3 && ["bash", "sh", "zsh"].includes(parts[0].split(/[\\/]/).pop()) && ["-c", "-lc"].includes(parts[1])) {{
          return marvisWorkLogCommandLabelFromText(parts[2]);
        }}
        return parts.length ? parts[0].split(/[\\/]/).pop() : "";
      }}
      const value = String(command || payload.cmd || payload.name || "").trim();
      return marvisWorkLogCommandLabelFromText(value);
    }}
    function marvisWorkLogCommandLabelFromText(value) {{
      value = String(value || "").trim();
      if (!value) return "";
      if (value.endsWith(" executor")) return value;
      const parts = value.match(/(?:[^\\s"']+|"[^"]*"|'[^']*')+/g) || [];
      if (parts.length >= 3) {{
        const shell = parts[0].replace(/^["']|["']$/g, "").split(/[\\/]/).pop();
        const flag = parts[1].replace(/^["']|["']$/g, "");
        if (["bash", "sh", "zsh"].includes(shell) && ["-c", "-lc"].includes(flag)) {{
          return marvisWorkLogCommandLabelFromText(parts[2].replace(/^["']|["']$/g, ""));
        }}
      }}
      return parts.length ? parts[0].replace(/^["']|["']$/g, "").split(/[\\/]/).pop() : value;
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
      return marvisConversationTextIsProtocolNoise(value);
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
    function marvisWorkLogProtocolArchiveText(text) {{
      const value = String(text || "").trim();
      if (!value) return "结构化片段已归档";
      if (value.includes("artifact_type")) return "结构化产物已归档";
      const cleaned = marvisWorkLogCleanProtocolSummary(value);
      if (cleaned) return cleaned;
      const markers = [
        "final_summary",
        "routing_decision",
        "role_envelope",
        "confirmation_options",
        "evidence_refs",
        "handoff_to",
        "required_roles",
        "next_action",
        "status",
      ].filter((marker) => value.includes(marker));
      if (markers.length) return `结构化片段已归档：${{markers.slice(0, 4).join("、")}}`;
      return "结构化片段已归档";
    }}
    function marvisWorkLogShouldFoldText(text) {{
      const value = String(text || "");
      if (marvisWorkLogLooksLikeAgentDump(value)) return true;
      if (value.length > 600) return true;
      if (value.includes("```")) return true;
      const stripped = value.trimStart();
      if ((stripped.startsWith("{{") || stripped.startsWith("[")) && value.length > 240) return true;
      if (/<(?:!doctype|html|body|script|style|pre|div|section)\\b/i.test(value)) return true;
      return value.split(/\\r?\\n/).some((line) => line.length > 220);
    }}
    function marvisWorkLogTextLooksLikeMachineOutput(text) {{
      const value = String(text || "").trim();
      if (!value) return false;
      const normalized = value.replace(/\\\\r\\\\n/g, "\\n").replace(/\\\\n/g, "\\n");
      if (/^(?:Task not found|Found \\d+ files?|No files found)\\b/im.test(normalized)) return true;
      const lines = normalized.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);
      if (!lines.length) return false;
      if (lines.length === 1) {{
        const line = lines[0];
        return /^\\d{{2,}}[:\\t ]+\\S/.test(line) || /^(?:[A-Za-z0-9_.-]+\\/)+[A-Za-z0-9_.-]+$/.test(line);
      }}
      let pathLike = 0;
      let lineHitLike = 0;
      let codeLike = 0;
      for (const line of lines) {{
        if (/^(?:\\/[^:\\s]+|(?:[A-Za-z0-9_.-]+\\/)+[A-Za-z0-9_.-]+)$/.test(line)) pathLike += 1;
        if (/^(?:\\d{{2,}}|[^:\\s]+:\\d{{1,5}})[:\\t ]+\\S/.test(line)) lineHitLike += 1;
        if (/\\b(?:def|class|const|let|var|return|import|from)\\b|[{{}}();=]/.test(line)) codeLike += 1;
      }}
      if (pathLike >= 2 || lineHitLike >= 2) return true;
      return lines.length >= 4 && (pathLike + lineHitLike + codeLike) / lines.length >= 0.5;
    }}
    function marvisWorkLogLooksLikeAgentDump(text) {{
      const value = String(text || "").trim();
      if (!value) return false;
      if (marvisWorkLogTextLooksLikeMachineOutput(value)) return true;
      return /\\bThe file\\b.+\\bhas been updated successfully\\b/i.test(value)
        || /\\b(?:All changes are in place|No matches found|Found \\d+ files?|Task not found)\\b/i.test(value)
        || /位于分支|尚未暂存|修改尚未加入提交|未跟踪的文件/.test(value);
    }}
    function marvisWorkLogCompactText(text) {{
      const value = String(text || "").trim();
      if (!value) return {{ text: "", output: "", chip: "" }};
      if (!marvisWorkLogShouldFoldText(value)) return {{ text: value, output: "", chip: "" }};
      return {{ text: "", output: value, chip: "过程输出 已折叠" }};
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
          return {{
            kind: "message",
            key,
            text: marvisWorkLogProtocolArchiveText(text),
            chip: "结构化片段 已归档",
            replaceText: kind === "message_completed",
          }};
        }}
        const compact = marvisWorkLogCompactText(text);
        return {{
          kind: "message",
          key,
          text: relaySanitizeProtocolLeakText(role, compact.text),
          chip: compact.chip,
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
      marvisWorkLogBody.appendChild(section);
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
      compactMarvisWorkLogSegment(segment);
      marvisWorkLogBody?.scrollTo({{ top: marvisWorkLogBody.scrollHeight, behavior: "smooth" }});
    }}
    function marvisWorkLogToolCategory(chipText, kind) {{
      const command = String(chipText || "").trim().split(/\\s+/, 1)[0].toLowerCase();
      if (["rg", "grep", "find", "fd", "ag"].includes(command)) return "检索";
      if (["sed", "nl", "cat", "head", "tail", "less"].includes(command)) return "读取";
      if (command === "git") return "检查变更";
      if (["pytest", "unittest", "coverage"].includes(command)) return "测试";
      if (["node", "npm", "npx", "pnpm", "yarn"].includes(command)) return "前端工具";
      if (["sqlite3", "psql", "mysql"].includes(command)) return "查询状态";
      if (kind === "file") return "文件变更";
      return "工具";
    }}
    function marvisWorkLogReadToolCounts(value) {{
      try {{
        const parsed = JSON.parse(String(value || "{{}}"));
        return new Map(Object.entries(parsed).map(([label, count]) => [label, Number(count) || 0]));
      }} catch (_error) {{
        return new Map();
      }}
    }}
    function marvisWorkLogWriteToolCounts(counts) {{
      return JSON.stringify(Object.fromEntries(Array.from(counts.entries())));
    }}
    function compactMarvisWorkLogSegment(segment) {{
      if (!segment) return;
      const line = segment.querySelector(".marvis-work-log-line");
      if (!line) return;
      const toolNodes = Array.from(line.querySelectorAll('[data-marvis-work-log-entry="command"], [data-marvis-work-log-entry="tool"], [data-marvis-work-log-entry="file"]'));
      const role = segment.dataset.marvisWorkLogSegment || "";
      let batch = line.querySelector(`[data-marvis-work-log-entry-key="${{CSS.escape(`tool-batch:${{role}}`)}}"]`);
      if (!batch && toolNodes.length < 4) return;
      if (batch && !toolNodes.length) return;
      const counts = new Map();
      const outputParts = [];
      let failed = false;
      for (const node of toolNodes) {{
        const chipText = node.querySelector(".marvis-work-log-tool-chip")?.textContent || node.dataset.marvisWorkLogEntry || "";
        const kind = node.dataset.marvisWorkLogEntry || "";
        const category = marvisWorkLogToolCategory(chipText, kind);
        counts.set(category, (counts.get(category) || 0) + 1);
        const body = node.querySelector("[data-marvis-work-log-output] pre")?.textContent || node.querySelector("p")?.textContent || "";
        outputParts.push(body ? `${{chipText}}\\n${{body}}` : chipText);
        failed = failed || node.classList.contains("is-failed");
      }}
      if (!batch) {{
        batch = document.createElement("div");
        batch.className = "marvis-work-log-entry";
        batch.dataset.marvisWorkLogEntry = "tool_batch";
        batch.dataset.marvisWorkLogEntryKey = `tool-batch:${{role}}`;
        batch.appendChild(document.createElement("p"));
        line.insertBefore(batch, toolNodes[0]);
      }}
      const totalCounts = marvisWorkLogReadToolCounts(batch.dataset.marvisWorkLogToolCounts);
      for (const [label, count] of counts.entries()) {{
        totalCounts.set(label, (totalCounts.get(label) || 0) + count);
      }}
      const previousCount = Number(batch.dataset.marvisWorkLogToolCount || "0") || 0;
      const totalCount = previousCount + toolNodes.length;
      batch.dataset.marvisWorkLogToolCount = String(totalCount);
      batch.dataset.marvisWorkLogToolCounts = marvisWorkLogWriteToolCounts(totalCounts);
      batch.classList.toggle("is-failed", batch.classList.contains("is-failed") || failed);
      let chip = batch.querySelector(".marvis-work-log-tool-chip");
      if (!chip) {{
        chip = document.createElement("span");
        chip.className = "marvis-work-log-tool-chip";
        batch.insertBefore(chip, batch.firstChild);
      }}
      chip.textContent = `工具调用 ${{totalCount}} 次`;
      const paragraph = batch.querySelector("p") || batch.appendChild(document.createElement("p"));
      paragraph.textContent = `${{Array.from(totalCounts.entries()).map(([label, count]) => `${{label}} ${{count}} 次`).join("、")}}。原始输出已折叠。`;
      let details = batch.querySelector("[data-marvis-work-log-output]");
      if (!details) {{
        details = document.createElement("details");
        details.className = "marvis-work-log-output";
        details.dataset.marvisWorkLogOutput = "";
        const summary = document.createElement("summary");
        summary.textContent = "查看输出";
        const pre = document.createElement("pre");
        details.append(summary, pre);
        batch.appendChild(details);
      }}
      const pre = details.querySelector("pre");
      if (pre) {{
        const existingOutput = pre.textContent || "";
        const newOutput = outputParts.join("\\n\\n");
        pre.textContent = existingOutput && newOutput ? `${{existingOutput}}\\n\\n${{newOutput}}` : (existingOutput || newOutput);
      }}
      toolNodes.forEach((node) => node.remove());
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
    function renderMarvisWorkLogConfirmation(payload = {{}}) {{
      if (!marvisWorkLogBody || !payload) return;
      const role = payload.role || "director";
      const source = String(payload.confirmation_source || "");
      const sourceLabel = confirmationSourceLabel(payload);
      const kind = String(payload.confirmation_kind || "relay_question");
      const requestId = String(payload.provider_request_id || "");
      const waitingReason = String(payload.waiting_reason || "");
      const summary = String(payload.summary || payload.next_action || "当前接力需要用户确认。").trim();
      const chip = source === "provider_native_approval"
        ? `${{sourceLabel}} · ${{kind}}`
        : `${{sourceLabel}} · 等待用户`;
      const textParts = [summary];
      if (requestId) textParts.push(`请求 ID：${{requestId}}`);
      if (waitingReason) textParts.push(`等待原因：${{waitingReason}}`);
      const segment = ensureMarvisWorkLogSegment(role);
      renderMarvisWorkLogEntry(segment, {{
        kind: "confirmation",
        key: `confirmation:${{payload.round_id || ""}}:${{role}}:${{source}}:${{requestId || payload.artifact_id || ""}}`,
        chip,
        text: textParts.filter(Boolean).join("\\n"),
        replaceText: true,
      }});
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
    function appendMarvisConversationUser(text, key = "", pending = false, attachments = {{}}) {{
      if (!conversationTimeline) return null;
      const body = relayHumanizeUserMessage(text);
      const normalizedBody = relayNormalizeConversationText(body);
      const hasAttachments = Boolean((attachments.images || []).length || (attachments.files || []).length);
      if ((!normalizedBody && !hasAttachments) || relayUserMessageIsRetryOrContext(body)) return null;
      if (normalizedBody) {{
        if (conversationUserBodies.has(normalizedBody)) return null;
        conversationUserBodies.add(normalizedBody);
      }}
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const node = document.createElement("article");
      node.className = "marvis-relay-user-message";
      node.dataset.nativeRole = "user";
      node.dataset.nativeKind = "user_message";
      if (key) node.dataset.nativeKey = key;
      if (pending) node.dataset.pendingFollowup = "true";
      if (body) {{
        const bubble = document.createElement("div");
        bubble.className = "marvis-relay-user-bubble";
        bubble.dataset.nativeMessageBody = "";
        bubble.textContent = body;
        node.appendChild(bubble);
      }}
      appendMarvisAttachmentList(node, attachments);
      conversationTimeline.appendChild(node);
      if (key) nativeTranscriptNodes.set(key, node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function appendMarvisConversationGuidance(payload = {{}}) {{
      if (!conversationTimeline) return null;
      const text = String(payload.text || payload.latest_user_input || "").trim();
      const hasAttachments = Boolean((payload.images || []).length || (payload.files || []).length);
      if (!text && !hasAttachments) return null;
      const id = payload.guidance_artifact_id || payload.artifact_id || payload.id || payload.pending_input_id || Date.now();
      const key = `user_guidance:${{id}}`;
      const existing = nativeTranscriptNodes.get(key) || conversationTimeline.querySelector(`[data-native-key='${{CSS.escape(key)}}']`);
      if (existing) return existing;
      const roundId = String(payload.steered_round_id || payload.round_id || activeRelayRoundId || CURRENT_ROUND_ID || "1");
      activateRelayRound({{ round_id: roundId }});
      const body = relayHumanizeUserMessage(text);
      const normalizedBody = relayNormalizeConversationText(body);
      if (normalizedBody) conversationUserBodies.add(normalizedBody);
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const node = document.createElement("article");
      node.className = "marvis-relay-user-message marvis-relay-guidance-message";
      node.dataset.nativeRole = "user";
      node.dataset.nativeKind = "user_guidance";
      node.dataset.nativeRoundId = roundId;
      node.dataset.nativeKey = key;
      const bubble = document.createElement("div");
      bubble.className = "marvis-relay-user-bubble marvis-relay-guidance-bubble";
      const label = document.createElement("span");
      label.className = "marvis-relay-guidance-label";
      label.textContent = "引导当前";
      const content = document.createElement("strong");
      content.textContent = body || "已添加附件";
      bubble.append(label, content);
      node.appendChild(bubble);
      appendMarvisAttachmentList(node, {{
        images: payload.images || [],
        files: payload.files || []
      }});
      conversationTimeline.appendChild(node);
      nativeTranscriptNodes.set(key, node);
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
    function clearMarvisConversationPausedRows() {{
      conversationTimeline?.querySelectorAll("[data-native-key^='relay-paused:']").forEach((node) => node.remove());
    }}
    function relayEventRoundId(payload) {{
      const value = payload?.round_id || payload?.payload?.round_id || "";
      return value ? String(value) : "";
    }}
    function isCurrentRoundEvent(payload) {{
      const roundId = relayEventRoundId(payload);
      return !roundId || roundId === activeRelayRoundId;
    }}
    function activateRelayRound(payload) {{
      const roundId = relayEventRoundId(payload);
      if (roundId) {{
        activeRelayRoundId = roundId;
        if (followupComposer) followupComposer.dataset.currentRoundId = roundId;
      }}
      return activeRelayRoundId;
    }}
    function appendMarvisConversationWaiting(roundId = "") {{
      if (!conversationTimeline) return null;
      clearMarvisConversationPausedRows();
      let node = conversationTimeline.querySelector("[data-marvis-followup-waiting]");
      if (node) {{
        if (roundId) node.dataset.nativeRoundId = roundId;
        return node;
      }}
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      node = document.createElement("article");
      node.className = "marvis-relay-agent-step marvis-relay-waiting";
      node.dataset.nativeRole = "director";
      node.dataset.nativeKind = "waiting";
      node.dataset.nativeRoundId = roundId || activeRelayRoundId || "1";
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
    function clearMarvisConversationWaiting(roundId = "") {{
      const activeRound = roundId || activeRelayRoundId || "";
      conversationTimeline?.querySelectorAll("[data-marvis-followup-waiting]").forEach((node) => {{
        if (!activeRound || !node.dataset.nativeRoundId || node.dataset.nativeRoundId === activeRound) {{
          node.remove();
        }}
      }});
    }}
    function appendMarvisConversationAssistant(role, text, kind = "followup_response", key = "", status = "passed", roundId = "") {{
      if (!conversationTimeline || !text) return null;
      clearMarvisConversationWaiting(roundId);
      const existing = key ? nativeTranscriptNodes.get(key) || conversationTimeline.querySelector(`[data-native-key='${{CSS.escape(key)}}']`) : null;
      const node = existing || document.createElement("article");
      node.className = "marvis-relay-agent-step";
      node.dataset.nativeRole = role || "director";
      node.dataset.nativeKind = kind || "followup_response";
      node.dataset.nativeRoundId = roundId || activeRelayRoundId || "1";
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
        action.textContent = `| ${{labelForStatus(status) || "已完成"}}`;
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
    function appendMarvisConversationHandoff(toRole, key = "", fromRole = "", roundId = "") {{
      if (!conversationTimeline || !toRole) return null;
      if (!fromRole || fromRole === toRole) return null;
      roundId = roundId || activeRelayRoundId || CURRENT_ROUND_ID || "1";
      const existingPair = conversationTimeline.querySelector(
        `[data-marvis-handoff][data-native-from-role='${{CSS.escape(fromRole)}}'][data-native-to-role='${{CSS.escape(toRole)}}'][data-native-round-id='${{CSS.escape(roundId)}}']`
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
      node.dataset.nativeRoundId = roundId;
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
      if (kind === "text_delta" || kind === "message_completed") {{
        const key = nativeMessageKey(role, nativeEvent, "assistant");
        const bufferedEnvelope = nativeEnvelopeBuffers.get(key) || "";
        if (kind === "text_delta") {{
          appendRoleStreamDelta(role, text, runtimeEventId || nativeEvent?.id, nativeEvent);
          setRoleStatus(role, "streaming");
        }}
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
          clearRolePreview(role);
          setRoleStatus(role, "streaming");
          return;
        }}
        if (kind === "message_completed") {{
          replaceRoleStreamWithCompleted(role, text, runtimeEventId || nativeEvent?.id, nativeEvent);
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
    function streamEventKey(role, eventId) {{
      const value = String(eventId || "").trim();
      return value ? `${{role || ""}}:${{value}}` : "";
    }}
    function roleStreamStableEventId(eventId, nativeEvent = null) {{
      const payload = nativeEventPayload(nativeEvent);
      const directPayload = nativeEvent && typeof nativeEvent === "object" ? nativeEvent : {{}};
      return payload.itemId
        || payload.item_id
        || payload.stream_key
        || directPayload.stream_key
        || payload.native_message_id
        || directPayload.native_message_id
        || payload.message_id
        || directPayload.message_id
        || payload.native_turn_id
        || directPayload.native_turn_id
        || payload.turnId
        || directPayload.turnId
        || payload.turn_id
        || directPayload.turn_id
        || eventId
        || "current";
    }}
    function roleStreamBufferKey(role, eventId) {{
      return `${{role || ""}}:${{eventId || "current"}}`;
    }}
    function removeRoleStreamNode(role) {{
      conversationTimeline?.querySelector(`[data-conversation-role-stream="${{role}}"]`)?.remove();
    }}
    function hideRoleStreamBuffer(role, bufferKey) {{
      hiddenProtocolStreamKeys.add(bufferKey);
      roleStreamBuffers.delete(bufferKey);
      removeRoleStreamNode(role);
    }}
    function marvisConversationRoleLabel(role) {{
      return MARVIS_WORK_LOG_ROLE_LABELS[role] || labelForRole(role) || "Marvis";
    }}
    function marvisConversationStreamAction(role, text) {{
      const count = Array.from(String(text || "")).length;
      const action = role === "director" ? "任务分配" : "任务处理";
      return `| ${{action}} 进行中${{count ? `，${{count}}字符` : ""}}`;
    }}
    function createMarvisRoleStreamMessage(role, key) {{
      if (!conversationTimeline) return null;
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const node = document.createElement("article");
      node.className = "marvis-relay-agent-step";
      node.dataset.nativeRole = role || "director";
      node.dataset.nativeKind = "text_delta";
      node.dataset.conversationRoleStream = role || "director";
      if (key) node.dataset.nativeKey = key;
      const label = marvisConversationRoleLabel(role);
      const avatar = document.createElement("span");
      avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{marvisConversationPersona(role)}}`;
      avatar.setAttribute("aria-label", label);
      const content = document.createElement("div");
      content.className = "marvis-relay-agent-content";
      const head = document.createElement("div");
      head.className = "marvis-relay-agent-head";
      const title = document.createElement("strong");
      title.textContent = label;
      const action = document.createElement("span");
      action.className = "marvis-relay-agent-action";
      action.dataset.marvisStreamAction = "true";
      head.append(title, document.createTextNode(" "), action);
      const body = document.createElement("div");
      body.className = "marvis-relay-agent-bubble";
      body.dataset.nativeMessageBody = "";
      content.append(head, body);
      node.append(avatar, content);
      conversationTimeline.appendChild(node);
      return node;
    }}
    function updateMarvisRoleStreamAction(node, role, text) {{
      const action = node?.querySelector("[data-marvis-stream-action]");
      if (action) action.textContent = marvisConversationStreamAction(role, text);
    }}
    function appendRoleStreamDelta(role, text, eventId = "", nativeEvent = null) {{
      if (!role || !text) return;
      const stableEventId = roleStreamStableEventId(eventId, nativeEvent);
      const bufferKey = roleStreamBufferKey(role, stableEventId);
      if (hiddenProtocolStreamKeys.has(bufferKey)) return;
      const eventKey = streamEventKey(role, eventId);
      if (eventKey && seenStreamEventKeys.has(eventKey)) return;
      if (eventKey) seenStreamEventKeys.add(eventKey);
      const value = String(text || "");
      const buffered = `${{roleStreamBuffers.get(bufferKey) || ""}}${{value}}`;
      roleStreamBuffers.set(bufferKey, buffered);
      if (
        marvisConversationTextIsProtocolNoise(buffered)
        || marvisConversationTextIsStructuredArtifactPlaceholder(buffered)
      ) {{
        hideRoleStreamBuffer(role, bufferKey);
        appendMarvisConversationWaiting(activeRelayRoundId);
        setRoleStatus(role, "streaming");
        return;
      }}
      if (marvisConversationTextIsPotentialProtocolPrefix(buffered)) {{
        appendMarvisConversationWaiting(activeRelayRoundId);
        setRoleStatus(role, "streaming");
        return;
      }}
      if (TERMINAL_ROLE_STATUSES.has(currentRoleStatus(role))) return;
      if (!conversationTimeline) return;
      if (conversationTimeline.querySelector(`[data-conversation-role-final="${{role}}"]`)) return;
      let node = conversationTimeline.querySelector(`[data-conversation-role-stream="${{role}}"]`);
      if (!node) {{
        node = createMarvisRoleStreamMessage(role, `stream:${{bufferKey}}`);
        if (!node) return;
      }}
      const body = node.querySelector("[data-native-message-body]");
      const current = body ? body.textContent || "" : "";
      updateMarvisRoleStreamAction(node, role, current + value);
      setNativeBodyText(node, current + value);
    }}
    function replaceRoleStreamWithCompleted(role, text, eventId = "", nativeEvent = null) {{
      if (!role || !text || !conversationTimeline) return;
      const stableEventId = roleStreamStableEventId(eventId, nativeEvent);
      const bufferKey = roleStreamBufferKey(role, stableEventId);
      const value = String(text || "");
      if (
        marvisConversationTextIsProtocolNoise(value)
        || marvisConversationTextIsStructuredArtifactPlaceholder(value)
        || relayTextLooksLikeEnvelope(value)
      ) {{
        hideRoleStreamBuffer(role, bufferKey);
        appendMarvisConversationWaiting(activeRelayRoundId);
        return;
      }}
      roleStreamBuffers.delete(bufferKey);
      hiddenProtocolStreamKeys.delete(bufferKey);
      removeRoleStreamNode(role);
      const key = nativeMessageKey(role, nativeEvent, "assistant");
      appendMarvisConversationAssistant(
        role,
        value,
        "message_completed",
        key,
        currentRoleStatus(role) || "completed",
        activeRelayRoundId
      );
      scrollNativeConversationToEnd();
    }}
    function clearRolePreview(role) {{
      conversationTimeline?.querySelector(`[data-conversation-role-preview="${{role}}"]`)?.remove();
      removeRoleStreamNode(role);
    }}
    function clearAllRolePreviews() {{
      Object.keys(roleStatuses).forEach(clearRolePreview);
    }}
    document.querySelectorAll("[data-conversation-role-stream]").forEach((node) => {{
      const role = node.dataset.conversationRoleStream;
      (node.dataset.streamEventIds || "").split(",").filter(Boolean).forEach((eventId) => {{
        const eventKey = streamEventKey(role, eventId);
        if (eventKey) seenStreamEventKeys.add(eventKey);
      }});
    }});
    const TERMINAL_ROLE_STATUSES = new Set(["passed", "completed", "blocked", "failed", "interrupted"]);
    function currentRoleStatus(role) {{
      return roleStatuses[role] || "";
    }}
    function canApplyRoleStatus(role, status, options = {{}}) {{
      if (!status) return false;
      if (options.force) return true;
      const currentStatus = currentRoleStatus(role);
      if (TERMINAL_ROLE_STATUSES.has(currentStatus) && !TERMINAL_ROLE_STATUSES.has(status)) return false;
      return true;
    }}
    function setRoleStatus(role, status, options = {{}}) {{
      if (!canApplyRoleStatus(role, status, options)) return;
      if (role && status) roleStatuses[role] = status;
    }}
    const followupComposer = document.querySelector("[data-marvis-followup-composer]");
    const pendingInputsContainer = document.querySelector("[data-marvis-pending-inputs]");
    const followupTextInput = followupComposer?.querySelector("textarea[name='text']");
    const followupSubmitButton = followupComposer?.querySelector("[data-marvis-submit]");
    const pendingInputs = new Map();
    let relayTaskStatus = followupComposer?.dataset.taskStatusValue || "";
    let waitingControlInput = "";
    function relayTaskIsRunning() {{
      return ["queued", "running", "streaming"].includes(String(relayTaskStatus || "").trim());
    }}
    function relayTaskAcceptsPendingInput() {{
      return relayTaskIsRunning() || String(relayTaskStatus || "").trim() === "waiting_user";
    }}
    function relayFollowupHasText() {{
      return Boolean(String(followupTextInput?.value || "").trim());
    }}
    function relayFollowupHasAttachments() {{
      return Boolean(window.marvisRelayAttachments?.hasAttachments?.());
    }}
    function updateRelayComposerAction() {{
      if (!followupSubmitButton) return;
      const showStop = relayTaskIsRunning() && !relayFollowupHasText() && !relayFollowupHasAttachments();
      followupSubmitButton.classList.toggle("is-stop", showStop);
      followupSubmitButton.setAttribute("aria-label", showStop ? "中断任务" : "发送补充");
    }}
    function marvisEscapeText(value) {{
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}
    function pendingInputId(item) {{
      return String(item?.id || item?.pending_input_id || "");
    }}
    function upsertPendingInput(item) {{
      const id = pendingInputId(item);
      if (!id) return;
      const next = {{ ...(pendingInputs.get(id) || {{}}), ...(item || {{}}), id }};
      if (!next.error_message) delete next.error_message;
      pendingInputs.set(id, next);
      renderPendingInputs();
    }}
    function removePendingInput(id) {{
      const key = String(id || "");
      if (key) pendingInputs.delete(key);
      renderPendingInputs();
    }}
    function visiblePendingInputs() {{
      return Array.from(pendingInputs.values()).filter((item) =>
        ["pending", "steered"].includes(String(item.status || ""))
      );
    }}
    function renderPendingInputs() {{
      if (!pendingInputsContainer) return;
      const rows = visiblePendingInputs();
      pendingInputsContainer.hidden = rows.length === 0;
      pendingInputsContainer.innerHTML = rows.map((item) => {{
        const id = pendingInputId(item);
        const isSteered = String(item.status || "") === "steered";
        const hasError = Boolean(item.error_message);
        const text = String(item.text || "").trim() || "已添加附件";
        const statusText = hasError ? String(item.error_message || "引导失败，仍已排队") : (isSteered ? "已引导当前，等待当前角色接收" : "已排队，当前 round 结束后自动开始");
        const className = `marvis-relay-pending-input${{isSteered ? " is-steered" : ""}}${{hasError ? " is-error" : ""}}`;
        const actions = isSteered ? "" : `<span class="marvis-relay-pending-actions">
            <button type="button" data-pending-steer="${{marvisEscapeText(id)}}">引导当前</button>
            <button type="button" data-pending-cancel="${{marvisEscapeText(id)}}">取消</button>
          </span>`;
        return `<article class="${{className}}" data-pending-input-id="${{marvisEscapeText(id)}}">
          <span class="marvis-relay-pending-status">${{statusText}}</span>
          <strong>${{marvisEscapeText(text)}}</strong>
          ${{actions}}
        </article>`;
      }}).join("");
    }}
    (INITIAL_PENDING_INPUTS || []).forEach(upsertPendingInput);
    function updateTaskStatus(status) {{
      if (!status) return;
      relayTaskStatus = String(status || "");
      if (followupComposer) followupComposer.dataset.taskStatusValue = relayTaskStatus;
      document.querySelectorAll("[data-task-status]").forEach((node) => {{
        node.textContent = TASK_STATUS_LABELS[status] || status;
      }});
      updateRelayComposerAction();
    }}
    pendingInputsContainer?.addEventListener("click", async (event) => {{
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const steerId = target.getAttribute("data-pending-steer");
      const cancelId = target.getAttribute("data-pending-cancel");
      const pendingId = steerId || cancelId;
      if (!pendingId) return;
      target.setAttribute("disabled", "disabled");
      const action = steerId ? "steer" : "cancel";
      try {{
        const response = await fetch(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/inputs/${{encodeURIComponent(pendingId)}}/${{action}}${{TOKEN_SUFFIX}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{}}),
        }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const payload = await response.json();
        const item = payload.pending_input || payload;
        if (action === "cancel") {{
          removePendingInput(pendingId);
        }} else {{
          upsertPendingInput(item);
          appendMarvisConversationGuidance(item);
        }}
      }} catch (_error) {{
        target.removeAttribute("disabled");
        const current = pendingInputs.get(String(pendingId)) || {{ id: pendingId, status: "pending" }};
        upsertPendingInput({{ ...current, error_message: "引导失败，仍已排队" }});
      }}
    }});
    let planControl = document.querySelector("[data-marvis-plan-control]");
    function confirmationOptionsFromPayload(payload = {{}}) {{
      const options = Array.isArray(payload.confirmation_options) ? payload.confirmation_options : [];
      return options.slice(0, 6).map((item, index) => {{
        if (typeof item === "string") {{
          const label = item.trim();
          return {{ id: `option_${{index + 1}}`, label, summary: "", instruction: label }};
        }}
        const source = item && typeof item === "object" ? item : {{}};
        const id = String(source.id || `option_${{index + 1}}`).trim();
        const label = String(source.label || source.title || source.name || source.summary || id).trim();
        const summary = String(source.summary || source.description || "").trim();
        const instruction = String(source.instruction || source.prompt || source.value || source.text || label).trim();
        if (!label && !instruction) return null;
        return {{ id, label: label || instruction, summary, instruction: instruction || label }};
      }}).filter(Boolean);
    }}
    function confirmationOptionsHtml(options) {{
      return options.map((option, index) => `<button class="marvis-relay-confirmation-option" type="button"
          data-confirmation-option-id="${{marvisEscapeText(option.id)}}"
          data-confirmation-option-label="${{marvisEscapeText(option.label)}}"
          data-confirmation-option-instruction="${{marvisEscapeText(option.instruction)}}"
          aria-pressed="${{index === 0 ? "true" : "false"}}">
          <strong>${{marvisEscapeText(option.label)}}</strong>
          <span>${{marvisEscapeText(option.summary || option.instruction)}}</span>
        </button>`).join("");
    }}
    function confirmationSourceLabel(payload = {{}}) {{
      const source = String(payload.confirmation_source || "");
      const provider = String(payload.provider || "").toLowerCase();
      if (source === "provider_native_plan" || source === "provider_native_approval") {{
        if (provider === "codex") return "Codex 原生确认";
        if (provider.startsWith("claude")) return "Claude 原生确认";
        return "Provider 原生确认";
      }}
      return "Relay 澄清确认";
    }}
    function hidePlanControlSurface() {{
      document.querySelectorAll("[data-marvis-plan-control]").forEach((node) => {{
        if (node instanceof HTMLElement) node.hidden = true;
      }});
      document.querySelectorAll("[data-marvis-confirmation-page]").forEach((node) => {{
        if (node instanceof HTMLElement) node.hidden = true;
      }});
      planControl = null;
    }}
    function ensurePlanControl(payload = {{}}) {{
      if (planControl && !planControl.hidden) return;
      if (planControl && planControl.hidden) {{
        planControl.remove();
        planControl = null;
      }}
      const roundId = String(payload.round_id || activeRelayRoundId || CURRENT_ROUND_ID || "1");
      const artifactId = String(payload.artifact_id || "0");
      const isPlanApproval = payload.waiting_reason === "plan_approval";
      const title = isPlanApproval ? "计划等待确认" : "等待确认";
      const primaryLabel = isPlanApproval ? "执行计划" : "选择执行";
      const primaryDecision = isPlanApproval ? "approve_plan" : "continue";
      const summary = String(payload.summary || payload.next_action || "当前接力需要你确认下一步。").trim();
      const sourceLabel = confirmationSourceLabel(payload);
      const confirmationKind = String(payload.confirmation_kind || "relay_question");
      const waitingReason = String(payload.waiting_reason || "");
      const providerRequestId = String(payload.provider_request_id || "");
      const metaText = [
        `来源：${{sourceLabel}}`,
        `请求类型：${{confirmationKind}}`,
        waitingReason ? `等待原因：${{waitingReason}}` : "",
        providerRequestId ? `请求 ID：${{providerRequestId}}` : "",
      ].filter(Boolean).join("\\n");
      const questions = Array.isArray(payload.open_questions) ? payload.open_questions.map((item) => String(item || "").trim()).filter(Boolean) : [];
      const options = confirmationOptionsFromPayload(payload);
      const optionHtml = confirmationOptionsHtml(options);
      const node = document.createElement("section");
      if (followupComposer) followupComposer.dataset.currentRoundId = roundId;
      node.className = "marvis-relay-confirmation-card";
      node.setAttribute("data-marvis-plan-control", "");
      node.setAttribute("data-marvis-confirmation-card", "");
      node.setAttribute("data-round-id", roundId);
      node.setAttribute("data-artifact-id", artifactId);
      node.setAttribute("aria-label", title);
      node.innerHTML = `<button class="marvis-relay-confirmation-thumb" type="button" data-marvis-confirmation-open aria-label="查看确认详情">
          <em>${{marvisEscapeText(sourceLabel)}}</em>
          <span>${{marvisEscapeText(title)}}</span>
          <strong>${{marvisEscapeText(summary)}}</strong>
        </button>
        <div class="marvis-relay-confirmation-options"${{optionHtml ? "" : " hidden"}}>
          ${{optionHtml}}
        </div>
        <div class="marvis-relay-confirmation-actions">
          <button type="button" data-plan-decision="${{marvisEscapeText(primaryDecision)}}">${{marvisEscapeText(primaryLabel)}}</button>
          <button type="button" data-waiting-input>补充内容</button>
          <button type="button" data-plan-decision="cancel_plan">停止</button>
        </div>`;
      document.querySelector(".marvis-relay-phone")?.appendChild(node);
      const page = document.createElement("div");
      page.className = "marvis-relay-confirmation-page";
      page.setAttribute("data-marvis-confirmation-page", "");
      page.hidden = true;
      page.innerHTML = `<div class="marvis-relay-confirmation-page-shell">
          <header>
            <button type="button" data-marvis-confirmation-close aria-label="返回">‹</button>
            <strong>${{marvisEscapeText(title)}}</strong>
          </header>
          <main>
            <small>${{marvisEscapeText(metaText)}}</small>
            <h2>${{marvisEscapeText(summary)}}</h2>
            <p>${{marvisEscapeText(summary + (questions.length ? "\\n\\n待确认：\\n" + questions.map((item) => "- " + item).join("\\n") : ""))}}</p>
          </main>
        </div>`;
      document.querySelector(".marvis-relay-phone")?.appendChild(page);
      planControl = node;
    }}
    document.addEventListener("click", async (event) => {{
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const openConfirmation = target.closest("[data-marvis-confirmation-open]");
      if (openConfirmation) {{
        document.querySelector("[data-marvis-confirmation-page]")?.removeAttribute("hidden");
        return;
      }}
      if (target.closest("[data-marvis-confirmation-close]")) {{
        document.querySelector("[data-marvis-confirmation-page]")?.setAttribute("hidden", "");
        return;
      }}
      const optionButton = target.closest("[data-confirmation-option-id]");
      if (optionButton instanceof HTMLElement) {{
        const control = optionButton.closest("[data-marvis-plan-control]");
        control?.querySelectorAll("[data-confirmation-option-id]").forEach((node) => {{
          if (node instanceof HTMLElement) node.setAttribute("aria-pressed", node === optionButton ? "true" : "false");
        }});
        return;
      }}
      if (target.hasAttribute("data-waiting-input")) {{
        waitingControlInput = "revise_plan";
        followupTextInput?.setAttribute("placeholder", "说明你的想法或修改要求");
        followupTextInput?.focus();
        return;
      }}
      const decision = target.getAttribute("data-plan-decision");
      if (!decision) return;
      if (decision === "revise_plan") {{
        waitingControlInput = "revise_plan";
        followupTextInput?.setAttribute("placeholder", "说明你的想法或修改要求");
        followupTextInput?.focus();
        return;
      }}
      const activePlanControl = target.closest("[data-marvis-plan-control]");
      if (!(activePlanControl instanceof HTMLElement)) return;
      target.setAttribute("disabled", "disabled");
      const roundId = activePlanControl.getAttribute("data-round-id") || CURRENT_ROUND_ID;
      const artifactId = activePlanControl.getAttribute("data-artifact-id") || "0";
      const selected = activePlanControl.querySelector("[data-confirmation-option-id][aria-pressed='true']") || activePlanControl.querySelector("[data-confirmation-option-id]");
      const controlPayload = {{ decision, artifact_id: Number(artifactId) || 0 }};
      if (selected instanceof HTMLElement && decision !== "cancel_plan") {{
        controlPayload.selected_option_id = selected.getAttribute("data-confirmation-option-id") || "";
        controlPayload.selected_option_label = selected.getAttribute("data-confirmation-option-label") || "";
        controlPayload.selected_option_instruction = selected.getAttribute("data-confirmation-option-instruction") || "";
      }}
      const shouldLeaveWaiting = decision === "approve_plan" || decision === "continue" || decision === "cancel_plan";
      if (shouldLeaveWaiting) {{
        hidePlanControlSurface();
        updateTaskStatus(decision === "cancel_plan" ? "interrupted" : "running");
        waitingControlInput = "";
      }}
      try {{
        const response = await fetch(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/rounds/${{encodeURIComponent(roundId)}}/control${{TOKEN_SUFFIX}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(controlPayload),
        }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        if (decision === "approve_plan" || decision === "continue") {{
          updateTaskStatus("running");
          waitingControlInput = "";
        }} else if (decision === "cancel_plan") {{
          updateTaskStatus("interrupted");
          waitingControlInput = "";
        }}
      }} catch (_error) {{
        target.removeAttribute("disabled");
        if (shouldLeaveWaiting) {{
          activePlanControl.hidden = false;
          planControl = activePlanControl;
          updateTaskStatus("waiting_user");
        }}
      }}
    }});
    let relayEventsAfter = Number(new URLSearchParams(String(EVENTS_SUFFIX || "").replace(/^\\?/, "")).get("after") || "0") || 0;
    let relayEventsSource = null;
    let relayEventsReconnectTimer = null;
    let relayEventsReconnectDelay = 500;
    const relayEventBindings = [];
    function updateRelayEventsCursor(event) {{
      const value = Number(event?.lastEventId || "0") || 0;
      if (value > relayEventsAfter) relayEventsAfter = value;
    }}
    function relayEventsSuffix() {{
      const params = new URLSearchParams(String(TOKEN_SUFFIX || "").replace(/^\\?/, ""));
      if (relayEventsAfter > 0) params.set("after", String(relayEventsAfter));
      const value = params.toString();
      return value ? `?${{value}}` : "";
    }}
    function addRelayEventListener(name, handler) {{
      relayEventBindings.push([name, handler]);
      if (relayEventsSource) relayEventsSource.addEventListener(name, handler);
    }}
    function scheduleRelayEventsReconnect() {{
      if (relayEventsReconnectTimer) return;
      if (relayEventsSource) {{
        relayEventsSource.close();
        relayEventsSource = null;
      }}
      relayEventsReconnectTimer = window.setTimeout(() => {{
        relayEventsReconnectTimer = null;
        connectRelayEventSource();
        relayEventsReconnectDelay = Math.min(relayEventsReconnectDelay * 2, 5000);
      }}, relayEventsReconnectDelay);
    }}
    function closeRelayEventSource() {{
      if (relayEventsReconnectTimer) {{
        clearTimeout(relayEventsReconnectTimer);
        relayEventsReconnectTimer = null;
      }}
      if (relayEventsSource) {{
        relayEventsSource.close();
        relayEventsSource = null;
      }}
    }}
    function connectRelayEventSource() {{
      closeRelayEventSource();
      relayEventsSource = new EventSource(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/events${{relayEventsSuffix()}}`);
      relayEventBindings.forEach(([name, handler]) => relayEventsSource.addEventListener(name, handler));
      relayEventsSource.onopen = () => {{
        relayEventsReconnectDelay = 500;
      }};
      relayEventsSource.onerror = () => {{
        scheduleRelayEventsReconnect();
      }};
    }}
    document.addEventListener("visibilitychange", () => {{
      if (document.visibilityState === "visible") connectRelayEventSource();
    }});
    window.addEventListener("pageshow", () => connectRelayEventSource());
    window.addEventListener("pagehide", closeRelayEventSource);
    window.addEventListener("beforeunload", closeRelayEventSource);
    addRelayEventListener("role.queued", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("running");
      const force = payload.reason === "new_followup_turn";
      if (force) clearMarvisConversationPausedRows();
      setRoleStatus(payload.role, "queued", {{ force }});
    }});
    addRelayEventListener("role.streaming", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("running");
      setRoleStatus(payload.role, "streaming");
    }});
    addRelayEventListener("dispatch.verified", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("running");
      setRoleStatus(payload.role, "streaming");
    }});
    addRelayEventListener("round.control", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      if (payload.decision === "cancel_plan") {{
        updateTaskStatus("interrupted");
      }} else {{
        updateTaskStatus("running");
        const nextRole = payload.next_role || payload.role || "";
        if (nextRole) setRoleStatus(nextRole, "queued", {{ force: true }});
      }}
      waitingControlInput = "";
    }});
    addRelayEventListener("dispatch.fallback", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      setRoleStatus(payload.role, "queued", {{ force: true }});
    }});
    addRelayEventListener("user.followup", (event) => {{
      const payload = parseRelayEvent(event);
      const roundId = activateRelayRound(payload);
      const key = payload.artifact_id ? `user_followup:${{payload.artifact_id}}` : `user_followup:${{payload.context_packet_id || Date.now()}}`;
      clearMarvisConversationPausedRows();
      appendMarvisConversationUser(payload.text || payload.latest_user_input || "", key, false, {{
        images: payload.images || [],
        files: payload.files || []
      }});
      appendMarvisConversationWaiting(roundId);
      updateTaskStatus("running");
      setRoleStatus("director", "queued", {{ force: true }});
    }});
    addRelayEventListener("user.input_queued", (event) => {{
      upsertPendingInput(parseRelayEvent(event));
    }});
    addRelayEventListener("user.input_steered", (event) => {{
      const payload = parseRelayEvent(event);
      upsertPendingInput(payload);
      appendMarvisConversationGuidance(payload);
    }});
    addRelayEventListener("user.input_cancelled", (event) => {{
      const payload = parseRelayEvent(event);
      removePendingInput(payload.id || payload.pending_input_id);
    }});
    addRelayEventListener("user.input_consumed", (event) => {{
      const payload = parseRelayEvent(event);
      removePendingInput(payload.id || payload.pending_input_id);
    }});
    addRelayEventListener("role.native_event", (event) => {{
      const payload = parseRelayEvent(event);
      renderMarvisWorkLogNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);
      if (!isCurrentRoundEvent(payload)) return;
      renderRelayNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);
    }});
    addRelayEventListener("role.output_delta", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      appendRoleStreamDelta(
        payload.role,
        payload.delta || payload.text || "",
        payload.runtime_event_id,
        payload
      );
      setRoleStatus(payload.role, "streaming");
    }});
    addRelayEventListener("role.followup_response", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const roundId = relayEventRoundId(payload);
      const displayText = payload.display_text || payload.text || payload.summary || "";
      if (marvisConversationTextIsStructuredArtifactPlaceholder(displayText)) {{
        setRoleStatus(payload.role || "director", payload.status || "passed");
        return;
      }}
      appendMarvisConversationAssistant(
        payload.role || "director",
        displayText,
        "followup_response",
        payload.artifact_id ? `followup_response:${{payload.artifact_id}}` : "",
        payload.status || "passed",
        roundId
      );
      setRoleStatus(payload.role || "director", payload.status || "passed");
    }});
    addRelayEventListener("routing.decision", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const role = payload.role || "director";
      clearRolePreview(role);
      setRoleStatus(role, payload.status || "passed");
    }});
    addRelayEventListener("role.envelope", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const envelope = {{ ...(payload.envelope || payload) }};
      const role = payload.role || envelope.role;
      clearRolePreview(role);
      const artifactType = String(envelope.artifact_type || "");
      const summaryText = String(envelope.summary || "");
      if (
        artifactType === "final_summary"
        && (role || "") === "director"
        && !String(envelope.handoff_to || "")
        && String(envelope.status || "") === "passed"
        && summaryText
        && !marvisConversationTextIsStructuredArtifactPlaceholder(summaryText)
      ) {{
        const finalSummaryKey = payload.artifact_id
          ? `final_summary_response:${{payload.artifact_id}}`
          : `final_summary_response:${{relayEventRoundId(envelope) || activeRelayRoundId || "1"}}:${{payload.runtime_event_id || event.lastEventId || summaryText}}`;
        appendMarvisConversationAssistant(
          role || "director",
          summaryText,
          "followup_response",
          finalSummaryKey,
          "passed",
          relayEventRoundId(envelope)
        );
      }}
      if (envelope.status) setRoleStatus(role, envelope.status);
    }});
    addRelayEventListener("handoff.created", (event) => {{
      const payload = parseRelayEvent(event);
      const toRole = payload.to_role || payload.handoff_to;
      const fromRole = payload.from_role || "";
      const roundId = String(payload.round_id || "1");
      if (!isCurrentRoundEvent(payload)) return;
      if (toRole) setRoleStatus(toRole, "queued");
      const handoffKey = `handoff:${{roundId}}:${{fromRole}}:${{toRole || ""}}:${{payload.artifact_id || payload.summary || event.lastEventId || ""}}`;
      appendMarvisConversationHandoff(toRole, handoffKey, fromRole, roundId);
    }});
    addRelayEventListener("role.status", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const reason = payload.reason || payload.payload?.reason || "";
      const force = reason === "new_followup_turn";
      if (force) clearMarvisConversationPausedRows();
      setRoleStatus(payload.role, payload.status, {{ force }});
      if (TERMINAL_ROLE_STATUSES.has(payload.status)) {{
        hidePlanControlSurface();
        clearRolePreview(payload.role);
      }}
    }});
    addRelayEventListener("task.waiting_user", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      updateTaskStatus("waiting_user");
      setRoleStatus(payload.role || "director", "waiting", {{ force: true }});
      renderMarvisWorkLogConfirmation(payload);
      ensurePlanControl(payload);
    }});
    addRelayEventListener("task.completed", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("completed");
      clearAllRolePreviews();
    }});
    addRelayEventListener("task.interrupted", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("interrupted");
      clearAllRolePreviews();
    }});
    connectRelayEventSource();
    followupTextInput?.addEventListener("input", updateRelayComposerAction);
    document.addEventListener("marvis-relay-attachments-changed", updateRelayComposerAction);
    updateRelayComposerAction();
    followupComposer?.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const form = event.currentTarget;
      const data = Object.fromEntries(new FormData(form).entries());
      const attachments = window.marvisRelayAttachments?.payload() || {{}};
      const hasAttachments = Boolean((attachments.images || []).length || (attachments.files || []).length);
      if (!String(data.text || "").trim() && !hasAttachments) {{
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
          }}
        }}
        return;
      }}
      if ((attachments.images || []).length) data.images = attachments.images;
      if ((attachments.files || []).length) data.files = attachments.files;
      if (String(relayTaskStatus || "").trim() === "waiting_user" && waitingControlInput) {{
        const roundId = activeRelayRoundId || form.dataset.currentRoundId || CURRENT_ROUND_ID;
        const response = await fetch(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/rounds/${{encodeURIComponent(roundId)}}/control${{TOKEN_SUFFIX}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ decision: waitingControlInput, comment: String(data.text || "").trim() }}),
        }});
        if (!response.ok) return;
        waitingControlInput = "";
        form.reset();
        window.marvisRelayAttachments?.clear();
        hidePlanControlSurface();
        updateTaskStatus("running");
        updateRelayComposerAction();
        return;
      }}
      if (relayTaskAcceptsPendingInput()) {{
        const response = await fetch(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/inputs${{TOKEN_SUFFIX}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(data),
        }});
        if (!response.ok) return;
        const payload = await response.json();
        const responseDisposition = String(payload.disposition || "pending");
        if (responseDisposition === "followup") {{
          const followup = payload.followup || payload;
          const roundId = activateRelayRound(followup);
          const key = followup.artifact_id ? `user_followup:${{followup.artifact_id}}` : `user_followup:${{followup.context_packet_id || Date.now()}}`;
          clearMarvisConversationPausedRows();
          appendMarvisConversationUser(followup.text || data.text || "已添加附件", key, false, {{
            images: followup.images || attachments.images || [],
            files: followup.files || attachments.files || []
          }});
          appendMarvisConversationWaiting(roundId);
          updateTaskStatus("running");
          setRoleStatus("director", "queued", {{ force: true }});
        }} else {{
          upsertPendingInput(payload.pending_input || payload);
        }}
        form.reset();
        window.marvisRelayAttachments?.clear();
        updateRelayComposerAction();
        return;
      }}
      const localKey = `local-followup:${{Date.now()}}`;
      clearMarvisConversationPausedRows();
      appendMarvisConversationUser(data.text || "已添加附件", localKey, true, attachments);
      appendMarvisConversationWaiting();
      updateTaskStatus("running");
      setRoleStatus("director", "queued", {{ force: true }});
      const response = await fetch(`/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/message${{TOKEN_SUFFIX}}`, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(data),
      }});
      if (!response.ok) {{
        markMarvisConversationUserFailed(localKey);
        clearMarvisConversationWaiting();
        return;
      }}
      form.reset();
      window.marvisRelayAttachments?.clear();
      updateRelayComposerAction();
    }});
    document.querySelector("[data-interrupt-url]")?.addEventListener("click", async (event) => {{
      const target = event.currentTarget;
      const response = await fetch(`${{target.dataset.interruptUrl}}${{TOKEN_SUFFIX}}`, {{ method: "POST" }});
      if (response.ok) {{
        updateTaskStatus("interrupted");
      }}
    }});
  </script>
</body>
</html>""")


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
    events = _relay_worker_events_for_roles(role_jobs, hub=hub)
    job_by_role = {str(getattr(job, "role", "") or ""): job for job in role_jobs}
    user_followup_texts = {
        _relay_user_message_dedupe_text(str(artifact.get("text") or ""))
        for artifact in (artifacts or [])
        if str(artifact.get("artifact_type") or "") == "user_followup"
        and _relay_user_message_dedupe_text(str(artifact.get("text") or ""))
    }
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
        if (
            kind in {"text_delta", "message_completed"}
            and (
                _relay_text_is_structured_artifact_placeholder(text)
                or text_contains_relay_protocol_payload(text)
                or _relay_parse_role_envelope_payload(text) is not None
            )
        ):
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
                    "round_id": "",
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
        if projected is None and str(row.get("kind") or "") == "message_completed":
            role = str(row.get("role") or "")
            body = _relay_sanitize_protocol_leak_text(role, str(row.get("body") or ""))
            if (
                body
                and not _relay_text_is_structured_artifact_placeholder(body)
                and not text_contains_relay_protocol_payload(body)
                and _relay_parse_role_envelope_payload(body) is None
                and not _relay_conversation_row_is_task_status_noise(
                    {"kind": "message_completed", "body": body}
                )
            ):
                projected = {**row, "body": body}
        if projected is None:
            continue
        if _relay_conversation_row_is_task_status_noise(projected):
            continue
        if str(projected.get("kind") or "") == "user_message":
            body = str(projected.get("body") or "").strip()
            dedupe_body = _relay_user_message_dedupe_text(body)
            if not dedupe_body or dedupe_body in user_followup_texts:
                continue
            if dedupe_body in seen_user_bodies:
                continue
            seen_user_bodies.add(dedupe_body)
        key = str(projected.get("key") or "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        projected_rows.append(projected)

    if artifacts is not None:
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
                dedupe_body = _relay_user_message_dedupe_text(body)
                if not dedupe_body or dedupe_body in seen_user_bodies:
                    continue
                seen_user_bodies.add(dedupe_body)
            key = str(artifact_row.get("key") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            projected_rows.append(artifact_row)
        pending_row = _relay_pending_followup_waiting_row(artifacts, job_by_role)
        if pending_row is not None:
            key = str(pending_row.get("key") or "")
            if not key or key not in seen_keys:
                if key:
                    seen_keys.add(key)
                projected_rows.append(pending_row)
    _relay_prune_direct_final_summary_rows(projected_rows)
    _relay_normalize_conversation_lifecycle_rows(projected_rows)
    has_pending_followup_waiting = any(
        str(row.get("kind") or "") == "waiting"
        and str(row.get("key") or "").startswith("followup-waiting:")
        for row in projected_rows
    )
    blocked_role = _relay_first_blocked_role(role_jobs)
    current_round_id = _relay_current_round_id_from_artifacts(artifacts)
    has_current_round_blocked_role_result = any(
        str(row.get("role") or "") == blocked_role
        and str(row.get("kind") or "") in {"role_envelope", "followup_response", "role_process"}
        and (
            str(row.get("kind") or "") == "followup_response"
            or str(row.get("artifact_type") or "") == "final_summary"
        )
        and str(row.get("round_id") or current_round_id) == current_round_id
        for row in projected_rows
    )
    if (
        blocked_role
        and not has_pending_followup_waiting
        and not has_current_round_blocked_role_result
    ):
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


def _relay_prune_direct_final_summary_rows(rows: list[dict[str, Any]]) -> None:
    non_director_round_ids = {
        str(row.get("round_id") or "").strip()
        for row in rows
        if str(row.get("role") or "") != "director"
        and str(row.get("kind") or "") in {"role_process", "followup_response", "message_completed"}
    }
    non_director_round_ids.discard("")
    has_unscoped_non_director_process = any(
        str(row.get("role") or "") != "director"
        and str(row.get("kind") or "") in {"role_process", "followup_response", "message_completed"}
        and not str(row.get("round_id") or "").strip()
        for row in rows
    )
    if not non_director_round_ids and not has_unscoped_non_director_process:
        return
    rows[:] = [
        row
        for row in rows
        if not (
            str(row.get("key") or "").startswith("final_summary_response:")
            and _relay_direct_final_summary_should_be_pruned(
                row,
                non_director_round_ids=non_director_round_ids,
                has_unscoped_non_director_process=has_unscoped_non_director_process,
            )
        )
    ]


def _relay_direct_final_summary_should_be_pruned(
    row: dict[str, Any],
    *,
    non_director_round_ids: set[str],
    has_unscoped_non_director_process: bool,
) -> bool:
    round_id = str(row.get("round_id") or "").strip()
    if round_id:
        return round_id in non_director_round_ids
    return has_unscoped_non_director_process or bool(non_director_round_ids)


def _relay_pending_followup_waiting_row(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    job_by_role: dict[str, Any],
) -> dict[str, str] | None:
    latest_followup_index = -1
    latest_followup_key = ""
    for index, artifact in enumerate(artifacts):
        if str(artifact.get("artifact_type") or "") != "user_followup":
            continue
        latest_followup_index = index
        latest_followup_key = str(artifact.get("id") or artifact.get("created_at") or index)
    if latest_followup_index < 0:
        return None
    for artifact in artifacts[latest_followup_index + 1 :]:
        artifact_type = str(artifact.get("artifact_type") or "")
        if artifact_type in {
            "followup_response",
            "routing_decision",
            "role_envelope",
            "final_summary",
            "implementation_report",
            "audit_report",
            "role_error",
        }:
            return None
        if _relay_canonical_payload_from_artifact(artifact) is not None:
            return None
    director = job_by_role.get("director")
    director_status = str(getattr(director, "status", "") or "")
    if director_status not in {"queued", "streaming"}:
        return None
    return {
        "role": "director",
        "kind": "waiting",
        "speaker": "Marvis",
        "meta": "进行中",
        "body": "...",
        "key": f"followup-waiting:{latest_followup_key}",
        "status": "streaming",
        "round_id": str(artifacts[latest_followup_index].get("round_id") or ""),
    }


def _relay_user_message_dedupe_text(text: str) -> str:
    body = str(text or "").strip()
    for marker in ("\n\n用户附带图片：", "\n\n用户附带文件："):
        if marker in body:
            body = body.split(marker, 1)[0].strip()
    return " ".join(body.split())


def _relay_round_id_sort_value(value: Any) -> int:
    try:
        return int(str(value or "").strip() or "0")
    except (TypeError, ValueError):
        return 0


def _relay_current_round_id_from_artifacts(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> str:
    current = 0
    for artifact in artifacts or ():
        current = max(current, _relay_round_id_sort_value(artifact.get("round_id")))
    return str(current or 1)


def _relay_payload_status_is_success(status: str) -> bool:
    return str(status or "").strip() in {"passed", "completed", "success", "succeeded", "done"}


def _relay_payload_status_is_terminal_failure(status: str) -> bool:
    return str(status or "").strip() in {"failed", "blocked", "error", "interrupted"}


def _relay_lifecycle_status_for_payload(
    payload: dict[str, Any],
    success_roles_in_round: set[str] | None = None,
) -> str:
    status = str(payload.get("status") or "").strip()
    role = str(payload.get("role") or payload.get("relay_role") or "").strip()
    artifact_type = str(payload.get("artifact_type") or "").strip()
    handoff_to = str(payload.get("handoff_to") or "").strip()
    if (
        role == "director"
        and artifact_type == "final_summary"
        and status == "waiting"
        and not handoff_to
        and "auditor" in (success_roles_in_round or set())
    ):
        return "passed"
    if not status and artifact_type in {"followup_response", "final_summary"} and not handoff_to:
        return "passed"
    return status or "passed"


def _relay_conversation_artifact_meta(payload: dict[str, Any]) -> str:
    status = _relay_lifecycle_status_for_payload(payload)
    role = str(payload.get("role") or payload.get("relay_role") or "").strip()
    artifact_type = str(payload.get("artifact_type") or "").strip()
    handoff_to = str(payload.get("handoff_to") or "").strip()
    if (
        role == "director"
        and artifact_type == "final_summary"
        and status == "waiting"
        and handoff_to
    ):
        return "passed"
    return status


def _relay_normalize_conversation_lifecycle_rows(rows: list[dict[str, str]]) -> None:
    success_roles_by_round: dict[str, set[str]] = {}
    for row in rows:
        if str(row.get("kind") or "") not in {
            "role_envelope",
            "followup_response",
            "role_process",
        }:
            continue
        role = str(row.get("role") or "")
        if not role:
            continue
        round_id = str(row.get("round_id") or "")
        success_roles = success_roles_by_round.setdefault(round_id, set())
        status = str(row.get("status") or row.get("meta") or "").strip()
        artifact_type = str(row.get("artifact_type") or "").strip()
        handoff_to = str(row.get("handoff_to") or "").strip()
        if (
            role == "director"
            and artifact_type == "final_summary"
            and status == "waiting"
            and not handoff_to
            and "auditor" in success_roles
        ):
            row["status"] = "passed"
            row["meta"] = "passed"
            status = "passed"
        if _relay_payload_status_is_success(status):
            success_roles.add(role)
        elif _relay_payload_status_is_terminal_failure(status):
            success_roles.discard(role)


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
            "images": list(artifact.get("images") or []),
            "files": list(artifact.get("files") or []),
            "round_id": str(artifact.get("round_id") or ""),
        }
    if artifact_type == "relay_board":
        text = str(artifact.get("latest_user_input") or "").strip()
        summary = str(artifact.get("summary") or "")
        next_step = str(artifact.get("next_step") or "")
        is_followup_board = (
            summary == "User follow-up routed to director"
            or next_step == "director review latest user input"
        )
        dedupe_text = _relay_user_message_dedupe_text(text)
        if not is_followup_board or not text or dedupe_text in user_followup_texts:
            return None
        return {
            "role": "user",
            "kind": "user_message",
            "speaker": "你",
            "meta": "",
            "body": _relay_humanize_user_message(text),
            "key": f"relay_board_followup:{artifact_key}",
            "round_id": str(artifact.get("round_id") or ""),
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
            "round_id": str(artifact.get("round_id") or ""),
        }
    if artifact_type in {"role_dispatch_metadata", "role_error", "role_artifact_invalid"}:
        return None
    if artifact_type == "followup_response":
        text = str(artifact.get("text") or artifact.get("summary") or "").strip()
        if not text:
            return None
        role = str(artifact.get("role") or artifact.get("relay_role") or "director")
        if _relay_text_is_structured_artifact_placeholder(text):
            return None
        display_text = _relay_followup_response_display_text(role, text)
        if _relay_text_is_structured_artifact_placeholder(display_text):
            return None
        status = _relay_lifecycle_status_for_payload(artifact)
        return {
            "role": role,
            "kind": "followup_response",
            "speaker": _relay_role_label(role),
            "meta": status,
            "body": display_text,
            "key": f"followup_response:{artifact_key}",
            "status": status,
            "round_id": str(artifact.get("round_id") or ""),
        }
    payload = _relay_canonical_payload_from_artifact(artifact)
    if payload is None:
        return None
    direct_final_row = _relay_conversation_direct_final_summary_row(
        payload,
        artifact_key=artifact_key,
        artifact=artifact,
    )
    if direct_final_row is not None:
        return direct_final_row
    process_row = _relay_conversation_product_process_row(
        payload,
        artifact_key=artifact_key,
        artifact=artifact,
    )
    if process_row is not None:
        return process_row
    from_role = str(payload.get("role") or payload.get("relay_role") or "")
    to_role = str(payload.get("handoff_to") or "")
    if from_role and to_role and from_role != to_role:
        return {
            "role": to_role,
            "kind": "handoff",
            "speaker": _relay_role_label(from_role),
            "meta": "",
            "body": str(payload.get("summary") or artifact.get("summary") or ""),
            "key": f"handoff:{from_role}:{to_role}:{artifact_key}",
            "from_role": from_role,
            "to_role": to_role,
            "round_id": str(payload.get("round_id") or artifact.get("round_id") or ""),
        }
    return None


def _relay_conversation_direct_final_summary_row(
    payload: dict[str, Any],
    *,
    artifact_key: str,
    artifact: dict[str, Any],
) -> dict[str, str] | None:
    role = str(payload.get("role") or payload.get("relay_role") or "").strip()
    artifact_type = str(payload.get("artifact_type") or "").strip()
    handoff_to = str(payload.get("handoff_to") or "").strip()
    if role != "director" or artifact_type != "final_summary" or handoff_to:
        return None
    if not _relay_payload_status_is_success(str(payload.get("status") or "")):
        return None
    body = _relay_direct_final_summary_body(payload, artifact)
    if not body:
        return None
    return {
        "role": role,
        "kind": "followup_response",
        "speaker": _relay_role_label(role),
        "meta": "passed",
        "body": body,
        "key": f"final_summary_response:{artifact_key}",
        "status": "passed",
        "artifact_type": artifact_type,
        "round_id": str(payload.get("round_id") or artifact.get("round_id") or ""),
    }


def _relay_direct_final_summary_body(
    payload: dict[str, Any],
    artifact: dict[str, Any],
) -> str:
    raw = str(payload.get("summary") or artifact.get("summary") or "").strip()
    if not raw:
        return ""
    if _relay_text_is_structured_artifact_placeholder(raw):
        return ""
    if _relay_direct_final_summary_has_internal_terms(raw):
        return ""
    body = _relay_humanize_display_text(raw).strip()
    body = _relay_replace_legacy_role_identifiers(body)
    if not body or _relay_text_is_structured_artifact_placeholder(body):
        return ""
    if _relay_text_needs_chinese_fallback(body):
        return ""
    if _relay_direct_final_summary_is_generic(body):
        return ""
    if _relay_direct_final_summary_has_internal_terms(body):
        return ""
    return _relay_sanitize_protocol_leak_text("director", body)


def _relay_direct_final_summary_is_generic(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or "")).strip("。；;,.，")
    return value.lower() in {
        "completed",
        "done",
        "passed",
        "success",
        "已完成",
        "任务已完成",
        "已完成任务",
        "搞定，有请下一位",
        "角色已返回结构化结果",
        "该角色已返回结构化结果，详情见结构化数据",
    }


def _relay_direct_final_summary_has_internal_terms(text: str) -> bool:
    value = str(text or "")
    markers = (
        "File/Search/Computer Agent",
        "Marvis/File/Search/Computer Agent",
        "expected_output_envelope",
        "结构化数据",
        "验收依据",
        "role_envelope",
        "routing_decision",
    )
    return any(marker in value for marker in markers)


def _relay_conversation_product_process_row(
    payload: dict[str, Any],
    *,
    artifact_key: str,
    artifact: dict[str, Any],
) -> dict[str, str] | None:
    role = str(payload.get("role") or payload.get("relay_role") or "").strip()
    artifact_type = str(payload.get("artifact_type") or "").strip()
    handoff_to = str(payload.get("handoff_to") or "").strip()
    if not role or not artifact_type:
        return None

    allowed_role_artifacts = {
        "architecture_plan",
        "implementation_report",
        "test_report",
        "audit_report",
    }
    is_director_dispatch = (
        role == "director"
        and artifact_type in {"routing_decision", "final_summary"}
        and bool(handoff_to)
    )
    is_role_process = role != "director" and artifact_type in allowed_role_artifacts
    if not is_director_dispatch and not is_role_process:
        return None

    body = _relay_product_process_body(payload, artifact)
    if not body:
        return None
    status = "passed" if is_director_dispatch else _relay_lifecycle_status_for_payload(payload)
    return {
        "role": role,
        "kind": "role_process",
        "speaker": _relay_role_label(role),
        "meta": status,
        "body": body,
        "key": f"process:{role}:{artifact_type}:{artifact_key}",
        "artifact_type": artifact_type,
        "status": status,
        "handoff_to": handoff_to,
        "display_summary": body,
        "round_id": str(payload.get("round_id") or artifact.get("round_id") or ""),
    }


def _relay_product_process_body(
    payload: dict[str, Any],
    artifact: dict[str, Any],
) -> str:
    for key in ("summary", "output", "reason"):
        value = str(payload.get(key) or artifact.get(key) or "").strip()
        if not value:
            continue
        value = _relay_humanize_display_text(value).strip()
        value = _relay_replace_legacy_role_identifiers(value)
        if _relay_text_is_structured_artifact_placeholder(value):
            continue
        if _relay_text_needs_chinese_fallback(value):
            continue
        return _relay_sanitize_protocol_leak_text(str(payload.get("role") or ""), value)
    return ""


def _marvis_chat_rows_from_relay_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chat_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for event in marvis_chat_events(project_relay_rows_to_marvis_interactions(rows)):
        row = event.metadata.get("relay_row")
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        chat_rows.append(row)
    return chat_rows


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
    rows = _marvis_chat_rows_from_relay_rows(rows)
    if not rows:
        if any(
            str(getattr(job, "status", "") or "") in {"queued", "streaming"} for job in role_jobs
        ):
            return _marvis_relay_waiting_message_html()
        return _marvis_relay_empty_conversation_html()
    current_round_id = _relay_current_round_id_from_artifacts(artifacts)

    def is_current_round_row(row: dict[str, str]) -> bool:
        round_id = str(row.get("round_id") or "").strip()
        return not round_id or round_id == current_round_id

    html_rows: list[str] = []
    previous_role = ""
    previous_round_id = ""
    handoffs_by_role: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if str(row.get("kind") or "") != "handoff":
            continue
        if not is_current_round_row(row):
            continue
        to_role = str(row.get("to_role") or row.get("role") or "")
        if to_role:
            handoffs_by_role.setdefault(to_role, []).append(row)
    rendered_handoffs: set[str] = set()
    rendered_handoff_identities: set[tuple[str, str, str, str]] = set()

    def append_handoff_once(handoff: dict[str, str]) -> bool:
        if not is_current_round_row(handoff):
            return False
        key = str(handoff.get("key") or "")
        pair = _marvis_relay_handoff_pair(handoff)
        if pair is None:
            return False
        identity = _marvis_relay_handoff_identity(handoff)
        if identity in rendered_handoff_identities:
            return True
        if key and key in rendered_handoffs:
            return True
        html = _marvis_relay_handoff_html(handoff)
        if not html:
            return False
        html_rows.append(html)
        rendered_handoff_identities.add(identity)
        if key:
            rendered_handoffs.add(key)
        return True

    for row in rows:
        role = str(row.get("role") or "")
        kind = str(row.get("kind") or "")
        row_round_id = str(row.get("round_id") or current_round_id)
        if row_round_id != previous_round_id:
            previous_role = ""
            previous_round_id = row_round_id
        if kind == "handoff":
            if is_current_round_row(row):
                append_handoff_once(row)
            continue
        if kind == "user_message":
            html_rows.append(_marvis_relay_message_html(row))
            previous_role = ""
            continue
        if kind == "role_process" and not is_current_round_row(row):
            continue
        if role:
            for handoff in handoffs_by_role.get(role, []):
                append_handoff_once(handoff)
        if kind in {"role_envelope", "role_process"} and previous_role and role and role != previous_role:
            for handoff in handoffs_by_role.get(role, []):
                if _marvis_relay_handoff_pair(handoff) == (previous_role, role):
                    append_handoff_once(handoff)
        if (
            kind in {"role_envelope", "role_process"}
            and is_current_round_row(row)
            and previous_role == "director"
            and role
            and role != "director"
            and _marvis_relay_handoff_identity({**row, "from_role": previous_role, "to_role": role})
            not in rendered_handoff_identities
        ):
            synthetic_key = f"synthetic-handoff:{previous_role}:{role}"
            append_handoff_once(
                {
                    "from_role": previous_role,
                    "to_role": role,
                    "role": role,
                    "round_id": str(row.get("round_id") or ""),
                    "key": synthetic_key,
                }
            )
        html_rows.append(_marvis_relay_message_html(row))
        if kind in {"role_envelope", "role_process"}:
            previous_role = role
    return "\n".join(html_rows)


def _marvis_relay_handoff_html(row: dict[str, str]) -> str:
    pair = _marvis_relay_handoff_pair(row)
    if pair is None:
        return ""
    from_role, to_role = pair
    key = str(row.get("key") or "")
    round_id = str(row.get("round_id") or "1")
    text = _marvis_relay_handoff_text(from_role, to_role)
    return (
        '<div class="marvis-relay-handoff" data-marvis-handoff '
        f'data-native-kind="handoff" data-native-from-role="{escape(from_role)}" '
        f'data-native-to-role="{escape(to_role)}" data-native-role="{escape(to_role)}" '
        f'data-native-round-id="{escape(round_id)}" '
        f'data-native-key="{escape(key)}">'
        f"{escape(text)}"
        "</div>"
    )


def _marvis_relay_handoff_pair(row: dict[str, str]) -> tuple[str, str] | None:
    from_role = str(row.get("from_role") or "").strip()
    to_role = str(row.get("to_role") or row.get("role") or "").strip()
    if not from_role or not to_role:
        return None
    if from_role == to_role:
        return None
    return from_role, to_role


def _marvis_relay_handoff_identity(row: dict[str, str]) -> tuple[str, str, str, str]:
    pair = _marvis_relay_handoff_pair(row)
    if pair is None:
        return ("", "", "", "")
    round_id = str(row.get("round_id") or "1")
    return (round_id, pair[0], pair[1], _marvis_relay_handoff_semantic_action(pair[0], pair[1]))


def _marvis_relay_handoff_semantic_action(from_role: str, to_role: str) -> str:
    if from_role == "director":
        return "director_dispatch"
    if from_role == "auditor" and to_role == "director":
        return "audit_to_director"
    if from_role == "auditor":
        return "audit_return"
    if to_role == "auditor":
        return "submit_for_audit"
    return "role_handoff"


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
        attachment_html = _marvis_relay_attachment_list_html(row)
        bubble_html = (
            f'<div class="marvis-relay-user-bubble" data-native-message-body>{escape(body)}</div>'
            if body
            else ""
        )
        return f"""
      <article class="marvis-relay-user-message" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(key)}">
        {bubble_html}
        {attachment_html}
      </article>
        """
    if kind == "waiting":
        persona, display_name = _marvis_relay_public_role("director")
        return f"""
      <article class="marvis-relay-agent-step marvis-relay-waiting" data-native-role="director" data-native-kind="waiting" data-native-key="{escape(key)}" data-marvis-followup-waiting="true">
        {_marvis_relay_avatar_html(persona, label=display_name)}
        <div class="marvis-relay-agent-content">
          <div class="marvis-relay-agent-head"><strong>{escape(display_name)}</strong></div>
          <div class="marvis-relay-agent-bubble" data-native-message-body>{escape(body or "...")}</div>
        </div>
      </article>
    """
    persona, display_name = _marvis_relay_public_role(role)
    meta = str(row.get("meta") or row.get("status") or "")
    status_label = _marvis_relay_role_status_label(meta)
    action = _marvis_relay_action_label(role, row)
    role_final_attr = (
        f' data-conversation-role-final="{escape(role)}"' if kind == "role_envelope" else ""
    )
    role_stream_attr = (
        f' data-conversation-role-stream="{escape(role)}"' if kind == "text_delta" else ""
    )
    stream_event_ids = str(row.get("preview_event_ids") or "")
    stream_event_ids_attr = (
        f' data-stream-event-ids="{escape(stream_event_ids)}"'
        if kind == "text_delta" and stream_event_ids
        else ""
    )
    show_action = kind in {"role_envelope", "role_process", "text_delta"} or role == "director"
    if show_action and action:
        action_html = (
            f'<span class="marvis-relay-agent-action">| {escape(action)} '
            f"{escape(status_label)}</span>"
        )
    elif show_action and status_label:
        action_html = f'<span class="marvis-relay-agent-action">| {escape(status_label)}</span>'
    else:
        action_html = ""
    return f"""
      <article class="marvis-relay-agent-step" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(key)}"{role_final_attr}{role_stream_attr}{stream_event_ids_attr}>
        {_marvis_relay_avatar_html(persona, label=display_name)}
        <div class="marvis-relay-agent-content">
          <div class="marvis-relay-agent-head"><strong>{escape(display_name)}</strong> {action_html}</div>
          <div class="marvis-relay-agent-bubble" data-native-message-body>{escape(body)}</div>
        </div>
      </article>
    """


def _marvis_relay_attachment_list_html(row: dict[str, Any]) -> str:
    image_items: list[str] = []
    file_items: list[str] = []
    for raw in list(row.get("images") or [])[:_MAX_NATIVE_IMAGE_ATTACHMENTS]:
        if not isinstance(raw, dict):
            continue
        src = str(raw.get("url") or raw.get("data_url") or "")
        if not (
            src.startswith("data:image/") or src.startswith("http://") or src.startswith("https://")
        ):
            continue
        image_items.append(
            '<img class="marvis-relay-message-image" '
            f'src="{escape(src, quote=True)}" alt="" loading="lazy">'
        )
    for raw in list(row.get("files") or [])[:_MAX_RELAY_TEXT_ATTACHMENTS]:
        if not isinstance(raw, dict):
            continue
        filename = str(raw.get("filename") or "文件")
        file_items.append(
            '<span class="marvis-relay-attachment-chip marvis-relay-attachment-chip-file">'
            '<span class="marvis-relay-attachment-icon" aria-hidden="true"></span>'
            f"<span>{escape(filename)}</span>"
            "</span>"
        )
    parts: list[str] = []
    if image_items:
        parts.append('<div class="marvis-relay-message-images">' + "".join(image_items) + "</div>")
    if file_items:
        parts.append('<div class="marvis-relay-attachment-list">' + "".join(file_items) + "</div>")
    if not parts:
        return ""
    return "".join(parts)


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
    if kind == "message_completed" and not _relay_text_is_structured_artifact_placeholder(body):
        return {**row, "body": _relay_sanitize_protocol_leak_text(str(row.get("role") or ""), body)}
    if (
        kind == "text_delta"
        and _relay_role_job_is_live_preview(job)
        and not _relay_text_is_structured_artifact_placeholder(body)
    ):
        role = str(row.get("role") or "")
        return {
            **row,
            "kind": "text_delta",
            "meta": str(row.get("meta") or ""),
            "body": _relay_sanitize_protocol_leak_text(role, body),
        }
    return None


def _relay_role_job_is_live_preview(job: Any | None) -> bool:
    if job is None:
        return False
    status = str(getattr(job, "status", "") or "")
    if status in {"blocked", "failed", "interrupted", "passed", "completed"}:
        return False
    return status == "streaming" or bool(getattr(job, "turn_running", False))


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
        return None

    if not _relay_text_looks_like_role_envelope(body):
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and _relay_dict_looks_like_role_envelope(parsed):
        return None

    error = str(getattr(job, "error_message", "") or "").strip() if job else ""
    status = str(getattr(job, "status", "") or "").strip() if job else ""
    if error or status in {"blocked", "failed"}:
        return None
    return None


def _relay_text_is_structured_artifact_placeholder(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    if value.startswith("{") and (
        _relay_parse_role_envelope_payload(value) is not None
        or _relay_text_looks_like_role_envelope(value)
    ):
        return True
    if text_contains_relay_protocol_payload(value):
        return True
    placeholders = (
        "结构化结果缺少",
        "结构化结果不是合法",
        "结构化结果未采用",
        "结构化产物未采用",
        "结构化输出已由系统处理",
        "详情见结构化数据",
        "原始协议内容不在主会话展示",
        "输出格式异常",
        "任务已阻塞",
        "invalid json",
    )
    return any(marker in value for marker in placeholders)


def _relay_text_looks_like_role_envelope(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    if text_contains_relay_protocol_payload(stripped):
        return True
    markers = (
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
    return sanitize_protocol_leak_text(role, text)


def _relay_followup_response_display_text(role: str, text: str) -> str:
    return followup_response_display_text(role, text)


def _relay_dict_looks_like_role_envelope(payload: dict[str, Any]) -> bool:
    return relay_dict_looks_like_role_envelope(payload)


def _relay_humanize_role_envelope(payload: dict[str, Any]) -> str:
    return humanize_role_envelope(payload)


def _relay_join_text_list(value: Any) -> str:
    return join_text_list(value)


def _relay_role_output_error_text(role: str, error: str) -> str:
    return role_output_error_text(role, error)


def _relay_protocol_output_hidden_text(role: str) -> str:
    return protocol_output_hidden_text(role)


def _relay_native_message_html(row: dict[str, str]) -> str:
    meta = str(row.get("meta") or "")
    meta_html = f'<span class="relay-message-meta">{escape(meta)}</span>' if meta else ""
    role = str(row.get("role", "") or "system")
    kind = str(row.get("kind", "") or "event")
    role_final_attr = (
        f' data-conversation-role-final="{escape(role)}"' if kind == "role_envelope" else ""
    )
    role_stream_attr = (
        f' data-conversation-role-stream="{escape(role)}"' if kind == "text_delta" else ""
    )
    stream_event_ids = str(row.get("preview_event_ids") or "")
    stream_event_ids_attr = (
        f' data-stream-event-ids="{escape(stream_event_ids)}"'
        if kind == "text_delta" and stream_event_ids
        else ""
    )
    return f"""
      <article class="relay-message" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(row.get("key", "") or "")}"{role_final_attr}{role_stream_attr}{stream_event_ids_attr}>
        {_marvis_relay_avatar_html(role, label=str(row.get("speaker", "") or "系统"))}
        <div class="relay-message-head">
          <strong>{escape(row.get("speaker", "") or "系统")}</strong>
          {meta_html}
        </div>
        <div class="relay-message-body" data-native-message-body>{escape(row.get("body", "") or "")}</div>
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


def _relay_role_canonical_payloads_by_role(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    success_roles_by_round: dict[str, set[str]] = {}
    for artifact in artifacts:
        payload = _relay_canonical_payload_from_artifact(artifact)
        if payload is None:
            continue
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if role:
            round_id = str(payload.get("round_id") or "")
            success_roles = success_roles_by_round.setdefault(round_id, set())
            normalized_status = _relay_lifecycle_status_for_payload(payload, success_roles)
            payload = {**payload, "status": normalized_status}
            payloads[role] = payload
            if _relay_payload_status_is_success(normalized_status):
                success_roles.add(role)
    return payloads


def _relay_role_canonical_payload_sequence(
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    success_roles_by_round: dict[str, set[str]] = {}
    for index, artifact in enumerate(artifacts):
        payload = _relay_canonical_payload_from_artifact(artifact)
        if payload is None:
            continue
        role = str(payload.get("role") or payload.get("relay_role") or "")
        if not role:
            continue
        round_id = str(payload.get("round_id") or "")
        success_roles = success_roles_by_round.setdefault(round_id, set())
        normalized_status = _relay_lifecycle_status_for_payload(payload, success_roles)
        payloads.append(
            {
                **payload,
                "status": normalized_status,
                "_relay_artifact_key": str(
                    artifact.get("id") or artifact.get("created_at") or index
                ),
            }
        )
        if _relay_payload_status_is_success(normalized_status):
            success_roles.add(role)
    return payloads


def _relay_canonical_payload_from_artifact(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    if str((artifact or {}).get("artifact_type") or "") in {"role_error", "role_artifact_invalid"}:
        return None
    payload = {
        key: value
        for key, value in dict(artifact or {}).items()
        if key not in {"id", "created_at", "output"} and value is not None
    }
    if "role" not in payload and payload.get("relay_role"):
        payload["role"] = payload["relay_role"]
    parsed = _relay_parse_role_envelope_payload(payload)
    return parsed


def _relay_parse_role_envelope_payload(
    text_or_payload: str | dict[str, Any],
) -> dict[str, Any] | None:
    result = parse_role_envelope(text_or_payload)
    if result.ok and result.payload:
        return dict(result.payload)
    return None


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
  <link rel="stylesheet" href="/static/native_app_bundle.css">
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
    .composer-action-menu { position: fixed; left: 26px; right: 72px; bottom: calc(110px + env(safe-area-inset-bottom)); max-height: min(58vh, 690px); overflow-y: auto; border: 1px solid #343434; border-radius: 26px; background: #202022; box-shadow: 0 20px 54px rgba(0,0,0,.55); padding: 26px 38px 28px; z-index: 20; opacity: 1; transform: translateY(0) scale(1); transform-origin: bottom left; transition: opacity 180ms var(--ease-default), transform 180ms var(--ease-default); scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.35) transparent; }
    .composer-action-menu::-webkit-scrollbar { width: 3px; }
    .composer-action-menu::-webkit-scrollbar-thumb { border-radius: 999px; background: rgba(255,255,255,.35); }
    .composer-action-menu.closed { opacity: 0; transform: translateY(8px) scale(0.96); pointer-events: none; }
    .composer-menu-item { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 22px; align-items: center; width: 100%; min-height: 74px; padding: 8px 0; border: 0; border-radius: 14px; background: transparent; color: var(--btn-primary-bg); text-align: left; }
    button.composer-menu-item:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .composer-menu-action { min-height: 86px; }
    .composer-menu-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 0; background: transparent; color: var(--btn-primary-bg); font-size: 0; font-weight: var(--weight-black); }
    .composer-menu-icon svg { width: 36px; height: 36px; stroke-width: 2.05; }
    .composer-menu-title { display: block; min-width: 0; color: var(--btn-primary-bg); font-size: 22px; line-height: 1.14; font-weight: var(--weight-black); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-desc { display: block; margin-top: 7px; min-width: 0; color: var(--text-dim); font-size: 17px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-check { color: var(--btn-primary-bg); font-size: 18px; font-weight: var(--weight-black); }
    .composer-menu-section { margin: 8px 0 12px; padding-top: 20px; border-top: 1px solid #3a3a3d; color: var(--text-dim); font-size: 16px; line-height: 1.2; font-weight: var(--weight-medium); }
    .plugin-list { display: grid; gap: 2px; }
    .plugin-dot { width: 42px; height: 42px; border-radius: 10px; background: transparent; color: var(--btn-primary-bg); display: grid; place-items: center; font-size: 15px; font-weight: var(--weight-black); overflow: hidden; }
    .plugin-dot img { width: 100%; height: 100%; object-fit: contain; display: block; }
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
      <button class="composer-menu-item composer-menu-action" id="menuUploadPhoto" type="button" role="menuitem">
        <span class="composer-menu-icon">▧</span>
        <span>
          <span class="composer-menu-title">上传照片</span>
          <span class="composer-menu-desc">添加图片到下一条消息</span>
        </span>
        <span></span>
      </button>
      <button class="composer-menu-item composer-menu-action" id="menuPlanMode" type="button" role="menuitem"__PLAN_MODE_ACTION_HIDDEN__ aria-pressed="false">
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
    let sessionsReconnectTimer = null;
    let projectRoot = "";
    let projectCatalog = [];
    let renderedSessionsDataSignature = "";
    const SESSION_PREVIEW_LIMIT = 10;
    const LIVE_PREFETCH_LIMIT = 4;
    const SESSION_REFRESH_PENDING_DELAY_MS = 10000;
    const SESSION_POLL_INTERVAL_MS = 30000;
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
        }, SESSION_REFRESH_PENDING_DELAY_MS);
      }
      if (selected && !sessions.some(session => session.native_thread_id === selected.native_thread_id)) selected = null;
      if (render) {
        renderSessionsIfDataChanged();
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

    function closeSessionsStream() {
      if (sessionsReconnectTimer) {
        clearTimeout(sessionsReconnectTimer);
        sessionsReconnectTimer = null;
      }
      if (sessionsEventSource) {
        sessionsEventSource.close();
        sessionsEventSource = null;
      }
    }

    function startSessionsStream() {
      if (sessionsEventSource) return;
      try {
        const source = new EventSource(sessionsStreamPath());
        sessionsEventSource = source;
        source.addEventListener("native_sessions", message => {
          const data = JSON.parse(message.data || "{}");
          applySessionsPayload(data, true);
        });
        source.onerror = () => {
          if (sessionsEventSource !== source) return;
          source.close();
          sessionsEventSource = null;
          if (!sessionsReconnectTimer) {
            sessionsReconnectTimer = window.setTimeout(() => {
              sessionsReconnectTimer = null;
              startSessionsStream();
            }, 3000);
          }
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

    function sessionsDataSignature() {
      return JSON.stringify({
        sessions: stableSignatureSessions().map(session => ({
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

    function stableSignatureSessions() {
      return [...sessions].sort((left, right) => {
        return sessionDomId(left).localeCompare(sessionDomId(right));
      });
    }

    function renderSessionsIfDataChanged() {
      const signature = sessionsDataSignature();
      if (signature === renderedSessionsDataSignature) return false;
      renderedSessionsDataSignature = signature;
      renderSessions({silent: true});
      return true;
    }

    async function loadHomeData() {
      await loadProjects();
      await loadSessions(false);
      renderNativePage();
      if (initialComposeCwd && !initialComposeCwdApplied) {
        initialComposeCwdApplied = true;
        selectComposeProject(initialComposeCwd);
        openCompose(initialComposeCwd);
      }
    }

    async function refreshSessionsSilently() {
      await loadSessions(true);
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
      modelSettingsButton.textContent = [modelText, effortText].filter(Boolean).join(" ");
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
      renderedSessionsDataSignature = sessionsDataSignature();
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
      const current = projectCatalog.find(project => String(project.alias || "") === "wlcodex")
        || projectCatalog.find(project => lastPath(project.cwd || "") === "wlcodex")
        || projectCatalog[0];
      if (current) return {cwd: String(current.cwd || ""), name: current.name || lastPath(current.cwd || "")};
      return {cwd: projectRoot, name: lastPath(projectRoot || "")};
    }

    function selectComposeProject(cwd) {
      selectedProjectCwd = String(cwd || "");
      noProjectSelected = !selectedProjectCwd;
      closeProjectPicker();
      renderComposeProject();
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
      refreshSessionsSilently();
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
    window.addEventListener("pageshow", event => {
      if (event.persisted) refreshSessionsSilently();
    });
    chatRow.onclick = () => openHistory("", "聊天");
    composeProjectButton.onclick = openProjectPicker;
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
      return projectCatalog.some(project => String(project.cwd || "") === value);
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
    window.addEventListener("pagehide", closeSessionsStream);
    window.addEventListener("beforeunload", closeSessionsStream);
    window.addEventListener("pageshow", () => startSessionsStream());
    setInterval(refreshSessionsSilently, SESSION_POLL_INTERVAL_MS);
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
                _codex_plugin_menu_items() if supports_plugin_menu else [],
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
  <link rel="stylesheet" href="/static/native_app_bundle.css">
__MARVIS_CSS_LINK__  <style>
    :root { --native-remote-blue: #58a6ff; --native-remote-red: #ff3b4f; --native-ui-font-size: 15px; --native-code-font-size: 12px; --native-top-control-y: calc(14px + env(safe-area-inset-top)); --native-top-control-size: 46px; }
    html, body, .native-mobile-shell, .codex-run-shell, .codex-transcript, .transcript-body, .codex-input-dock, input, textarea { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
    body { background: #000; }
    body { scrollbar-width: none; }
    body::-webkit-scrollbar { display: none; }
    .aurora-bg { background: #000 !important; }
    .noise-overlay::before { display: none !important; }
    .native-mobile-shell, .codex-run-shell { min-height: 100vh; background: #000; }
    .viewport-debug { position: fixed; left: 12px; right: 12px; bottom: calc(var(--codex-dock-height, 150px) + 14px + env(safe-area-inset-bottom)); z-index: 40; max-height: 34vh; margin: 0; padding: 10px 12px; overflow: auto; border: 1px solid rgba(88,166,255,.55); border-radius: 12px; background: rgba(0,0,0,.88); color: #dbeafe; font: 11px/1.45 var(--font-mono); white-space: pre-wrap; box-shadow: 0 14px 36px rgba(0,0,0,.5); }
    .viewport-debug[hidden] { display: none; }
    header { position: sticky; top: 0; z-index: 3; display: grid; grid-template-columns: var(--native-top-control-size) 1fr var(--native-top-control-size); align-items: center; gap: 8px; min-height: calc(var(--native-top-control-y) + var(--native-top-control-size) + 8px); padding: 0 18px; background: #000; border-bottom: 0; }
    .circle { width: var(--native-top-control-size); height: var(--native-top-control-size); min-height: var(--native-top-control-size); box-sizing: border-box; display: grid; place-items: center; padding: 0; border-radius: 50%; border-color: #343434; background: #202022; color: #f5f5f5; font-size: 0; line-height: 1; }
    .circle svg { width: 28px; height: 28px; stroke-width: 2.35; }
    #back { position: fixed; top: var(--native-top-control-y); left: clamp(17px, 4.4vw, 26px); z-index: 6; }
    .session-float { position: fixed; top: var(--native-top-control-y); left: clamp(70px, 18.5vw, 74px); right: clamp(112px, 29vw, 118px); z-index: 5; display: grid; grid-template-columns: minmax(0, 1fr); align-items: center; height: var(--native-top-control-size); min-height: var(--native-top-control-size); box-sizing: border-box; padding: 0 15px; border: 1px solid #343434; border-radius: 23px; background: #242426; color: #f4f4f5; box-shadow: 0 12px 30px rgba(0,0,0,.38); }
    .session-float-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; line-height: 1.15; font-weight: var(--weight-black); }
    .session-float-meta { display: flex; gap: 7px; align-items: center; min-width: 0; margin-top: 4px; color: #d0d0d4; font-size: 11px; line-height: 1; overflow: hidden; white-space: nowrap; }
    #sessionFloatMeta { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
    .session-float-state { flex: 0 0 auto; color: #d0d0d4; }
    .session-float-run-id { flex: 0 0 auto; margin-left: auto; color: #8f929a; font-variant-numeric: tabular-nums; }
    .session-float.busy .session-float-state { color: var(--color-warning); }
    .session-float.failed .session-float-state { color: var(--color-error); }
    .session-float.done .session-float-state { color: var(--color-success); }
    .session-float-meta .laptop { width: 13px; height: 9px; border: 1.6px solid currentColor; border-radius: 2px; position: relative; display: inline-block; }
    .session-float-meta .laptop:after { content: ""; position: absolute; left: -3px; right: -3px; bottom: -5px; height: 2px; background: currentColor; border-radius: 2px; }
    .header-run-indicator { position: fixed; top: var(--native-top-control-y); right: clamp(17px, 4.4vw, 26px); z-index: 6; display: grid; grid-template-columns: 27px 27px; gap: 10px; align-items: center; justify-content: center; width: 94px; height: var(--native-top-control-size); min-height: var(--native-top-control-size); box-sizing: border-box; border: 1px solid #343434; border-radius: 23px; background: #242426; color: #f4f4f5; box-shadow: 0 12px 30px rgba(0,0,0,.38); }
    .header-run-button { width: 27px; min-height: 27px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; color: inherit; -webkit-tap-highlight-color: transparent; }
    button.header-run-button:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }
    .header-run-status { display: grid; place-items: center; width: 27px; min-height: 27px; }
    .header-run-spinner { width: 23px; height: 23px; border: 3px solid #5a5b60; border-right-color: transparent; border-radius: 50%; opacity: .72; }
    .header-run-menu { display: grid; place-items: center; width: 24px; height: 27px; line-height: 1; color: #f4f4f5; font-size: 0; font-weight: var(--weight-extrabold); }
    .header-run-menu svg { width: 24px; height: 24px; }
    .header-run-dot { display: none; width: 8px; height: 8px; border-radius: 50%; background: var(--native-remote-red); box-shadow: 0 0 10px rgba(255,59,79,.35); }
    .header-run-indicator.running .header-run-spinner { border-color: transparent; border-top-color: var(--native-remote-blue); border-right-color: var(--native-remote-blue); opacity: 1; animation: nativeRemoteSpin .85s linear infinite; }
    .header-run-indicator.finished .header-run-spinner { display: none; }
    .header-run-indicator.finished .header-run-dot { display: block; }
    .native-header-popover { position: fixed; top: calc(86px + env(safe-area-inset-top)); right: 20px; z-index: 10; width: min(326px, calc(100vw - 40px)); border: 1px solid #343434; border-radius: 24px; background: #242426; color: #f4f4f5; box-shadow: 0 18px 44px rgba(0,0,0,.55); overflow: hidden; }
    .native-header-popover[hidden] { display: none; }
    .context-info-sheet { position: fixed; left: 0; right: 0; bottom: 0; z-index: 30; display: grid; max-height: min(54vh, 420px); padding: 10px 26px calc(34px + env(safe-area-inset-bottom)); border-radius: 30px 30px 0 0; background: #000; color: #f4f4f5; border-top: 1px solid #111114; box-shadow: 0 -22px 54px rgba(0,0,0,.78); overflow: auto; }
    .context-info-sheet[hidden] { display: none; }
    .context-sheet-handle { justify-self: center; width: 58px; height: 5px; margin: 0 0 24px; border-radius: 999px; background: #252529; }
    .context-sheet-header { position: relative; display: grid; place-items: center; min-height: 34px; margin-bottom: 22px; }
    .context-info-title { margin: 0; color: #f4f4f5; font-size: 22px; line-height: 1.15; font-weight: var(--weight-black); text-align: center; letter-spacing: 0; }
    .context-info-close { position: absolute; top: -6px; right: 0; width: 38px; min-height: 38px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; color: #f4f4f5; font-size: 32px; line-height: 1; font-weight: var(--weight-medium); -webkit-tap-highlight-color: transparent; }
    button.context-info-close:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }
    .context-info-grid { display: grid; gap: 13px; }
    .context-info-row { display: grid; grid-template-columns: 158px minmax(0, 1fr); gap: 10px; align-items: start; min-height: 22px; }
    .context-info-label { color: #d9d9dd; font-size: 13px; line-height: 1.45; font-weight: var(--weight-medium); white-space: nowrap; }
    .context-info-value { min-width: 0; color: #f4f4f5; font: 700 13px/1.45 var(--font-mono); overflow-wrap: anywhere; word-break: normal; white-space: normal; }
    .context-info-value-wrap { min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) 36px; gap: 8px; align-items: start; }
    .context-info-copy { width: 36px; min-height: 36px; display: grid; place-items: center; padding: 0; margin-top: -5px; border: 0; border-radius: 50%; background: transparent; color: #f4f4f5; -webkit-tap-highlight-color: transparent; }
    .context-info-copy svg { width: 29px; height: 29px; }
    button.context-info-copy:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }
    .session-action-menu { padding: 18px 0; }
    .session-action-title { min-width: 0; margin: 0; padding: 0 38px 12px; color: #d7d7dc; font-size: 18px; line-height: 1.2; font-weight: var(--weight-extrabold); text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .session-action-item { width: 100%; min-height: 64px; display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 18px; align-items: center; padding: 0 42px; border: 0; border-radius: 0; background: transparent; color: #f4f4f5; text-align: left; font-size: 20px; line-height: 1.2; font-weight: var(--weight-medium); -webkit-tap-highlight-color: transparent; }
    button.session-action-item:not(.secondary):not(.warn):not(:disabled):hover { background: rgba(255,255,255,.06); filter: none; }
    .session-action-item.danger { color: #ff4b55; }
    .session-action-icon { display: grid; place-items: center; width: 36px; height: 36px; }
    .session-action-icon svg { width: 30px; height: 30px; }
    .session-display-settings { display: grid; gap: 12px; margin: 12px 0 6px; padding: 14px 30px 16px; border-top: 1px solid rgba(255,255,255,.08); border-bottom: 1px solid rgba(255,255,255,.08); }
    .session-display-settings-title { color: #d7d7dc; font-size: 14px; line-height: 1.2; font-weight: var(--weight-extrabold); }
    .font-size-setting { display: grid; grid-template-columns: minmax(0, 1fr) 76px; gap: 12px; align-items: center; color: #f4f4f5; font-size: 16px; line-height: 1.25; font-weight: var(--weight-medium); }
    .font-size-setting input { width: 76px; min-height: 38px; padding: 0 9px; border: 1px solid #3a3a3d; border-radius: 10px; background: #111114; color: #f4f4f5; font-size: 16px; text-align: center; }
    @keyframes nativeRemoteSpin { to { transform: rotate(360deg); } }
    .screen-title { min-width: 0; text-align: center; visibility: hidden; }
    header > button:last-child { visibility: hidden; pointer-events: none; }
    h1 { margin: 0; font-size: 22px; font-weight: var(--weight-extrabold); letter-spacing: 0; }
    .subtitle { margin-top: 5px; color: var(--text-muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 7px; background: var(--color-warning); vertical-align: 1px; transition: background 300ms ease; }
    .connected .status-dot { background: var(--color-success); animation: breathe 2s ease-in-out infinite; }
    .reconnecting .status-dot { background: var(--color-error); animation: breathe 1s ease-in-out infinite; }
    main { padding: 12px 20px calc(var(--codex-dock-height, 150px) + 32px + env(safe-area-inset-bottom)); }
    .event-cursor { color: #777b86; font-size: 12px; }
    .codex-transcript { display: grid; gap: 18px; padding-top: 8px; }
    .transcript-item { display: grid; gap: 7px; min-width: 0; padding: 0; }
    .transcript-meta { color: #9aa0aa; font-size: 12px; }
    .transcript-body { min-width: 0; max-width: 100%; white-space: normal; overflow-wrap: anywhere; color: var(--btn-primary-bg); font-size: var(--native-ui-font-size); line-height: 1.55; letter-spacing: 0; }
    .transcript-body p { margin: 0 0 13px; overflow-wrap: anywhere; word-break: break-word; }
    .transcript-body p:last-child { margin-bottom: 0; }
    .transcript-body h3 { margin: 18px 0 8px; color: var(--text-heading); font-size: 16px; line-height: 1.35; }
    .transcript-body h3:first-child { margin-top: 0; }
    .transcript-body ul, .transcript-body ol { margin: 0 0 13px 1.3em; padding: 0; display: grid; gap: 6px; white-space: normal; }
    .transcript-body li { padding-left: 2px; white-space: normal; }
    .transcript-body strong { color: var(--text-heading); font-weight: var(--weight-extrabold); }
    .transcript-body a { color: var(--color-link); text-decoration: none; border-bottom: 1px solid rgba(147, 197, 253, .45); transition: border-color 150ms ease; }
    .transcript-body a:hover { border-bottom-color: rgba(147, 197, 253, .7); }
    .transcript-body code { white-space: normal; overflow-wrap: anywhere; word-break: break-word; padding: 1px 5px; border-radius: 5px; border: 1px solid rgba(255,255,255,0.06); background: var(--bg-code); color: var(--text-code); font: var(--native-code-font-size)/1.45 var(--font-mono); }
    .transcript-body pre { margin: 0 0 13px; overflow: auto; padding: 14px 16px; border: 1px solid var(--border-code); border-radius: 8px; background: linear-gradient(145deg, #0c0e14, #101420); box-shadow: inset 0 1px 0 rgba(255,255,255,0.04); white-space: pre; scrollbar-width: thin; scrollbar-color: #383c46 transparent; }
    .transcript-body pre code { white-space: pre; overflow-wrap: normal; word-break: normal; padding: 0; border-radius: 0; background: transparent; font-size: var(--native-code-font-size); line-height: 1.5; }
    .transcript-item.user { justify-self: end; justify-items: end; max-width: min(82%, 520px); }
    .transcript-item.user .transcript-meta { display: none; }
    .transcript-item.user .transcript-body { white-space: pre-wrap; padding: 10px 13px; border: 1px solid #333842; border-radius: 20px 20px 4px 20px; background: var(--bg-user-bubble); line-height: 1.5; }
    .transcript-item.local-pending .transcript-body { opacity: .86; }
    .transcript-item.assistant { justify-self: start; max-width: 100%; margin: 4px 0 12px; }
    .transcript-item.assistant .transcript-body { color: #b8bcc7; }
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
    .status-event { display: grid; grid-template-columns: 18px 1fr; gap: 10px; align-items: start; color: var(--text-placeholder); font-size: var(--native-ui-font-size); line-height: 1.5; }
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
    .history-fold { width: 100%; min-height: 38px; margin: 2px 0 10px; border: 0; border-bottom: 1px solid var(--border-subtle); border-radius: 0; background: transparent; color: var(--text-dim); text-align: left; font-size: 15px; appearance: none; -webkit-appearance: none; }
    .history-fold:disabled { background: transparent; color: var(--text-dim); opacity: 1; -webkit-text-fill-color: var(--text-dim); cursor: progress; }
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
    .turn-fold-preview-assistant { justify-self: start; max-width: 100%; color: var(--text-secondary); }
    .turn-fold-body { display: grid; grid-template-rows: 0fr; overflow: hidden; opacity: 0; transition: grid-template-rows 200ms ease, opacity 200ms ease 50ms; }
    .turn-fold-body-inner { min-height: 0; overflow: hidden; }
    .turn-fold:not(.collapsed) .turn-fold-body { grid-template-rows: 1fr; opacity: 1; padding: 12px 0 18px; }
    .codex-tool-call, .approval-card { position: relative; border: 1px solid var(--border-default); background: #0f1014; border-radius: 10px; overflow: hidden; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }
    .codex-tool-call.failed { border-color: #7f1d1d; }
    .tool-head, .approval-head { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; padding: 11px 12px; border-bottom: 1px solid var(--border-header); }
    .tool-title, .approval-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--btn-primary-bg); font-size: 14px; font-weight: var(--weight-bold); }
    .tool-state { color: var(--text-muted); font-size: 12px; }
    .tool-output { margin: 0; max-height: 260px; overflow: auto; padding: 11px 12px; color: var(--text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; font: var(--native-code-font-size) ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.45; }
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
    .codex-input-dock { position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; display: grid; gap: 6px; padding: 12px 18px 20px; background: linear-gradient(to top, rgba(0,0,0,.98) 56%, rgba(0,0,0,.76) 80%, rgba(0,0,0,0)); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 0; }
    .composer-tools { display: flex; gap: 10px; align-items: center; min-width: 0; padding: 0; }
    .composer-settings { position: relative; flex: 1; display: grid; grid-template-columns: minmax(128px, 1.2fr) minmax(96px, 1fr) minmax(96px, 1fr); gap: 8px; min-width: 0; max-width: 100%; }
    .setting-pill { width: 100%; min-width: 0; min-height: 36px; border-radius: 18px; padding: 0 8px; overflow: hidden; background: var(--bg-pill); color: var(--btn-primary-bg); border: 1px solid transparent; font-size: 13px; font-weight: var(--weight-extrabold); text-overflow: ellipsis; white-space: nowrap; transition: background var(--duration-fast) ease, border-color var(--duration-fast) ease; }
    .setting-pill.modified { border-color: rgba(147, 197, 253, 0.35); background: var(--bg-pill-modified); }
    .setting-pill:not(:disabled):hover { background: var(--bg-pill-hover); }
    .setting-pill.permissions { flex: 0 0 auto; }
    .setting-pill.handoff { flex: 0 0 auto; background: #1f2937; border: 1px solid var(--border-input); }
    .mode-chip-row { display: flex; gap: 8px; align-items: center; min-height: 0; }
    .mode-chip { display: inline-flex; align-items: center; gap: 8px; min-height: 38px; max-width: 100%; padding: 0 13px; border: 0; border-radius: 19px; background: var(--bg-pill); color: var(--btn-primary-bg); font-size: 14px; font-weight: var(--weight-extrabold); }
    .mode-chip[hidden] { display: none; }
    .mode-chip-cancel { display: inline-grid; place-items: center; width: 18px; min-height: 18px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--btn-primary-bg); font-size: 16px; line-height: 1; }
    button.mode-chip-cancel:not(.secondary):not(.warn):not(:disabled):hover { background: rgba(255,255,255,.1); filter: none; }
    .model-popover { position: absolute; left: 0; bottom: 50px; width: min(330px, calc(100vw - 52px)); border: 1px solid var(--border-popover); border-radius: 22px; background: var(--bg-popover); box-shadow: 0 20px 54px rgba(0,0,0,.55); overflow: hidden; z-index: 6; opacity: 1; transform: translateY(0) scale(1); transform-origin: bottom left; transition: opacity 180ms var(--ease-default), transform 180ms var(--ease-default); }
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
    .attach-button { width: 44px; min-height: 44px; display: grid; place-items: center; padding: 0; border-radius: 50%; background: #242426; color: var(--btn-primary-bg); border: 1px solid #343434; font-size: 0; line-height: 1; }
    .attach-button svg { width: 29px; height: 29px; stroke-width: 2.15; }
    .composer-action-menu { position: fixed; left: 26px; right: 72px; bottom: calc(110px + env(safe-area-inset-bottom)); max-height: min(58vh, 690px); overflow-y: auto; border: 1px solid #343434; border-radius: 26px; background: #202022; box-shadow: 0 20px 54px rgba(0,0,0,.55); padding: 26px 38px 28px; z-index: 20; opacity: 1; transform: translateY(0) scale(1); transform-origin: bottom left; transition: opacity 180ms var(--ease-default), transform 180ms var(--ease-default); scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.35) transparent; }
    .composer-action-menu::-webkit-scrollbar { width: 3px; }
    .composer-action-menu::-webkit-scrollbar-thumb { border-radius: 999px; background: rgba(255,255,255,.35); }
    .composer-action-menu.closed { opacity: 0; transform: translateY(8px) scale(0.96); pointer-events: none; }
    .composer-menu-item { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 22px; align-items: center; width: 100%; min-height: 74px; padding: 8px 0; border: 0; border-radius: 14px; background: transparent; color: var(--btn-primary-bg); text-align: left; }
    button.composer-menu-item:not(.secondary):not(.warn):not(:disabled):hover { background: var(--bg-option-hover); filter: none; }
    .composer-menu-item:disabled { opacity: .82; }
    .composer-menu-action { min-height: 86px; }
    .composer-menu-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 0; background: transparent; color: var(--btn-primary-bg); font-size: 0; font-weight: var(--weight-black); }
    .composer-menu-icon svg { width: 36px; height: 36px; stroke-width: 2.05; }
    .composer-menu-title { display: block; min-width: 0; color: var(--btn-primary-bg); font-size: 22px; line-height: 1.14; font-weight: var(--weight-black); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-desc { display: block; margin-top: 7px; min-width: 0; color: var(--text-dim); font-size: 17px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .composer-menu-check { color: var(--btn-primary-bg); font-size: 18px; font-weight: var(--weight-black); }
    .composer-menu-section { margin: 8px 0 12px; padding-top: 20px; border-top: 1px solid #3a3a3d; color: var(--text-dim); font-size: 16px; line-height: 1.2; font-weight: var(--weight-medium); }
    .plugin-list { display: grid; gap: 2px; }
    .plugin-dot { width: 42px; height: 42px; border-radius: 10px; background: transparent; color: var(--btn-primary-bg); display: grid; place-items: center; font-size: 15px; font-weight: var(--weight-black); overflow: hidden; }
    .plugin-dot img { width: 100%; height: 100%; object-fit: contain; display: block; }
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
    .interruption-choice { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 2px 0; }
    .interruption-choice[hidden] { display: none; }
    .choice-action { min-height: 42px; border-radius: 12px; background: var(--bg-interact); color: var(--btn-primary-bg); border: 1px solid var(--border-input); }
    .choice-action.primary { background: var(--btn-primary-bg); color: var(--btn-primary-color); border: 0; }
    .dock-row { position: relative; display: grid; grid-template-columns: 44px minmax(0, 1fr); gap: 10px; min-width: 0; align-items: end; }
    .dock-actions { display: flex; gap: 10px; min-width: 0; }
    .dock-actions[hidden] { display: none; }
    #prompt { flex: 1; min-width: 0; min-height: 44px; max-height: 132px; border-radius: 22px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--btn-primary-bg); padding: 9px 48px 9px 18px; font-size: 18px; line-height: 24px; resize: none; overflow-y: auto; }
    .primary-action { position: absolute; right: 4px; bottom: 4px; width: 36px; min-height: 36px; border-radius: 50%; padding: 0; display: grid; place-items: center; background: #f4f4f5; color: #050505; font-size: 0; line-height: 1; }
    .primary-action svg { width: 25px; height: 25px; stroke-width: 2.5; }
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
      <span class="session-float-meta">
        <span class="laptop"></span>
        <span id="sessionFloatMeta">wlcodex</span>
        <span class="session-float-state" id="sessionFloatState">连接会话</span>
        <span class="session-float-run-id">#__RUN_ID__</span>
      </span>
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
      <div class="session-display-settings" id="sessionDisplaySettings">
        <div class="session-display-settings-title">显示字号</div>
        <label class="font-size-setting" for="uiFontSizeInput">
          <span>UI字号</span>
          <input id="uiFontSizeInput" type="number" min="12" max="20" step="1" inputmode="numeric">
        </label>
        <label class="font-size-setting" for="codeFontSizeInput">
          <span>代码字号</span>
          <input id="codeFontSizeInput" type="number" min="12" max="20" step="1" inputmode="numeric">
        </label>
      </div>
      <button class="session-action-item" id="copySessionIdButton" type="button" role="menuitem">
        <span class="session-action-icon"></span><span>复制会话 ID</span>
      </button>
    </section>
    <main>
      <span class="event-cursor" id="cursor" hidden></span>
      <button class="history-fold" id="historyFold" hidden>更早的消息</button>
      <section class="codex-transcript" id="events"><div class="empty" id="empty">输入消息开始新会话</div></section>
      <div class="composer-activity" id="composerActivity" aria-hidden="true">
        <span class="composer-activity-dot"></span>
        <span class="composer-activity-dot"></span>
        <span class="composer-activity-dot"></span>
      </div>
    </main>
    <section class="codex-input-dock">
      <div class="attachment-strip" id="attachmentStrip" hidden></div>
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
          <button class="composer-menu-item composer-menu-action" id="menuUploadPhoto" type="button" role="menuitem">
            <span class="composer-menu-icon">▧</span>
            <span>
              <span class="composer-menu-title">上传照片</span>
              <span class="composer-menu-desc">添加图片到下一条消息</span>
            </span>
            <span></span>
          </button>
          <button class="composer-menu-item composer-menu-action" id="menuPlanMode" type="button" role="menuitem"__PLAN_MODE_ACTION_HIDDEN__ aria-pressed="false">
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
        <input id="imageInput" type="file" accept="image/*" multiple hidden>
        <span class="send-status" id="sendStatus"></span>
      </div>
      <div class="selected-plugin-strip" id="selectedPluginStrip" hidden></div>
      <div class="mode-chip-row">
        <div class="mode-chip plan-mode-chip" id="planModeChip" hidden>
          <span>☷ 计划</span>
          <button class="mode-chip-cancel" id="planModeChipCancel" type="button" aria-label="取消计划模式">×</button>
        </div>
      </div>
      <div class="interruption-choice" id="interruptionChoice" hidden>
        <button class="choice-action primary" id="steerChoice" type="button">引导</button>
        <button class="choice-action" id="queueChoice" type="button">排队</button>
      </div>
      <div class="dock-row">
        <button class="attach-button" id="attachmentButton" type="button" aria-label="上传照片">＋</button>
        <textarea id="prompt" rows="1" placeholder="继续 __PROVIDER_LABEL_TEXT__ 会话"></textarea>
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
    const copySessionIdButton = document.getElementById("copySessionIdButton");
    const uiFontSizeInput = document.getElementById("uiFontSizeInput");
    const codeFontSizeInput = document.getElementById("codeFontSizeInput");
    const sessionFloat = document.getElementById("sessionFloat");
    const sessionFloatTitle = document.getElementById("sessionFloatTitle");
    const sessionFloatMeta = document.getElementById("sessionFloatMeta");
    const sessionFloatState = document.getElementById("sessionFloatState");
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
    let nativeItemCursor = 0;
    let nativeUpdateCursor = 0;
    let previousEventCount = 0;
    let source = null;
    let streamReconnectTimer = null;
    let streamReconnectDelay = 500;
    let pollInFlight = false;
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
    function resizePromptInput() {
      promptInput.style.height = "auto";
      promptInput.style.height = `${Math.min(Math.max(promptInput.scrollHeight, 44), 132)}px`;
    }
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
    const NATIVE_TIMELINE_RECENT_LIMIT = 80;
    const OLDER_EVENT_LIMIT = 80;
    const OLDER_VISIBLE_PAGE_ATTEMPTS = 5;
    const MODEL_SETTINGS_STORAGE_KEY = "wlcodexNativeModelSettings";
    const MODEL_SETTINGS_STORAGE_VERSION = 2;
    const PERMISSION_SETTINGS_STORAGE_KEY = "wlcodexNativePermissionSettings";
    const COLLABORATION_MODE_STORAGE_KEY = "wlcodexNativeCollaborationMode";
    const DISPLAY_SETTINGS_STORAGE_KEY = "wlcodexNativeDisplaySettings";
    const DEFAULT_DISPLAY_SETTINGS = Object.freeze({uiFontSize: 15, codeFontSize: 12});
    const DISPLAY_FONT_SIZE_LIMITS = Object.freeze({
      uiFontSize: Object.freeze({min: 12, max: 20}),
      codeFontSize: Object.freeze({min: 12, max: 20})
    });
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
    let savedDisplaySettings = loadSavedDisplaySettings();
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
    contextThreadCopyButton.innerHTML = ICONS.copy;
    copySessionIdButton.querySelector(".session-action-icon").innerHTML = ICONS.copy;
    historyFold.onclick = loadOlderEvents;
    applyDisplaySettings();
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
    function isNoisyNativeSyncError(message) {
      const text = String(message || "").trim().toLowerCase();
      return text === "not found" || text === "native session not found" || text === "keyerror";
    }
    function clampDisplayFontSize(value, fallback, min, max) {
      const parsed = Number.parseInt(String(value || ""), 10);
      if (!Number.isFinite(parsed)) return fallback;
      return Math.max(min, Math.min(max, parsed));
    }
    function displayFontSizeLimits(key) {
      return DISPLAY_FONT_SIZE_LIMITS[key] || DISPLAY_FONT_SIZE_LIMITS.uiFontSize;
    }
    function normalizeDisplaySettings(settings) {
      const source = settings || {};
      const uiLimits = displayFontSizeLimits("uiFontSize");
      const codeLimits = displayFontSizeLimits("codeFontSize");
      return {
        uiFontSize: clampDisplayFontSize(source.uiFontSize, DEFAULT_DISPLAY_SETTINGS.uiFontSize, uiLimits.min, uiLimits.max),
        codeFontSize: clampDisplayFontSize(source.codeFontSize, DEFAULT_DISPLAY_SETTINGS.codeFontSize, codeLimits.min, codeLimits.max)
      };
    }
    function loadSavedDisplaySettings() {
      try {
        return normalizeDisplaySettings(JSON.parse(localStorage.getItem(DISPLAY_SETTINGS_STORAGE_KEY) || "{}"));
      } catch (_error) {
        return normalizeDisplaySettings({});
      }
    }
    function persistDisplaySettings() {
      try {
        localStorage.setItem(DISPLAY_SETTINGS_STORAGE_KEY, JSON.stringify(savedDisplaySettings));
      } catch (_error) {}
    }
    function applyDisplaySettings() {
      document.documentElement.style.setProperty("--native-ui-font-size", `${savedDisplaySettings.uiFontSize}px`);
      document.documentElement.style.setProperty("--native-code-font-size", `${savedDisplaySettings.codeFontSize}px`);
      setDisplayFontInputValue(uiFontSizeInput, savedDisplaySettings.uiFontSize);
      setDisplayFontInputValue(codeFontSizeInput, savedDisplaySettings.codeFontSize);
    }
    function setDisplayFontInputValue(input, value) {
      if (input) input.value = String(value);
    }
    function updateDisplayFontSizeDraft(input, key) {
      const raw = String((input && input.value) || "");
      if (raw === "") return;
      const parsed = Number.parseInt(raw, 10);
      if (!Number.isFinite(parsed)) return;
      const limits = displayFontSizeLimits(key);
      const clamped = Math.max(limits.min, Math.min(limits.max, parsed));
      const cssProperty = key === "codeFontSize" ? "--native-code-font-size" : "--native-ui-font-size";
      document.documentElement.style.setProperty(cssProperty, `${clamped}px`);
    }
    function commitDisplayFontSizeInput(input, key) {
      const raw = String((input && input.value) || "");
      if (raw === "") {
        applyDisplaySettings();
        return;
      }
      updateDisplayFontSize(key, raw);
    }
    function commitDisplayFontSizeInputOnEnter(event, input, key) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      commitDisplayFontSizeInput(input, key);
      if (input) input.blur();
    }
    function updateDisplayFontSize(key, value) {
      savedDisplaySettings = normalizeDisplaySettings({...savedDisplaySettings, [key]: value});
      persistDisplaySettings();
      applyDisplaySettings();
    }
    function isFetchNetworkError(error) {
      const text = String((error && error.message) || error || "");
      return Boolean(error && error.name === "TypeError") || /failed to fetch|network/i.test(text);
    }
    function isNativeControlRecoverableError(error) {
      const text = String((error && error.message) || error || "");
      return isFetchNetworkError(error) || /JsonRpcTimeout|timeout|timed out/i.test(text);
    }
    function delay(ms) {
      return new Promise(resolve => window.setTimeout(resolve, ms));
    }
    function snapshotNativeTurnControl() {
      return {
        nativeTurnId,
        activeTurnId,
        nativeTurnRunning,
        nativeItemCursor,
        nativeUpdateCursor
      };
    }
    function nativeTurnAdvancedSince(snapshot) {
      const before = snapshot || {};
      return Boolean(
        (nativeTurnId && nativeTurnId !== before.nativeTurnId) ||
        (activeTurnId && activeTurnId !== before.activeTurnId) ||
        (nativeTurnRunning && !before.nativeTurnRunning) ||
        (nativeItemCursor > Number(before.nativeItemCursor || 0)) ||
        (nativeUpdateCursor > Number(before.nativeUpdateCursor || 0))
      );
    }
    async function recoverNativeControlAfterFetchFailure(error, snapshot) {
      if (!isNativeControlRecoverableError(error)) return false;
      await delay(700);
      await syncNativeTranscript();
      await pollEvents();
      return nativeTurnAdvancedSince(snapshot);
    }
    function clearComposerDraft() {
      promptInput.value = "";
      resizePromptInput();
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
      resizePromptInput();
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
    async function withNativeSoftTimeout(promise, message, delayMs = 12000) {
      let settled = false;
      let timedOut = false;
      let timer = null;
      const timeoutPromise = new Promise(resolve => {
        timer = window.setTimeout(() => {
          if (!settled) {
            timedOut = true;
            updateRunState(message, "busy");
          }
          resolve({});
        }, delayMs);
      });
      promise.catch(() => {});
      try {
        const result = await Promise.race([promise, timeoutPromise]);
        if (!timedOut) return result;
        promise.finally(() => {
          settled = true;
          if (timer) window.clearTimeout(timer);
        }).catch(() => {});
        return {};
      } finally {
        if (!timedOut) {
          settled = true;
          if (timer) window.clearTimeout(timer);
        }
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
        const session = await withNativeSoftTimeout(
          api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}`),
          "同步会话状态较慢"
        );
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
        resizePromptInput();
        return;
      }
      if (promptHasPluginMention(value, mention)) return;
      const separator = value && !value.endsWith(" ") ? " " : "";
      promptInput.value = value + separator + mention + " ";
      const nextCursor = promptInput.value.length;
      promptInput.setSelectionRange(nextCursor, nextCursor);
      resizePromptInput();
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
    copySessionIdButton.onclick = copyNativeSessionId;
    uiFontSizeInput.oninput = () => updateDisplayFontSizeDraft(uiFontSizeInput, "uiFontSize");
    codeFontSizeInput.oninput = () => updateDisplayFontSizeDraft(codeFontSizeInput, "codeFontSize");
    uiFontSizeInput.onchange = () => commitDisplayFontSizeInput(uiFontSizeInput, "uiFontSize");
    codeFontSizeInput.onchange = () => commitDisplayFontSizeInput(codeFontSizeInput, "codeFontSize");
    uiFontSizeInput.onblur = () => commitDisplayFontSizeInput(uiFontSizeInput, "uiFontSize");
    codeFontSizeInput.onblur = () => commitDisplayFontSizeInput(codeFontSizeInput, "codeFontSize");
    uiFontSizeInput.onkeydown = event => commitDisplayFontSizeInputOnEnter(event, uiFontSizeInput, "uiFontSize");
    codeFontSizeInput.onkeydown = event => commitDisplayFontSizeInputOnEnter(event, codeFontSizeInput, "codeFontSize");
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
      resizePromptInput();
      updatePluginAutocomplete();
      updateComposerDisabled();
    });
    resizePromptInput();
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
      if (event.kind === "completed" || event.kind === "failed") return true;
      if (action === "turn_completed" || action === "turn_failed") return true;
      if (event.kind === "lifecycle") return isCompletedStatus(status) || isFailedStatus(status);
      return false;
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
      modelSettingsButton.textContent = [modelText, effortText].filter(Boolean).join(" ");
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
        const result = await withNativeSoftTimeout(api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/attach`, {
          method: "POST",
          body: "{}"
        }), "连接原生会话较慢");
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
        const result = await withNativeSoftTimeout(api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/sync`, {
          method: "POST",
          body: "{}"
        }), "同步原生 transcript 较慢");
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
      if (nativeThreadId && !invalidNativeThreadId) {
        syncNativeTranscript().then(pollEvents);
      }
      nativeTranscriptSyncTimer = setInterval(pollEvents, 1000);
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) pollEvents();
      });
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
      startNativeTranscriptSyncLoop();
    });
    window.addEventListener("pagehide", closeLiveEventSource);
    window.addEventListener("beforeunload", closeLiveEventSource);
    window.addEventListener("pageshow", () => {
      if (!source && currentStreamCursor()) openStream(currentStreamCursor());
      pollEvents();
    });
    function refreshNativeControlInBackground() {
      attachNative().then(loadNativeSessionInfo).catch(error => {
        renderStatus("native_sync_failed", error.message || String(error));
      });
    }
    async function loadRecentEvents() {
      let snapshot;
      if (nativeThreadId) {
        snapshot = await api(nativeMessagesPath("limit=" + NATIVE_TIMELINE_RECENT_LIMIT));
        handleNativeSyncSnapshot(snapshot);
        loadedEvents = nativeMessageItemsToEvents(snapshot.items);
      } else {
        snapshot = await api(eventsPath("tail=" + NATIVE_TIMELINE_RECENT_LIMIT));
        handleNativeSyncSnapshot(snapshot);
        loadedEvents = normalizeEventList(snapshot.events);
        if (
          !loadedEvents.length ||
          !hasLiveDisplayEvents(loadedEvents) ||
          hasUnresolvedApprovalRequests(loadedEvents)
        ) {
          snapshot = await api(eventsPath("tail=" + NATIVE_TIMELINE_RECENT_LIMIT));
          handleNativeSyncSnapshot(snapshot);
          loadedEvents = normalizeEventList(snapshot.events);
        }
      }
      previousEventCount = snapshot.previous_event_count || 0;
      oldestEventId = loadedEvents.length ? loadedEvents[0].id : 0;
      latestEventId = loadedEvents.length ? loadedEvents[loadedEvents.length - 1].id : 0;
      if (nativeThreadId) applyNativeFeedSnapshot(snapshot, loadedEvents);
      rebuildStream();
      updateHistoryFold();
      openStream(currentStreamCursor());
      pollEvents();
    }
    function handleNativeSyncSnapshot(snapshot) {
      if (snapshot && typeof snapshot.previous_item_count === "number") {
        snapshot.previous_event_count = snapshot.previous_item_count;
      }
      if (nativeThreadId) applyNativeFeedSnapshot(snapshot);
      if (snapshot && snapshot.run_state) applyNativeRunState(snapshot.run_state);
      if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);
      else clearStatusNode("native_sync_failed");
    }
    function applyNativeFeedSnapshot(snapshot, snapshotEvents = []) {
      if (!nativeThreadId || !snapshot) return;
      const itemCursor = Number(
        snapshot.item_cursor !== undefined ? snapshot.item_cursor : snapshot.cursor
      );
      const updateCursor = Number(snapshot.update_cursor);
      if (Number.isFinite(itemCursor)) {
        nativeItemCursor = Math.max(nativeItemCursor, itemCursor);
      } else if (snapshotEvents.length) {
        nativeItemCursor = Math.max(nativeItemCursor, lastVisibleEventId(snapshotEvents));
      }
      if (Number.isFinite(updateCursor)) {
        nativeUpdateCursor = Math.max(nativeUpdateCursor, updateCursor);
      }
    }
    function advanceNativeFeedCursors(event, options = {}) {
      if (!nativeThreadId || !event) return;
      const itemCursor = Number(event.id || 0);
      if (Number.isFinite(itemCursor) && itemCursor > 0 && options.visible !== false) {
        nativeItemCursor = Math.max(nativeItemCursor, itemCursor);
      }
      const updateCursor = Number(options.updateCursor || 0);
      if (Number.isFinite(updateCursor) && updateCursor > 0) {
        nativeUpdateCursor = Math.max(nativeUpdateCursor, updateCursor);
      }
    }
    function isValidEventObject(event) {
      return Boolean(event && typeof event === "object" && event.kind);
    }
    function normalizeEventList(sourceEvents) {
      if (!Array.isArray(sourceEvents)) return [];
      return sourceEvents.filter(isValidEventObject);
    }
    function nativeMessageItemsToEvents(items) {
      if (!Array.isArray(items)) return [];
      return items.map(nativeMessageItemToEvent).filter(isValidEventObject);
    }
    function nativeMessageItemToEvent(item) {
      if (!item || typeof item !== "object") return null;
      const payload = Object.assign({}, item.payload || {});
      payload.text = String(item.text || payload.text || "");
      payload.itemId = String(payload.itemId || item.item_key || item.id || "");
      payload.item_id = String(payload.item_id || payload.itemId);
      payload.native_turn_id = String(payload.native_turn_id || item.turn_key || "");
      payload.status = String(item.status || payload.status || "");
      payload.role = String(item.role || payload.role || "");
      payload.message_snapshot = true;
      let kind = String(item.kind || "");
      if (kind === "message") {
        kind = payload.status === "completed" ? "message_completed" : "text_delta";
      }
      return {
        id: Number(item.cursor || item.id || 0),
        sequence: Number(item.cursor || item.id || 0),
        type: kind,
        source_type: "native.conversation.item",
        kind,
        role: String(item.role || ""),
        visible: true,
        provider: item.provider || PROVIDER,
        native_thread_id: item.native_thread_id || nativeThreadId,
        occurred_at: item.updated_at || "",
        payload
      };
    }
    function applyNativeRunState(runState) {
      if (!nativeThreadId || !runState || typeof runState !== "object") return;
      nativeTurnRunning = Boolean(runState.active);
      activeTurnId = nativeTurnRunning ? String(runState.active_turn_id || activeTurnId || "") : "";
      setComposerActivity(nativeTurnRunning || sendingPrompt);
      updateNativeHeaderContext();
      updateComposerDisabled();
    }
    function lastVisibleEventId(sourceEvents) {
      let lastId = 0;
      for (const event of normalizeEventList(sourceEvents)) {
        if (!event || isInternalEvent(event) || !event.id) continue;
        lastId = Math.max(lastId, event.id);
      }
      return lastId;
    }
    function hasLoadedEventId(eventId) {
      if (!eventId) return false;
      return loadedEvents.some(event => event && event.id === eventId);
    }
    function hasLiveDisplayEvents(sourceEvents) {
      return normalizeEventList(sourceEvents).some(event => {
        if (!event) return false;
        if (isInternalEvent(event)) return false;
        return event.kind !== "event";
      });
    }
    function hasNativePlanEvents(sourceEvents) {
      return normalizeEventList(sourceEvents).some(event => isNativePlanEvent(event));
    }
    function mergeDisplayEvents(currentEvents, nextEvents) {
      const byId = new Map();
      for (const event of normalizeEventList(currentEvents)) {
        if (event && event.id) byId.set(event.id, event);
      }
      for (const event of normalizeEventList(nextEvents)) {
        if (event && event.id) byId.set(event.id, event);
      }
      return Array.from(byId.values()).sort((left, right) => (left.id || 0) - (right.id || 0));
    }
    function hasUnresolvedApprovalRequests(sourceEvents) {
      const requested = new Set();
      const resolved = new Set();
      for (const event of normalizeEventList(sourceEvents)) {
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
      if (isCanonicalNativeDisplayEvent(event)) return false;
      return Boolean(
        event && (
          event.type === "model.usage.updated" ||
          isProviderRawFrameEvent(event) ||
          isProviderDisplayCompletedEvent(event) ||
          isCompatibilityMirrorEvent(event) ||
          isNativeExecutionDetail(event) ||
          isNativeReasoningDetail(event) ||
          (isNativeActivityDetail(event) && !isNativePlanEvent(event))
        )
      );
    }
    function isCanonicalNativeDisplayEvent(event) {
      if (!isNativeFeedbackMode(event) || !event) return false;
      if (event.visible === false) return false;
      return (
        event.kind === "user_message" ||
        event.kind === "text_delta" ||
        event.kind === "message_completed" ||
        isNativePlanEvent(event) ||
        event.kind === "approval_requested" ||
        event.kind === "approval_resolved"
      );
    }
    function isProviderRawFrameEvent(event) {
      return Boolean(event && (
        event.type === "provider.raw.frame" ||
        event.kind === "provider_raw_frame"
      ));
    }
    function isProviderDisplayCompletedEvent(event) {
      const payload = (event && event.payload) || {};
      return Boolean(
        event && (
          event.type === "provider.display.completed" ||
          payload.compatibility_projection === "provider.display.completed"
        ) && String(payload.text || payload.summary || "").trim()
      );
    }
    function isProviderDisplayDeltaEvent(event) {
      const payload = (event && event.payload) || {};
      return Boolean(event && (
        event.type === "provider.display.delta" ||
        (event.kind === "text_delta" && payload.display_source === "provider")
      ));
    }
    function isCompatibilityMirrorEvent(event) {
      const payload = (event && event.payload) || {};
      return Boolean(event && (
        event.kind === "compatibility_event" ||
        payload.compatibility_projection === "model.text.delta"
      ));
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
    function scheduleOlderTranscriptSync() {
      if (!nativeThreadId || invalidNativeThreadId) return;
      syncNativeTranscript().then(pollEvents);
    }
    function displayEventCount(sourceEvents) {
      return dedupeDisplayEvents(sourceEvents).length;
    }
    async function loadOlderEvents() {
      if (!oldestEventId || !previousEventCount) return;
      historyFold.disabled = true;
      historyFold.setAttribute("aria-busy", "true");
      const visibleBeforeLoad = displayEventCount(loadedEvents);
      try {
        scheduleOlderTranscriptSync();
        for (let attempt = 0; attempt < OLDER_VISIBLE_PAGE_ATTEMPTS; attempt++) {
          if (!oldestEventId || !previousEventCount) break;
          const snapshot = nativeThreadId
            ? await loadNativeMessages(`before=${oldestEventId}&limit=${OLDER_EVENT_LIMIT}`)
            : await api(eventsPath(`before=${oldestEventId}&limit=${OLDER_EVENT_LIMIT}`));
          handleNativeSyncSnapshot(snapshot);
          const older = nativeThreadId
            ? nativeMessageItemsToEvents(snapshot.items)
            : normalizeEventList(snapshot.events);
          if (!older.length) {
            previousEventCount = snapshot.previous_event_count || 0;
            break;
          }
          loadedEvents = normalizeEventList(older.concat(loadedEvents));
          previousEventCount = snapshot.previous_event_count || 0;
          oldestEventId = loadedEvents.length ? loadedEvents[0].id : 0;
          if (displayEventCount(loadedEvents) > visibleBeforeLoad) break;
        }
        rebuildStream();
        updateHistoryFold();
      } finally {
        historyFold.removeAttribute("aria-busy");
        historyFold.disabled = false;
        updateHistoryFold();
      }
    }
    function closeLiveEventSource() {
      if (streamReconnectTimer) {
        clearTimeout(streamReconnectTimer);
        streamReconnectTimer = null;
      }
      if (source) {
        source.close();
        source = null;
      }
    }
    function scheduleStreamReconnect() {
      if (streamReconnectTimer) return;
      if (source) {
        source.close();
        source = null;
      }
      streamReconnectTimer = window.setTimeout(() => {
        streamReconnectTimer = null;
        openStream(currentStreamCursor());
        streamReconnectDelay = Math.min(streamReconnectDelay * 2, 5000);
      }, streamReconnectDelay);
    }
    function currentStreamCursor() {
      return nativeThreadId ? nativeUpdateCursor : latestEventId;
    }
    function openStream(afterId) {
      closeLiveEventSource();
      source = new EventSource(streamPathWithCursor(afterId));
      source.onopen = () => {
        streamReconnectDelay = 500;
        setConnectionState("connected");
      };
      source.onerror = () => {
        setConnectionState("reconnecting");
        pollEvents();
        scheduleStreamReconnect();
      };
      source.onmessage = (message) => renderNativeStreamPayload(JSON.parse(message.data));
      [
        "message_added",
        "message_updated",
        "message_completed",
        "run_state",
        "sync_state",
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
        source.addEventListener(kind, message => renderNativeStreamPayload(JSON.parse(message.data)));
      });
    }
    function renderNativeStreamPayload(payload) {
      if (nativeThreadId && payload && typeof payload === "object") {
        if (payload.run_state) {
          applyNativeRunState(payload.run_state);
          return;
        }
        if (payload.item) {
          applyNativeFeedSnapshot(payload);
          renderLiveEvent(payload.event || nativeMessageItemToEvent(payload.item));
          return;
        }
      }
      renderLiveEvent(payload);
    }
    function streamPathWithCursor(afterId) {
      if (nativeThreadId) {
        return nativeMessagesStreamPath(afterId);
      }
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      if (afterId) params.set("after", String(afterId));
      if (nativeThreadId) params.set("native_thread_id", nativeThreadId);
      if (PROVIDER) params.set("native_provider", PROVIDER);
      const suffix = params.toString();
      return suffix ? streamPathBase + "?" + suffix : streamPathBase;
    }
    function nativeTimelineStreamPath(afterId) {
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      if (afterId) params.set("after", String(afterId));
      const suffix = params.toString();
      const base = `${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/timeline/stream`;
      return suffix ? base + "?" + suffix : base;
    }
    function nativeMessagesStreamPath(afterId) {
      const params = new URLSearchParams();
      if (token) params.set("token", token);
      if (afterId) params.set("after_update", String(afterId));
      const suffix = params.toString();
      const base = `${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/messages/stream`;
      return suffix ? base + "?" + suffix : base;
    }
    function nativeTimelinePath(params) {
      const search = new URLSearchParams(params);
      if (token) search.set("token", token);
      return `${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/timeline?${search.toString()}`;
    }
    function nativeMessagesPath(params) {
      const search = new URLSearchParams(params);
      if (token) search.set("token", token);
      return `${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/messages?${search.toString()}`;
    }
    function loadNativeMessages(params) {
      return api(nativeMessagesPath(params));
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
        const snapshot = nativeThreadId
          ? await api(nativeMessagesPath(`after_update=${nativeUpdateCursor}&limit=100`))
          : await api(eventsPath("after=" + latestEventId + "&limit=100"));
        handleNativeSyncSnapshot(snapshot);
        if (nativeThreadId) {
          const nextEvents = nativeMessageItemsToEvents(snapshot.items);
          for (const event of nextEvents) renderLiveEvent(event);
          setConnectionState("connected");
          return;
        }
        const nextEvents = normalizeEventList(snapshot.events);
        for (const event of nextEvents) renderLiveEvent(event);
        setConnectionState("connected");
      } catch (_error) {
        setConnectionState("reconnecting");
      } finally {
        pollInFlight = false;
      }
    }
    function rebuildStream() {
      commitRenderedStream(loadedEvents);
    }
    function commitRenderedStream(nextEvents) {
      const previousChildren = Array.from(events.childNodes);
      const previousTarget = renderTarget;
      const previousTranscriptNodes = new Map(transcriptNodes);
      const previousStatusNodes = new Map(statusNodes);
      const previousCommandNodes = new Map(commandNodes);
      const previousFileChangeSummaryNodes = new Map(fileChangeSummaryNodes);
      const previousFileChangeSummaryStates = new Map(fileChangeSummaryStates);
      const staging = document.createElement("div");
      try {
        transcriptNodes.clear();
        statusNodes.clear();
        commandNodes.clear();
        fileChangeSummaryNodes.clear();
        fileChangeSummaryStates.clear();
        const groups = foldGroups(dedupeDisplayEvents(nextEvents)).map(orderTranscriptGroupEvents);
        const latestTurnId = latestFoldGroupTurnId(groups);
        groups.forEach(group => {
          renderFoldGroup(group, {latestTurnId, target: staging});
        });
        if (!staging.childNodes.length && hasRenderedTranscriptContent(previousChildren)) {
          throw new Error("empty staged native transcript");
        }
        events.replaceChildren(...Array.from(staging.childNodes));
      } catch (error) {
        transcriptNodes.clear();
        for (const [key, value] of previousTranscriptNodes) transcriptNodes.set(key, value);
        statusNodes.clear();
        for (const [key, value] of previousStatusNodes) statusNodes.set(key, value);
        commandNodes.clear();
        for (const [key, value] of previousCommandNodes) commandNodes.set(key, value);
        fileChangeSummaryNodes.clear();
        for (const [key, value] of previousFileChangeSummaryNodes) fileChangeSummaryNodes.set(key, value);
        fileChangeSummaryStates.clear();
        for (const [key, value] of previousFileChangeSummaryStates) fileChangeSummaryStates.set(key, value);
        renderTarget = previousTarget;
        events.replaceChildren(...previousChildren);
        renderStatus("render_recovered", (error && error.message) || "显示同步已恢复，正在重新连接");
        setConnectionState("reconnecting");
        window.setTimeout(pollEvents, 0);
      } finally {
        renderTarget = previousTarget;
      }
    }
    function hasRenderedTranscriptContent(nodes) {
      return Array.from(nodes || []).some(node => {
        if (!node || node.nodeType !== 1) return false;
        if (node === empty) return false;
        if (node.classList && node.classList.contains("empty")) return false;
        return true;
      });
    }
    function renderLiveEvent(event) {
      if (!isValidEventObject(event)) return;
      if (!nativeThreadId && event.id && event.id <= latestEventId) return;
      if (nativeThreadId && event.id && hasLoadedEventId(event.id)) {
        advanceNativeFeedCursors(event, {visible: !isInternalEvent(event)});
        if (isAssistantMessageEvent(event) || event.kind === "message_completed") {
          loadedEvents = loadedEvents.filter(existing => !existing || existing.id !== event.id);
          loadedEvents.push(event);
          rebuildStream();
          scrollToBottom();
        }
        return;
      }
      const previousLatestTurnId = latestFoldGroupTurnId(foldGroups(dedupeDisplayEvents(loadedEvents)));
      if (!nativeThreadId && event.id) latestEventId = Math.max(latestEventId, event.id);
      const incomingTurnId = eventFoldTurnId(event);
      const duplicateDisplayEvent = isDuplicateDisplayEvent(event, loadedEvents);
      if (!isInternalEvent(event)) {
        advanceNativeFeedCursors(event, {visible: true});
      } else {
        advanceNativeFeedCursors(event, {visible: false});
      }
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
      for (const event of normalizeEventList(sourceEvents)) {
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
      const completedAssistantTexts = completedAssistantTextByTurn(sourceEvents);
      const completedAssistantFinalTurns = completedAssistantFinalTurnSet(sourceEvents);
      const canonicalUserTurns = canonicalUserTranscriptTurnSet(sourceEvents);
      const seen = new Set();
      const seenUserMessages = new Map();
      const seenUserMessageText = new Map();
      const seenAssistantVisible = new Map();
      const seenAssistantCompleted = new Map();
      const result = [];
      for (const event of normalizeEventList(sourceEvents)) {
        if (isInternalEvent(event)) continue;
        if (isAssistantMessageEvent(event) && !hasVisibleTranscriptText(event)) continue;
        if (shouldDropAssistantMirrorEvent(event, completedAssistantTexts, completedAssistantFinalTurns)) continue;
        if (isAssistantMessageEvent(event)) {
          const assistantFingerprint = assistantDisplayTextFingerprint(event);
          const previousAssistantIndex = assistantFingerprint
            ? seenAssistantVisible.get(assistantFingerprint)
            : undefined;
          if (previousAssistantIndex !== undefined) {
            const previousAssistant = result[previousAssistantIndex];
            if (!previousAssistant) {
              seenAssistantVisible.delete(assistantFingerprint);
            } else {
              if (
                assistantVisibleDedupePriority(event) >
                assistantVisibleDedupePriority(previousAssistant)
              ) {
                result[previousAssistantIndex] = event;
              }
              continue;
            }
          }
          if (assistantFingerprint) seenAssistantVisible.set(assistantFingerprint, result.length);
        }
        if (event.kind === "message_completed") {
          const completedFingerprint = completedAssistantTextFingerprint(event);
          const previousCompletedIndex = completedFingerprint
            ? seenAssistantCompleted.get(completedFingerprint)
            : undefined;
          if (previousCompletedIndex !== undefined) {
            const previousCompleted = result[previousCompletedIndex];
            if (!previousCompleted) {
              seenAssistantCompleted.delete(completedFingerprint);
            } else {
              if (
                completedAssistantDedupePriority(event) >
                completedAssistantDedupePriority(previousCompleted)
              ) {
                result[previousCompletedIndex] = event;
              }
              continue;
            }
          }
          if (completedFingerprint) seenAssistantCompleted.set(completedFingerprint, result.length);
        }
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
          const userTextFingerprint = userMessageTextFingerprint(event);
          const previousUserTextIndex = userTextFingerprint
            ? seenUserMessageText.get(userTextFingerprint)
            : undefined;
          if (previousUserTextIndex !== undefined) {
            const previousUser = result[previousUserTextIndex];
            if (shouldDedupeUserBySyntheticText(event, previousUser)) {
              if (userMessageDedupePriority(event) > userMessageDedupePriority(previousUser)) {
                result[previousUserTextIndex] = event;
              }
              continue;
            }
          }
          if (userTextFingerprint) seenUserMessageText.set(userTextFingerprint, result.length);
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
    function visibleTranscriptText(event) {
      return stripCodexAppDirectives(rawTranscriptText(event));
    }
    function rawTranscriptText(event) {
      const payload = (event && event.payload) || {};
      return String(payload.text || payload.delta || payload.summary || payload.message || "");
    }
    function stripCodexAppDirectives(text) {
      return String(text || "")
        .replace(/::[a-z][a-z0-9-]*[{][^}\\n]*[}]/gi, "")
        .replace(/[ \t]+$/gm, "")
        .replace(/\\n{3,}/g, "\\n\\n")
        .trim();
    }
    function hasVisibleTranscriptText(event) {
      return Boolean(visibleTranscriptText(event).trim());
    }
    function completedAssistantTextByTurn(sourceEvents) {
      const byTurn = new Map();
      const globalKey = "__global__";
      for (const event of sourceEvents || []) {
        if (!event || isInternalEvent(event)) continue;
        if (event.kind !== "message_completed") continue;
        const turnId = eventFoldTurnId(event);
        const fingerprint = assistantDisplayTextFingerprint(event);
        if (!fingerprint) continue;
        if (!byTurn.has(globalKey)) byTurn.set(globalKey, new Set());
        byTurn.get(globalKey).add(fingerprint);
        if (!turnId) continue;
        if (!byTurn.has(turnId)) byTurn.set(turnId, new Set());
        byTurn.get(turnId).add(fingerprint);
      }
      return byTurn;
    }
    function shouldDropAssistantMirrorEvent(event, completedAssistantTexts, completedAssistantFinalTurns = new Set()) {
      if (!event || event.kind !== "text_delta") return false;
      const payload = event.payload || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (!isProviderDisplayDeltaEvent(event) && !itemId.startsWith("jsonl-assistant")) return false;
      const turnId = eventFoldTurnId(event);
      if (turnId && completedAssistantFinalTurns.has(turnId)) return true;
      const fingerprint = assistantDisplayTextFingerprint(event);
      return Boolean(
        fingerprint &&
        (
          (turnId && completedAssistantTexts.get(turnId)?.has(fingerprint)) ||
          completedAssistantTexts.get("__global__")?.has(fingerprint)
        )
      );
    }
    function completedAssistantFinalTurnSet(sourceEvents) {
      const turns = new Set();
      for (const event of normalizeEventList(sourceEvents)) {
        if (!event || event.kind !== "message_completed") continue;
        const key = assistantTurnKey(event);
        if (key) turns.add(key);
      }
      return turns;
    }
    function assistantDisplayTextFingerprint(event) {
      return normalizeTranscriptText(visibleTranscriptText(event));
    }
    function completedAssistantTextFingerprint(event) {
      const turnId = eventFoldTurnId(event);
      const fingerprint = assistantDisplayTextFingerprint(event);
      if (!fingerprint) return "";
      if (!turnId) return `global:${fingerprint}`;
      return `turn:${turnId}:${fingerprint}`;
    }
    function completedAssistantDedupePriority(event) {
      if (!event) return 0;
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      if (event.type === "model.message.completed" && /^item-[0-9]+$/.test(itemId)) return 50;
      if (event.type === "model.message.completed") return 40;
      if (itemId.startsWith("jsonl-assistant-final")) return 30;
      if (event.type === "provider.display.completed") return 20;
      return 10;
    }
    function assistantVisibleDedupePriority(event) {
      if (!event) return 0;
      if (event.kind === "message_completed") return completedAssistantDedupePriority(event);
      if (isOfficialAssistantTranscriptEvent(event)) return 15;
      if (isProviderDisplayDeltaEvent(event)) return 5;
      return 10;
    }
    function canonicalUserTranscriptTurnSet(sourceEvents) {
      const turns = new Set();
      for (const event of normalizeEventList(sourceEvents)) {
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
      return Boolean(payload.local) || itemId.startsWith("local-user-") || itemId.startsWith("jsonl-user:") || turnId.startsWith("jsonl-turn:");
    }
    function isTurnlessSyntheticUserMessageEvent(event) {
      return isSyntheticUserMessageEvent(event) && !eventFoldTurnId(event);
    }
    function userMessageTextFingerprint(event) {
      if (!event || event.kind !== "user_message") return "";
      const payload = event.payload || {};
      const text = normalizeTranscriptText(
        String(payload.text || payload.delta || payload.summary || payload.content || payload.prompt || "")
      );
      const images = Array.isArray(payload.images) ? payload.images.length : 0;
      if (!text && !images) return "";
      return `user-message-text:${text}:${images}`;
    }
    function shouldDedupeUserBySyntheticText(event, previous) {
      return Boolean(
        userMessageTextFingerprint(event) &&
        userMessageTextFingerprint(event) === userMessageTextFingerprint(previous) &&
        (isTurnlessSyntheticUserMessageEvent(event) || isTurnlessSyntheticUserMessageEvent(previous))
      );
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
      for (const event of normalizeEventList(sourceEvents)) {
        if (isOfficialAssistantTranscriptEvent(event)) {
          const key = assistantTurnKey(event);
          if (key) turns.add(key);
        }
      }
      return turns;
    }
    function isAssistantMessageEvent(event) {
      return Boolean(event && (event.kind === "text_delta" || event.kind === "message_completed"));
    }
    function isTranscriptEvent(event) {
      return Boolean(event && (event.kind === "user_message" || isAssistantMessageEvent(event)));
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
      if (!isValidEventObject(event)) return true;
      const completedAssistantTexts = completedAssistantTextByTurn(previousEvents);
      const completedAssistantFinalTurns = completedAssistantFinalTurnSet(previousEvents);
      if (shouldDropAssistantMirrorEvent(event, completedAssistantTexts, completedAssistantFinalTurns)) return true;
      if (event.kind === "message_completed") {
        const completedFingerprint = completedAssistantTextFingerprint(event);
        if (completedFingerprint) {
          let previousPriority = 0;
          for (const previous of normalizeEventList(previousEvents)) {
            if (previous.kind !== "message_completed") continue;
            if (completedAssistantTextFingerprint(previous) !== completedFingerprint) continue;
            previousPriority = Math.max(previousPriority, completedAssistantDedupePriority(previous));
          }
          if (previousPriority >= completedAssistantDedupePriority(event)) return true;
        }
      }
      const key = mirroredDisplayKey(event);
      if (key && normalizeEventList(previousEvents).some(previous => mirroredDisplayKey(previous) === key)) {
        return true;
      }
      if (event.kind === "user_message") {
        const fingerprint = canonicalUserMessageFingerprint(event);
        if (fingerprint && normalizeEventList(previousEvents).some(previous => canonicalUserMessageFingerprint(previous) === fingerprint)) {
          return true;
        }
        if (normalizeEventList(previousEvents).some(previous => shouldDedupeUserBySyntheticText(event, previous))) {
          return true;
        }
      }
      if (!event.id) return false;
      const eventId = String(event.id);
      return normalizeEventList(previousEvents).some(previous => String(previous.id || "") === eventId);
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
        const leftItemOrder = transcriptItemOrder(left);
        const rightItemOrder = transcriptItemOrder(right);
        if (leftItemOrder !== Number.MAX_SAFE_INTEGER || rightItemOrder !== Number.MAX_SAFE_INTEGER) {
          return (
            leftItemOrder - rightItemOrder ||
            displayEventOrder(left) - displayEventOrder(right) ||
            Number((left && left.id) || 0) - Number((right && right.id) || 0)
          );
        }
        return (
          displayEventOrder(left) - displayEventOrder(right) ||
          Number((left && left.id) || 0) - Number((right && right.id) || 0)
        );
      });
    }
    function displayEventOrder(event) {
      if (!event) return 60;
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
      return group.reduce((latest, event) => Math.max(latest, Number((event && event.id) || 0)), 0);
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
      const target = options.target || events;
      const summary = buildFoldSummary(group, options.latestTurnId || "");
      if (!summary.shouldCollapse) {
        for (const event of group) {
          if (!isValidEventObject(event)) continue;
          render(event, {scroll: false, historical: true, target});
        }
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
      target.append(details);
      const previousTarget = renderTarget;
      renderTarget = inner;
      try {
        for (const event of group) {
          if (!isValidEventObject(event)) continue;
          render(event, {scroll: false, historical: true});
        }
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
        if (!event) continue;
        if (event.kind !== kind) continue;
        const payload = event.payload || {};
        const text = (kind === "user_message"
          ? String(payload.text || payload.delta || "")
          : visibleTranscriptText(event)
        ).trim();
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
      const firstEvent = group.find(isValidEventObject) || {};
      const nativeTurnId = String((firstEvent.payload || {}).native_turn_id || "");
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
      return group.some(event => (
        !isInternalEvent(event) &&
        (!isAssistantMessageEvent(event) || hasVisibleTranscriptText(event))
      ));
    }
    function groupHasGeneratedPrompt(group) {
      return group.some(event => {
        if (!isAssistantMessageEvent(event)) return false;
        return Boolean(splitGeneratedPromptText(visibleTranscriptText(event)));
      });
    }
    function hasPendingApproval(group) {
      const requested = new Set();
      for (const event of group) {
        if (!event || event.kind !== "approval_requested") continue;
        const key = approvalRequestKey(event);
        if (!key) return true;
        requested.add(key);
      }
      if (!requested.size) return false;
      const resolved = new Set();
      for (const event of loadedEvents) {
        if (!event || event.kind !== "approval_resolved") continue;
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
      if (!event) return "";
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
      const payload = (event && event.payload) || {};
      return Boolean(event && (event.kind === "failed" || isFailedStatus(payload.status)));
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
      if (event.id) cursor.textContent = "#" + event.id;
    }
    function renderTranscript(event, role, label, opts = {}) {
      const payload = event.payload || {};
      const assistantRole = role.includes("assistant");
      let visibleText = assistantRole ? visibleTranscriptText(event) : String(payload.text || payload.delta || payload.summary || "");
      if (assistantRole && !visibleText.trim()) return;
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
        if (!assistantRole) row.append(meta);
        row.append(body);
        renderTarget.append(row);
        node = {row, body, text: ""};
        transcriptNodes.set(key, node);
      }
      const incomingText = visibleText;
      if (assistantRole) {
        if (event.kind === "message_completed" || payload.message_snapshot) {
          node.text = visibleText;
          if (event.kind === "message_completed") node.row.dataset.completed = "true";
        } else {
          node.text += visibleText;
          visibleText = node.text;
        }
        const renderedPrompt = renderGeneratedPromptTranscript(node.body, node.text, event);
        node.row.classList.toggle("prompt-message", renderedPrompt);
        if (renderedPrompt) return;
        renderMarkdownLite(node.body, visibleText);
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
      const display = statusDisplay(event, fallback);
      if (!display.title && !display.detail) return;
      const key = statusKey(event);
      let node = statusNodes.get(key);
      if (!node) {
        const row = document.createElement("div");
        row.className = "status-event " + (tone || "neutral");
        const body = document.createElement("div");
        const title = document.createElement("span");
        title.className = "status-title";
        const detail = document.createElement("span");
        detail.className = "status-detail";
        body.append(title, detail);
        row.append(body);
        renderTarget.append(row);
        node = {row, title, detail};
        statusNodes.set(key, node);
      }
      node.row.className = "status-event " + (tone || "neutral");
      node.title.textContent = display.title;
      node.detail.textContent = display.detail;
      node.detail.hidden = !display.detail;
      updateRunState(display.title || display.detail, tone || "neutral");
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
      if (kind === "native_sync_failed") {
        if (isNoisyNativeSyncError(text)) return;
      }
      renderStatusEvent(
        {kind, payload: {text: text || ""}},
        text || kind,
        kind === "attach_failed" ? "failed" : "neutral"
      );
    }
    function clearStatusNode(kind) {
      const key = statusKey({kind, payload: {}});
      const node = statusNodes.get(key);
      if (!node) return;
      node.row.remove();
      statusNodes.delete(key);
    }
    function updateRunState(text, tone) {
      if (!text) return;
      writeCompactText(sessionFloatState, text);
      sessionFloat.className = "session-float " + (tone || "neutral");
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
      if (event.kind === "message_completed") return completedAssistantMessageKey(event);
      if (itemId.startsWith("jsonl-assistant")) return itemId;
      return `${payload.native_turn_id || payload.turnId || ""}:assistant`;
    }
    function completedAssistantMessageKey(event) {
      const payload = (event && event.payload) || {};
      const itemId = String(payload.itemId || payload.item_id || "");
      const fingerprint = completedAssistantTextFingerprint(event);
      if (fingerprint) return fingerprint;
      if (itemId) return `${itemId}:${event.id || transcriptTextFingerprint(event)}`;
      return `${payload.native_turn_id || payload.turnId || ""}:completed:${event.id || transcriptTextFingerprint(event)}`;
    }
    function transcriptTextFingerprint(event) {
      return visibleTranscriptText(event).slice(0, 160);
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
        const ordered = line.match(/^\\s*\\d+[.)]\\s*(.+)$/);
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
    function statusDisplay(event, fallback) {
      const payload = event.payload || {};
      const title = statusTitle(event, fallback);
      let detail = String(payload.delta || payload.text || payload.summary || "").trim();
      if (!detail && fallback && fallback !== title) detail = String(fallback).trim();
      if (detail === title) detail = "";
      return {title, detail};
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
        template.replace("__SAFE_TITLE__", safe_title)
        .replace("__NATIVE_APP_HEAD__", _NATIVE_APP_HEAD)
        .replace("__MARVIS_CSS_LINK__", marvis_css_link)
        .replace("__MARVIS_BODY_ATTR__", marvis_body_attr)
        .replace("__MARVIS_EXTRA_HTML__", "")
        .replace("__PROVIDER_LABEL_TEXT__", safe_title)
        .replace("__STREAM_PATH__", stream_path)
        .replace("__AGENT_RUN_ID__", str(agent_run_id))
        .replace("__RUN_ID__", str(agent_run_id))
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
                _codex_plugin_menu_items() if supports_plugin_menu else [],
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
