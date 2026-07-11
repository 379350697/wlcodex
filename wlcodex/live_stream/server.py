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
from wlcodex.codex_native.controller import build_native_session_presentation
from wlcodex.council import (
    CouncilConfig,
    CouncilReviewPacket,
    CouncilReviewRequest,
    CouncilReviewResult,
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
from wlcodex.live_stream.council_routes import handle_council_route
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.models import WorkerStreamEvent
from wlcodex.live_stream.native_pages import (
    native_app_icon_svg as _render_native_app_icon_svg,
    native_app_manifest as _render_native_app_manifest,
    native_provider_display_name as _render_native_provider_display_name,
    render_native_provider_index_page,
    render_native_workflows_page,
)
from wlcodex.live_stream.native_agent_routes import handle_native_agent_route
from wlcodex.live_stream.native_templates.registry import render_native_template
from wlcodex.live_stream.native_templates.codex_page import render_native_codex_page
from wlcodex.live_stream.native_message_projection import (
    format_native_message_sse_event,
    format_native_timeline_sse_event,
    is_visible_native_timeline_event as _is_visible_native_timeline_event,
    native_conversation_item_json as _native_conversation_item_json,
    native_messages_run_state as _native_messages_run_state,
    native_timeline_display_event as _native_timeline_display_event,
    native_timeline_display_summary as _native_timeline_display_summary,
)
from wlcodex.live_stream.presentation import (
    activity_label as _relay_activity_label,
    presentation_state_filter as _relay_presentation_state_filter,
    role_label as _relay_role_label,
    role_status_label as _relay_role_status_label,
    status_class_name as _relay_status_class_name,
    summary_presentation as _relay_summary_presentation,
    summary_presentation_state as _relay_summary_presentation_state,
    task_status_label as _relay_task_status_label,
)
from wlcodex.live_stream.relay_navigation import (
    marvis_relay_bottom_nav as _marvis_relay_bottom_nav,
    marvis_relay_topbar as _marvis_relay_topbar,
    relay_chat_href as _relay_chat_href,
    relay_inbox_href as _relay_inbox_href,
    relay_settings_href as _relay_settings_href,
    relay_task_events_suffix as _relay_task_events_suffix,
    relay_task_list_href as _relay_task_list_href,
    relay_task_view_href as _relay_task_view_href,
    relay_workspace_href as _relay_workspace_href,
)
from wlcodex.live_stream.relay_composer import (
    _marvis_relay_attachment_script,
    _marvis_relay_attachment_sheet_html,
    _marvis_relay_workspace_dock,
)
from wlcodex.live_stream.relay_chat_page import render_relay_chat_home_page
from wlcodex.live_stream.relay_collection_routes import handle_relay_collection_route
from wlcodex.live_stream.relay_event_route import handle_relay_task_events_route
from wlcodex.live_stream.relay_task_routes import (
    RelayTaskRouteDependencies,
    handle_relay_task_route,
)
from wlcodex.live_stream.relay_task_detail_template import (
    render_relay_task_detail_page,
)
from wlcodex.live_stream.relay_ui_routes import (
    RelayUiRouteDependencies,
    handle_relay_ui_route,
)
from wlcodex.live_stream.relay_conversation_view import render_conversation_rows
from wlcodex.live_stream.relay_config_page import render_relay_config_page
from wlcodex.live_stream.relay_list_views import (
    marvis_relay_avatar_html as _marvis_relay_avatar_html,
    relay_task_card_html as _relay_task_card_html,
    relay_task_pagination_html as _relay_task_pagination_html,
    relay_workspace_nav_html as _relay_workspace_nav_html,
)
from wlcodex.live_stream.relay_sse_projection import (
    compact_relay_sse_payload as _compact_relay_sse_payload,
    offer_json_queue as _offer_json_queue,
    relay_active_worker_jobs as _relay_active_worker_jobs,
    relay_worker_payload as _relay_worker_payload,
)
from wlcodex.live_stream.relay_work_log_views import (
    render_work_log_entry as _render_work_log_entry,
    render_work_log_segment as _render_work_log_segment,
)
from wlcodex.live_stream.workflow_routes import handle_workflow_route
from wlcodex.live_stream.routing import (
    agent_id_from_path as _agent_id_from_path,
    native_login_provider_from_path as _native_login_provider_from_path,
    native_messages_route_from_path as _native_messages_route_from_path,
    native_page_provider_from_path as _native_page_provider_from_path,
    native_provider_route_parts as _native_provider_route_parts,
    native_timeline_route_from_path as _native_timeline_route_from_path,
    normalize_relay_api_path as _normalize_relay_api_path,
    optional_nonempty_string as _optional_nonempty_string,
    relay_task_api_parts as _relay_task_api_parts,
    relay_task_id_from_ui_path as _relay_task_id_from_ui_path,
    safe_int as _safe_int,
)
from wlcodex.native_timeline import (
    NativeTimelineEvent,
    NativeTimelineItem,
    NativeTimelineStore,
)
from wlcodex.native_turn_semantics import turn_semantics_json
from wlcodex.jsonrpc import JsonRpcError, JsonRpcTimeout
from wlcodex.maintenance import MaintenanceWindowError, assert_submissions_open
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
from wlcodex.relay.graph import build_marvis_relay_state
from wlcodex.relay.marvis_interaction import (
    project_relay_rows_to_marvis_typed_events,
)
from wlcodex.relay.mutations import (
    MutationStore,
    RelayMutationClaim,
    RelayMutationStore,
)
from wlcodex.relay.models import RELAY_ROLE_IDS
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
_RELAY_MARVIS_CSS_HREF = "/static/relay_marvis.css?v=20260710-dialog-a11y"
_RELAY_MOBILE_JS_HREF = "/static/relay_mobile.js?v=20260701-mobile-web"

_NATIVE_APP_HEAD = """  <link rel="manifest" href="/native/manifest.webmanifest">
  <meta name="theme-color" content="#000000">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="WLCodex">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">"""


def _relay_mobile_web_head(title: str) -> str:
    return f"""  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light only">
  <meta name="theme-color" content="#FAF8F5">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="{_RELAY_MARVIS_CSS_HREF}">
  <script src="/static/surface_runtime.js?v=20260710-semantic-closure" defer></script>
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
    "✎": _ICON_SVG["pencil"],
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


_NATIVE_THREAD_NOT_FOUND_ERROR = (
    "历史会话已不在当前 Codex 后台中，无法继续发送。"
    "请回到会话列表选择可恢复会话，或新建会话继续。"
)


def _is_native_thread_not_found_error(exc: JsonRpcError) -> bool:
    return exc.code == -32600 and re.match(
        r"^thread not found:\s*\S+\s*$",
        str(exc.rpc_message or ""),
        flags=re.IGNORECASE,
    ) is not None


def _native_json_rpc_error_payload(exc: JsonRpcError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": exc.rpc_message or str(exc),
        "code": exc.code,
    }
    if _is_native_thread_not_found_error(exc):
        payload["error"] = _NATIVE_THREAD_NOT_FOUND_ERROR
        payload["native_error_code"] = "native_thread_not_found"
    return payload


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
        self._relay_lifecycle_task: asyncio.Task[None] | None = None
        self._login_tickets: dict[str, float] = {}

    def _maintenance_submission_error(self) -> str | None:
        """Return a public error when the global maintenance gate is closed.

        Native sessions are provider-owned and therefore do not necessarily
        create a Relay task row.  Consult the runtime store's shared SQLite
        connection here so Native, Relay and Telegram all obey one durable
        submission freeze.
        """

        store = getattr(self._hub, "_store", None)
        conn = getattr(store, "_conn", None)
        if conn is None:
            return None
        try:
            assert_submissions_open(conn)
        except MaintenanceWindowError as exc:
            return str(exc)
        return None

    async def _reject_if_maintenance_frozen(self, writer: asyncio.StreamWriter) -> bool:
        error = self._maintenance_submission_error()
        if error is None:
            return False
        await self._send_json(
            writer,
            423,
            {
                "error": error,
                "code": "maintenance_submissions_frozen",
                "retryable": True,
            },
        )
        return True

    def _mutation_store(self) -> MutationStore:
        """Return the shared durable mutation ledger for Native and Relay.

        ``WorkerLiveStreamHub`` is constructed around the runtime store, whose
        SQLite connection is migrated together with the rest of WLCodex.  A
        Native provider session is not a Relay task, but both need the same
        replay protection at the HTTP boundary.
        """

        store = getattr(self._hub, "_store", None)
        conn = getattr(store, "_conn", None)
        if conn is None:
            raise RuntimeError("runtime mutation ledger is unavailable")
        return MutationStore.from_connection(conn)

    async def _begin_native_mutation(
        self,
        writer: asyncio.StreamWriter,
        *,
        headers: dict[str, str],
        operation: str,
        payload: dict[str, Any],
    ) -> tuple[MutationStore, RelayMutationClaim | None] | None:
        """Claim one idempotent Native operation or send its replay/conflict.

        Empty keys preserve the historical programmatic API.  Every current
        Native UI mutation supplies a key, so retries after a transport loss
        replay the first durable response rather than start a second turn.
        """

        mutation_store = self._mutation_store()
        try:
            claim = mutation_store.claim(
                key=headers.get("idempotency-key", ""),
                operation=operation,
                task_id=None,
                payload=payload,
            )
        except ValueError as exc:
            await self._send_json(writer, 400, {"error": str(exc)})
            return None
        if claim is None:
            return mutation_store, None
        if claim.is_replay:
            await self._send_json(
                writer,
                int(claim.response_status or 200),
                dict(claim.response_payload or {}),
            )
            return None
        if not claim.should_execute:
            await self._send_json(
                writer,
                409,
                {
                    "error": claim.error or "mutation is already in progress",
                    "retryable": claim.status == "in_progress",
                },
            )
            return None
        return mutation_store, claim

    async def _finish_native_mutation(
        self,
        writer: asyncio.StreamWriter,
        mutation: tuple[MutationStore, RelayMutationClaim | None],
        *,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        mutation_store, claim = mutation
        if claim is not None:
            mutation_store.complete(claim.key, status=status, payload=payload)
        await self._send_json(writer, status, payload)

    @staticmethod
    def _abandon_native_mutation(
        mutation: tuple[MutationStore, RelayMutationClaim | None],
    ) -> None:
        mutation_store, claim = mutation
        if claim is not None:
            mutation_store.abandon(claim.key)

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
        self._schedule_relay_lifecycle_worker()

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
        if (
            self._relay_lifecycle_task is not None
            and self._relay_lifecycle_task is not asyncio.current_task()
        ):
            tasks.append(self._relay_lifecycle_task)
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
                "/native/workflows/relay/inbox",
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
                # A worker-event snapshot is an observation.  In particular,
                # history pagination is used during page refresh, so it must
                # not start a transcript import that writes runtime/timeline
                # state.  A separately owned watcher or an explicit sync owns
                # those writes; expose only whether one is already running.
                native_sync_pending = self._native_transcript_task_running(
                    native_provider_key,
                    native_thread_id,
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
                    _native_json_rpc_error_payload(exc),
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
        # Deduplication changes persisted timeline rows, so it belongs to the
        # background import owner rather than any GET or initial SSE replay.
        # Keeping it beside projection also makes the visible timeline stable
        # regardless of which read surface observes it first.
        if self._native_timeline is not None:
            self._native_timeline.suppress_duplicate_completed_messages(
                provider_name,
                native_thread_id,
            )
            self._native_timeline.latest_turn_run_state(
                provider_name,
                native_thread_id,
                repair=True,
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

    def _schedule_native_timeline_lifecycle_reconcile(
        self,
        provider: str,
        native_thread_id: str,
    ) -> bool:
        """Repair legacy Native lifecycle projection after an SSE replay.

        ``latest_turn_run_state`` can derive a terminal state from runtime
        events even when an older process skipped its materialized lifecycle
        item.  The repair is intentionally owned by this idempotent background
        task rather than a GET handler or the initial SSE snapshot.  Repeating
        the task after reconnect/restart is safe because the timeline upsert is
        keyed by provider, thread, turn and item key.
        """
        if self._native_timeline is None or not native_thread_id:
            return False
        provider_name = provider.strip().lower() or "codex"
        task_key = ("native_timeline_reconcile", provider_name, native_thread_id)
        existing = self._native_background_tasks.get(task_key)
        if existing is not None and not existing.done():
            return True

        async def reconcile() -> None:
            try:
                # Let the initial snapshot flush before any reconciliation is
                # allowed to write an item or publish an update to the stream.
                await asyncio.sleep(_NATIVE_BACKGROUND_REFRESH_DELAY_SECONDS)
                timeline = self._native_timeline
                if timeline is not None:
                    timeline.latest_turn_run_state(
                        provider_name,
                        native_thread_id,
                        repair=True,
                    )
                self._native_background_errors.pop(task_key, None)
            except Exception as exc:
                self._native_background_errors[task_key] = (
                    str(exc) or "native timeline lifecycle reconciliation failed"
                )
            finally:
                if self._native_background_tasks.get(task_key) is task:
                    self._native_background_tasks.pop(task_key, None)

        task = asyncio.create_task(reconcile())
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
            payload.setdefault("native_provider", provider_name)
            # A detail GET is an observation, not a synchronization command.
            # In particular, never let page refreshes project a Codex session
            # into SQLite/timeline as a side effect.  Explicit /sync and the
            # background watcher remain the write owners.
            payload["native_sync_pending"] = bool(
                (task := self._native_background_tasks.get(key)) is not None
                and not task.done()
            )
            if native_sync_error:
                payload["native_sync_error"] = native_sync_error
            payload["presentation"] = build_native_session_presentation(
                payload,
                sync_error=native_sync_error,
            )
            return payload
        try:
            read_only_session = getattr(target, "peek_session", None)
            if read_only_session is None:
                # ``read_session`` is intentionally allowed to project/import
                # provider history for command and background-sync flows.  A
                # GET must never fall back to it: third-party providers that
                # have not opted into the read-only contract get a transparent
                # stale snapshot instead of an accidental database mutation.
                raise RuntimeError(
                    "native provider does not expose a read-only session snapshot"
                )
            result = await asyncio.wait_for(
                read_only_session(native_thread_id),
                timeout=self._native_sessions_timeout_seconds,
            )
            payload = _json_object(result)
            payload.setdefault("native_provider", provider_name)
            payload.setdefault("native_session_source", "daemon")
            payload["native_sync_pending"] = False
            payload["presentation"] = build_native_session_presentation(payload)
            return payload
        except KeyError:
            raise
        except (asyncio.TimeoutError, JsonRpcTimeout) as exc:
            native_sync_error = str(exc) or "native session sync timed out"
        except Exception as exc:
            native_sync_error = str(exc) or "native session sync failed"
        payload = {
            "native_thread_id": native_thread_id,
            "native_provider": provider_name,
            "native_session_source": "stub",
            "native_sync_error": native_sync_error,
            "native_sync_pending": False,
            "native_sync_recovery": "请使用同步操作重试；缓存恢复后会显示最后成功更新时间。",
            "thread": {"id": native_thread_id, "threadId": native_thread_id},
        }
        payload["presentation"] = build_native_session_presentation(
            payload,
            sync_error=native_sync_error,
        )
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
        if not fresh:
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
        session_payloads: list[dict[str, Any]] = []
        for session in sessions:
            item = _json_object(session)
            item.setdefault("native_provider", provider_name)
            item.setdefault("native_session_source", native_session_source)
            item["presentation"] = build_native_session_presentation(
                item,
                sync_error=native_sync_error,
            )
            session_payloads.append(item)
        payload: dict[str, Any] = {
            "sessions": session_payloads,
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
        initial_payload = await self._native_sessions_payload(
            provider_name,
            target,
            legacy_codex_controller=legacy_codex_controller,
            fresh=False,
            # The SSE hello snapshot is a read-only snapshot.  Refresh work
            # starts only after this payload has reached the client.
            schedule_refresh=False,
        )
        await _write_json_sse(writer, "native_sessions", initial_payload)
        self._ensure_native_sessions_watcher(
            provider_name,
            target,
            legacy_codex_controller=legacy_codex_controller,
        )
        # A cache-first listing needs one eventual daemon refresh even when
        # the local transcript index itself has not changed.  This is a
        # background worker, intentionally scheduled after the pure hello
        # snapshot rather than from GET/SSE initialization.
        self._schedule_native_sessions_refresh(
            provider_name,
            target,
            legacy_codex_controller=legacy_codex_controller,
        )

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

    async def _relay_task_detail(self, task_id: int) -> Any:
        """Return a Relay read model without advancing its lifecycle.

        HTTP GET, page rendering, and the first SSE snapshot are observations.
        They must never claim work, dispatch a role, or manufacture artifacts.
        The lifecycle worker below owns reconciliation instead.
        """
        if self._relay_service is None:
            raise KeyError(f"unknown relay task id: {task_id}")
        return self._relay_service.get_task_readonly(task_id)

    async def _handle_relay_ui_route(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        await handle_relay_ui_route(
            deps=RelayUiRouteDependencies(
                is_authorized=self._is_authorized,
                send_json=self._send_json,
                send_html=self._send_html,
                send_redirect=self._send_redirect,
                token_entry_page=_native_token_entry_page,
                native_workflows_page=_native_workflows_page,
                selected_workspace=_relay_selected_workspace,
                project_rows=_relay_project_rows,
                settings_href=_relay_settings_href,
                chat_home_page=_relay_chat_home_page,
                blocked_inbox_page=_relay_blocked_inbox_page,
                page_number=_relay_page_number,
                presentation_state_filter=_relay_presentation_state_filter,
                task_list_page=_relay_task_list_page,
                config_page=_relay_config_page,
                task_id_from_path=_relay_task_id_from_ui_path,
                task_detail_view=_relay_task_detail_view,
                task_detail_page=_relay_task_detail_page,
                task_detail=self._relay_task_detail,
            ),
            writer=writer,
            method=method,
            path=path,
            headers=headers,
            query=query,
            native_registry=self._native_registry,
            native_controller=self._native_controller,
            relay_service=self._relay_service,
            workspace_catalog=self._workspace_catalog,
            hub=self._hub,
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

        async def begin_mutation(
            operation: str,
            task_id: int | None,
            payload: dict[str, Any],
        ) -> tuple[RelayMutationStore, RelayMutationClaim | None] | None:
            """Acquire a durable mutation key or answer a replay immediately."""

            mutation_store = RelayMutationStore.from_relay_service(self._relay_service)
            try:
                claim = mutation_store.claim(
                    key=headers.get("idempotency-key", ""),
                    operation=operation,
                    task_id=task_id,
                    payload=payload,
                )
            except ValueError as exc:
                await self._send_json(writer, 400, {"error": str(exc)})
                return None
            if claim is None:
                return mutation_store, None
            if claim.is_replay:
                await self._send_json(
                    writer,
                    int(claim.response_status or 200),
                    dict(claim.response_payload or {}),
                )
                return None
            if not claim.should_execute:
                await self._send_json(
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
            await self._send_json(writer, status, payload)

        def abandon_mutation(
            mutation: tuple[RelayMutationStore, RelayMutationClaim | None],
        ) -> None:
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
                service=self._relay_service,
                send_json=self._send_json,
                read_request_json=self._read_request_json,
                begin_mutation=begin_mutation,
                finish_mutation=finish_mutation,
                abandon_mutation=abandon_mutation,
                presentation_state_filter=_relay_presentation_state_filter,
                page_number=_relay_page_number,
                safe_int=_safe_int,
                optional_nonempty_string=_optional_nonempty_string,
                safe_images=_safe_image_attachments,
                safe_files=_safe_relay_file_attachments,
                acceptance_criteria=_relay_acceptance_criteria_from_body,
                task_detail=self._relay_task_detail,
                maintenance_error_type=MaintenanceWindowError,
            ):
                return

            await handle_relay_task_route(
                deps=RelayTaskRouteDependencies(
                    send_json=self._send_json,
                    read_request_json=self._read_request_json,
                    task_api_parts=_relay_task_api_parts,
                    task_detail=self._relay_task_detail,
                    task_detail_json=_relay_task_detail_json_payload,
                    task_events=handle_relay_task_events_route,
                    safe_int=_safe_int,
                    safe_images=_safe_image_attachments,
                    safe_files=_safe_relay_file_attachments,
                    optional_nonempty_string=_optional_nonempty_string,
                    first_blocked_role=_relay_first_blocked_role,
                    schedule_dispatch=self._schedule_relay_dispatch,
                    schedule_reconcile=self._schedule_relay_task_reconcile,
                    reject_if_maintenance_frozen=self._reject_if_maintenance_frozen,
                    begin_mutation=begin_mutation,
                    finish_mutation=finish_mutation,
                    abandon_mutation=abandon_mutation,
                    maintenance_error_type=MaintenanceWindowError,
                ),
                normalized_path=normalized_path,
                method=method,
                query=query,
                reader=reader,
                writer=writer,
                headers=headers,
                service=self._relay_service,
                hub=self._hub,
                send_sse=_send_relay_sse,
            )
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
        await handle_native_agent_route(
            self,
            reader,
            writer,
            method,
            path,
            headers,
            query,
            provider_route_parts=_native_provider_route_parts,
            json_object=_json_object,
            optional_nonempty_string=_optional_nonempty_string,
            safe_images=_safe_image_attachments,
            permission_kwargs_from_body=_native_permission_kwargs_from_body,
            collaboration_kwargs_from_body=_codex_collaboration_kwargs_from_body,
            disabled_reason=_native_disabled_reason,
            login_ticket_ttl_seconds=_LOGIN_TICKET_TTL_SECONDS,
        )
        return

    async def _handle_workflow_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        await handle_workflow_route(
            reader,
            writer,
            method,
            path,
            headers,
            workflow_service=self._workflow_service,
            authorized=lambda request_writer, request_headers, *, require_token: self._is_authorized(
                request_writer,
                request_headers,
                query,
                require_token=require_token,
            ),
            require_token=(
                self._native_registry is not None or self._native_controller is not None
            ),
            send_json=self._send_json,
            read_request_json=self._read_request_json,
            json_object=_json_object,
        )

    async def _handle_council_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, list[str]],
    ) -> None:
        await handle_council_route(
            self,
            reader,
            writer,
            method,
            path,
            headers,
            query,
            run_id_from_path=_council_run_id_from_path,
            projects_payload=_council_projects_payload,
            packet_from_body=_council_packet_from_body,
            public_run_payload=_council_run_public_payload,
            provider_resolver=_ServerNativeProviderResolver,
        )

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

    def _schedule_relay_lifecycle_worker(self) -> bool:
        """Reconcile Relay lifecycle state outside request handling.

        Reconciliation may consume provider events and persist projections, so it
        is deliberately not reachable from any GET/SSE path.  The Relay store
        uses a durable completion-event claim, making repeated worker passes and
        a process restart safe.  A short initial delay also preserves the
        read-only contract for startup snapshots.
        """
        if self._relay_service is None:
            return False
        existing = self._relay_lifecycle_task
        if existing is not None and not existing.done():
            return True

        async def reconcile_loop() -> None:
            try:
                await asyncio.sleep(1.0)
                while True:
                    service = self._relay_service
                    if service is None:
                        return
                    try:
                        list_tasks = getattr(service, "list_tasks_readonly", service.list_tasks)
                        summaries = list_tasks()
                        runtime_store = getattr(self._hub, "_store", None)
                        for summary in summaries:
                            task_id = int(getattr(summary, "task_id", 0) or 0)
                            if not task_id:
                                continue
                            presentation = getattr(summary, "presentation", None)
                            state = str(
                                getattr(presentation, "state", "")
                                or getattr(summary, "status", "")
                                or ""
                            )
                            if state in {"completed", "interrupted", "failed"}:
                                continue
                            reconcile = getattr(
                                service,
                                "reconcile_task_lifecycle",
                                service.ensure_task_lifecycle_current,
                            )
                            try:
                                result = reconcile(task_id, runtime_store)
                            except TypeError:
                                # Compatibility with older Relay service
                                # implementations while rolling out this release.
                                result = reconcile(task_id)
                            if asyncio.iscoroutine(result):
                                await result
                        scan = getattr(service, "scan_active_native_runtime_events", None)
                        if runtime_store is not None and callable(scan):
                            result = scan(runtime_store)
                            if asyncio.iscoroutine(result):
                                await result
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._native_background_errors[("relay_lifecycle",)] = (
                            str(exc) or "relay lifecycle reconciliation failed"
                        )
                    else:
                        self._native_background_errors.pop(("relay_lifecycle",), None)
                    await asyncio.sleep(2.0)
            finally:
                if self._relay_lifecycle_task is task:
                    self._relay_lifecycle_task = None

        task = asyncio.create_task(reconcile_loop())
        self._relay_lifecycle_task = task
        return True

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

    def _schedule_relay_task_reconcile(self, task_id: int) -> bool:
        """Request an explicit lifecycle refresh without writing in the request.

        The task worker is the only boundary allowed to ingest provider runtime
        events.  This turns an inbox "refresh" into a durable-background
        operation instead of smuggling reconciliation into a page/API read.
        """

        if self._relay_service is None:
            return False

        async def reconcile() -> None:
            service = self._relay_service
            if service is None:
                return
            runtime_store = getattr(self._hub, "_store", None)
            result = service.reconcile_task_lifecycle(task_id, runtime_store)
            if asyncio.iscoroutine(result):
                await result

        task = asyncio.create_task(reconcile())
        self._relay_dispatch_tasks.add(task)
        task.add_done_callback(self._relay_dispatch_tasks.discard)
        return True

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
        sync_pending = self._native_transcript_task_running(
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
            # The replay above is the initial snapshot and deliberately has
            # no write/sync side effects.  Start a separately owned worker
            # only after that snapshot has reached the client.
            self._schedule_native_timeline_transcript_sync_if_needed(
                provider,
                native_thread_id,
            )
            self._schedule_native_timeline_lifecycle_reconcile(
                provider,
                native_thread_id,
            )
            while not writer.is_closing():
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=_NATIVE_TRANSCRIPT_WATCH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    self._schedule_native_timeline_transcript_sync_if_needed(
                        provider,
                        native_thread_id,
                    )
                    self._schedule_native_timeline_lifecycle_reconcile(
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
        sync_pending = self._native_transcript_task_running(
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
        run_state = self._native_timeline.latest_turn_run_state(
            provider_key,
            native_thread_id,
        )
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
            # Do not make the first SSE replay a write path.  Once the
            # client owns its read-only snapshot, the background worker may
            # import any newer transcript state and publish it through this
            # subscription.
            self._schedule_native_timeline_transcript_sync_if_needed(
                provider_key,
                native_thread_id,
            )
            self._schedule_native_timeline_lifecycle_reconcile(
                provider_key,
                native_thread_id,
            )
            while not writer.is_closing():
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=_NATIVE_TRANSCRIPT_WATCH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    self._schedule_native_timeline_transcript_sync_if_needed(
                        provider_key,
                        native_thread_id,
                    )
                    self._schedule_native_timeline_lifecycle_reconcile(
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
    task_id = int(getattr(getattr(detail, "task", None), "id", 0) or 0)
    presentation = getattr(detail, "presentation", None)
    if hasattr(presentation, "to_dict"):
        presentation_payload = presentation.to_dict()
    elif isinstance(presentation, dict):
        presentation_payload = dict(presentation)
    else:
        presentation_payload = {}
    if presentation_payload:
        # This is deliberately an unsequenced snapshot.  It gives every
        # subscriber the same read-only user-facing contract used by the
        # task page without advancing Last-Event-ID or manufacturing a
        # durable lifecycle event.
        await _write_relay_sse_payload(
            writer,
            event_id=None,
            event_type="presentation.snapshot",
            payload={
                "event_type": "presentation.snapshot",
                "task_id": task_id,
                "current_round_id": int(getattr(detail, "current_round_id", 0) or 0),
                "presentation": presentation_payload,
            },
        )
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
                    refreshed = relay_service.get_task_readonly(task_id)
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
    event_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    payload = _compact_relay_sse_payload(event_type, payload)
    # Ephemeral snapshots (for example an observed native frame or the
    # presentation projection) have no durable Relay sequence.  Omitting an
    # ``id`` is important: an empty SSE id resets EventSource's resume cursor.
    if event_id:
        writer.write(f"id: {event_id}\n".encode("utf-8"))
    writer.write(f"event: {event_type}\n".encode("utf-8"))
    writer.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
    await writer.drain()


async def _write_relay_worker_event(
    writer: asyncio.StreamWriter,
    *,
    task_id: int,
    role: str,
    worker_event: WorkerStreamEvent,
    relay_service: Any | None = None,
) -> int:
    """Write an observed native event without turning the SSE reader into a writer.

    Relay's lifecycle worker is the only component allowed to project native
    runtime events into ``relay_stream_events``.  An SSE subscription can see a
    hub event before that worker's next pass, so it may safely forward an
    ephemeral copy to the connected page.  Persisting it here used to make a
    page refresh (and even the initial snapshot) advance the database and
    occasionally race the lifecycle projector.
    """
    relay_event = _relay_worker_payload(task_id, role, worker_event)
    if relay_event is None:
        return 0
    event_type, payload = relay_event
    await _write_relay_sse_payload(
        writer,
        event_id="",
        event_type=event_type,
        payload=payload,
    )
    return 0


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
    return _render_native_app_manifest()


def _native_app_icon_svg() -> str:
    return _render_native_app_icon_svg()


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


def _relay_acceptance_criteria_from_body(body: dict[str, Any]) -> list[str]:
    """Normalize API/form criteria without accepting a hidden empty contract."""
    raw = body.get("acceptance_criteria", body.get("acceptanceCriteria", []))
    if isinstance(raw, str):
        values = raw.replace("\r\n", "\n").split("\n")
    elif isinstance(raw, list | tuple):
        values = raw
    else:
        values = []
    result: list[str] = []
    for value in values:
        criterion = str(value or "").strip()
        if criterion and criterion not in result:
            result.append(criterion)
    return result


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
    return _render_native_provider_display_name(provider)


def _native_provider_index_html(
    providers: list[dict[str, str]],
    *,
    access_token: str = "",
) -> str:
    return render_native_provider_index_page(
        providers,
        access_token=access_token,
        token_suffix=_token_suffix,
        replace_icons=_replace_html_icons,
    )


def _native_workflows_page(*, access_token: str = "") -> str:
    return render_native_workflows_page(
        access_token=access_token,
        token_suffix=_token_suffix,
        replace_icons=_replace_html_icons,
    )


def _relay_task_list_page(
    summaries: list[Any],
    *,
    providers: list[dict[str, str]],
    relay_config: dict[str, Any] | None = None,
    projects: list[Any] | None = None,
    selected_workspace: str = "",
    access_token: str = "",
    page: int = 1,
    total: int = 0,
    total_pages: int = 1,
    active_count: int = 0,
    state_counts: dict[str, int] | None = None,
    status_filter: str = "",
) -> str:
    token_suffix = _token_suffix(access_token)
    relay_config = relay_config or {}
    selected_workspace = str(selected_workspace or "")
    filters = [
        "running", "waiting_user", "waiting_approval", "blocked", "failed",
        "completed", "interrupted", "stale",
    ]
    counts = {status: int((state_counts or {}).get(status, 0) or 0) for status in filters}
    filter_html = "\n".join(
        f'<a class="relay-filter-chip{" active" if status == status_filter else ""}" '
        f'href="{escape(_relay_task_list_href(selected_workspace, access_token, 1, status=status))}">'
        f"{escape(_relay_task_status_label(status))} <span>{counts.get(status, 0)}</span></a>"
        for status in filters
    )
    pagination_html = _relay_task_pagination_html(
        current_page=page,
        total_pages=total_pages,
        selected_workspace=selected_workspace,
        access_token=access_token,
        status_filter=status_filter,
    )
    if summaries:
        task_list_html = "\n".join(
            _relay_task_card_html(summary, token_suffix) for summary in summaries
        )
    else:
        task_list_html = """
          <section class="relay-empty-state">
            <h2>还没有接力任务</h2>
            <p>创建一个大任务后，总工程师会先接收并调度架构、开发、测试和审计角色。</p>
            <p>当前筛选没有任务。可以新建任务，或调整状态筛选。</p>
          </section>
        """
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
          <a class="marvis-relay-icon-button" href="{escape(_relay_settings_href(selected_workspace, access_token))}" aria-label="Relay设置">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
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
            <span class="relay-muted">当前 {total} 个任务，{active_count} 个需要跟进</span>
          </div>
          <div class="relay-toolbar">
            <a class="relay-secondary" href="{escape(_relay_inbox_href(selected_workspace, access_token))}">待办收件箱</a>
            <a class="relay-primary" href="{escape(_relay_chat_href(selected_workspace, access_token))}">新建任务</a>
          </div>
          <div class="relay-filter-row" aria-label="relay task status filters">
            <a class="relay-filter-chip{" active" if not status_filter else ""}" href="{escape(_relay_task_list_href(selected_workspace, access_token, 1))}">全部 <span>{sum(counts.values())}</span></a>
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
</body>
</html>""")


def _relay_blocked_inbox_page(
    summaries: list[Any],
    *,
    selected_workspace: str = "",
    access_token: str = "",
) -> str:
    """Render the actionable Relay Inbox from the shared read-only projection.

    This intentionally groups *presentation* states rather than raw task
    statuses.  A user should not have to know whether a provider called a
    pause ``waiting`` or ``blocked`` in order to find the one next action.
    """

    token_suffix = _token_suffix(access_token)
    groups: list[tuple[str, str, tuple[str, ...]]] = [
        ("waiting_me", "等待我", ("waiting_user", "waiting_approval")),
        ("waiting_system", "等待系统", ("running",)),
        ("recovery", "需要恢复", ("blocked", "failed", "interrupted")),
        ("stale", "已陈旧", ("stale",)),
    ]
    grouped: dict[str, list[Any]] = {key: [] for key, _label, _states in groups}
    for summary in summaries:
        state = _relay_summary_presentation_state(summary)
        for key, _label, states in groups:
            if state in states:
                grouped[key].append(summary)
                break

    def card(summary: Any, bucket: str) -> str:
        presentation = _relay_summary_presentation(summary)
        state = _relay_summary_presentation_state(summary)
        task_id = int(getattr(summary, "task_id", 0) or 0)
        task_href = f"/native/workflows/relay/tasks/{task_id}{token_suffix}"
        evidence_href = _relay_task_view_href(task_id, access_token, "board")
        actor = presentation.get("current_actor")
        actor = actor if isinstance(actor, dict) else {}
        actor_label = str(actor.get("label") or "系统协调")
        reason = str(presentation.get("blocking_reason") or "").strip()
        next_action = str(presentation.get("next_action") or "查看任务状态。")
        freshness = presentation.get("freshness")
        freshness = freshness if isinstance(freshness, dict) else {}
        updated = _relay_activity_label(
            freshness.get("updated_at") or getattr(summary, "last_activity_at", "")
        )
        action_html = [
            f'<a class="relay-inbox-action secondary" href="{escape(task_href)}">补充信息</a>',
            f'<a class="relay-inbox-action secondary" href="{escape(evidence_href)}">查看证据</a>',
        ]
        if bucket == "recovery":
            if bool(freshness.get("recovery_required")):
                # The external provider may already have accepted an approval
                # action.  Do not offer a resume (which starts a fresh turn)
                # until the background reconciler has a durable receipt.
                action_html.insert(
                    0,
                    f'<button class="relay-inbox-action primary" type="button" '
                    f'data-relay-inbox-action="refresh" data-task-id="{task_id}">检查审批回执</button>',
                )
            else:
                action_html.insert(
                    0,
                    f'<button class="relay-inbox-action primary" type="button" '
                    f'data-relay-inbox-action="resume" data-task-id="{task_id}">恢复</button>',
                )
                action_html.append(
                    f'<button class="relay-inbox-action secondary" type="button" '
                    f'data-relay-inbox-action="archive" data-task-id="{task_id}">归档</button>',
                )
        elif bucket == "stale":
            action_html.insert(
                0,
                f'<button class="relay-inbox-action primary" type="button" '
                f'data-relay-inbox-action="refresh" data-task-id="{task_id}">恢复同步</button>',
            )
            action_html.append(
                f'<button class="relay-inbox-action secondary" type="button" '
                f'data-relay-inbox-action="archive" data-task-id="{task_id}">归档</button>',
            )
        return f"""
          <article class="relay-inbox-card" data-relay-inbox-card data-state="{escape(state)}">
            <div class="relay-inbox-card-head">
              <span class="relay-status-badge is-{escape(_relay_status_class_name(state))}">{escape(_relay_task_status_label(state))}</span>
              <span class="relay-muted">{escape(updated)}</span>
            </div>
            <h3>{escape(str(getattr(summary, 'title', '') or '未命名任务'))}</h3>
            <p><strong>当前责任：</strong>{escape(actor_label)}</p>
            <p><strong>唯一下一步：</strong>{escape(next_action)}</p>
            {f'<p class="relay-inbox-reason"><strong>原因：</strong>{escape(reason)}</p>' if reason else ''}
            <div class="relay-inbox-actions">{''.join(action_html)}</div>
            <p class="relay-inbox-status" data-relay-inbox-status role="status" aria-live="polite"></p>
          </article>
        """

    sections = []
    for key, label, _states in groups:
        items = grouped[key]
        cards = "".join(card(summary, key) for summary in items)
        if not cards:
            cards = '<p class="relay-inbox-empty">当前没有需要处理的任务。</p>'
        sections.append(
            f"""
            <section class="relay-inbox-group" aria-labelledby="relay-inbox-{key}">
              <div class="relay-inbox-group-head">
                <h2 id="relay-inbox-{key}">{escape(label)}</h2>
                <span>{len(items)}</span>
              </div>
              <div class="relay-inbox-cards">{cards}</div>
            </section>
            """
        )
    workspace_label = Path(selected_workspace).name or selected_workspace or "wlcodex"
    topbar_html = _marvis_relay_topbar(
        title="Marvis",
        subtitle=workspace_label,
        back_href=_relay_workspace_href(selected_workspace, access_token),
        right_html=(
            f'<a class="marvis-relay-icon-button" href="{escape(_relay_settings_href(selected_workspace, access_token))}" '
            'aria-label="Relay设置"><span class="marvis-relay-icon-list" aria-hidden="true"></span></a>'
        ),
    )
    bottom_nav_html = _marvis_relay_bottom_nav(
        "tasks", access_token=access_token, selected_workspace=selected_workspace
    )
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
{_relay_mobile_web_head("Relay 待办收件箱")}
  <style>
    html {{ background: var(--bg-canvas); }}
    body {{ margin: 0; color: var(--text-primary); background: transparent; }}
    main {{ width: min(920px, 100%); box-sizing: border-box; margin: 0 auto; padding: 18px; }}
    .relay-inbox-intro {{ display: grid; gap: 5px; margin-bottom: 18px; }}
    .relay-inbox-intro h2, .relay-inbox-group h2, .relay-inbox-card h3 {{ margin: 0; }}
    .relay-inbox-intro p, .relay-inbox-card p {{ margin: 0; line-height: 1.5; }}
    .relay-inbox-intro p, .relay-inbox-card p, .relay-inbox-empty {{ color: var(--text-muted); }}
    .relay-inbox-groups {{ display: grid; gap: 18px; }}
    .relay-inbox-group {{ display: grid; gap: 10px; }}
    .relay-inbox-group-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .relay-inbox-group-head span {{ min-width: 28px; min-height: 28px; display: grid; place-items: center; border-radius: 999px; background: rgba(88,166,255,.14); color: var(--text-primary); }}
    .relay-inbox-cards {{ display: grid; gap: 10px; }}
    .relay-inbox-card {{ display: grid; gap: 10px; padding: 14px; border: 1px solid var(--border-card); border-radius: 10px; background: var(--bg-surface); }}
    .relay-inbox-card-head, .relay-inbox-actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .relay-inbox-card-head {{ justify-content: space-between; }}
    .relay-inbox-actions {{ margin-top: 2px; }}
    .relay-inbox-action {{ min-height: 44px; box-sizing: border-box; display: inline-grid; place-items: center; border: 1px solid var(--border-subtle); border-radius: 7px; padding: 0 13px; color: var(--text-primary); background: transparent; text-decoration: none; font: inherit; cursor: pointer; }}
    .relay-inbox-action.primary {{ border-color: var(--color-link); background: rgba(88,166,255,.12); }}
    .relay-inbox-action[disabled] {{ opacity: .58; cursor: wait; }}
    .relay-inbox-status {{ min-height: 1.3em; font-size: 13px; }}
    .relay-inbox-status.is-error {{ color: #d83a3a; }}
    @media (max-width: 760px) {{ main {{ padding: 12px; }} .relay-inbox-action {{ flex: 1 1 130px; }} }}
  </style>
</head>
<body data-marvis-relay-view="inbox">
  <div class="marvis-relay-phone">
    {topbar_html}
    <main>
      <section class="relay-inbox-intro" aria-labelledby="relay-inbox-title">
        <h2 id="relay-inbox-title">待办收件箱</h2>
        <p>按当前可见语义聚合。每张卡只显示一个下一步，不以底层状态机要求你猜测。</p>
      </section>
      <div class="relay-inbox-groups">{''.join(sections)}</div>
    </main>
    <nav class="marvis-relay-bottom-nav" aria-label="Marvis relay navigation">{bottom_nav_html}</nav>
  </div>
  <script>
    const TOKEN_SUFFIX = {json.dumps(token_suffix)};
    const makeIdempotencyKey = () => crypto.randomUUID ? crypto.randomUUID() : `${{Date.now()}}-${{Math.random()}}`;
    document.querySelectorAll("[data-relay-inbox-action]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        const action = button.dataset.relayInboxAction || "";
        const taskId = button.dataset.taskId || "";
        if (!taskId || !action) return;
        const card = button.closest("[data-relay-inbox-card]");
        const status = card?.querySelector("[data-relay-inbox-status]");
        const endpoint = action === "archive" ? "archive" : action === "refresh" ? "refresh" : "resume";
        const key = button.dataset.idempotencyKey || makeIdempotencyKey();
        button.dataset.idempotencyKey = key;
        button.disabled = true;
        if (status) {{ status.textContent = action === "archive" ? "正在归档…" : "正在处理…"; status.classList.remove("is-error"); }}
        try {{
          const response = await fetch(`/api/relay/tasks/${{encodeURIComponent(taskId)}}/${{endpoint}}${{TOKEN_SUFFIX}}`, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json", "Idempotency-Key": key }},
            body: JSON.stringify(action === "resume" ? {{ force: true }} : {{}}),
          }});
          const payload = await response.json().catch(() => ({{}}));
          if (!response.ok) throw new Error(payload.error || "操作失败，请重试。");
          if (action === "archive") {{ card?.remove(); return; }}
          if (status) status.textContent = action === "refresh" ? "已请求同步，请等待新状态。" : "已恢复任务。";
        }} catch (error) {{
          if (status) {{ status.textContent = error?.message || "操作失败，请重试。"; status.classList.add("is-error"); }}
        }} finally {{
          button.disabled = false;
        }}
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
    return render_relay_chat_home_page(
        selected_workspace=selected_workspace,
        access_token=access_token,
        token_suffix=_token_suffix(access_token),
        document_head=_relay_mobile_web_head("Marvis 对话"),
        replace_icons=_replace_html_icons,
    )


def _relay_config_page(
    *,
    providers: list[dict[str, str]],
    relay_config: dict[str, Any] | None = None,
    selected_workspace: str = "",
    access_token: str = "",
) -> str:
    return render_relay_config_page(
        providers=providers,
        relay_config=relay_config,
        selected_workspace=selected_workspace,
        access_token=access_token,
        token_suffix=_token_suffix(access_token),
        provider_display_name=_native_provider_display_name,
        replace_icons=_replace_html_icons,
    )


def _relay_page_number(raw_page: str) -> int:
    try:
        return max(1, int(raw_page))
    except (TypeError, ValueError):
        return 1


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
        const idempotencyKey = target.dataset.idempotencyKey
          || (crypto.randomUUID ? crypto.randomUUID() : `${{Date.now()}}-${{Math.random()}}`);
        target.dataset.idempotencyKey = idempotencyKey;
        target.setAttribute("aria-busy", "true");
        try {{
          const response = await fetch(`/api/relay/config${{TOKEN_SUFFIX}}`, {{
            method: "POST",
            headers: {{"Content-Type": "application/json", "Idempotency-Key": idempotencyKey}},
            body: JSON.stringify({{assignments: nextAssignments}}),
          }});
          if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
          const payload = await response.json();
          assignments = payload.assignments || nextAssignments;
          delete target.dataset.idempotencyKey;
          updatePersonaProvider(activeRole, assignments[activeRole]);
          if (title) title.textContent = personas[activeRole]?.title || "Relay Agent";
          renderProviderOptions();
        }} catch (error) {{
          if (modelStatus) modelStatus.textContent = "保存失败，请重试";
        }} finally {{
          target.removeAttribute("aria-busy");
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
      // Token totals are supplementary read-model data.  Do not keep a
      // second-level poll alive behind a healthy Relay event stream or while
      // the page is hidden: the last confirmed value remains useful until a
      // visible refresh succeeds.
      const TOKEN_STATS_REFRESH_INTERVAL_MS = 30000;
      let tokenStatsRefreshTimer = null;
      const stopTokenStatsRefresh = () => {{
        if (!tokenStatsRefreshTimer) return;
        window.clearInterval(tokenStatsRefreshTimer);
        tokenStatsRefreshTimer = null;
      }};
      const startTokenStatsRefresh = () => {{
        if (document.visibilityState === "hidden" || tokenStatsRefreshTimer) return;
        tokenStatsRefreshTimer = window.setInterval(
          refreshTokenStats,
          TOKEN_STATS_REFRESH_INTERVAL_MS,
        );
      }};
      document.addEventListener("visibilitychange", () => {{
        if (document.visibilityState === "hidden") {{
          stopTokenStatsRefresh();
          return;
        }}
        refreshTokenStats();
        startTokenStatsRefresh();
      }});
      window.addEventListener("pagehide", stopTokenStatsRefresh);
      refreshTokenStats();
      startTokenStatsRefresh();
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
    <form class="marvis-relay-composer" data-marvis-followup-composer data-task-status-value="{escape(task_status)}" data-current-round-id="{int(current_round_id)}" method="post" action="/api/relay/tasks/{task_id}/message" onsubmit="return false">
      <button class="marvis-relay-plus" type="button" aria-label="添加" data-marvis-attach-open>+</button>
      <textarea name="text" placeholder="{escape(placeholder)}" aria-label="继续补充给总工程师"></textarea>
      <button class="marvis-relay-submit" type="submit" aria-label="发送补充" data-marvis-submit>
        <span class="marvis-relay-submit-arrow" aria-hidden="true">↑</span>
      </button>
      <div class="marvis-relay-composer-attachments" data-marvis-attachment-strip hidden></div>
      <button class="marvis-relay-interrupt" type="button" data-marvis-interrupt-button data-interrupt-url="/api/relay/tasks/{task_id}/interrupt">中断当前执行</button>
      <p class="marvis-relay-mutation-status" data-relay-mutation-status role="status" aria-live="polite"></p>
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
    <section class="marvis-relay-confirmation-page" data-marvis-confirmation-page data-round-id="{round_id}" data-artifact-id="{artifact_id}" role="dialog" aria-modal="true" aria-label="{escape(title)}" hidden>
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
    </section>
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
    <section class="marvis-work-log" data-marvis-work-log data-marvis-work-log-max-event-id="{max(0, int(max_event_id))}" aria-label="工作日志" hidden>
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
    return _render_work_log_segment(
        segment,
        index=index,
        render_avatar=_marvis_relay_avatar_html,
    )


def _marvis_relay_work_log_entry_html(entry: WorkLogEntry) -> str:
    return _render_work_log_entry(entry)


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


def _relay_routing_route_label(route: str) -> str:
    return routing_route_label(route)


def _relay_humanize_display_text(text: str, *, english_fallback: str = "") -> str:
    return humanize_display_text(text, english_fallback=english_fallback)


def _relay_text_needs_chinese_fallback(text: str) -> bool:
    return text_needs_chinese_fallback(text)


def _relay_routing_risk_label(risk: str) -> str:
    return routing_risk_label(risk)


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


def _relay_task_detail_json_payload(detail: Any, relay_service: Any) -> dict[str, Any]:
    """Serialize an already-read Relay detail without lifecycle repair.

    ``detail`` is deliberately obtained through ``get_task_readonly`` by the
    HTTP handler.  Building the auxiliary Marvis graph from the service used
    to call its mutating compatibility getter a second time, which made an
    otherwise harmless detail GET backfill old lifecycle rows.  The graph is
    a pure view of this detail, so keep it on the same read-only boundary.

    ``relay_service`` remains an argument for source compatibility with
    existing internal callers; it must not be consulted while rendering.
    """
    del relay_service
    payload = detail.to_dict() if hasattr(detail, "to_dict") else dict(detail)
    round_id = int(getattr(detail, "current_round_id", 0) or payload.get("current_round_id") or 0)
    state = build_marvis_relay_state(detail, round_id=round_id or None)
    payload["marvis_relay_state"] = state.to_json_dict()
    return payload


def _relay_task_detail_page(
    detail: Any,
    *,
    access_token: str = "",
    view: str = "conversation",
    events: list[Any] | tuple[Any, ...] | None = None,
    hub: WorkerLiveStreamHub | None = None,
    token_stats: dict[str, Any] | None = None,
) -> str:
    return render_relay_task_detail_page(
        detail,
        access_token=access_token,
        view=view,
        events=events,
        hub=hub,
        token_stats=token_stats,
        helpers={
            "_relay_task_detail_view": _relay_task_detail_view,
            "_token_suffix": _token_suffix,
            "_relay_latest_event_sequence": _relay_latest_event_sequence,
            "_relay_task_events_suffix": _relay_task_events_suffix,
            "_relay_role_canonical_payloads_by_role": _relay_role_canonical_payloads_by_role,
            "_marvis_relay_conversation_html": _marvis_relay_conversation_html,
            "_relay_role_canonical_payload_sequence": _relay_role_canonical_payload_sequence,
            "_relay_workspace_href": _relay_workspace_href,
            "_marvis_relay_topbar": _marvis_relay_topbar,
            "_relay_settings_href": _relay_settings_href,
            "_marvis_relay_bottom_nav": _marvis_relay_bottom_nav,
            "_marvis_token_int": _marvis_token_int,
            "_format_marvis_relay_token_count": _format_marvis_relay_token_count,
            "_marvis_relay_work_log_html": _marvis_relay_work_log_html,
            "_marvis_relay_work_log_body_html": _marvis_relay_work_log_body_html,
            "_marvis_relay_max_event_id_from_events": _marvis_relay_max_event_id_from_events,
            "_marvis_relay_plan_control_html": _marvis_relay_plan_control_html,
            "_marvis_relay_followup_composer": _marvis_relay_followup_composer,
            "_replace_html_icons": _replace_html_icons,
            "_relay_mobile_web_head": _relay_mobile_web_head,
            "_marvis_relay_attachment_script": _marvis_relay_attachment_script,
            "RELAY_ROLE_IDS": RELAY_ROLE_IDS,
            "_relay_role_label": _relay_role_label,
            "_marvis_relay_public_role": _marvis_relay_public_role,
            "_marvis_relay_handoff_role_label": _marvis_relay_handoff_role_label,
            "_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS": _MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS,
            "_MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS": _MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS,
            "_native_provider_display_name": _native_provider_display_name,
            "_NATIVE_APP_HEAD": _NATIVE_APP_HEAD,
            "_native_permission_presets": _native_permission_presets,
            "_codex_plugin_menu_items": _codex_plugin_menu_items,
            "_ICONS_JS_LITERAL": _ICONS_JS_LITERAL,
        },
    )


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
    for event in project_relay_rows_to_marvis_typed_events(rows):
        if str(event.get("event_type") or "") not in {
            "marvis.chat.message",
            "marvis.chat.handoff",
            "marvis.interrupt.requested",
        }:
            continue
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        row = metadata.get("relay_row")
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
    waiting_html = (
        _marvis_relay_waiting_message_html()
        if any(str(getattr(job, "status", "") or "") in {"queued", "streaming"} for job in role_jobs)
        else ""
    )
    return render_conversation_rows(
        rows,
        current_round_id=_relay_current_round_id_from_artifacts(artifacts),
        empty_html=_marvis_relay_empty_conversation_html(),
        waiting_html=waiting_html,
        render_handoff=_marvis_relay_handoff_html,
        render_message=_marvis_relay_message_html,
        handoff_pair=_marvis_relay_handoff_pair,
        handoff_identity=_marvis_relay_handoff_identity,
    )


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
    return render_native_codex_page(
        provider_name,
        theme=theme,
        helpers={
            "_native_provider_display_name": _native_provider_display_name,
            "_replace_html_icons": _replace_html_icons,
            "_NATIVE_APP_HEAD": _NATIVE_APP_HEAD,
            "turn_semantics_json": turn_semantics_json,
            "_native_permission_presets": _native_permission_presets,
            "_codex_plugin_menu_items": _codex_plugin_menu_items,
            "_ICONS_JS_LITERAL": _ICONS_JS_LITERAL,
        },
    )

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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>__SAFE_TITLE__</title>
__NATIVE_APP_HEAD__
  <link rel="stylesheet" href="/static/native_app_bundle.css">
  <script src="/static/surface_runtime.js?v=20260710-semantic-closure"></script>
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
    .native-presentation-notice { display: flex; align-items: center; gap: 12px; min-height: 44px; margin: 2px 0 14px; padding: 10px 12px; border: 1px solid rgba(245,196,81,.52); border-radius: 12px; background: rgba(103,78,12,.27); color: #fff4c4; font-size: 13px; line-height: 1.42; }
    .native-presentation-notice[hidden] { display: none; }
    .native-presentation-copy { min-width: 0; flex: 1 1 auto; }
    .native-presentation-retry { flex: 0 0 auto; min-height: 44px; padding: 0 12px; border: 1px solid rgba(245,196,81,.72); border-radius: 10px; background: #2d250f; color: #fff4c4; font-size: 13px; font-weight: var(--weight-extrabold); }
    .native-presentation-retry:disabled { opacity: .64; }
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
    .transcript-body .markdown-table-wrap { max-width: 100%; margin: 0 0 14px; overflow-x: auto; scrollbar-width: thin; scrollbar-color: #383c46 transparent; }
    .transcript-body table { width: max-content; min-width: min(100%, 520px); border-collapse: collapse; color: var(--btn-primary-bg); font-size: var(--native-ui-font-size); line-height: 1.35; }
    .transcript-body th, .transcript-body td { padding: 9px 12px; border-bottom: 1px solid rgba(255,255,255,0.08); white-space: nowrap; text-align: right; font-variant-numeric: tabular-nums; }
    .transcript-body th:first-child, .transcript-body td:first-child { text-align: left; }
    .transcript-body th { color: var(--text-heading); font-weight: var(--weight-extrabold); }
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
    .plan-execution-bar { position: fixed; left: 14px; right: 14px; bottom: calc(var(--codex-dock-height, 150px) + 12px + env(safe-area-inset-bottom)); z-index: 10; display: grid; overflow: hidden; padding: 14px 16px 10px; border: 1px solid rgba(255,255,255,.08); border-radius: 18px; background: #242426; color: #f4f4f5; box-shadow: 0 18px 44px rgba(0,0,0,.55); }
    .plan-execution-bar[hidden] { display: none; }
    body.has-plan-execution-bar main { padding-bottom: calc(var(--codex-dock-height, 150px) + 210px + env(safe-area-inset-bottom)); }
    .plan-execution-title { display: block; padding: 0 0 13px; color: #f4f4f5; font-size: 15px; line-height: 1.35; font-weight: var(--weight-medium); }
    .plan-execution-row { width: 100%; min-height: 48px; display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: center; gap: 9px; padding: 0; border: 0; border-top: 1px solid rgba(255,255,255,.08); border-radius: 0; background: transparent; color: #f4f4f5; text-align: left; font-size: 15px; line-height: 1.3; font-weight: var(--weight-medium); }
    .plan-execution-row:disabled { opacity: .48; }
    .plan-execution-confirm svg, .plan-execution-revise svg { width: 18px; height: 18px; stroke-width: 2.2; }
    .plan-execution-icon { width: 28px; min-height: 28px; display: grid; place-items: center; color: #e4e4e7; }
    .plan-execution-confirm { min-height: 48px; margin: 0 0 8px; padding: 0 14px; border-top: 0; border-radius: 14px; background: #f4f4f5; color: #111; font-weight: var(--weight-extrabold); }
    .plan-execution-confirm .plan-execution-icon { color: #111; }
    .plan-execution-secondary { grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,.08); }
    .plan-execution-revise { min-width: 0; min-height: 48px; display: grid; grid-template-columns: 28px minmax(0, 1fr); align-items: center; gap: 9px; padding: 0; border: 0; border-radius: 0; background: transparent; color: #9ca3af; text-align: left; font-size: 15px; line-height: 1.3; }
    .plan-execution-revise .plan-execution-icon { color: #a1a1aa; }
    .plan-execution-revise:disabled { opacity: .48; }
    .plan-execution-placeholder { min-width: 0; color: #9ca3af; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .plan-execution-skip { min-height: 34px; padding: 0 14px; border: 1px solid rgba(255,255,255,.1); border-radius: 17px; background: #f4f4f5; color: #111; font-size: 14px; font-weight: var(--weight-bold); }
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
    .model-catalog-notice { display: flex; gap: 10px; align-items: center; min-width: 0; padding: 9px 10px; border: 1px solid rgba(245,196,81,.52); border-radius: 12px; background: rgba(103,78,12,.27); color: #fff4c4; font-size: 12px; line-height: 1.4; }
    .model-catalog-notice[hidden] { display: none; }
    .model-catalog-notice-copy { min-width: 0; flex: 1 1 auto; }
    .model-catalog-retry { flex: 0 0 auto; min-height: 44px; padding: 0 11px; border: 1px solid rgba(245,196,81,.72); border-radius: 10px; background: #2d250f; color: #fff4c4; font-size: 12px; font-weight: var(--weight-extrabold); }
    .model-catalog-retry:disabled { opacity: .64; }
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
      .plan-execution-bar { left: 50%; right: auto; transform: translateX(-50%); width: min(748px, calc(100% - 28px)); }
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
        <div class="context-info-row"><span class="context-info-label">会话状态:</span><span class="context-info-value" id="contextPresentationStateValue">等待同步</span></div>
        <div class="context-info-row"><span class="context-info-label">同步来源:</span><span class="context-info-value" id="contextFreshnessValue">等待同步</span></div>
        <div class="context-info-row"><span class="context-info-label">下一步:</span><span class="context-info-value" id="contextNextActionValue">等待同步</span></div>
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
      <section class="native-presentation-notice" id="nativePresentationNotice" role="status" aria-live="polite" hidden>
        <span class="native-presentation-copy" id="nativePresentationNoticeText"></span>
        <button class="native-presentation-retry" id="nativePresentationRetry" type="button" hidden>重新同步</button>
      </section>
      <span class="event-cursor" id="cursor" hidden></span>
      <button class="history-fold" id="historyFold" hidden>更早的消息</button>
      <section class="codex-transcript" id="events"><div class="empty" id="empty">输入消息开始新会话</div></section>
      <div class="composer-activity" id="composerActivity" aria-hidden="true">
        <span class="composer-activity-dot"></span>
        <span class="composer-activity-dot"></span>
        <span class="composer-activity-dot"></span>
      </div>
    </main>
    <section class="plan-execution-bar" id="planExecutionBar" aria-label="执行计划" hidden>
      <span class="plan-execution-title">是否执行此计划?</span>
      <button class="plan-execution-row plan-execution-confirm" id="planExecutionConfirm" type="button">
        <span class="plan-execution-icon">✓</span>
        <span>是，执行此计划</span>
      </button>
      <div class="plan-execution-row plan-execution-secondary">
        <button class="plan-execution-revise" id="planExecutionRevise" type="button">
          <span class="plan-execution-icon">✎</span>
          <span class="plan-execution-placeholder">否，请说明修改内容</span>
        </button>
        <button class="plan-execution-skip" id="planExecutionSkip" type="button">跳过</button>
      </div>
    </section>
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
      <div class="model-catalog-notice" id="modelCatalogNotice" role="status" aria-live="polite" hidden>
        <span class="model-catalog-notice-copy" id="modelCatalogNoticeText"></span>
        <button class="model-catalog-retry" id="modelCatalogRetry" type="button">重试同步</button>
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
    <button class="new-messages-notice" id="newMessagesNotice" type="button" hidden>
      有新消息，查看
    </button>
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
    const newMessagesNotice = document.getElementById("newMessagesNotice");
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
    const contextPresentationStateValue = document.getElementById("contextPresentationStateValue");
    const contextFreshnessValue = document.getElementById("contextFreshnessValue");
    const contextNextActionValue = document.getElementById("contextNextActionValue");
    const contextThreadCopyButton = document.getElementById("contextThreadCopyButton");
    const sessionActionTitle = document.getElementById("sessionActionTitle");
    const copySessionIdButton = document.getElementById("copySessionIdButton");
    const uiFontSizeInput = document.getElementById("uiFontSizeInput");
    const codeFontSizeInput = document.getElementById("codeFontSizeInput");
    const sessionFloat = document.getElementById("sessionFloat");
    const sessionFloatTitle = document.getElementById("sessionFloatTitle");
    const sessionFloatMeta = document.getElementById("sessionFloatMeta");
    const sessionFloatState = document.getElementById("sessionFloatState");
    const nativePresentationNotice = document.getElementById("nativePresentationNotice");
    const nativePresentationNoticeText = document.getElementById("nativePresentationNoticeText");
    const nativePresentationRetry = document.getElementById("nativePresentationRetry");
    const historyFold = document.getElementById("historyFold");
    const composerActivity = document.getElementById("composerActivity");
    const inputDock = document.querySelector(".codex-input-dock");
    const params = new URLSearchParams(location.search);
    const viewportDebug = document.getElementById("viewportDebug");
    const debugViewport = params.get("debug_viewport") === "1";
    const token = params.get("token") || "";
    const PROVIDER = __PROVIDER_JSON__;
__ICONS_JS__
    const NATIVE_TURN_SEMANTICS = __NATIVE_TURN_SEMANTICS_JSON__;
    const PROVIDER_LABEL = __PROVIDER_LABEL_JSON__;
    const API_BASE = __API_BASE_JSON__;
    const SUPPORTS_PLAN_MODE = __SUPPORTS_PLAN_MODE_JSON__;
    const SUPPORTS_PLUGIN_MENU = __SUPPORTS_PLUGIN_MENU_JSON__;
    const USES_CLAUDE_PLAN_PERMISSION_MODE = __USES_CLAUDE_PLAN_PERMISSION_MODE_JSON__;
    const timelineScroller = window.WLCodexSurfaceRuntime.createConditionalScroller({
      container: window,
      threshold: 96,
      notice: newMessagesNotice,
    });
    function scrollToBottom(force = false) {
      return timelineScroller.scrollToBottom(force);
    }
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
    const NATIVE_TRANSCRIPT_FALLBACK_INTERVAL_MS = 30000;
    const terminalTranscriptSyncTurns = new Set();
    const authHeaders = token ? {"Authorization": "Bearer " + token} : {};
    const promptInput = document.getElementById("prompt");
    const continueButton = document.getElementById("continue");
    const steerButton = document.getElementById("steer");
    const interruptButton = document.getElementById("interrupt");
    const dockActions = document.querySelector(".dock-actions");
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
    const modelCatalogNotice = document.getElementById("modelCatalogNotice");
    const modelCatalogNoticeText = document.getElementById("modelCatalogNoticeText");
    const modelCatalogRetry = document.getElementById("modelCatalogRetry");
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
    const nativeAppShell = planPage?.previousElementSibling;
    const planPageClose = document.getElementById("planPageClose");
    const planPageDownload = document.getElementById("planPageDownload");
    const planPageCopy = document.getElementById("planPageCopy");
    const planPageTitle = document.getElementById("planPageTitle");
    const planPageSummary = document.getElementById("planPageSummary");
    const planPageBody = document.getElementById("planPageBody");
    const planPageExecute = document.getElementById("planPageExecute");
    const planExecutionBar = document.getElementById("planExecutionBar");
    const planExecutionConfirm = document.getElementById("planExecutionConfirm");
    const planExecutionRevise = document.getElementById("planExecutionRevise");
    const planExecutionSkip = document.getElementById("planExecutionSkip");
    const planPageFocusableSelector = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    let planPagePreviouslyFocused = null;
    let planPagePreviousOverflow = "";
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
    const turnStatus = {active: false, turnId: "", label: "连接会话", tone: "neutral", terminal: false};
    let modelCatalog = [];
    let modelCatalogAvailable = false;
    let modelCatalogLoading = false;
    let modelCatalogUnavailableReason = "正在同步模型目录";
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
    function nativeErrorMessage(message, code = "") {
      const text = String(message || "");
      if (String(code || "") === "native_thread_not_found" || /^thread not found:\\s*\\S+\\s*$/i.test(text)) {
        return "历史会话已不在当前 Codex 后台中，无法继续发送。请回到会话列表选择可恢复会话，或新建会话继续。";
      }
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
          const error = new Error(nativeErrorMessage(
            body.error || response.statusText,
            body.native_error_code || ""
          ));
          error.code = body.native_error_code || body.code || response.status;
          error.httpStatus = response.status;
          throw error;
        }
        return response.json().catch(() => ({}));
      } catch (error) {
        if (error && error.name === "AbortError") throw new Error("请求超时");
        throw error;
      } finally {
        if (timeoutId) window.clearTimeout(timeoutId);
      }
    }
    function nativeMutationKey(operation) {
      const runtime = window.WLCodexSurfaceRuntime;
      if (runtime && typeof runtime.mutationKey === "function") {
        return runtime.mutationKey("native-" + operation);
      }
      return "native-" + operation + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    }
    async function withNativeSoftTimeout(promise, message, delayMs = 12000) {
      let settled = false;
      let timedOut = false;
      let timer = null;
      const timeoutPromise = new Promise(resolve => {
        timer = window.setTimeout(() => {
          if (!settled) {
            timedOut = true;
            requestTurnStatusUpdate(message, "busy");
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
        body: JSON.stringify(body),
        headers: {"Idempotency-Key": nativeMutationKey("session-" + action)}
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
        const reason = error.message || "无法读取原生会话状态";
        updateNativeSessionInfo({
          status: reason,
          presentation: {
            state: "stale",
            freshness: {
              source: "unavailable",
              updated_at: "",
              is_stale: true,
              reason,
            },
            blocking_reason: reason,
            next_action: "重新同步",
            allowed_actions: ["refresh"],
          },
        });
      }
    }
    function updateNativeSessionInfo(session) {
      currentSessionInfo = {...currentSessionInfo, ...(session || {})};
      updateNativeHeaderContext();
      applyNativeSessionPresentation(currentSessionInfo);
    }
    function nativePresentationStateLabel(value) {
      const labels = {
        running: "正在执行",
        waiting_user: "等待你的输入",
        waiting_approval: "等待审批",
        blocked: "已阻塞",
        completed: "已完成",
        interrupted: "已中断",
        failed: "执行失败",
        stale: "状态已陈旧",
      };
      return labels[String(value || "").trim()] || "等待同步";
    }
    function nativePresentationSourceLabel(value) {
      const labels = {
        daemon: "原生实时状态",
        "app-server": "原生实时状态",
        live: "原生实时状态",
        native: "原生实时状态",
        cache: "缓存快照",
        stub: "尚未取得会话快照",
        unavailable: "连接失败",
      };
      return labels[String(value || "").trim().toLowerCase()] || "等待同步";
    }
    function nativePresentationUpdatedAt(value) {
      if (value === undefined || value === null || value === "") return "未知";
      const numeric = Number(value);
      const candidate = Number.isFinite(numeric) && String(value).trim() !== ""
        ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric)
        : new Date(String(value));
      if (Number.isNaN(candidate.getTime())) return String(value);
      return candidate.toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
    }
    function applyNativeSessionPresentation(session) {
      const presentation = (session && session.presentation) || null;
      if (!presentation || typeof presentation !== "object") {
        writeCompactText(contextPresentationStateValue, "等待同步");
        writeCompactText(contextFreshnessValue, "等待同步");
        writeCompactText(contextNextActionValue, "等待同步");
        nativePresentationNotice.hidden = true;
        nativePresentationRetry.hidden = true;
        return;
      }
      const freshness = presentation.freshness || {};
      const stale = Boolean(freshness.is_stale || freshness.stale);
      const stateLabel = nativePresentationStateLabel(presentation.state);
      const sourceLabel = nativePresentationSourceLabel(freshness.source);
      const updatedLabel = nativePresentationUpdatedAt(freshness.updated_at);
      const nextAction = String(presentation.next_action || "等待同步").trim() || "等待同步";
      const reason = String(
        freshness.reason || presentation.blocking_reason || ""
      ).trim();
      writeCompactText(contextPresentationStateValue, stateLabel);
      writeCompactText(contextFreshnessValue, `${sourceLabel} · ${updatedLabel}`);
      writeCompactText(contextNextActionValue, nextAction);
      const shouldShowNotice = stale || Boolean(reason);
      nativePresentationNotice.hidden = !shouldShowNotice;
      nativePresentationRetry.hidden = !stale;
      if (shouldShowNotice) {
        const explanation = reason || "原生状态尚未确认，请先重新同步后再决定下一步。";
        nativePresentationNoticeText.textContent = `${sourceLabel}，最后更新 ${updatedLabel}。${explanation} 下一步：${nextAction}`;
      }
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
    function usableModelCatalogEntries(rawModels) {
      return (Array.isArray(rawModels) ? rawModels : []).filter(model => {
        const modelId = String((model && (model.model || model.id)) || "").trim();
        return Boolean(modelId);
      });
    }

    function showModelCatalogUnavailable(reason) {
      modelCatalogUnavailableReason = String(reason || "未能同步可用模型").trim() || "未能同步可用模型";
      modelCatalogNoticeText.textContent = `模型目录不可用：${modelCatalogUnavailableReason}。当前会话仍可继续，但不会发送本地保存的模型选择；请检查 ${PROVIDER_LABEL} 二进制和 app-server 配置后重试。`;
      modelCatalogNotice.hidden = false;
      modelCatalogRetry.disabled = modelCatalogLoading;
    }

    function setModelCatalogUnavailable(reason) {
      modelCatalogAvailable = false;
      modelCatalog = [];
      showModelCatalogUnavailable(reason);
      modelSelector.innerHTML = '<option value="">模型目录不可用</option>';
      reasoningSelector.innerHTML = '<option value="">推理</option>';
      serviceTierSelector.innerHTML = '<option value="">速度</option>';
      modelSelector.disabled = true;
      reasoningSelector.disabled = true;
      serviceTierSelector.disabled = true;
      modelOptions.innerHTML = "";
      reasoningOptions.innerHTML = "";
      serviceTierOptions.innerHTML = "";
      modelOptions.hidden = true;
      reasoningOptions.hidden = true;
      serviceTierOptions.hidden = true;
      modelSettingsDirty = false;
      updateSettingVisibility();
      updateSettingSummary();
    }

    function setModelCatalogAvailable(models) {
      modelCatalog = usableModelCatalogEntries(models);
      modelCatalogAvailable = modelCatalog.length > 0;
      if (!modelCatalogAvailable) {
        setModelCatalogUnavailable("提供方未返回可用模型");
        return false;
      }
      modelCatalogNotice.hidden = true;
      modelCatalogRetry.disabled = false;
      modelSelector.disabled = false;
      reasoningSelector.disabled = false;
      serviceTierSelector.disabled = false;
      renderModelSettings();
      return true;
    }

    async function loadModelCatalog() {
      if (modelCatalogLoading) return false;
      modelCatalogLoading = true;
      modelCatalogRetry.disabled = true;
      try {
        const result = await api(`${API_BASE}/models`);
        return setModelCatalogAvailable(result.models);
      } catch (error) {
        setModelCatalogUnavailable(error.message || "未能同步可用模型");
        return false;
      } finally {
        modelCatalogLoading = false;
        modelCatalogRetry.disabled = false;
        updateComposerDisabled();
      }
    }
    function renderModelSettings() {
      if (!modelCatalogAvailable) {
        setModelCatalogUnavailable(modelCatalogUnavailableReason);
        return;
      }
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
      if (!modelCatalogAvailable) return null;
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
      if (!modelCatalogAvailable) {
        return normalizeModelSettings({version: MODEL_SETTINGS_STORAGE_VERSION});
      }
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
          body: JSON.stringify({action}),
          headers: {"Idempotency-Key": nativeMutationKey("approval-" + requestId)}
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
      requestTurnStatusUpdate(stateNode.textContent, state === "failed" ? "failed" : "busy");
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
    planPage.addEventListener("click", event => {
      if (event.target === planPage) closePlanPage();
    });
    planPage.addEventListener("keydown", event => {
      if (event.key !== "Tab" || planPage.hidden) return;
      const focusable = Array.from(planPage.querySelectorAll(planPageFocusableSelector))
        .filter(node => node instanceof HTMLElement && !node.hidden);
      if (!focusable.length) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    planPageDownload.onclick = () => {
      if (!activePlan) return;
      downloadPlanText(activePlan.title, activePlan.body);
    };
    planPageCopy.onclick = () => {
      if (!activePlan) return;
      copyPromptCardText(planPageCopy, activePlan.body);
    };
    planPageExecute.onclick = executeActivePlan;
    planExecutionConfirm.onclick = executeActivePlan;
    planExecutionSkip.onclick = hidePlanExecutionBar;
    planExecutionRevise.onclick = () => {
      promptInput.focus({preventScroll: true});
    };
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
    modelCatalogRetry.onclick = () => {
      void loadModelCatalog();
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
      if (!modelCatalogAvailable || event.target === modelSelector) return;
      toggleSettingOptions(modelOptions);
    };
    serviceTierSettingRow.onclick = event => {
      if (!modelCatalogAvailable || event.target === serviceTierSelector) return;
      toggleSettingOptions(serviceTierOptions);
    };
    reasoningSettingRow.onclick = event => {
      if (!modelCatalogAvailable || event.target === reasoningSelector) return;
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
      if (event.key !== "Escape") return;
      if (!planPage.hidden) {
        event.preventDefault();
        closePlanPage();
        return;
      }
      closeHeaderPopovers();
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
      if (action === "choose") {
        openInterruptionChoice();
        return;
      }
      if (action === "wait") {
        setSendStatus("当前轮正在执行，请等待或使用单独的中断按钮", "");
        return;
      }
      // Sending, keyboard submission, attachments and steer choices must
      // never become an interrupt mutation.  Interrupts are issued only by
      // ``interruptButton.onclick`` below, where the user sees that intent.
      if (action !== "continue" && action !== "steer") return;
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
      requestTurnStatusUpdate(action === "steer" ? "修正中" : "发送中", "busy");
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
        if (nativeTurnRunning) requestTurnStatusUpdate("正在处理", "busy");
        updateNativeHeaderContext();
        pendingUserEcho = null;
        setSendStatus("已发送", "ok");
        await pollEvents();
      } catch (error) {
        if (await recoverNativeControlAfterFetchFailure(error, controlSnapshot)) {
          pendingUserEcho = null;
          clearComposerDraft();
          requestTurnStatusUpdate(nativeTurnRunning ? "正在处理" : "已发送", nativeTurnRunning ? "busy" : "neutral");
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
      const modelSettings = readSelectedModelSettings();
      const body = {prompt};
      // A catalog outage must never turn a previous localStorage choice into
      // an unverified provider model for an existing session.  Continue the
      // known session with provider defaults until a fresh catalog returns.
      if (modelCatalogAvailable && modelSettings.model) body.model = modelSettings.model;
      if (modelCatalogAvailable && modelSettings.effort) body.effort = modelSettings.effort;
      if (modelCatalogAvailable && modelSettings.service_tier) body.service_tier = modelSettings.service_tier;
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
      requestTurnStatusUpdate("中断中", "busy");
      setSendStatus("中断中", "");
      try {
        await nativeControl("interrupt", {turn_id: activeTurnId});
        activeTurnId = "";
        nativeTurnRunning = false;
        requestTurnStatusUpdate("已中断", "done", {terminal: true});
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
        event.kind === "user_message" &&
        payload.native_turn_id &&
        !hasTerminalTurnEvent(payload.native_turn_id)
      ) {
        activeTurnId = payload.native_turn_id;
        nativeTurnRunning = true;
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
      if (isTerminalTurnEvent(event)) {
        requestTurnStatusUpdate(statusTitle(event, statusText(event, payload)), statusTone(event), {event, terminal: true});
      } else if (nativeTurnRunning || sendingPrompt) {
        requestTurnStatusUpdate("正在处理", "busy", {event});
      }
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
    function hasTerminalTurnEvent(turnId) {
      const targetTurnId = String(turnId || "");
      if (!targetTurnId) return false;
      return loadedEvents.some(candidate => isTerminalTurnEvent(candidate) && eventFoldTurnId(candidate) === targetTurnId);
    }
    function isCompletedStatus(status) {
      return (NATIVE_TURN_SEMANTICS.completed || []).includes(
        String(status || "").trim().toLowerCase()
      );
    }
    function setComposerActivity(active) {
      composerActivity.classList.toggle("active", Boolean(active));
    }
    function reconcileTurnStatusFromFlags(label = "", tone = "") {
      const active = Boolean(nativeTurnRunning || sendingPrompt || activeTurnId);
      turnStatus.active = active;
      turnStatus.turnId = active ? String(activeTurnId || nativeTurnId || turnStatus.turnId || "") : "";
      if (active) {
        turnStatus.terminal = false;
        turnStatus.tone = "busy";
        turnStatus.label = label || turnStatus.label || "正在处理";
      } else if (label || tone) {
        turnStatus.terminal = false;
        turnStatus.tone = tone || "neutral";
        turnStatus.label = label || turnStatus.label || "连接会话";
      }
    }
    function isTerminalRunStatusUpdate(_tone, options = {}) {
      const event = options.event || null;
      if (options.terminal === true) return true;
      if (event && isTerminalTurnEvent(event)) return true;
      return false;
    }
    function isNonTerminalCompletionLabel(label) {
      return ["完成", "已完成", "审批已处理", "已批准一次", "本会话已批准", "已拒绝"].includes(
        String(label || "").trim()
      );
    }
    function requestTurnStatusUpdate(label, tone, options = {}) {
      label = String(label || "").trim();
      tone = tone || "neutral";
      const terminal = isTerminalRunStatusUpdate(tone, options);
      reconcileTurnStatusFromFlags(label, tone);
      if (turnStatus.active && !terminal && isNonTerminalCompletionLabel(label)) label = "正在处理";
      if (turnStatus.active && !terminal) {
        tone = "busy";
        label = label || turnStatus.label || "正在处理";
        turnStatus.terminal = false;
      } else if (terminal) {
        turnStatus.active = false;
        turnStatus.turnId = "";
        turnStatus.terminal = true;
        turnStatus.tone = tone;
        turnStatus.label = label || (tone === "failed" ? "失败" : "完成");
      } else {
        turnStatus.terminal = false;
        turnStatus.tone = tone;
        turnStatus.label = label || turnStatus.label || "连接会话";
      }
      updateRunState(turnStatus.label, turnStatus.tone);
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
      showPlanExecutionBar(activePlan);
      return activePlan;
    }
    function openPlanPage(plan = activePlan) {
      if (!plan || !String(plan.body || "").trim()) return;
      activePlan = plan;
      renderPlanPage(plan);
      planPagePreviouslyFocused = document.activeElement;
      planPagePreviousOverflow = document.body.style.overflow;
      if (nativeAppShell) {
        nativeAppShell.inert = true;
        nativeAppShell.setAttribute("aria-hidden", "true");
      }
      planPage.hidden = false;
      document.body.style.overflow = "hidden";
      updatePlanActionState();
      requestAnimationFrame(() => planPageClose?.focus());
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
      if (planPage.hidden) return;
      planPage.hidden = true;
      document.body.style.overflow = planPagePreviousOverflow;
      if (nativeAppShell) {
        nativeAppShell.inert = false;
        nativeAppShell.removeAttribute("aria-hidden");
      }
      if (planPagePreviouslyFocused instanceof HTMLElement) {
        planPagePreviouslyFocused.focus({preventScroll: true});
      }
      planPagePreviouslyFocused = null;
    }
    function showPlanExecutionBar(plan = activePlan) {
      if (!planExecutionBar) return;
      const visible = Boolean(plan && plan.executable && nativeThreadId);
      planExecutionBar.hidden = !visible;
      document.body.classList.toggle("has-plan-execution-bar", visible);
      updatePlanExecutionBar();
      requestAnimationFrame(syncDockHeight);
    }
    function hidePlanExecutionBar() {
      if (!planExecutionBar) return;
      planExecutionBar.hidden = true;
      document.body.classList.remove("has-plan-execution-bar");
    }
    function isWaitingOnActivePlanConfirmation() {
      if (!activePlan || !activePlan.executable || !nativeTurnRunning) return false;
      const planTurnId = String(activePlan.turnId || "");
      const currentTurnId = String(activeTurnId || nativeTurnId || turnStatus.turnId || "");
      return Boolean(planTurnId && currentTurnId && planTurnId === currentTurnId);
    }
    function canExecuteActivePlan() {
      if (!activePlan || !activePlan.executable || !nativeThreadId || sendingPrompt) return false;
      return !nativeTurnRunning || isWaitingOnActivePlanConfirmation();
    }
    function updatePlanExecutionBar() {
      const disabled = !canExecuteActivePlan();
      if (planExecutionConfirm) planExecutionConfirm.disabled = disabled;
      if (planExecutionRevise) planExecutionRevise.disabled = sendingPrompt;
      if (planExecutionBar && !planExecutionBar.hidden && (!activePlan || !activePlan.executable)) {
        hidePlanExecutionBar();
      }
    }
    function updatePlanActionState() {
      const disabled = !canExecuteActivePlan();
      planPageExecute.disabled = disabled;
      document.querySelectorAll(".plan-card-execute").forEach(button => {
        button.disabled = disabled;
      });
      updatePlanExecutionBar();
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
      if (nativeTurnRunning && !isWaitingOnActivePlanConfirmation()) {
        setSendStatus("等待当前轮结束", "error");
        return;
      }
      const prompt = planExecutionPrompt(activePlan.body);
      clearSelectedPlanModeForExecution();
      hidePlanExecutionBar();
      const body = buildNativePromptBody(prompt, {collaborationMode: explicitDefaultCollaborationMode()});
      body.force_new_turn = true;
      renderLocalUserEcho(prompt, []);
      closePlanPage();
      sendingPrompt = true;
      updateComposerDisabled();
      setSendStatus("执行计划", "");
      continueButton.classList.add("loading");
      requestTurnStatusUpdate("执行计划", "busy");
      try {
        const result = await nativeControl("continue", body);
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.active_turn_id || (result.turn_running ? result.turn_id || "" : "");
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        if (nativeTurnRunning) requestTurnStatusUpdate("正在处理", "busy");
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
    function isPlanExecutionUserMessage(event) {
      if (!event || event.kind !== "user_message") return false;
      const payload = event.payload || {};
      const text = String(payload.text || payload.delta || payload.summary || payload.content || payload.prompt || "").trim();
      return text === "Implement the proposed plan." || isPlanExecutionPrompt(text);
    }
    function syncPlanExecutionUiFromEvent(event) {
      if (!isPlanExecutionUserMessage(event)) return;
      clearSelectedPlanModeForExecution();
      hidePlanExecutionBar();
      closePlanPage();
      if (activePlan) {
        activePlan = {...activePlan, executable: false, status: "executing"};
      }
      updatePlanActionState();
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
      const requiresTurn = mode === "steer";
      const hasActiveTurn = Boolean(nativeTurnRunning && nativeThreadId && activeTurnId);
      const canSteer = hasActiveTurn && canSteerActiveTurn();
      const canInterrupt = hasActiveTurn && canInterruptActiveTurn();
      if (dockActions) dockActions.hidden = !(canSteer || canInterrupt);
      steerButton.hidden = !canSteer;
      interruptButton.hidden = !canInterrupt;
      continueButton.innerHTML = ICONS.send;
      continueButton.classList.remove("stop");
      continueButton.setAttribute(
        "aria-label",
        mode === "wait" ? "等待当前轮" : nativeTurnRunning ? "发送到当前轮" : "发送"
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
      modelSelector.disabled = !modelCatalogAvailable || sendingPrompt || nativeTurnRunning;
      modelSettingsButton.disabled = false;
      permissionSelector.disabled = sendingPrompt;
      permissionSettingsButton.disabled = false;
      reasoningSelector.disabled = !modelCatalogAvailable || sendingPrompt || nativeTurnRunning || reasoningSelector.options.length <= 1;
      serviceTierSelector.disabled = !modelCatalogAvailable || sendingPrompt || nativeTurnRunning || serviceTierSelector.options.length <= 1;
      interruptButton.disabled = sendingPrompt || !canInterruptActiveTurn() || !nativeThreadId || !activeTurnId;
      updateHandoffControls();
      syncSettingOptionsDisabled();
      setComposerActivity(nativeTurnRunning || sendingPrompt);
    }
    function updateSettingSummary() {
      if (!modelCatalogAvailable) {
        modelSettingValue.textContent = "模型目录不可用";
        modelSettingsButton.textContent = "模型目录不可用";
        modelSettingsButton.classList.remove("modified");
        return;
      }
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
      scrollToBottom(true);
    }
    async function attachNative() {
      if (invalidNativeThreadId) return;
      if (!nativeThreadId || attached) return;
      attached = true;
      try {
        const result = await withNativeSoftTimeout(api(`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/attach`, {
          method: "POST",
          body: "{}",
          headers: {"Idempotency-Key": nativeMutationKey("session-attach")}
        }), "连接原生会话较慢");
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.active_turn_id || "";
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        requestTurnStatusUpdate(nativeTurnRunning ? "正在处理" : "连接会话", nativeTurnRunning ? "busy" : "neutral");
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
          body: "{}",
          headers: {"Idempotency-Key": nativeMutationKey("session-sync")}
        }), "同步原生 transcript 较慢");
        if (result && result.turn_id) nativeTurnId = result.turn_id;
        activeTurnId = result.turn_running ? (result.active_turn_id || result.turn_id || activeTurnId || "") : "";
        nativeTurnRunning = Boolean(result.turn_running || activeTurnId);
        requestTurnStatusUpdate(nativeTurnRunning ? "正在处理" : "连接会话", nativeTurnRunning ? "busy" : "neutral");
        updateComposerDisabled();
        updateNativeHeaderContext();
      } catch (error) {
        renderStatus("native_sync_failed", error.message || String(error));
      }
    }
    nativePresentationRetry.addEventListener("click", async () => {
      if (!nativeThreadId || invalidNativeThreadId || nativePresentationRetry.disabled) return;
      nativePresentationRetry.disabled = true;
      nativePresentationRetry.textContent = "正在同步";
      try {
        await syncNativeTranscript();
        // This is deliberately an explicit user action.  Normal page loads
        // and SSE initial snapshots stay read-only; only this retry is allowed
        // to request provider transcript synchronization.
        await loadNativeSessionInfo();
      } finally {
        nativePresentationRetry.disabled = false;
        nativePresentationRetry.textContent = "重新同步";
      }
    });
    function stopNativeTranscriptFallback() {
      if (!nativeTranscriptSyncTimer) return;
      window.clearInterval(nativeTranscriptSyncTimer);
      nativeTranscriptSyncTimer = null;
    }
    function startNativeTranscriptFallback() {
      // EventSource is the normal delivery path.  Polling exists only as a
      // low-frequency, visible-page recovery path while it reconnects.
      if (document.visibilityState === "hidden" || source || nativeTranscriptSyncTimer) return;
      nativeTranscriptSyncTimer = window.setInterval(
        pollEvents,
        NATIVE_TRANSCRIPT_FALLBACK_INTERVAL_MS,
      );
    }
    function resumeNativeLiveConnection() {
      if (document.visibilityState === "hidden") return;
      pollEvents();
      if (!source) openStream(currentStreamCursor());
    }
    function startNativeTranscriptSyncLoop() {
      if (nativeThreadId && !invalidNativeThreadId) {
        // Opening the page is read-only: messages are restored from the
        // cached snapshot and SSE.  Transcript writes stay behind explicit
        // recovery/history actions or the server-side background worker.
        resumeNativeLiveConnection();
      }
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") {
          stopNativeTranscriptFallback();
          closeLiveEventSource();
          return;
        }
        resumeNativeLiveConnection();
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
    loadNativeSessionInfo().catch(() => {});
    loadRecentEvents().catch(error => {
      renderStatus("load_recent_failed", error.message || String(error));
    }).then(() => {
      startNativeTranscriptSyncLoop();
    });
    window.addEventListener("pagehide", () => {
      stopNativeTranscriptFallback();
      closeLiveEventSource();
    });
    window.addEventListener("beforeunload", () => {
      stopNativeTranscriptFallback();
      closeLiveEventSource();
    });
    window.addEventListener("pageshow", () => {
      resumeNativeLiveConnection();
    });
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
      const wasActive = Boolean(nativeTurnRunning || activeTurnId || turnStatus.active);
      nativeTurnRunning = Boolean(runState.active);
      activeTurnId = nativeTurnRunning ? String(runState.active_turn_id || activeTurnId || "") : "";
      requestTurnStatusUpdate(nativeTurnRunning ? "正在处理" : wasActive ? "完成" : "连接会话", nativeTurnRunning ? "busy" : wasActive ? "done" : "neutral", {terminal: wasActive && !nativeTurnRunning, runState});
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
    function nativePlanTurnSet(sourceEvents) {
      const turns = new Set();
      for (const event of normalizeEventList(sourceEvents)) {
        if (!isNativePlanEvent(event)) continue;
        const turnId = eventFoldTurnId(event);
        if (turnId) turns.add(turnId);
      }
      return turns;
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
      const itemId = String(payload.itemId || payload.item_id || "");
      return Boolean(
        isNativeFeedbackMode(event) &&
        event &&
        event.kind === "activity" &&
        (payload.action === "plan_updated" || itemId.endsWith("-plan"))
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
      if (document.visibilityState === "hidden") return;
      if (source) {
        source.close();
        source = null;
      }
      startNativeTranscriptFallback();
      if (streamReconnectTimer) return;
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
      if (document.visibilityState === "hidden") return;
      closeLiveEventSource();
      source = new EventSource(streamPathWithCursor(afterId));
      source.onopen = () => {
        streamReconnectDelay = 500;
        stopNativeTranscriptFallback();
        setConnectionState("connected");
      };
      source.onerror = () => {
        setConnectionState("reconnecting");
        pollEvents();
        startNativeTranscriptFallback();
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
        scrollToBottom();
        return;
      }
      if (isOfficialAssistantTranscriptEvent(event)) {
        rebuildStream();
        applyNativeTurnState(event);
        updateComposerDisabled();
        if (event.id) cursor.textContent = "#" + event.id;
        scrollToBottom();
        return;
      }
      if (previousLatestTurnId && incomingTurnId && incomingTurnId !== previousLatestTurnId) {
        rebuildStream();
        applyNativeTurnState(event);
        updateComposerDisabled();
        if (event.id) cursor.textContent = "#" + event.id;
        scrollToBottom();
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
      const nativePlanTurns = nativePlanTurnSet(sourceEvents);
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
        if (
          isAssistantMessageEvent(event) &&
          nativePlanTurns.has(eventFoldTurnId(event)) &&
          proposedPlanTextFromText(visibleTranscriptText(event))
        ) {
          continue;
        }
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
      return (NATIVE_TURN_SEMANTICS.failed || []).includes(
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
      syncPlanExecutionUiFromEvent(event);
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
      else requestTurnStatusUpdate(statusTitle(event, statusText(event, payload)), statusTone(event), {event});
      } finally {
        renderTarget = previousTarget;
      }
      if (options.scroll !== false) scrollToBottom();
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
      requestTurnStatusUpdate("计划已更新", "busy", {event});
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
      const proposedPlan = proposedPlanTextFromText(text);
      if (proposedPlan) {
        target.replaceChildren();
        if (hasNativePlanEventForTurn(event)) return true;
        const titleText = planTitleFromText(proposedPlan, "");
        const summaryText = planSummaryFromText(proposedPlan);
        setActivePlanFromEvent(event, proposedPlan, titleText, summaryText);
        const plan = document.createElement("div");
        plan.className = "plan-item prompt-plan-fallback";
        plan.append(createPlanCardElement(proposedPlan, titleText, {executable: true}));
        target.append(plan);
        return true;
      }
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
    function proposedPlanTextFromText(text) {
      const source = String(text || "").replace(/\\r\\n/g, "\\n").trim();
      const match = source.match(/<proposed_plan>\\s*([\\s\\S]*?)\\s*<\\/proposed_plan>/i);
      return match ? match[1].trim() : "";
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
      requestTurnStatusUpdate(display.title || display.detail, tone || "neutral", {event});
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
      requestTurnStatusUpdate(commandStatus(event.kind), event.kind === "command_failed" ? "failed" : "busy", {event});
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
      requestTurnStatusUpdate("等待审批", "busy", {event});
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
        const table = tryReadMarkdownTable(lines, index);
        if (table) {
          flushParagraph();
          flushList();
          renderMarkdownTable(target, table);
          index = table.endIndex;
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
    function tryReadMarkdownTable(lines, startIndex) {
      const header = splitMarkdownTableRow(lines[startIndex] || "");
      const separator = splitMarkdownTableRow(lines[startIndex + 1] || "");
      if (header.length < 2 || separator.length < header.length) return null;
      if (!separator.every(cell => /^:?-{3,}:?$/.test(cell.replace(/\\s+/g, "")))) return null;
      const rows = [];
      let index = startIndex + 2;
      while (index < lines.length) {
        const cells = splitMarkdownTableRow(lines[index] || "");
        if (cells.length < 2) break;
        rows.push(cells);
        index += 1;
      }
      return {header, rows, endIndex: index - 1};
    }
    function splitMarkdownTableRow(line) {
      const value = String(line || "").trim();
      if (!value.includes("|")) return [];
      const trimmed = value.replace(/^\\|/, "").replace(/\\|$/, "");
      return trimmed.split("|").map(cell => cell.trim());
    }
    function renderMarkdownTable(target, table) {
      const wrapper = document.createElement("div");
      wrapper.className = "markdown-table-wrap";
      const tableNode = document.createElement("table");
      const thead = document.createElement("thead");
      const headerRow = document.createElement("tr");
      for (const cell of table.header) {
        const th = document.createElement("th");
        appendInlineMarkdown(th, cell);
        headerRow.append(th);
      }
      thead.append(headerRow);
      tableNode.append(thead);
      const tbody = document.createElement("tbody");
      for (const row of table.rows) {
        const tr = document.createElement("tr");
        for (let index = 0; index < table.header.length; index++) {
          const td = document.createElement("td");
          appendInlineMarkdown(td, row[index] || "");
          tr.append(td);
        }
        tbody.append(tr);
      }
      tableNode.append(tbody);
      wrapper.append(tableNode);
      target.append(wrapper);
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
      if (event.kind === "lifecycle" && isFailedStatus((event.payload || {}).status)) return "failed";
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
      if (event.kind === "lifecycle" && isFailedStatus(status)) return terminalStatusLabel(status);
      if (event.kind === "reasoning_delta") return "Thinking";
      if (event.kind === "completed") return "完成";
      if (event.kind === "failed") return "失败";
      return fallback || event.kind || "状态";
    }
    function terminalStatusLabel(status) {
      const normalized = String(status || "").trim().toLowerCase();
      if ((NATIVE_TURN_SEMANTICS.interrupted || []).includes(normalized)) return "已中断";
      if ((NATIVE_TURN_SEMANTICS.timeout || []).includes(normalized)) return "已超时";
      return "失败";
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
            "__NATIVE_TURN_SEMANTICS_JSON__",
            json.dumps(turn_semantics_json(), ensure_ascii=False),
        )
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
    function nativeMutationKey(operation) {{
      const runtime = window.WLCodexSurfaceRuntime;
      if (runtime && typeof runtime.mutationKey === "function") {{
        return runtime.mutationKey("native-" + operation);
      }}
      return "native-" + operation + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    }}
    async function nativeControl(action, body) {{
      if (!nativeThreadId) return;
      await api(`/api/native/codex/sessions/${{encodeURIComponent(nativeThreadId)}}/${{action}}`, {{
        method: "POST",
        body: JSON.stringify(body),
        headers: {{"Idempotency-Key": nativeMutationKey("session-" + action)}}
      }});
    }}
    async function resolveApproval(requestId, action) {{
      await api(`/api/native/codex/approvals/${{encodeURIComponent(requestId)}}/resolve`, {{
        method: "POST",
        body: JSON.stringify({{action}}),
        headers: {{"Idempotency-Key": nativeMutationKey("approval-" + requestId)}}
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
