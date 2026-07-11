"""Relay task-composer page.

The page owns only presentation and browser mutation feedback.  The socket
server remains responsible for authorization and the Relay API contract.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from html import escape
from pathlib import Path

from wlcodex.live_stream.relay_composer import (
    _marvis_relay_attachment_script,
    _marvis_relay_task_composer,
)
from wlcodex.live_stream.relay_navigation import (
    marvis_relay_bottom_nav,
    marvis_relay_topbar,
    relay_settings_href,
    relay_workspace_href,
)


def render_relay_chat_home_page(
    *,
    selected_workspace: str = "",
    access_token: str = "",
    token_suffix: str,
    document_head: str,
    replace_icons: Callable[[str], str],
) -> str:
    """Render the new-task page from the stable Relay navigation contract."""

    selected_workspace = str(selected_workspace or "")
    workspace_label = Path(selected_workspace).name or selected_workspace or "wlcodex"
    topbar_html = marvis_relay_topbar(
        title="Marvis",
        subtitle=workspace_label,
        back_href=f"/native{token_suffix}",
        right_html=f"""
          <a class="marvis-relay-icon-button" href="{escape(relay_settings_href(selected_workspace, access_token))}" aria-label="Relay设置">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
          </a>
          <a class="marvis-relay-icon-button" href="{escape(relay_workspace_href(selected_workspace, access_token))}" aria-label="任务">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
          </a>
        """,
    )
    bottom_nav_html = marvis_relay_bottom_nav(
        "tasks",
        access_token=access_token,
        selected_workspace=selected_workspace,
    )
    composer_html = _marvis_relay_task_composer(
        token_suffix=token_suffix,
        selected_workspace=selected_workspace,
        access_token=access_token,
        placeholder="请在此输入任务",
    )
    return replace_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
{document_head}
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
    const mutationStatus = marvisComposer?.querySelector("[data-relay-mutation-status]");
    const goalContract = marvisComposer?.querySelector("[data-relay-goal-contract]");
    const executionContract = marvisComposer?.querySelector("[data-relay-execution-contract]");
    const setComposerStatus = (text, isError = false) => {{
      if (!mutationStatus) return;
      mutationStatus.textContent = text;
      mutationStatus.classList.toggle("is-error", isError);
    }};
    const updateExecutionContract = () => {{
      const mode = marvisComposer?.querySelector("input[name=execution_mode]:checked")?.value || "standard";
      if (goalContract) goalContract.hidden = mode !== "goal";
      if (executionContract) executionContract.textContent = mode === "plan_first"
        ? "先计划：架构计划必须经你确认后才进入实现。"
        : mode === "goal"
          ? "目标验收：必须完成实现，并提供独立测试或审计证据后才能完成。"
          : "标准执行：系统根据任务自动选择角色与子代理。";
    }};
    marvisComposer?.querySelectorAll("input[name=execution_mode]").forEach((input) => input.addEventListener("change", updateExecutionContract));
    updateExecutionContract();
    marvisComposer?.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const data = Object.fromEntries(new FormData(marvisComposer).entries());
      const attachments = window.marvisRelayAttachments?.payload() || {{}};
      const hasAttachments = Boolean((attachments.images || []).length || (attachments.files || []).length);
      const title = String(data.title || "").trim() || (hasAttachments ? "请查看附件" : "");
      if (!title) {{ setComposerStatus("请先描述任务。", true); return; }}
      data.title = title;
      data.prompt = title;
      if (String(data.execution_mode || "") === "goal") {{
        if (!String(data.execution_goal || "").trim()) {{ setComposerStatus("目标验收需要明确目标。", true); return; }}
        if (!String(data.acceptance_criteria || "").trim()) {{ setComposerStatus("目标验收需要至少一条验收条件。", true); return; }}
      }}
      if ((attachments.images || []).length) data.images = attachments.images;
      if ((attachments.files || []).length) data.files = attachments.files;
      const submit = marvisComposer.querySelector("[data-marvis-submit]");
      const idempotencyKey = submit?.dataset.idempotencyKey
        || (crypto.randomUUID ? crypto.randomUUID() : `${{Date.now()}}-${{Math.random()}}`);
      if (submit) submit.dataset.idempotencyKey = idempotencyKey;
      submit?.setAttribute("aria-busy", "true");
      if (submit) submit.disabled = true;
      setComposerStatus("正在创建任务…");
      try {{
        const response = await fetch(`/api/relay/tasks${{TOKEN_SUFFIX}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json", "Idempotency-Key": idempotencyKey }},
          body: JSON.stringify(data),
        }});
        const payload = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(payload.error || "创建任务失败，请重试。");
        if (!payload?.task?.id) throw new Error("服务未返回任务标识，请重试。");
        if (submit) delete submit.dataset.idempotencyKey;
        window.marvisRelayAttachments?.clear();
        window.location.href = `/native/workflows/relay/tasks/${{encodeURIComponent(payload.task.id)}}${{TOKEN_SUFFIX}}`;
      }} catch (error) {{
        setComposerStatus(error?.message || "创建失败，请重试。", true);
      }} finally {{
        submit?.removeAttribute("aria-busy");
        if (submit) submit.disabled = false;
      }}
    }});
  </script>
</body>
</html>""")
