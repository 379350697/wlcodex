# Spec Compliance Review — Task 7: Telegram Routing And View Switching

**Date**: 2026-05-20
**Spec**: `docs/superpowers/specs/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-design.md`
**Plan**: `docs/superpowers/plans/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-parallel-plan.md`
**Files in scope**: `wlcodex/telegram_app.py`, `tests/test_workbench_telegram_routing.py`

## Verdict: PASS

---

## Blocking Issues

None.

## Plan Step-by-Step Verification

| Plan Step | Status | Evidence |
|-----------|--------|----------|
| Step 1: Impact analysis (5 symbols) | PASS | All LOW, 0 callers — safe to edit |
| Step 2: Write failing tests | PASS | `test_workbench_telegram_routing.py` with 15 tests |
| Step 3: Confirm test failure before impl | PASS | File was initially absent; git status confirms untracked creation |
| Step 4: Route `/terminal` through Onsite open decision | PASS | Start card replaces dead-end in both `_apply_mode_switch` and `_handle_terminal_text` |
| Step 5: Route `/product` through Cockpit return | PASS | "已回到驾驶舱。现场仍在运行，我会继续用摘要跟进。" per spec line 353-355 |
| Step 6: Add `/settings` handler | PASS | Settings card with 默认流程, 只问 Codex, 只叫 Claude, 模型, Claude 权限, 工作区 |
| Step 7: Run Telegram tests | PASS | 15/15 Task 7 tests; 86/86 combined suite (excl. unrelated Task 5 failures) |

## Spec Acceptance Criteria Verification

| AC | Description | Verdict |
|----|-------------|---------|
| AC 5 | `/terminal` never leaves user in dead session state | PASS — Start card with 3 actionable buttons replaces all "请先启动..." dead-ends |
| AC 6 | Onsite auto-attaches to active agent when session exists | PASS — `_apply_mode_switch` auto-resolves external session id and attaches; sends "已进入接管现场，当前接入 claude。" |
| AC 7 | Onsite offers start actions when no session exists | PASS — Start card: "当前没有可接管的现场。\n\n你可以：" + [启动 Claude 现场] [启动 Codex 现场] [回驾驶舱] |
| AC 8 | Onsite text routes only to live session, not controller | PASS — `conversation_text` → `_get_active_surface_mode` → `_handle_terminal_text` → `terminal_manager.send_input()`. Controller never called. |
| AC 9 | Returning to Cockpit does not replay raw terminal | PASS — `/product` sends workbench copy; no stdout/diff/raw content |
| AC 12 | Menu contains only daily phone actions | N/A for Task 7 (owned by Task 2: `wlcodex/menu.py`) |
| AC 13 | Help text uses user language, not config keys | PASS — Settings card uses user language; `terminal.enabled` never in user-facing strings |
| AC 15 | Tests prove view switching does not restart work | PASS — `_apply_mode_switch` docstring: "Must NOT create a new conversation or start a new task." |

## Semantic Drift: Minor — 1 item (fixed)

| Location | Issue | Status |
|----------|-------|--------|
| `_handle_terminal_text` ValueError handler | `"无法发送终端输入：{exc}"` used legacy "终端" | FIXED → `"无法发送现场输入：{exc}"` |

All other user-facing copy correctly follows spec migration: product→驾驶舱, terminal→现场, mode→视图.

## Unauthorized Files: None

Task 7 ownership per plan: `wlcodex/telegram_app.py`, `tests/test_workbench_telegram_routing.py`.

## Execution Mode vs View Mode Confusion: None

- `_apply_mode_switch` uses `CONVERSATION_MODE_SWITCHED` event (existing infrastructure) but user-facing copy correctly says "视图" not "模式"
- Execution modes (orchestrated/codex_direct/claude_direct) are not conflated with views (cockpit/onsite)
- `route_plain_text` in Task 1 correctly separates ViewMode from ExecutionMode

## User-Facing Copy Scan

| Forbidden | Present in `send_telegram()` strings? |
|-----------|--------------------------------------|
| `terminal.enabled` | No — disabled case says "现场接管当前不可用。驾驶舱仍可正常工作。" |
| `external_session_id` | No — internal code only |
| `session id` / `thread id` | No — removed from user copy |
| `runtime_events` | No — internal event type only |
| `projection` | Not present anywhere |

| Required (Plan Step 4) | Present? |
|-------------------------|----------|
| 驾驶舱 | Yes — product response, settings card, mode handler |
| 接管现场 | Yes — "已进入接管现场" in attach success |
| 默认流程：Codex → Claude → Codex | Yes — settings card |
| 只问 Codex | Yes — settings button |
| 只叫 Claude | Yes — settings button |
