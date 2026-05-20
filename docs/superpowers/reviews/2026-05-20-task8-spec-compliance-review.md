# Spec Compliance Review — Task 8: End-To-End Workbench Integration

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step 1: All 6 scenarios covered

| Plan Scenario | Tests | Status |
|---------------|-------|--------|
| `ordinary text -> orchestrated run -> onsite -> cockpit -> verify summary` | `TestClosedLoop1_OrchestratedWorkflow` (3) + `test_terminal_then_product_through_handlers_preserves_conversation` | PASS |
| `/codex prompt -> Codex-only -> onsite raw Codex view` | `TestClosedLoop3_CodexOnly` (5: routing + controller + Onsite view + adapter) | PASS |
| `/claude prompt -> Claude-only -> onsite raw Claude view -> Codex verify action` | `TestClosedLoop4_ClaudeOnlyWithVerify` (6: routing + controller + Onsite view + verify affordance) | PASS |
| `restart replay restores view and execution mode` | `TestClosedLoop6_RestartRecoveryReplay` (6: all fields, determinism, orphaned Cockpit-usable) | PASS |
| `approval resolved from Cockpit is reflected in Onsite` | `TestClosedLoop5_SharedApprovals` (6: cross-projection visibility) | PASS |
| `approval resolved from Onsite is reflected in Cockpit` | Same class (cross-projection + WorkbenchState both-view check) | PASS |

## Plan Step 2-4

| Step | Result |
|------|--------|
| Step 2: `pytest tests/test_workbench_remote_integration.py -q` | 49 passed |
| Step 4: cross-track regression | 119 passed |

## Spec Acceptance Criteria: 15/15

| AC | Description | Status |
|----|-------------|--------|
| 1 | Ordinary text → Codex → Claude → Codex | PASS — `test_chief_engineer_creates_orchestration_run_and_codex_analysis` verifies full chain (orchestration_run + codex_analysis_run + orchestrator_start) |
| 2 | `/codex` runs Codex-only, never calls Claude | PASS — orchestrator starts=0, Claude calls=0, CODEX_DIRECT mode |
| 3 | `/claude` runs Claude-only, no auto-Codex | PASS — orchestrator starts=0, zero Codex agent_runs, Claude actually invoked |
| 4 | Claude-only offers "让 Codex 验收" | PASS — VERIFY action in response buttons |
| 5 | `/terminal` never dead session | PASS — 6 tests exhaust no-session paths, START_CARD always offered |
| 6 | Onsite auto-attaches to active agent | PASS — fallback + auto-open through WlCodexHandlers |
| 7 | Onsite start actions when no session | PASS — START_CARD always actionable |
| 8 | Onsite text → only live session | PASS — controller.assert_not_called(), adapter receives input |
| 9 | No raw terminal replay on Cockpit return | PASS — forbidden words absent from view-switch notice |
| 10 | Independent cursors | PASS — cockpit_cursor ≠ onsite_cursor verified |
| 11 | Restart reconstructs view + mode + session | PASS — 6 replay tests, all execution mode × view combos |
| 12 | Menu: daily phone actions | Task 2 ownership |
| 13 | Help: user language, no config keys | PASS — 9 forbidden words checked across 5 rendered outputs |
| 14 | Raw terminal redacted | Task 4 ownership |
| 15 | View switching ≠ restart work | PASS — session survives /terminal → Onsite text → /product |

## Semantic Drift: NONE

All assertions traced to production code lines:
- `render_view_header` → rendering.py:12
- `create_orchestration_run(goal=...)` → controller.py:1550-1552
- `create_agent_run(agent="codex", role="analysis")` → controller.py:1556-1558
- `send(request)` in background task → controller.py:1429
- `"让 Codex 验收"` button → controller.py:1377-1378
- `route_plain_text(ONSITE) == ONSITE_INPUT` → routing.py:17-18
- `open_for_conversation` → START_CARD when no session → manager.py:154-159
- `leave_view` → session still "attached" → manager.py:218-232

## Unauthorized Files: NONE

Task 8 write ownership: `tests/test_workbench_remote_integration.py` only.
No other files modified.

## Pre-existing Failures (not Task 8 responsibility)

| Failure | Owner |
|---------|-------|
| `test_claude_cmd_routes_to_runtime_runner_without_direct_streaming` | Task 7 (`test_telegram_handlers.py`) |
| `test_claude_cmd_starts_runtime_runner_without_renderer` | Task 7 (`test_telegram_handlers.py`) |
