# Code Quality Review — Task 6: Workbench Runtime Events And Recovery Projection

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Code Quality Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS** (1 minor defect, 0 blocking)

---

## Files Reviewed

| File | Lines changed | Type |
|------|--------------|------|
| `wlcodex/runtime_events.py` | +25 | Add 17 EventType constants |
| `wlcodex/runtime_state.py` | +155 | Add WorkbenchRuntimeState + replay + pass-through |
| `wlcodex/runtime_projector.py` | +29 | Add compat mappings + summaries |
| `tests/test_workbench_runtime_state.py` | 512 (new) | 26 tests across 8 classes |

---

## 1. Code Structure & Organization

### EventType Class (`runtime_events.py`)

**Observation — Duplicate constant assignments (Minor)**

`WORKBENCH_EXECUTION_MODE_SELECTED` and `ONSITE_SESSION_ORPHANED` each appear twice:

```
Line 170:  WORKBENCH_EXECUTION_MODE_SELECTED = "workbench.execution_mode.selected"
Line 207:  WORKBENCH_EXECUTION_MODE_SELECTED = "workbench.execution_mode.selected"  # duplicate

Line 173:  ONSITE_SESSION_ORPHANED = "onsite.session.orphaned"
Line 214:  ONSITE_SESSION_ORPHANED = "onsite.session.orphaned"                      # duplicate
```

Python class-attribute semantics: the second assignment overwrites the first with an identical value. No runtime bug, no memory leak, no semantic difference. But the duplicate violates the single-source-of-truth principle the codebase otherwise follows. The two comment groups ("Workbench execution mode" at line 169 vs "Workbench view / execution mode" at line 204) suggest a merge artifact.

**Remediation**: Remove one of the two blocks. Keep the semantically grouped block (lines 204-218) and delete the scattered constants at lines 169-173.

**Severity**: Minor (no behavioral impact, purely organizational)

### WorkbenchRuntimeState + Replay (`runtime_state.py`)

Block is appended at module bottom (line 956+). Follows existing section-header convention (`# ---`). Module-level lookup tables (`_VIEW_MODE_MAP`, `_VALID_EXECUTION_MODES`, `_COCKPIT_SURFACES`, `_ONSITE_SURFACES`) declared before the dataclass — correct ordering for readability.

### Projector (`runtime_projector.py`)

Changes are additive to existing `_TASK_EVENT_COMPAT_TYPES` dict and `_event_summary` function. No structural changes. Follows existing pattern.

### Tests (`test_workbench_runtime_state.py`)

8 test classes, 26 test methods. Classes organized by concern (View, ExecutionMode, ActiveAgent, Cursor, Orphaned, Recovery, Determinism, Constants). Clear docstrings on non-obvious tests. Helper `_event()` factory at module top with sensible defaults — no fixture bloat, no conftest dependency, self-contained.

---

## 2. Naming Conventions

| Symbol | Assessment |
|--------|-----------|
| `WorkbenchRuntimeState` | Consistent with `RuntimeStateSnapshot`, `RuntimeAgentState` |
| `replay_workbench_events` | Follows `replay_events`, `replay_surface_events` |
| `_apply_workbench_event` | Private, follows `_apply_surface_event`, `_apply_event` |
| `_VIEW_MODE_MAP` | Module-private, SCREAMING_CASE for constant |
| `_COCKPIT_SURFACES` | Clear — set of surface names belonging to cockpit |
| `_ONSITE_SURFACES` | Clear — set of surface names belonging to onsite |
| `_VALID_EXECUTION_MODES` | Clear — frozenset validation guard |
| Field: `onsite_orphan_reason` | Descriptive, carries "why orphaned" context |
| Field: `onsite_external_session_id` | Internal field, correctly prefixed (not exposed to users) |

No naming conflicts with existing symbols. No shadowing of builtins.

---

## 3. Type Safety

```python
# WorkbenchRuntimeState — all fields have explicit type annotations and defaults
view: str = "cockpit"
execution_mode: str = "orchestrated"
active_agent: str = ""
cockpit_cursor: int = 0
onsite_cursor: int = 0
onsite_session_status: str = "detached"
onsite_external_session_id: str = ""
onsite_orphan_reason: str = ""

# Function signatures match existing patterns
def replay_workbench_events(events: list[Any]) -> WorkbenchRuntimeState:  # Any = RuntimeEvent duck-type
def _apply_workbench_event(state: WorkbenchRuntimeState, event: Any) -> None:
```

The `list[Any]` for events matches `replay_events(events: list[Any])` — consistent with existing codebase conventions. RuntimeEvent is a frozen dataclass so duck-typing `.event_type`, `.payload` is safe.

Module-level lookups all use immutable types:
- `dict[str, str]` (effectively constant)
- `frozenset` (truly immutable) ✅

---

## 4. State Mutation & Purity

`replay_workbench_events` creates a fresh `WorkbenchRuntimeState()` and passes it through `_apply_workbench_event` which mutates in place. This is the same pattern used by `replay_events` → `_apply_event` and `replay_surface_events` → `_apply_surface_event`. Consistent.

Determinism: no external state, no random, no timestamps used in state derivation. Same events always produce same state. `test_replay_is_deterministic` verifies this.

---

## 5. Guard Clause Quality

Each event handler branch has appropriate guards:

| Handler | Guard | Rationale |
|---------|-------|-----------|
| CONVERSATION_MODE_SWITCHED | `if to_mode:` | Empty to_mode is no-op |
| WORKBENCH_EXECUTION_MODE_SELECTED | `if mode in _VALID_EXECUTION_MODES:` | Reject unknown values |
| SURFACE_CURSOR_ADVANCED | `if surface in _COCKPIT_SURFACES and position > ...` | Namespace check + monotonic guard |
| TERMINAL_SESSION_ATTACHED | `if agent:` | No agent = meaningless attach |
| TERMINAL_SESSION_DETACHED | `if agent and agent == state.active_agent:` | Only detach matching agent |
| TERMINAL_SESSION_DETACHED | `if state.onsite_session_status != "orphaned":` | Preserve orphaned over detach |
| TERMINAL_SESSION_ABORTED | `if agent and agent == state.active_agent:` | Only abort matching agent |
| ONSITE_SESSION_ORPHANED | `if agent:` | Agent required for orphan |
| AGENT_RUN_ORPHANED | `if agent and agent == state.active_agent:` | Match → orphan |
| AGENT_RUN_ORPHANED | `elif agent and not state.active_agent:` | Empty → infer |
| SYSTEM_RECOVERY_STARTED | `if state.onsite_session_status == "attached":` | Only transition from attached |

No handler mutates state without a guard. No handler has a path that both sets and clears the same field.

**Quality note**: The `elif agent and not state.active_agent:` guard on AGENT_RUN_ORPHANED is the correct fix — it prevents an unrelated agent's orphan event from overwriting the active agent. Review of prior version found `elif agent:` (no active_agent check) which was a semantic bug. Now fixed.

---

## 6. Test Quality

### Coverage by concern

| Concern | Tests | Edge cases covered |
|---------|-------|--------------------|
| View replay | 4 | cockpit, onsite, last-switch-wins, legacy product/terminal |
| Execution mode | 4 | 3 modes + default=orchestrated |
| Active agent | 3 | mode_switch source, attach source, detach+reattach chain |
| Cursor | 5 | cockpit via product, cockpit via cockpit, onsite via terminal, onsite via onsite, backward-rejected |
| Orphaned | 5 | direct event, agent propagation (match), non-matching guard, empty-inference, reattach-clears |
| Full recovery | 2 | 8-event simulation, empty-replay defaults |
| Determinism | 1 | 3-event sequence, all fields compared |
| Constants | 2 | existence + exact value |

### Test isolation

Each test creates its own event list via `_event()` factory. No shared mutable state. No database dependency. Tests are pure unit tests of a pure function — can run in any order, in parallel.

### Assertion quality

Assertions are specific and single-concern:
```python
assert state.view == "cockpit"           # not: assert state == expected_state
assert state.active_agent == "claude"    # targeted field checks
assert state.onsite_cursor == 10         # cursor position after backward-rejected event
```

No `assert True`, no `assert len(...)`, no vague equality checks. Each assertion maps to a specific spec requirement.

### Missing coverage (non-blocking)

- **Unknown surface name in cursor event**: `SURFACE_CURSOR_ADVANCED` with `surface="unknown"` — falls through silently. Harmless but untested.
- **Invalid execution_mode value**: `WORKBENCH_EXECUTION_MODE_SELECTED` with `execution_mode="invalid"` — rejected by frozenset guard. Harmless but untested.
- **Simultaneous CONVERSATION_MODE_SWITCHED with both to_mode and active_agent**: covered by existing tests that have both fields. ✅

---

## 7. Docstring Quality

| Symbol | Docstring | Accuracy |
|--------|----------|----------|
| `WorkbenchRuntimeState` | "Workbench state reconstructed purely from runtime events. This is NOT the source of truth — the runtime event log is." | ✅ Explicitly states non-authority |
| `replay_workbench_events` | "Pure function: no side effects, no database access. Deterministic." | ✅ Accurately describes guarantees |
| `_apply_workbench_event` | Inline comments per branch (`# --- View change via ... ---`) | ✅ Clear, follows existing style |
| `_event_summary` additions | f-strings with payload field references | ✅ Matches existing format |

No docstring lies. No stale comments referencing removed code.

---

## 8. Performance

`replay_workbench_events` is O(n) where n = event count. Each event handler is O(1) — dict lookups, frozenset membership tests, string comparisons. No allocations per event (state mutated in place). Suitable for replay of thousands of events on daemon restart.

`WorkbenchRuntimeState` is a flat dataclass (8 fields, all primitives). Instantiation cost is negligible.

---

## 9. Security

No user input reaches `replay_workbench_events` — it consumes only already-persisted `RuntimeEvent` objects. Payload values are extracted via `.get()` with defaults, no `eval`, no `exec`, no deserialization of untrusted data. The `onsite_external_session_id` field is internal-only; it is never rendered in user-facing output by Task 6 code.

---

## 10. Adherence to Project Conventions

| Convention | Followed? | Evidence |
|-----------|-----------|----------|
| `from __future__ import annotations` | ✅ | test file |
| `@dataclass` for state models | ✅ | `WorkbenchRuntimeState` |
| `frozenset` for validation sets | ✅ | `_VALID_EXECUTION_MODES`, `_COCKPIT_SURFACES`, `_ONSITE_SURFACES` |
| `dict[str, str]` for mapping tables | ✅ | `_VIEW_MODE_MAP` |
| Private `_` prefix for internal helpers | ✅ | `_apply_workbench_event` |
| Section headers `# ---` | ✅ | Consistent with existing sections |
| `pass  # informational` in dispatch | ✅ | Follows existing pass-through pattern |
| No comments on "what" (code is self-documenting) | ✅ | Comments explain "why" not "what" |
| No multi-paragraph docstrings | ✅ | All docstrings ≤ 5 lines |

---

## Summary

| Category | Grade | Notes |
|----------|-------|-------|
| Structure | PASS | |
| Naming | PASS | |
| Types | PASS | |
| Guards | PASS | All 8 handlers have appropriate guards |
| Tests | PASS | 26 tests, 8 concerns, good edge coverage |
| Docs | PASS | Source-of-truth disclaimer explicit |
| Performance | PASS | O(n), flat dataclass |
| Security | PASS | No user input, no eval |
| Conventions | PASS | Matches existing patterns |

**Defects**:
- 1 minor: Duplicate EventType constant assignments (lines 170/207, 173/214). Merge artifact, zero behavioral impact.

**Verdict: PASS**
