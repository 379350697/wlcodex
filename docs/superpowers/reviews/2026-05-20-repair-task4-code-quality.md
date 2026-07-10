# Code Quality Review — Repair Task 4: Historical Attach And Resume

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Code Quality Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Files Reviewed

| File | Change | Assessment |
|------|--------|------------|
| `wlcodex/surfaces/terminal/manager.py` | +40 lines | `SESSION_PICKER` + `attach_historical` + updated `open_for_conversation` |

## Quality Dimensions

### Precision (PASS)

- `OnsiteDecisionKind.SESSION_PICKER`: 1 enum value added
- `OnsiteDecision.available_sessions`: 1 field added (tuple, default `()`)
- `open_for_conversation`: restructured with early-return pattern (no logic duplicated)
- `attach_historical`: 30-line method, clear guard + reuse logic

### Guard Quality (PASS)

| Guard | Rationale |
|-------|-----------|
| `resumability is SUMMARY_ONLY` → `ValueError` | Prevents invalid attach |
| `agent == "claude"` → `"stream_json"` | Correct strategy mapping |
| Existing ref scan before `self.attach()` | Reuse, don't duplicate |

### No Over-Abstraction (PASS)

- `SESSION_PICKER` is 1 enum value, not a new class hierarchy
- `attach_historical` is 1 method, not a new manager/strategy
- `available_sessions` is 1 tuple field, not a new data structure

### Pattern Consistency (PASS)

- `SESSION_PICKER` follows same Enum pattern as `AUTO_OPEN`/`START_CARD`
- `attach_historical` delegates to `self.attach()` for actual creation
- Decision order matches Repair Spec exactly

### Test Quality

| Suite | Result |
|-------|--------|
| `tests/test_workbench_onsite_terminal.py` | 27 passed |
| `tests/test_terminal_surface.py` | 37 passed |
| Total | 64 passed, 0 failed |

## Blocking Issues: NONE
