# Spec Compliance Review — Repair Task 3: Agent Session Library Projection

> **SUPERSEDED — historical review only.** It is not a current release result;
> use [the current semantic contract](../../product-semantics.md) and current tests.

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step Verification

| Plan Step | Requirement | Evidence | Status |
|-----------|-------------|----------|--------|
| Step 1 | Impact analysis on `list_recent_agent_runs`, `update_agent_run_status` | LOW risk | PASS |
| Step 2 | Write failing projection tests | 17 tests in `test_workbench_session_library.py` | PASS |
| Step 3 | Confirm test failure before implementation | `ModuleNotFoundError: wlcodex.workbench.sessions` | PASS |
| Step 4 | Implement session models + library | `AgentSessionSummary`, `AgentSessionResumability`, `AgentSessionLibrary` | PASS |
| Step 5 | Add user-safe renderer | `render_session_library` in `rendering.py` | PASS |
| Step 6 | Focused tests pass | 17 passed | PASS |

## Spec Acceptance Criteria

| AC# | Requirement | Status |
|-----|-------------|--------|
| 15 | `/sessions` shows Codex and Claude historical sessions | PASS — `list_for_workbench` projects both agents |
| 16 | Session cards hide raw session/thread/task ids | PASS — `user_label` never exposes `internal_ref` |
| 29 | User copy has no banned internal terms | PASS — `render_session_library` uses "历史现场", "Claude 现场", "Codex 现场" |

## Projection Rules Verified

| Rule | Code Evidence | Status |
|------|-------------|--------|
| agent must be codex or claude | `if run.agent not in ("codex", "claude"): continue` | PASS |
| external_session_id present → RESUMABLE | `_classify(status, internal_ref)` | PASS |
| no resume reference → SUMMARY_ONLY | Returns `SUMMARY_ONLY` when `not internal_ref` | PASS |
| title from completion_summary → role → agent fallback | Chain: `completion_summary or prompt_packet_summary or role or agent` | PASS |
| newest first | `list_recent_agent_runs` orders by `id DESC` | PASS |
| deduplicate same agent + same internal_ref | `seen: set[tuple[str, str]]` | PASS |

## User Copy Verification

| Term | Present in render output? |
|------|--------------------------|
| 历史现场 | Yes — header |
| Claude 现场 | Yes — agent="claude" |
| Codex 现场 | Yes — agent="codex" |
| 可继续 | Yes — RESUMABLE label |
| 可回顾 | Yes — SUMMARY_ONLY label |
| external_session_id | No |
| thread id | No |
| session id | No |

## Semantic Drift: NONE

Session Library is a pure projection over existing `agent_runs`. No new tables, no new fields. All internal IDs are on `internal_ref` (never rendered). `user_label` is computed from `resumability` enum, not from raw data.

## Unauthorized Files: NONE

Task 3 ownership: `wlcodex/workbench/sessions.py` (new), `wlcodex/workbench/rendering.py` (modified), `tests/test_workbench_session_library.py` (new).
