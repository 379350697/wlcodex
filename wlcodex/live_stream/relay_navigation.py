"""Pure Relay navigation/template primitives shared by rendered surfaces.

Keeping URL construction and the common mobile chrome out of the HTTP server
makes the visible Relay contract independently testable and prevents task,
inbox and detail pages from drifting apart as routes evolve.
"""

from __future__ import annotations

from html import escape
from urllib.parse import quote


def relay_workspace_href(
    workspace: str,
    access_token: str,
    *,
    status: str = "",
    compose: bool = False,
) -> str:
    params = _relay_params(access_token, workspace)
    if status:
        params.append(f"status={quote(status)}")
    if compose:
        params.append("compose=1")
    suffix = "?" + "&".join(params) if params else ""
    return f"/native/workflows/relay{suffix}"


def relay_inbox_href(workspace: str, access_token: str) -> str:
    return _relay_path_with_workspace(
        "/native/workflows/relay/inbox", workspace, access_token, safe=""
    )


def relay_chat_href(workspace: str, access_token: str) -> str:
    """Open the task composer as a transient state of the task workspace."""

    return relay_workspace_href(workspace, access_token, compose=True)


def relay_task_list_href(
    workspace: str,
    access_token: str,
    page: int,
    *,
    status: str = "",
) -> str:
    params = _relay_params(access_token, workspace)
    if status:
        params.append(f"status={quote(status)}")
    params.append(f"page={max(1, int(page))}")
    return f"/native/workflows/relay?{'&'.join(params)}"


def relay_settings_href(workspace: str, access_token: str) -> str:
    return _relay_path_with_workspace("/native/workflows/relay/config", workspace, access_token)


def relay_task_view_href(task_id: int, access_token: str, view: str) -> str:
    params = []
    if access_token:
        params.append(f"token={quote(access_token, safe='')}")
    selected_view = "board" if str(view or "").strip().lower() == "board" else "conversation"
    params.append(f"view={quote(selected_view, safe='')}")
    return f"/native/workflows/relay/tasks/{task_id}?{'&'.join(params)}"


def relay_task_events_suffix(access_token: str, after: int) -> str:
    params = []
    if access_token:
        params.append(f"token={quote(access_token, safe='')}")
    if after > 0:
        params.append(f"after={after}")
    return "?" + "&".join(params) if params else ""


def relay_config_href(workspace: str, access_token: str) -> str:
    return relay_settings_href(workspace, access_token)


def marvis_relay_topbar(
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


def marvis_relay_bottom_nav(
    active: str = "tasks",
    *,
    access_token: str = "",
    selected_workspace: str = "",
) -> str:
    token_suffix = f"?token={quote(str(access_token or ''), safe='')}" if access_token else ""
    workspace_query = ""
    if selected_workspace:
        joiner = "&" if token_suffix else "?"
        workspace_query = f"{joiner}workspace={quote(selected_workspace, safe='/')}"
    hrefs = {
        "tasks": f"/native/workflows/relay{token_suffix}{workspace_query}",
        "settings": f"/native/workflows/relay/config{token_suffix}{workspace_query}",
    }
    rows = []
    for key, label, icon in (("tasks", "任务", "clock"), ("settings", "设置", "settings")):
        class_name = f"marvis-relay-nav-item{' active' if key == active else ''}"
        current = ' aria-current="page"' if key == active else ""
        rows.append(
            f'''\n        <a class="{class_name}" href="{escape(hrefs[key])}" data-marvis-nav="{escape(key)}"{current}>
          {marvis_relay_nav_icon_html(icon)}
          <span>{escape(label)}</span>
        </a>
        '''
        )
    return "\n".join(rows)


def marvis_relay_nav_icon_html(icon: str) -> str:
    icons = {
        "chat": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
        "clock": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
        "tool": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
        "person": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
        "settings": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06-2 2-.06-.06A1.65 1.65 0 0 0 15.9 18a1.65 1.65 0 0 0-1 .6l-.1.1H12v-2.82l.1-.1a1.65 1.65 0 0 0 .6-1 1.65 1.65 0 0 0-.6-1l-.1-.1V10.9h2.8l.1.1a1.65 1.65 0 0 0 1 .6 1.65 1.65 0 0 0 1.82-.33l.06-.06 2 2-.06.06A1.65 1.65 0 0 0 19.4 15z"/></svg>',
    }
    return f'<span class="marvis-relay-nav-icon" aria-hidden="true">{icons.get(icon, icons["chat"])}</span>'


def _relay_path_with_workspace(
    path: str,
    workspace: str,
    access_token: str,
    *,
    safe: str = "/",
) -> str:
    params = _relay_params(access_token, workspace, safe=safe)
    return f"{path}?{'&'.join(params)}" if params else path


def _relay_params(access_token: str, workspace: str, *, safe: str = "/") -> list[str]:
    params: list[str] = []
    if access_token:
        params.append(f"token={quote(access_token, safe=safe)}")
    if workspace:
        params.append(f"workspace={quote(workspace, safe=safe)}")
    return params
