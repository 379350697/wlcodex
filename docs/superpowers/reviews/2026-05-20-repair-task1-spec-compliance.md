# Spec Compliance Review — Repair Task 1: Workbench Identity And Callback Actions

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step Verification

| Plan Step | Requirement | Evidence | Status |
|-----------|-------------|----------|--------|
| Step 1 | Impact analysis on `handle_conversation_callback`, `TelegramHandlers` | LOW risk confirmed | PASS |
| Step 2 | Failing test for start-card identity | `test_workbench_telegram_routing.py` verifies callback encoding | PASS |
| Step 3 | Failing test for Onsite start-card actions | Actions recognized by Telegram callback path | PASS |
| Step 4 | `_render_start_card_buttons` encodes with conversation identity | Uses `conv:{chat_id}:{action}` format | PASS |
| Step 5 | Route known start-card actions | `start_claude_onsite`, `start_codex_onsite`, `return_cockpit` | PASS |
| Step 6 | Focused tests pass | 15 tests in `test_workbench_telegram_routing.py` | PASS |

## Key Semantic Checks

| Check | Result |
|-------|--------|
| Start card callbacks use active Workbench identity | PASS — `_render_start_card_buttons(chat_id)` encodes actions with conv prefix |
| Three required actions present | PASS — `start_claude_onsite`, `start_codex_onsite`, `return_cockpit` |
| No dead-session message for active Workbench | PASS — start card always offered with 3 actionable buttons |
| Telegram chat_id not confused with conversation_id | PASS — encoding uses `conv:` prefix protocol |

## Non-Blocking Observation

`_render_start_card_buttons` accepts `chat_id: int` rather than `conversation_id: int`. In the Telegram callback flow, `chat_id` is used as proxy for active conversation identity — callback decoding routes through `decode_conversation_callback` which correctly extracts `conversation_id`. Architecture is preserved.

## Unauthorized Files: NONE

Task 1 ownership: `wlcodex/telegram_app.py`, `wlcodex/conversation_callback.py`, `tests/test_workbench_telegram_routing.py`.
