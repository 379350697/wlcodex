# Code Quality Review — Task 2: Cockpit Menu And Help UX

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer:** Code Quality Reviewer
**Date:** 2026-05-20
**Verdict: PASS**

## Precondition

Spec Compliance Reviewer PASSED with 1 notation (workspace placeholder — architecturally justified).

## Quality Blockers

None.

## Precision of Changes

**`wlcodex/menu.py`:** 4 insertions, 5 deletions. Only `_NATURAL_COMMANDS` changed. `_PRIMARY_COMMANDS` and `build_bot_commands()` logic untouched.

**`wlcodex/status.py`:** 43 insertions, 36 deletions. Only three string bodies modified: `render_help()`, `render_conversation_help(natural)`, `render_conversation_help(legacy)`. No function signatures, no logic, no imports changed.

No dead code, no unused imports, no stale comments.

## Over-Abstraction: None

The implementation edits two module-level constants and three string literals. It does not introduce new classes, helper functions, module state, import dependencies, or template engines. The existing pattern (profile string dispatch, pure string renderers) is preserved without modification.

## Pattern Preservation

| Module Pattern | Status |
|---------------|--------|
| `menu.py`: two-tuple list → profile dispatch → return copy | Unchanged |
| `status.py`: pure string renderers, no side effects | Unchanged |
| `status.py`: early return for natural, fall through to legacy | Unchanged |
| `controller.py:127`: `HELP_TEXT = render_conversation_help()` module-level constant | Picks up new text without signature change |
| `main.py:522`: `build_bot_commands(config_profile)` | Picks up new menu automatically |

## Duplicate Logic: None

Three help text variants are independent string literals. No shared template engine, no parameterized interpolation. For three variants, a shared template would be premature abstraction.

## Test Quality

All 9 tests are business-facing, mapping to spec requirements:

- `test_natural_menu_has_exactly_six_daily_actions` — exact list comparison with order
- `test_natural_menu_hides_typed_only_commands` — each hidden command individually asserted
- `test_legacy_menu_is_preserved` — positive + negative assertions
- `test_natural_help_contains_cockpit_language` — semantic phrases, not keywords
- `test_natural_help_does_not_leak_implementation_keys` — case-insensitive + substring checks
- `test_natural_help_does_not_reference_product_terminal_split` — validates old language removal
- `test_menu_labels_use_cockpit_product_language` — dict-based exact match for all 6 labels
- `test_legacy_help_still_lists_advanced_diagnostics` — non-regression

No assertion is trivially true. Every assertion maps to a spec requirement or plan step.

## State Drift Risk: None

All changed functions are pure — same input always produces same output. No mutable state, no caching, no module-level variables that could diverge between Cockpit and Onsite.

## Concurrent Task Ownership: No Conflict

Plan ownership matrix confirms zero file overlap between Task 2 and Tasks 3–7. Task 5's `controller.py` imports `render_conversation_help` from `status.py` — a downstream consumer relationship, not a conflict.

## Error-Path Completeness

`build_bot_commands` falls through to `_PRIMARY_COMMANDS` for non-"natural" profiles. `render_conversation_help` uses same branching. Both functions have no external dependencies that could fail.

## Non-Blocking Notes

### Note 1: `MODE_LABELS` terminology not updated

**Location:** `wlcodex/status.py:318-322`

Current labels (`"总工程师"`, `"Codex 直聊"`, `"Claude 直聊"`) pre-date the spec's "默认工程流程"/"只问 Codex"/"只叫 Claude" language. Not changed by Task 2 (diff confirms `MODE_LABELS` untouched). Belongs to Task 5 (execution modes).

### Note 2: Legacy help workflow description inconsistency (pre-existing)

**Location:** `wlcodex/status.py:384` vs `line 400`

Header says "默认流程：Codex -> Claude -> Codex" but "对话模式" section says "直接发消息 — 默认交给 Codex 分析". Pre-existing inconsistency preserved unchanged. Task 5 scope.

### Note 3: Natural help workspace placeholder

**Location:** `wlcodex/status.py:372`

`"工作区：当前项目"` is a static placeholder. The renderer cannot access runtime workspace state. As noted by Spec Compliance Reviewer — architecturally justified.

### Note 4: `render_help()` has redundant workflow descriptions

Line 173 (new header) and line 189 (old body) both describe the default workflow. Removing the old line risks breaking the existing `test_help_is_not_empty`. Minimal-change tradeoff — acceptable.

## Required Fixes

None.
