# WLCodex Final Real Closure Drift Repair Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

> Superseded: the 2026-05-20 Remote Workbench repair replaces this task-led
> product model. References to `/task`, `/continue`, `/steer`, task queues,
> task ids, and user-managed session/thread ids are legacy diagnostics only and
> must not be used for Cockpit, Onsite, `/status`, `/help`, `/sessions`, or
> ordinary Telegram text.

## Summary

This is the final V1 repair spec for WLCodex after review found protocol drift and incomplete business closure. The target is not a fake-backend demonstration. The target is a personal Telegram cockpit that can drive the real local Linux Codex CLI app-server end to end: start real Codex work, monitor progress remotely, approve real app-server requests from Telegram, inspect local artifacts without adding Codex context, continue historical sessions explicitly, and survive daemon restarts.

Fake backends may remain as unit-test helpers, but they are not smoke tests and are not acceptance evidence. V1 is accepted only when the real app-server path and the real Telegram command path are both proven.

## Non-Negotiable Invariants

1. No fake smoke is allowed. A fake backend may test pure Python logic only.
2. Real Codex execution uses `codex app-server` on loopback only.
3. Telegram status, logs, diffs, SQLite summaries, and local history are never injected into Codex automatically.
4. `/task` creates a fresh Codex thread by default.
5. Historical context is used only by explicit `/continue` or `/fork`.
6. SQLite is a local ledger, not memory.
7. One workspace can have only one active write task at a time.
8. `allow_write = false` workspaces refuse `/task`, `/continue`, `/steer`, `/fork`, and approval actions that would write.
9. Every Telegram handler is allowlist and private-chat guarded.
10. Visible help must match real registered Telegram commands.
11. App-server protocol payloads must match the checked-in generated schemas.
12. If app-server is down, the bot stays alive, `/health` reports the failure, and `/task` fails cleanly without pretending work started.

## Final Command Surface

Telegram command names must be commands Telegram can actually route. The canonical V1 commands are:

- `/start`
- `/help`
- `/health`
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
- `/sessions`

`/sessions` replaces the old documented `/codex-sessions` because Telegram command routing does not support hyphenated command names reliably. The router may accept `/codex-sessions` as a text alias in tests, but help and `CommandHandler` registration must advertise `/sessions`.

## Real Closure Definition

WLCodex V1 is complete only when all of these work against real runtime components:

- `wlcodex` starts, starts or reuses a loopback `codex app-server`, and polls Telegram.
- `/health` reports process state, WebSocket state, request timeout state, DB state, and Telegram readiness.
- Authorized private `/task <workspace> <prompt>` creates a real app-server thread and starts a real turn.
- Unauthorized users and group chats cannot run operational commands.
- Real app-server notifications update SQLite task state and Telegram status cards.
- Real app-server approval server requests create Telegram approval buttons.
- Telegram approval buttons send the correct JSON-RPC response to the held app-server request.
- `/continue` resumes the same app-server thread and sends only the new prompt.
- `/steer` sends same-turn steering only when an active turn exists.
- `/tail`, `/events`, `/diff`, and `/files` inspect local state without backend calls.
- `/pause`, `/abort`, `/archive`, and `/fork` keep SQLite, app-server, and workspace locks consistent.
- Restart recovery pauses previously active tasks and keeps `/sessions` and inspection commands useful.
- A final real smoke report records exact commands, environment gates, observed Telegram transcript, and pass/fail evidence.

## App-Server Lifecycle

`main.py` owns the app-server lifecycle in real mode.

Startup sequence:

1. Load config and open SQLite.
2. Run idempotent migrations.
3. Pause stale active tasks from a previous daemon.
4. Create `AppServerProcess` from configured Codex binary, host, port, and startup timeout.
5. Try to connect to an already-running loopback app-server.
6. If connection fails, start `codex app-server --listen ws://127.0.0.1:<port>`.
7. Poll WebSocket readiness until a real connect succeeds or timeout expires.
8. Build `AppServerCodexBackend` with the process manager and request timeout.
9. Start the event pump.
10. Start Telegram polling.

Shutdown sequence:

1. Stop Telegram polling.
2. Stop event pump.
3. Close WebSocket and cancel pending JSON-RPC requests.
4. Stop the managed app-server process only if WLCodex started it.

Health is healthy only when:

- process manager says alive, or an external loopback process was successfully reused
- WebSocket connection is open
- last successful app-server request is recent or no request has been made
- SQLite responds to a simple query

## Protocol Contract

The backend must be schema-driven, not guessed.

Required request shapes:

- `thread/start`: `{ "cwd": path, "approvalPolicy": configured_policy, "sandbox": configured_sandbox }`
- `turn/start`: `{ "threadId": thread_id, "input": [{ "type": "text", "text": prompt }] }`
- `thread/resume`: `{ "threadId": thread_id }`
- `turn/steer`: `{ "threadId": thread_id, "expectedTurnId": active_turn_id, "input": [{ "type": "text", "text": prompt }] }`
- `turn/interrupt`: `{ "threadId": thread_id, "turnId": active_turn_id }`
- `thread/fork`: only if the real schema supports it; otherwise V1 `/fork` creates a fresh `thread/start` and links `parent_task_id`.

Required response parsing:

- `thread/start` reads `result.thread.id`.
- `turn/start` reads `result.turn.id`.
- `thread/resume` reads `result.thread.id` when present and preserves the existing thread id otherwise.
- `turn/started` reads `params.threadId` and `params.turn.id`.
- `turn/completed` reads `params.threadId` and `params.turn.id`.
- All event ingestion accepts nested `turn.id` and does not rely on a top-level `turnId`.

`JsonRpcClient.request()` must have a timeout. A timed-out request fails the local task cleanly and does not hang the Telegram handler forever.

Server-request handling must not block the WebSocket receive loop. Approval requests are held by JSON-RPC id, emitted as typed backend events, and answered later by Telegram callbacks.

## Task Start Flow

`/task <workspace> <prompt>`:

1. Validate auth.
2. Parse command.
3. Load workspace.
4. Enforce `allow_write`.
5. Enforce one active write task per workspace.
6. Create a local queued task reservation with no thread id yet.
7. Ensure app-server readiness.
8. Call `thread/start`.
9. Persist `codex_thread_id`.
10. Call `turn/start`.
11. Persist returned or notified turn id.
12. Send Telegram status card and store its message id.

If app-server fails after the local task reservation, the task becomes `failed` with `last_error`. This avoids orphan remote work and leaves an audit trail.

## Continue, Steer, Fork

`/continue <task_id> <prompt>`:

- Allowed from `done`, `failed`, `paused`, and `aborted`.
- Refuses `queued`, `running`, `waiting_approval`, and `archived`.
- Requires existing `codex_thread_id`.
- Requires workspace available and writable.
- Calls `thread/resume`, then `turn/start` with only the new prompt.
- Moves task to `queued` immediately and to `running` on `turn/started`.

`/steer <task_id> <prompt>`:

- Allowed only for `running` or `waiting_approval` tasks with an active turn id.
- Requires workspace writable.
- Calls `turn/steer` with `expectedTurnId`.
- Does not create a new turn and does not append local status text.

`/fork <task_id> <prompt>`:

- Requires source task with a thread id.
- Requires workspace available and writable.
- Creates a new task with `parent_task_id`.
- Starts a fresh thread unless real `thread/fork` is confirmed by schema and real smoke.
- Sends only the fork prompt plus a short explicit parent reference visible to the user.

## Approval Flow

Supported app-server request types:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`

For command and file approvals:

- Approve once returns `{ "decision": "accept" }`.
- Approve session returns `{ "decision": "acceptForSession" }` only when `allow_session_approval = true`.
- Deny returns `{ "decision": "decline" }`.
- Cancel returns `{ "decision": "cancel" }`.

For permissions approvals:

- Approve once returns `{ "permissions": requested_permissions, "scope": "turn" }`.
- Approve session returns `{ "permissions": requested_permissions, "scope": "session" }` only when session approvals are allowed.
- Deny returns `{ "permissions": {}, "scope": "turn" }`.
- Cancel returns `{ "permissions": {}, "scope": "turn" }` and interrupts the active turn when an active turn id is known.

Approval callback sequence:

1. Validate Telegram user and private chat.
2. Load approval row.
3. Reject expired, duplicate, or non-pending rows without sending backend responses.
4. Build the schema-correct JSON-RPC response from stored approval kind and payload.
5. Send backend response.
6. Only after successful backend send, mark approval resolved.
7. Decrement `pending_approval_count`.
8. If no pending approvals remain and the task is still active, move `waiting_approval -> running`.
9. Edit approval message and task status card.

If backend delivery fails, the approval stays pending, `last_error` is recorded, and the user can retry.

## Local Monitoring

Remote monitoring must be useful while staying token-cheap.

Local data sinks:

- SQLite `task_events` stores structured event metadata.
- Per-task log files store bounded command output and assistant/status deltas.
- `touched_files` stores unique changed paths.
- Latest app-server diff payload is stored as a `diff_updated` event when available.

Inspection commands:

- `/events` reads SQLite events.
- `/tail` reads the per-task log file; if no file exists, it falls back to recent `command_output` and `agent_message_delta` events.
- `/diff` first reads latest stored diff event, then falls back to `git diff --stat` in the task workspace.
- `/files` reads touched files.

None of these commands call Codex or become prompt context.

## State Machine

Task statuses:

- `queued`: local task reserved or new turn requested
- `running`: active Codex turn running
- `waiting_approval`: at least one pending approval exists
- `paused`: user or startup recovery paused local tracking
- `done`: last turn completed with no pending approvals
- `failed`: local backend/controller failure
- `aborted`: user interrupted or cancelled active work
- `archived`: hidden read-only history

Allowed transitions:

- `queued -> running`
- `queued -> failed`
- `queued -> aborted`
- `running -> waiting_approval`
- `running -> done`
- `running -> failed`
- `running -> paused`
- `running -> aborted`
- `waiting_approval -> running`
- `waiting_approval -> done`
- `waiting_approval -> failed`
- `waiting_approval -> paused`
- `waiting_approval -> aborted`
- `done -> queued`
- `done -> archived`
- `failed -> queued`
- `failed -> archived`
- `paused -> queued`
- `paused -> archived`
- `aborted -> queued`
- `aborted -> archived`

Workspace locks are held for `queued`, `running`, `waiting_approval`, and `paused`. Locks are released for `done`, `failed`, `aborted`, and `archived`.

## SQLite Migration Requirements

Migrations must support existing skeleton databases. `CREATE TABLE IF NOT EXISTS` is not enough.

Required migration behavior:

- Add missing task columns with guarded `ALTER TABLE`.
- Add missing tables idempotently.
- Add unique indexes idempotently.
- Never crash when a previous local DB lacks new columns.
- Store a schema version in `schema_meta`.

## Real Acceptance Strategy

Unit tests are required but not sufficient.

Allowed test categories:

- Unit tests with fake transports for pure logic.
- Contract tests that compare backend payload builders against `runtime/protocol` schemas.
- Real app-server integration tests.
- Real Telegram live smoke.

Disallowed acceptance evidence:

- Fake backend E2E as smoke.
- A report claiming completion while the real app-server test is skipped, hung, or blocked.
- A report that says Telegram is untested.

Required final acceptance commands:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

The live Telegram smoke may require env vars for bot token, authorized user id, and chat id. If the live Telegram test cannot be automated on the host, a manual smoke report must include the exact Telegram messages, timestamps, task ids, app-server logs, and SQLite rows proving the same flow. A manual report is acceptable only for Telegram delivery, not for app-server protocol.

## Final Acceptance Checklist

- [ ] `/health` is healthy with a real managed or reused app-server.
- [ ] `/task` starts a real Codex thread and real turn.
- [ ] Real turn events update SQLite and Telegram status.
- [ ] Real approval request reaches Telegram and callback resolves app-server.
- [ ] `/continue` resumes the exact historical thread without local memory injection.
- [ ] `/steer` steers only active turns.
- [ ] `/tail`, `/events`, `/diff`, `/files`, and `/sessions` show useful local state.
- [ ] Restart recovery pauses active tasks and keeps history browsable.
- [ ] No advertised command is unregistered or fake-only.
- [ ] Final report contains no fake smoke as acceptance evidence.
