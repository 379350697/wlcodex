# Spec Compliance Review — Repair Task 6: Execution-Mode Session Persistence

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step Verification

| Plan Step | Requirement | Evidence | Status |
|-----------|-------------|----------|--------|
| Step 1 | Impact analysis on `handle_claude_direct`, `handle_codex_direct`, `_project_session_id` | LOW risk | PASS |
| Step 2 | Failing Claude-only persistence test | `test_workbench_execution_modes.py` verifies session ID persistence | PASS |
| Step 3 | Failing Codex session persistence test | Codex thread reference persisted via `update_agent_run_status` | PASS |
| Step 4 | Persist Claude session id | `update_agent_run_status(..., external_session_id=session_id)` called on completion | PASS |
| Step 5 | Persist Codex thread reference | Thread ID stored as internal session reference | PASS |
| Step 6 | Strict direct-mode semantics | `/codex` → no Claude; `/claude` → no Codex analysis; completion → no auto-verify | PASS |
| Step 7 | Focused tests pass | 52 passed (execution_modes + related) | PASS |

## Key Requirements Verified

### Claude-Only Session Persistence

| Check | Code Evidence |
|-------|--------------|
| session_id captured from stream/result | Controller tracks `latest_session_id` from stream events |
| `update_agent_run_status` called with `external_session_id` | `_run_claude_direct_async` updates run on completion |
| Raw session_id not in Telegram text | `external_session_id` stored in DB, not rendered |

### Codex-Only Thread Persistence

| Check | Code Evidence |
|-------|--------------|
| Thread ID stored as internal reference | `external_session_id` field reused for Codex thread ref |
| No Claude invocation | `_handle_codex_direct_impl` calls Codex backend directly |

### Direct-Mode Semantics

| Assertion | Status |
|-----------|--------|
| `/codex` does not call Claude | PASS — verified in `test_workbench_execution_modes.py` |
| `/claude` does not call Codex analysis | PASS — verified in `test_workbench_execution_modes.py` |
| `/claude` completion does not auto-start Codex verification | PASS — only explicit "让 Codex 验收" triggers verify |
| "让 Codex 验收" button present after Claude completion | PASS — `VERIFY` action button in response |

## Originating Evidence

Original Cockpit/Onsite Task 5 Spec Compliance Review (PASS) already verified execution mode behavior. This repair review confirms the session persistence layer is correctly wired to the Agent Session Library.

## Semantic Drift: NONE

- Execution mode (orchestrated/codex_direct/claude_direct) ≠ view mode (cockpit/onsite)
- Session persistence is internal — no user-facing ID exposure
- Claude-only completion offers verification, doesn't auto-trigger

## Unauthorized Files: NONE

Task 6 ownership: `wlcodex/controller.py`, `wlcodex/runtime_projector.py`, test files.
