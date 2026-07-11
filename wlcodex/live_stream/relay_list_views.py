"""Read-only Relay list presentation fragments.

Keeping these fragments outside of the socket server makes the task-list
contract independently testable: all task cards consume the same presentation
projection and navigation URL builders as the inbox and detail surfaces.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from wlcodex.live_stream.presentation import (
    activity_label,
    role_label,
    status_class_name,
    summary_presentation,
    summary_presentation_state,
    task_status_label,
)
from wlcodex.live_stream.relay_navigation import (
    relay_task_list_href,
    relay_workspace_href,
)


def marvis_relay_avatar_html(role: str, *, label: str = "") -> str:
    role_name = str(role or "marvis").strip() or "marvis"
    alt = label or role_label(role_name)
    return (
        f'<span class="marvis-relay-avatar marvis-relay-avatar-{escape(role_name)}" '
        f'aria-label="{escape(alt)}"></span>'
    )


def relay_workspace_nav_html(
    projects: list[Any],
    *,
    selected_workspace: str,
    access_token: str,
) -> str:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for project in projects:
        cwd = str(project.get("cwd", "") or "")
        if not cwd or cwd in seen:
            continue
        seen.add(cwd)
        rows.append((cwd, str(project.get("name") or Path(cwd).name or cwd)))
    if selected_workspace and selected_workspace not in seen:
        rows.insert(0, (selected_workspace, Path(selected_workspace).name or selected_workspace))
    links = "\n".join(
        '<a class="relay-workspace-link'
        f'{" active" if workspace == selected_workspace else ""}" '
        f'data-workspace-value="{escape(workspace)}" '
        f'href="{escape(relay_workspace_href(workspace, access_token))}">'
        f"{escape(label)}</a>"
        for workspace, label in rows
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


def relay_task_pagination_html(
    *,
    current_page: int,
    total_pages: int,
    selected_workspace: str,
    access_token: str,
    status_filter: str = "",
) -> str:
    if total_pages <= 1:
        return ""
    if current_page > 1:
        previous_html = (
            '<a class="relay-page-link" '
            f'href="{escape(relay_task_list_href(selected_workspace, access_token, current_page - 1, status=status_filter))}">'
            "上一页</a>"
        )
    else:
        previous_html = '<span class="relay-page-disabled" aria-disabled="true">上一页</span>'
    if current_page < total_pages:
        next_html = (
            '<a class="relay-page-link" '
            f'href="{escape(relay_task_list_href(selected_workspace, access_token, current_page + 1, status=status_filter))}">'
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


def relay_task_card_html(summary: Any, token_suffix: str) -> str:
    presentation = summary_presentation(summary)
    status = summary_presentation_state(summary)
    freshness = presentation.get("freshness")
    freshness = freshness if isinstance(freshness, dict) else {}
    activity = activity_label(freshness.get("updated_at") or getattr(summary, "last_activity_at", ""))
    workspace = str(summary.workspace or "")
    project_name = Path(workspace).name or workspace or "wlcodex"
    actor = presentation.get("current_actor")
    actor = actor if isinstance(actor, dict) else {}
    actor_label = str(actor.get("label") or "系统协调")
    handoff = str(getattr(summary, "latest_handoff_summary", "") or "").strip()
    blocking_reason = str(presentation.get("blocking_reason") or "").strip()
    next_action = str(presentation.get("next_action") or "等待系统更新。")
    evidence_html = (
        f'<div class="relay-summary">最新交接：{escape(handoff)}</div>' if handoff else ""
    )
    blocking_html = (
        f'<div class="relay-summary relay-card-blocked">阻塞原因：{escape(blocking_reason)}</div>'
        if blocking_reason
        else ""
    )
    status_class = "relay-status-badge"
    if status:
        status_class += f" is-{status_class_name(status)}"
    return f"""
      <article class="relay-task-card marvis-relay-task-card" data-status="{escape(status)}">
        <div class="relay-card-identity">
          <div class="relay-card-avatar-row">
            {marvis_relay_avatar_html("marvis", label="Marvis")}
          </div>
          <div class="relay-card-side">
            <span class="{escape(status_class)}">{escape(task_status_label(status))}</span>
            <span class="relay-card-project-pill">{escape(project_name)}</span>
            <span class="relay-card-activity">{escape(activity)}</span>
          </div>
        </div>
        <div class="relay-title">{escape(summary.title)}</div>
        <div class="relay-card-side"><strong>当前责任：</strong>{escape(actor_label)}</div>
        {evidence_html}
        {blocking_html}
        <div class="relay-summary"><strong>下一步：</strong>{escape(next_action)}</div>
        <div class="marvis-relay-task-card-footer">
          <a class="relay-open relay-card-open" href="/native/workflows/relay/tasks/{int(summary.task_id)}{token_suffix}">打开任务</a>
        </div>
      </article>
    """
