"""Native worker live-page template."""

from __future__ import annotations

import json
from html import escape
from urllib.parse import quote

from wlcodex.live_stream.native_templates.dependencies import NativePageDependencies

def render_live_page(
    agent_run_id: int,
    *,
    native_provider: str = "codex",
    theme: str = "",
    deps: NativePageDependencies,
) -> str:
    _native_provider_display_name = deps.provider_display_name
    _replace_html_icons = deps.replace_html_icons
    _NATIVE_APP_HEAD = deps.app_head
    turn_semantics_json = deps.turn_semantics_json
    _native_permission_presets = deps.permission_presets
    _codex_plugin_menu_items = deps.plugin_menu_items
    _ICONS_JS_LITERAL = deps.icons_js_literal
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
    const nativeStreamConnection = window.WLCodexSurfaceRuntime.createSseConnection({
      url: () => streamPathWithCursor(currentStreamCursor()),
      onOpen: () => {
        stopNativeTranscriptFallback();
        setConnectionState("connected");
      },
      onError: () => {
        setConnectionState("reconnecting");
        pollEvents().catch(() => {});
      },
      onReconnectScheduled: () => {
        startNativeTranscriptFallback();
      },
      onMessage: (message) => renderNativeStreamPayload(JSON.parse(message.data)),
    });
    const nativeStreamEventKinds = [
      "message_added", "message_updated", "message_completed", "run_state", "sync_state",
      "lifecycle", "activity", "user_message", "text_delta", "reasoning_delta",
      "command_started", "command_output", "command_completed", "command_failed",
      "file_changed", "diff_updated", "approval_requested", "approval_resolved",
      "completed", "failed", "event",
    ];
    let nativeStreamListenersBound = false;
    function bindNativeStreamListeners() {
      if (nativeStreamListenersBound) return;
      nativeStreamListenersBound = true;
      nativeStreamEventKinds.forEach(kind => nativeStreamConnection.addEventListener(
        kind,
        message => renderNativeStreamPayload(JSON.parse(message.data)),
      ));
    }
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
      if (document.visibilityState === "hidden" || nativeStreamConnection.source || nativeTranscriptSyncTimer) return;
      nativeTranscriptSyncTimer = window.setInterval(
        pollEvents,
        NATIVE_TRANSCRIPT_FALLBACK_INTERVAL_MS,
      );
    }
    function resumeNativeLiveConnection() {
      if (document.visibilityState === "hidden") return;
      pollEvents();
      if (!nativeStreamConnection.source) openStream(currentStreamCursor());
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
      nativeStreamConnection.close();
    }
    function currentStreamCursor() {
      return nativeThreadId ? nativeUpdateCursor : latestEventId;
    }
    function openStream(afterId) {
      if (document.visibilityState === "hidden") return;
      nativeStreamConnection.close();
      bindNativeStreamListeners();
      nativeStreamConnection.connect();
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
