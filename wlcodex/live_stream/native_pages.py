"""Static Native entry-page templates isolated from HTTP and provider state."""

from __future__ import annotations

from collections.abc import Callable
from html import escape
import json
from urllib.parse import quote


def native_app_manifest() -> str:
    return json.dumps(
        {
            "name": "WLCodex Native",
            "short_name": "WLCodex",
            "description": "Native mobile workspace for WLCodex sessions.",
            "start_url": "/native",
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
        },
        ensure_ascii=False,
        separators=(",", ": "),
    )


def native_app_icon_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#000000"/>
  <rect x="72" y="76" width="368" height="360" rx="72" fill="#111214"/>
  <path d="M312 160 216 256l96 96" fill="none" stroke="#f4f4f5" stroke-width="42" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="370" cy="136" r="24" fill="#58a6ff"/>
</svg>"""


def native_provider_display_name(provider: str) -> str:
    names = {"codex": "Codex", "claude": "Claude", "antigravity": "Antigravity"}
    provider_name = str(provider or "").strip()
    return names.get(provider_name, provider_name.replace("-", " ").title() or "Native")


def render_native_provider_index_page(
    providers: list[dict[str, str]],
    *,
    access_token: str,
    token_suffix: Callable[[str], str],
    replace_icons: Callable[[str], str],
) -> str:
    suffix = token_suffix(access_token)
    council_link = f'''<a class="provider workflow" data-native-entry="workflows" href="/native/workflows{suffix}">
        <span>工作流</span><small>进入 Relay 大任务与已支持的协作工作流</small></a>'''
    if providers:
        links = "\n".join(
            f'<a class="provider" href="/native/{quote(str(provider["provider"]), safe="")}{suffix}">'
            f"<span>{escape(native_provider_display_name(str(provider['provider'])))}</span>"
            f"<small>{escape(str(provider.get('provider_engine', '')))}</small></a>"
            for provider in providers
        )
    else:
        links = '<div class="empty">No native providers configured.</div>'
    return replace_icons(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Native Agents</title><link rel="stylesheet" href="/static/native_index_bundle.css"></head>
<body class="aurora-bg noise-overlay"><main><div class="native-index-topbar">
<button class="circle native-back" id="back" aria-label="back" aria-disabled="true" disabled>‹</button>
<h1>Native Agents</h1><span class="native-back-spacer" aria-hidden="true"></span></div>
{council_link}{links}</main></body></html>""")


def render_native_workflows_page(
    *,
    access_token: str,
    token_suffix: Callable[[str], str],
    replace_icons: Callable[[str], str],
) -> str:
    suffix = token_suffix(access_token)
    return replace_icons(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>工作流</title><link rel="stylesheet" href="/static/native_index_bundle.css"></head>
<body class="aurora-bg noise-overlay"><main><div class="native-index-topbar">
<a class="circle native-back" href="/native{suffix}" aria-label="back">‹</a><h1>工作流</h1>
<span class="native-back-spacer" aria-hidden="true"></span></div>
<a class="provider workflow" data-native-entry="marvis-relay" href="/native/workflows/relay{suffix}">
<span>Marvis 接力</span><small>对话式发布任务，总工程师调度多角色实时协作。</small></a>
<a class="provider council" href="/council{suffix}"><span>议会审核</span><small>沿用现有五席审核入口。</small></a>
<p class="native-workflow-note">未实现的 Skills、Profile、Dev Flow 与工作树不会作为可操作入口展示。</p>
</main></body></html>""")
