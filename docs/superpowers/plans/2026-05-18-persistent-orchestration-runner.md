# Persistent Orchestration Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make chief-engineer work run in a background runner so Telegram handlers are never blocked by full Codex-Claude-Codex orchestration.

**Architecture:** Move the long-running streaming orchestration loop out of `CommandController` into a dedicated `OrchestrationRunner`. The controller creates ledger rows, reserves the tracking task, starts the runner as an `asyncio.Task`, and returns an ACK immediately. Codex app-server prompt aggregation keeps using event fan-out, but replaces the single 60 second absolute wait with sliding idle timeout plus hard timeout for analysis and verification turns.

**Tech Stack:** Python asyncio, SQLite ledger, existing Codex app-server backend, existing interaction renderer, pytest.

---

### Task 1: Background Runner Boundary

**Files:**
- Create: `wlcodex/orchestration_runner.py`
- Modify: `wlcodex/controller.py`
- Test: `tests/test_orchestration_runner.py`

- [ ] **Step 1: Write failing tests**

Add tests proving `CommandController._handle_chief_engineer_impl()` starts a background job and returns without awaiting a slow orchestration, and proving the runner records terminal ledger state.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_orchestration_runner.py -q`

Expected: fails because `OrchestrationRunner` does not exist and the controller still awaits the loop inline.

- [ ] **Step 3: Implement runner**

Create an `OrchestrationRunner` with `start_chief_engineer(...) -> asyncio.Task[None]`. It owns the existing streaming-loop ledger updates currently embedded in `CommandController`.

- [ ] **Step 4: Wire controller**

Inject an optional runner into `CommandController`. When interaction streaming is enabled and a runner exists, reserve the task, create orchestration and Codex analysis run rows, emit `run_started`, schedule the runner, and immediately return `ControllerResponse("", already_rendered=True)`.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_orchestration_runner.py tests/test_controller_flow.py tests/test_telegram_handlers.py -q`

Expected: all selected tests pass.

### Task 2: Codex Prompt Liveness

**Files:**
- Modify: `wlcodex/codex_backend.py`
- Modify: `wlcodex/config.py`
- Modify: `wlcodex/main.py`
- Modify: `config/wlcodex.example.toml`
- Modify: `config/wlcodex.toml`
- Test: `tests/test_codex_backend_events.py`
- Test: `tests/test_config.py`
- Test: `tests/test_main_composition.py`

- [ ] **Step 1: Write failing tests**

Add tests that analysis/verification turns use long hard timeouts, refresh their idle deadline on matching events, interrupt on idle timeout, and interrupt on hard timeout even when events keep arriving.

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_codex_backend_events.py tests/test_config.py tests/test_main_composition.py -q`

Expected: fails because only `request_timeout_seconds` exists.

- [ ] **Step 3: Implement config**

Add backend config fields for Codex analysis hard timeout, verification hard timeout, and prompt idle timeout. Keep `request_timeout_seconds` for JSON-RPC request calls and general direct turns.

- [ ] **Step 4: Implement liveness wait**

Change `AppServerCodexBackend.send_codex_prompt()` to compute per-mode hard deadlines and sliding idle deadlines. Refresh idle on matching turn/thread events. Keep event fan-out intact.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_codex_backend_events.py tests/test_config.py tests/test_main_composition.py -q`

Expected: all selected tests pass.

### Task 3: Integration Verification and Release

**Files:**
- Modify as needed from Tasks 1 and 2.

- [ ] **Step 1: Run related tests**

Run: `.venv/bin/python -m pytest tests/test_orchestration_runner.py tests/test_controller_flow.py tests/test_telegram_handlers.py tests/test_codex_backend_events.py tests/test_config.py tests/test_main_composition.py -q`

- [ ] **Step 2: Run full tests**

Run: `.venv/bin/python -m pytest -q`

- [ ] **Step 3: Check diff hygiene**

Run: `git diff --check`

- [ ] **Step 4: GitNexus detect changes**

Run GitNexus `detect_changes(scope="all")` and verify affected scope matches controller/backend/config/runner work.

- [ ] **Step 5: Commit, push, restart**

Commit with a focused message, push `main`, restart `wlcodex.service`, and verify service is active.
