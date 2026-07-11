from __future__ import annotations

import json
from html import escape
from urllib.parse import quote

from wlcodex.live_stream.native_pages import native_provider_display_name

def render_native_token_entry_page(return_to: str = "/native/codex") -> str:
    return_to_json = json.dumps(safe_native_return_path(return_to))
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WLCodex</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    body { display: grid; place-items: center; padding: 26px; min-height: 100vh; position: relative; }
    main { width: min(420px, 100%); display: grid; gap: 20px; padding: 32px 28px; border-radius: 20px; background: rgba(17, 18, 23, 0.65); border: 1px solid rgba(255, 255, 255, 0.08); backdrop-filter: blur(16px); box-shadow: var(--shadow-lg), var(--shadow-glow); z-index: 1; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }
    h1 { margin: 0; font-size: 32px; font-weight: var(--weight-black); background: var(--gradient-accent); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
    p { margin: 0; color: var(--text-placeholder); line-height: 1.5; }
    form { display: grid; gap: 12px; }
    input { width: 100%; height: 54px; border-radius: 14px; border: 1px solid var(--border-popover); background: #14161d; color: var(--text-primary); padding: 0 14px; font-size: 16px; }
    button { height: 52px; border: 0; border-radius: 14px; background: linear-gradient(135deg, #f4f4f5 0%, #e0e7ff 50%, #f4f4f5 100%); background-size: 200% 100%; color: var(--bg-canvas); font-size: 16px; font-weight: var(--weight-black); box-shadow: 0 4px 20px rgba(244, 244, 245, 0.1); transition: background-position 400ms ease, box-shadow 300ms ease, transform 150ms ease; }
    button:not(:disabled):hover { background-position: 100% 0; box-shadow: 0 4px 28px rgba(244, 244, 245, 0.18); transform: translateY(-1px); }
    button:active { transform: translateY(0); }
    .status { min-height: 20px; color: var(--color-error-light); font-size: 14px; }
  </style>
</head>
<body class="aurora-bg noise-overlay">
  <main>
    <h1>Codex</h1>
    <p>输入访问令牌后进入手机远程控制页。令牌只保存在此浏览器本地。</p>
    <form id="tokenForm">
      <input id="tokenInput" name="token" placeholder="访问令牌" autocomplete="current-password" autofocus>
      <button type="submit">进入</button>
      <div class="status" id="status"></div>
    </form>
  </main>
  <script>
    function rememberToken(value) {
      try { localStorage.setItem("wlcodexToken", value); } catch (_error) {}
      document.cookie = "wlcodex_token=" + encodeURIComponent(value) + "; Path=/; Max-Age=2592000; SameSite=Lax";
    }
    const params = new URLSearchParams(location.search);
    const queryToken = params.get("token") || "";
    let savedToken = "";
    try { savedToken = localStorage.getItem("wlcodexToken") || ""; } catch (_error) {}
    const token = queryToken || savedToken;
    if (token) {
      rememberToken(token);
      location.replace(__RETURN_TO__);
    }
    const input = document.getElementById("tokenInput");
    const status = document.getElementById("status");
    document.getElementById("tokenForm").onsubmit = event => {
      event.preventDefault();
      const value = input.value.trim();
      if (!value) {
        status.textContent = "请输入访问令牌";
        input.focus();
        return;
      }
      rememberToken(value);
      location.href = __RETURN_TO__;
    };
  </script>
</body>
</html>""".replace("__RETURN_TO__", return_to_json)


def safe_native_return_path(path: str) -> str:
    value = str(path or "").strip()
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/native/codex"
    if "\r" in value or "\n" in value:
        return "/native/codex"
    return value


def render_native_login_ticket_page(ticket: str, provider_name: str = "codex") -> str:
    safe_ticket = escape(ticket, quote=True)
    display_name = escape(native_provider_display_name(provider_name))
    safe_provider = quote(provider_name, safe="")
    safe_action = f"/native/{safe_provider}/login?ticket={quote(ticket, safe='')}"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Codex</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    body {{ display: grid; place-items: center; padding: 26px; min-height: 100vh; position: relative; }}
    main {{ width: min(420px, 100%); display: grid; gap: 20px; padding: 32px 28px; border-radius: 20px; background: rgba(17, 18, 23, 0.65); border: 1px solid rgba(255, 255, 255, 0.08); backdrop-filter: blur(16px); box-shadow: var(--shadow-lg), var(--shadow-glow); z-index: 1; animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }}
    h1 {{ margin: 0; font-size: 32px; font-weight: var(--weight-black); background: var(--gradient-accent); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }}
    p {{ margin: 0; color: var(--text-placeholder); line-height: 1.5; }}
    form {{ display: grid; gap: 12px; }}
    button {{ height: 52px; border: 0; border-radius: 14px; background: linear-gradient(135deg, #f4f4f5 0%, #e0e7ff 50%, #f4f4f5 100%); background-size: 200% 100%; color: var(--bg-canvas); font-size: 16px; font-weight: var(--weight-black); box-shadow: 0 4px 20px rgba(244, 244, 245, 0.1); transition: background-position 400ms ease, box-shadow 300ms ease, transform 150ms ease; }}
    button:not(:disabled):hover {{ background-position: 100% 0; box-shadow: 0 4px 28px rgba(244, 244, 245, 0.18); transform: translateY(-1px); }}
    button:active {{ transform: translateY(0); }}
  </style>
</head>
<body class="aurora-bg noise-overlay">
  <main>
    <h1>{display_name}</h1>
    <p>点击进入手机远程控制页。此链接只能使用一次。</p>
    <form method="post" action="{safe_action}">
      <input type="hidden" name="ticket" value="{safe_ticket}">
      <button type="submit">进入 {display_name}</button>
    </form>
  </main>
</body>
</html>"""
