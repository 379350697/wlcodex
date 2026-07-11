"""Relay list and blocked-inbox page templates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RelayTaskListPageDependencies:
    token_suffix: Any
    pagination_html: Any
    workspace_nav_html: Any
    topbar: Any
    settings_href: Any
    bottom_nav: Any
    replace_html_icons: Any
    mobile_web_head: Any
    inbox_href: Any
    chat_href: Any
    task_list_href: Any
    task_card_html: Any
    task_status_label: Any


@dataclass(frozen=True)
class RelayInboxPageDependencies:
    token_suffix: Any
    summary_presentation_state: Any
    topbar: Any
    workspace_href: Any
    settings_href: Any
    bottom_nav: Any
    replace_html_icons: Any
    mobile_web_head: Any
    activity_label: Any
    status_class_name: Any
    summary_presentation: Any
    task_status_label: Any
    task_view_href: Any


def render_relay_task_list_page(
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
    deps: RelayTaskListPageDependencies,
) -> str:
    _token_suffix = deps.token_suffix
    _relay_task_pagination_html = deps.pagination_html
    _relay_workspace_nav_html = deps.workspace_nav_html
    _marvis_relay_topbar = deps.topbar
    _relay_settings_href = deps.settings_href
    _marvis_relay_bottom_nav = deps.bottom_nav
    _replace_html_icons = deps.replace_html_icons
    _relay_mobile_web_head = deps.mobile_web_head
    _relay_inbox_href = deps.inbox_href
    _relay_chat_href = deps.chat_href
    _relay_task_list_href = deps.task_list_href
    _relay_task_card_html = deps.task_card_html
    _relay_task_status_label = deps.task_status_label
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


def render_relay_blocked_inbox_page(
    summaries: list[Any],
    *,
    selected_workspace: str = "",
    access_token: str = "",
    deps: RelayInboxPageDependencies,
) -> str:
    _token_suffix = deps.token_suffix
    _relay_summary_presentation_state = deps.summary_presentation_state
    _marvis_relay_topbar = deps.topbar
    _relay_workspace_href = deps.workspace_href
    _relay_settings_href = deps.settings_href
    _marvis_relay_bottom_nav = deps.bottom_nav
    _replace_html_icons = deps.replace_html_icons
    _relay_mobile_web_head = deps.mobile_web_head
    _relay_activity_label = deps.activity_label
    _relay_status_class_name = deps.status_class_name
    _relay_summary_presentation = deps.summary_presentation
    _relay_task_status_label = deps.task_status_label
    _relay_task_view_href = deps.task_view_href
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
