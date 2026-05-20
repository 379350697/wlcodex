# Final Gate — WLCodex Workbench Session Library And Task Internalization Repair

**Date**: 2026-05-20
**Reviewer**: Closed-Loop Verification Agent (fourth pass — BLOCKER A (/status leaks) + BLOCKER B (continuation semantics))

---

## Verdict: BLOCKED

> **阻塞原因**: Live Telegram smoke 未执行。这是硬性阻塞。
> **已修复**: BLOCKER A (/status 内部ID泄漏) + BLOCKER B (历史continuation 语义断裂)。

---

## 6 BLOCKERs 修复记录

### BLOCKER 1: callback identity 漂移 — ✅ 已修复

**问题**: `_render_start_card_buttons` 和 `_render_session_picker_buttons` 使用 `chat_id` 编码 callback，当 `chat_id ≠ conversation_id` 时，`handle_conversation_callback` 报 "对话不存在或已被删除"。

**修复**:
- `_render_start_card_buttons(self, chat_id)` → `_render_start_card_buttons(self, conversation_id)`
- `_render_session_picker_buttons(self, chat_id, sessions)` → `_render_session_picker_buttons(self, conversation_id, sessions)`
- `_handle_session_picker_callback` 内所有 sub-button 的 `f"conv:{chat_id}:"` → `f"conv:{conversation_id}:"`
- 4 个调用点全部改为传递 `conversation_id`（当 `conversation_id is None` 时 fallback 到 `chat_id`）

### BLOCKER 2: 历史 session 继续后没有进入 Onsite — ✅ 已修复

**问题**: `attach_session`/`resume_session` 只调用 `attach_historical` 但不记录 mode switch，导致下一条普通文本仍走 Cockpit controller。

**修复**:
- 新增 `_attach_and_enter_onsite` 方法：attach + 记录 `CONVERSATION_MODE_SWITCHED` 事件（to_mode="terminal"）
- 新增 `_record_mode_switch` 方法：将 mode switch 写入 `runtime_events`
- `attach_session` 和 `resume_session` 都走 `_attach_and_enter_onsite`，确保下一条文本进入 `_handle_terminal_text`

### BLOCKER 3: 历史 session 继续没有创建新的内部 task/run — ✅ 已修复

**问题**: `attach_historical` 只创建 `TerminalSessionRef`，不是 task/run。Final Gate 错误地把 `TerminalSessionRef` 当 task/run。

**修复**:
- `resume_session`: 在 attach 之前调用 `self._ledger.create_agent_run()` 创建内部 `agent_run`，agent 匹配原 session，`external_session_id` 携带原 session 的 `internal_ref`，role="continuation"
- `resume_from_summary`: 同样创建 `agent_run`，但不带 `external_session_id`
- `attach_session`（纯接管）：不创建 task/run（仅进入 Onsite，不给"继续"承诺）

### BLOCKER 4: Codex/Claude session reference 持久化 — ✅ 已修复

**问题**: `/codex` 创建 thread 后未写入 `agent_runs.external_session_id`；`/claude` 完成时未捕获 `stream_event.session_id` 或 `result.session_id`。

**修复**:
- **Codex direct** (`handle_codex_direct`): thread 创建后调用 `update_agent_run_status(..., external_session_id=thread_id)`
- **Claude direct streaming** (`_run_claude_direct_async`): 在 stream 循环中捕获 `stream_event.session_id`
- **Claude direct non-streaming**: 捕获 `result.session_id`
- Success 和 failure 路径都传递 `external_session_id=claude_session_id`

### BLOCKER 5: 用户文案扫描 — ✅ 已修复

**问题**: `/status` 输出暴露 "内部任务：#" 和 "内部 Claude 运行：#"（`render_conversation_status` 中 "高级详情" 部分）。`/status` 是主菜单入口，不得暴露内部 ID。

**修复**:
- 删除 `render_conversation_status` 的 "高级详情" section（包含 `内部任务：#{...}`、`内部 Claude 运行：#{...}`）
- 删除 `render_session_list` 的 `内部任务 #{...}` 行
- Legacy diagnostic 函数（`render_task_card`, `render_task_list`）保持不变，仅被 `/task`, `/pause`, `/abort` 等 diagnostic 命令调用

### BLOCKER 6: Final Gate 证据逻辑 — ✅ 已修复

**问题**: Final Gate 写 RELEASE_CANDIDATE，但自己承认 live smoke 未执行。硬性违规。

**修复**: 本文档。Verdict = BLOCKED。在 live smoke 完成之前不得放行。

---

## 测试证据

### Targeted Workbench Suite

```
pytest tests/test_workbench_core.py tests/test_workbench_cockpit_menu.py \
  tests/test_workbench_commands.py tests/test_workbench_onsite_terminal.py \
  tests/test_workbench_execution_modes.py tests/test_workbench_runtime_state.py \
  tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py \
  tests/test_workbench_session_library.py -q
```
**结果: 185 passed, 0 failed**

### Existing Related Suite

```
pytest tests/test_controller_flow.py tests/test_telegram_handlers.py \
  tests/test_terminal_surface.py tests/test_dual_surface_integration.py \
  tests/test_runtime_projector.py tests/test_runtime_state_replay.py \
  tests/test_recovery.py tests/test_router.py tests/test_status.py \
  tests/test_task_service.py -q
```
**结果: 303 passed, 0 failed**

---

## 语义漂移审查

| 概念 | 状态 |
|------|------|
| Workbench (仅 /new 创建) | 无漂移 — conversation_id 保持一致 |
| Agent Session (可回顾/可接管/可继续) | 无漂移 — AgentSessionLibrary 投影 agent_runs |
| Task (内部执行票据) | 无漂移 — resume 时创建 hidden agent_run，用户不可见 |
| Cockpit / Onsite (同一 Workbench 两个视图) | 无漂移 — ViewMode 枚举独立于 ExecutionMode |
| Execution mode / View mode 分离 | 无漂移 — 独立枚举 |
| /codex 纯 Codex, /claude 纯 Claude | 无漂移 — 背靠背调用，0 交叉 |
| Claude-only + "让 Codex 验收" | 无漂移 — VERIFY action button |
| /terminal 不死会话 | 无漂移 — 3-tier 决策（attach→picker→start card） |
| /product 不回放 raw terminal | 无漂移 — 纯文案确认 |
| /sessions 历史现场库 | 无漂移 — AgentSessionLibrary 输出 |
| 用户文案无内部 ID | 无漂移 — /status 已修复，legacy diagnostic 保留 |

---

## 用户文案扫描

### 正常用户入口（clean）
- `/help` / `/start`: ✅ 无内部词
- `/status` (active conversation): ✅ 不再显示 "内部任务 #" / "内部 Claude 运行 #"
- `/sessions`: ✅ AgentSessionLibrary 输出，无 raw ID
- `/terminal` (start/picker): ✅ 产品文案 + action buttons
- 普通文本响应: ✅ `_handle_workspace_busy` 产品文案

### Legacy diagnostic 入口（允许内部词）
- `/task`, `/tasks`, `/pause`, `/abort`, `/archive`, `/continue`, `/steer`, `/fork`: 保留 task ID/blocker/queue position

### Banned terms 扫描结果
所有 banned terms (`terminal.enabled`, `external_session_id`, `session id`, `thread id`, `runtime_events`, `agent_run`, `任务 #`, `阻塞者`, `队列位置`) 在 normal user copy 中均 **ABSENT** 或 **CONFINED to diagnostic paths**。

---

## Remaining Blockers

### BLOCKER A: /status 内部ID泄漏 — ✅ 已修复 (pass 4)

**根因**: `StatusCommand` handler 在 `runtime_store` 可用时调用 `build_runtime_status` + `format_status_display`，暴露 `运行 #<agent_run_id>`, `最近事件 #<event_id>`, `Agent 运行记录`, `事件总数`。

**修复**: 删除 `StatusCommand` 中 `build_runtime_status`/`format_status_display` 分支。`/status` 统一使用 `render_conversation_status`（产品清洁格式化器）。诊断输出保留给 `/trace`。

### BLOCKER B: 历史 continuation 语义断裂 — ✅ 已修复 (pass 4)

**根因**: "继续修改"按钮立即调用 `create_agent_run`，不等用户输入。没有 pending continuation 状态。

**修复**:
- 新增 `_pending_continuation` dict：存储 `{agent, internal_ref, title, source_run_id, summary_only}`
- `resume_session` / `resume_from_summary` 点击：存入 pending state，进入 Onsite
- `attach_session` 点击：清空 pending，仅接管现场
- `_handle_terminal_text`：首条 Onsite 文本 → `_execute_pending_continuation` → 创建 hidden task + agent_run（含 `hidden_task_id` 链接 + `external_session_id` 持久化）+ terminal attach + `send_input` + `set_task_status("running")`
- 不创建新 Workbench（conversation_id 保持一致）

### B1: Live Telegram smoke 未执行 — **BLOCKING**

硬性要求：必须在真实 Telegram + 真实 Codex/Claude backend 环境执行以下 smoke checklist：

1. `/new` 真人历史现场 smoke
2. 普通文本：按默认流程
3. `/terminal` — 不死会话
4. `/product` — 不回放 raw terminal
5. `/claude` Reply exactly with: claude only ok
6. 点击：让 Codex 验收
7. `/sessions` — 历史现场库，无内部 ID
8. 点击最近 Claude 现场：查看回顾
9. 点击最近 Claude 现场：接管现场
10. 输入：continue from this historical session
11. `/product` — 确认回到驾驶舱
12. `/codex` Reply exactly with: codex only ok
13. `/sessions` — 确认 Codex 现场出现
14. `/new` 第二个工作台 — 确认新 Workbench

**无 live smoke → 必须 BLOCKED。**

---

## Release Note Summary (for when unblocked)

WLCodex 现在保持一个持续远程工作台直到你主动开启新工作台。你可以在驾驶舱和现场视图之间切换，浏览历史 Codex/Claude 工作现场，从任意历史现场继续工作。默认工程流程（Codex → Claude → Codex）保持不变。所有内部技术标识不在普通用户界面暴露。

---

**Verdict: BLOCKED — 等待 live Telegram smoke 执行**
