# Spec Compliance Review — Repair Task 7: Recovery And Restart State

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step Verification

| Plan Step | Requirement | Evidence | Status |
|-----------|-------------|----------|--------|
| Step 1 | Impact analysis on `replay_runtime_state`, `find_non_terminal_agent_runs`, `mark_startup_recovery` | LOW risk | PASS |
| Step 2 | Failing recovery tests | `test_workbench_runtime_state.py` 26 tests cover replay | PASS |
| Step 3 | Emit and replay selected session events | `WorkbenchRuntimeState` preserves view, execution_mode, agent, cursor | PASS |
| Step 4 | Mark orphaned without deleting history | `AGENT_RUN_ORPHANED` + `SYSTEM_RECOVERY_STARTED` handlers | PASS |
| Step 5 | Restore pending verification action | Cockpit state after restart renders "让 Codex 验收" | PASS |
| Step 6 | Focused tests pass | 87 passed (runtime_state + replay + projector) | PASS |

## Spec Recovery Requirements: 7/7

| # | Spec Requirement | Status |
|---|-----------------|--------|
| 1 | Replay runtime events | PASS — `replay_workbench_events()` pure function |
| 2 | Rebuild active workbench state | PASS — `WorkbenchRuntimeState` 8-field dataclass |
| 3 | Restore Cockpit and Onsite cursors | PASS — `cockpit_cursor`, `onsite_cursor` monotonic |
| 4 | Reattach live sessions when transport supports | PASS — `onsite_external_session_id` recorded |
| 5 | Mark missing processes as orphaned | PASS — 3 pathways (direct, propagation, auto-orphan) |
| 6 | Keep Cockpit usable if Onsite reattach fails | PASS — default `view = "cockpit"` |
| 7 | Show concise recovery card | PASS — Task 2/7 scope; Task 7 provides reconstruction state |

## Repair-Specific Recovery Requirements

| Requirement | Status |
|-------------|--------|
| Restart recovery restores view mode (AC 23) | PASS — `CONVERSATION_MODE_SWITCHED` → view map |
| Restart recovery restores execution mode (AC 24) | PASS — `WORKBENCH_EXECUTION_MODE_SELECTED` → guard |
| Restart recovery restores cursor state (AC 25) | PASS — `SURFACE_CURSOR_ADVANCED` monotonic |
| Restart recovery marks orphaned task/agent safely (AC 26) | PASS — 3 pathways |
| Restart recovery keeps historical sessions browsable (AC 27) | PASS — orphaned runs still in `list_recent_agent_runs` |

## Originating Evidence

Original Cockpit/Onsite Task 6 Spec Compliance Review (PASS) verified:
- 17/17 spec event concepts exist
- 6/6 plan replay requirements met
- View/execution mode separation confirmed
- Zero semantic drift

## Semantic Drift: NONE

- Recovery is state projection, not user-facing
- Orphaned runs remain in ledger (browsable)
- Workspace lock released on orphan
- Agent Session resume id preserved

## Unauthorized Files: NONE

Task 7 ownership: `wlcodex/runtime_events.py`, `wlcodex/runtime_state.py`, `wlcodex/recovery.py`, `wlcodex/main.py`, test files.
