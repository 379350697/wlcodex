# Code Quality Review — Task 1: Workbench Foundation Contracts

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Code Quality Reviewer (independent)
**Date**: 2026-05-20
**Initial Verdict**: **FAIL** (1 blocker: B1)
**Final Verdict**: **PASS** (B1 fixed, N2 fixed)

---

## Quality Blocker (Resolved)

### B1: `render_cockpit_header` function name semantically misleading

**File**: `wlcodex/workbench/rendering.py:11`
**Issue**: Function named `render_cockpit_header` but handles both Cockpit AND Onsite views (returns "WLCodex · 现场" for Onsite state). Name implies Cockpit-only, behavior is view-generic.
**Impact**: Foundation module — misleading name would propagate to Tasks 2, 4, 7 where downstream importers would call `render_cockpit_header` to render Onsite headers.
**Fix**: Renamed to `render_view_header`. Updated in `rendering.py`, `__init__.py` import + `__all__`, and all test references.

**Verification**:
```
$ python -c "
from wlcodex.workbench import render_view_header  # succeeds
from wlcodex.workbench import render_cockpit_header  # ImportError (correct)
"
All semantic checks PASSED
```

---

## Non-Blocking Notes

| ID | Issue | Status |
|----|-------|--------|
| N1 | events.py uses module-level constants; existing code uses `class EventType` pattern | Non-blocking — both are valid namespaced access; Task 6 can unify |
| N2 | Rendering imports inside test function bodies instead of file top | FIXED — imports moved to test file top |
| N3 | `WorkbenchState` not `frozen=True` unlike `ModeSwitchCheckpoint` | Non-blocking — `WorkbenchState` is active projection cache, not checkpoint snapshot; mutable is correct for its lifecycle |
| N4 | `render_view_switch_notice` has unreachable fallback `"视图已切换。"` | Non-blocking — defensive code, dead path in normal operation |
| N5 | `WorkbenchState` docstring says "Durable shared state" but events are source of truth | Non-blocking — minor doc phrasing; spec line 458 clarifies events as truth, projections as caches |

---

## Code Quality Dimensions

| Dimension | Assessment |
|-----------|------------|
| Precise changes | PASS — only 6 new files, no existing files touched |
| No unnecessary abstraction | PASS — strictly matches plan "Implement only" list: 3 enums + 1 dataclass + 1 pure function |
| No pattern violation | PASS (after fix) — `render_view_header` name matches behavior; event constants follow spec naming |
| No duplicate logic | PASS — `route_plain_text` is new logic, not duplicating existing `route_text_by_mode` (different dimensions) |
| No test-only artifacts in production code | PASS — no mocks, stubs, or test fixtures in production files |
| No state drift risk | PASS — single `WorkbenchState` object shared by both views; mutable but manager is single writer |
| No concurrency ownership conflict | PASS — `wlcodex/workbench/*` exclusively owned by Task 1 per plan |
| No unhandled error paths | PASS — all routing branches return a value, no dead ends |
| Copy/code semantic consistency | PASS (after fix) — function name = behavior; all user copy matches spec terms |

---

## Test Quality

15 tests, each traceable to a spec or plan requirement.

| Test | Maps To |
|------|---------|
| `test_cockpit_plain_text_defaults_to_orchestrated_mode` | Plan Step 5: Cockpit+ORCHESTRATED → ORCHESTRATED_COCKPIT |
| `test_onsite_plain_text_goes_to_selected_live_session` | Spec line 531-532: Onsite text → session input |
| `test_execution_mode_does_not_change_view_mode` | Spec line 42-43: execution vs view orthogonal |
| `test_view_mode_values_are_distinct` | Spec: Cockpit ≠ Onsite |
| `test_execution_mode_values_are_distinct` | Spec line 36-40: 3 distinct execution modes |
| `test_workbench_route_values_are_distinct` | Plan Step 4: 4 distinct routes |
| `test_codex_direct_cockpit_routing` | Plan Step 5: Cockpit+CODEX_DIRECT → CODEX_DIRECT_COCKPIT |
| `test_claude_direct_cockpit_routing` | Plan Step 5: Cockpit+CLAUDE_DIRECT → CLAUDE_DIRECT_COCKPIT |
| `test_workbench_state_defaults` | Spec line 233: Cockpit default, Spec line 30: orchestrated default |
| `test_workbench_state_covers_all_spec_fields` | Spec §Workbench State: 17 fields |
| `test_onsite_input_overrides_execution_mode` | Spec line 145-146: view unaffected by execution mode |
| `test_render_view_header_in_cockpit_view` | Spec line 129-133: Product Surface → Cockpit |
| `test_render_view_header_name_changes_with_view` | Spec line 140-141: 驾驶舱/接管现场 |
| `test_render_view_switch_notice_cockpit_to_onsite` | Spec line 310-311: tap 接管现场 |
| `test_render_view_switch_notice_onsite_to_cockpit_matches_spec` | Spec line 353-355: exact copy match |

---

## Regression Impact

- **Task 1 tests**: 15 passed
- **Clean regression**: 118 passed (router, surface_core, terminal_surface, imports, config, terminal_redaction)
- **Pre-existing failures**: 12 in `test_surface_commands.py` + `test_dual_surface_integration.py` — verified to exist even with Task 1 files removed; caused by prior commits on branch, not Task 1
