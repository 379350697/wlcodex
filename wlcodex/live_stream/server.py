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
from wlcodex.live_stream.council_pages import (
    render_council_review_page,
    render_council_seats_page,
)
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
from wlcodex.live_stream.native_templates.auth_pages import (
    render_native_login_ticket_page,
    render_native_token_entry_page,
)
from wlcodex.live_stream.native_templates.codex_page import render_native_codex_page
from wlcodex.live_stream.native_templates.live_page import render_live_page
from wlcodex.live_stream.native_templates.legacy_live_page import render_legacy_live_page
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
from wlcodex.live_stream.relay_detail_components import (
    render_empty_conversation as _render_empty_conversation,
    render_empty_work_log as _render_empty_work_log,
    render_followup_composer as _render_followup_composer,
    render_handoff as _render_handoff,
    render_message as _render_relay_message,
    render_native_empty_conversation as _render_native_empty_conversation,
    render_native_message as _render_native_message,
    render_plan_control as _render_plan_control,
    render_waiting_message as _render_waiting_message,
    render_work_log_shell as _render_work_log_shell,
)
from wlcodex.live_stream.relay_config_page import render_relay_config_page
from wlcodex.live_stream.relay_list_views import (
    marvis_relay_avatar_html as _marvis_relay_avatar_html,
    relay_task_card_html as _relay_task_card_html,
    relay_task_pagination_html as _relay_task_pagination_html,
    relay_workspace_nav_html as _relay_workspace_nav_html,
)
from wlcodex.live_stream.relay_page_assets import (
    render_relay_mobile_web_head as _relay_mobile_web_head,
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
from wlcodex.live_stream.relay_workspace_pages import (
    render_relay_blocked_inbox_page,
    render_relay_task_list_page,
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
    "历史会话已不在当前 Codex 后台中，无法继续发送。请回到会话列表选择可恢复会话，或新建会话继续。"
)


def _is_native_thread_not_found_error(exc: JsonRpcError) -> bool:
    return (
        exc.code == -32600
        and re.match(
            r"^thread not found:\s*\S+\s*$",
            str(exc.rpc_message or ""),
            flags=re.IGNORECASE,
        )
        is not None
    )


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
        tasks.extend(
            task for task in self._relay_dispatch_tasks if task is not asyncio.current_task()
        )
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
                before = _safe_int(before_value, default=0) if str(before_value).strip() else None
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
                before = _safe_int(before_value, default=0) if str(before_value).strip() else None
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
                (task := self._native_background_tasks.get(key)) is not None and not task.done()
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
                raise RuntimeError("native provider does not expose a read-only session snapshot")
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
            authorized=lambda request_writer, request_headers, *, require_token: (
                self._is_authorized(
                    request_writer,
                    request_headers,
                    query,
                    require_token=require_token,
                )
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
        native_thread_id = (
            _optional_nonempty_string(
                query.get("native_thread_id", [""])[0] or query.get("thread_id", [""])[0]
            )
            or ""
        )
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
    return render_relay_task_list_page(
        summaries,
        providers=providers,
        relay_config=relay_config,
        projects=projects,
        selected_workspace=selected_workspace,
        access_token=access_token,
        page=page,
        total=total,
        total_pages=total_pages,
        active_count=active_count,
        state_counts=state_counts,
        status_filter=status_filter,
        helpers={
            "_token_suffix": _token_suffix,
            "_relay_task_pagination_html": _relay_task_pagination_html,
            "_relay_workspace_nav_html": _relay_workspace_nav_html,
            "_marvis_relay_topbar": _marvis_relay_topbar,
            "_relay_settings_href": _relay_settings_href,
            "_marvis_relay_bottom_nav": _marvis_relay_bottom_nav,
            "_replace_html_icons": _replace_html_icons,
            "_relay_mobile_web_head": _relay_mobile_web_head,
            "_relay_inbox_href": _relay_inbox_href,
            "_relay_chat_href": _relay_chat_href,
            "_relay_task_list_href": _relay_task_list_href,
            "_relay_task_card_html": _relay_task_card_html,
            "_relay_task_status_label": _relay_task_status_label,
        },
    )


def _relay_blocked_inbox_page(
    summaries: list[Any],
    *,
    selected_workspace: str = "",
    access_token: str = "",
) -> str:
    return render_relay_blocked_inbox_page(
        summaries,
        selected_workspace=selected_workspace,
        access_token=access_token,
        helpers={
            "_token_suffix": _token_suffix,
            "_relay_summary_presentation_state": _relay_summary_presentation_state,
            "_marvis_relay_topbar": _marvis_relay_topbar,
            "_relay_workspace_href": _relay_workspace_href,
            "_relay_settings_href": _relay_settings_href,
            "_marvis_relay_bottom_nav": _marvis_relay_bottom_nav,
            "_replace_html_icons": _replace_html_icons,
            "_relay_mobile_web_head": _relay_mobile_web_head,
            "_relay_activity_label": _relay_activity_label,
            "_relay_status_class_name": _relay_status_class_name,
            "_relay_summary_presentation": _relay_summary_presentation,
            "_relay_task_status_label": _relay_task_status_label,
            "_relay_task_view_href": _relay_task_view_href,
        },
    )


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
    return [project for project in payload.get("projects", []) if str(project.get("cwd", "") or "")]


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
    return _render_followup_composer(
        task_id=task_id,
        placeholder=placeholder,
        workspace=workspace,
        access_token=access_token,
        task_status=task_status,
        current_round_id=current_round_id,
        pending_inputs=pending_inputs,
        render_workspace_dock=_marvis_relay_workspace_dock,
        render_attachment_sheet=_marvis_relay_attachment_sheet_html,
    )


def _marvis_relay_plan_control_html(detail: Any) -> str:
    return _render_plan_control(detail)


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
    return _render_work_log_shell(
        body_html=body_html,
        token_text=token_text,
        token_total=token_total,
        max_event_id=max_event_id,
    )


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
        return _render_empty_work_log()
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
    confirmation = round_execution.get("confirmation") if isinstance(round_execution, dict) else {}
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
        label = (
            _marvis_relay_confirmation_source_label(
                confirmation_source,
                str(confirmation.get("provider") or ""),
            )
            or "等待确认"
        )
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
        if kind in {"text_delta", "message_completed"} and (
            _relay_text_is_structured_artifact_placeholder(text)
            or text_contains_relay_protocol_payload(text)
            or _relay_parse_role_envelope_payload(text) is not None
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
        if any(
            str(getattr(job, "status", "") or "") in {"queued", "streaming"} for job in role_jobs
        )
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
    return _render_handoff(
        row,
        handoff_pair=_marvis_relay_handoff_pair,
        handoff_text=_marvis_relay_handoff_text,
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
    return _render_empty_conversation()


def _marvis_relay_waiting_message_html() -> str:
    return _render_waiting_message()


def _marvis_relay_message_html(row: dict[str, str]) -> str:
    return _render_relay_message(
        row,
        public_role=_marvis_relay_public_role,
        render_avatar=_marvis_relay_avatar_html,
        role_status_label=_marvis_relay_role_status_label,
        action_label=_marvis_relay_action_label,
        render_attachment_list=_marvis_relay_attachment_list_html,
    )


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
    return _render_native_empty_conversation()


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
    return _render_native_message(row, render_avatar=_marvis_relay_avatar_html)


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
    return render_council_review_page(replace_html_icons=_replace_html_icons)


def _council_seats_page() -> str:
    return render_council_seats_page(replace_html_icons=_replace_html_icons)


def _native_token_entry_page(return_to: str = "/native/codex") -> str:
    return render_native_token_entry_page(return_to)


def _native_login_ticket_page(ticket: str, provider_name: str = "codex") -> str:
    return render_native_login_ticket_page(ticket, provider_name)


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
    return render_live_page(
        agent_run_id,
        native_provider=native_provider,
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


def _legacy_live_page(agent_run_id: int) -> str:
    return render_legacy_live_page(agent_run_id)
