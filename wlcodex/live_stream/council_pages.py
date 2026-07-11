from __future__ import annotations

from collections.abc import Callable

def render_council_review_page(*, replace_html_icons: Callable[[str], str]) -> str:
    return replace_html_icons("""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>议会审核</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    html { background: var(--bg-canvas); }
    body { background: transparent; }
    header { position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 52px 1fr auto; gap: 12px; align-items: center; min-height: 72px; padding: 10px 18px; background: rgba(5,5,8,.82); backdrop-filter: blur(20px) saturate(1.4); -webkit-backdrop-filter: blur(20px) saturate(1.4); border-bottom: 1px solid var(--border-header); }
    .circle { width: 46px; height: 46px; font-size: 28px; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .config-link { min-height: 42px; padding: 0 14px; border-radius: 21px; border: 1px solid #34363d; color: var(--text-primary); display: inline-grid; place-items: center; text-decoration: none; font-weight: var(--weight-bold); }
    main { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 420px); gap: 18px; width: min(1180px, 100%); margin: 0 auto; padding: 18px; }
    section { min-width: 0; }
    .panel { border: 1px solid var(--border-card); background: var(--bg-surface); border-radius: 8px; padding: 14px; }
    .stack { display: grid; gap: 12px; }
    label { display: grid; gap: 6px; color: var(--text-secondary); font-size: 14px; font-weight: var(--weight-bold); }
    input, textarea, select { border: 1px solid var(--border-input-alt); border-radius: 8px; background: #1b1d24; }
    textarea { min-height: 124px; resize: vertical; line-height: 1.48; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .run { min-height: 48px; border: 0; border-radius: 8px; background: linear-gradient(135deg, #f4f4f5 0%, #e0e7ff 50%, #f4f4f5 100%); background-size: 200% 100%; color: var(--bg-canvas); font-weight: var(--weight-black); font-size: 16px; box-shadow: 0 4px 20px rgba(244, 244, 245, 0.1); transition: background-position 400ms ease, box-shadow 300ms ease; }
    .run:not(:disabled):hover { background-position: 100% 0; box-shadow: 0 4px 28px rgba(244, 244, 245, 0.18); }
    .run:disabled { opacity: .55; cursor: progress; }
    .muted { color: var(--text-muted); font-size: 13px; line-height: 1.45; }
    .seat-list, .results { display: grid; gap: 10px; }
    .seat, .result { position: relative; border: 1px solid var(--border-card); border-radius: 8px; padding: 12px 12px 12px 18px; background: var(--bg-elevated); animation: fadeInUp var(--duration-enter, 250ms) var(--ease-out-expo, cubic-bezier(0.19, 1, 0.22, 1)) both; }
    .seat::before { content: ""; position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px; border-radius: 2px; background: var(--seat-accent, var(--color-link)); }
    .seat:nth-child(1) { --seat-accent: #ef4444; }
    .seat:nth-child(2) { --seat-accent: #3b82f6; }
    .seat:nth-child(3) { --seat-accent: #f59e0b; }
    .seat:nth-child(4) { --seat-accent: #a855f7; }
    .seat:nth-child(5) { --seat-accent: #22c55e; }
    .seat-head, .result-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .seat-title, .result-title { font-weight: var(--weight-black); }
    .badge { border-radius: 999px; padding: 4px 8px; background: #22252e; color: var(--text-secondary); font-size: 12px; white-space: nowrap; }
    .summary { margin-top: 8px; color: #d9dde6; line-height: 1.5; white-space: pre-wrap; }
    .session-link { display: inline-grid; place-items: center; min-height: 32px; margin-top: 10px; padding: 0 10px; border: 1px solid #3b3f49; border-radius: 8px; color: var(--text-primary); text-decoration: none; font-size: 13px; font-weight: var(--weight-bold); }
    .error { color: var(--color-error-text); }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; padding-bottom: 96px; }
      header { grid-template-columns: 46px 1fr; }
      .config-link { grid-column: 1 / -1; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body class="aurora-bg noise-overlay">
  <header>
    <a class="circle" href="/native" aria-label="back">‹</a>
    <h1>议会审核</h1>
    <a class="config-link" href="/council/seats">席位配置</a>
  </header>
  <main>
    <section class="panel stack">
      <div>
        <strong>Review Packet</strong>
        <div class="muted">把同一份方案锁定后交给五个席位审核。</div>
      </div>
      <label>标题<input id="title" value="方案审核"></label>
      <label>方案<textarea id="proposal" placeholder="粘贴要审核的方案、需求或实现摘要"></textarea></label>
      <label>上下文<textarea id="context" placeholder="可选：相关背景、约束、当前分支、风险"></textarea></label>
      <div class="row">
        <label>成功标准<textarea id="success" placeholder="每行一条"></textarea></label>
        <label>约束<textarea id="constraints" placeholder="每行一条"></textarea></label>
      </div>
      <label>工作目录<select id="cwd"><option value="">正在读取项目...</option></select></label>
      <button class="run" id="run" disabled>启动议会审核</button>
      <div class="muted" id="status">正在读取席位配置...</div>
    </section>
    <aside class="stack">
      <section class="panel stack">
        <div>
          <strong>当前席位</strong>
          <div class="muted" id="diversity">模型多样性等待计算</div>
        </div>
        <div class="seat-list" id="seats"></div>
      </section>
      <section class="panel stack">
        <div>
          <strong>审核结果</strong>
          <div class="muted">每个席位会独立显示状态，启动后可打开原生会话。</div>
        </div>
        <div class="results" id="results"></div>
      </section>
    </aside>
  </main>
  <script>
    const DEFAULT_CONFIG_URL = "/api/council/config/default";
    const PROJECTS_URL = "/api/council/projects";
    const RUN_URL = "/api/council/runs";
    const POLL_INTERVAL_MS = 1200;
    const STORAGE_KEY = "wlcodexCouncilConfig";
    let config = null;
    let projects = [];
    let pollTimer = null;
    let activeRunId = "";

    const $ = (id) => document.getElementById(id);
    const lines = (text) => text.split("\\n").map((item) => item.trim()).filter(Boolean);
    const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[ch]));

    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {"Content-Type": "application/json", ...(options.headers || {})},
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function savedConfig(defaultConfig) {
      try {
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        if (saved && Array.isArray(saved.assignments)) return {...defaultConfig, ...saved};
      } catch (_error) {}
      return defaultConfig;
    }

    function renderSeats() {
      const assignments = (config.assignments || []).filter((seat) => seat.enabled !== false);
      const definitions = Object.fromEntries((config.seat_definitions || []).map((seat) => [seat.seat_id, seat]));
      $("seats").innerHTML = assignments.map((assignment) => {
        const definition = definitions[assignment.seat_id] || {};
        return `<div class="seat"><div class="seat-head"><span class="seat-title">${esc(definition.role || assignment.seat_id)}</span><span class="badge">${esc(assignment.provider)} · ${esc(assignment.model)}</span></div><div class="summary">${esc(definition.mission || "")}</div></div>`;
      }).join("");
      const unique = new Set(assignments.map((item) => `${item.provider}:${item.model}`)).size;
      $("diversity").textContent = `模型多样性 ${unique}/${assignments.length || 0}`;
    }

    function renderProjects(payload) {
      projects = Array.isArray(payload.projects) ? payload.projects : [];
      if (!projects.length) {
        $("cwd").innerHTML = `<option value="">未找到 ${esc(payload.root || "/Users/wl/projects")} 下的项目</option>`;
        return;
      }
      const preferred = projects.find((project) => project.name === "wlcodex") || projects[0];
      $("cwd").innerHTML = projects.map((project) => `<option value="${esc(project.cwd)}">${esc(project.name)}</option>`).join("");
      $("cwd").value = preferred.cwd;
    }

    function boardStatusLabel(status) {
      return ({
        queued: "等待席位启动",
        running: "席位审核中",
        partial: "部分席位已启动，等待输出",
        completed: "审核完成",
        failed: "审核失败",
      })[String(status || "")] || "等待席位输出";
    }

    function seatStatusLabel(status) {
      return ({
        queued: "等待启动",
        running: "启动中",
        started: "已启动，等待输出",
        completed: "已完成",
        failed: "失败",
      })[String(status || "")] || "等待";
    }

    function consensusLabel(consensus) {
      return ({
        no_completed_reviews: "等待席位输出",
        approved: "通过",
        approved_with_changes: "带修改通过",
        rejected: "未通过",
        mixed: "意见不一致",
      })[String(consensus || "")] || "等待汇总";
    }

    function isBoardActive(board) {
      return ["queued", "running"].includes(String((board && board.status) || ""));
    }

    function setRunBusy(isBusy) {
      $("run").disabled = isBusy;
      $("run").textContent = isBusy ? "议会审核中..." : "启动议会审核";
    }

    function setBoardStatus(board) {
      $("status").textContent = board ? boardStatusLabel(board.status) : "席位配置已就绪。";
    }

    function renderBoard(board) {
      const synthesis = board.synthesis || {};
      const results = board.results || [];
      const chairLabel = synthesis.consensus ? consensusLabel(synthesis.consensus) : boardStatusLabel(board.status);
      $("results").innerHTML = [
        `<div class="result"><div class="result-head"><span class="result-title">Chair Synthesis</span><span class="badge">${esc(chairLabel)}</span></div><div class="summary">${esc((synthesis.required_changes || []).join("\\n") || "等待席位输出同步。")}</div></div>`,
        ...results.map((result) => {
          const sessionLink = result.native_session_path ? `<a class="session-link" href="${esc(result.native_session_path)}">打开原生会话</a>` : "";
          const verdict = result.verdict ? ` · ${esc(result.verdict)}` : "";
          return `<div class="result"><div class="result-head"><span class="result-title">${esc(result.seat_id)}</span><span class="badge">${esc(seatStatusLabel(result.status))}${verdict}</span></div><div class="summary">${esc(result.summary || result.error || "")}</div>${sessionLink}</div>`;
        })
      ].join("");
    }

    function stopPolling(resetRun = false) {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      if (resetRun) {
        activeRunId = "";
        setRunBusy(false);
      }
    }

    function startPolling(runId) {
      stopPolling();
      if (!runId) return;
      activeRunId = runId;
      setRunBusy(true);
      pollTimer = setInterval(async () => {
        try {
          const board = await api(`${RUN_URL}/${encodeURIComponent(runId)}`);
          renderBoard(board);
          setBoardStatus(board);
          if (!isBoardActive(board)) stopPolling(true);
        } catch (error) {
          stopPolling(true);
          $("status").innerHTML = `<span class="error">${esc(error.message)}</span>`;
        }
      }, POLL_INTERVAL_MS);
    }

    async function loadConfig() {
      const [defaults, projectPayload] = await Promise.all([
        api(DEFAULT_CONFIG_URL),
        api(PROJECTS_URL),
      ]);
      config = savedConfig(defaults);
      renderProjects(projectPayload);
      renderSeats();
      setBoardStatus(null);
      setRunBusy(false);
    }

    $("run").onclick = async () => {
      if (activeRunId) {
        $("status").textContent = "当前议会还在审核中";
        return;
      }
      activeRunId = "starting";
      setRunBusy(true);
      $("status").textContent = "正在启动席位...";
      $("results").innerHTML = "";
      try {
        const board = await api(RUN_URL, {
          method: "POST",
          body: JSON.stringify({
            async: true,
            title: $("title").value,
            proposal: $("proposal").value,
            context: $("context").value,
            success_criteria: lines($("success").value),
            constraints: lines($("constraints").value),
            cwd: $("cwd").value,
            config,
          }),
        });
        renderBoard(board);
        setBoardStatus(board);
        if (isBoardActive(board)) {
          startPolling(board.run_id);
        } else {
          stopPolling(true);
        }
      } catch (error) {
        stopPolling(true);
        $("status").innerHTML = `<span class="error">${esc(error.message)}</span>`;
      }
    };
    loadConfig().catch((error) => {$("status").innerHTML = `<span class="error">${esc(error.message)}</span>`;});
  </script>
</body>
</html>""")
def render_council_seats_page(*, replace_html_icons: Callable[[str], str]) -> str:
    return replace_html_icons("""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>议会席位配置</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="/static/animations.css">
  <link rel="stylesheet" href="/static/effects.css">
  <style>
    html { background: var(--bg-canvas); }
    body { background: transparent; }
    header { position: sticky; top: 0; z-index: 2; display: grid; grid-template-columns: 52px 1fr auto; gap: 12px; align-items: center; min-height: 72px; padding: 10px 18px; background: rgba(5,5,8,.82); backdrop-filter: blur(20px) saturate(1.4); -webkit-backdrop-filter: blur(20px) saturate(1.4); border-bottom: 1px solid var(--border-header); }
    .circle { width: 46px; height: 46px; font-size: 28px; }
    h1 { margin: 0; font-size: 22px; letter-spacing: 0; }
    .review-link, button.save { min-height: 42px; padding: 0 14px; border-radius: 21px; border: 1px solid #34363d; color: var(--text-primary); background: #202126; display: inline-grid; place-items: center; text-decoration: none; font-weight: var(--weight-bold); }
    main { display: grid; gap: 14px; width: min(980px, 100%); margin: 0 auto; padding: 18px; }
    .toolbar { display: flex; justify-content: space-between; gap: 12px; align-items: center; border: 1px solid var(--border-card); background: var(--bg-surface); border-radius: 8px; padding: 14px; }
    .muted { color: var(--text-muted); font-size: 13px; line-height: 1.45; }
    .seat-grid { display: grid; gap: 12px; }
    .seat { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(140px, .7fr) minmax(160px, .8fr) auto; gap: 10px; align-items: center; border: 1px solid var(--border-card); border-radius: 8px; padding: 12px; background: var(--bg-elevated); }
    .seat-title { display: grid; gap: 5px; min-width: 0; }
    .role { font-weight: var(--weight-black); font-size: 17px; }
    .mission { color: var(--text-placeholder); font-size: 13px; line-height: 1.45; }
    label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 12px; font-weight: var(--weight-bold); }
    input, select { border: 1px solid var(--border-input-alt); border-radius: 8px; background: #1b1d24; padding: 10px 11px; }
    .switch { width: 54px; height: 32px; }
    @media (max-width: 760px) {
      header { grid-template-columns: 46px 1fr; }
      .review-link { grid-column: 1 / -1; }
      .toolbar { display: grid; }
      .seat { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body class="aurora-bg noise-overlay">
  <header>
    <a class="circle" href="/native" aria-label="back">‹</a>
    <h1>议会席位配置</h1>
    <a class="review-link" href="/council">议会审核</a>
  </header>
  <main>
    <section class="toolbar">
      <div>
        <strong>默认议会</strong>
        <div class="muted">固定五席：唱反调、第一性原理、扩展思路、局外人、执行者</div>
        <div class="muted" id="diversity">读取席位中...</div>
      </div>
      <button class="save" id="save">保存配置</button>
    </section>
    <section class="seat-grid" id="seats"></section>
  </main>
  <script>
    const DEFAULT_CONFIG_URL = "/api/council/config/default";
    const STORAGE_KEY = "wlcodexCouncilConfig";
    let config = null;
    let models = {};
    const $ = (id) => document.getElementById(id);
    const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[ch]));

    async function api(path) {
      const response = await fetch(path);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function savedConfig(defaultConfig) {
      try {
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        if (saved && Array.isArray(saved.assignments)) return {...defaultConfig, ...saved};
      } catch (_error) {}
      return defaultConfig;
    }

    function modelOptions(provider, current) {
      const available = models[provider] || [];
      const ids = available.map((item) => item.id || item.name || item.model).filter(Boolean);
      if (current && !ids.includes(current)) ids.unshift(current);
      return ids.map((id) => `<option value="${esc(id)}"${id === current ? " selected" : ""}>${esc(id)}</option>`).join("");
    }

    function render() {
      const providers = config.providers || [];
      const definitions = Object.fromEntries((config.seat_definitions || []).map((seat) => [seat.seat_id, seat]));
      $("seats").innerHTML = (config.assignments || []).map((assignment, index) => {
        const definition = definitions[assignment.seat_id] || {};
        const providerOptions = providers.map((provider) => {
          const id = provider.provider;
          return `<option value="${esc(id)}"${id === assignment.provider ? " selected" : ""}>${esc(id)}</option>`;
        }).join("");
        return `<div class="seat" data-index="${index}">
          <div class="seat-title"><span class="role">${esc(definition.role || assignment.seat_id)}</span><span class="mission">${esc(definition.mission || "")}</span></div>
          <label>Provider<select data-field="provider">${providerOptions}</select></label>
          <label>Model<select data-field="model">${modelOptions(assignment.provider, assignment.model)}</select></label>
          <label>启用<input class="switch" type="checkbox" data-field="enabled"${assignment.enabled !== false ? " checked" : ""}></label>
        </div>`;
      }).join("");
      updateDiversity();
    }

    function updateDiversity() {
      const enabled = (config.assignments || []).filter((seat) => seat.enabled !== false);
      const unique = new Set(enabled.map((seat) => `${seat.provider}:${seat.model}`)).size;
      $("diversity").textContent = `模型多样性 ${unique}/${enabled.length || 0}，允许同一模型担任多个席位。`;
    }

    $("seats").onchange = (event) => {
      const row = event.target.closest(".seat");
      if (!row) return;
      const assignment = config.assignments[Number(row.dataset.index)];
      const field = event.target.dataset.field;
      if (field === "enabled") assignment.enabled = event.target.checked;
      if (field === "provider") {
        assignment.provider = event.target.value;
        const first = (models[assignment.provider] || [])[0];
        assignment.model = first ? (first.id || first.name || first.model || assignment.model) : assignment.model;
        render();
        return;
      }
      if (field === "model") assignment.model = event.target.value;
      updateDiversity();
    };

    $("save").onclick = () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        mode: config.mode,
        enabled: config.enabled,
        assignments: config.assignments,
        required_seat_ids: config.required_seat_ids,
      }));
      $("diversity").textContent = "配置已保存。";
      setTimeout(updateDiversity, 900);
    };

    api(DEFAULT_CONFIG_URL).then((defaults) => {
      models = defaults.models || {};
      config = savedConfig(defaults);
      render();
    }).catch((error) => {
      $("diversity").textContent = error.message;
    });
  </script>
</body>
</html>""")
