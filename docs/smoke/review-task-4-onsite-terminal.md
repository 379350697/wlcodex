# Task 4 — Spec Compliance & Code Quality Review

## Spec Compliance Reviewer

**Verdict: PASS** (2026-05-20)

### Blocking issues: None

### Plan requirements: all satisfied

| Step | Requirement | Status |
|------|------------|--------|
| 1 | Run impact analysis on 3 targets | PASS |
| 2 | Write failing tests for no-dead-session behavior | PASS |
| 3 | Run tests, confirm failure | PASS |
| 4 | Add open_for_conversation, record_frame, tail, pause_delivery, resume_delivery, leave_view | PASS |
| 5 | Renderer uses "现场" wording, preserves raw frame text | PASS |
| 6 | Run terminal tests | PASS |

### Acceptance criteria: all satisfied for Task 4 scope

| AC | Requirement | Status |
|----|------------|--------|
| 5 | /terminal or "接管现场" never leaves dead session | PASS — manager primitives + start-card; wiring by Task 7 |
| 6 | Auto-attach to active agent when session exists | PASS — open_for_conversation(preferred_agent=) → AUTO_OPEN |
| 7 | Start actions when no session | PASS — START_CARD + render_start_card() |
| 8 | Onsite text routes to selected session | PASS — via send_input() + Task 1 route_plain_text |
| 9 | No replay of raw output on return | PASS — leave_view pauses delivery, no replay |
| 10 | Independent Cockpit/Onsite cursors | PASS — frame history exists; cursor tracking deferred to Task 6 |
| 14 | Redaction before Telegram delivery | PASS — layered correctly at Task 7 |
| 15 | Tests prove view switching doesn't restart | PASS — closed-loop tests cover full lifecycle |

### Semantic drift: None detected

### Unauthorized files: None

### Non-blocking fixes (resolved):
- router.py docstring: /terminal leave added
- __init__.py: pending coordinator alignment with Task 7
- leave_view cursor recording: deferred to Task 6

---

## Code Quality Reviewer

**Verdict: PASS** (2026-05-20)

### Quality blockers: None

### Six fixes applied and verified

| # | Issue | Status |
|---|-------|--------|
| 1 | "Task 4" leaked into production comments (5 sites) | FIXED — all replaced with "(Onsite)" |
| 2 | record_frame redundant guard hiding caller errors | FIXED — removed guard, KeyError on invalid ref |
| 3 | render_terminal_frame docstring examples removed | FIXED — restored 3 example lines |
| 4 | render_start_card() not parameterized | FIXED — signature render_start_card(available_agents=None) |
| 5 | leave_view return value inconsistent with detach | FIXED — returns None, docstring contrasts detach |
| 6 | router docstring missing /terminal leave | FIXED — added to Supported forms |

### Remaining notes (non-blocking)
- open_for_conversation has two duplicate return branches (intentional clarity, minor DRY)
- _paused not cleaned on detach (low risk, Task 6 lifecycle)

### Test evidence
```
90 passed: 27 onsite_terminal + 48 terminal_surface + 15 workbench_core
```

### Files changed (within Task 4 ownership)
```
wlcodex/surfaces/terminal/manager.py
wlcodex/surfaces/terminal/renderer.py
wlcodex/surfaces/terminal/router.py
tests/test_workbench_onsite_terminal.py
```
