"""Historical worker live-page template."""

from __future__ import annotations

from html import escape


def render_legacy_live_page(agent_run_id: int) -> str:
    stream_path = f"/api/workers/{agent_run_id}/stream"
    safe_title = escape(f"Worker Live Stream #{agent_run_id}")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    body {{ background: var(--btn-primary-color); position: relative; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 12px 16px; background: rgba(25, 27, 32, 0.82); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border-bottom: 1px solid var(--border-default); }}
    main {{ padding: 12px 12px 132px; position: relative; z-index: 1; }}
    .event {{ white-space: pre-wrap; border-bottom: 1px solid var(--border-default); padding: 10px 4px; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }}
    .meta {{ color: #a1a1aa; font-size: 12px; margin-bottom: 4px; }}
    .approval_requested {{ color: #facc15; }}
    .failed {{ color: var(--color-error-light); }}
    .completed {{ color: var(--color-success); }}
    .controls {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; display: grid; gap: 8px; padding: 10px; background: linear-gradient(to top, rgba(0,0,0,.98) 55%, rgba(0,0,0,.85) 78%, rgba(0,0,0,0)); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-top: 1px solid var(--border-default); }}
    .row {{ display: flex; gap: 8px; min-width: 0; }}
    input {{ flex: 1; min-width: 0; border-radius: 8px; border: 1px solid var(--border-input); background: #17191f; color: var(--btn-primary-bg); padding: 11px; font-size: 15px; }}
    button {{ min-height: 40px; }}
    button.secondary {{ background: #1b1e25; color: var(--btn-primary-bg); }}
    button.warn {{ background: var(--color-error-light); color: #1b0707; }}
    .approval-actions {{ display: flex; gap: 8px; margin-top: 8px; }}
  </style>
</head>
<body class="aurora-bg noise-overlay">
  <header>
    <strong>Worker Live Stream</strong>
    <span id="state">connecting</span>
    <span id="cursor"></span>
  </header>
  <main id="events"></main>
  <section class="controls">
    <div class="row">
      <input id="prompt" placeholder="继续官方 Codex 会话">
      <button id="continue">发送</button>
    </div>
    <div class="row">
      <button class="secondary" id="steer">修正当前轮</button>
      <button class="warn" id="interrupt">中断</button>
    </div>
  </section>
  <script>
    const state = document.getElementById("state");
    const cursor = document.getElementById("cursor");
    const events = document.getElementById("events");
    const params = new URLSearchParams(location.search);
    const token = params.get("token") || "";
    let nativeThreadId = params.get("native_thread_id") || "";
    let nativeTurnId = "";
    const authHeaders = token ? {{"Authorization": "Bearer " + token}} : {{}};
    const promptInput = document.getElementById("prompt");
    const streamPath = token ? "{stream_path}?token=" + encodeURIComponent(token) : "{stream_path}";
    const source = new EventSource(streamPath);
    source.onopen = () => {{ state.textContent = "connected"; }};
    source.onerror = () => {{ state.textContent = "reconnecting"; }};
    source.onmessage = (message) => render(JSON.parse(message.data));
    [
      "lifecycle",
      "activity",
      "user_message",
      "text_delta",
      "reasoning_delta",
      "command_started",
      "command_output",
      "command_completed",
      "command_failed",
      "file_changed",
      "diff_updated",
      "approval_requested",
      "approval_resolved",
      "completed",
      "failed",
      "event"
    ].forEach(kind => {{
      source.addEventListener(kind, message => render(JSON.parse(message.data)));
    }});
    async function api(path, options = {{}}) {{
      const response = await fetch(path, {{
        ...options,
        headers: {{"Content-Type": "application/json", ...authHeaders, ...(options.headers || {{}})}}
      }});
      if (!response.ok) {{
        const body = await response.json().catch(() => ({{}}));
        throw new Error(body.error || response.statusText);
      }}
      return response.json().catch(() => ({{}}));
    }}
    function nativeMutationKey(operation) {{
      const runtime = window.WLCodexSurfaceRuntime;
      if (runtime && typeof runtime.mutationKey === "function") {{
        return runtime.mutationKey("native-" + operation);
      }}
      return "native-" + operation + "-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    }}
    async function nativeControl(action, body) {{
      if (!nativeThreadId) return;
      await api(`/api/native/codex/sessions/${{encodeURIComponent(nativeThreadId)}}/${{action}}`, {{
        method: "POST",
        body: JSON.stringify(body),
        headers: {{"Idempotency-Key": nativeMutationKey("session-" + action)}}
      }});
    }}
    async function resolveApproval(requestId, action) {{
      await api(`/api/native/codex/approvals/${{encodeURIComponent(requestId)}}/resolve`, {{
        method: "POST",
        body: JSON.stringify({{action}}),
        headers: {{"Idempotency-Key": nativeMutationKey("approval-" + requestId)}}
      }});
    }}
    document.getElementById("continue").onclick = () => nativeControl("continue", {{prompt: promptInput.value}});
    document.getElementById("steer").onclick = () => nativeControl("steer", {{prompt: promptInput.value, expected_turn_id: nativeTurnId}});
    document.getElementById("interrupt").onclick = () => nativeControl("interrupt", {{turn_id: nativeTurnId}});
    function render(event) {{
      const payload = event.payload || {{}};
      if (payload.native_thread_id) nativeThreadId = payload.native_thread_id;
      if (payload.native_turn_id) nativeTurnId = payload.native_turn_id;
      cursor.textContent = " last event " + event.id;
      const row = document.createElement("div");
      row.className = "event " + event.kind;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = "#" + event.id + " " + event.kind + " " + event.type;
      const body = document.createElement("div");
      body.textContent = payload.delta || payload.text || payload.summary || JSON.stringify(payload, null, 2);
      row.append(meta, body);
      if (event.kind === "approval_requested" && payload.source_kind === "codex_native" && payload.codexRequestId) {{
        const actions = document.createElement("div");
        actions.className = "approval-actions";
        const approveOnce = document.createElement("button");
        approveOnce.textContent = "批准一次";
        approveOnce.onclick = () => resolveApproval(payload.codexRequestId, "approve_once");
        const approveSession = document.createElement("button");
        approveSession.textContent = "本会话批准";
        approveSession.onclick = () => resolveApproval(payload.codexRequestId, "approve_session");
        const deny = document.createElement("button");
        deny.className = "secondary";
        deny.textContent = "拒绝";
        deny.onclick = () => resolveApproval(payload.codexRequestId, "deny");
        const cancel = document.createElement("button");
        cancel.className = "secondary";
        cancel.textContent = "取消";
        cancel.onclick = () => resolveApproval(payload.codexRequestId, "cancel");
        actions.append(approveOnce, approveSession, deny, cancel);
        row.append(actions);
      }}
      events.append(row);
      window.scrollTo(0, document.body.scrollHeight);
    }}
  </script>
</body>
</html>"""
