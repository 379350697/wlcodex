# WLCodex Full Telegram Codex Cockpit Closure Implementation Plan

> Superseded for product implementation: follow the 2026-05-20 Remote
> Workbench repair plans instead. Task-led `/task`, `/continue`, `/steer`,
> queue, blocker, task id, session id, and thread id user flows below are
> legacy diagnostics only.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current WLCodex skeleton into a complete personal Telegram cockpit that can drive real local Codex app-server tasks, approvals, progress, history, and recovery end to end.

**Architecture:** Keep the existing Python package but close every boundary: Telegram handlers call an authenticated command controller, the controller calls a task service, the task service persists state in SQLite and calls a real app-server backend, and backend events feed local status rendering without re-entering Codex context. Codex protocol details stay isolated in the backend layer, while Telegram displays only typed task/approval/status data.

**Tech Stack:** Python 3.12, `python-telegram-bot`, `websockets`, stdlib `sqlite3`, `pytest`, `pytest-asyncio`, `ruff`, Codex CLI app-server, systemd.

---

## Current State To Preserve

The current code already has:

- package skeleton
- typed config loader
- basic task/event ledger
- basic router
- compact status renderer
- workspace lock helper
- fake backend
- app-server protocol schema files
- tests that currently pass

Do not throw this away. Replace skeleton seams with real behavior and extend tests around each business closure.

## File Structure

- Modify: `wlcodex/config.py` - add backend/approval config and stronger validation.
- Modify: `wlcodex/models.py` - add approval, backend event, callback, and status dataclasses.
- Modify: `wlcodex/db.py` - add migrations for full ledger tables and recovery helpers.
- Modify: `wlcodex/router.py` - parse complete V1 command surface.
- Modify: `wlcodex/status.py` - render task cards, task lists, approval cards, health cards.
- Modify: `wlcodex/locks.py` - ensure locks release on terminal statuses.
- Modify: `wlcodex/codex_backend.py` - implement JSON-RPC client, app-server process manager, real backend, fake backend parity.
- Modify: `wlcodex/task_service.py` - implement state machine, event ingestion, approvals, resume, steer, abort, archive, fork.
- Modify: `wlcodex/telegram_app.py` - implement authenticated handlers and callback buttons.
- Modify: `wlcodex/main.py` - compose config, DB, service, backend, Telegram app, startup recovery.
- Modify: `README.md` - update setup and smoke test commands for this Linux host.
- Modify: `config/wlcodex.example.toml` - add backend/approval config.
- Modify: `deploy/systemd/wlcodex.service.example` - use host-compatible entrypoint.
- Create: `wlcodex/jsonrpc.py` - JSON-RPC request/response correlation.
- Create: `wlcodex/app_server_process.py` - app-server subprocess lifecycle.
- Create: `wlcodex/controller.py` - command controller that returns renderable responses.
- Create: `wlcodex/approval.py` - approval callback encoding, decoding, idempotence.
- Create: `wlcodex/inspection.py` - local tail/events/diff/files inspection.
- Create: `tests/fakes.py` - fake backend and fake Telegram helpers shared by tests.
- Create: `tests/test_jsonrpc.py`
- Create: `tests/test_app_server_process.py`
- Create: `tests/test_approval.py`
- Create: `tests/test_controller_flow.py`
- Create: `tests/test_telegram_handlers.py`
- Create: `tests/test_recovery.py`
- Create: `tests/test_inspection.py`
- Create: `tests/test_app_server_backend_integration.py`

## Task 1: Lock The Review Regressions Into Tests

**Files:**
- Create: `tests/test_review_regressions.py`

- [ ] **Step 1: Add regression tests for the missing business closures**

Create `tests/test_review_regressions.py` with tests asserting:

- `build_application()` registers handlers for `/task`, `/tasks`, `/status`, `/continue`, `/steer`, `/tail`, `/events`, `/diff`, `/files`, `/pause`, `/abort`, `/archive`, `/fork`, `/codex-sessions`, `/health`.
- Unauthorized private users cannot run `/task`.
- Authorized group chats cannot run `/task`.
- `AppServerCodexBackend.create_thread()` no longer raises the spike `RuntimeError` when constructed with a fake JSON-RPC transport.
- A task moves from `queued` to `running` when a turn starts.
- A task moves to `done` when a turn completes.
- A workspace lock is released after `done`, `failed`, or `aborted`.
- `/steer` calls backend steering, not backend continue.
- Approval callbacks are idempotent.

- [ ] **Step 2: Run the regression tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_review_regressions.py -q
```

Expected:

```text
FAIL
```

The failures must point at the current skeleton gaps, not import errors.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_review_regressions.py
git commit -m "test: capture full cockpit closure regressions"
```

## Task 2: Expand Models And Ledger Schema

**Files:**
- Modify: `wlcodex/models.py`
- Modify: `wlcodex/db.py`
- Modify: `tests/test_db.py`
- Create: `tests/test_recovery.py`

- [ ] **Step 1: Extend models**

Add:

- `ApprovalStatus`: `pending`, `approved`, `denied`, `cancelled`, `expired`
- `ApprovalKind`: `command`, `file_change`, `permissions`
- `TaskCommandResult`
- `ApprovalRequest`
- `BackendRequest`
- `TouchedFile`
- `TaskSnapshot`

Extend `Task` with:

- `active_turn_id`
- `telegram_chat_id`
- `telegram_status_message_id`
- `changed_file_count`
- `pending_approval_count`
- `token_input`
- `token_output`

- [ ] **Step 2: Add SQLite migrations**

Update `Ledger.migrate()` so it creates or upgrades:

- `tasks` with the extended fields
- `task_events`
- `approval_requests`
- `touched_files`
- `backend_requests`
- `telegram_updates`

Use idempotent `ALTER TABLE` guards so existing local databases do not crash.

- [ ] **Step 3: Add repository methods**

Add methods:

- `create_task(workspace_alias, workspace_path, title, codex_thread_id, parent_task_id, telegram_chat_id)`
- `set_task_status(task_id, status, phase="", summary="", error="")`
- `set_active_turn(task_id, turn_id)`
- `set_status_message(task_id, chat_id, message_id)`
- `record_event(task_id, event_type, payload)`
- `create_approval(task_id, codex_request_id, codex_item_id, codex_turn_id, kind, summary, command_json, telegram_message_id)`
- `resolve_approval(approval_id, status)`
- `pending_approvals(task_id)`
- `record_touched_file(task_id, path, change_kind)`
- `mark_active_tasks_recovery_paused()`
- `list_tasks(include_archived=False)`

- [ ] **Step 4: Update DB tests**

Add tests proving:

- migrations are idempotent
- approval rows can be created and resolved once
- duplicate approval resolution returns the existing resolved state
- touched files are unique per task/path/change kind
- recovery startup changes `queued`, `running`, `waiting_approval` to `paused` and records events

- [ ] **Step 5: Run DB and recovery tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_recovery.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 6: Commit**

```bash
git add wlcodex/models.py wlcodex/db.py tests/test_db.py tests/test_recovery.py
git commit -m "feat: add full cockpit ledger schema"
```

## Task 3: Implement JSON-RPC Transport Boundary

**Files:**
- Create: `wlcodex/jsonrpc.py`
- Create: `tests/test_jsonrpc.py`

- [ ] **Step 1: Add JSON-RPC tests**

Tests must cover:

- request ids increase monotonically
- successful response resolves the waiting future
- error response raises `JsonRpcError`
- notification dispatch calls registered handlers
- server request dispatch calls registered request handlers and sends one response
- unknown server request returns a JSON-RPC error response

- [ ] **Step 2: Implement `JsonRpcClient`**

Create a transport-agnostic class with:

- `async request(method: str, params: dict) -> dict`
- `async notify(method: str, params: dict) -> None`
- `on_notification(method: str, handler)`
- `on_server_request(method: str, handler)`
- `async receive_message(message: dict) -> None`
- `async close()`

The class must accept async `send_json(message: dict)` in the constructor so tests can run without WebSocket.

- [ ] **Step 3: Run JSON-RPC tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_jsonrpc.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/jsonrpc.py tests/test_jsonrpc.py
git commit -m "feat: add json-rpc transport boundary"
```

## Task 4: Implement App-Server Process Manager

**Files:**
- Create: `wlcodex/app_server_process.py`
- Create: `tests/test_app_server_process.py`

- [ ] **Step 1: Add process manager tests**

Tests must verify:

- command includes configured Codex binary
- command binds to `ws://127.0.0.1:<port>`
- non-loopback host is rejected
- startup timeout produces a backend health error
- shutdown terminates the process once

- [ ] **Step 2: Implement process manager**

Create:

- `AppServerProcessConfig`
- `AppServerProcess`
- `BackendHealth`

The process manager starts:

```bash
codex app-server --listen ws://127.0.0.1:<port>
```

It must not bind to `0.0.0.0`.

- [ ] **Step 3: Run process tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_app_server_process.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/app_server_process.py tests/test_app_server_process.py
git commit -m "feat: manage local codex app-server process"
```

## Task 5: Implement Real AppServerCodexBackend

**Files:**
- Modify: `wlcodex/codex_backend.py`
- Create: `tests/test_codex_backend_events.py`
- Modify: `tests/test_codex_backend.py`
- Create: `tests/test_app_server_backend_integration.py`

- [ ] **Step 1: Extend backend protocol**

Add methods:

- `create_thread(workspace_path: str) -> str`
- `start_turn(thread_id: str, prompt: str) -> str`
- `continue_turn(thread_id: str, prompt: str) -> str`
- `steer_turn(thread_id: str, expected_turn_id: str, prompt: str) -> None`
- `interrupt_turn(thread_id: str, turn_id: str) -> None`
- `fork_thread(thread_id: str, workspace_path: str) -> str`
- `archive_thread(thread_id: str) -> None`
- `resolve_approval(codex_request_id: str, decision: str) -> None`
- `events() -> AsyncIterator[BackendEvent]`
- `health() -> BackendHealth`

Update `FakeCodexBackend` with the same behavior so controller tests can assert business flow.

- [ ] **Step 2: Map app-server methods**

Implement real backend mappings:

- `thread/start`
- `thread/resume`
- `thread/fork`
- `thread/archive`
- `turn/start`
- `turn/steer`
- `turn/interrupt`

`start_turn` input must be a single text item containing only the user's prompt.

- [ ] **Step 3: Map notifications to typed events**

Translate:

- thread status changes
- turn started/completed
- plan updates
- diff updates
- command output deltas
- file change output deltas
- token usage updates
- approval server requests

Do not expose raw Telegram concepts in backend events.

- [ ] **Step 4: Add backend unit tests**

Use a fake JSON-RPC transport to assert exact outgoing methods and payloads:

- `create_thread` sends `thread/start`
- `start_turn` sends `turn/start`
- `continue_turn` sends `thread/resume` then `turn/start`
- `steer_turn` sends `turn/steer`
- approval server request becomes `BackendEvent(type="approval_requested")`
- command output delta becomes local event data only

- [ ] **Step 5: Add gated integration test**

Create `tests/test_app_server_backend_integration.py` that skips unless:

```bash
WLCODEX_RUN_CODEX_INTEGRATION=1
```

The test starts app-server on loopback, creates a temporary workspace, starts a thread, sends:

```text
Reply with exactly: wlcodex integration ok
```

Expected assistant completion contains:

```text
wlcodex integration ok
```

- [ ] **Step 6: Run backend unit tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_codex_backend.py tests/test_codex_backend_events.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 7: Commit**

```bash
git add wlcodex/codex_backend.py tests/test_codex_backend.py tests/test_codex_backend_events.py tests/test_app_server_backend_integration.py
git commit -m "feat: implement codex app-server backend"
```

## Task 6: Implement Approval Service

**Files:**
- Create: `wlcodex/approval.py`
- Create: `tests/test_approval.py`

- [ ] **Step 1: Add approval tests**

Tests must cover:

- callback data encodes approval id and action
- callback data rejects malformed payloads
- approve once maps to app-server `accept`
- approve session maps to `acceptForSession`
- deny maps to `decline`
- cancel maps to `cancel`
- duplicate callback does not send a second backend response
- expired approval returns an expired user message

- [ ] **Step 2: Implement approval service**

Create:

- `ApprovalAction`
- `ApprovalCallback`
- `encode_approval_callback(approval_id, action)`
- `decode_approval_callback(data)`
- `ApprovalService.resolve_callback(callback, backend, ledger, now)`

It must update SQLite before sending the app-server response enough to prevent duplicate approvals. If backend response fails, record `last_error` on the approval row.

- [ ] **Step 3: Run approval tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_approval.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/approval.py tests/test_approval.py
git commit -m "feat: add idempotent approval resolution"
```

## Task 7: Implement Task Service State Machine

**Files:**
- Modify: `wlcodex/task_service.py`
- Modify: `wlcodex/locks.py`
- Modify: `tests/test_task_service.py`

- [ ] **Step 1: Add state machine tests**

Tests must cover every allowed transition in the spec and reject:

- done -> running
- archived -> running
- failed -> running without explicit continue
- running -> archived

Also test:

- start task acquires workspace lock
- turn completed releases workspace lock by moving to done
- failed backend moves task to failed and releases lock
- approval requested moves task to waiting_approval
- approval resolved moves waiting_approval back to running

- [ ] **Step 2: Implement lifecycle methods**

Add:

- `start_task(workspace_alias, prompt, telegram_chat_id)`
- `continue_task(task_id, prompt)`
- `steer_task(task_id, prompt)`
- `pause_task(task_id)`
- `abort_task(task_id)`
- `archive_task(task_id)`
- `fork_task(parent_task_id, prompt)`
- `apply_backend_event(event)`
- `complete_turn(task_id, turn_id)`
- `fail_task(task_id, error)`

These methods must be the only place that changes task state.

- [ ] **Step 3: Remove private attribute use**

Replace callers of `service._ledger` and `service._workspace` with public methods:

- `service.list_tasks()`
- `service.get_task(task_id)`
- `service.get_workspace(alias)`

- [ ] **Step 4: Run task service tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_service.py tests/test_review_regressions.py -q
```

Expected:

```text
PASS for task service and state-machine related review regressions
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/task_service.py wlcodex/locks.py tests/test_task_service.py tests/test_review_regressions.py
git commit -m "feat: close task lifecycle state machine"
```

## Task 8: Implement Inspection Service

**Files:**
- Create: `wlcodex/inspection.py`
- Create: `tests/test_inspection.py`

- [ ] **Step 1: Add inspection tests**

Tests must prove:

- `/events` reads SQLite events
- `/tail` reads local task log excerpts
- `/files` reads touched files
- `/diff` returns a bounded string
- inspection commands do not call backend
- output respects configured max characters

- [ ] **Step 2: Implement inspection service**

Create:

- `TaskInspector.events(task_id)`
- `TaskInspector.tail(task_id)`
- `TaskInspector.files(task_id)`
- `TaskInspector.diff(task_id)`

`diff()` may use the latest stored diff event first. If none exists, run `git diff --stat` in the configured workspace with a timeout and return a bounded result.

- [ ] **Step 3: Run inspection tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_inspection.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/inspection.py tests/test_inspection.py
git commit -m "feat: add local task inspection"
```

## Task 9: Implement Command Controller

**Files:**
- Create: `wlcodex/controller.py`
- Modify: `wlcodex/router.py`
- Create: `tests/test_controller_flow.py`

- [ ] **Step 1: Expand router tests**

Ensure parser supports:

- `/help`
- `/task <workspace> <prompt>`
- `/task <id>`
- `/tasks`
- `/status`
- `/continue <id> <prompt>`
- `/steer <id> <prompt>`
- `/tail <id>`
- `/events <id>`
- `/diff <id>`
- `/files <id>`
- `/pause <id>`
- `/abort <id>`
- `/archive <id>`
- `/fork <id> <prompt>`
- `/codex-sessions`
- `/health`

- [ ] **Step 2: Implement controller**

Create `CommandController.handle(text, telegram_context) -> ControllerResponse`.

It must:

- route commands
- call task service
- call backend
- call inspector
- return text plus optional buttons
- never inject status/log/diff text into Codex prompts

- [ ] **Step 3: Add controller flow tests**

Test:

- `/task` calls backend `create_thread` and `start_turn`
- `/continue` calls backend `continue_turn`
- `/steer` calls backend `steer_turn`
- `/tail` does not call backend
- `/archive` refuses running task
- `/health` reports backend and DB health

- [ ] **Step 4: Run controller tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_router.py tests/test_controller_flow.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/router.py wlcodex/controller.py tests/test_router.py tests/test_controller_flow.py
git commit -m "feat: add command controller"
```

## Task 10: Implement Telegram Handlers And Buttons

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Create: `tests/test_telegram_handlers.py`
- Modify: `tests/test_telegram_auth.py`

- [ ] **Step 1: Add Telegram handler tests**

Use fake update/context helpers to test:

- every V1 command is registered
- unauthorized updates are rejected before controller
- group updates are rejected before controller
- authorized `/task` calls controller
- parse errors become user-facing usage messages
- controller exceptions become concise error messages
- approval callback calls approval service
- duplicate/expired approval callback edits message with final status

- [ ] **Step 2: Implement auth guard**

Every handler must call a shared guard:

```python
ensure_authorized(update, config.telegram)
```

No operational handler may bypass it.

- [ ] **Step 3: Register command handlers**

Register:

- `start`
- `help`
- `task`
- `tasks`
- `status`
- `continue`
- `steer`
- `tail`
- `events`
- `diff`
- `files`
- `pause`
- `abort`
- `archive`
- `fork`
- `codex-sessions`
- `health`

Register `CallbackQueryHandler` for approval callbacks.

- [ ] **Step 4: Implement status message update behavior**

On task creation, store `chat_id` and `message_id`. Backend event loop may edit this message through a status updater. If edit fails, send a new status message and update SQLite mapping.

- [ ] **Step 5: Run Telegram tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_telegram_auth.py tests/test_telegram_handlers.py tests/test_review_regressions.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 6: Commit**

```bash
git add wlcodex/telegram_app.py tests/test_telegram_auth.py tests/test_telegram_handlers.py tests/test_review_regressions.py
git commit -m "feat: wire authenticated telegram cockpit"
```

## Task 11: Implement Status Updater And Backend Event Pump

**Files:**
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/main.py`
- Modify: `wlcodex/task_service.py`
- Create: `tests/test_status_updates.py`

- [ ] **Step 1: Add status update tests**

Tests must cover:

- backend turn started event updates task to running
- plan update changes phase
- diff update changes changed file count
- approval request changes status to waiting_approval
- turn completed changes status to done when no pending approval remains
- status text stays under Telegram limits
- update throttling coalesces noisy events

- [ ] **Step 2: Implement event pump**

Create an async background task that:

- consumes `backend.events()`
- calls `task_service.apply_backend_event(event)`
- records local event/log data
- schedules Telegram status edit

It must not call backend with text generated from status rendering.

- [ ] **Step 3: Implement richer renderers**

Add:

- `render_task_card(snapshot)`
- `render_task_list(tasks)`
- `render_approval_card(approval)`
- `render_health_card(health)`
- `render_inspection_result(title, body)`

- [ ] **Step 4: Run status update tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_status.py tests/test_status_updates.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 5: Commit**

```bash
git add wlcodex/status.py wlcodex/main.py wlcodex/task_service.py tests/test_status.py tests/test_status_updates.py
git commit -m "feat: update telegram status from backend events"
```

## Task 12: Compose Runtime In `main`

**Files:**
- Modify: `wlcodex/main.py`
- Modify: `tests/test_command_flow.py`
- Create: `tests/test_main_composition.py`

- [ ] **Step 1: Add composition tests**

Tests must assert:

- `main` opens SQLite path from config
- runs migrations
- runs recovery pause
- builds task service with configured workspaces
- builds app-server backend with configured Codex host/port
- builds Telegram app with controller and approval service
- missing token exits with clear message

- [ ] **Step 2: Implement composition root**

`main.py` must:

1. parse `--config`
2. load config
3. read Telegram token from env
4. open ledger
5. migrate
6. recovery pause active tasks
7. create app-server process/backend
8. create task service
9. create inspector
10. create approval service
11. create controller
12. build Telegram app
13. run polling
14. shutdown backend cleanly on exit

- [ ] **Step 3: Run composition tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_main_composition.py tests/test_command_flow.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 4: Commit**

```bash
git add wlcodex/main.py tests/test_main_composition.py tests/test_command_flow.py
git commit -m "feat: compose full cockpit runtime"
```

## Task 13: Update Config, README, Systemd, And Ignore Rules

**Files:**
- Modify: `config/wlcodex.example.toml`
- Modify: `README.md`
- Modify: `deploy/systemd/wlcodex.service.example`
- Modify: `.gitignore`

- [ ] **Step 1: Update config example**

Add:

```toml
[backend]
startup_timeout_seconds = 15
request_timeout_seconds = 60
event_log_max_chars = 20000

[approval]
callback_timeout_seconds = 3600
allow_session_approval = true
```

- [ ] **Step 2: Update README with real commands**

Use `.venv/bin/python` and `.venv/bin/wlcodex` in examples because this host does not have `python` on PATH.

Document:

- creating Telegram bot token
- setting allowed user id
- creating `config/wlcodex.toml`
- running `.venv/bin/python -m pytest -q`
- running `.venv/bin/wlcodex --config config/wlcodex.toml`
- manual smoke test command sequence

- [ ] **Step 3: Update systemd service**

Use:

```ini
ExecStart=/media/wl/新加卷/codex/wlcodex/.venv/bin/wlcodex --config /media/wl/新加卷/codex/wlcodex/config/wlcodex.toml
```

Document token should be supplied through an environment file or drop-in, not committed.

- [ ] **Step 4: Update `.gitignore`**

Ensure these are ignored:

```gitignore
.venv/
.pytest_cache/
.ruff_cache/
__pycache__/
*.py[cod]
*.egg-info/
runtime/*.sqlite3
runtime/tasks/
runtime/*.log
config/wlcodex.toml
```

Do not ignore `runtime/protocol/` if generated schema files remain intentionally tracked.

- [ ] **Step 5: Commit docs/config**

```bash
git add config/wlcodex.example.toml README.md deploy/systemd/wlcodex.service.example .gitignore
git commit -m "docs: document full cockpit runtime"
```

## Task 14: End-To-End Fake Backend Acceptance

**Files:**
- Create: `tests/test_e2e_fake_backend.py`

- [ ] **Step 1: Add fake backend E2E tests**

Create tests for:

- authorized user starts `/task`
- backend emits turn started
- status updates to running
- backend emits approval request
- Telegram callback approves once
- backend receives approval resolution
- backend emits turn completed
- status becomes done
- `/tasks` shows done task
- `/continue <id>` uses the same thread id
- `/steer <id>` refuses after done and says to use `/continue`
- `/archive <id>` archives task

- [ ] **Step 2: Run fake E2E tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_e2e_fake_backend.py -q
```

Expected:

```text
PASS
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_e2e_fake_backend.py
git commit -m "test: add full fake-backend cockpit acceptance"
```

## Task 15: Real App-Server Smoke Verification

**Files:**
- Modify: `tests/test_app_server_backend_integration.py`
- Create: `docs/superpowers/reports/2026-05-16-wlcodex-real-app-server-smoke.md`

- [ ] **Step 1: Run unit suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected:

```text
PASS
```

- [ ] **Step 2: Run lint**

Run:

```bash
.venv/bin/python -m ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 3: Run real app-server integration if credentials/session allow**

Run:

```bash
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_app_server_backend_integration.py -q
```

Expected:

```text
PASS
```

If credentials or local Codex auth block this test, record the exact blocker in the report and do not claim real app-server acceptance.

- [ ] **Step 4: Write smoke report**

Create `docs/superpowers/reports/2026-05-16-wlcodex-real-app-server-smoke.md` with:

- test commands run
- pass/fail output
- whether real app-server integration passed
- any manual Telegram smoke result
- remaining operational risks

- [ ] **Step 5: Commit verification report**

```bash
git add tests/test_app_server_backend_integration.py docs/superpowers/reports/2026-05-16-wlcodex-real-app-server-smoke.md
git commit -m "test: verify full cockpit closure"
```

## Final Review Checklist

Before declaring this implementation complete:

- [ ] `/start` and `/help` work for the authorized private user.
- [ ] Unauthorized users cannot run operational commands.
- [ ] `/task` starts real Codex app-server work.
- [ ] Status card updates from backend events.
- [ ] Approval card resolves app-server requests.
- [ ] `/continue` resumes the selected thread only.
- [ ] `/steer` uses active turn steering.
- [ ] `/tail`, `/events`, `/diff`, `/files` do not call Codex.
- [ ] Task terminal states release workspace lock.
- [ ] Restart recovery pauses active tasks.
- [ ] `.venv/bin/python -m pytest -q` passes.
- [ ] `.venv/bin/python -m ruff check .` passes.
- [ ] Real app-server integration is either passing or explicitly blocked with evidence.

## Self-Review

- Spec coverage: every full-closure flow in the spec maps to at least one task: auth in Task 10, Codex backend in Task 5, approvals in Task 6, state machine in Task 7, inspections in Task 8, controller in Task 9, runtime composition in Task 12, recovery in Task 2/12, E2E in Task 14.
- Placeholder scan: no task asks for an unspecified future implementation. The real app-server smoke allows a credentials blocker only if recorded with evidence.
- Type consistency: task ids are integers; Codex thread and turn ids are strings; approval ids use SQLite integer ids plus Codex JSON-RPC request ids; command parsing returns typed command objects.
