# WLCodex Final Real Closure Smoke Report

## Environment

- Date: 2026-05-16
- Codex CLI version: 0.121.0
- OS: Linux 6.17.0-23-generic (Ubuntu 24.4.0 x86_64)
- Python: 3.12.3
- App-server endpoint: ws://127.0.0.1:17432 (loopback, test port)
- Telegram bot mode: gated behind WLCODEX_RUN_TELEGRAM_LIVE=1

## Commands Run

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -q
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

## Unit + Contract Suite

**Result: 189 passed, 5 skipped**

The 5 skipped tests are the gated real app-server integration test (1 test) and the gated live Telegram smoke tests (3 tests, 1 skip inside).
All unit, contract, drift repair, approval consistency, and fake-backend E2E tests pass.

## Lint

**Result: All checks passed!**

## Real App-Server Evidence

The real app-server integration test **PASSED** against Codex CLI 0.121.0:

```bash
WLCODEX_RUN_CODEX_INTEGRATION=1 .venv/bin/python -m pytest tests/test_real_app_server_integration.py -v
# 1 passed in 18.53s
```

Observed flow:
- App-server started on `ws://127.0.0.1:17432`
- WebSocket readiness probed via `wait_ready_async()` — connected
- Initialize handshake sent: `initialize` + `initialized` notification
- Real thread created: UUID-format `codex_thread_id` returned via nested `result.thread.id`
- Real turn started: turn id returned via nested `result.turn.id`
- Events observed: `turn_started`, `item_started`, `item_completed`, `agent_message_delta`, `turn_completed`
- Turn completed successfully within timeout

Key protocol finding: Codex app-server requires `initialize` RPC before `thread/start` (standard JSON-RPC lifecycle). Backend performs this handshake in `_ensure_client()`.

## Real Telegram Evidence

**PRE-LAUNCH PREFLIGHT READY** — bot token and allowlisted user ID are configured through the private environment file. `chat_id` and `task_id` are intentionally not required before the first real Telegram interaction.

The live smoke test is split into two gates:
- Pre-launch: requires token + allowlisted user ID, then validates local config.
- Post-interaction: requires `WLCODEX_LIVE_SMOKE_TASK_ID` after a real `/task`.
- Optional strict chat check: `WLCODEX_TELEGRAM_CHAT_ID`, when set, must match the SQLite task row.
- `WLCODEX_RUN_TELEGRAM_LIVE != 1` → `SKIPPED`
- Current pre-launch result with private env loaded: `1 passed, 2 skipped`
- Fake-backend thread IDs (e.g., "fake-xxxx") are explicitly rejected by UUID validation
- Bot token is never printed to logs, stdout, or test output

The pre-launch env vars are:
```bash
export WLCODEX_TELEGRAM_BOT_TOKEN=...
export WLCODEX_TELEGRAM_ALLOWED_USER_ID=...
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

After the first real `/task`, add:
```bash
export WLCODEX_LIVE_SMOKE_TASK_ID=<task_id>
# Optional: export WLCODEX_TELEGRAM_CHAT_ID=<chat_id>
WLCODEX_RUN_TELEGRAM_LIVE=1 .venv/bin/python -m pytest tests/test_live_telegram_smoke.py -q
```

## Drift Repairs Applied (this session)

### 1. Approval expiry — real Codex unlock (REPAIRED)
- **Before**: Expired approvals marked `EXPIRED` locally but no response sent to Codex app-server. The held JSON-RPC request was never resolved, leaving the Codex turn permanently stuck waiting for approval.
- **After**: Expiry path in `resolve_callback` sends `cancel`/`decline` response to backend via `resolve_approval()` BEFORE local DB changes. For permissions: sends cancel (empty permissions) and interrupts the active turn. An independent background ticker in `event_bridge.py` proactively expires stale approvals every 60s, even when no backend events arrive. Local state is cleaned up only after backend unlock succeeds; if backend unlock fails, the approval remains pending.

### 2. /tail bounded log + SQLite agent_message_delta fallback (REPAIRED)
- **Before**: Per-task log files grew 7x24 without bound. `agent_message_delta` deltas were only written to log files, never to SQLite events — so the SQLite `/tail` fallback was always blank for assistant output.
- **After**: Log files capped at 500KB (keep last ~375KB of content on overflow). `agent_message_delta` events stored in `task_events` with delta text (up to 2000 chars) so `/tail` SQLite fallback works. `command_output` truncation expanded from 500 to 2000 chars.

### 3. Live Telegram smoke — no fake pass, no secret leaks (HARDENED)
- **Before**: Test accepted `failed` and `aborted` as valid smoke evidence. No UUID validation on thread IDs.
- **After**: Test rejects `failed`/`aborted`/`queued`/`archived`. Validates `codex_thread_id` matches UUID pattern to reject fake-backend prefixes. Docstring explicitly states "NEVER prints bot_token". Pre-launch checks no longer require impossible `chat_id`/`task_id` values.

## Previously Completed Drift Repairs (verified in this run)

- [x] Protocol contracts locked with schema-driven builders/parsers
- [x] JSON-RPC requests have timeout; server requests are non-blocking
- [x] App-server lifecycle: `AppServerProcess` with real WebSocket readiness probe
- [x] Backend uses `build_thread_start_params`, `build_turn_start_params`, `parse_thread_start_response`, `parse_turn_response`
- [x] Workspace write checks enforced; `reserve_task` separates reservation from thread creation
- [x] `/continue` enforces workspace availability, transitions to `queued`
- [x] State machine supports `done/failed/paused/aborted -> queued` for continue
- [x] Approval resolution: backend response FIRST, then local resolve, decrement counter, move task back to running
- [x] Approval responses use `build_approval_response()` with schema-correct shapes
- [x] `/tail` falls back to SQLite events when no log file exists
- [x] `/diff` reads stored `diff_updated` events
- [x] `/sessions` is canonical command (with `/codex-sessions` alias)
- [x] SQLite migrations upgrade legacy databases via guarded `ALTER TABLE`
- [x] `schema_meta` table records `schema_version = 2`
- [x] Codex app-server `initialize` handshake added to `_ensure_client()`
- [x] Permissions approval: correct payload storage and schema response
- [x] `allow_session_approval`: config wired through all layers, button hidden when disabled
- [x] `thread/fork`: nested response parsing
- [x] App-server lifecycle: managed process shutdown, external process reuse, health probe before first connect
- [x] Live smoke assertions: failed/aborted rejected, blocked marked honestly

## Acceptance Status

| Gate | Status | Evidence |
|------|--------|----------|
| Unit + contract suite | **CLOSED** | 189 passed, 0 failed |
| Ruff lint | **CLOSED** | All checks passed |
| Real app-server integration | **CLOSED** | 1 passed — thread/start, turn/start, events, turn/completed verified against Codex CLI 0.121.0 |
| Live Telegram preflight | **CLOSED** | 1 passed, 2 skipped with private env loaded; chat_id/task_id not required before first Telegram interaction |
| Live Telegram post-interaction smoke | **BLOCKED** | Requires `WLCODEX_LIVE_SMOKE_TASK_ID` from a real human-to-bot `/task` |
| Live approval smoke | **BLOCKED** | Requires live Telegram smoke first, plus `WLCODEX_LIVE_APPROVAL_REQUIRED=1` |

## Conclusion

Three drift repairs applied in this session:

1. **Approval expiry real unlock**: Expiry now sends cancel/decline to Codex before local DB changes, keeps local state pending if backend unlock fails, and runs from an independent scanner — **closed**.
2. **Bounded log + agent_message_delta fallback**: Log files capped at 500KB, agent_message_delta stored in SQLite so /tail fallback works — **closed**.
3. **Live smoke hardened**: UUID validation rejects fake thread IDs, secrets never printed, pre-launch does not require impossible chat_id/task_id values — **closed**.

**What is verified**: 189 unit + contract tests pass, lint clean, real Codex app-server (0.121.0) thread→turn→events→completion lifecycle proven, and live Telegram preflight passes with the private environment file.

**What is blocked**: Full Telegram post-interaction smoke requires a real `/task` to produce a SQLite task row and `WLCODEX_LIVE_SMOKE_TASK_ID`. `chat_id` is discovered from that row; `WLCODEX_TELEGRAM_CHAT_ID` is optional for stricter checking. The test does NOT accept fake-backend evidence as smoke.

**What remains**: A human operator must start WLCodex, send a real Telegram `/task`, receive the status card, and then run the post-interaction smoke with the generated task id.
