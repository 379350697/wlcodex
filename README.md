# WLCodex

WLCodex is a conversation-first chief-engineer Telegram cockpit. The user talks
naturally. WLCodex routes intent to Codex (analysis, architecture, verification),
Claude Code (implementation), or a Codex-led orchestration loop.

## V2 — Conversation-First

**Default UX**: Send a plain text message. WLCodex starts a conversation and
routes it to Codex for analysis, not task-ID operations.

**Direct channels**:
- `/codex <prompt>` — talk directly to Codex
- `/claude <prompt>` — talk directly to Claude Code (when enabled)
- `/auto <prompt>` — full Codex → Claude → Codex orchestration loop

**Compact context**: Model prompts use compact context packets, never raw
Telegram transcripts. Token budget is a hard architectural constraint.

**Legacy commands** (advanced/diagnostic): `/task`, `/continue`, `/steer`,
`/tail`, `/events`, `/diff`, `/files`, `/pause`, `/abort`, `/archive`, `/fork`

**Menu commands**: `/new`, `/stop`, `/status`, `/sessions`, `/switch`, `/model`,
`/verify`, `/health`, `/help`

## Interaction profiles

WLCodex separates runtime orchestration from Telegram presentation.

```toml
[interaction]
profile = "natural"
streaming_enabled = true
show_footer = false
edit_min_interval_seconds = 1.0
```

`natural` is the default chat surface: plain text starts or continues a
conversation, Telegram shows typing while work starts, and model deltas stream
into one edited message. This does not double model tokens because it forwards
deltas from the same Codex/Claude run.

`legacy` preserves task-card style behavior for operator workflows.

`cockpit` is reserved for a future richer remote-control profile.

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

Primary conversation UX:

| Command | Description |
|---------|-------------|
| `/start` / `/help` | Show help |
| `/health` | Backend health check |
| plain text | Continue the active conversation; default mode is chief-engineer orchestration |
| `/new [title]` | Start a fresh conversation |
| `/codex <prompt>` | Send one direct analysis turn to Codex |
| `/claude <prompt>` | Send one direct implementation turn to Claude Code |
| `/auto <prompt>` | Run Codex analysis → Claude implementation → Codex verification |
| `/status` | Show active conversation/task status |
| `/sessions` | List conversations/thread mappings |
| `/switch <workspace>` | Switch the active conversation workspace |
| `/model [name]` | Show or set the preferred model |
| `/verify` | Ask Codex to verify the latest implementation evidence |
| `/stop` | Stop the current active conversation task |

### Dual Surface Modes

WLCodex supports two independent surfaces over the same conversation:
**Product mode** (default) and **Terminal mode** (raw remote control).

| Command | Description |
|---------|-------------|
| `/product` | Switch to product mode (phone-friendly event UX) |
| `/terminal` | Switch to terminal mode (default agent: claude) |
| `/terminal claude` | Switch to terminal mode with Claude agent |
| `/terminal codex` | Switch to terminal mode with Codex agent |
| `/terminal agent claude` | Switch to terminal mode with Claude agent (explicit) |
| `/terminal agent codex` | Switch to terminal mode with Codex agent (explicit) |
| `/terminal tail` | Resume terminal push / show latest output |
| `/terminal pause` | Pause terminal push, keep session alive |
| `/terminal detach` | Stop terminal push, keep session alive |
| `/terminal product` | Switch back to product mode |
| `/mode` | Show current surface mode |

- **Product mode** is the default. It renders structured event cards for a
  phone-friendly experience.
- **Terminal mode** is a raw control surface that may produce large output.
  It is disabled by default (`terminal.enabled = false`) until live smoke passes.
- Mode switches do not create new conversations. The conversation ID stays
  the same across `/product` and `/terminal`.
- Terminal detach stops Telegram output delivery but does not abort the
  underlying agent session.

Legacy task commands remain available for diagnostics and low-level app-server
acceptance:

| Command | Description |
|---------|-------------|
| `/task <workspace> <prompt>` | Start a raw Codex task |
| `/task <id>` | Show raw task details |
| `/tasks` | List active raw tasks |
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

## Manual smoke test

1. Start the bot: `.venv/bin/wlcodex --config config/wlcodex.toml`
2. In private Telegram chat with the bot:
   - Send `/health` → Should report backend status
   - Send `/new 真人 smoke` → Should create a fresh conversation
   - Send `请用中文只回复：wlcodex telegram live ok`
   - Observe a conversation-first response, not a raw `/task` card
   - Send `/status` → Should show the active conversation status
   - Send `/sessions` → Should list the conversation/session

Use `/codex <prompt>`, `/claude <prompt>`, or `/auto <prompt>` only when you
want to force a specific route. Plain text is the default product path.

### Human smoke pass criteria

Use this as the real product acceptance smoke for the current `natural`
interaction profile:

1. Config has `[interaction] profile = "natural"` and `streaming_enabled = true`.
2. A plain text message starts or continues a conversation without a mechanical
   textual ACK such as "正在处理你的消息，请稍候".
3. Telegram shows typing while the run is being prepared.
4. Visible model output streams into one edited message instead of a sequence of
   duplicate status cards.
5. The normal reply body does not expose task id, thread id, workspace, mode, or
   token counters. Those details remain available through `/status` and
   diagnostic commands.
6. Completion shows a compact action row. `查看 diff` appears when the workspace
   really has a git diff, including changes made by Claude Code.
7. `/codex <prompt>` streams Codex deltas from the same Codex run. It must not
   start a second model call just to render Telegram output.
8. `/claude <prompt>` streams Claude output when Claude is enabled. If Claude
   returns a streaming error, the Telegram run fails visibly and the agent run is
   recorded as `failed`, not left `queued`.
9. `/auto <prompt>` runs the full Codex analysis → Claude implementation →
   Codex verification chain. A passing run records analysis, implementation,
   verification, orchestration status, decision, active Claude run, and
   conversation summary in SQLite.
10. If Claude fails during `/auto`, the orchestration stops as `failed`; Codex
    verification must not continue after the Claude stream error.
11. Approval requests still appear as explicit approval cards with buttons, not
    as mixed natural-chat text.

The human smoke is considered passed only when the visible Telegram behavior and
the ledger state both match the criteria above. A green unit test run alone is
not enough evidence for this smoke.

## Live Telegram Smoke (Real Acceptance)

The primary human smoke is conversation-first, as above. The automated live
pytest gate below still validates the lower-level legacy `/task` path because it
asserts a real Codex app-server UUID in the SQLite `tasks` ledger. Treat it as a
diagnostic app-server evidence gate, not the default user journey.

Set the environment:

```bash
export WLCODEX_TELEGRAM_BOT_TOKEN=...
export WLCODEX_TELEGRAM_ALLOWED_USER_ID=...
export WLCODEX_RUN_TELEGRAM_LIVE=1
.venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

This preflight does not require `chat_id` or `task_id`; those only exist after
a real legacy `/task` interaction. Start WLCodex with the same config. From the
authorized private Telegram chat, send this diagnostic sequence:

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

Approval smoke also uses legacy `/task`, because approval rows are tied to raw
Codex task/app-server request evidence:

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
