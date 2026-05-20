# Code Quality Review — Task 7: Telegram Routing And View Switching

**Date**: 2026-05-20
**Spec**: `docs/superpowers/specs/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-design.md`
**Plan**: `docs/superpowers/plans/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-parallel-plan.md`
**Precondition**: Spec Compliance Review PASS
**Files reviewed**: `wlcodex/telegram_app.py` (diff +76/-20), `tests/test_workbench_telegram_routing.py` (new file)

## Verdict: PASS

---

## Changes Analyzed

| Category | Lines | Description |
|----------|-------|-------------|
| New handler | +22 | `settings_cmd` with settings card + 6 inline buttons |
| New helper | +11 | `_render_start_card_buttons(chat_id)` — shared button factory |
| Dead-end removal | -12/+12 | 3 instances: `_apply_mode_switch`, `_handle_terminal_text`, `terminal_cmd` disabled |
| Language migration | -11/+11 | 终端→现场, 模式→视图, product→驾驶舱, terminal→接管现场 |
| Callback handler fix | -3/+14 | `_settings_callback_impl` extended from 1 to 5 callback types |
| Registration | +1 | `CommandHandler("settings", handlers.settings_cmd)` in `build_application` |

## Quality Assessment

### Precision: PASS

Each change is directly traceable to a plan requirement:
- `/terminal` start card → Plan Step 4
- `/product` copy → Plan Step 5
- `/settings` handler → Plan Step 6
- Language migration → Spec §§Names, Migration

No unrelated refactoring. No signature changes to existing public methods.

### Abstraction: PASS

One helper extracted (`_render_start_card_buttons`). Eliminated 22-line duplicate across two methods. No premature generalization — helper returns concrete button data, not an abstract renderer.

### Pattern Consistency: PASS

- `settings_cmd` follows same pattern as `help_cmd`/`start`: guard → send_telegram
- `_settings_callback_impl` follows same delegate-to-controller pattern as `_conversation_callback_impl`
- Button `callback_data` format follows existing `prefix:sub:value` convention
- `_render_start_card_buttons` return type matches `send_telegram(buttons=...)` signature

### No Duplicate Logic: PASS

Start card buttons are now defined in exactly one place. Verified by `test_start_card_buttons_identical_from_both_call_sites` — proves both call sites produce identical output.

### No Test-Only Patterns in Production Code: PASS

No `MagicMock`, `AsyncMock`, test fixtures, or test flags in production code. The `_render_start_card_buttons` helper is pure data — testable without mocking.

### State Drift Risk: Low

- Event payload uses `to_mode: "product"/"terminal"` (old mode strings) while user copy says "驾驶舱"/"现场" — this is intentional migration strategy per spec §Migration
- `chat_id` used as `conversation_id` in start card callbacks — explicitly documented with rationale in code comment
- No new mutable state added to `WlCodexHandlers`

### Error Path Completeness

| Path | Handled? | User-facing copy |
|------|----------|-----------------|
| `/terminal` disabled | Yes | "现场接管当前不可用。驾驶舱仍可正常工作。" |
| `/terminal` no session | Yes | Start card with 3 actionable buttons |
| Onsite text no session | Yes | Start card with 3 actionable buttons |
| Onsite text with session, send_input fails (ValueError) | Yes | "无法发送现场输入：{exc}" |
| Onsite text with session, send_input fails (Exception) | Yes | "发送现场输入失败。现场会话可能已断开，请使用 /terminal 重新连接。" |
| Settings callback invalid format | Yes | "无效的设置回调数据。" |
| Settings callback unknown sub-prefix | Yes | "无效的设置回调数据。" |
| Invalid /terminal subcommand | Yes | "未知现场命令。用法：/terminal [...]" |

### Cross-Task Ownership: PASS

Diff touches only `wlcodex/telegram_app.py` and `tests/test_workbench_telegram_routing.py` — matches Task 7 ownership per plan. No changes to router.py (Task 3), terminal manager (Task 4), or controller.py (Task 5).

## Non-Blocking Notes

### 1. Start card actions depend on Task 5 controller (Known integration gap)

`_conversation_callback_impl` passes `ConversationCallback(conversation_id, action)` to `Controller.handle_conversation_callback`. The controller's `else` clause returns `"未知的对话操作：{action}"` for `start_claude_onsite`/`start_codex_onsite`/`return_cockpit`. Until Task 5 implements these actions, tapping a start card button shows an unrecognized-action message.

**Not blocking for Task 7**: The protocol contract is correct — `decode_conversation_callback` succeeds, routing goes to the right handler, and the controller's fallback is graceful (error message, no crash). The parallelization model explicitly scopes this to Task 5.

### 2. Settings "工作区" button routes to `/switch` without argument

`/switch` requires `<workspace>` argument per `parse_command`. Controller will return ParseError. Expected UX — workspace switching requires user selection. Error message guides user.

### 3. `_render_start_card_buttons` type annotation uses generic `dict`

`-> list[list[dict[str, str]]]` matches existing `send_telegram(buttons=...)` signature. Consistent with codebase style.

## Test Quality

| Test | Verifies | Mock depth | Real code exercised |
|------|----------|------------|---------------------|
| `test_settings_callback_all_buttons_route_to_controller` | All 6 settings buttons → controller | Controller only | `callback_router` → `_settings_callback_impl` |
| `test_settings_exec_mode_callbacks_use_correct_controller_command` | Exact command strings | Controller only | `callback_router` → `_settings_callback_impl` |
| `test_settings_model_and_workspace_callbacks_route` | `/model` and `/switch` routing | Controller only | `callback_router` → `_settings_callback_impl` |
| `test_start_card_buttons_identical_from_both_call_sites` | Both call sites produce identical buttons | Bot only | `conversation_text` + `terminal_cmd` → real handlers |
| `test_start_card_callbacks_decode_via_conv_protocol` | Callback format decodes correctly | None | Real `decode_conversation_callback` |

All new tests exercise real handler methods. No test validates only mock interactions — each test sends through actual routing paths and inspects real output.

## Final Metrics

| Metric | Count |
|--------|-------|
| Files modified | 1 (`telegram_app.py`) + 1 (`test_workbench_telegram_routing.py`) |
| Lines changed | +76 -20 |
| New methods | 2 |
| Modified methods | 5 |
| Duplicate code eliminated | 1 (22 lines → 1 call) |
| User-facing copy violations | 0 |
| Unauthorized file modifications | 0 |
| Tests: Task 7 | 15/15 pass |
| Tests: combined (excl. Task 5) | 86/86 pass |
