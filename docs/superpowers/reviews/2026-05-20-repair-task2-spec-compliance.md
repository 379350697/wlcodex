# Spec Compliance Review — Repair Task 2: Execution Lane And Task Internalization

**Reviewer**: Spec Compliance Reviewer (independent)
**Date**: 2026-05-20
**Verdict**: **PASS**

---

## Plan Step Verification

| Plan Step | Requirement | Evidence | Status |
|-----------|-------------|----------|--------|
| Step 1 | Impact analysis on `handle_conversation_text`, `handle_codex_direct`, `handle_claude_direct`, `reserve_task` | LOW risk | PASS |
| Step 2 | Failing lane tests | `test_workbench_execution_modes.py` covers execution mode routing | PASS |
| Step 3 | Failing lifecycle test | Task lifecycle tests verify terminal states | PASS |
| Step 4 | `_execution_lane_decision` helper | Added at controller.py:1059 with 4 routing rules | PASS |
| Step 5 | Hide task internals from busy copy | `_handle_workspace_busy` now uses product copy without task IDs | PASS |
| Step 6 | Close lifecycle paths | All Codex/Claude direct paths move to terminal status | PASS |
| Step 7 | Focused tests pass | 62 passed (controller_flow + execution_modes) | PASS |

## Critical Fix: Workspace-Busy Copy

**BEFORE** (leaked task IDs):
```
工作区正忙
当前任务：#42
状态：running
请选择：
```

**AFTER** (product copy):
```
当前工作台正在执行。你可以追加到当前执行，等它结束，停止当前后执行，或新开工作台。
```

## Execution Lane Decision Rules

| Input | Active Run | Result |
|-------|-----------|--------|
| onsite_text | any | `onsite_input` |
| codex_direct / claude_direct | present | `explicit_choice` |
| codex_direct / claude_direct | absent | `idle` |
| ordinary text | present | `append` |
| ordinary text | absent | `idle` |

## Semantic Checks

| Check | Result |
|-------|--------|
| Task IDs hidden from normal busy copy | PASS — `_handle_workspace_busy` now product language |
| Task IDs remain visible in diagnostic commands (/task, /pause, /abort) | PASS — legacy diagnostic paths unchanged |
| Execution lane prevents competing hidden tasks | PASS — `_execution_lane_decision` returns `explicit_choice` for direct modes when busy |
| Lifecycle closes on all paths | PASS — terminal status on success, error, cancellation |

## Unauthorized Files: NONE

Task 2 ownership: `wlcodex/controller.py`, `wlcodex/task_service.py`, test files.
