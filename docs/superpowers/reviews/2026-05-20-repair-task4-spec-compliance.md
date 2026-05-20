# Spec Compliance Review — Repair Task 4: Historical Attach And Resume

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step Verification

| Plan Step | Requirement | Evidence | Status |
|-----------|-------------|----------|--------|
| Step 1 | Impact analysis on `TerminalSessionManager`, `ClaudeTerminalAdapter`, `CodexTerminalAdapter` | LOW risk | PASS |
| Step 2 | Failing tests for picker and attach | `test_workbench_onsite_terminal.py` covers Onsite decision | PASS |
| Step 3 | Failing tests for resume adapters | Contract coverage for `--resume` and `thread/resume` | PASS |
| Step 4 | Extend Onsite decision model | `SESSION_PICKER` added to `OnsiteDecisionKind` | PASS |
| Step 5 | Add `attach_historical` | Method added to `TerminalSessionManager` | PASS |
| Step 6 | Focused tests pass | 64 passed (onsite_terminal + terminal_surface) | PASS |

## Onsite Decision Model

**BEFORE** (2 kinds):
```
AUTO_OPEN → live session
START_CARD → start actions
```

**AFTER** (3 kinds):
```
AUTO_OPEN → live session (when attached)
SESSION_PICKER → historical sessions (when history exists, no attached)
START_CARD → start actions (when neither attached nor history)
```

Decision order per Repair Spec:
1. attached session → AUTO_OPEN
2. historical resumable sessions → SESSION_PICKER
3. no sessions → START_CARD

## Historical Attach Rules

| Rule | Implementation |
|------|---------------|
| SUMMARY_ONLY cannot attach | `ValueError` raised with actionable message |
| claude → strategy "stream_json" | `"stream_json" if agent == "claude" else "app_server"` |
| codex → strategy "app_server" | Same ternary |
| internal_ref as external_session_id | `external_session_id=internal_ref` |
| reuses existing attached ref | Scans `_sessions` for same agent+ref before creating new |

## Backend Resume Capacity

| Backend | Resume Mechanism | Code Location |
|---------|-----------------|---------------|
| Claude | `claude --resume <session_id> -p <text>` | `claude_remote.py:11` |
| Codex | `thread/resume` API | `codex_backend.py:708` |

## Spec Acceptance Criteria

| AC# | Requirement | Status |
|-----|-------------|--------|
| 17 | Historical Claude session can be resumed | PASS — `attach_historical` wires `internal_ref` to `external_session_id` |
| 18 | Historical Codex session can be resumed | PASS — same mechanism, `app_server` strategy |
| 21 | `/terminal` with history shows picker | PASS — `SESSION_PICKER` when `historical_sessions` non-empty |
| 22 | `/terminal` with no history shows start card | PASS — `START_CARD` when no attached + no history |

## Semantic Drift: NONE

- "接管现场", "回驾驶舱" copy preserved
- Onsite decision model extended, not replaced
- Backend adapters unchanged — only manager layer extended

## Unauthorized Files: NONE

Task 4 ownership: `wlcodex/surfaces/terminal/manager.py`, test files.
