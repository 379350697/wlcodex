# Spec Compliance Review — Task 1: Workbench Foundation Contracts

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step Verification

| Plan Step | Requirement | Evidence | Status |
|-----------|-------------|----------|--------|
| Step 1 | Impact analysis on `SurfaceMode`, `ModeSwitchCommand` | Both LOW risk | PASS |
| Step 2 | Write 3 failing tests for view/execution separation | 15 tests created (exceeds minimum) | PASS |
| Step 3 | Confirm test failure | `ModuleNotFoundError: wlcodex.workbench.models` | PASS |
| Step 4 | Add minimal models | `ViewMode`, `ExecutionMode`, `WorkbenchRoute`, `WorkbenchState` | PASS |
| Step 5 | Add `route_plain_text` | 4 routing rules implemented | PASS |
| Step 6 | Tests pass | 15 passed | PASS |

---

## View / Execution Mode Separation

Spec line 42-43: "Those are execution modes, not view modes. The user can view any execution mode from Cockpit or Onsite."
Spec line 145-146: "Execution mode controls who does the work. View controls how the user sees and steers the work."

| Check | Result |
|-------|--------|
| `ViewMode` enum: COCKPIT / ONSITE | PASS — view dimension only |
| `ExecutionMode` enum: ORCHESTRATED / CODEX_DIRECT / CLAUDE_DIRECT | PASS — execution dimension only |
| Both fields independent on `WorkbenchState` | PASS — `view: ViewMode`, `execution_mode: ExecutionMode` |
| `route_plain_text` considers both dimensions independently | PASS |
| No code confuses Cockpit/Onsite as execution modes | PASS |

---

## Default Workflow Preserved

Spec line 28-32: "The default business workflow remains: Codex analysis -> Claude implementation -> Codex verification"

| Check | Result |
|-------|--------|
| `ExecutionMode.ORCHESTRATED` exists | PASS |
| `WorkbenchState.execution_mode` defaults to `ORCHESTRATED` | PASS |
| `WorkbenchState.view` defaults to `COCKPIT` | PASS |
| Cockpit + ORCHESTRATED → `ORCHESTRATED_COCKPIT` route | PASS |

---

## Codex-only / Claude-only Semantics

Spec line 167-218 defines explicit semantics for direct modes.

| Mode | Enum Value | Route | Status |
|------|-----------|-------|--------|
| Codex-only | `ExecutionMode.CODEX_DIRECT` | `CODEX_DIRECT_COCKPIT` | PASS |
| Claude-only | `ExecutionMode.CLAUDE_DIRECT` | `CLAUDE_DIRECT_COCKPIT` | PASS |

---

## WorkbenchState Field Completeness

Spec §Workbench State (line 440-456) lists 17 fields. All 17 present on `WorkbenchState` dataclass.

| # | Spec Field | models.py Attribute | Status |
|---|-----------|-------------------|--------|
| 1 | workbench id | `conversation_id: int` | PASS |
| 2 | chat id | `chat_id: int` | PASS |
| 3 | workspace alias | `workspace_alias: str` | PASS |
| 4 | active view | `view: ViewMode` | PASS |
| 5 | active execution mode | `execution_mode: ExecutionMode` | PASS |
| 6 | active phase | `active_phase: str` | PASS |
| 7 | active agent | `active_agent: str` | PASS |
| 8 | Codex thread id | `codex_thread_id: str` | PASS |
| 9 | Codex turn id | `codex_turn_id: str` | PASS |
| 10 | Claude session id | `claude_session_id: str` | PASS |
| 11 | onsite session refs by agent | `onsite_session_refs: dict` | PASS |
| 12 | cockpit cursor | `cockpit_cursor: int` | PASS |
| 13 | onsite cursor | `onsite_cursor: int` | PASS |
| 14 | latest diff summary | `latest_diff_summary: str` | PASS |
| 15 | pending approvals | `pending_approvals: list` | PASS |
| 16 | pending user context | `pending_user_context: str` | PASS |
| 17 | latest user-visible message ids | `latest_user_visible_message_ids: dict` | PASS |

Total: 17/17 covered.

---

## Events Completeness

Spec §Events (line 466-486) lists 18 event concepts. All 18 in `events.py`.

| # | Spec Event | Constant | Status |
|---|-----------|----------|--------|
| 1 | `workbench.created` | `WORKBENCH_CREATED` | PASS |
| 2 | `workbench.view.changed` | `WORKBENCH_VIEW_CHANGED` | PASS |
| 3 | `workbench.execution_mode.selected` | `WORKBENCH_EXECUTION_MODE_SELECTED` | PASS |
| 4 | `workbench.route.decided` | `WORKBENCH_ROUTE_DECIDED` | PASS |
| 5 | `onsite.session.started` | `ONSITE_SESSION_STARTED` | PASS |
| 6 | `onsite.session.attached` | `ONSITE_SESSION_ATTACHED` | PASS |
| 7 | `onsite.session.detached` | `ONSITE_SESSION_DETACHED` | PASS |
| 8 | `onsite.session.orphaned` | `ONSITE_SESSION_ORPHANED` | PASS |
| 9 | `onsite.input.sent` | `ONSITE_INPUT_SENT` | PASS |
| 10 | `onsite.output.frame` | `ONSITE_OUTPUT_FRAME` | PASS |
| 11 | `onsite.cursor.advanced` | `ONSITE_CURSOR_ADVANCED` | PASS |
| 12 | `cockpit.cursor.advanced` | `COCKPIT_CURSOR_ADVANCED` | PASS |
| 13 | `cockpit.summary.rendered` | `COCKPIT_SUMMARY_RENDERED` | PASS |
| 14 | `approval.requested` | `APPROVAL_REQUESTED` | PASS |
| 15 | `approval.resolved` | `APPROVAL_RESOLVED` | PASS |
| 16 | `diff.updated` | `DIFF_UPDATED` | PASS |
| 17 | `run.completed` | `RUN_COMPLETED` | PASS |
| 18 | `run.failed` | `RUN_FAILED` | PASS |

Total: 18/18 covered.

---

## Routing Rules

Plan Step 5 defines 4 routing cases. All implemented.

| Input | Expected Output | Code | Status |
|-------|----------------|------|--------|
| `view==ONSITE` | `ONSITE_INPUT` | routing.py:17-18 | PASS |
| `view==COCKPIT && execution_mode==ORCHESTRATED` | `ORCHESTRATED_COCKPIT` | routing.py:26 (fallthrough) | PASS |
| `view==COCKPIT && execution_mode==CODEX_DIRECT` | `CODEX_DIRECT_COCKPIT` | routing.py:20-21 | PASS |
| `view==COCKPIT && execution_mode==CLAUDE_DIRECT` | `CLAUDE_DIRECT_COCKPIT` | routing.py:23-24 | PASS |

Critical spec constraint (line 535-536): "Onsite text must never call the Cockpit product controller" — `route_plain_text` returns `ONSITE_INPUT` for Onsite, never a Cockpit route. PASS.

---

## User-Facing Copy

| Check | Result |
|-------|--------|
| "驾驶舱" used for Cockpit display name | PASS |
| "接管现场" used for Onsite entry | PASS |
| Onsite→Cockpit copy matches spec line 353-355 exactly | PASS |
| `terminal.enabled` absent from user copy | PASS |
| `external_session_id` absent from user copy | PASS |
| `thread id` absent from user copy | PASS |
| `session id` absent from user copy | PASS |

---

## Unauthorized Files: NONE

Task 1 write ownership: `wlcodex/workbench/*`, `tests/test_workbench_core.py`.

No existing files modified. `git diff HEAD` changes are from prior commits on the branch, not from Task 1.

---

## Semantic Drift: NONE

- View and execution modes are orthogonal enums, never confused
- Cockpit is default view, ORCHESTRATED is default execution mode
- All 17 WorkbenchState fields match spec §Workbench State
- All 18 event constants match spec §Events
- Routing respects view/execution separation unconditionally
