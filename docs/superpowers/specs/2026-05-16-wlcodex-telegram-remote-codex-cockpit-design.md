# WLCodex Telegram Remote Codex Cockpit Design

## Summary

WLCodex is a personal Telegram remote cockpit for running Codex CLI on a Linux host. It lets the owner start new coding tasks, monitor progress, approve risky actions, inspect logs and diffs, continue historical sessions, and steer long-running work from a phone without standing next to the machine.

The first version is intentionally small: one trusted Telegram user, one local daemon, local SQLite state, and Codex CLI as the execution engine. Telegram is treated as a dashboard and steering layer, not as model memory.

## Core Invariants

These rules are non-negotiable:

1. Progress display must not be fed back into Codex context.
2. New tasks create fresh Codex threads by default.
3. Historical context is resumed only when the user explicitly selects a task or thread.
4. Local memory is a ledger, not model memory. It records tasks, events, approvals, files, diffs, and summaries, but it is not automatically injected into prompts.
5. Each workspace allows at most one active write task at a time.
6. Telegram must show enough progress to feel like sitting beside the terminal, while default output remains quiet and low-noise.
7. Dangerous commands, write access, network escalation, deployment, and arbitrary shell execution require an explicit approval path.

## Goals

- Provide a Telegram private-chat interface for personal remote Codex use.
- Use the already-installed Linux Codex CLI as the backend.
- Support task isolation with `task_id -> codex_thread_id` mapping.
- Support history browsing, resume, fork, archive, and recovery.
- Support low-token progress monitoring through local event summarization.
- Support approval cards for Codex permission requests.
- Keep the stack stable, lightweight, and service-friendly on Linux.
- Store all durable operational state locally.
- Make the Codex backend replaceable if app-server protocol changes.

## Non-Goals For V1

- No Discord integration.
- No multi-user collaboration.
- No multi-agent execution.
- No automatic long-term memory injection.
- No automatic production deployment.
- No arbitrary Telegram-to-shell bridge.
- No cron-based automatic code modification.
- No web dashboard.
- No cloud relay in the first version.

## User Experience

The owner talks to a Telegram bot in a private chat.

### New Work

`/task lightfee Fix the live sidecar health timeout without changing deployment scripts`

Behavior:

- Creates a new local task row.
- Creates or starts a new Codex thread for that task.
- Binds the task to a configured workspace alias, such as `lightfee`.
- Posts one editable status card.
- Runs Codex with workspace-write sandboxing and approval prompts enabled.

### Status

`/status`

Shows active and recently completed tasks:

```text
#42 lightfee running  Fix sidecar health timeout  18m
#41 lightfee done     Audit cloud2 event gaps     2h
#40 wlcodex paused    Design Telegram cockpit     yesterday
```

### Task Details

`/task 42`

Shows:

- title
- workspace alias and path
- status
- current phase
- current command, if any
- pending approval, if any
- changed file count
- latest short summary
- action buttons

Buttons:

- Continue
- Steer
- Diff
- Tail
- Files
- Pause
- Abort
- Archive
- Fork

### Continue Historical Work

`/continue 42 Use the conservative fix and keep the deployment scripts untouched`

Behavior:

- Resumes task #42 and its Codex thread.
- Sends only the user instruction to Codex.
- Does not inject previous Telegram status cards or logs.

### Fork Historical Work

`/fork 42 Try the alternative watchdog-based approach in a fresh thread`

Behavior:

- Creates a new task.
- Links it to parent task #42.
- Starts a fresh Codex thread.
- Injects only the user-provided fork instruction plus a short parent reference chosen by the user-facing command.

### Steering

`/steer 42 Stop changing config files and explain the failing test first`

Behavior:

- Sends an explicit user instruction to the running or next turn of task #42.
- Records the intervention in the local ledger.

### Inspection Commands

Inspection commands read local state and do not alter Codex context:

- `/tail 42` shows the last relevant local event/log lines.
- `/diff 42` shows compact diff stats and optionally a trimmed patch.
- `/files 42` shows touched files.
- `/test 42` shows last detected test command and result.
- `/events 42` shows recent structured task events.
- `/codex-sessions` lists known Codex threads and their mapped tasks.

## Token Control Model

The bridge separates three streams:

1. Codex context: user task prompts, explicit continue/steer messages, and Codex-managed conversation state.
2. Telegram display: rendered status cards, command results, buttons, and log excerpts.
3. Local ledger: SQLite tables and log files containing events, approvals, summaries, commands, diffs, and task metadata.

Only stream 1 costs model context tokens. Streams 2 and 3 are local or Telegram-only unless the user explicitly sends a command that resumes or steers a Codex task.

The status renderer uses local rules, not model summarization:

- last known phase
- pending approval state
- current command
- changed file count
- last assistant message excerpt
- last test result
- elapsed time

## Architecture

```text
Telegram private chat
        |
        v
python-telegram-bot handlers
        |
        v
Command router and task service
        |
        +--> SQLite task ledger
        +--> Status renderer
        +--> Workspace lock manager
        |
        v
Codex backend interface
        |
        +--> App-server backend, preferred
        +--> Exec backend, diagnostic fallback
```

### Main Components

#### Telegram Adapter

Owns Telegram-specific details:

- command handlers
- inline buttons
- callback data
- message edits
- user allowlist
- private-chat enforcement

It does not know Codex protocol details.

#### Command Router

Converts Telegram commands into typed actions:

- create task
- continue task
- steer task
- inspect task
- approve request
- deny request
- archive task
- fork task

It is deterministic and easy to test without Telegram.

#### Task Service

Owns the product rules:

- default new thread per task
- explicit resume only
- no automatic memory injection
- task status transitions
- workspace write locking
- ledger writes

#### Codex Backend

An interface around Codex CLI so the rest of the app is insulated from protocol changes.

Preferred backend:

- starts `codex app-server --listen ws://127.0.0.1:<port>`
- connects via WebSocket
- manages threads, turns, approval events, and streamed task events

Fallback backend:

- runs `codex exec` for diagnostic smoke tests and basic one-shot tasks
- does not claim full remote approval support

#### Ledger

SQLite stores durable state:

- tasks
- workspaces
- codex threads
- task events
- approval requests
- Telegram messages
- workspace locks

Large logs can be stored as files under `runtime/tasks/<task_id>/` and referenced from SQLite.

## Tech Stack

The first implementation should favor boring tools:

- Python 3.12+
- `python-telegram-bot` for Telegram Bot API
- `websockets` for Codex app-server WebSocket transport
- SQLite through Python stdlib `sqlite3`
- `pytest` for tests
- `ruff` for formatting and linting
- `systemd` for 7x24 Linux service management

Why Python:

- already available on the host
- excellent stdlib support for SQLite, subprocesses, signals, and logging
- mature Telegram libraries
- simple long-running daemon deployment
- low operational complexity compared with a browser or VS Code extension bridge

## Configuration

Configuration is read from a local TOML file:

`config/wlcodex.toml`

Example:

```toml
[telegram]
bot_token_env = "WLCODEX_TELEGRAM_BOT_TOKEN"
allowed_user_ids = [123456789]
private_chat_only = true

[codex]
binary = "codex"
app_server_host = "127.0.0.1"
app_server_port = 17431
approval_policy = "on-request"
sandbox = "workspace-write"

[storage]
sqlite_path = "runtime/wlcodex.sqlite3"
task_log_dir = "runtime/tasks"

[display]
status_update_min_interval_seconds = 2
tail_lines = 40
diff_max_chars = 3500

[[workspaces]]
alias = "wlcodex"
path = "/media/wl/新加卷/codex/wlcodex"
allow_write = true

[[workspaces]]
alias = "lightfee"
path = "/media/wl/新加卷/codex/LightFeeV2"
allow_write = true
```

Secrets are never stored in the TOML file. The bot token is read from the named environment variable.

## Data Model

### Task

Fields:

- `id`
- `workspace_alias`
- `workspace_path`
- `title`
- `status`: `queued`, `running`, `waiting_approval`, `paused`, `done`, `failed`, `aborted`, `archived`
- `codex_thread_id`
- `parent_task_id`
- `created_at`
- `updated_at`
- `last_summary`
- `last_phase`
- `last_error`

### Event

Fields:

- `id`
- `task_id`
- `event_type`
- `payload_json`
- `created_at`

Events include:

- task_created
- turn_started
- codex_message
- command_started
- command_finished
- approval_requested
- approval_approved
- approval_denied
- files_changed
- test_detected
- task_completed
- task_failed
- user_steer

### Approval Request

Fields:

- `id`
- `task_id`
- `codex_request_id`
- `kind`
- `summary`
- `command`
- `status`: `pending`, `approved`, `denied`, `expired`
- `created_at`
- `resolved_at`

## Safety Model

V1 security defaults:

- Telegram private chat only.
- Explicit `allowed_user_ids`.
- No group chats.
- No public HTTP listener.
- Codex app-server listens on loopback only.
- Workspace aliases must be configured explicitly.
- New workspaces cannot be added from Telegram.
- One active write task per workspace.
- No `/sh` command.
- No automatic deploy command.
- Approval buttons expire after a configurable timeout.
- All approvals are logged.

## Error Handling

The daemon should degrade visibly:

- If Telegram is reachable but Codex is down, `/status` still works and active tasks are marked `failed` or `paused`.
- If app-server startup fails, the user sees a clear backend error.
- If a Telegram status edit fails, the daemon sends a new message and updates the message mapping.
- If the daemon restarts, it reloads active tasks from SQLite and marks previously running tasks as `paused_recovery`.
- If an approval arrives after expiry, the user sees that it expired and no action is sent to Codex.

## Observability

Local logs:

- `runtime/wlcodex.log`
- `runtime/tasks/<task_id>/events.jsonl`
- `runtime/tasks/<task_id>/codex.log`

Telegram observability:

- `/status`
- `/health`
- `/events <task_id>`
- `/tail <task_id>`

The bot should never stream unbounded logs into Telegram.

## Version Roadmap

### V1: Remote Cockpit Core

- Telegram private bot
- task ledger
- workspace config
- new task / continue / steer
- status cards
- local logs
- Codex app-server spike and backend wrapper
- history listing
- workspace lock

### V1.5: Watchdog And Recovery

- stuck-task reminders
- daemon restart recovery
- app-server reconnect
- status heartbeat

### V2: Fixed Skills

- `/health <workspace>`
- `/audit <workspace>`
- `/review-diff <task_id>`
- `/deploy-check <workspace>`

### V3: Controlled Summaries

- manual task summary reuse
- explicit `/use-summary <task_id>` flow
- no automatic memory injection

### V4: Parallelism

- read-only background inspectors
- worktree-backed write tasks
- strict conflict prevention

## Acceptance Criteria For V1

- The owner can start a new task from Telegram.
- A new task defaults to a new Codex thread.
- The owner can view task history.
- The owner can continue a selected historical task.
- Telegram status updates do not become Codex prompt input.
- The owner can inspect logs and diffs on demand.
- The daemon stores task state in SQLite.
- The daemon can restart without losing task history.
- Only the configured Telegram user can control the bot.
- Two write tasks cannot run in the same workspace at the same time.
- The Codex backend is isolated behind an interface.

