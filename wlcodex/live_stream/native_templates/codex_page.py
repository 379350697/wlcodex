"""Native Codex-compatible page template."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote


def render_native_codex_page(
    provider_name: str = "codex",
    *,
    theme: str = "",
    helpers: dict[str, Any],
) -> str:
    _native_provider_display_name = helpers["_native_provider_display_name"]
    _replace_html_icons = helpers["_replace_html_icons"]
    _NATIVE_APP_HEAD = helpers["_NATIVE_APP_HEAD"]
    turn_semantics_json = helpers["turn_semantics_json"]
    _native_permission_presets = helpers["_native_permission_presets"]
    _codex_plugin_menu_items = helpers["_codex_plugin_menu_items"]
    _ICONS_JS_LITERAL = helpers["_ICONS_JS_LITERAL"]
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
  <script src="/static/surface_runtime.js?v=20260710-semantic-closure"></script>
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
    .recent-status.stale::before { content: "!"; display: grid; place-items: center; width: 18px; height: 18px; border: 1px solid #f5c451; border-radius: 50%; color: #f5c451; font-size: 12px; font-weight: 800; }
    .recent-status.running .status-time, .recent-status.finished .status-time, .recent-status.stale .status-time { display: none; }
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
    .compose-mode-hint { display: grid; gap: 6px; width: min(100%, 260px); padding: 16px 18px; border: 1px solid #303036; border-radius: 24px; background: #000; color: var(--text-primary); text-align: left; }
    .compose-mode-hint strong { font-size: 18px; line-height: 1.2; font-weight: var(--weight-black); }
    .compose-mode-hint span { color: var(--text-secondary); font-size: 13px; line-height: 1.45; }
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
    .model-catalog-notice { display: flex; gap: 10px; align-items: center; min-width: 0; padding: 9px 10px; border: 1px solid rgba(245,196,81,.52); border-radius: 12px; background: rgba(103,78,12,.27); color: #fff4c4; font-size: 12px; line-height: 1.4; }
    .model-catalog-notice[hidden] { display: none; }
    .model-catalog-notice-copy { min-width: 0; flex: 1 1 auto; }
    .model-catalog-retry { flex: 0 0 auto; min-height: 44px; padding: 0 11px; border: 1px solid rgba(245,196,81,.72); border-radius: 10px; background: #2d250f; color: #fff4c4; font-size: 12px; font-weight: var(--weight-extrabold); }
    .model-catalog-retry:disabled { opacity: .64; }
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
      <div class="compose-mode-hint" role="status" aria-live="polite">
        <strong>工作区</strong>
        <span>当前仅提供项目或当前目录起步，不展示未实现的工作树入口。</span>
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
    <div class="model-catalog-notice" id="modelCatalogNotice" role="status" aria-live="polite" hidden>
      <span class="model-catalog-notice-copy" id="modelCatalogNoticeText"></span>
      <button class="model-catalog-retry" id="modelCatalogRetry" type="button">重试同步</button>
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
    const NATIVE_TURN_SEMANTICS = __NATIVE_TURN_SEMANTICS_JSON__;
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
    let sessionsFallbackPollTimer = null;
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
    const modelCatalogNotice = document.getElementById("modelCatalogNotice");
    const modelCatalogNoticeText = document.getElementById("modelCatalogNoticeText");
    const modelCatalogRetry = document.getElementById("modelCatalogRetry");
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
    let modelCatalogAvailable = false;
    let modelCatalogLoading = false;
    let modelCatalogUnavailableReason = "正在同步模型目录";
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

    function nativeMutationKey(operation) {
      const runtime = window.WLCodexSurfaceRuntime;
      if (runtime && typeof runtime.mutationKey === "function") {
        return runtime.mutationKey("native-" + operation);
      }
      return "native-" + operation + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
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

    function stopSessionsFallbackPoll() {
      if (!sessionsFallbackPollTimer) return;
      window.clearInterval(sessionsFallbackPollTimer);
      sessionsFallbackPollTimer = null;
    }

    function startSessionsFallbackPoll() {
      // The session stream owns normal refreshes.  A visible page falls back
      // to a slow poll only while that stream is unavailable.
      if (document.visibilityState === "hidden" || sessionsEventSource || sessionsFallbackPollTimer) return;
      sessionsFallbackPollTimer = window.setInterval(
        refreshSessionsSilently,
        SESSION_POLL_INTERVAL_MS,
      );
    }

    function startSessionsStream() {
      if (document.visibilityState === "hidden" || sessionsEventSource) return;
      try {
        const source = new EventSource(sessionsStreamPath());
        sessionsEventSource = source;
        source.onopen = () => {
          stopSessionsFallbackPoll();
        };
        source.addEventListener("native_sessions", message => {
          const data = JSON.parse(message.data || "{}");
          applySessionsPayload(data, true);
        });
        source.onerror = () => {
          if (sessionsEventSource !== source) return;
          source.close();
          sessionsEventSource = null;
          startSessionsFallbackPoll();
          if (document.visibilityState === "hidden") return;
          if (!sessionsReconnectTimer) {
            sessionsReconnectTimer = window.setTimeout(() => {
              sessionsReconnectTimer = null;
              startSessionsStream();
            }, 3000);
          }
        };
      } catch (_error) {
        sessionsEventSource = null;
        startSessionsFallbackPoll();
      }
    }

    function resumeSessionsLiveConnection() {
      if (document.visibilityState === "hidden") return;
      refreshSessionsSilently();
      startSessionsStream();
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
          presentation_state: String(((session.presentation || {}).state) || ""),
          presentation_source: String((((session.presentation || {}).freshness || {}).source) || ""),
          presentation_stale: Boolean((((session.presentation || {}).freshness || {}).is_stale)),
          presentation_reason: String((((session.presentation || {}).freshness || {}).reason) || ""),
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

    function usableModelCatalogEntries(rawModels) {
      return (Array.isArray(rawModels) ? rawModels : []).filter(model => {
        const modelId = String((model && (model.model || model.id)) || "").trim();
        return Boolean(modelId);
      });
    }

    function showModelCatalogUnavailable(reason) {
      modelCatalogUnavailableReason = String(reason || "未能同步可用模型").trim() || "未能同步可用模型";
      modelCatalogNoticeText.textContent = `模型目录不可用：${modelCatalogUnavailableReason}。无法创建新会话；请检查 ${PROVIDER_LABEL} 二进制和 app-server 配置后重试。`;
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
        updateStartControls();
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
      const freshness = ((session && session.presentation) || {}).freshness || {};
      if (freshness.is_stale || freshness.stale) return "stale";
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
      const presentation = (session && session.presentation) || {};
      const freshness = presentation.freshness || {};
      if (workspace) parts.push(workspace);
      if (settings) parts.push(settings);
      if (freshness.is_stale || freshness.stale) parts.push("缓存快照");
      else if (presentation.state === "waiting_approval") parts.push("等待审批");
      else if (presentation.state === "waiting_user") parts.push("等待输入");
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
      if (!modelCatalogAvailable) {
        showModelCatalogUnavailable(modelCatalogUnavailableReason);
        updateStartControls();
        return;
      }
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
      if (modelCatalogAvailable && settings.model) body.model = settings.model;
      if (modelCatalogAvailable && settings.effort) body.effort = settings.effort;
      if (modelCatalogAvailable && settings.service_tier) body.service_tier = settings.service_tier;
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
          body: JSON.stringify(body),
          headers: {"Idempotency-Key": nativeMutationKey("session-start")}
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
      sendButton.disabled = startingChat || (
        viewMode === "compose" && (!modelCatalogAvailable || !hasDraft)
      );
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
    modelCatalogRetry.onclick = () => {
      void loadModelCatalog();
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
    window.addEventListener("pagehide", () => {
      stopSessionsFallbackPoll();
      closeSessionsStream();
    });
    window.addEventListener("beforeunload", () => {
      stopSessionsFallbackPoll();
      closeSessionsStream();
    });
    window.addEventListener("pageshow", resumeSessionsLiveConnection);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        stopSessionsFallbackPoll();
        closeSessionsStream();
        return;
      }
      resumeSessionsLiveConnection();
    });
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
