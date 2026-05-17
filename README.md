# WLCodex

WLCodex is a personal Telegram cockpit for controlling local Linux Codex CLI tasks via `codex app-server`.

## V1 safety rules

- Private Telegram chat only
- Allowlisted Telegram user IDs only
- New tasks use fresh Codex threads by default
- History resumes only by explicit task selection
- Telegram status cards are never fed back into Codex context
- SQLite is a local ledger, not automatic model memory
- One active write task per workspace
- App-server binds to loopback only (127.0.0.1)

## Task Liveness

Paused tasks still hold the workspace write slot — they count as "active write tasks"
and block new `/task` reservations on the same workspace.  This is intentional: a
paused task has an open Codex thread that may resume.

The `TaskWatchdog` runs inside the `EventBridge` event pump (every
`watchdog_interval_seconds`, default 60 s).  It scans all active tasks and:

- Marks a task `task_timeout` when it has been stuck in RUNNING / QUEUED /
  WAITING_APPROVAL beyond its configured threshold (`max_running_seconds`,
  `max_queued_seconds`, `max_waiting_approval_seconds`).
- Marks a task `backend_dead` when the backend has been unhealthy for longer
  than `backend_dead_grace_seconds` — releasing the workspace slot.

On startup, `main.py` pauses any task that was RUNNING, QUEUED, or
WAITING_APPROVAL during the previous run, then sends recovery notifications to
the Telegram chat so the user can `/continue` or `/abort` each one.

## Local setup

```bash
# Create venv and install
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# Configure
cp config/wlcodex.example.toml config/wlcodex.toml
# Edit config/wlcodex.toml:
#   - Set allowed_user_ids to your Telegram user ID
#   - Verify workspace paths

# Set bot token
export WLCODEX_TELEGRAM_BOT_TOKEN="your-bot-token-from-botfather"

# Run tests
.venv/bin/python -m pytest -q

# Run lint
.venv/bin/python -m ruff check .

# Start bot (requires codex CLI on PATH)
.venv/bin/wlcodex --config config/wlcodex.toml

# Or with fake backend for testing
.venv/bin/wlcodex --config config/wlcodex.toml --fake-backend
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` / `/help` | Show help |
| `/health` | Backend health check |
| `/task <workspace> <prompt>` | Start a new Codex task |
| `/task <id>` | Show task details |
| `/tasks` / `/status` | List active tasks |
| `/continue <id> <prompt>` | Resume a historical task thread |
| `/steer <id> <prompt>` | Steer the active turn |
| `/tail <id>` | Show recent local log lines |
| `/events <id>` | Show SQLite event log |
| `/diff <id>` | Show recent file changes |
| `/files <id>` | Show touched file list |
| `/pause <id>` | Pause a running task |
| `/abort <id>` | Abort a running task |
| `/archive <id>` | Archive a completed task |
| `/fork <id> <prompt>` | Fork a task to a new thread |
| `/sessions` | List thread mappings |

## Manual smoke test

1. Start the bot: `.venv/bin/wlcodex --config config/wlcodex.toml`
2. In private Telegram chat with the bot:
   - Send `/health` → Should report backend status
   - Send `/task <alias> Reply with exactly: smoke ok`
   - Observe status updates
   - Send `/tasks` → Should show the task
   - Send `/task <id>` → Should show task details
   - After task completes, send `/archive <id>`

## Live Telegram Smoke (Real Acceptance)

Set the environment:

```bash
export WLCODEX_TELEGRAM_BOT_TOKEN=...
export WLCODEX_TELEGRAM_ALLOWED_USER_ID=...
export WLCODEX_RUN_TELEGRAM_LIVE=1
.venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

This preflight does not require `chat_id` or `task_id`; those only exist after
a real human-to-bot interaction. Start WLCodex with the same config. From the
authorized private Telegram chat, send:

```text
/health
/task <workspace> Reply exactly with: wlcodex telegram live ok
/tasks
/sessions
```

After the status card shows the created task id, export and run:

```bash
export WLCODEX_LIVE_SMOKE_TASK_ID=<task_id>
.venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

`WLCODEX_TELEGRAM_CHAT_ID` is optional. When unset, the smoke test accepts any
private chat created by the allowlisted Telegram user and verifies the actual
`telegram_chat_id` recorded in SQLite.

### Approval Smoke

Use a prompt that triggers a real app-server approval request:

```text
/task <workspace> Create file wlcodex_approval_probe.txt with text approval-ok. If permission is requested, wait for my Telegram approval.
```

1. Observe the Telegram approval card.
2. Click Approve once.
3. Observe task status returning to running or done.
4. Verify the file exists.

Then set:

```bash
export WLCODEX_LIVE_APPROVAL_REQUIRED=1
export WLCODEX_LIVE_SMOKE_TASK_ID=<approval_task_id>
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

## systemd

Copy `deploy/systemd/wlcodex.service.example` to a user service. It reads
the private environment file from `/home/wl/.config/wlcodex/env`:

```bash
systemctl --user daemon-reload
systemctl --user enable --now wlcodex.service
```

## Real app-server integration

Real Codex app-server integration tests require:
```bash
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
```

## Final Acceptance

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

Fake backend tests are unit-test helpers and NOT smoke evidence.
