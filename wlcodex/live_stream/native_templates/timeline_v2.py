from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote


def render_timeline_v2_template(
    provider: str,
    context: dict[str, Any],
) -> str:
    provider_name = provider.strip() or "codex"
    provider_label = _provider_label(provider_name)
    api_base = f"/api/native/{quote(provider_name, safe='')}"
    native_thread_id = str(context.get("native_thread_id") or "")
    initial_events = [_event_dict(event) for event in context.get("initial_events") or []]
    initial_json = json.dumps(initial_events, ensure_ascii=False)
    thread_json = json.dumps(native_thread_id, ensure_ascii=False)
    api_json = json.dumps(api_base, ensure_ascii=False)
    provider_json = json.dumps(provider_name, ensure_ascii=False)
    title = f"{provider_label} Timeline V2"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; --bg: #050507; --panel: #101116; --line: #454b5c; --text: #f8fafc; --muted: #c3cad5; --accent: #adc8ff; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ width: min(760px, 100%); margin: 0 auto; padding: 18px 14px 92px; }}
    header {{ position: sticky; top: 0; z-index: 2; margin: -18px -14px 14px; padding: 14px; background: rgba(5, 5, 7, .94); border-bottom: 1px solid var(--line); backdrop-filter: blur(16px); }}
    h1 {{ margin: 0; font-size: 18px; font-weight: 700; }}
    .meta {{ color: var(--muted); font-size: 12px; }}
    .timeline {{ display: flex; flex-direction: column; gap: 12px; }}
    .item {{ border: 1px solid var(--line); border-radius: 8px; padding: 11px 12px; background: var(--panel); white-space: pre-wrap; overflow-wrap: anywhere; }}
    .item.user {{ margin-left: 20%; background: #0c1020; border-color: #2c3766; }}
    .item.assistant {{ margin-right: 8%; }}
    .item.system {{ color: var(--muted); }}
    .item-role {{ display: block; margin-bottom: 6px; color: var(--muted); font-size: 12px; }}
    .composer {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 3; padding: 10px 12px 14px; background: rgba(5, 5, 7, .96); border-top: 1px solid var(--line); }}
    .composer-inner {{ display: grid; grid-template-columns: 1fr auto; gap: 8px; width: min(760px, 100%); margin: 0 auto; }}
    textarea {{ min-height: 44px; max-height: 120px; resize: vertical; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: #11131a; color: var(--text); font: inherit; }}
    button {{ min-height: 44px; border: 1px solid #7088c7; border-radius: 8px; padding: 0 16px; background: #25345f; color: var(--text); font-weight: 700; }}
    button:disabled {{ opacity: .55; }}
    .empty {{ color: var(--muted); padding: 24px 2px; }}
    .status {{ margin-top: 6px; color: var(--muted); font-size: 12px; }}
    :focus-visible {{ outline: 3px solid var(--accent); outline-offset: 3px; }}
    .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
    @media (forced-colors: active) {{
      * {{ forced-color-adjust: auto; }}
      .item, textarea, button {{ border: 1px solid CanvasText; }}
    }}
  </style>
</head>
<body data-native-template="timeline-v2">
  <main>
    <header>
      <h1>{escape(title)}</h1>
      <div class="meta">只读 timeline 投影；发送后等待 timeline 确认</div>
    </header>
    <section id="timeline" class="timeline" role="log" aria-live="off" aria-relevant="additions text"></section>
    <div id="empty" class="empty">暂无 timeline 消息</div>
  </main>
  <form id="composer" class="composer">
    <div class="composer-inner">
      <label class="sr-only" for="prompt">继续 {escape(provider_label)} 会话</label>
      <textarea id="prompt" placeholder="继续 {escape(provider_label)} 会话"></textarea>
      <button id="send" type="submit">发送</button>
    </div>
    <div id="status" class="status" role="status" aria-live="polite"></div>
  </form>
  <script>
    const PROVIDER = {provider_json};
    const API_BASE = {api_json};
    let nativeThreadId = {thread_json};
    let latestEventId = 0;
    let source = null;
    const renderedEvents = new Set();
    const itemNodes = new Map();
    const initialEvents = {initial_json};
    const timeline = document.getElementById("timeline");
    const empty = document.getElementById("empty");
    const status = document.getElementById("status");
    const promptEl = document.getElementById("prompt");
    const sendEl = document.getElementById("send");
    window.wlcodexNativeTimelineV2State = {{ provider: PROVIDER, renderedEvents, itemNodes, get latestEventId() {{ return latestEventId; }} }};

    function token() {{
      return localStorage.getItem("wlcodexToken") || new URLSearchParams(location.search).get("token") || "";
    }}
    async function api(path, options = {{}}) {{
      const headers = Object.assign({{"Content-Type": "application/json"}}, options.headers || {{}});
      const accessToken = token();
      if (accessToken) headers.Authorization = "Bearer " + accessToken;
      const response = await fetch(path, Object.assign({{}}, options, {{headers}}));
      if (!response.ok) throw new Error(await response.text() || response.statusText);
      return response.json();
    }}
    function nativeTimelinePath(params) {{
      const search = new URLSearchParams(params);
      const accessToken = token();
      if (accessToken) search.set("token", accessToken);
      return `${{API_BASE}}/sessions/${{encodeURIComponent(nativeThreadId)}}/timeline?${{search.toString()}}`;
    }}
    function nativeTimelineStreamPath(afterId) {{
      const search = new URLSearchParams();
      const accessToken = token();
      if (accessToken) search.set("token", accessToken);
      if (afterId) search.set("after", String(afterId));
      const suffix = search.toString();
      return `${{API_BASE}}/sessions/${{encodeURIComponent(nativeThreadId)}}/timeline/stream` + (suffix ? "?" + suffix : "");
    }}
    function eventKey(event) {{
      if (event.id || event.sequence) return String(event.id || event.sequence);
      const payload = event.payload || {{}};
      return [event.kind || "", payload.native_turn_id || payload.turnId || "", payload.itemId || payload.item_id || eventText(event)].join(":");
    }}
    function itemKey(event, role) {{
      const payload = event.payload || {{}};
      return [role, payload.native_turn_id || payload.turnId || "", payload.itemId || payload.item_id || event.kind || ""].join(":");
    }}
    function eventText(event) {{
      const payload = event.payload || {{}};
      return String(payload.text || payload.delta || payload.summary || payload.message || "");
    }}
    function roleFor(event) {{
      if (event.kind === "user_message") return "user";
      if (event.kind === "text_delta" || event.kind === "message_completed") return "assistant";
      return "system";
    }}
    function ensureItemNode(event, role) {{
      const key = itemKey(event, role);
      if (itemNodes.has(key)) return itemNodes.get(key);
      empty.hidden = true;
      const node = document.createElement("article");
      node.className = "item " + role;
      const label = document.createElement("span");
      label.className = "item-role";
      label.textContent = role === "user" ? "你" : (role === "assistant" ? PROVIDER : event.kind);
      const body = document.createElement("div");
      node.append(label, body);
      timeline.append(node);
      const record = {{node, body}};
      itemNodes.set(key, record);
      return record;
    }}
    function shouldAutoScroll() {{
      return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 96;
    }}
    function scrollToBottom() {{
      window.scrollTo({{top: document.documentElement.scrollHeight, behavior: "auto"}});
    }}
    function renderTimelineEvent(event) {{
      if (!event || !event.kind) return;
      if (event.id) latestEventId = Math.max(latestEventId, Number(event.id || 0));
      const key = eventKey(event);
      if (renderedEvents.has(key)) return;
      const text = eventText(event);
      if (!text.trim()) return;
      const followTail = shouldAutoScroll();
      const role = roleFor(event);
      const record = ensureItemNode(event, role);
      if (event.kind === "text_delta") {{
        record.body.textContent += text;
      }} else {{
        record.body.textContent = text;
      }}
      renderedEvents.add(key);
      if (followTail) scrollToBottom();
      else status.textContent = "有新消息";
      if (event.kind === "message_completed") status.textContent = "回复已完成";
    }}
    async function pollTimeline() {{
      if (!nativeThreadId) return;
      const snapshot = await api(nativeTimelinePath(`after=${{latestEventId}}&limit=100`));
      for (const event of snapshot.events || []) renderTimelineEvent(event);
    }}
    function openTimelineStream() {{
      if (!nativeThreadId) return;
      if (source) source.close();
      source = new EventSource(nativeTimelineStreamPath(latestEventId));
      source.onmessage = message => renderTimelineEvent(JSON.parse(message.data));
      ["user_message", "text_delta", "message_completed", "reasoning_delta", "command_started", "command_output", "command_completed", "command_failed", "approval_requested", "approval_resolved"].forEach(kind => {{
        source.addEventListener(kind, message => renderTimelineEvent(JSON.parse(message.data)));
      }});
      source.onerror = () => {{
        status.textContent = "连接中断，正在增量拉取";
        pollTimeline().catch(() => {{}});
      }};
    }}
    document.getElementById("composer").addEventListener("submit", async event => {{
      event.preventDefault();
      if (!nativeThreadId) {{
        status.textContent = "请先从带 native_thread_id 的链接打开 v2 会话";
        return;
      }}
      const prompt = promptEl.value.trim();
      if (!prompt) return;
      sendEl.disabled = true;
      status.textContent = "提交中，等待 timeline 确认";
      try {{
        await api(`${{API_BASE}}/sessions/${{encodeURIComponent(nativeThreadId)}}/continue`, {{
          method: "POST",
          body: JSON.stringify({{prompt, force_new_turn: true}})
        }});
        promptEl.value = "";
        status.textContent = "已提交，等待 timeline 确认";
        await pollTimeline();
      }} catch (error) {{
        status.textContent = error.message || String(error);
      }} finally {{
        sendEl.disabled = false;
      }}
    }});
    document.addEventListener("visibilitychange", () => {{
      if (document.hidden) {{
        if (source) source.close();
        source = null;
        return;
      }}
      pollTimeline().catch(() => {{}});
      openTimelineStream();
    }});
    initialEvents.forEach(renderTimelineEvent);
    openTimelineStream();
  </script>
</body>
</html>"""


def _provider_label(provider: str) -> str:
    labels = {
        "codex": "Codex",
        "claude": "Claude",
        "antigravity": "Antigravity",
    }
    return labels.get(provider, provider[:1].upper() + provider[1:])


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_display_json_dict"):
        value = event.to_display_json_dict()
    elif hasattr(event, "to_json_dict"):
        value = event.to_json_dict()
    elif isinstance(event, dict):
        value = dict(event)
    else:
        value = {}
    return value
