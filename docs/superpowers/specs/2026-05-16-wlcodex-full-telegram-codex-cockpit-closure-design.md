# WLCodex Full Telegram Codex Cockpit Closure Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

> Superseded: the 2026-05-20 Remote Workbench repair replaces this task-led
> product model. References to `/task`, `/continue`, `/steer`, task queues,
> task ids, and user-managed session/thread ids are legacy diagnostics only and
> must not be used for Cockpit, Onsite, `/status`, `/help`, `/sessions`, or
> ordinary Telegram text.

## Summary

This design replaces the earlier skeleton-first WLCodex scope with a full business closure scope. The target is a usable personal Telegram cockpit that can remotely drive local Linux Codex CLI through Codex app-server: start real Codex tasks, stream progress into low-noise Telegram status cards, request approvals through Telegram buttons, continue or steer historical tasks explicitly, inspect logs/diffs on demand, and recover after daemon restarts.

The project remains personal, local-first, and lightweight. The full closure means every user-visible command either works end to end or is deliberately absent from the command surface. The bot must not advertise capabilities that are only implemented in tests or documents.

## Non-Negotiable Invariants

1. Telegram progress, status cards, logs, diffs, and local event summaries are never fed back into Codex context automatically.
2. New `/task` commands create fresh Codex threads by default.
3. Historical context is used only by explicit `/continue <task_id>` or `/fork <task_id>` commands.
4. SQLite is a ledger, not model memory. The ledger records what happened; it does not silently become prompt context.
5. One configured workspace can have only one active write task at a time.
6. Every Telegram handler is private-chat and allowlist guarded.
7. Real Codex execution must use app-server on loopback only.
8. Approvals must be resolved by explicit Telegram button actions and recorded in SQLite.
9. If a feature is visible in Telegram help, it must be wired to real behavior and tests.
10. If Codex app-server is unavailable, the bot must stay alive and report the backend as unhealthy instead of pretending to run tasks.

## Full Closure Definition

WLCodex is closed for V1 only when these flows work end to end:

- `/start` shows safe help for an authorized private chat.
- Unauthorized users and group chats receive no operational access.
- `/task <workspace> <prompt>` creates a fresh Codex app-server thread, starts a turn, creates a task row, creates or edits a Telegram status card, and records events.
- Codex streamed notifications update task phase, summary, changed-file hints, turn id, and status card without entering Codex context.
- Codex approval server requests create pending approval rows and Telegram buttons.
- Telegram approval buttons resolve app-server JSON-RPC requests and update the ledger.
- `/tasks` and `/status` list active and recent tasks from SQLite.
- `/task <task_id>` renders task details and action buttons.
- `/continue <task_id> <prompt>` resumes that exact thread and starts a new turn with only the user prompt.
- `/steer <task_id> <prompt>` sends `turn/steer` to the currently active turn, and refuses with a clear message if no active turn exists.
- `/tail <task_id>`, `/events <task_id>`, `/diff <task_id>`, and `/files <task_id>` inspect local state only.
- `/pause <task_id>`, `/abort <task_id>`, `/archive <task_id>`, and `/fork <task_id> <prompt>` update task state or app-server state consistently.
- Daemon restart marks previously active tasks as recovery-paused and keeps history browsable.
- systemd startup works with a configured token environment variable.

## Explicit Non-Goals

- No Discord.
- No multi-user mode.
- No arbitrary `/sh`.
- No cron automation.
- No automatic deployment.
- No multi-agent execution.
- No implicit long-term memory.
- No public network listener.
- No web dashboard.

## User Workflows

### Authorization

All Telegram updates are rejected unless:

- `effective_user.id` is in `telegram.allowed_user_ids`
- `effective_chat.type == "private"`
- `telegram.private_chat_only == true`

Rejected updates are logged locally with user id and chat type. They do not reveal workspace aliases, task ids, or system state.

### Start New Task

Command:

```text
/task lightfee Fix the health timeout, keep deployment untouched
```

Expected flow:

1. Telegram handler validates auth.
2. Router parses workspace alias and prompt.
3. Task service validates configured workspace and `allow_write`.
4. Task service acquires the workspace write lock.
5. App-server backend starts or reuses a loopback app-server process.
6. Backend calls `thread/start` with `cwd`, configured sandbox, and configured approval policy.
7. Ledger creates task with `codex_thread_id`.
8. Backend calls `turn/start` with one text item containing only the prompt.
9. Ledger records `task_created`, `thread_started`, and `turn_started`.
10. Telegram sends a status card and stores message id for future edits.

### Monitor Progress

App-server notifications are converted to local events:

- `thread/status/changed`
- `turn/started`
- `turn/plan/updated`
- `turn/diff/updated`
- `item/started`
- `item/completed`
- `item/agentMessage/delta`
- `item/commandExecution/outputDelta`
- `item/fileChange/outputDelta`
- `turn/completed`

The status card is edited at most once per configured interval. It shows:

- task id and title
- workspace
- state
- phase
- active command summary
- pending approval count
- changed file count
- token usage if app-server reports it
- last concise assistant/status excerpt

Full logs are written locally and are available through inspection commands. They are not sent to Codex.

### Approval

When app-server sends approval server requests:

- command execution approval
- file change approval
- permissions approval

WLCodex creates an `approval_requests` row and edits/sends a Telegram approval card with buttons:

- Approve once
- Approve session
- Deny
- Cancel

The callback handler:

1. validates auth
2. checks approval status is still pending
3. checks callback belongs to the same task/request
4. sends the correct JSON-RPC response to app-server
5. records resolution
6. updates the status card

Late or duplicate callbacks must not send a second app-server response.

### Continue Historical Task

Command:

```text
/continue 42 Use the conservative path and run tests again
```

Expected behavior:

- Task #42 is loaded from SQLite.
- Its `codex_thread_id` must exist.
- Backend calls `thread/resume`.
- Backend calls `turn/start` with only the new user prompt.
- No previous logs, status cards, or summaries are injected.

### Steer Active Turn

Command:

```text
/steer 42 Stop changing config files and explain the failing test
```

Expected behavior:

- Task #42 must have an active turn id.
- Backend calls `turn/steer` with `threadId`, `expectedTurnId`, and one text item.
- If no active turn exists, Telegram says to use `/continue`.

### Inspect

Inspection commands never call Codex:

- `/tail 42` reads local event/log files.
- `/events 42` reads recent SQLite event rows.
- `/diff 42` uses last app-server diff event or local `git diff --stat` scoped to the task workspace.
- `/files 42` reads touched file events.
- `/codex-sessions` lists SQLite task/thread mappings and app-server loaded threads if available.

### Pause, Abort, Archive, Fork

- `/pause 42` marks task paused and stops Telegram status edits. If a turn is active, the user is prompted to abort or leave Codex running.
- `/abort 42` calls `turn/interrupt` when an active turn exists, then marks task aborted.
- `/archive 42` archives local task only after it is done, failed, paused, or aborted.
- `/fork 42 <prompt>` creates a new task and a fresh app-server thread. It links `parent_task_id=42` and sends only the fork prompt plus a short user-visible parent reference, not hidden logs.

## State Machine

Task statuses:

- `queued`: row created before turn starts
- `running`: active Codex turn is running
- `waiting_approval`: app-server is waiting for at least one approval request
- `paused`: user paused task or restart recovery paused it
- `done`: last turn completed successfully and no pending approval remains
- `failed`: backend or task execution failed
- `aborted`: user aborted active work
- `archived`: hidden from default task list

Allowed transitions:

- queued -> running
- queued -> failed
- running -> waiting_approval
- running -> done
- running -> failed
- running -> paused
- running -> aborted
- waiting_approval -> running
- waiting_approval -> failed
- waiting_approval -> paused
- waiting_approval -> aborted
- done -> archived
- failed -> archived
- paused -> running
- paused -> archived
- aborted -> archived

Workspace write lock is active for `queued`, `running`, `waiting_approval`, and `paused`. It is released for `done`, `failed`, `aborted`, and `archived`.

## Data Model

SQLite tables:

### tasks

- `id`
- `workspace_alias`
- `workspace_path`
- `title`
- `status`
- `codex_thread_id`
- `active_turn_id`
- `parent_task_id`
- `telegram_chat_id`
- `telegram_status_message_id`
- `created_at`
- `updated_at`
- `last_phase`
- `last_summary`
- `last_error`
- `changed_file_count`
- `pending_approval_count`
- `token_input`
- `token_output`

### task_events

- `id`
- `task_id`
- `event_type`
- `payload_json`
- `created_at`

### approval_requests

- `id`
- `task_id`
- `codex_request_id`
- `codex_item_id`
- `codex_turn_id`
- `kind`
- `summary`
- `command_json`
- `status`
- `telegram_message_id`
- `created_at`
- `resolved_at`

### touched_files

- `id`
- `task_id`
- `path`
- `change_kind`
- `created_at`

### backend_requests

- `id`
- `jsonrpc_id`
- `method`
- `task_id`
- `status`
- `created_at`
- `completed_at`
- `error`

### telegram_updates

- `id`
- `telegram_update_id`
- `user_id`
- `chat_id`
- `update_type`
- `allowed`
- `created_at`

## Codex App-Server Integration

The backend owns:

- app-server process lifecycle
- WebSocket connection
- JSON-RPC request ids
- response correlation
- notification dispatch
- server request response dispatch

Required methods:

- `thread/start`
- `thread/resume`
- `thread/fork`
- `thread/archive`
- `turn/start`
- `turn/steer`
- `turn/interrupt`

Required server notifications:

- `thread/started`
- `thread/status/changed`
- `thread/tokenUsage/updated`
- `turn/started`
- `turn/completed`
- `turn/diff/updated`
- `turn/plan/updated`
- `item/started`
- `item/completed`
- `item/agentMessage/delta`
- `item/commandExecution/outputDelta`
- `item/fileChange/outputDelta`

Required server requests:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`

The backend must expose typed Python events to the task service. Telegram code must not parse raw app-server JSON.

## Telegram Command Surface

V1 commands:

- `/start`
- `/help`
- `/task <workspace> <prompt>`
- `/task <task_id>`
- `/tasks`
- `/status`
- `/continue <task_id> <prompt>`
- `/steer <task_id> <prompt>`
- `/tail <task_id>`
- `/events <task_id>`
- `/diff <task_id>`
- `/files <task_id>`
- `/pause <task_id>`
- `/abort <task_id>`
- `/archive <task_id>`
- `/fork <task_id> <prompt>`
- `/codex-sessions`
- `/health`

No hidden command may bypass approval or workspace locks.

## Configuration

Existing TOML config remains the source of truth, extended with:

```toml
[backend]
startup_timeout_seconds = 15
request_timeout_seconds = 60
event_log_max_chars = 20000

[approval]
callback_timeout_seconds = 3600
allow_session_approval = true
```

All app-server listeners bind to `127.0.0.1` only.

## Recovery

On startup:

1. open SQLite and run migrations
2. mark `running`, `queued`, and `waiting_approval` tasks as `paused`
3. add `recovery_paused` event to each changed task
4. keep `/tasks` and `/task <id>` browsable
5. do not auto-resume Codex

The owner must explicitly `/continue` or `/abort` recovered tasks.

## Testing Requirements

Unit tests:

- config validation
- router parsing
- auth middleware
- ledger migrations and state transitions
- status rendering
- workspace lock release/acquire
- JSON-RPC client request correlation
- backend event translation
- approval callback idempotence
- Telegram command flow with fake backend

Integration tests:

- gated by `WLCODEX_RUN_CODEX_INTEGRATION=1`
- start app-server on loopback
- create thread
- start harmless turn
- receive completion
- trigger or simulate approval path if app-server supports deterministic approval fixture

Manual smoke test:

1. run bot with a real Telegram token
2. send `/health`
3. send `/task wlcodex Reply with exactly: smoke ok`
4. observe status card updates
5. send `/tasks`
6. send `/task <id>`
7. send `/archive <id>` after completion

## Acceptance Criteria

- Unauthorized Telegram users cannot run any operational command.
- A real authorized `/task` starts real Codex work through app-server.
- The first task does not permanently lock a workspace after completion/failure/abort.
- App-server approval requests can be approved or denied from Telegram.
- `/continue` resumes only the selected task thread.
- `/steer` uses active turn steering, not historical continue.
- `/tail`, `/events`, `/diff`, and `/files` inspect local state only.
- Daemon restart preserves task history and pauses active work safely.
- Tests and lint pass.
- README documents the actual commands that work on this Linux host.
