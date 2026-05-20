# Final Gate — WLCodex Workbench Session Library And Task Internalization Repair

**Date**: 2026-05-20
**Reviewer**: Closed-Loop Verification Agent (fifth pass — continuation lifecycle closure)

---

## Verdict: BLOCKED

**Blocking reason**: live Telegram smoke has not been executed. This is required release evidence.

This pass fixes the previous continuation lifecycle gap:

- `resume_session` / `resume_from_summary` store pending continuation state.
- The first Onsite text creates the execution ticket.
- Resumable sessions create a hidden task + agent_run linked by `hidden_task_id`.
- Success and failure both terminalize the hidden task and agent_run.
- Summary-only continuation no longer attempts raw `attach_historical`.
- No new Workbench is created.

---

## Closed-Loop Checklist

| Item | Status | Evidence |
| --- | --- | --- |
| Task 1-9 completed | PASS | implementation and repair review docs present |
| Task 1-9 Spec Compliance Reviewer PASS | PASS | `docs/superpowers/reviews/2026-05-20-task*-spec-compliance-review.md` and repair Task 1-8 spec reviews |
| Task 1-9 Code Quality Reviewer PASS | PASS | `docs/superpowers/reviews/2026-05-20-task*-code-quality-review.md` and repair Task 1-8 code reviews |
| Remote Workbench semantic model | PASS | tests in targeted Workbench suite |
| Default text remains Codex -> Claude -> Codex | PASS | execution mode tests |
| `/codex` Codex-only | PASS | execution mode tests |
| `/claude` Claude-only | PASS | execution mode tests |
| Claude-only offers "让 Codex 验收" | PASS | Telegram routing/rendering tests |
| `/terminal` / 接管现场 no dead session | PASS | routing tests |
| Onsite text not routed to Cockpit controller | PASS | routing tests |
| `/product` no raw terminal replay | PASS | integration tests |
| Restart recovery restores state | PASS | runtime/recovery tests |
| Mobile menu is user entrance | PASS | cockpit menu tests |
| Normal user copy hides internal terms | PASS | user-copy scan tests |
| GitNexus detect_changes | PASS | `detect_changes(scope="all")` low risk |
| Related suites complete | PASS | targeted 188 passed, related 303 passed |
| Acceptance criteria table | PASS | 35-item table below |
| Live Telegram smoke | FAIL | not executed |

---

## Test Evidence

### Focused Continuation Regression

```
pytest tests/test_workbench_telegram_routing.py -q -k "pending_continuation or resume_from_summary"
```

**Result**: 3 passed, 15 deselected.

### Targeted Workbench Suite

```
pytest tests/test_workbench_core.py tests/test_workbench_cockpit_menu.py \
  tests/test_workbench_commands.py tests/test_workbench_onsite_terminal.py \
  tests/test_workbench_execution_modes.py tests/test_workbench_runtime_state.py \
  tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py \
  tests/test_workbench_session_library.py -q
```

**Result**: 188 passed, 0 failed.

### Existing Related Suite

```
pytest tests/test_controller_flow.py tests/test_telegram_handlers.py \
  tests/test_terminal_surface.py tests/test_dual_surface_integration.py \
  tests/test_runtime_projector.py tests/test_runtime_state_replay.py \
  tests/test_recovery.py tests/test_router.py tests/test_status.py \
  tests/test_task_service.py -q
```

**Result**: 303 passed, 0 failed.

### User Copy Internal-Term Scan

```
pytest tests/test_workbench_cockpit_menu.py \
  tests/test_workbench_remote_integration.py::TestRenderingLanguageCompliance \
  tests/test_status.py::test_status_command_must_not_use_format_status_display \
  tests/test_controller_flow.py::test_status_uses_runtime_events_when_available -q
```

**Result**: 15 passed, 0 failed.

### Hygiene

```
git diff --check
git status --short
```

`git diff --check`: clean.

`git status --short`: expected modified files only after this repair pass:

```text
M docs/superpowers/reviews/2026-05-20-workbench-session-library-final-gate.md
M tests/test_workbench_telegram_routing.py
M wlcodex/telegram_app.py
```

### GitNexus

```
detect_changes(repo="wlcodex", scope="all")
```

**Result**: 14 changed symbols, 2 files, risk low, 0 affected processes.

---

## Acceptance Criteria Comparison

| # | Criterion | Status | Evidence |
| --- | --- | --- | --- |
| 1 | `/new` creates the only new user Workbench boundary | PASS | targeted Workbench tests |
| 2 | Ordinary messages after `/new` stay in same Workbench | FAIL | integration tests pass; live Telegram check missing |
| 3 | Default Cockpit text runs Codex -> Claude -> Codex | PASS | execution-mode tests |
| 4 | `/codex` is Codex-only and does not call Claude | PASS | execution-mode tests |
| 5 | `/claude` is Claude-only and does not auto-trigger Codex | PASS | execution-mode tests |
| 6 | Claude-only completion offers "让 Codex 验收" | PASS | routing/rendering tests |
| 7 | `/terminal` never dead-missing conversation for active Workbench | PASS | Telegram regression tests |
| 8 | Start-card callbacks use conversation id, not chat id | PASS | callback encoding tests |
| 9 | Onsite text routes to selected Agent Session | PASS | routing tests |
| 10 | `/product` returns to Cockpit without raw replay | PASS | integration tests |
| 11 | Running Workbench text appends/steers or shows choices | PASS | execution lane tests |
| 12 | Hidden tasks cannot silently pile up | PASS | continuation lifecycle regression tests |
| 13 | Claude-only task reaches terminal state and releases lock | PASS | execution-mode regression tests |
| 14 | Task ids and queue blockers hidden from normal copy | PASS | user-copy scan tests |
| 15 | `/sessions` shows Codex and Claude history | PASS | session library tests |
| 16 | Session cards hide raw session/thread/task ids | PASS | rendered copy tests |
| 17 | Historical Claude session resumes through stored reference | PASS | terminal adapter/session tests |
| 18 | Historical Codex session resumes through stored reference | PASS | terminal adapter/session tests |
| 19 | Continuing history creates new internal task/run linked to session | PASS | new continuation tests assert `hidden_task_id` and `external_session_id` |
| 20 | Continuing history does not create new Workbench | PASS | continuation tests use same `conversation_id` |
| 21 | `/terminal` with history shows picker | PASS | Telegram routing tests |
| 22 | `/terminal` with no active/history shows start card | PASS | Telegram routing tests |
| 23 | Restart recovery restores view mode | PASS | runtime/recovery tests |
| 24 | Restart recovery restores execution mode | PASS | runtime/recovery tests |
| 25 | Restart recovery restores cursor state | PASS | runtime/recovery tests |
| 26 | Restart recovery marks orphaned task/agent safely | PASS | recovery tests |
| 27 | Restart recovery keeps history browsable | PASS | recovery/session tests |
| 28 | Menu reads like mobile product actions | PASS | cockpit menu tests |
| 29 | User copy has no banned internal terms | PASS | user-copy scan tests |
| 30 | GitNexus detect_changes scoped risk | PASS | low risk, 0 affected processes |
| 31 | Targeted Workbench suite complete | PASS | 188 passed |
| 32 | Existing related suite complete | PASS | 303 passed |
| 33 | Every task has Spec Compliance Reviewer PASS | PASS | task/repaired review docs present |
| 34 | Every task has Code Quality Reviewer PASS | PASS | task/repaired review docs present |
| 35 | Final Gate uses closed-loop evidence | PASS | verdict remains BLOCKED because live evidence is missing |

---

## Semantic Drift Review

**Current state**: no known semantic drift in committed behavior after this pass, except release evidence remains incomplete.

Verified semantics:

- Workbench remains the continuing user context until `/new`.
- Cockpit and Onsite are views, not separate sessions.
- Execution mode and view mode remain separate.
- `/codex` remains Codex-only.
- `/claude` remains Claude-only and offers Codex verification as an action.
- `接管现场` attaches without creating a task/run.
- `继续修改` creates hidden task + agent_run only on first Onsite text.
- Continuation success and failure terminalize the internal task/run.
- `从摘要新开` does not raw-attach a summary-only session.
- `/status` uses clean product copy; diagnostics remain under explicit diagnostic routes.

---

## Remaining Blockers

### B1: Live Telegram smoke not executed — BLOCKING

Must run in real Telegram + real Codex/Claude backend:

1. `/new` 真人历史现场 smoke
2. ordinary text through default flow
3. `/terminal`
4. `/product`
5. `/claude Reply exactly with: claude only ok`
6. tap `让 Codex 验收`
7. `/sessions`
8. review recent Claude session
9. attach recent Claude session
10. send `continue from this historical session`
11. `/product`
12. `/codex Reply exactly with: codex only ok`
13. `/sessions`
14. `/new` second Workbench

No live smoke means no release recommendation.

---

## Release Note Summary

WLCodex keeps one continuous Remote Workbench until `/new`. Users can switch between Cockpit and Onsite, browse historical Codex/Claude sessions, and continue previous sessions without seeing internal task/session ids.

---

**Verdict: BLOCKED — waiting for live Telegram smoke**
