# Task 5 — Spec Compliance & Code Quality Review

## Spec Compliance Reviewer

**Verdict: PASS** (2026-05-20)

### Blocking issues: None

### Plan requirements: all satisfied

| Step | Requirement | Status |
|------|------------|--------|
| 1 | Run impact analysis on handle_conversation_text, handle_codex_direct, handle_claude_direct, handle_auto_mode | PASS — all LOW risk |
| 2 | Write failing tests for 6 outcomes (orchestrated, /codex, /claude, /auto, verify affordance, no Claude enqueue) | PASS — 11 tests |
| 3 | Run tests, confirm current gaps | PASS — confirmed gaps |
| 4 | Add explicit execution-mode labels to responses | PASS — Codex: "这次只交给 Codex，不会调用 Claude 修改代码。" (spec L191); Claude: "这次直接交给 Claude 实施。完成后你可以点\"让 Codex 验收\"。" (spec L217) |
| 5 | Add Claude-only verification affordance | PASS — "让 Codex 验收" button → conv:{id}:verify → handle_verify |
| 6 | Run focused tests | PASS — 52 passed (11 execution + 41 existing) |

### Acceptance criteria: all satisfied for Task 5 scope

| AC | Requirement | Status |
|----|------------|--------|
| 1 | Ordinary text → Codex→Claude→Codex | PASS — handle_conversation_text → _handle_chief_engineer_impl → orchestrator |
| 2 | /codex never calls Claude | PASS — Codex backend directly; runner.starts==0, claude.calls==0, backend.turns>=1 |
| 3 | /claude no auto Codex analysis/verify | PASS — background Claude invocation; codex_runs==0, runner.starts==0, claude_runs>=1 |
| 4 | "让 Codex 验收" action | PASS — button + callback mapping to handle_verify |

### Execution modes vs view modes: No confusion

Three `ConversationMode` values (CHIEF_ENGINEER, CODEX_DIRECT, CLAUDE_DIRECT) control executor. View modes (COCKPIT, ONSITE from workbench/models.py) are a separate dimension. No cross-contamination.

### Default Codex → Claude → Codex preserved

handle_conversation_text L817-829 unchanged: chief_engineer mode → _handle_chief_engineer_impl → orchestrator. _handle_chief_engineer_impl not modified by Task 5.

### Complete closed loops

| Mode | Start | Execute | Verify/Finish |
|---|---|---|---|
| Orchestrated | plain text or /auto | orchestrator background asyncio task | status/trace, diff |
| Codex-only | /codex <prompt> | Codex app-server start_turn() | "查看状态" button |
| Claude-only | /claude <prompt> | background task → claude.send() → agent run "done" | "让 Codex 验收" + "查看状态" |

### Semantic drift: None introduced

**Non-blocking observations:**

- **SD-A**: handle_conversation_text L826-828 — plain text in CLAUDE_DIRECT mode routes to _handle_codex_analysis_only (Codex), not Claude direct session as spec L517-520 specifies. This is a cross-cutting routing concern assigned to Task 7. NOT a Task 5 issue.
- **SD-B**: "让 Codex 验收" button shown at /claude start, not at completion. V1 timing artifact — Telegram handler returns one synchronous response. Explanatory text "完成后你可以点..." bridges the gap. NOT blocking.

### Unauthorized files: None

Task 5 ownership: `wlcodex/controller.py` (modified, +218/-4), `tests/test_workbench_execution_modes.py` (created). Other modified files belong to other workers per parallelization model.

### User-facing copy: No internal identifiers exposed

Does NOT contain or expose: terminal.enabled, external_session_id, thread id, session id, projection, runtime_events.

---

## Code Quality Reviewer

**Verdict: PASS** (2026-05-20)

### Quality blockers: None

### Reviewed items

| # | Concern | Finding |
|---|---------|---------|
| 1 | Leaked task references in production code | None — no "Task 5", "task-5", FIXME, TODO, HACK, XXX in controller.py |
| 2 | Import placement | `import asyncio` at module top (L5), standard Python convention. `AgentRequest` and `InteractionEvent` use local imports (late-binding, consistent with existing controller pattern at L1235, L1363, L1426, L1615) |
| 3 | Background task lifecycle | `_background_tasks: set[asyncio.Task]` properly tracked, `add_done_callback(self._background_tasks.discard)` prevents memory leak |
| 4 | Error handling | `_run_claude_direct_async` catches Exception, updates agent run to "failed", emits RUN_FAILED event — complete error surface |
| 5 | Streaming vs non-streaming split | Two clear paths: streaming calls `send_streaming()` + forwards deltas through interaction renderer; non-streaming calls `send()` + captures result text. Completion summary correctly handled in both paths |
| 6 | Event emission | USER_MESSAGE_RECEIVED, RUN_COMPLETED, RUN_FAILED events emitted with correct aggregate types, correlation IDs, and visibility scopes |
| 7 | Docstring quality | All three new methods have docstrings explaining purpose and side effects |
| 8 | Redundancy | _handle_claude_direct_impl shares setup pattern with _handle_chief_engineer_impl (task reservation, agent run creation, event emission) — acceptable for separate execution paths |
| 9 | Method visibility | New methods are private (underscore-prefixed), consistent with existing controller method naming |

### Test evidence

```
tests/test_workbench_execution_modes.py — 11 passed (new)
tests/test_command_flow.py              — 7 passed
tests/test_conversation_router.py       — 10 passed
tests/test_orchestration_runner.py      — 24 passed
Total: 52 passed, 0 failed
```

### Files changed (within Task 5 ownership)

```
wlcodex/controller.py                   (+218/-4)
tests/test_workbench_execution_modes.py (new, 393 lines)
```
