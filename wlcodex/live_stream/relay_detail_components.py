"""Relay task-detail HTML components.

The detail route owns data projection only.  Keeping its small HTML components
here prevents the Relay page from drifting back into the HTTP server while the
callbacks keep presentation policy explicit at the route boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from typing import Any


def render_followup_composer(
    *,
    task_id: int,
    placeholder: str,
    workspace: str,
    access_token: str,
    task_status: str,
    current_round_id: int,
    pending_inputs: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    render_workspace_dock: Callable[..., str],
    render_attachment_sheet: Callable[[], str],
) -> str:
    workspace_dock = render_workspace_dock(workspace, access_token=access_token)
    pending_visible = any(
        str(item.get("status") or "") in {"pending", "steered"} for item in (pending_inputs or [])
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
    {render_attachment_sheet()}
    """


def render_plan_control(detail: Any) -> str:
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
        if str(artifact.get("status") or "") == "waiting":
            waiting_artifact = artifact
            break
    if waiting_artifact is None:
        for artifact in reversed(getattr(detail, "artifacts", []) or []):
            if (
                int(artifact.get("round_id") or round_id) == round_id
                and str(artifact.get("status") or "") == "waiting"
            ):
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
        waiting_artifact.get("confirmation_source") or confirmation.get("source") or ""
    )
    confirmation_provider = str(
        waiting_artifact.get("provider") or confirmation.get("provider") or ""
    )
    confirmation_kind = str(
        waiting_artifact.get("confirmation_kind") or confirmation.get("kind") or ""
    )
    waiting_reason = str(
        waiting_artifact.get("waiting_reason")
        or (round_execution.get("waiting_reason", "") if isinstance(round_execution, dict) else "")
    )
    source_label = str(waiting_artifact.get("confirmation_source_label") or "").strip()
    if not source_label:
        if confirmation_source in {"provider_native_plan", "provider_native_approval"}:
            source_label = (
                "Codex 原生确认"
                if confirmation_provider == "codex"
                else "Claude 原生确认"
                if confirmation_provider.startswith("claude")
                else "Provider 原生确认"
            )
        elif confirmation_source == "relay_prompt_fallback" or not confirmation_source:
            source_label = "Relay 澄清确认"
    is_plan = str(waiting_artifact.get("artifact_type") or "") == "architecture_plan"
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
    options = confirmation_options(waiting_artifact.get("confirmation_options"))
    option_html = "".join(
        f'''<button class="marvis-relay-confirmation-option" type="button" data-confirmation-option-id="{escape(option["id"])}" data-confirmation-option-label="{escape(option["label"])}" data-confirmation-option-instruction="{escape(option["instruction"])}" aria-pressed="{"true" if index == 0 else "false"}"><strong>{escape(option["label"])}</strong><span>{escape(option["summary"] or option["instruction"])}</span></button>'''
        for index, option in enumerate(options)
    )
    question_html = "".join(f"<li>{escape(question)}</li>" for question in questions)
    detail_body = (
        summary
        if not questions
        else f"{summary}\n\n待确认：\n" + "\n".join(f"- {item}" for item in questions)
    )
    meta_parts = [f"来源：{source_label}", f"请求类型：{confirmation_kind or 'relay_question'}"]
    if waiting_reason:
        meta_parts.append(f"等待原因：{waiting_reason}")
    provider_request_id = str(
        waiting_artifact.get("provider_request_id") or confirmation.get("provider_request_id") or ""
    )
    if provider_request_id:
        meta_parts.append(f"请求 ID：{provider_request_id}")
    meta_text = "\n".join(meta_parts)
    return f"""
    <section class="marvis-relay-confirmation-card" data-marvis-confirmation-card data-marvis-plan-control data-round-id="{round_id}" data-artifact-id="{artifact_id}" aria-label="{escape(title)}">
      <button class="marvis-relay-confirmation-thumb" type="button" data-marvis-confirmation-open aria-label="查看确认详情"><em>{escape(source_label)}</em><span>{escape(title)}</span><strong>{escape(summary)}</strong></button>
      <div class="marvis-relay-confirmation-options"{" hidden" if not option_html else ""}>{option_html}</div>
      <div class="marvis-relay-confirmation-actions"><button type="button" data-plan-decision="{escape(primary_decision)}">{escape(primary_label)}</button><button type="button" data-waiting-input>补充内容</button><button type="button" data-plan-decision="cancel_plan">停止</button></div>
    </section>
    <section class="marvis-relay-confirmation-page" data-marvis-confirmation-page data-round-id="{round_id}" data-artifact-id="{artifact_id}" role="dialog" aria-modal="true" aria-label="{escape(title)}" hidden><div class="marvis-relay-confirmation-page-shell"><header><button type="button" data-marvis-confirmation-close aria-label="返回">‹</button><strong>{escape(title)}</strong></header><main><small>{escape(meta_text)}</small><h2>{escape(summary)}</h2><p>{escape(detail_body)}</p><ul{" hidden" if not question_html else ""}>{question_html}</ul></main></div></section>
    """


def confirmation_options(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for index, item in enumerate(raw[:6], start=1):
        if isinstance(item, str):
            option_id, label, summary, instruction = (
                f"option_{index}",
                item.strip(),
                "",
                item.strip(),
            )
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
        if label or instruction:
            options.append(
                {
                    "id": option_id or f"option_{index}",
                    "label": label or instruction,
                    "summary": summary,
                    "instruction": instruction or label,
                }
            )
    return options


def render_work_log_shell(
    *, body_html: str, token_text: str, token_total: int, max_event_id: int
) -> str:
    return f"""
    <div class="marvis-relay-backdrop" data-marvis-work-log-backdrop hidden></div>
    <section class="marvis-work-log" data-marvis-work-log data-marvis-work-log-max-event-id="{max(0, int(max_event_id))}" aria-label="工作日志" hidden>
      <button class="marvis-work-log-close" type="button" data-marvis-close-log aria-label="关闭">×</button>
      <div class="marvis-work-log-tabs"><button class="marvis-work-log-tab active" type="button">工作日志</button></div>
      <div class="marvis-work-log-hero"><div class="marvis-work-log-desks" aria-hidden="true"><img src="/static/marvis/office-desk-worker-1.png" alt=""><img src="/static/marvis/office-desk-empty-slot.png" alt=""><img src="/static/marvis/office-desk-empty-slot.png" alt=""></div><div class="marvis-work-log-metrics"><span>空闲中...</span><strong data-marvis-work-log-token-value data-token-total="{max(0, int(token_total))}">{escape(token_text)} ☕</strong></div></div>
      <div class="marvis-work-log-body" data-marvis-work-log-body>{body_html}</div>
    </section>
    """


def render_empty_work_log() -> str:
    return '<p class="marvis-work-log-empty">暂无工作日志</p>'


def render_handoff(
    row: dict[str, str],
    *,
    handoff_pair: Callable[[dict[str, str]], tuple[str, str] | None],
    handoff_text: Callable[[str, str], str],
) -> str:
    pair = handoff_pair(row)
    if pair is None:
        return ""
    from_role, to_role = pair
    return (
        '<div class="marvis-relay-handoff" data-marvis-handoff '
        f'data-native-kind="handoff" data-native-from-role="{escape(from_role)}" data-native-to-role="{escape(to_role)}" data-native-role="{escape(to_role)}" data-native-round-id="{escape(str(row.get("round_id") or "1"))}" data-native-key="{escape(str(row.get("key") or ""))}">{escape(handoff_text(from_role, to_role))}</div>'
    )


def render_empty_conversation() -> str:
    return """<article class="marvis-relay-agent-step marvis-relay-waiting" data-native-role="director" data-native-kind="status" data-native-empty><span class="marvis-relay-avatar marvis-relay-avatar-marvis" aria-label="Marvis"></span><div><div class="marvis-relay-agent-head"><strong>Marvis</strong></div><div class="marvis-relay-agent-bubble">等待总工程师接收任务。</div></div></article>"""


def render_waiting_message() -> str:
    return """<article class="marvis-relay-agent-step marvis-relay-waiting" data-native-role="director" data-native-kind="waiting"><span class="marvis-relay-avatar marvis-relay-avatar-marvis" aria-label="Marvis"></span><div><div class="marvis-relay-agent-head"><strong>Marvis</strong></div><div class="marvis-relay-agent-bubble">...</div></div></article>"""


def render_message(
    row: dict[str, str],
    *,
    public_role: Callable[[str], tuple[str, str]],
    render_avatar: Callable[..., str],
    role_status_label: Callable[[str], str],
    action_label: Callable[[str, dict[str, str]], str],
    render_attachment_list: Callable[[dict[str, Any]], str],
) -> str:
    role, kind, body, key = (
        str(row.get("role") or "system"),
        str(row.get("kind") or "event"),
        str(row.get("body") or ""),
        str(row.get("key") or ""),
    )
    if kind == "user_message":
        bubble_html = (
            f'<div class="marvis-relay-user-bubble" data-native-message-body>{escape(body)}</div>'
            if body
            else ""
        )
        return f'<article class="marvis-relay-user-message" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(key)}">{bubble_html}{render_attachment_list(row)}</article>'
    if kind == "waiting":
        persona, display_name = public_role("director")
        return f'<article class="marvis-relay-agent-step marvis-relay-waiting" data-native-role="director" data-native-kind="waiting" data-native-key="{escape(key)}" data-marvis-followup-waiting="true">{render_avatar(persona, label=display_name)}<div class="marvis-relay-agent-content"><div class="marvis-relay-agent-head"><strong>{escape(display_name)}</strong></div><div class="marvis-relay-agent-bubble" data-native-message-body>{escape(body or "...")}</div></div></article>'
    persona, display_name = public_role(role)
    status_label, action = (
        role_status_label(str(row.get("meta") or row.get("status") or "")),
        action_label(role, row),
    )
    final_attr = (
        f' data-conversation-role-final="{escape(role)}"' if kind == "role_envelope" else ""
    )
    stream_attr = f' data-conversation-role-stream="{escape(role)}"' if kind == "text_delta" else ""
    ids = str(row.get("preview_event_ids") or "")
    ids_attr = f' data-stream-event-ids="{escape(ids)}"' if kind == "text_delta" and ids else ""
    show_action = kind in {"role_envelope", "role_process", "text_delta"} or role == "director"
    action_html = (
        f'<span class="marvis-relay-agent-action">| {escape(action)} {escape(status_label)}</span>'
        if show_action and action
        else f'<span class="marvis-relay-agent-action">| {escape(status_label)}</span>'
        if show_action and status_label
        else ""
    )
    return f'<article class="marvis-relay-agent-step" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(key)}"{final_attr}{stream_attr}{ids_attr}>{render_avatar(persona, label=display_name)}<div class="marvis-relay-agent-content"><div class="marvis-relay-agent-head"><strong>{escape(display_name)}</strong> {action_html}</div><div class="marvis-relay-agent-bubble" data-native-message-body>{escape(body)}</div></div></article>'


def render_relay_attachment_list(
    row: dict[str, Any],
    *,
    max_images: int,
    max_files: int,
) -> str:
    """Render sanitized user attachments for one Relay conversation row."""

    image_items: list[str] = []
    file_items: list[str] = []
    for raw in list(row.get("images") or [])[:max_images]:
        if not isinstance(raw, dict):
            continue
        src = str(raw.get("url") or raw.get("data_url") or "")
        if not src.startswith(("data:image/", "http://", "https://")):
            continue
        image_items.append(
            '<img class="marvis-relay-message-image" '
            f'src="{escape(src, quote=True)}" alt="" loading="lazy">'
        )
    for raw in list(row.get("files") or [])[:max_files]:
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
    return "".join(parts)


def render_native_empty_conversation() -> str:
    return """<article class="relay-message" data-native-role="system" data-native-kind="status" data-native-empty><div class="relay-message-head"><strong>系统</strong></div><div class="relay-message-body" data-native-message-body>等待原生会话输出。</div></article>"""


def render_native_message(row: dict[str, str], *, render_avatar: Callable[..., str]) -> str:
    meta = str(row.get("meta") or "")
    meta_html = f'<span class="relay-message-meta">{escape(meta)}</span>' if meta else ""
    role, kind = str(row.get("role", "") or "system"), str(row.get("kind", "") or "event")
    final_attr = (
        f' data-conversation-role-final="{escape(role)}"' if kind == "role_envelope" else ""
    )
    stream_attr = f' data-conversation-role-stream="{escape(role)}"' if kind == "text_delta" else ""
    ids = str(row.get("preview_event_ids") or "")
    ids_attr = f' data-stream-event-ids="{escape(ids)}"' if kind == "text_delta" and ids else ""
    return f'<article class="relay-message" data-native-role="{escape(role)}" data-native-kind="{escape(kind)}" data-native-key="{escape(str(row.get("key", "") or ""))}"{final_attr}{stream_attr}{ids_attr}>{render_avatar(role, label=str(row.get("speaker", "") or "系统"))}<div class="relay-message-head"><strong>{escape(str(row.get("speaker", "") or "系统"))}</strong>{meta_html}</div><div class="relay-message-body" data-native-message-body>{escape(str(row.get("body", "") or ""))}</div></article>'
