# WLCodex Maintainability Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce the maintenance cost of WLCodex's largest modules through incremental, tested extractions that preserve existing behavior.

**Architecture:** Keep current public facades stable while moving implementation details into smaller, responsibility-focused modules. Start with low-risk static asset delivery for live stream pages, then split command dispatch, persistence internals, event handling, and Telegram command groups.

**Tech Stack:** Python 3.14, asyncio, stdlib `sqlite3`, package-local static assets, existing pytest suite, GitNexus impact/detect-changes.

---

## Scope Order

This plan is intentionally phased. Do not attempt all phases in one large change.

1. Establish live stream static asset delivery and extract the first asset.
2. Extract `CommandController` dispatch data without moving business behavior.
3. Split `Ledger` internals behind its existing facade.
4. Split `EventBridge` handlers behind `process_event()`.
5. Split Telegram command groups and keyboard helpers.
6. Audit live stream safety and accessibility after asset extraction.

## Task 1: Add Live Stream Static Asset Delivery

**Files:**
- Modify: `wlcodex/live_stream/server.py`
- Create: `wlcodex/live_stream/static/native_index.css`
- Test: `tests/test_worker_live_stream_server.py`

- [ ] **Step 1: Run GitNexus impact before editing**

Run:

```bash
npx gitnexus impact --repo wlcodex WorkerLiveStreamServer --direction upstream
npx gitnexus impact --repo wlcodex _send_html --direction upstream
```

Expected: report direct callers and affected tests. If risk is high or critical, stop and report before editing.

- [ ] **Step 2: Write failing test for static CSS route**

Add a test to `tests/test_worker_live_stream_server.py` that requests `/static/native_index.css` through `WorkerLiveStreamServer._handle_client` or the existing server test helper.

The test must assert:

```python
assert status_code == 200
assert "Content-Type: text/css; charset=utf-8" in response_headers
assert "Cache-Control: no-cache" in response_headers
assert ".provider-index" in response_body
```

- [ ] **Step 3: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py -k "static_css" -vv
```

Expected: fail because `/static/native_index.css` is not routed yet.

- [ ] **Step 4: Add static asset route**

Implement a minimal route in `WorkerLiveStreamServer._handle_client` for paths under `/static/`.

Rules:

- Serve only package-local files under `wlcodex/live_stream/static/`.
- Reject path traversal.
- Use explicit content types for `.css` and `.js`.
- Return `404` for missing assets.
- Use `Cache-Control: no-cache` during this transition.

- [ ] **Step 5: Create first CSS asset**

Create `wlcodex/live_stream/static/native_index.css` with a small class that can be loaded independently:

```css
.provider-index {
  min-height: 100vh;
}
```

- [ ] **Step 6: Run focused test and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py -k "static_css" -vv
```

Expected: pass.

- [ ] **Step 7: Run live stream regression subset**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_server.py tests/test_worker_live_stream_native_routes.py -q
```

Expected: pass.

## Task 2: Use Static CSS In Native Provider Index Page

**Files:**
- Modify: `wlcodex/live_stream/server.py`
- Modify: `wlcodex/live_stream/static/native_index.css`
- Test: `tests/test_worker_live_stream_native_routes.py`

- [ ] **Step 1: Run GitNexus impact**

Run:

```bash
npx gitnexus impact --repo wlcodex _native_provider_index_html --direction upstream
```

Expected: identify route callers and page tests.

- [ ] **Step 2: Write failing test for stylesheet link**

Add or update a native provider index route test to assert:

```python
assert '<link rel="stylesheet" href="/static/native_index.css">' in html
```

- [ ] **Step 3: Verify RED**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -k "provider_index" -vv
```

Expected: fail because the page does not link the stylesheet yet.

- [ ] **Step 4: Move native provider index CSS**

Move the CSS rules from `_native_provider_index_html` into `native_index.css` and add the stylesheet link to the returned HTML.

Keep only dynamic title/body data in Python.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_worker_live_stream_native_routes.py -k "provider_index" -vv
```

Expected: pass.

## Task 3: Extract Command Dispatch Metadata

**Files:**
- Create: `wlcodex/controller_dispatch.py`
- Modify: `wlcodex/controller.py`
- Test: `tests/test_command_flow.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Run GitNexus impact**

Run:

```bash
npx gitnexus impact --repo wlcodex CommandController --direction upstream
npx gitnexus impact --repo wlcodex "CommandController.handle" --direction upstream
```

Expected: broad caller/test impact. Report risk before editing.

- [ ] **Step 2: Write failing dispatch table test**

Add a test that imports `wlcodex.controller_dispatch` and verifies key commands map to stable handler names:

```python
assert command_handler_name("new") == "handle_new_conversation"
assert command_handler_name("codex") == "handle_codex_direct"
assert command_handler_name("claude") == "handle_claude_direct"
assert command_handler_name("auto") == "handle_auto_mode"
```

- [ ] **Step 3: Verify RED**

Run:

```bash
.venv/bin/pytest tests/test_command_flow.py -k "dispatch_table" -vv
```

Expected: fail because `controller_dispatch.py` does not exist.

- [ ] **Step 4: Create dispatch helper**

Create `wlcodex/controller_dispatch.py` with a small immutable mapping and lookup function:

```python
from __future__ import annotations

COMMAND_HANDLER_NAMES = {
    "new": "handle_new_conversation",
    "codex": "handle_codex_direct",
    "claude": "handle_claude_direct",
    "auto": "handle_auto_mode",
    "verify": "handle_verify",
    "stop": "handle_stop_current",
    "switch": "handle_switch_workspace",
    "model": "handle_model",
    "exec_mode": "handle_exec_mode",
    "claude_mode": "handle_claude_permission",
}


def command_handler_name(command_type: str) -> str:
    return COMMAND_HANDLER_NAMES[command_type]
```

- [ ] **Step 5: Wire `CommandController.handle()` to the helper**

Replace direct repeated command-name dispatch for the covered commands with lookup through `command_handler_name`.

Do not move handler implementations in this task.

- [ ] **Step 6: Verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_command_flow.py tests/test_controller_flow.py -q
```

Expected: pass.

## Task 4: Extract Ledger Row Mappers

**Files:**
- Create: `wlcodex/db_rows.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Run GitNexus impact**

Run:

```bash
npx gitnexus impact --repo wlcodex "Class:wlcodex/db.py:Ledger" --direction upstream
```

Expected: broad persistence and test impact. Report risk before editing.

- [ ] **Step 2: Write mapper import regression test**

Add a test that imports representative row mapper names from `wlcodex.db_rows`.

Expected names:

```python
from wlcodex.db_rows import task_from_row, event_from_row, conversation_from_row
```

- [ ] **Step 3: Verify RED**

Run:

```bash
.venv/bin/pytest tests/test_db.py -k "row_mapper" -vv
```

Expected: fail because `db_rows.py` does not exist.

- [ ] **Step 4: Move row mappers**

Move row mapper functions from the bottom of `db.py` into `db_rows.py` with public names:

- `_task` -> `task_from_row`
- `_event` -> `event_from_row`
- `_conversation` -> `conversation_from_row`

Keep compatibility aliases inside `db.py` if needed for incremental migration.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_db.py -q
```

Expected: pass.

## Task 5: Extract EventBridge Approval Notifications

**Files:**
- Create: `wlcodex/event_approval_notifications.py`
- Modify: `wlcodex/event_bridge.py`
- Test: `tests/test_event_bridge.py`

- [ ] **Step 1: Run GitNexus impact**

Run:

```bash
npx gitnexus impact --repo wlcodex EventBridge --direction upstream
npx gitnexus impact --repo wlcodex "_on_approval_requested" --direction upstream
```

Expected: event bridge and approval-flow test impact.

- [ ] **Step 2: Write failing helper test**

Add a test for a pure helper that builds approval buttons from approval id and session-approval policy.

Expected behavior:

```python
buttons = build_approval_buttons(approval_id=123, allow_session=True)
assert buttons[0][0]["text"] == "批准一次"
assert buttons[0][1]["text"] == "本会话批准"
```

- [ ] **Step 3: Verify RED**

Run:

```bash
.venv/bin/pytest tests/test_event_bridge.py -k "approval_buttons" -vv
```

Expected: fail because the helper module does not exist.

- [ ] **Step 4: Create helper and wire EventBridge**

Move approval button construction from `EventBridge._on_approval_requested` into `event_approval_notifications.py`.

Keep `_on_approval_requested` as the orchestration method that finds task/approval rows and sends Telegram.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_event_bridge.py -q
```

Expected: pass.

## Task 6: Detect Changes And Phase Gate

**Files:**
- No source edits unless fixing regressions.

- [ ] **Step 1: Run GitNexus detect changes**

Run:

```bash
npx gitnexus detect-changes --repo wlcodex
```

Expected: affected symbols match the phase scope.

- [ ] **Step 2: Run focused phase suite**

Run:

```bash
.venv/bin/pytest \
  tests/test_worker_live_stream_server.py \
  tests/test_worker_live_stream_native_routes.py \
  tests/test_command_flow.py \
  tests/test_controller_flow.py \
  tests/test_db.py \
  tests/test_event_bridge.py \
  -q
```

Expected: pass.

- [ ] **Step 3: Review and commit**

Review diff:

```bash
git diff --stat
git diff -- docs/superpowers/specs/2026-06-04-wlcodex-maintainability-optimization-design.md \
  docs/superpowers/plans/2026-06-04-wlcodex-maintainability-optimization-implementation-plan.md
```

Commit only after verification:

```bash
git add docs/superpowers/specs/2026-06-04-wlcodex-maintainability-optimization-design.md \
  docs/superpowers/plans/2026-06-04-wlcodex-maintainability-optimization-implementation-plan.md \
  wlcodex/live_stream/server.py \
  wlcodex/live_stream/static/native_index.css \
  wlcodex/controller_dispatch.py \
  wlcodex/db_rows.py \
  wlcodex/event_approval_notifications.py \
  tests/test_worker_live_stream_server.py \
  tests/test_worker_live_stream_native_routes.py \
  tests/test_command_flow.py \
  tests/test_db.py \
  tests/test_event_bridge.py
git commit -m "refactor: start maintainability optimization"
```

## Later Phase Plan Seeds

After Task 6, create separate plans for:

- full live page CSS/JS extraction
- conversation/workbench controller module split
- `Ledger` schema module extraction
- staged auto event bridge extraction
- Telegram command group split
- live stream accessibility and `innerHTML` audit
