"""Relay task-detail template, isolated from transport and route dispatch."""

from __future__ import annotations

import json
from html import escape
from typing import Any


def render_relay_task_detail_page(
    detail: Any,
    *,
    access_token: str = "",
    view: str = "conversation",
    events: list[Any] | tuple[Any, ...] | None = None,
    hub: Any = None,
    token_stats: dict[str, Any] | None = None,
    helpers: dict[str, Any],
) -> str:
    # The caller provides the semantic render helpers explicitly.  Keeping the
    # page in this module removes transport coupling without duplicating the
    # established Relay projection vocabulary during the extraction.
    _relay_task_detail_view = helpers["_relay_task_detail_view"]
    _token_suffix = helpers["_token_suffix"]
    _relay_latest_event_sequence = helpers["_relay_latest_event_sequence"]
    _relay_task_events_suffix = helpers["_relay_task_events_suffix"]
    _relay_role_canonical_payloads_by_role = helpers["_relay_role_canonical_payloads_by_role"]
    _marvis_relay_conversation_html = helpers["_marvis_relay_conversation_html"]
    _relay_role_canonical_payload_sequence = helpers["_relay_role_canonical_payload_sequence"]
    _relay_workspace_href = helpers["_relay_workspace_href"]
    _marvis_relay_topbar = helpers["_marvis_relay_topbar"]
    _relay_settings_href = helpers["_relay_settings_href"]
    _marvis_relay_bottom_nav = helpers["_marvis_relay_bottom_nav"]
    _marvis_token_int = helpers["_marvis_token_int"]
    _format_marvis_relay_token_count = helpers["_format_marvis_relay_token_count"]
    _marvis_relay_work_log_html = helpers["_marvis_relay_work_log_html"]
    _marvis_relay_work_log_body_html = helpers["_marvis_relay_work_log_body_html"]
    _marvis_relay_max_event_id_from_events = helpers["_marvis_relay_max_event_id_from_events"]
    _marvis_relay_plan_control_html = helpers["_marvis_relay_plan_control_html"]
    _marvis_relay_followup_composer = helpers["_marvis_relay_followup_composer"]
    _replace_html_icons = helpers["_replace_html_icons"]
    _relay_mobile_web_head = helpers["_relay_mobile_web_head"]
    _marvis_relay_attachment_script = helpers["_marvis_relay_attachment_script"]
    RELAY_ROLE_IDS = helpers["RELAY_ROLE_IDS"]
    _relay_role_label = helpers["_relay_role_label"]
    _marvis_relay_public_role = helpers["_marvis_relay_public_role"]
    _marvis_relay_handoff_role_label = helpers["_marvis_relay_handoff_role_label"]
    _MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS = helpers["_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS"]
    _MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS = helpers["_MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS"]
    _native_provider_display_name = helpers["_native_provider_display_name"]
    _NATIVE_APP_HEAD = helpers["_NATIVE_APP_HEAD"]
    _native_permission_presets = helpers["_native_permission_presets"]
    _codex_plugin_menu_items = helpers["_codex_plugin_menu_items"]
    _ICONS_JS_LITERAL = helpers["_ICONS_JS_LITERAL"]
    view = _relay_task_detail_view(view)
    token_suffix = _token_suffix(access_token)
    event_history = list(events or [])
    event_after = _relay_latest_event_sequence(event_history)
    events_suffix = _relay_task_events_suffix(access_token, event_after)
    canonical_payloads = _relay_role_canonical_payloads_by_role(
        getattr(detail, "artifacts", []) or []
    )
    native_conversation_html = _marvis_relay_conversation_html(
        detail.role_jobs,
        hub=hub,
        canonical_payloads=canonical_payloads,
        canonical_payload_sequence=_relay_role_canonical_payload_sequence(
            getattr(detail, "artifacts", []) or []
        ),
        artifacts=getattr(detail, "artifacts", []) or [],
    )
    task = detail.task
    back_href = _relay_workspace_href(str(task.workspace or ""), access_token)
    device_label = "wanglin的Mac mini"
    topbar_html = _marvis_relay_topbar(
        title="Marvis",
        subtitle=device_label,
        back_href=back_href,
        right_html=f"""
          <a class="marvis-relay-icon-button" href="{escape(_relay_settings_href(str(task.workspace or ""), access_token))}" aria-label="Relay设置">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
          </a>
          <button class="marvis-relay-icon-button" type="button" data-marvis-open-log aria-label="工作日志">
            <span class="marvis-relay-icon-list" aria-hidden="true"></span>
          </button>
        """,
    )
    bottom_nav_html = _marvis_relay_bottom_nav(
        "tasks",
        access_token=access_token,
        selected_workspace=str(task.workspace or ""),
    )
    token_total = _marvis_token_int((token_stats or {}).get("total_consumed_tokens"))
    token_text = _format_marvis_relay_token_count(token_total)
    work_log_html = _marvis_relay_work_log_html(
        body_html=_marvis_relay_work_log_body_html(
            detail,
            hub=hub,
            canonical_payloads=canonical_payloads,
        ),
        token_text=token_text,
        token_total=token_total,
        max_event_id=_marvis_relay_max_event_id_from_events(detail.role_jobs, hub=hub),
    )
    plan_control_html = _marvis_relay_plan_control_html(detail)
    followup_composer_html = _marvis_relay_followup_composer(
        task_id=int(task.id),
        workspace=str(task.workspace or ""),
        access_token=access_token,
        task_status=str(task.status or ""),
        current_round_id=int(getattr(detail, "current_round_id", 1) or 1),
        pending_inputs=[
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (getattr(detail, "pending_inputs", []) or [])
        ],
    )
    return _replace_html_icons(f"""<!doctype html>
<html lang="zh-CN">
<head>
{_relay_mobile_web_head(f"{task.title} · Relay")}
  <style>
    html {{ background: #f6f6f6; }}
    body {{ margin: 0; color: #111; background: #f6f6f6; }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
  </style>
</head>
<body data-relay-view="{escape(view)}" data-marvis-relay-view="{escape(view)}">
  <div class="marvis-relay-phone">
  {topbar_html}
  <main class="marvis-relay-task-main">
    <section class="relay-view relay-conversation-panel" data-view-panel="conversation" aria-label="会话">
      <div class="marvis-relay-chat-thread relay-conversation" data-native-conversation-timeline>
        {native_conversation_html}
      </div>
    </section>
  </main>
  {plan_control_html}
  {followup_composer_html}
  <nav class="marvis-relay-bottom-nav" aria-label="Marvis relay navigation">
    {bottom_nav_html}
  </nav>
  </div>
  {work_log_html}
  <script>
    {_marvis_relay_attachment_script()}
    const TASK_ID = {json.dumps(str(task.id))};
    const CURRENT_ROUND_ID = {json.dumps(str(getattr(detail, "current_round_id", 1) or 1))};
    let activeRelayRoundId = CURRENT_ROUND_ID;
    const TOKEN_SUFFIX = {json.dumps(token_suffix)};
    const EVENTS_SUFFIX = {json.dumps(events_suffix)};
    const INITIAL_PENDING_INPUTS = {
        json.dumps(
            [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in (getattr(detail, "pending_inputs", []) or [])
            ],
            ensure_ascii=False,
        )
    };
    const ROLE_LABELS = {
        json.dumps({role: _relay_role_label(role) for role in RELAY_ROLE_IDS}, ensure_ascii=False)
    };
    const MARVIS_WORK_LOG_ROLE_LABELS = {
        json.dumps(
            {role: _marvis_relay_public_role(role)[1] for role in RELAY_ROLE_IDS},
            ensure_ascii=False,
        )
    };
    const MARVIS_WORK_LOG_ROLE_PERSONAS = {
        json.dumps(
            {role: _marvis_relay_public_role(role)[0] for role in RELAY_ROLE_IDS},
            ensure_ascii=False,
        )
    };
    const MARVIS_HANDOFF_ROLE_LABELS = {
        json.dumps(
            {role: _marvis_relay_handoff_role_label(role) for role in RELAY_ROLE_IDS},
            ensure_ascii=False,
        )
    };
    const MARVIS_LEGACY_ROLE_LABEL_PARTS = {
        json.dumps(_MARVIS_RELAY_LEGACY_ROLE_LABEL_PARTS, ensure_ascii=False)
    };
    const MARVIS_LEGACY_ROLE_SLUG_PARTS = {
        json.dumps(_MARVIS_RELAY_LEGACY_ROLE_SLUG_PARTS, ensure_ascii=False)
    };
    const STATUS_LABELS = {
        json.dumps(
            {
                "idle": "未调度",
                "queued": "排队中",
                "streaming": "执行中",
                "waiting": "等待中",
                "passed": "已完成",
                "failed": "失败",
                "blocked": "阻塞",
                "interrupted": "已中断",
                "completed": "已完成",
            },
            ensure_ascii=False,
        )
    };
    const TASK_STATUS_LABELS = {
        json.dumps(
            {
                "queued": "排队中",
                "running": "进行中",
                "waiting_user": "等待你",
                "blocked": "已阻塞",
                "failed": "失败",
                "completed": "已完成",
                "interrupted": "已中断",
            },
            ensure_ascii=False,
        )
    };
    const roleStatuses = {
        json.dumps(
            {
                str(getattr(job, "role", "") or ""): str(
                    getattr(job, "status", "") or "idle"
                )
                for job in detail.role_jobs
            },
            ensure_ascii=False,
        )
    };
    const marvisRelayPhone = document.querySelector(".marvis-relay-phone");
    const marvisWorkLog = document.querySelector("[data-marvis-work-log]");
    const marvisWorkLogBackdrop = document.querySelector("[data-marvis-work-log-backdrop]");
    const marvisWorkLogDesktopQuery = window.matchMedia("(min-width: 980px)");
    const MARVIS_DIALOG_FOCUSABLE = "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
    const marvisDialogReturnFocus = new WeakMap();
    let marvisWorkLogTrigger = null;
    let marvisWorkLogCloseTimer = null;

    function marvisFocusableNodes(surface) {{
      if (!(surface instanceof HTMLElement)) return [];
      return Array.from(surface.querySelectorAll(MARVIS_DIALOG_FOCUSABLE)).filter((node) =>
        node instanceof HTMLElement
        && !node.hidden
        && !node.closest("[hidden]")
        && node.getAttribute("aria-hidden") !== "true"
      );
    }}
    function trapMarvisDialogFocus(surface, event) {{
      if (event.key !== "Tab") return;
      const nodes = marvisFocusableNodes(surface);
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {{
        event.preventDefault();
        last.focus();
      }} else if (!event.shiftKey && document.activeElement === last) {{
        event.preventDefault();
        first.focus();
      }}
    }}
    function focusMarvisDialog(surface) {{
      marvisFocusableNodes(surface)[0]?.focus({{ preventScroll: true }});
    }}
    function setMarvisSurfaceInert(surface, isInert) {{
      if (!(surface instanceof HTMLElement)) return;
      if ("inert" in surface) surface.inert = Boolean(isInert);
      if (isInert) {{
        surface.setAttribute("aria-hidden", "true");
      }} else {{
        surface.removeAttribute("aria-hidden");
      }}
    }}
    function setMarvisModalBackground(dialog, isOpen) {{
      [marvisRelayPhone, marvisWorkLog, marvisWorkLogBackdrop].forEach((surface) => {{
        if (surface instanceof HTMLElement && surface !== dialog) {{
          setMarvisSurfaceInert(surface, isOpen);
        }}
      }});
    }}
    function bindMarvisConfirmationPage(page) {{
      if (!(page instanceof HTMLElement) || page.dataset.marvisConfirmationDialogBound === "true") return;
      page.dataset.marvisConfirmationDialogBound = "true";
      page.setAttribute("role", "dialog");
      page.setAttribute("aria-modal", "true");
      page.addEventListener("keydown", (event) => {{
        if (page.hidden) return;
        if (event.key === "Escape") {{
          event.preventDefault();
          closeMarvisConfirmationPage(page);
          return;
        }}
        trapMarvisDialogFocus(page, event);
      }});
    }}
    function moveMarvisConfirmationPagesToDocumentRoot() {{
      document.querySelectorAll("[data-marvis-confirmation-page]").forEach((page) => {{
        if (!(page instanceof HTMLElement)) return;
        if (page.parentElement === marvisRelayPhone) document.body.appendChild(page);
        bindMarvisConfirmationPage(page);
      }});
    }}
    function marvisConfirmationPageFor(trigger) {{
      const control = trigger instanceof HTMLElement
        ? trigger.closest("[data-marvis-plan-control]")
        : null;
      const artifactId = control?.getAttribute("data-artifact-id") || "";
      const roundId = control?.getAttribute("data-round-id") || "";
      const pages = Array.from(document.querySelectorAll("[data-marvis-confirmation-page]"))
        .filter((page) => page instanceof HTMLElement);
      return pages.find((page) =>
        page.getAttribute("data-artifact-id") === artifactId
        && page.getAttribute("data-round-id") === roundId
      ) || pages[pages.length - 1] || null;
    }}
    function openMarvisConfirmationPage(page, trigger) {{
      if (!(page instanceof HTMLElement)) return;
      bindMarvisConfirmationPage(page);
      if (page.parentElement !== document.body) document.body.appendChild(page);
      const previouslyFocused = trigger instanceof HTMLElement
        ? trigger
        : document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      marvisDialogReturnFocus.set(page, previouslyFocused);
      page.hidden = false;
      focusMarvisDialog(page);
      setMarvisModalBackground(page, true);
      requestAnimationFrame(() => {{
        if (page.hidden) return;
        focusMarvisDialog(page);
      }});
    }}
    function closeMarvisConfirmationPage(page, options = {{}}) {{
      if (!(page instanceof HTMLElement) || page.hidden) return;
      const restoreFocus = options.restoreFocus !== false;
      const previouslyFocused = marvisDialogReturnFocus.get(page);
      marvisDialogReturnFocus.delete(page);
      page.hidden = true;
      setMarvisModalBackground(page, false);
      if (
        restoreFocus
        && previouslyFocused instanceof HTMLElement
        && previouslyFocused.isConnected
      ) {{
        requestAnimationFrame(() => {{
          if (!previouslyFocused.closest("[hidden]")) {{
            previouslyFocused.focus({{ preventScroll: true }});
          }}
        }});
      }}
    }}
    moveMarvisConfirmationPagesToDocumentRoot();

    function marvisWorkLogIsDesktop() {{
      return marvisWorkLogDesktopQuery.matches;
    }}
    function updateMarvisWorkLogSemantics() {{
      if (!(marvisWorkLog instanceof HTMLElement)) return;
      if (marvisWorkLogIsDesktop()) {{
        marvisWorkLog.removeAttribute("role");
        marvisWorkLog.removeAttribute("aria-modal");
      }} else {{
        marvisWorkLog.setAttribute("role", "dialog");
        marvisWorkLog.setAttribute("aria-modal", "true");
      }}
    }}
    function restoreMarvisWorkLogFocus() {{
      const previouslyFocused = marvisWorkLogTrigger
        || document.querySelector("[data-marvis-open-log]");
      marvisWorkLogTrigger = null;
      if (
        previouslyFocused instanceof HTMLElement
        && previouslyFocused.isConnected
      ) {{
        requestAnimationFrame(() => {{
          if (!previouslyFocused.closest("[hidden]")) {{
            previouslyFocused.focus({{ preventScroll: true }});
          }}
        }});
      }}
    }}
    function finishMarvisWorkLogClose() {{
      marvisWorkLogCloseTimer = null;
      if (!(marvisWorkLog instanceof HTMLElement)) return;
      marvisWorkLog.hidden = true;
      marvisWorkLogBackdrop?.setAttribute("hidden", "");
      setMarvisModalBackground(marvisWorkLog, false);
      restoreMarvisWorkLogFocus();
    }}
    function openMarvisWorkLog(trigger) {{
      if (!(marvisWorkLog instanceof HTMLElement)) return;
      if (marvisWorkLogCloseTimer) {{
        clearTimeout(marvisWorkLogCloseTimer);
        marvisWorkLogCloseTimer = null;
      }}
      marvisWorkLogTrigger = trigger instanceof HTMLElement
        ? trigger
        : document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      marvisWorkLog.hidden = false;
      updateMarvisWorkLogSemantics();
      if (!marvisWorkLogIsDesktop()) {{
        focusMarvisDialog(marvisWorkLog);
        setMarvisModalBackground(marvisWorkLog, true);
        marvisWorkLogBackdrop?.removeAttribute("hidden");
      }}
      requestAnimationFrame(() => {{
        if (marvisWorkLog.hidden) return;
        marvisWorkLog.classList.add("open");
        if (!marvisWorkLogIsDesktop()) {{
          marvisWorkLogBackdrop?.classList.add("visible");
          focusMarvisDialog(marvisWorkLog);
        }}
      }});
    }}
    function closeMarvisWorkLog() {{
      if (!(marvisWorkLog instanceof HTMLElement) || marvisWorkLog.hidden) return;
      if (marvisWorkLogCloseTimer) clearTimeout(marvisWorkLogCloseTimer);
      marvisWorkLog.classList.remove("open");
      marvisWorkLogBackdrop?.classList.remove("visible");
      if (marvisWorkLogIsDesktop()) {{
        finishMarvisWorkLogClose();
        return;
      }}
      marvisWorkLogCloseTimer = setTimeout(finishMarvisWorkLogClose, 240);
    }}
    function initializeMarvisWorkLog() {{
      if (!(marvisWorkLog instanceof HTMLElement)) return;
      updateMarvisWorkLogSemantics();
      if (marvisWorkLogIsDesktop()) {{
        marvisWorkLog.hidden = false;
        marvisWorkLog.classList.add("open");
      }} else {{
        marvisWorkLog.hidden = true;
        marvisWorkLog.classList.remove("open");
        marvisWorkLogBackdrop?.classList.remove("visible");
        marvisWorkLogBackdrop?.setAttribute("hidden", "");
      }}
    }}
    function syncMarvisWorkLogViewport() {{
      if (!(marvisWorkLog instanceof HTMLElement)) return;
      if (marvisWorkLogCloseTimer) {{
        clearTimeout(marvisWorkLogCloseTimer);
        marvisWorkLogCloseTimer = null;
      }}
      updateMarvisWorkLogSemantics();
      if (marvisWorkLogIsDesktop()) {{
        marvisWorkLogBackdrop?.classList.remove("visible");
        marvisWorkLogBackdrop?.setAttribute("hidden", "");
        setMarvisModalBackground(marvisWorkLog, false);
        if (!marvisWorkLog.hidden) marvisWorkLog.classList.add("open");
        return;
      }}
      if (marvisWorkLog.hidden) return;
      focusMarvisDialog(marvisWorkLog);
      setMarvisModalBackground(marvisWorkLog, true);
      marvisWorkLogBackdrop?.removeAttribute("hidden");
      requestAnimationFrame(() => {{
        if (marvisWorkLog.hidden || marvisWorkLogIsDesktop()) return;
        marvisWorkLog.classList.add("open");
        marvisWorkLogBackdrop?.classList.add("visible");
        focusMarvisDialog(marvisWorkLog);
      }});
    }}
    initializeMarvisWorkLog();
    marvisWorkLogDesktopQuery.addEventListener?.("change", syncMarvisWorkLogViewport);
    document.querySelectorAll("[data-marvis-open-log]").forEach((button) => {{
      button.addEventListener("click", () => openMarvisWorkLog(button));
    }});
    document.querySelectorAll("[data-marvis-close-log], [data-marvis-work-log-backdrop]").forEach((button) => {{
      button.addEventListener("click", closeMarvisWorkLog);
    }});
    marvisWorkLog?.addEventListener("keydown", (event) => {{
      if (marvisWorkLog.hidden) return;
      if (event.key === "Escape") {{
        event.preventDefault();
        closeMarvisWorkLog();
        return;
      }}
      if (!marvisWorkLogIsDesktop()) trapMarvisDialogFocus(marvisWorkLog, event);
    }});
    function labelForRole(role) {{
      return ROLE_LABELS[role] || role || "角色";
    }}
    function marvisHandoffRoleLabel(role) {{
      return MARVIS_HANDOFF_ROLE_LABELS[role] || labelForRole(role);
    }}
    function marvisHandoffText(fromRole, toRole) {{
      const toName = marvisHandoffRoleLabel(toRole);
      if (fromRole === "director") return `Marvis 拍了拍 ${{toName}} 说， 别等了，这就开始`;
      const fromName = marvisHandoffRoleLabel(fromRole);
      if (toRole === "auditor") return `${{fromName}}交给${{toName}}复核`;
      if (fromRole === "auditor" && toRole === "director") return `${{fromName}}交回Marvis收尾`;
      if (fromRole === "auditor") return `${{fromName}}退回${{toName}}继续处理`;
      return `${{fromName}}交给${{toName}}继续处理`;
    }}
    function marvisLegacyPersonaLabels(role) {{
      return (MARVIS_LEGACY_ROLE_LABEL_PARTS[role] || []).map((parts) => parts.join(" "));
    }}
    function relayReplaceLegacyRoleDisplayNames(text) {{
      let value = String(text || "");
      Object.keys(MARVIS_WORK_LOG_ROLE_PERSONAS).forEach((role) => {{
        const currentLabel = MARVIS_WORK_LOG_ROLE_LABELS[role] || "";
        marvisLegacyPersonaLabels(role).forEach((legacyLabel) => {{
          if (!legacyLabel || !currentLabel || legacyLabel === currentLabel) return;
          value = value.split(legacyLabel).join(currentLabel);
        }});
      }});
      Object.keys(MARVIS_LEGACY_ROLE_SLUG_PARTS).forEach((role) => {{
        const currentSlug = MARVIS_WORK_LOG_ROLE_PERSONAS[role] || "";
        (MARVIS_LEGACY_ROLE_SLUG_PARTS[role] || []).forEach((parts) => {{
          const legacySlug = parts.join("-");
          if (!legacySlug || !currentSlug || legacySlug === currentSlug) return;
          value = value.split(legacySlug).join(currentSlug);
        }});
      }});
      return value;
    }}
    function labelForStatus(status) {{
      return STATUS_LABELS[status] || status || "未知";
    }}
    function normalizeRelayPayload(raw) {{
      const source = raw && typeof raw === "object" ? raw : {{}};
      const nested = source.payload && typeof source.payload === "object" && !Array.isArray(source.payload) ? source.payload : {{}};
      const normalized = {{ ...nested }};
      Object.entries(source).forEach(([key, value]) => {{
        if (key === "payload") return;
        if (value === undefined || value === null || value === "") return;
        normalized[key] = value;
      }});
      if (!normalized.role && nested.role) normalized.role = nested.role;
      return normalized;
    }}
    function parseRelayEvent(event) {{
      updateRelayEventsCursor(event);
      return normalizeRelayPayload(JSON.parse(event.data || "{{}}"));
    }}
    const conversationTimeline = document.querySelector("[data-native-conversation-timeline]");
    const nativeTranscriptNodes = new Map();
    const nativeEnvelopeBuffers = new Map();
    const conversationUserBodies = new Set();
    const seenStreamEventKeys = new Set();
    const roleStreamBuffers = new Map();
    const hiddenProtocolStreamKeys = new Set();
    function scrollNativeConversationToEnd() {{
      if (conversationTimeline) conversationTimeline.scrollTop = conversationTimeline.scrollHeight;
    }}
    function relayNormalizeConversationText(text) {{
      return String(text || "").replace(/\\s+/g, " ").trim();
    }}
    function relayUserMessageIsRetryOrContext(text) {{
      const value = String(text || "");
      return value.includes("系统已要求当前角色重新输出合法结构化结果。") || value.includes("expected_output_envelope:") || value.includes("你刚才作为");
    }}
    function nativeEventPayload(nativeEvent) {{
      if (!nativeEvent || typeof nativeEvent !== "object") return {{}};
      if (nativeEvent.payload && typeof nativeEvent.payload === "object") return nativeEvent.payload;
      return nativeEvent;
    }}
    function nativeEventText(nativeEvent) {{
      const payload = nativeEventPayload(nativeEvent);
      const value = payload.text ?? payload.delta ?? payload.summary ?? payload.content ?? payload.message ?? payload.output ?? payload.chunk ?? "";
      return String(value || "");
    }}
    function relayExtractContextField(text, field) {{
      const prefix = `${{field}}:`;
      const lines = String(text || "").split(/\\r?\\n/);
      const labels = ["task_id:", "role:", "workspace:", "goal:", "latest_user_input:", "handoff_summaries:", "constraints:", "expected_output_envelope:"];
      for (let index = 0; index < lines.length; index += 1) {{
        const line = lines[index].trimEnd();
        if (!line.startsWith(prefix)) continue;
        const inline = line.slice(prefix.length).trim();
        if (inline) return inline;
        const collected = [];
        for (const nextLine of lines.slice(index + 1)) {{
          const trimmed = nextLine.trim();
          if (labels.some((label) => trimmed.startsWith(label))) break;
          if (trimmed) collected.push(trimmed);
        }}
        return collected.join("\\n").trim();
      }}
      return "";
    }}
    function relayHumanizeUserMessage(text) {{
      const value = String(text || "");
      if (value.includes("你刚才作为") && value.includes("expected_output_envelope:")) {{
        return "";
      }}
      if (!value.includes("latest_user_input:") && !value.includes("expected_output_envelope:")) return value;
      return relayExtractContextField(value, "latest_user_input") || relayExtractContextField(value, "goal") || value;
    }}
    function relayTextLooksLikeEnvelope(text) {{
      const value = String(text || "").trim();
      if (!value.startsWith("{{")) return false;
      if (/"artifact_type"\\s*:\\s*"(?:routing_decision|role_envelope|final_summary|architecture_plan|implementation_report|audit_report|test_report|followup_response)"/.test(value)) return true;
      return [
        "relay_role",
        "routing_decision",
        "acceptance_criteria",
        "handoff_to",
        "required_roles",
        "open_questions",
        "next_action",
      ].some((marker) => value.includes(marker));
    }}
    function relayTextHasProtocolFragmentShape(text) {{
      const value = String(text || "").trim();
      return value.includes("{{")
        || value.includes("}}")
        || value.includes('",')
        || value.includes('":')
        || value.includes('\\\\\"')
        || value.includes("],")
        || value.startsWith('"')
        || value.startsWith("[");
    }}
    function marvisConversationTextIsProtocolNoise(text) {{
      const value = String(text || "").trim();
      if (!value) return true;
      if (relayTextLooksLikeEnvelope(value)) return true;
      const artifactTypePattern = /"artifact_type"\\s*:\\s*"(?:routing_decision|role_envelope|final_summary|architecture_plan|implementation_report|audit_report|followup_response)"/;
      if (artifactTypePattern.test(value)) return true;
      if (!relayTextHasProtocolFragmentShape(value)) return false;
      const markers = [
        "relay_role",
        "routing_decision",
        "role_envelope",
        "final_summary",
        "confirmation_options",
        "evidence_refs",
        "handoff_to",
        "required_roles",
        "next_action",
        "open_questions",
        "acceptance_criteria",
        "status",
        "summary",
        "reason",
        "role",
      ];
      const matched = markers.filter((marker) => value.includes(marker));
      if (matched.length >= 2) return true;
      const markerSet = new Set(matched);
      if (markerSet.has("evidence_refs") && markerSet.has("handoff_to")) return true;
      if (markerSet.has("final_summary") && markerSet.has("confirmation_options")) return true;
      if (markerSet.has("acceptance_criteria")) return true;
      if (markerSet.has("confirmation_options")) return true;
      if (markerSet.has("required_roles")) return true;
      if (markerSet.has("handoff_to")) return true;
      if (markerSet.has("next_action")) return true;
      return false;
    }}
    function marvisConversationTextIsPotentialProtocolPrefix(text) {{
      const value = String(text || "").trim();
      if (!value) return false;
      if (!relayTextHasProtocolFragmentShape(value)) return false;
      if (value.startsWith("{{")) return true;
      if (value.startsWith("[") && !/[\\u4e00-\\u9fff]/.test(value)) return true;
      if (value.startsWith('"')) {{
        const compact = value.replace(/\\s+/g, "");
        if (/^"?[A-Za-z_]*$/.test(compact)) return true;
        if (compact.includes('":') || compact.includes('":["') || compact.includes('",')) return true;
        if (!/[\\u4e00-\\u9fff]/.test(value) && compact.length < 240) return true;
      }}
      return false;
    }}
    function marvisConversationTextIsStructuredArtifactPlaceholder(text) {{
      const value = String(text || "").trim();
      if (!value) return true;
      if (marvisConversationTextIsProtocolNoise(value)) return true;
      if (value.startsWith("{{") && (relayParseEnvelope(value) || relayTextLooksLikeEnvelope(value))) return true;
      return [
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
      ].some((marker) => value.includes(marker));
    }}
    function relayDictLooksLikeEnvelope(payload) {{
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
      const artifactType = String(payload.artifact_type || "");
      if ([
        "routing_decision",
        "role_envelope",
        "final_summary",
        "architecture_plan",
        "implementation_report",
        "audit_report",
        "test_report",
        "followup_response",
      ].includes(artifactType)) return true;
      return [
        "relay_role",
        "next_action",
        "open_questions",
        "required_roles",
        "acceptance_criteria",
        "handoff_to",
        "status",
      ].some((key) => Object.prototype.hasOwnProperty.call(payload, key));
    }}
    function relayParseEnvelope(text) {{
      const value = String(text || "").trim();
      try {{
        const parsed = JSON.parse(value);
        return relayDictLooksLikeEnvelope(parsed) ? parsed : null;
      }} catch (_error) {{
        for (let index = 0; index < value.length; index += 1) {{
          if (value[index] !== "{{") continue;
          try {{
            const parsed = JSON.parse(value.slice(index));
            return relayDictLooksLikeEnvelope(parsed) ? parsed : null;
          }} catch (_nestedError) {{
            continue;
          }}
        }}
      }}
      return null;
    }}
    function relayJoinTextList(value) {{
      if (Array.isArray(value)) return value.map((item) => String(item || "").trim()).filter(Boolean).join("；");
      return String(value || "").trim();
    }}
    function relayRouteLabel(route) {{
      const labels = {{
        director_only: "总工程师直接完成",
        core_relay: "核心接力",
        full_relay: "完整五角色接力",
        audit_first: "审计优先",
        waiting_user: "等待用户确认",
        blocked: "已阻塞",
      }};
      return labels[route] || route || "";
    }}
    function relayTextNeedsChineseFallback(text) {{
      const value = relayReplaceLegacyRoleDisplayNames(text).trim();
      if (!/[A-Za-z]{{3,}}/.test(value)) return false;
      if (!/[一-龥]/.test(value)) return true;
      return /[A-Za-z]{{3,}}(?:[ -]+[A-Za-z]{{2,}}){{1,}}/.test(value);
    }}
    function relayHumanizeDisplayText(text, englishFallback = "") {{
      let value = relayReplaceLegacyRoleDisplayNames(text);
      const replacements = [
        [/路由为director_only/g, "由总工程师直接处理"],
        [/director_only/g, "总工程师直接处理"],
        [/core_relay/g, "核心角色接力"],
        [/full_relay/g, "五角色完整接力"],
        [/audit_first/g, "先审计再推进"],
        [/waiting_user/g, "等待你补充"],
        [/complete directly after routing by checking current market sources and returning the latest available gold price/g, "由总工程师核验最新行情来源并给出结果"],
        [/complete directly after routing/g, "由总工程师直接处理"],
        [/complete directly/g, "直接处理"],
        [/dispatch next role/g, "交给下一位角色处理"],
      ];
      replacements.forEach(([pattern, label]) => {{
        value = value.replace(pattern, label);
      }});
      if (englishFallback && relayTextNeedsChineseFallback(value)) return englishFallback;
      return value;
    }}
    function relaySanitizeProtocolLeakText(role, text) {{
      let value = relayHumanizeDisplayText(text);
      const sentinel = "原始结构化输出不在主会话展示。";
      if (value.includes(sentinel)) return value.split(sentinel, 1)[0] + sentinel;
      const markers = ["artifact_type", "expected_output_envelope", "routing_decisioncomplexity", "required_roles", "handoff_to"];
      if (markers.some((marker) => value.includes(marker)) && value.includes("{{")) {{
        return "";
      }}
      return value;
    }}
    const marvisWorkLogBody = document.querySelector("[data-marvis-work-log-body]");
    const marvisWorkLogTokenValue = document.querySelector("[data-marvis-work-log-token-value]");
    const marvisWorkLogSeenRuntimeIds = new Set();
    const marvisWorkLogInitialMaxEventId = Number(marvisWorkLog?.dataset.marvisWorkLogMaxEventId || "0");
    let marvisWorkLogTokenTotal = Number(marvisWorkLogTokenValue?.dataset.tokenTotal || "0");
    function marvisWorkLogRoleLabel(role) {{
      return MARVIS_WORK_LOG_ROLE_LABELS[role] || labelForRole(role) || "Marvis";
    }}
    function marvisWorkLogRolePersona(role) {{
      return MARVIS_WORK_LOG_ROLE_PERSONAS[role] || "marvis";
    }}
    function marvisWorkLogFormatTokens(value) {{
      const count = Math.max(0, Math.round(Number(value || 0)));
      if (count >= 1000) return `${{Math.round(count / 1000)}}K`;
      return String(count);
    }}
    function marvisWorkLogUsageTotal(nativeEvent) {{
      const payload = nativeEventPayload(nativeEvent);
      const usage = payload.usage && typeof payload.usage === "object" ? payload.usage : null;
      const total = payload.total && typeof payload.total === "object" ? payload.total : null;
      const candidates = [payload];
      if (usage) {{
        candidates.push(usage);
        if (usage.total && typeof usage.total === "object") candidates.push(usage.total);
      }}
      if (total) candidates.push(total);
      for (const candidate of candidates) {{
        for (const key of ["total_tokens", "tokens", "consumed_tokens"]) {{
          const value = Number(candidate[key] || 0);
          if (Number.isFinite(value) && value > 0) return Math.round(value);
        }}
      }}
      for (const candidate of candidates) {{
        let subtotal = 0;
        for (const key of ["input_tokens", "output_tokens", "reasoning_output_tokens"]) {{
          const value = Number(candidate[key] || 0);
          if (Number.isFinite(value) && value > 0) subtotal += Math.round(value);
        }}
        if (subtotal > 0) return subtotal;
      }}
      return 0;
    }}
    function updateMarvisWorkLogTokenTotal(nativeEvent, runtimeEventId = "") {{
      const numericId = Number(runtimeEventId || nativeEvent?.id || 0);
      if (numericId && numericId <= marvisWorkLogInitialMaxEventId) return;
      const usageTotal = marvisWorkLogUsageTotal(nativeEvent);
      if (!usageTotal || !marvisWorkLogTokenValue) return;
      marvisWorkLogTokenTotal += usageTotal;
      marvisWorkLogTokenValue.dataset.tokenTotal = String(marvisWorkLogTokenTotal);
      marvisWorkLogTokenValue.textContent = `${{marvisWorkLogFormatTokens(marvisWorkLogTokenTotal)}} ☕`;
    }}
    function marvisWorkLogNativeKind(nativeEvent) {{
      if (nativeEvent?.kind) return nativeEvent.kind;
      const type = nativeEvent?.type || "";
      const map = {{
        "model.text.delta": "text_delta",
        "model.message.completed": "message_completed",
        "model.reasoning.delta": "reasoning_delta",
        "model.usage.updated": "usage_updated",
        "tool.call.started": "tool_call_started",
        "tool.call.progress": "tool_call_progress",
        "tool.call.completed": "tool_call_completed",
        "tool.call.failed": "tool_call_failed",
        "command.started": "command_started",
        "command.output.delta": "command_output",
        "command.completed": "command_completed",
        "command.failed": "command_failed",
        "file.changed": "file_changed",
        "diff.updated": "diff_updated",
        "approval.requested": "approval_requested",
        "approval.resolved": "approval_resolved",
        "agent.run.activity": "activity",
        "agent.run.started": "lifecycle",
        "agent.run.heartbeat": "lifecycle",
        "agent.run.completed": "completed",
        "agent.run.failed": "failed",
      }};
      return map[type] || type || "event";
    }}
    function marvisWorkLogEventStatus(kind) {{
      if (kind.endsWith("_completed") || kind === "completed") return "已完成";
      if (kind.endsWith("_failed") || kind === "failed") return "调用失败";
      return "进行中";
    }}
    function marvisWorkLogToolLabel(payload) {{
      for (const key of ["tool_name", "name", "tool", "display_name", "action"]) {{
        const value = String(payload[key] || "").trim();
        if (value) return value;
      }}
      return "";
    }}
    function marvisWorkLogCommandLabel(payload) {{
      const command = payload.command;
      if (Array.isArray(command)) {{
        const parts = command.map(String).filter((part) => part.trim());
        if (parts.length >= 2 && parts[1] === "executor") return `${{parts[0]}} executor`;
        if (parts.length >= 3 && ["bash", "sh", "zsh"].includes(parts[0].split(/[\\/]/).pop()) && ["-c", "-lc"].includes(parts[1])) {{
          return marvisWorkLogCommandLabelFromText(parts[2]);
        }}
        return parts.length ? parts[0].split(/[\\/]/).pop() : "";
      }}
      const value = String(command || payload.cmd || payload.name || "").trim();
      return marvisWorkLogCommandLabelFromText(value);
    }}
    function marvisWorkLogCommandLabelFromText(value) {{
      value = String(value || "").trim();
      if (!value) return "";
      if (value.endsWith(" executor")) return value;
      const parts = value.match(/(?:[^\\s"']+|"[^"]*"|'[^']*')+/g) || [];
      if (parts.length >= 3) {{
        const shell = parts[0].replace(/^["']|["']$/g, "").split(/[\\/]/).pop();
        const flag = parts[1].replace(/^["']|["']$/g, "");
        if (["bash", "sh", "zsh"].includes(shell) && ["-c", "-lc"].includes(flag)) {{
          return marvisWorkLogCommandLabelFromText(parts[2].replace(/^["']|["']$/g, ""));
        }}
      }}
      return parts.length ? parts[0].replace(/^["']|["']$/g, "").split(/[\\/]/).pop() : value;
    }}
    function marvisWorkLogOutputText(payload) {{
      for (const key of ["output", "stderr", "stdout", "result", "message", "error", "delta", "chunk"]) {{
        const value = payload[key];
        if (value === undefined || value === null) continue;
        if (typeof value === "object") return JSON.stringify(value, null, 2);
        const text = String(value || "").trim();
        if (text) return text;
      }}
      return "";
    }}
    function marvisWorkLogStableKey(prefix, nativeEvent, label) {{
      const payload = nativeEventPayload(nativeEvent);
      const stable = payload.itemId || payload.item_id || payload.call_id || payload.tool_call_id || payload.message_id || payload.native_message_id || payload.native_turn_id || payload.turnId || label || nativeEvent?.id || `${{Date.now()}}:${{Math.random()}}`;
      return `${{prefix}}:${{stable}}`;
    }}
    function marvisWorkLogTextIsProtocolNoise(text) {{
      const value = String(text || "").trim();
      if (!value) return true;
      return marvisConversationTextIsProtocolNoise(value);
    }}
    function marvisWorkLogCleanProtocolSummary(text) {{
      const value = String(text || "").trim();
      if (!value) return "";
      if (!marvisWorkLogTextIsProtocolNoise(value)) return value;
      let source = value;
      let match = source.match(/"summary"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"/);
      if (!match && source.includes('\\\\"summary\\\\"')) {{
        source = source.replace(/\\\\"/g, '"');
        match = source.match(/"summary"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"/);
      }}
      if (!match) return "";
      let cleaned = match[1] || "";
      try {{
        cleaned = JSON.parse(`"${{cleaned}}"`);
      }} catch (_error) {{}}
      cleaned = String(cleaned || "").trim();
      return cleaned && !marvisWorkLogTextIsProtocolNoise(cleaned) ? cleaned : "";
    }}
    function marvisWorkLogProtocolArchiveText(text) {{
      const value = String(text || "").trim();
      if (!value) return "结构化片段已归档";
      if (value.includes("artifact_type")) return "结构化产物已归档";
      const cleaned = marvisWorkLogCleanProtocolSummary(value);
      if (cleaned) return cleaned;
      const markers = [
        "final_summary",
        "routing_decision",
        "role_envelope",
        "confirmation_options",
        "evidence_refs",
        "handoff_to",
        "required_roles",
        "next_action",
        "status",
      ].filter((marker) => value.includes(marker));
      if (markers.length) return `结构化片段已归档：${{markers.slice(0, 4).join("、")}}`;
      return "结构化片段已归档";
    }}
    function marvisWorkLogShouldFoldText(text) {{
      const value = String(text || "");
      if (marvisWorkLogLooksLikeAgentDump(value)) return true;
      if (value.length > 600) return true;
      if (value.includes("```")) return true;
      const stripped = value.trimStart();
      if ((stripped.startsWith("{{") || stripped.startsWith("[")) && value.length > 240) return true;
      if (/<(?:!doctype|html|body|script|style|pre|div|section)\\b/i.test(value)) return true;
      return value.split(/\\r?\\n/).some((line) => line.length > 220);
    }}
    function marvisWorkLogTextLooksLikeMachineOutput(text) {{
      const value = String(text || "").trim();
      if (!value) return false;
      const normalized = value.replace(/\\\\r\\\\n/g, "\\n").replace(/\\\\n/g, "\\n");
      if (/^(?:Task not found|Found \\d+ files?|No files found)\\b/im.test(normalized)) return true;
      const lines = normalized.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);
      if (!lines.length) return false;
      if (lines.length === 1) {{
        const line = lines[0];
        return /^\\d{{2,}}[:\\t ]+\\S/.test(line) || /^(?:[A-Za-z0-9_.-]+\\/)+[A-Za-z0-9_.-]+$/.test(line);
      }}
      let pathLike = 0;
      let lineHitLike = 0;
      let codeLike = 0;
      for (const line of lines) {{
        if (/^(?:\\/[^:\\s]+|(?:[A-Za-z0-9_.-]+\\/)+[A-Za-z0-9_.-]+)$/.test(line)) pathLike += 1;
        if (/^(?:\\d{{2,}}|[^:\\s]+:\\d{{1,5}})[:\\t ]+\\S/.test(line)) lineHitLike += 1;
        if (/\\b(?:def|class|const|let|var|return|import|from)\\b|[{{}}();=]/.test(line)) codeLike += 1;
      }}
      if (pathLike >= 2 || lineHitLike >= 2) return true;
      return lines.length >= 4 && (pathLike + lineHitLike + codeLike) / lines.length >= 0.5;
    }}
    function marvisWorkLogLooksLikeAgentDump(text) {{
      const value = String(text || "").trim();
      if (!value) return false;
      if (marvisWorkLogTextLooksLikeMachineOutput(value)) return true;
      return /\\bThe file\\b.+\\bhas been updated successfully\\b/i.test(value)
        || /\\b(?:All changes are in place|No matches found|Found \\d+ files?|Task not found)\\b/i.test(value)
        || /位于分支|尚未暂存|修改尚未加入提交|未跟踪的文件/.test(value);
    }}
    function marvisWorkLogCompactText(text) {{
      const value = String(text || "").trim();
      if (!value) return {{ text: "", output: "", chip: "" }};
      if (!marvisWorkLogShouldFoldText(value)) return {{ text: value, output: "", chip: "" }};
      return {{ text: "", output: value, chip: "过程输出 已折叠" }};
    }}
    function marvisWorkLogEntryFromNativeEvent(role, nativeEvent) {{
      const kind = marvisWorkLogNativeKind(nativeEvent);
      const type = nativeEvent?.type || "";
      const payload = nativeEventPayload(nativeEvent);
      if (kind === "usage_updated" || type === "model.usage.updated") return {{ usage: true }};
      if (kind === "user_message" || kind === "reasoning_delta") return null;
      if (kind === "text_delta" || kind === "message_completed") {{
        const text = nativeEventText(nativeEvent);
        const key = marvisWorkLogStableKey(`message:${{role || ""}}`, nativeEvent, "assistant");
        if (marvisWorkLogTextIsProtocolNoise(text)) {{
          return {{
            kind: "message",
            key,
            text: marvisWorkLogProtocolArchiveText(text),
            chip: "结构化片段 已归档",
            replaceText: kind === "message_completed",
          }};
        }}
        const compact = marvisWorkLogCompactText(text);
        return {{
          kind: "message",
          key,
          text: relaySanitizeProtocolLeakText(role, compact.text),
          chip: compact.chip,
          output: compact.output,
          replaceText: kind === "message_completed",
        }};
      }}
      if (kind.startsWith("tool_call")) {{
        const label = marvisWorkLogToolLabel(payload) || "tool";
        return {{
          kind: "tool",
          key: marvisWorkLogStableKey("tool", nativeEvent, label),
          chip: `${{label}} ${{marvisWorkLogEventStatus(kind)}}`,
          output: marvisWorkLogOutputText(payload),
          failed: kind.endsWith("_failed"),
        }};
      }}
      if (kind.startsWith("command")) {{
        const label = marvisWorkLogCommandLabel(payload) || "command";
        return {{
          kind: "command",
          key: marvisWorkLogStableKey("command", nativeEvent, label),
          chip: `${{label}} ${{marvisWorkLogEventStatus(kind)}}`,
          output: marvisWorkLogOutputText(payload),
          failed: kind.endsWith("_failed"),
        }};
      }}
      if (kind === "approval_requested" || kind === "approval_resolved") {{
        return {{
          kind: "approval",
          key: marvisWorkLogStableKey("approval", nativeEvent, kind),
          chip: kind === "approval_requested" ? "等待审批" : "审批已处理",
          text: nativeEventText(nativeEvent),
        }};
      }}
      if (kind === "file_changed" || kind === "diff_updated") {{
        const label = kind === "file_changed" ? "文件变更" : "差异更新";
        const fileText = payload.path || payload.file || payload.filename || payload.summary || payload.message || "";
        return {{
          kind: "file",
          key: marvisWorkLogStableKey("file", nativeEvent, label),
          chip: `${{label}} 已记录`,
          text: String(fileText || ""),
        }};
      }}
      if (["activity", "lifecycle", "completed", "failed"].includes(kind)) {{
        let text = nativeEventText(nativeEvent);
        if (!text && kind === "failed") text = String(payload.error || payload.reason || payload.status || "调用失败");
        if (!text) return null;
        return {{
          kind,
          key: marvisWorkLogStableKey(kind, nativeEvent, kind),
          text,
          chip: kind === "failed" ? "调用失败" : "",
          failed: kind === "failed",
        }};
      }}
      return null;
    }}
    function createMarvisWorkLogAvatar(role) {{
      const avatar = document.createElement("span");
      avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{marvisWorkLogRolePersona(role)}}`;
      avatar.setAttribute("aria-label", marvisWorkLogRoleLabel(role));
      return avatar;
    }}
    function ensureMarvisWorkLogSegment(role) {{
      if (!marvisWorkLogBody) return null;
      const empty = marvisWorkLogBody.querySelector(".marvis-work-log-empty");
      if (empty) empty.remove();
      const segments = Array.from(marvisWorkLogBody.querySelectorAll("[data-marvis-work-log-segment]"));
      const lastSegment = segments[segments.length - 1];
      if (lastSegment && lastSegment.dataset.marvisWorkLogSegment === role) return lastSegment;
      const section = document.createElement("section");
      section.className = "marvis-work-log-role marvis-work-log-segment";
      section.dataset.marvisWorkLogRole = role || "";
      section.dataset.marvisWorkLogSegment = role || "";
      section.dataset.marvisWorkLogSegmentIndex = String(segments.length);
      const main = document.createElement("div");
      main.className = "marvis-work-log-role-main";
      const title = document.createElement("h3");
      title.textContent = marvisWorkLogRoleLabel(role);
      const line = document.createElement("div");
      line.className = "marvis-work-log-line";
      main.append(title, line);
      section.append(createMarvisWorkLogAvatar(role), main);
      marvisWorkLogBody.appendChild(section);
      return section;
    }}
    function renderMarvisWorkLogEntry(segment, entry) {{
      if (!entry || entry.usage) return;
      if (entry.removeKey) {{
        document.querySelectorAll(`[data-marvis-work-log-entry-key="${{CSS.escape(entry.removeKey)}}"]`).forEach((node) => node.remove());
        return;
      }}
      if (!segment) return;
      const line = segment.querySelector(".marvis-work-log-line");
      if (!line) return;
      let node = entry.key ? line.querySelector(`[data-marvis-work-log-entry-key="${{CSS.escape(entry.key)}}"]`) : null;
      if (!node) {{
        node = document.createElement("div");
        node.className = "marvis-work-log-entry";
        node.dataset.marvisWorkLogEntry = entry.kind || "event";
        if (entry.key) node.dataset.marvisWorkLogEntryKey = entry.key;
        const paragraph = document.createElement("p");
        node.appendChild(paragraph);
        line.appendChild(node);
      }}
      node.classList.toggle("is-failed", Boolean(entry.failed));
      if (entry.chip) {{
        const chipText = relayReplaceLegacyRoleDisplayNames(entry.chip);
        let chip = node.querySelector(".marvis-work-log-tool-chip");
        if (!chip) {{
          chip = document.createElement("span");
          chip.className = "marvis-work-log-tool-chip";
          node.insertBefore(chip, node.firstChild);
        }}
        chip.textContent = chipText;
      }}
      const paragraph = node.querySelector("p") || node.appendChild(document.createElement("p"));
      if (entry.text) {{
        const entryText = relayReplaceLegacyRoleDisplayNames(entry.text);
        paragraph.textContent = entry.replaceText ? entryText : `${{paragraph.textContent || ""}}${{entryText}}`;
        const cleaned = marvisWorkLogCleanProtocolSummary(paragraph.textContent);
        if (cleaned && cleaned !== paragraph.textContent) {{
          paragraph.textContent = cleaned;
        }} else if (entry.replaceText && marvisWorkLogTextIsProtocolNoise(paragraph.textContent)) {{
          node.remove();
          return;
        }}
      }}
      if (entry.output) {{
        let details = node.querySelector("[data-marvis-work-log-output]");
        if (!details) {{
          details = document.createElement("details");
          details.className = "marvis-work-log-output";
          details.dataset.marvisWorkLogOutput = "";
          const summary = document.createElement("summary");
          summary.textContent = "查看输出";
          const pre = document.createElement("pre");
          details.append(summary, pre);
          node.appendChild(details);
        }}
        const pre = details.querySelector("pre");
        const entryOutput = relayReplaceLegacyRoleDisplayNames(entry.output);
        if (pre && !pre.textContent.includes(entryOutput)) {{
          pre.textContent = pre.textContent ? `${{pre.textContent}}\\n${{entryOutput}}` : entryOutput;
        }}
      }}
      compactMarvisWorkLogSegment(segment);
      marvisWorkLogBody?.scrollTo({{ top: marvisWorkLogBody.scrollHeight, behavior: "smooth" }});
    }}
    function marvisWorkLogToolCategory(chipText, kind) {{
      const command = String(chipText || "").trim().split(/\\s+/, 1)[0].toLowerCase();
      if (["rg", "grep", "find", "fd", "ag"].includes(command)) return "检索";
      if (["sed", "nl", "cat", "head", "tail", "less"].includes(command)) return "读取";
      if (command === "git") return "检查变更";
      if (["pytest", "unittest", "coverage"].includes(command)) return "测试";
      if (["node", "npm", "npx", "pnpm", "yarn"].includes(command)) return "前端工具";
      if (["sqlite3", "psql", "mysql"].includes(command)) return "查询状态";
      if (kind === "file") return "文件变更";
      return "工具";
    }}
    function marvisWorkLogReadToolCounts(value) {{
      try {{
        const parsed = JSON.parse(String(value || "{{}}"));
        return new Map(Object.entries(parsed).map(([label, count]) => [label, Number(count) || 0]));
      }} catch (_error) {{
        return new Map();
      }}
    }}
    function marvisWorkLogWriteToolCounts(counts) {{
      return JSON.stringify(Object.fromEntries(Array.from(counts.entries())));
    }}
    function compactMarvisWorkLogSegment(segment) {{
      if (!segment) return;
      const line = segment.querySelector(".marvis-work-log-line");
      if (!line) return;
      const toolNodes = Array.from(line.querySelectorAll('[data-marvis-work-log-entry="command"], [data-marvis-work-log-entry="tool"], [data-marvis-work-log-entry="file"]'));
      const role = segment.dataset.marvisWorkLogSegment || "";
      let batch = line.querySelector(`[data-marvis-work-log-entry-key="${{CSS.escape(`tool-batch:${{role}}`)}}"]`);
      if (!batch && toolNodes.length < 4) return;
      if (batch && !toolNodes.length) return;
      const counts = new Map();
      const outputParts = [];
      let failed = false;
      for (const node of toolNodes) {{
        const chipText = node.querySelector(".marvis-work-log-tool-chip")?.textContent || node.dataset.marvisWorkLogEntry || "";
        const kind = node.dataset.marvisWorkLogEntry || "";
        const category = marvisWorkLogToolCategory(chipText, kind);
        counts.set(category, (counts.get(category) || 0) + 1);
        const body = node.querySelector("[data-marvis-work-log-output] pre")?.textContent || node.querySelector("p")?.textContent || "";
        outputParts.push(body ? `${{chipText}}\\n${{body}}` : chipText);
        failed = failed || node.classList.contains("is-failed");
      }}
      if (!batch) {{
        batch = document.createElement("div");
        batch.className = "marvis-work-log-entry";
        batch.dataset.marvisWorkLogEntry = "tool_batch";
        batch.dataset.marvisWorkLogEntryKey = `tool-batch:${{role}}`;
        batch.appendChild(document.createElement("p"));
        line.insertBefore(batch, toolNodes[0]);
      }}
      const totalCounts = marvisWorkLogReadToolCounts(batch.dataset.marvisWorkLogToolCounts);
      for (const [label, count] of counts.entries()) {{
        totalCounts.set(label, (totalCounts.get(label) || 0) + count);
      }}
      const previousCount = Number(batch.dataset.marvisWorkLogToolCount || "0") || 0;
      const totalCount = previousCount + toolNodes.length;
      batch.dataset.marvisWorkLogToolCount = String(totalCount);
      batch.dataset.marvisWorkLogToolCounts = marvisWorkLogWriteToolCounts(totalCounts);
      batch.classList.toggle("is-failed", batch.classList.contains("is-failed") || failed);
      let chip = batch.querySelector(".marvis-work-log-tool-chip");
      if (!chip) {{
        chip = document.createElement("span");
        chip.className = "marvis-work-log-tool-chip";
        batch.insertBefore(chip, batch.firstChild);
      }}
      chip.textContent = `工具调用 ${{totalCount}} 次`;
      const paragraph = batch.querySelector("p") || batch.appendChild(document.createElement("p"));
      paragraph.textContent = `${{Array.from(totalCounts.entries()).map(([label, count]) => `${{label}} ${{count}} 次`).join("、")}}。原始输出已折叠。`;
      let details = batch.querySelector("[data-marvis-work-log-output]");
      if (!details) {{
        details = document.createElement("details");
        details.className = "marvis-work-log-output";
        details.dataset.marvisWorkLogOutput = "";
        const summary = document.createElement("summary");
        summary.textContent = "查看输出";
        const pre = document.createElement("pre");
        details.append(summary, pre);
        batch.appendChild(details);
      }}
      const pre = details.querySelector("pre");
      if (pre) {{
        const existingOutput = pre.textContent || "";
        const newOutput = outputParts.join("\\n\\n");
        pre.textContent = existingOutput && newOutput ? `${{existingOutput}}\\n\\n${{newOutput}}` : (existingOutput || newOutput);
      }}
      toolNodes.forEach((node) => node.remove());
    }}
    function renderMarvisWorkLogNativeEvent(role, nativeEvent, runtimeEventId = "") {{
      if (!marvisWorkLogBody || !nativeEvent) return;
      const numericId = Number(runtimeEventId || nativeEvent?.id || 0);
      if (numericId && numericId <= marvisWorkLogInitialMaxEventId) return;
      if (numericId && marvisWorkLogSeenRuntimeIds.has(numericId)) return;
      if (numericId) marvisWorkLogSeenRuntimeIds.add(numericId);
      const entry = marvisWorkLogEntryFromNativeEvent(role, nativeEvent);
      if (!entry) return;
      if (entry.usage) {{
        updateMarvisWorkLogTokenTotal(nativeEvent, runtimeEventId);
        return;
      }}
      if (entry.removeKey) {{
        renderMarvisWorkLogEntry(null, entry);
        return;
      }}
      const segment = ensureMarvisWorkLogSegment(role || "");
      renderMarvisWorkLogEntry(segment, entry);
    }}
    function marvisInterruptPayloadFromTypedEvent(payload = {{}}) {{
      const typedEvent = payload?.marvis_event || {{}};
      if (typedEvent.event_type !== "marvis.interrupt.requested") return payload || {{}};
      const metadata = typedEvent.metadata || {{}};
      const normalized = {{ ...(payload || {{}}) }};
      normalized.role = typedEvent.role || metadata.role || normalized.role || "director";
      normalized.summary = typedEvent.body || normalized.summary || normalized.next_action || "";
      normalized.artifact_type = metadata.artifact_type || normalized.artifact_type || "";
      normalized.open_questions = Array.isArray(metadata.open_questions)
        ? metadata.open_questions
        : (Array.isArray(normalized.open_questions) ? normalized.open_questions : []);
      normalized.round_id = metadata.round_id || normalized.round_id || "";
      normalized.marvis_event = typedEvent;
      return normalized;
    }}
    function marvisStateProjectionFromTypedEvent(payload = {{}}) {{
      const typedEvent = payload?.marvis_event || {{}};
      const metadata = typedEvent.metadata || {{}};
      const state = metadata.marvis_relay_state && typeof metadata.marvis_relay_state === "object"
        ? metadata.marvis_relay_state
        : (payload.marvis_relay_state && typeof payload.marvis_relay_state === "object" ? payload.marvis_relay_state : {{}});
      const transition = metadata.relay_transition && typeof metadata.relay_transition === "object"
        ? metadata.relay_transition
        : (payload.relay_transition && typeof payload.relay_transition === "object" ? payload.relay_transition : {{}});
      return {{ typedEvent, metadata, state, transition }};
    }}
    function applyMarvisRelayStateProjection(payload = {{}}) {{
      const projection = marvisStateProjectionFromTypedEvent(payload);
      const state = projection.state || {{}};
      if (!Object.keys(state).length) return false;
      if (state.round_id) {{
        activeRelayRoundId = String(state.round_id);
        if (followupComposer) followupComposer.dataset.currentRoundId = activeRelayRoundId;
      }}
      if (state.terminal_status) updateTaskStatus(state.terminal_status);
      else if (state.current_node === "waiting_user") updateTaskStatus("waiting_user");
      else if (state.current_node) updateTaskStatus("running");
      Object.entries(state.role_statuses || {{}}).forEach(([role, status]) => {{
        setRoleStatus(role, status, {{ force: true }});
      }});
      return true;
    }}
    function renderMarvisWorkLogConfirmation(payload = {{}}) {{
      if (!marvisWorkLogBody || !payload) return;
      const role = payload.role || "director";
      const source = String(payload.confirmation_source || "");
      const sourceLabel = confirmationSourceLabel(payload);
      const kind = String(payload.confirmation_kind || "relay_question");
      const requestId = String(payload.provider_request_id || "");
      const waitingReason = String(payload.waiting_reason || "");
      const summary = String(payload.summary || payload.next_action || "当前接力需要用户确认。").trim();
      const chip = source === "provider_native_approval"
        ? `${{sourceLabel}} · ${{kind}}`
        : `${{sourceLabel}} · 等待用户`;
      const textParts = [summary];
      if (requestId) textParts.push(`请求 ID：${{requestId}}`);
      if (waitingReason) textParts.push(`等待原因：${{waitingReason}}`);
      const segment = ensureMarvisWorkLogSegment(role);
      renderMarvisWorkLogEntry(segment, {{
        kind: "confirmation",
        key: `confirmation:${{payload.round_id || ""}}:${{role}}:${{source}}:${{requestId || payload.artifact_id || ""}}`,
        chip,
        text: textParts.filter(Boolean).join("\\n"),
        replaceText: true,
      }});
    }}
    function relayRiskLabel(risk) {{
      const labels = {{ low: "低", medium: "中", high: "高", critical: "关键" }};
      return labels[risk] || risk || "";
    }}
    function relayHumanizeEnvelope(envelope) {{
      const lines = [];
      const summary = relayHumanizeDisplayText(
        envelope.summary || envelope.output || envelope.reason || "",
        "该角色已返回结构化结果，详情见结构化数据。"
      ).trim();
      if (summary) lines.push(`结论：${{summary}}`);
      const nextAction = relayHumanizeDisplayText(
        envelope.next_action || "",
        "下一步见结构化数据。"
      ).trim();
      if (nextAction) lines.push(`下一步：${{nextAction}}`);
      const questions = relayHumanizeDisplayText(
        relayJoinTextList(envelope.open_questions),
        "待确认内容见结构化数据。"
      );
      if (questions) lines.push(`待确认：${{questions}}`);
      const route = String(envelope.route || "").trim();
      const risk = String(envelope.risk || "").trim();
      if (route || risk) {{
        const parts = [];
        if (route) parts.push(`路径：${{relayRouteLabel(route)}}`);
        if (risk) parts.push(`风险：${{relayRiskLabel(risk)}}`);
        lines.push(parts.join(" · "));
      }}
      const acceptance = relayHumanizeDisplayText(
        relayJoinTextList(envelope.acceptance_criteria),
        "验收依据见结构化数据。"
      );
      if (acceptance) lines.push(`验收依据：${{acceptance}}`);
      return lines.length ? lines.join("\\n") : "角色已返回结构化结果。";
    }}
    function nativeMessageKey(role, nativeEvent, bucket = "") {{
      const payload = nativeEventPayload(nativeEvent);
      const stable = payload.itemId || payload.item_id || payload.message_id || payload.native_message_id || payload.native_turn_id || payload.turnId || nativeEvent?.id || `${{Date.now()}}:${{Math.random()}}`;
      return `${{bucket}}:${{role || ""}}:${{stable}}`;
    }}
    function setNativeBodyText(node, text, append = false) {{
      const body = node?.querySelector("[data-native-message-body]");
      if (!body) return;
      if (append) body.textContent += text;
      else body.textContent = text;
      scrollNativeConversationToEnd();
    }}
    function createNativeMessage(role, kind, speaker, meta, key) {{
      if (!conversationTimeline) return null;
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const block = document.createElement("article");
      block.className = "relay-message";
      block.dataset.nativeRole = role || "system";
      block.dataset.nativeKind = kind || "status";
      if (key) block.dataset.nativeKey = key;
      const avatar = document.createElement("span");
      avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{role || "system"}}`;
      avatar.setAttribute("aria-label", speaker || labelForRole(role));
      const head = document.createElement("div");
      head.className = "relay-message-head";
      const title = document.createElement("strong");
      title.textContent = speaker || labelForRole(role);
      head.appendChild(title);
      if (meta) {{
        const metaNode = document.createElement("span");
        metaNode.className = "relay-message-meta";
        metaNode.textContent = meta;
        head.appendChild(metaNode);
      }}
      const body = document.createElement("div");
      body.className = "relay-message-body";
      body.dataset.nativeMessageBody = "";
      block.appendChild(avatar);
      block.appendChild(head);
      block.appendChild(body);
      conversationTimeline.appendChild(block);
      scrollNativeConversationToEnd();
      return block;
    }}
    function marvisConversationPersona(role) {{
      if (role === "architect") return "computer";
      if (role === "implementer") return "app";
      if (role === "tester") return "search";
      if (role === "auditor") return "file";
      return "marvis";
    }}
    function appendMarvisConversationUser(text, key = "", pending = false, attachments = {{}}) {{
      if (!conversationTimeline) return null;
      const body = relayHumanizeUserMessage(text);
      const normalizedBody = relayNormalizeConversationText(body);
      const hasAttachments = Boolean((attachments.images || []).length || (attachments.files || []).length);
      if ((!normalizedBody && !hasAttachments) || relayUserMessageIsRetryOrContext(body)) return null;
      if (normalizedBody) {{
        if (conversationUserBodies.has(normalizedBody)) return null;
        conversationUserBodies.add(normalizedBody);
      }}
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const node = document.createElement("article");
      node.className = "marvis-relay-user-message";
      node.dataset.nativeRole = "user";
      node.dataset.nativeKind = "user_message";
      if (key) node.dataset.nativeKey = key;
      if (pending) node.dataset.pendingFollowup = "true";
      if (body) {{
        const bubble = document.createElement("div");
        bubble.className = "marvis-relay-user-bubble";
        bubble.dataset.nativeMessageBody = "";
        bubble.textContent = body;
        node.appendChild(bubble);
      }}
      appendMarvisAttachmentList(node, attachments);
      conversationTimeline.appendChild(node);
      if (key) nativeTranscriptNodes.set(key, node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function appendMarvisConversationGuidance(payload = {{}}) {{
      if (!conversationTimeline) return null;
      const text = String(payload.text || payload.latest_user_input || "").trim();
      const hasAttachments = Boolean((payload.images || []).length || (payload.files || []).length);
      if (!text && !hasAttachments) return null;
      const id = payload.guidance_artifact_id || payload.artifact_id || payload.id || payload.pending_input_id || Date.now();
      const key = `user_guidance:${{id}}`;
      const existing = nativeTranscriptNodes.get(key) || conversationTimeline.querySelector(`[data-native-key='${{CSS.escape(key)}}']`);
      if (existing) return existing;
      const roundId = String(payload.steered_round_id || payload.round_id || activeRelayRoundId || CURRENT_ROUND_ID || "1");
      activateRelayRound({{ round_id: roundId }});
      const body = relayHumanizeUserMessage(text);
      const normalizedBody = relayNormalizeConversationText(body);
      if (normalizedBody) conversationUserBodies.add(normalizedBody);
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const node = document.createElement("article");
      node.className = "marvis-relay-user-message marvis-relay-guidance-message";
      node.dataset.nativeRole = "user";
      node.dataset.nativeKind = "user_guidance";
      node.dataset.nativeRoundId = roundId;
      node.dataset.nativeKey = key;
      const bubble = document.createElement("div");
      bubble.className = "marvis-relay-user-bubble marvis-relay-guidance-bubble";
      const label = document.createElement("span");
      label.className = "marvis-relay-guidance-label";
      label.textContent = "引导当前";
      const content = document.createElement("strong");
      content.textContent = body || "已添加附件";
      bubble.append(label, content);
      node.appendChild(bubble);
      appendMarvisAttachmentList(node, {{
        images: payload.images || [],
        files: payload.files || []
      }});
      conversationTimeline.appendChild(node);
      nativeTranscriptNodes.set(key, node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function markMarvisConversationUserFailed(key) {{
      if (!key) return;
      const node = nativeTranscriptNodes.get(key) || conversationTimeline?.querySelector(`[data-native-key='${{CSS.escape(key)}}']`);
      if (!node) return;
      node.classList.add("is-failed");
      node.dataset.pendingFollowup = "failed";
      node.title = "发送失败";
    }}
    function clearMarvisConversationPausedRows() {{
      conversationTimeline?.querySelectorAll("[data-native-key^='relay-paused:']").forEach((node) => node.remove());
    }}
    function relayEventRoundId(payload) {{
      const value = payload?.round_id || payload?.payload?.round_id || "";
      return value ? String(value) : "";
    }}
    function isCurrentRoundEvent(payload) {{
      const roundId = relayEventRoundId(payload);
      return !roundId || roundId === activeRelayRoundId;
    }}
    function activateRelayRound(payload) {{
      const roundId = relayEventRoundId(payload);
      if (roundId) {{
        activeRelayRoundId = roundId;
        if (followupComposer) followupComposer.dataset.currentRoundId = roundId;
      }}
      return activeRelayRoundId;
    }}
    function appendMarvisConversationWaiting(roundId = "") {{
      if (!conversationTimeline) return null;
      clearMarvisConversationPausedRows();
      let node = conversationTimeline.querySelector("[data-marvis-followup-waiting]");
      if (node) {{
        if (roundId) node.dataset.nativeRoundId = roundId;
        return node;
      }}
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      node = document.createElement("article");
      node.className = "marvis-relay-agent-step marvis-relay-waiting";
      node.dataset.nativeRole = "director";
      node.dataset.nativeKind = "waiting";
      node.dataset.nativeRoundId = roundId || activeRelayRoundId || "1";
      node.dataset.marvisFollowupWaiting = "true";
      const avatar = document.createElement("span");
      avatar.className = "marvis-relay-avatar marvis-relay-avatar-marvis";
      avatar.setAttribute("aria-label", "Marvis");
      const content = document.createElement("div");
      content.className = "marvis-relay-agent-content";
      const head = document.createElement("div");
      head.className = "marvis-relay-agent-head";
      const title = document.createElement("strong");
      title.textContent = "Marvis";
      const action = document.createElement("span");
      action.className = "marvis-relay-agent-action";
      action.textContent = "| 任务分配 进行中";
      head.append(title, document.createTextNode(" "), action);
      const bubble = document.createElement("div");
      bubble.className = "marvis-relay-agent-bubble";
      bubble.dataset.nativeMessageBody = "";
      bubble.textContent = "...";
      content.append(head, bubble);
      node.append(avatar, content);
      conversationTimeline.appendChild(node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function clearMarvisConversationWaiting(roundId = "") {{
      const activeRound = roundId || activeRelayRoundId || "";
      conversationTimeline?.querySelectorAll("[data-marvis-followup-waiting]").forEach((node) => {{
        if (!activeRound || !node.dataset.nativeRoundId || node.dataset.nativeRoundId === activeRound) {{
          node.remove();
        }}
      }});
    }}
    function appendMarvisConversationAssistant(role, text, kind = "followup_response", key = "", status = "passed", roundId = "") {{
      if (!conversationTimeline || !text) return null;
      clearMarvisConversationWaiting(roundId);
      const existing = key ? nativeTranscriptNodes.get(key) || conversationTimeline.querySelector(`[data-native-key='${{CSS.escape(key)}}']`) : null;
      const node = existing || document.createElement("article");
      node.className = "marvis-relay-agent-step";
      node.dataset.nativeRole = role || "director";
      node.dataset.nativeKind = kind || "followup_response";
      node.dataset.nativeRoundId = roundId || activeRelayRoundId || "1";
      if (key) node.dataset.nativeKey = key;
      if (!existing) {{
        const avatar = document.createElement("span");
        avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{marvisConversationPersona(role)}}`;
        avatar.setAttribute("aria-label", labelForRole(role));
        const content = document.createElement("div");
        content.className = "marvis-relay-agent-content";
        const head = document.createElement("div");
        head.className = "marvis-relay-agent-head";
        const title = document.createElement("strong");
        title.textContent = labelForRole(role);
        const action = document.createElement("span");
        action.className = "marvis-relay-agent-action";
        action.textContent = `| ${{labelForStatus(status) || "已完成"}}`;
        head.append(title, document.createTextNode(" "), action);
        const bubble = document.createElement("div");
        bubble.className = "marvis-relay-agent-bubble";
        bubble.dataset.nativeMessageBody = "";
        content.append(head, bubble);
        node.append(avatar, content);
        conversationTimeline.appendChild(node);
      }}
      setNativeBodyText(node, text);
      if (key) nativeTranscriptNodes.set(key, node);
      return node;
    }}
    function appendMarvisConversationHandoff(toRole, key = "", fromRole = "", roundId = "") {{
      if (!conversationTimeline || !toRole) return null;
      if (!fromRole || fromRole === toRole) return null;
      roundId = roundId || activeRelayRoundId || CURRENT_ROUND_ID || "1";
      const existingPair = conversationTimeline.querySelector(
        `[data-marvis-handoff][data-native-from-role='${{CSS.escape(fromRole)}}'][data-native-to-role='${{CSS.escape(toRole)}}'][data-native-round-id='${{CSS.escape(roundId)}}']`
      );
      if (existingPair) return existingPair;
      const handoffKey = key || `handoff:${{toRole}}`;
      let node = conversationTimeline.querySelector(`[data-native-key='${{CSS.escape(handoffKey)}}']`);
      if (node) return node;
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      node = document.createElement("div");
      node.className = "marvis-relay-handoff";
      node.dataset.marvisHandoff = "";
      node.dataset.nativeKind = "handoff";
      node.dataset.nativeRole = toRole;
      node.dataset.nativeFromRole = fromRole;
      node.dataset.nativeToRole = toRole;
      node.dataset.nativeRoundId = roundId;
      node.dataset.nativeKey = handoffKey;
      node.textContent = marvisHandoffText(fromRole, toRole);
      conversationTimeline.appendChild(node);
      nativeTranscriptNodes.set(handoffKey, node);
      scrollNativeConversationToEnd();
      return node;
    }}
    function renderRelayNativeEvent(role, nativeEvent, runtimeEventId = "") {{
      if (!conversationTimeline || !nativeEvent) return;
      const kind = nativeEvent.kind || "event";
      const payload = nativeEventPayload(nativeEvent);
      const text = nativeEventText(nativeEvent);
      const provider = nativeEvent.source || payload.provider || "";
      const roleLabel = labelForRole(role);
      if (kind === "user_message") {{
        const body = relayHumanizeUserMessage(text);
        const normalizedBody = relayNormalizeConversationText(body);
        if (!normalizedBody || relayUserMessageIsRetryOrContext(body) || conversationUserBodies.has(normalizedBody)) return;
        conversationUserBodies.add(normalizedBody);
        const key = nativeMessageKey(role, nativeEvent, "user");
        let node = nativeTranscriptNodes.get(key);
        if (!node) {{
          node = createNativeMessage(role, "user_message", "你", roleLabel, key);
          nativeTranscriptNodes.set(key, node);
        }}
        setNativeBodyText(node, body);
        return;
      }}
      if (kind === "text_delta" || kind === "message_completed") {{
        const key = nativeMessageKey(role, nativeEvent, "assistant");
        const bufferedEnvelope = nativeEnvelopeBuffers.get(key) || "";
        if (kind === "text_delta") {{
          appendRoleStreamDelta(role, text, runtimeEventId || nativeEvent?.id, nativeEvent);
          setRoleStatus(role, "streaming");
        }}
        if (bufferedEnvelope || relayTextLooksLikeEnvelope(text)) {{
          const candidate = kind === "text_delta" ? bufferedEnvelope + text : text || bufferedEnvelope;
          if (kind === "text_delta") nativeEnvelopeBuffers.set(key, candidate);
          const envelope = relayParseEnvelope(candidate);
          if (!envelope) {{
            if (kind === "message_completed") {{
              nativeEnvelopeBuffers.delete(key);
            }}
            return;
          }}
          nativeEnvelopeBuffers.delete(key);
          clearRolePreview(role);
          setRoleStatus(role, "streaming");
          return;
        }}
        if (kind === "message_completed") {{
          replaceRoleStreamWithCompleted(role, text, runtimeEventId || nativeEvent?.id, nativeEvent);
        }}
        return;
      }}
    }}
    document.querySelectorAll("[data-native-key]").forEach((node) => {{
      if (node.dataset.nativeKey) nativeTranscriptNodes.set(node.dataset.nativeKey, node);
    }});
    document.querySelectorAll('[data-native-kind="user_message"] [data-native-message-body]').forEach((node) => {{
      const normalizedBody = relayNormalizeConversationText(node.textContent || "");
      if (normalizedBody) conversationUserBodies.add(normalizedBody);
    }});
    function streamEventKey(role, eventId) {{
      const value = String(eventId || "").trim();
      return value ? `${{role || ""}}:${{value}}` : "";
    }}
    function roleStreamStableEventId(eventId, nativeEvent = null) {{
      const payload = nativeEventPayload(nativeEvent);
      const directPayload = nativeEvent && typeof nativeEvent === "object" ? nativeEvent : {{}};
      return payload.itemId
        || payload.item_id
        || payload.stream_key
        || directPayload.stream_key
        || payload.native_message_id
        || directPayload.native_message_id
        || payload.message_id
        || directPayload.message_id
        || payload.native_turn_id
        || directPayload.native_turn_id
        || payload.turnId
        || directPayload.turnId
        || payload.turn_id
        || directPayload.turn_id
        || eventId
        || "current";
    }}
    function roleStreamBufferKey(role, eventId) {{
      return `${{role || ""}}:${{eventId || "current"}}`;
    }}
    function removeRoleStreamNode(role) {{
      conversationTimeline?.querySelector(`[data-conversation-role-stream="${{role}}"]`)?.remove();
    }}
    function hideRoleStreamBuffer(role, bufferKey) {{
      hiddenProtocolStreamKeys.add(bufferKey);
      roleStreamBuffers.delete(bufferKey);
      removeRoleStreamNode(role);
    }}
    function marvisConversationRoleLabel(role) {{
      return MARVIS_WORK_LOG_ROLE_LABELS[role] || labelForRole(role) || "Marvis";
    }}
    function marvisConversationStreamAction(role, text) {{
      const count = Array.from(String(text || "")).length;
      const action = role === "director" ? "任务分配" : "任务处理";
      return `| ${{action}} 进行中${{count ? `，${{count}}字符` : ""}}`;
    }}
    function createMarvisRoleStreamMessage(role, key) {{
      if (!conversationTimeline) return null;
      const empty = conversationTimeline.querySelector("[data-native-empty]");
      if (empty) empty.remove();
      const node = document.createElement("article");
      node.className = "marvis-relay-agent-step";
      node.dataset.nativeRole = role || "director";
      node.dataset.nativeKind = "text_delta";
      node.dataset.conversationRoleStream = role || "director";
      if (key) node.dataset.nativeKey = key;
      const label = marvisConversationRoleLabel(role);
      const avatar = document.createElement("span");
      avatar.className = `marvis-relay-avatar marvis-relay-avatar-${{marvisConversationPersona(role)}}`;
      avatar.setAttribute("aria-label", label);
      const content = document.createElement("div");
      content.className = "marvis-relay-agent-content";
      const head = document.createElement("div");
      head.className = "marvis-relay-agent-head";
      const title = document.createElement("strong");
      title.textContent = label;
      const action = document.createElement("span");
      action.className = "marvis-relay-agent-action";
      action.dataset.marvisStreamAction = "true";
      head.append(title, document.createTextNode(" "), action);
      const body = document.createElement("div");
      body.className = "marvis-relay-agent-bubble";
      body.dataset.nativeMessageBody = "";
      content.append(head, body);
      node.append(avatar, content);
      conversationTimeline.appendChild(node);
      return node;
    }}
    function updateMarvisRoleStreamAction(node, role, text) {{
      const action = node?.querySelector("[data-marvis-stream-action]");
      if (action) action.textContent = marvisConversationStreamAction(role, text);
    }}
    function appendRoleStreamDelta(role, text, eventId = "", nativeEvent = null) {{
      if (!role || !text) return;
      const stableEventId = roleStreamStableEventId(eventId, nativeEvent);
      const bufferKey = roleStreamBufferKey(role, stableEventId);
      if (hiddenProtocolStreamKeys.has(bufferKey)) return;
      const eventKey = streamEventKey(role, eventId);
      if (eventKey && seenStreamEventKeys.has(eventKey)) return;
      if (eventKey) seenStreamEventKeys.add(eventKey);
      const value = String(text || "");
      const buffered = `${{roleStreamBuffers.get(bufferKey) || ""}}${{value}}`;
      roleStreamBuffers.set(bufferKey, buffered);
      if (
        marvisConversationTextIsProtocolNoise(buffered)
        || marvisConversationTextIsStructuredArtifactPlaceholder(buffered)
      ) {{
        hideRoleStreamBuffer(role, bufferKey);
        appendMarvisConversationWaiting(activeRelayRoundId);
        setRoleStatus(role, "streaming");
        return;
      }}
      if (marvisConversationTextIsPotentialProtocolPrefix(buffered)) {{
        appendMarvisConversationWaiting(activeRelayRoundId);
        setRoleStatus(role, "streaming");
        return;
      }}
      if (TERMINAL_ROLE_STATUSES.has(currentRoleStatus(role))) return;
      if (!conversationTimeline) return;
      if (conversationTimeline.querySelector(`[data-conversation-role-final="${{role}}"]`)) return;
      let node = conversationTimeline.querySelector(`[data-conversation-role-stream="${{role}}"]`);
      if (!node) {{
        node = createMarvisRoleStreamMessage(role, `stream:${{bufferKey}}`);
        if (!node) return;
      }}
      const body = node.querySelector("[data-native-message-body]");
      const current = body ? body.textContent || "" : "";
      updateMarvisRoleStreamAction(node, role, current + value);
      setNativeBodyText(node, current + value);
    }}
    function replaceRoleStreamWithCompleted(role, text, eventId = "", nativeEvent = null) {{
      if (!role || !text || !conversationTimeline) return;
      const stableEventId = roleStreamStableEventId(eventId, nativeEvent);
      const bufferKey = roleStreamBufferKey(role, stableEventId);
      const value = String(text || "");
      if (
        marvisConversationTextIsProtocolNoise(value)
        || marvisConversationTextIsStructuredArtifactPlaceholder(value)
        || relayTextLooksLikeEnvelope(value)
      ) {{
        hideRoleStreamBuffer(role, bufferKey);
        appendMarvisConversationWaiting(activeRelayRoundId);
        return;
      }}
      roleStreamBuffers.delete(bufferKey);
      hiddenProtocolStreamKeys.delete(bufferKey);
      removeRoleStreamNode(role);
      const key = nativeMessageKey(role, nativeEvent, "assistant");
      appendMarvisConversationAssistant(
        role,
        value,
        "message_completed",
        key,
        currentRoleStatus(role) || "completed",
        activeRelayRoundId
      );
      scrollNativeConversationToEnd();
    }}
    function clearRolePreview(role) {{
      conversationTimeline?.querySelector(`[data-conversation-role-preview="${{role}}"]`)?.remove();
      removeRoleStreamNode(role);
    }}
    function clearAllRolePreviews() {{
      Object.keys(roleStatuses).forEach(clearRolePreview);
    }}
    document.querySelectorAll("[data-conversation-role-stream]").forEach((node) => {{
      const role = node.dataset.conversationRoleStream;
      (node.dataset.streamEventIds || "").split(",").filter(Boolean).forEach((eventId) => {{
        const eventKey = streamEventKey(role, eventId);
        if (eventKey) seenStreamEventKeys.add(eventKey);
      }});
    }});
    const TERMINAL_ROLE_STATUSES = new Set(["passed", "completed", "blocked", "failed", "interrupted"]);
    function currentRoleStatus(role) {{
      return roleStatuses[role] || "";
    }}
    function canApplyRoleStatus(role, status, options = {{}}) {{
      if (!status) return false;
      if (options.force) return true;
      const currentStatus = currentRoleStatus(role);
      if (TERMINAL_ROLE_STATUSES.has(currentStatus) && !TERMINAL_ROLE_STATUSES.has(status)) return false;
      return true;
    }}
    function setRoleStatus(role, status, options = {{}}) {{
      if (!canApplyRoleStatus(role, status, options)) return;
      if (role && status) roleStatuses[role] = status;
    }}
    const followupComposer = document.querySelector("[data-marvis-followup-composer]");
    const pendingInputsContainer = document.querySelector("[data-marvis-pending-inputs]");
    const followupTextInput = followupComposer?.querySelector("textarea[name='text']");
    const followupSubmitButton = followupComposer?.querySelector("[data-marvis-submit]");
    const followupInterruptButton = followupComposer?.querySelector("[data-marvis-interrupt-button]");
    const relayMutationStatus = followupComposer?.querySelector("[data-relay-mutation-status]");
    const pendingInputs = new Map();
    let relayTaskStatus = followupComposer?.dataset.taskStatusValue || "";
    let waitingControlInput = "";
    function relayTaskIsRunning() {{
      return ["queued", "running", "streaming"].includes(String(relayTaskStatus || "").trim());
    }}
    function relayTaskAcceptsPendingInput() {{
      return relayTaskIsRunning() || String(relayTaskStatus || "").trim() === "waiting_user";
    }}
    function relayFollowupHasText() {{
      return Boolean(String(followupTextInput?.value || "").trim());
    }}
    function relayFollowupHasAttachments() {{
      return Boolean(window.marvisRelayAttachments?.hasAttachments?.());
    }}
    function updateRelayComposerAction() {{
      if (!followupSubmitButton) return;
      followupSubmitButton.setAttribute("aria-label", "发送补充");
      if (followupInterruptButton) {{
        const canInterrupt = relayTaskIsRunning();
        followupInterruptButton.hidden = !canInterrupt;
        followupInterruptButton.disabled = !canInterrupt;
      }}
    }}
    function setRelayMutationStatus(text, isError = false) {{
      if (!relayMutationStatus) return;
      relayMutationStatus.textContent = text;
      relayMutationStatus.classList.toggle("is-error", isError);
    }}
    const relayMutationClient = window.WLCodexSurfaceRuntime.createMutationClient({{
      prefix: "relay",
      keyAttribute: "relayIdempotencyKey",
    }});
    async function relayMutation(url, body, button) {{
      return relayMutationClient.request(url, body, button);
    }}
    function marvisEscapeText(value) {{
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}
    function pendingInputId(item) {{
      return String(item?.id || item?.pending_input_id || "");
    }}
    function upsertPendingInput(item) {{
      const id = pendingInputId(item);
      if (!id) return;
      const next = {{ ...(pendingInputs.get(id) || {{}}), ...(item || {{}}), id }};
      if (!next.error_message) delete next.error_message;
      pendingInputs.set(id, next);
      renderPendingInputs();
    }}
    function removePendingInput(id) {{
      const key = String(id || "");
      if (key) pendingInputs.delete(key);
      renderPendingInputs();
    }}
    function visiblePendingInputs() {{
      return Array.from(pendingInputs.values()).filter((item) =>
        ["pending", "steered"].includes(String(item.status || ""))
      );
    }}
    function renderPendingInputs() {{
      if (!pendingInputsContainer) return;
      const rows = visiblePendingInputs();
      pendingInputsContainer.hidden = rows.length === 0;
      pendingInputsContainer.innerHTML = rows.map((item) => {{
        const id = pendingInputId(item);
        const isSteered = String(item.status || "") === "steered";
        const hasError = Boolean(item.error_message);
        const text = String(item.text || "").trim() || "已添加附件";
        const statusText = hasError ? String(item.error_message || "引导失败，仍已排队") : (isSteered ? "已引导当前，等待当前角色接收" : "已排队，当前 round 结束后自动开始");
        const className = `marvis-relay-pending-input${{isSteered ? " is-steered" : ""}}${{hasError ? " is-error" : ""}}`;
        const actions = isSteered ? "" : `<span class="marvis-relay-pending-actions">
            <button type="button" data-pending-steer="${{marvisEscapeText(id)}}">引导当前</button>
            <button type="button" data-pending-cancel="${{marvisEscapeText(id)}}">取消</button>
          </span>`;
        return `<article class="${{className}}" data-pending-input-id="${{marvisEscapeText(id)}}">
          <span class="marvis-relay-pending-status">${{statusText}}</span>
          <strong>${{marvisEscapeText(text)}}</strong>
          ${{actions}}
        </article>`;
      }}).join("");
    }}
    (INITIAL_PENDING_INPUTS || []).forEach(upsertPendingInput);
    function updateTaskStatus(status) {{
      if (!status) return;
      relayTaskStatus = String(status || "");
      if (followupComposer) followupComposer.dataset.taskStatusValue = relayTaskStatus;
      document.querySelectorAll("[data-task-status]").forEach((node) => {{
        node.textContent = TASK_STATUS_LABELS[status] || status;
      }});
      updateRelayComposerAction();
    }}
    pendingInputsContainer?.addEventListener("click", async (event) => {{
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const steerId = target.getAttribute("data-pending-steer");
      const cancelId = target.getAttribute("data-pending-cancel");
      const pendingId = steerId || cancelId;
      if (!pendingId) return;
      const action = steerId ? "steer" : "cancel";
      try {{
        const payload = await relayMutation(
          `/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/inputs/${{encodeURIComponent(pendingId)}}/${{action}}${{TOKEN_SUFFIX}}`,
          {{}},
          target,
        );
        const item = payload.pending_input || payload;
        if (action === "cancel") {{
          removePendingInput(pendingId);
        }} else {{
          upsertPendingInput(item);
          appendMarvisConversationGuidance(item);
        }}
      }} catch (_error) {{
        const current = pendingInputs.get(String(pendingId)) || {{ id: pendingId, status: "pending" }};
        upsertPendingInput({{ ...current, error_message: "引导失败，仍已排队" }});
      }}
    }});
    let planControl = document.querySelector("[data-marvis-plan-control]");
    function confirmationOptionsFromPayload(payload = {{}}) {{
      const options = Array.isArray(payload.confirmation_options) ? payload.confirmation_options : [];
      return options.slice(0, 6).map((item, index) => {{
        if (typeof item === "string") {{
          const label = item.trim();
          return {{ id: `option_${{index + 1}}`, label, summary: "", instruction: label }};
        }}
        const source = item && typeof item === "object" ? item : {{}};
        const id = String(source.id || `option_${{index + 1}}`).trim();
        const label = String(source.label || source.title || source.name || source.summary || id).trim();
        const summary = String(source.summary || source.description || "").trim();
        const instruction = String(source.instruction || source.prompt || source.value || source.text || label).trim();
        if (!label && !instruction) return null;
        return {{ id, label: label || instruction, summary, instruction: instruction || label }};
      }}).filter(Boolean);
    }}
    function confirmationOptionsHtml(options) {{
      return options.map((option, index) => `<button class="marvis-relay-confirmation-option" type="button"
          data-confirmation-option-id="${{marvisEscapeText(option.id)}}"
          data-confirmation-option-label="${{marvisEscapeText(option.label)}}"
          data-confirmation-option-instruction="${{marvisEscapeText(option.instruction)}}"
          aria-pressed="${{index === 0 ? "true" : "false"}}">
          <strong>${{marvisEscapeText(option.label)}}</strong>
          <span>${{marvisEscapeText(option.summary || option.instruction)}}</span>
        </button>`).join("");
    }}
    function confirmationSourceLabel(payload = {{}}) {{
      const source = String(payload.confirmation_source || "");
      const provider = String(payload.provider || "").toLowerCase();
      if (source === "provider_native_plan" || source === "provider_native_approval") {{
        if (provider === "codex") return "Codex 原生确认";
        if (provider.startsWith("claude")) return "Claude 原生确认";
        return "Provider 原生确认";
      }}
      return "Relay 澄清确认";
    }}
    function hidePlanControlSurface() {{
      document.querySelectorAll("[data-marvis-confirmation-page]").forEach((node) => {{
        closeMarvisConfirmationPage(node, {{ restoreFocus: false }});
      }});
      document.querySelectorAll("[data-marvis-plan-control]").forEach((node) => {{
        if (node instanceof HTMLElement) node.hidden = true;
      }});
      planControl = null;
    }}
    function ensurePlanControl(payload = {{}}) {{
      if (planControl && !planControl.hidden) return;
      if (planControl && planControl.hidden) {{
        planControl.remove();
        planControl = null;
      }}
      const roundId = String(payload.round_id || activeRelayRoundId || CURRENT_ROUND_ID || "1");
      const artifactId = String(payload.artifact_id || "0");
      const isPlanApproval = payload.waiting_reason === "plan_approval";
      const title = isPlanApproval ? "计划等待确认" : "等待确认";
      const primaryLabel = isPlanApproval ? "执行计划" : "选择执行";
      const primaryDecision = isPlanApproval ? "approve_plan" : "continue";
      const summary = String(payload.summary || payload.next_action || "当前接力需要你确认下一步。").trim();
      const sourceLabel = confirmationSourceLabel(payload);
      const confirmationKind = String(payload.confirmation_kind || "relay_question");
      const waitingReason = String(payload.waiting_reason || "");
      const providerRequestId = String(payload.provider_request_id || "");
      const metaText = [
        `来源：${{sourceLabel}}`,
        `请求类型：${{confirmationKind}}`,
        waitingReason ? `等待原因：${{waitingReason}}` : "",
        providerRequestId ? `请求 ID：${{providerRequestId}}` : "",
      ].filter(Boolean).join("\\n");
      const questions = Array.isArray(payload.open_questions) ? payload.open_questions.map((item) => String(item || "").trim()).filter(Boolean) : [];
      const options = confirmationOptionsFromPayload(payload);
      const optionHtml = confirmationOptionsHtml(options);
      const node = document.createElement("section");
      if (followupComposer) followupComposer.dataset.currentRoundId = roundId;
      node.className = "marvis-relay-confirmation-card";
      node.setAttribute("data-marvis-plan-control", "");
      node.setAttribute("data-marvis-confirmation-card", "");
      node.setAttribute("data-round-id", roundId);
      node.setAttribute("data-artifact-id", artifactId);
      node.setAttribute("aria-label", title);
      node.innerHTML = `<button class="marvis-relay-confirmation-thumb" type="button" data-marvis-confirmation-open aria-label="查看确认详情">
          <em>${{marvisEscapeText(sourceLabel)}}</em>
          <span>${{marvisEscapeText(title)}}</span>
          <strong>${{marvisEscapeText(summary)}}</strong>
        </button>
        <div class="marvis-relay-confirmation-options"${{optionHtml ? "" : " hidden"}}>
          ${{optionHtml}}
        </div>
        <div class="marvis-relay-confirmation-actions">
          <button type="button" data-plan-decision="${{marvisEscapeText(primaryDecision)}}">${{marvisEscapeText(primaryLabel)}}</button>
          <button type="button" data-waiting-input>补充内容</button>
          <button type="button" data-plan-decision="cancel_plan">停止</button>
        </div>`;
      document.querySelector(".marvis-relay-phone")?.appendChild(node);
      const page = document.createElement("section");
      page.className = "marvis-relay-confirmation-page";
      page.setAttribute("data-marvis-confirmation-page", "");
      page.setAttribute("data-round-id", roundId);
      page.setAttribute("data-artifact-id", artifactId);
      page.setAttribute("role", "dialog");
      page.setAttribute("aria-modal", "true");
      page.setAttribute("aria-label", title);
      page.hidden = true;
      page.innerHTML = `<div class="marvis-relay-confirmation-page-shell">
          <header>
            <button type="button" data-marvis-confirmation-close aria-label="返回">‹</button>
            <strong>${{marvisEscapeText(title)}}</strong>
          </header>
          <main>
            <small>${{marvisEscapeText(metaText)}}</small>
            <h2>${{marvisEscapeText(summary)}}</h2>
            <p>${{marvisEscapeText(summary + (questions.length ? "\\n\\n待确认：\\n" + questions.map((item) => "- " + item).join("\\n") : ""))}}</p>
          </main>
        </div>`;
      document.body.appendChild(page);
      bindMarvisConfirmationPage(page);
      planControl = node;
    }}
    document.addEventListener("click", async (event) => {{
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const openConfirmation = target.closest("[data-marvis-confirmation-open]");
      if (openConfirmation) {{
        openMarvisConfirmationPage(
          marvisConfirmationPageFor(openConfirmation),
          openConfirmation,
        );
        return;
      }}
      const closeConfirmation = target.closest("[data-marvis-confirmation-close]");
      if (closeConfirmation) {{
        closeMarvisConfirmationPage(
          closeConfirmation.closest("[data-marvis-confirmation-page]"),
        );
        return;
      }}
      const optionButton = target.closest("[data-confirmation-option-id]");
      if (optionButton instanceof HTMLElement) {{
        const control = optionButton.closest("[data-marvis-plan-control]");
        control?.querySelectorAll("[data-confirmation-option-id]").forEach((node) => {{
          if (node instanceof HTMLElement) node.setAttribute("aria-pressed", node === optionButton ? "true" : "false");
        }});
        return;
      }}
      if (target.hasAttribute("data-waiting-input")) {{
        waitingControlInput = "revise_plan";
        followupTextInput?.setAttribute("placeholder", "说明你的想法或修改要求");
        followupTextInput?.focus();
        return;
      }}
      const decision = target.getAttribute("data-plan-decision");
      if (!decision) return;
      if (decision === "revise_plan") {{
        waitingControlInput = "revise_plan";
        followupTextInput?.setAttribute("placeholder", "说明你的想法或修改要求");
        followupTextInput?.focus();
        return;
      }}
      const activePlanControl = target.closest("[data-marvis-plan-control]");
      if (!(activePlanControl instanceof HTMLElement)) return;
      const roundId = activePlanControl.getAttribute("data-round-id") || CURRENT_ROUND_ID;
      const artifactId = activePlanControl.getAttribute("data-artifact-id") || "0";
      const selected = activePlanControl.querySelector("[data-confirmation-option-id][aria-pressed='true']") || activePlanControl.querySelector("[data-confirmation-option-id]");
      const controlPayload = {{ decision, artifact_id: Number(artifactId) || 0 }};
      if (selected instanceof HTMLElement && decision !== "cancel_plan") {{
        controlPayload.selected_option_id = selected.getAttribute("data-confirmation-option-id") || "";
        controlPayload.selected_option_label = selected.getAttribute("data-confirmation-option-label") || "";
        controlPayload.selected_option_instruction = selected.getAttribute("data-confirmation-option-instruction") || "";
      }}
      const shouldLeaveWaiting = decision === "approve_plan" || decision === "continue" || decision === "cancel_plan";
      if (shouldLeaveWaiting) {{
        hidePlanControlSurface();
        updateTaskStatus(decision === "cancel_plan" ? "interrupted" : "running");
        waitingControlInput = "";
      }}
      try {{
        await relayMutation(
          `/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/rounds/${{encodeURIComponent(roundId)}}/control${{TOKEN_SUFFIX}}`,
          controlPayload,
          target,
        );
        if (decision === "approve_plan" || decision === "continue") {{
          updateTaskStatus("running");
          waitingControlInput = "";
        }} else if (decision === "cancel_plan") {{
          updateTaskStatus("interrupted");
          waitingControlInput = "";
        }}
      }} catch (_error) {{
        if (shouldLeaveWaiting) {{
          activePlanControl.hidden = false;
          planControl = activePlanControl;
          updateTaskStatus("waiting_user");
        }}
      }}
    }});
    let relayEventsAfter = Number(new URLSearchParams(String(EVENTS_SUFFIX || "").replace(/^\\?/, "")).get("after") || "0") || 0;
    function updateRelayEventsCursor(event) {{
      const value = Number(event?.lastEventId || "0") || 0;
      if (value > relayEventsAfter) relayEventsAfter = value;
    }}
    function relayEventsSuffix() {{
      const params = new URLSearchParams(String(TOKEN_SUFFIX || "").replace(/^\\?/, ""));
      if (relayEventsAfter > 0) params.set("after", String(relayEventsAfter));
      const value = params.toString();
      return value ? `?${{value}}` : "";
    }}
    const relayEventsConnection = window.WLCodexSurfaceRuntime.createSseConnection({{
      url: () => `/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/events${{relayEventsSuffix()}}`,
    }});
    function addRelayEventListener(name, handler) {{
      relayEventsConnection.addEventListener(name, handler);
    }}
    function closeRelayEventSource() {{ relayEventsConnection.close(); }}
    function connectRelayEventSource() {{
      relayEventsConnection.connect();
    }}
    document.addEventListener("visibilitychange", () => {{
      if (document.visibilityState === "visible") connectRelayEventSource();
      else closeRelayEventSource();
    }});
    window.addEventListener("pageshow", () => connectRelayEventSource());
    window.addEventListener("pagehide", closeRelayEventSource);
    window.addEventListener("beforeunload", closeRelayEventSource);
    addRelayEventListener("presentation.snapshot", (event) => {{
      const payload = parseRelayEvent(event);
      const presentation = payload.presentation;
      if (!presentation || typeof presentation !== "object") return;
      const state = String(presentation.state || "");
      if (state) updateTaskStatus(state);
      const actor = presentation.current_actor;
      if (actor && typeof actor === "object" && actor.role && actor.status) {{
        setRoleStatus(String(actor.role), String(actor.status), {{ force: true }});
      }}
    }});
    addRelayEventListener("role.queued", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("running");
      const force = payload.reason === "new_followup_turn";
      if (force) clearMarvisConversationPausedRows();
      setRoleStatus(payload.role, "queued", {{ force }});
    }});
    addRelayEventListener("role.streaming", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("running");
      setRoleStatus(payload.role, "streaming");
    }});
    addRelayEventListener("dispatch.verified", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      updateTaskStatus("running");
      setRoleStatus(payload.role, "streaming");
    }});
    addRelayEventListener("round.control", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const stateProjection = marvisStateProjectionFromTypedEvent(payload);
      const appliedState = applyMarvisRelayStateProjection(payload);
      hidePlanControlSurface();
      const decision = stateProjection.metadata.decision || payload.decision || "";
      if (!appliedState && decision === "cancel_plan") {{
        updateTaskStatus("interrupted");
      }} else if (!appliedState) {{
        updateTaskStatus("running");
        const nextRole = stateProjection.transition.goto || payload.next_role || payload.role || "";
        if (nextRole) setRoleStatus(nextRole, "queued", {{ force: true }});
      }}
      waitingControlInput = "";
    }});
    addRelayEventListener("dispatch.fallback", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      setRoleStatus(payload.role, "queued", {{ force: true }});
    }});
    addRelayEventListener("user.followup", (event) => {{
      const payload = parseRelayEvent(event);
      const roundId = activateRelayRound(payload);
      const key = payload.artifact_id ? `user_followup:${{payload.artifact_id}}` : `user_followup:${{payload.context_packet_id || Date.now()}}`;
      clearMarvisConversationPausedRows();
      appendMarvisConversationUser(payload.text || payload.latest_user_input || "", key, false, {{
        images: payload.images || [],
        files: payload.files || []
      }});
      appendMarvisConversationWaiting(roundId);
      updateTaskStatus("running");
      setRoleStatus("director", "queued", {{ force: true }});
    }});
    addRelayEventListener("user.input_queued", (event) => {{
      upsertPendingInput(parseRelayEvent(event));
    }});
    addRelayEventListener("user.input_steered", (event) => {{
      const payload = parseRelayEvent(event);
      upsertPendingInput(payload);
      appendMarvisConversationGuidance(payload);
    }});
    addRelayEventListener("user.input_cancelled", (event) => {{
      const payload = parseRelayEvent(event);
      removePendingInput(payload.id || payload.pending_input_id);
    }});
    addRelayEventListener("user.input_consumed", (event) => {{
      const payload = parseRelayEvent(event);
      removePendingInput(payload.id || payload.pending_input_id);
    }});
    addRelayEventListener("role.native_event", (event) => {{
      const payload = parseRelayEvent(event);
      renderMarvisWorkLogNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);
      if (!isCurrentRoundEvent(payload)) return;
      renderRelayNativeEvent(payload.role, payload.native_event || payload, payload.runtime_event_id);
    }});
    addRelayEventListener("role.output_delta", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      appendRoleStreamDelta(
        payload.role,
        payload.delta || payload.text || "",
        payload.runtime_event_id,
        payload
      );
      setRoleStatus(payload.role, "streaming");
    }});
    addRelayEventListener("role.followup_response", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const roundId = relayEventRoundId(payload);
      const displayText = payload.display_text || payload.text || payload.summary || "";
      if (marvisConversationTextIsStructuredArtifactPlaceholder(displayText)) {{
        setRoleStatus(payload.role || "director", payload.status || "passed");
        return;
      }}
      appendMarvisConversationAssistant(
        payload.role || "director",
        displayText,
        "followup_response",
        payload.artifact_id ? `followup_response:${{payload.artifact_id}}` : "",
        payload.status || "passed",
        roundId
      );
      setRoleStatus(payload.role || "director", payload.status || "passed");
    }});
    addRelayEventListener("routing.decision", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const role = payload.role || "director";
      clearRolePreview(role);
      setRoleStatus(role, payload.status || "passed");
    }});
    addRelayEventListener("role.envelope", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const envelope = {{ ...(payload.envelope || payload) }};
      const role = payload.role || envelope.role;
      clearRolePreview(role);
      const artifactType = String(envelope.artifact_type || "");
      const summaryText = String(envelope.summary || "");
      if (
        artifactType === "final_summary"
        && (role || "") === "director"
        && !String(envelope.handoff_to || "")
        && String(envelope.status || "") === "passed"
        && summaryText
        && !marvisConversationTextIsStructuredArtifactPlaceholder(summaryText)
      ) {{
        const finalSummaryKey = payload.artifact_id
          ? `final_summary_response:${{payload.artifact_id}}`
          : `final_summary_response:${{relayEventRoundId(envelope) || activeRelayRoundId || "1"}}:${{payload.runtime_event_id || event.lastEventId || summaryText}}`;
        appendMarvisConversationAssistant(
          role || "director",
          summaryText,
          "followup_response",
          finalSummaryKey,
          "passed",
          relayEventRoundId(envelope)
        );
      }}
      if (envelope.status) setRoleStatus(role, envelope.status);
    }});
    addRelayEventListener("handoff.created", (event) => {{
      const payload = parseRelayEvent(event);
      const marvisMetadata = payload.marvis_event?.metadata || {{}};
      const toRole = marvisMetadata.to_role || payload.to_role || payload.handoff_to;
      const fromRole = marvisMetadata.from_role || payload.from_role || "";
      const roundId = String(payload.round_id || "1");
      if (!isCurrentRoundEvent(payload)) return;
      if (toRole) setRoleStatus(toRole, "queued");
      const handoffKey = `handoff:${{roundId}}:${{fromRole}}:${{toRole || ""}}:${{payload.artifact_id || payload.summary || event.lastEventId || ""}}`;
      appendMarvisConversationHandoff(toRole, handoffKey, fromRole, roundId);
    }});
    addRelayEventListener("role.status", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const stateProjection = marvisStateProjectionFromTypedEvent(payload);
      const reason = payload.reason || payload.payload?.reason || "";
      const force = reason === "new_followup_turn";
      if (force) clearMarvisConversationPausedRows();
      const role = stateProjection.typedEvent.role || payload.role;
      const status = stateProjection.typedEvent.status || stateProjection.metadata.status || payload.status;
      setRoleStatus(role, status, {{ force }});
      if (TERMINAL_ROLE_STATUSES.has(status)) {{
        hidePlanControlSurface();
        clearRolePreview(role);
      }}
    }});
    addRelayEventListener("task.waiting_user", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      const interruptPayload = marvisInterruptPayloadFromTypedEvent(payload);
      updateTaskStatus("waiting_user");
      setRoleStatus(interruptPayload.role || "director", "waiting", {{ force: true }});
      renderMarvisWorkLogConfirmation(interruptPayload);
      ensurePlanControl(interruptPayload);
    }});
    addRelayEventListener("task.completed", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      applyMarvisRelayStateProjection(payload);
      updateTaskStatus("completed");
      clearAllRolePreviews();
    }});
    addRelayEventListener("task.interrupted", (event) => {{
      const payload = parseRelayEvent(event);
      if (!isCurrentRoundEvent(payload)) return;
      hidePlanControlSurface();
      applyMarvisRelayStateProjection(payload);
      updateTaskStatus("interrupted");
      clearAllRolePreviews();
    }});
    connectRelayEventSource();
    followupTextInput?.addEventListener("input", updateRelayComposerAction);
    document.addEventListener("marvis-relay-attachments-changed", updateRelayComposerAction);
    updateRelayComposerAction();
    followupComposer?.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const form = event.currentTarget;
      const data = Object.fromEntries(new FormData(form).entries());
      const attachments = window.marvisRelayAttachments?.payload() || {{}};
      const hasAttachments = Boolean((attachments.images || []).length || (attachments.files || []).length);
      if (!String(data.text || "").trim() && !hasAttachments) {{
        setRelayMutationStatus("请输入补充内容或添加附件。", true);
        return;
      }}
      if ((attachments.images || []).length) data.images = attachments.images;
      if ((attachments.files || []).length) data.files = attachments.files;
      setRelayMutationStatus("正在发送…");
      let localKey = "";
      try {{
        if (String(relayTaskStatus || "").trim() === "waiting_user" && waitingControlInput) {{
          const roundId = activeRelayRoundId || form.dataset.currentRoundId || CURRENT_ROUND_ID;
          await relayMutation(
            `/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/rounds/${{encodeURIComponent(roundId)}}/control${{TOKEN_SUFFIX}}`,
            {{ decision: waitingControlInput, comment: String(data.text || "").trim() }},
            followupSubmitButton,
          );
          waitingControlInput = "";
          form.reset();
          window.marvisRelayAttachments?.clear();
          hidePlanControlSurface();
          updateTaskStatus("running");
          setRelayMutationStatus("已提交，任务继续执行。");
          return;
        }}
        if (relayTaskAcceptsPendingInput()) {{
          const payload = await relayMutation(
            `/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/inputs${{TOKEN_SUFFIX}}`,
            data,
            followupSubmitButton,
          );
          const responseDisposition = String(payload.disposition || "pending");
          if (responseDisposition === "followup") {{
            const followup = payload.followup || payload;
            const roundId = activateRelayRound(followup);
            const key = followup.artifact_id ? `user_followup:${{followup.artifact_id}}` : `user_followup:${{followup.context_packet_id || Date.now()}}`;
            clearMarvisConversationPausedRows();
            appendMarvisConversationUser(followup.text || data.text || "已添加附件", key, false, {{
              images: followup.images || attachments.images || [],
              files: followup.files || attachments.files || []
            }});
            appendMarvisConversationWaiting(roundId);
            updateTaskStatus("running");
            setRoleStatus("director", "queued", {{ force: true }});
            setRelayMutationStatus("已加入当前任务。");
          }} else {{
            upsertPendingInput(payload.pending_input || payload);
            setRelayMutationStatus("已排队，将在当前轮结束后处理。");
          }}
          form.reset();
          window.marvisRelayAttachments?.clear();
          return;
        }}
        localKey = `local-followup:${{Date.now()}}`;
        clearMarvisConversationPausedRows();
        appendMarvisConversationUser(data.text || "已添加附件", localKey, true, attachments);
        appendMarvisConversationWaiting();
        updateTaskStatus("running");
        setRoleStatus("director", "queued", {{ force: true }});
        await relayMutation(
          `/api/relay/tasks/${{encodeURIComponent(TASK_ID)}}/message${{TOKEN_SUFFIX}}`,
          data,
          followupSubmitButton,
        );
        form.reset();
        window.marvisRelayAttachments?.clear();
        setRelayMutationStatus("已发送给总工程师。");
      }} catch (error) {{
        setRelayMutationStatus(error?.message || "发送失败，请重试。", true);
        if (localKey) {{
          markMarvisConversationUserFailed(localKey);
          clearMarvisConversationWaiting();
        }}
      }} finally {{
        updateRelayComposerAction();
      }}
    }});
    followupInterruptButton?.addEventListener("click", async (event) => {{
      event.preventDefault();
      if (!relayTaskIsRunning()) return;
      setRelayMutationStatus("正在中断当前执行…");
      try {{
        await relayMutation(`${{followupInterruptButton.dataset.interruptUrl}}${{TOKEN_SUFFIX}}`, {{}}, followupInterruptButton);
        updateTaskStatus("interrupted");
        clearAllRolePreviews();
        setRelayMutationStatus("已请求中断。", false);
      }} catch (error) {{
        setRelayMutationStatus(error?.message || "中断失败，请重试。", true);
      }} finally {{
        updateRelayComposerAction();
      }}
    }});
  </script>
</body>
</html>""")
