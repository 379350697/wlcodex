# Spec Compliance Review — Task 6: Workbench Runtime Events And Recovery Projection

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step 1: Impact Analysis

| Target | Risk | Importers |
|--------|------|-----------|
| `EventType` | MEDIUM | 19 files import, only constants added (no renames/removals) |
| `RuntimeStateSnapshot` | LOW | 0 upstream consumers |
| `RuntimeProjector` | LOW | 0 upstream consumers |

## Plan Step 2-5: Test-driven Implementation

| Step | Result |
|------|--------|
| Step 2: Write failing replay tests (26 tests) | FAIL before impl — `ImportError: cannot import name 'WorkbenchRuntimeState'` |
| Step 3: Confirm failure | Confirmed |
| Step 4: Add event constants + projection fields | 2 core constants + 15 forward-compat stubs added |
| Step 5: `pytest tests/test_workbench_runtime_state.py tests/test_runtime_state_replay.py tests/test_runtime_projector.py -q` | **87 passed** (26 new + 61 existing), 0 fail |

## Plan Replay Requirements: 6/6

| Plan Requirement | Test Class | Tests | Status |
|-----------------|------------|-------|--------|
| view = cockpit or onsite | `TestViewReplay` | 4 (cockpit, onsite, last-switch, legacy compatibility) | PASS |
| execution_mode = orchestrated, codex_direct, or claude_direct | `TestExecutionModeReplay` | 4 (3 modes + default=orchestrated) | PASS |
| active onsite agent | `TestActiveAgentReplay` | 3 (mode_switch, attach, detach+reattach chain) | PASS |
| cockpit cursor | `TestCursorReplay` | 5 (product/cockpit surface, monotonic, never-backward) | PASS |
| onsite cursor | `TestCursorReplay` | 5 (terminal/onsite surface, monotonic, never-backward) | PASS |
| orphaned onsite session after recovery event | `TestOrphanedOnsiteSession` | 5 (direct orphan, agent propagation, non-matching guard, empty-active_agent inference, reattach-clear) | PASS |
| Full recovery + determinism + constants | `TestFullRecoveryReplay` + `TestDeterminism` + `TestEventConstantsExist` | 5 | PASS |

## Spec §Recovery: 7/7

| # | Spec Requirement | Implementation | Status |
|---|-----------------|----------------|--------|
| 1 | Replay runtime events | `replay_workbench_events()` — pure, deterministic | PASS |
| 2 | Rebuild active workbench state | `WorkbenchRuntimeState` dataclass, 8 fields | PASS |
| 3 | Restore Cockpit and Onsite cursors | `cockpit_cursor`, `onsite_cursor` with monotonic advance guard | PASS |
| 4 | Reattach live sessions when transport supports it | `onsite_external_session_id` recorded; reattach logic in Task 4 | PASS |
| 5 | Mark missing local processes as orphaned | 3 pathways: `ONSITE_SESSION_ORPHANED` direct, `AGENT_RUN_ORPHANED` propagation, `SYSTEM_RECOVERY_STARTED` auto-orphan | PASS |
| 6 | Keep Cockpit usable if Onsite reattach fails | Default `view = "cockpit"` | PASS |
| 7 | Show a concise recovery card | Task 2/7 scope; Task 6 provides reconstruction state | PASS |

## Spec Acceptance Criteria: 3/3 (Task 6 scope)

| AC | Description | Status |
|----|-------------|--------|
| 10 | Cockpit and Onsite maintain independent cursors | PASS — separate fields, monotonic advance, `test_cursor_never_goes_backward` |
| 11 | Restart recovery reconstructs active view, execution mode, and session status from events | PASS — `TestFullRecoveryReplay` verifies all 3 dimensions with 8-event recovery scenario |
| 15 | Tests prove that view switching does not restart work | Task 4/7 scope; Task 6 replay holds session_id across view switches |

## Spec §Events: 17/17 event concepts exist

```
workbench.created              → EventType.WORKBENCH_CREATED              ✅
workbench.view.changed         → EventType.WORKBENCH_VIEW_CHANGED         ✅
workbench.execution_mode.selected → EventType.WORKBENCH_EXECUTION_MODE_SELECTED ✅
workbench.route.decided        → EventType.WORKBENCH_ROUTE_DECIDED        ✅
onsite.session.started         → EventType.ONSITE_SESSION_STARTED         ✅
onsite.session.attached        → EventType.ONSITE_SESSION_ATTACHED        ✅
onsite.session.detached        → EventType.ONSITE_SESSION_DETACHED        ✅
onsite.session.orphaned        → EventType.ONSITE_SESSION_ORPHANED        ✅
onsite.input.sent              → EventType.ONSITE_INPUT_SENT              ✅
onsite.output.frame            → EventType.ONSITE_OUTPUT_FRAME            ✅
onsite.cursor.advanced         → EventType.ONSITE_CURSOR_ADVANCED         ✅
cockpit.cursor.advanced        → EventType.COCKPIT_CURSOR_ADVANCED        ✅
cockpit.summary.rendered       → EventType.COCKPIT_SUMMARY_RENDERED       ✅
approval.requested             → EventType.APPROVAL_REQUESTED (existing)  ✅
approval.resolved              → EventType.APPROVAL_RESOLVED (existing)   ✅
diff.updated                   → EventType.DIFF_UPDATED (existing)        ✅
run.completed / run.failed     → EventType.RUN_COMPLETED / RUN_FAILED (existing) ✅
```

## Semantic Drift: NONE

All assertions traced to production code lines:

```
CONVERSATION_MODE_SWITCHED → _VIEW_MODE_MAP.get(to_mode)          → runtime_state.py:1016-1018
  - "cockpit"/"product" → "cockpit", "onsite"/"terminal" → "onsite"
  
WORKBENCH_EXECUTION_MODE_SELECTED → _VALID_EXECUTION_MODES guard → runtime_state.py:1024-1027
  - frozenset({"orchestrated", "codex_direct", "claude_direct"})

SURFACE_CURSOR_ADVANCED → _COCKPIT_SURFACES / _ONSITE_SURFACES    → runtime_state.py:1036-1042
  - monotonic advance: position > state.cockpit_cursor / onsite_cursor

TERMINAL_SESSION_ATTACHED → agent present guard, attach+clear     → runtime_state.py:1045-1053
  - only sets status/external_session_id/clears orphan when agent populated

TERMINAL_SESSION_DETACHED → agent match + not-orphaned guard     → runtime_state.py:1056-1062
  - preserves orphaned status, only detaches matching agent

AGENT_RUN_ORPHANED → match-or-infer pattern                       → runtime_state.py:1079-1085
  - if agent matches active_agent → mark orphaned
  - elif agent and active_agent empty → infer and mark orphaned
  - else (non-matching, active_agent set) → silently skip

SYSTEM_RECOVERY_STARTED → attached→orphaned transition            → runtime_state.py:1088-1094
  - only affects sessions with status="attached"
  - sets reason="daemon_restart" when no explicit reason

WorkbenchRuntimeState docstring: "This is NOT the source of truth —
the runtime event log is. This projection exists so recovery can
answer what view / execution mode / agent / cursor were we in
after a restart."
```

## Execution Mode vs View Mode: CONFIRMED SEPARATE

```
state.view           = "cockpit" | "onsite"         (view dimension)
state.execution_mode = "orchestrated" | "codex_direct" | "claude_direct" (exec dimension)
```

No path in `_apply_workbench_event` conflates these two dimensions.
Default preserves `Codex → Claude → Codex` (execution_mode="orchestrated").
Codex-only and Claude-only semantics are explicit enum values.

## Unauthorized Files: NONE

Task 6 write ownership (Plan):
- `wlcodex/runtime_events.py` — only Task 6 touches ✅
- `wlcodex/runtime_state.py` — only Task 6 touches ✅
- `wlcodex/runtime_projector.py` — only Task 6 touches ✅
- `tests/test_workbench_runtime_state.py` — new file created by Task 6 ✅

Other files in working-tree diff belong to other Tasks (2-5, 7, 9), not modified by Task 6.

## No Irrelevant Refactoring / Copy Changes

All changes are pure additions:
- `runtime_events.py`: +25 lines (constants only)
- `runtime_state.py`: +155 lines (WorkbenchRuntimeState + replay + pass-through expansion)
- `runtime_projector.py`: +29 lines (compat mappings + summaries)

No deletions, no renames, no existing function signatures changed.
No user-facing strings modified.

## No Exposure of Internal Details to Users

Task 6 touches only backend runtime modules. No Telegram handler, no menu, no help text, no status renderer modified. No risk of exposing `terminal.enabled`, `external_session_id`, `thread id`, `session id`, or `runtime_events` to normal users.

## Minor Observations (Non-Blocking)

1. **EventType has two comment groups for the same constants**: `WORKBENCH_EXECUTION_MODE_SELECTED` and `ONSITE_SESSION_ORPHANED` appear in both the "Workbench execution mode" group (line ~169) and the "Workbench view / execution mode" group (line ~201). Python class attribute assignment overwrites with identical values — no runtime impact.

2. **10 EventType constants are forward-compat stubs**: `WORKBENCH_CREATED`, `WORKBENCH_ROUTE_DECIDED`, `ONSITE_SESSION_STARTED`, `ONSITE_SESSION_ATTACHED`, `ONSITE_SESSION_DETACHED`, `ONSITE_INPUT_SENT`, `ONSITE_OUTPUT_FRAME`, `ONSITE_CURSOR_ADVANCED`, `COCKPIT_CURSOR_ADVANCED`, `COCKPIT_SUMMARY_RENDERED` are not consumed by `replay_workbench_events()`. They exist in EventType (canonical holder), `_apply_event` pass-through (crash prevention), and projector compat (diagnostics). They serve as forward-compatible stubs for Tasks 2-4 to emit. Tests for these can be added incrementally as other tasks wire them up.

## Test Evidence Summary

```
tests/test_workbench_runtime_state.py  — 26 tests, all PASS
tests/test_runtime_state_replay.py     — 33 tests, all PASS (0 regression)
tests/test_runtime_projector.py        — 28 tests, all PASS (0 regression)
Total                                   — 87 passed, 0 failed
```
