"""Relay provider-assignment configuration page."""

from __future__ import annotations

import json
from collections.abc import Callable
from html import escape
from typing import Any

from wlcodex.live_stream.presentation import role_label
from wlcodex.live_stream.relay_navigation import relay_workspace_href
from wlcodex.relay.models import RELAY_ROLE_IDS


def render_relay_config_page(
    *,
    providers: list[dict[str, str]],
    relay_config: dict[str, Any] | None = None,
    selected_workspace: str = "",
    access_token: str = "",
    token_suffix: str,
    provider_display_name: Callable[[str], str],
    replace_icons: Callable[[str], str],
) -> str:
    relay_config = relay_config or {}
    config_providers = relay_config.get("providers")
    provider_rows = (
        config_providers if isinstance(config_providers, list) and config_providers else providers
    )
    role_config_html = relay_role_config_html(
        relay_config,
        provider_rows,
        provider_display_name=provider_display_name,
    )
    back_href = relay_workspace_href(selected_workspace, access_token)
    return replace_icons(f"""<!doctype html>
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
    const saveButton = document.getElementById("save-relay-config");
    saveButton?.addEventListener("click", async () => {{
      const assignments = {{}};
      document.querySelectorAll("[data-role-provider]").forEach((select) => {{
        assignments[select.dataset.roleProvider] = select.value;
      }});
      const idempotencyKey = saveButton.dataset.idempotencyKey
        || (crypto.randomUUID ? crypto.randomUUID() : `${{Date.now()}}-${{Math.random()}}`);
      saveButton.dataset.idempotencyKey = idempotencyKey;
      saveButton.disabled = true;
      saveButton.setAttribute("aria-busy", "true");
      statusNode.textContent = "保存中…";
      try {{
        const response = await fetch(`/api/relay/config${{TOKEN_SUFFIX}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json", "Idempotency-Key": idempotencyKey }},
          body: JSON.stringify({{assignments}}),
        }});
        if (!response.ok) {{
          const payload = await response.json().catch(() => ({{}}));
          throw new Error(payload.error || "保存失败");
        }}
        delete saveButton.dataset.idempotencyKey;
        statusNode.textContent = "配置已保存";
        window.location.href = RELAY_HISTORY_HREF;
      }} catch (error) {{
        statusNode.textContent = error?.message || "保存失败，请重试";
      }} finally {{
        saveButton.disabled = false;
        saveButton.removeAttribute("aria-busy");
      }}
    }});
  </script>
</body>
</html>""")


def relay_role_config_html(
    relay_config: dict[str, Any],
    providers: list[Any],
    *,
    provider_display_name: Callable[[str], str],
) -> str:
    assignments = relay_config.get("assignments")
    assignment_map = assignments if isinstance(assignments, dict) else {}
    roles = relay_config.get("roles")
    role_rows = (
        roles
        if isinstance(roles, list) and roles
        else [{"role": role, "display_name": role_label(role)} for role in RELAY_ROLE_IDS]
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
            f"{escape(provider_display_name(str(provider.get('provider', ''))))}</option>"
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
                <strong>{escape(str(role_entry.get("display_name") or role_label(role)))}</strong>
                <div class="relay-muted">当前：{escape(provider_display_name(selected))}</div>
              </div>
              <select class="relay-provider-select" data-role-provider="{escape(role)}" aria-label="{escape(role_label(role))} Provider">
                {options}
              </select>
              <div class="relay-config-tools">{tool_chips}</div>
            </div>
            """
        )
    return "\n".join(rows)
