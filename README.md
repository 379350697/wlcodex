# WLCodex

WLCodex is a remote workbench for phone-driven software engineering. One local
machine runs the work. The phone shows two views of the same live workbench:
**Cockpit** (驾驶舱) for concise progress and decisions, and **Onsite** (现场)
for raw terminal-style live control.

## Remote Workbench

**Plain text default**: Codex read-only analysis.

Send a plain text message for read-only Codex analysis, answers, review, and
planning. Use `/auto` only when you explicitly want the full Codex analysis →
Claude implementation → Codex verification workflow. The Cockpit shows progress,
asks for decisions, and summarizes results. The Onsite shows live agent output
when you need raw visibility.

### Product terminology

| Internal concept | User-facing (Chinese) | User-facing (English) |
|---|---|---|
| Workbench | 工作台 | Remote Workbench |
| Cockpit view | 驾驶舱 | Cockpit |
| Onsite view | 现场 | Onsite / Live Worksite |
| Auto orchestrated | /auto：Codex -> Claude -> Codex | Explicit engineer workflow |
| Codex-only mode | 只问 Codex | Ask Codex only |
| Claude-only mode | 只叫 Claude | Ask Claude only |
| Open Onsite | 接管现场 | Open live worksite |
| Leave Onsite | 回驾驶舱 | Return to Cockpit |
| Codex verify after Claude | 让 Codex 验收 | Let Codex verify |

**Execution modes** (who does the work — separate from which view you use):

| Mode | Trigger | Behavior |
|------|---------|----------|
| Plain text | no slash command | Codex read-only analysis |
| Auto orchestrated | `/auto <prompt>` | Codex → Claude → Codex |
| Codex-only | `/codex <prompt>` | Codex only, no Claude |
| Claude-only | `/claude <prompt>` | Claude only, no automatic Codex analysis or verification |

Codex-only sends the prompt directly to Codex without Claude or /auto gates.
Claude-only is for direct hands-on implementation; Cockpit will offer a
"让 Codex 验收" action after Claude-only work completes.

After any direct run finishes, the next plain text message returns to read-only
Codex analysis. `/codex` and `/claude` remain direct single-agent paths; only
`/auto` starts the full orchestrated workflow.

`/new` is the Workbench session boundary. Until the next `/new`, Codex turns
reuse the same Codex thread and Claude Code turns resume the same Claude session
when the backend exposes a session id. This lets `/auto` execute from the
context established by earlier plain-text analysis in the same Workbench.

**Views** (how you see and steer the work):

| View | Purpose | Shows | Hides |
|------|---------|-------|-------|
| Cockpit 驾驶舱 | Concise progress and decisions | Workbench title, execution mode, phase, agent, progress, approvals, result summary | Raw JSON, session IDs, long stdout, full diffs by default |
| Onsite 现场 | Raw live worksite control | Live agent output, recent frames, tail controls, direct input | Cockpit summaries |

Both views share the same workbench. Switching views does not restart work.
Leaving the Onsite does not stop the underlying agent.

## Interaction

```toml
[interaction]
profile = "natural"
streaming_enabled = true
show_footer = false
edit_min_interval_seconds = 1.0
```

`natural` is the recommended interaction style for the Cockpit view: plain text
starts or continues a conversation, Telegram shows typing while work starts, and
model deltas stream into one edited message. This does not double model tokens
because it forwards deltas from the same Codex/Claude run.

`legacy` uses task-card style rendering for operator workflows.

`cockpit` is reserved for richer Cockpit view rendering; currently behaves like
`legacy`.

## Safety rules

- Private Telegram chat only
- Allowlisted Telegram user IDs only
- New work uses fresh Codex threads by default
- History resumes only by explicit selection
- Telegram status cards are never fed back into Codex context
- SQLite is a local ledger, not automatic model memory
- One active write task per workspace
- App-server binds to loopback only (127.0.0.1)
- Onsite frames are redacted before Telegram delivery (tokens, keys, secrets)

## Task Liveness

Paused tasks still hold the workspace write slot — they count as "active write tasks"
and block new reservations on the same workspace. This is intentional: a
paused task has an open Codex thread that may resume.

The `TaskWatchdog` runs inside the `EventBridge` event pump (every
`watchdog_interval_seconds`, default 60 s). It scans all active tasks and:

- Marks a task `task_timeout` when it has been stuck in RUNNING / QUEUED /
  WAITING_APPROVAL beyond its configured threshold (`max_running_seconds`,
  `max_queued_seconds`, `max_waiting_approval_seconds`).
- Marks a task `backend_dead` when the backend has been unhealthy for longer
  than `backend_dead_grace_seconds` — releasing the workspace slot.

On startup, `main.py` pauses any task that was RUNNING, QUEUED, or
WAITING_APPROVAL during the previous run, then sends recovery notifications to
the Telegram chat so the user can continue or abort each one.

## Recovery

On daemon restart, WLCodex replays runtime events, rebuilds the active workbench
state, and restores Cockpit and Onsite cursors. Live sessions are reattached when
the transport supports it. Missing local processes are marked as orphaned.
Cockpit remains usable even if Onsite reattach fails.

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

# Run fast default tests
.venv/bin/python -m pytest -q

# Profile the full test suite with native pytest output
bash scripts/pytest-profile 50

# Run all tests, including marked slow/integration/live tests
.venv/bin/python -m pytest -m "" -q

# Run lint
.venv/bin/python -m ruff check .

# Start bot (requires codex CLI on PATH)
.venv/bin/wlcodex --config config/wlcodex.toml

# Or with fake backend for testing
.venv/bin/wlcodex --config config/wlcodex.toml --fake-backend
```

## Telegram Commands

### Daily menu

| Command | Label | Purpose |
|---------|-------|---------|
| `/new` | 新工作台 | Start a fresh workbench |
| `/status` | 状态 | Show active workbench |
| `/terminal` | 接管现场 | Open Onsite live worksite |
| `/diff` | 变更 | Inspect file changes |
| `/settings` | 设置 | Route, model, permissions, workspace |
| `/help` | 帮助 | Compact guide |

### Primary commands

| Command | Description |
|---------|-------------|
| `/start` / `/help` | Show help |
| `/health` | Backend health check |
| plain text | Continue the active workbench with read-only Codex analysis |
| `/new [title]` | Start a fresh workbench |
| `/codex <prompt>` | Codex-only direct work |
| `/claude <prompt>` | Claude-only direct implementation |
| `/auto <prompt>` | Full Codex → Claude → Codex orchestration |
| `/status` | Show active workbench status |
| `/switch <workspace>` | Switch the active workspace |
| `/model [name]` | Show or set the preferred model |
| `/verify` | Ask Codex to verify the latest implementation evidence |
| `/sessions` | List historical agent sessions for the active workbench |
| `/stop` | Stop the current active conversation |

### View switching

| Command | Description |
|---------|-------------|
| `/terminal` | Open Onsite view; auto-selects the active agent when a session exists |
| `/terminal claude` | Open Onsite with Claude agent |
| `/terminal codex` | Open Onsite with Codex agent |
| `/terminal tail` | Resume Onsite push / show latest output |
| `/terminal pause` | Pause Onsite push, keep session alive |
| `/terminal detach` | Leave Onsite view, keep session alive |
| `/product` | Return to Cockpit view |
| `/mode` | Show current view mode |

- Cockpit (驾驶舱) is the default view. It renders structured status and
  approval cards for a phone-friendly experience.
- Onsite (现场) opens a raw live worksite view. Text sent in Onsite routes
  directly to the selected agent.
- View switches do not create new conversations. The workbench stays the same
  across Cockpit and Onsite.
- Leaving the Onsite stops Telegram output delivery but does not abort the
  underlying agent session.
- Opening the Onsite when no session exists shows a start card with options to
  start a session or return to Cockpit.
- Onsite availability depends on operator configuration.

### Legacy diagnostics

Legacy commands remain available only for diagnostics and low-level inspection.
They are hidden from the daily menu and are not part of the normal Workbench
journey:

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
| `/claude_mode` | Set Claude permission mode |

## Manual smoke test

1. Start the bot: `.venv/bin/wlcodex --config config/wlcodex.toml`
2. In private Telegram chat with the bot:
   - Send `/health` → Should report backend status
   - Send `/new 真人 smoke` → Should create a fresh workbench
   - Send `请用中文只回复：wlcodex telegram live ok`
   - Observe a Workbench-first response, not a raw diagnostic card
   - Send `/status` → Should show the active workbench status
   - Send `/sessions` → Should list historical agent sessions for the workbench

Use `/codex <prompt>`, `/claude <prompt>`, or `/auto <prompt>` only when you
want to force a specific execution mode. Plain text uses read-only Codex
analysis; only `/auto` enters the orchestrated workflow.

### Human smoke pass criteria

Use this as the real product acceptance smoke for the current `natural`
interaction profile:

1. Config has `[interaction] profile = "natural"` and `streaming_enabled = true`.
2. A plain text message starts or continues a workbench without a mechanical
   textual ACK such as "正在处理你的消息，请稍候".
3. Telegram shows typing while the run is being prepared.
4. Visible model output streams into one edited message instead of a sequence of
   duplicate status cards.
5. The normal reply body does not expose task ids, thread ids, session IDs,
   workspace locks, queue positions, or token counters. Operator details remain
   limited to explicit diagnostic commands.
6. Completion shows a compact action row. `查看 diff` appears when the workspace
   really has a git diff, including changes made by Claude Code.
7. `/codex <prompt>` runs Codex-only and does not call Claude.
8. `/claude <prompt>` runs Claude-only and does not trigger automatic Codex
   analysis or verification. Completion should offer a "让 Codex 验收" action.
9. `/auto <prompt>` runs the full Codex → Claude → Codex chain. A passing run
   records all phases in SQLite.
10. If Claude fails during `/auto`, the orchestration stops as `failed`; Codex
    verification must not continue after the Claude stream error.
11. Approval requests appear as explicit approval cards with buttons, not as
    mixed natural-chat text.
12. `/terminal` with no active session shows a start card (options to start a
    session or return to Cockpit), never a dead-end error.
13. Text sent in Onsite view routes to the live agent session, not the Cockpit
    controller.
14. Switching between Cockpit and Onsite preserves the workbench — work does not
    restart and the active run is not lost.
15. `/help` uses "驾驶舱", "接管现场", and "/auto：Codex -> Claude -> Codex"; it
    does not expose configuration keys like `terminal.enabled`.

The human smoke is considered passed only when the visible Telegram behavior and
the ledger state both match the criteria above. A green unit test run alone is
not enough evidence for this smoke.

## Live Telegram Smoke (Real Acceptance)

The primary human smoke is Workbench-first, as above. The automated live pytest
gate should be treated as a product evidence gate when it exercises `/new`,
plain text, `/terminal`, `/product`, `/claude`, the "让 Codex 验收" action,
`/codex`, and `/sessions` without exposing internal ids.

Set the environment:

```bash
export WLCODEX_TELEGRAM_BOT_TOKEN=...
export WLCODEX_TELEGRAM_ALLOWED_USER_ID=...
export WLCODEX_RUN_TELEGRAM_LIVE=1
.venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

This preflight does not require `chat_id` or diagnostic ids. Start WLCodex with
the same config. From the authorized private Telegram chat, send this product
sequence:

```text
/health
/new 真人历史现场 smoke
请用中文只回复：wlcodex telegram live ok
/terminal
/product
/claude Reply exactly with: claude only ok
点击：让 Codex 验收
/codex Reply exactly with: codex only ok
/sessions
```

Then run:

```bash
.venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

`WLCODEX_TELEGRAM_CHAT_ID` is optional. When unset, the smoke test accepts any
private chat created by the allowlisted Telegram user and verifies the actual
`telegram_chat_id` recorded in SQLite.

### Legacy Diagnostic Smoke

Legacy `/task` smoke is an operator diagnostic for the app-server ledger. It is
not the default user journey and must not be used as product release evidence.
It is intentionally manual; the automated live smoke follows Workbench runtime
events and agent session refs instead of legacy task ids.

```text
/task <workspace> Create file wlcodex_approval_probe.txt with text approval-ok. If permission is requested, wait for my Telegram approval.
```

1. Observe the Telegram approval card.
2. Click Approve once.
3. Observe task status returning to running or done.
4. Verify the file exists.

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
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest -m "" tests/test_real_app_server_integration.py -q
```

## Final Acceptance

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -m "" -q
.venv/bin/python -m ruff check .
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest -m "" tests/test_real_app_server_integration.py -q
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest -m "" tests/test_live_telegram_smoke.py -q
```

Fake backend tests are unit-test helpers and NOT smoke evidence.
