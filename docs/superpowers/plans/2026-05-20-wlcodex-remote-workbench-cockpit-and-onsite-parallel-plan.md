# WLCodex Remote Workbench Cockpit And Onsite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the half-finished dual-mode interaction with one remote workbench that supports Cockpit and Onsite views plus orchestrated, Codex-only, and Claude-only execution modes.

**Architecture:** Add a thin workbench contract layer that separates view state from execution mode, then adapt Telegram menu/help, terminal attach behavior, direct execution routes, and recovery around that contract. Keep existing controller, runtime events, Telegram outbox, and surface modules where they already fit; make precise changes around routing and rendering boundaries.

**Tech Stack:** Python, pytest, SQLite-backed runtime event store, existing WLCodex controller/orchestration runner, existing Telegram bot/outbox, existing Codex app-server backend, existing Claude stream-json backend, current `wlcodex/surfaces/*` modules.

---

## Required Reading

- Spec: `docs/superpowers/specs/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-design.md`
- Current dual-surface spec: `docs/superpowers/specs/2026-05-19-wlcodex-dual-surface-product-and-terminal-mode-design.md`
- Current menu: `wlcodex/menu.py`
- Current Telegram routing: `wlcodex/telegram_app.py`
- Current command parser: `wlcodex/router.py`
- Current terminal manager: `wlcodex/surfaces/terminal/manager.py`
- Current runtime event model: `wlcodex/runtime_events.py`, `wlcodex/runtime_state.py`, `wlcodex/runtime_projector.py`

## Engineering Rules

- Run GitNexus impact analysis before editing any existing function, class, or
  method.
- If impact is HIGH or CRITICAL, stop and report before editing.
- Write a failing test before implementation.
- Keep changes small and trace each changed line to an acceptance criterion.
- Do not rename existing commands in the first pass; add product language while
  preserving compatibility.
- Do not let two parallel workers write the same file set.
- Every worker must assume other workers are editing nearby tracks and must not
  revert unrelated changes.
- Run `gitnexus_detect_changes(scope="all")` before any commit.

## Parallelization Model

Task 1 is the shared foundation and lands first. Tasks 2 through 7 can run in
parallel after Task 1 because they own disjoint files. Tasks 8 and 9 integrate
the parallel work and run after the relevant tracks land.

| Track | Tasks | Write Ownership | Dependency |
| --- | --- | --- | --- |
| Foundation | 1 | `wlcodex/workbench/*`, `tests/test_workbench_core.py` | none |
| Cockpit UX | 2 | `wlcodex/menu.py`, `wlcodex/status.py`, `tests/test_workbench_cockpit_menu.py` | Task 1 |
| Telegram commands | 3 | `wlcodex/router.py`, `tests/test_workbench_commands.py` | Task 1 |
| Onsite sessions | 4 | `wlcodex/surfaces/terminal/*`, `tests/test_workbench_onsite_terminal.py` | Task 1 |
| Execution modes | 5 | `wlcodex/controller.py`, `tests/test_workbench_execution_modes.py` | Task 1 |
| Runtime/recovery | 6 | `wlcodex/runtime_events.py`, `wlcodex/runtime_state.py`, `wlcodex/runtime_projector.py`, `tests/test_workbench_runtime_state.py` | Task 1 |
| Telegram routing | 7 | `wlcodex/telegram_app.py`, `tests/test_workbench_telegram_routing.py` | Tasks 1, 3, 4 |
| Integration | 8 | `tests/test_workbench_remote_integration.py` | Tasks 2-7 |
| Docs/config cleanup | 9 | `README.md`, `config/wlcodex.example.toml`, docs tests if present | Tasks 2-8 |

## File Structure

Create:

```text
wlcodex/workbench/__init__.py
wlcodex/workbench/models.py
wlcodex/workbench/routing.py
wlcodex/workbench/rendering.py
wlcodex/workbench/events.py
tests/test_workbench_core.py
tests/test_workbench_cockpit_menu.py
tests/test_workbench_commands.py
tests/test_workbench_onsite_terminal.py
tests/test_workbench_execution_modes.py
tests/test_workbench_runtime_state.py
tests/test_workbench_telegram_routing.py
tests/test_workbench_remote_integration.py
```

Modify only in the named tasks:

```text
wlcodex/menu.py
wlcodex/status.py
wlcodex/router.py
wlcodex/surfaces/terminal/manager.py
wlcodex/surfaces/terminal/router.py
wlcodex/surfaces/terminal/renderer.py
wlcodex/controller.py
wlcodex/runtime_events.py
wlcodex/runtime_state.py
wlcodex/runtime_projector.py
wlcodex/telegram_app.py
wlcodex/main.py
README.md
config/wlcodex.example.toml
```

## Task 1: Workbench Foundation Contracts

**Files:**
- Create: `wlcodex/workbench/__init__.py`
- Create: `wlcodex/workbench/models.py`
- Create: `wlcodex/workbench/routing.py`
- Create: `wlcodex/workbench/rendering.py`
- Create: `wlcodex/workbench/events.py`
- Test: `tests/test_workbench_core.py`

- [ ] **Step 1: Run impact analysis for nearby symbols**

Run:

```bash
npx gitnexus impact --repo wlcodex --target SurfaceMode --direction upstream
npx gitnexus impact --repo wlcodex --target ModeSwitchCommand --direction upstream
```

Expected: record direct callers and risk. Stop before editing existing symbols if
the result is HIGH or CRITICAL.

- [ ] **Step 2: Write failing tests for view and execution separation**

Create `tests/test_workbench_core.py` with tests for these outcomes:

```python
from wlcodex.workbench.models import (
    ExecutionMode,
    ViewMode,
    WorkbenchRoute,
    WorkbenchState,
)
from wlcodex.workbench.routing import route_plain_text


def test_cockpit_plain_text_defaults_to_orchestrated_mode():
    state = WorkbenchState(
        conversation_id=42,
        chat_id=100,
        workspace_alias="wlcodex",
        view=ViewMode.COCKPIT,
        execution_mode=ExecutionMode.ORCHESTRATED,
        active_agent="",
        active_phase="idle",
    )

    route = route_plain_text(state, "重做终端手机体验")

    assert route is WorkbenchRoute.ORCHESTRATED_COCKPIT


def test_onsite_plain_text_goes_to_selected_live_session():
    state = WorkbenchState(
        conversation_id=42,
        chat_id=100,
        workspace_alias="wlcodex",
        view=ViewMode.ONSITE,
        execution_mode=ExecutionMode.ORCHESTRATED,
        active_agent="claude",
        active_phase="implementation",
    )

    route = route_plain_text(state, "继续修失败测试")

    assert route is WorkbenchRoute.ONSITE_INPUT


def test_execution_mode_does_not_change_view_mode():
    state = WorkbenchState(
        conversation_id=42,
        chat_id=100,
        workspace_alias="wlcodex",
        view=ViewMode.COCKPIT,
        execution_mode=ExecutionMode.CLAUDE_DIRECT,
        active_agent="claude",
        active_phase="implementation",
    )

    assert state.view is ViewMode.COCKPIT
    assert state.execution_mode is ExecutionMode.CLAUDE_DIRECT
```

- [ ] **Step 3: Run the new tests and confirm failure**

Run:

```bash
pytest tests/test_workbench_core.py -q
```

Expected: FAIL because `wlcodex.workbench` does not exist.

- [ ] **Step 4: Add minimal workbench models**

Implement only:

```text
ViewMode.COCKPIT
ViewMode.ONSITE
ExecutionMode.ORCHESTRATED
ExecutionMode.CODEX_DIRECT
ExecutionMode.CLAUDE_DIRECT
WorkbenchRoute.ORCHESTRATED_COCKPIT
WorkbenchRoute.CODEX_DIRECT_COCKPIT
WorkbenchRoute.CLAUDE_DIRECT_COCKPIT
WorkbenchRoute.ONSITE_INPUT
WorkbenchState dataclass
```

- [ ] **Step 5: Add pure plain-text routing**

`route_plain_text(state, text)` returns:

```text
ONSITE_INPUT when state.view == ONSITE
ORCHESTRATED_COCKPIT when state.view == COCKPIT and execution_mode == ORCHESTRATED
CODEX_DIRECT_COCKPIT when state.view == COCKPIT and execution_mode == CODEX_DIRECT
CLAUDE_DIRECT_COCKPIT when state.view == COCKPIT and execution_mode == CLAUDE_DIRECT
```

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_workbench_core.py -q
```

Expected: PASS.

## Task 2: Cockpit Menu And Help UX

**Files:**
- Modify: `wlcodex/menu.py`
- Modify: `wlcodex/status.py`
- Test: `tests/test_workbench_cockpit_menu.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target build_bot_commands --direction upstream
npx gitnexus impact --repo wlcodex --target render_conversation_help --direction upstream
```

- [ ] **Step 2: Write failing menu tests**

Create tests that assert the natural menu contains exactly:

```text
new
status
terminal
diff
settings
help
```

Also assert it does not contain:

```text
codex
claude
auto
model
claude_mode
sessions
health
files
```

- [ ] **Step 3: Write failing help-copy tests**

Assert natural help contains:

```text
默认流程：Codex -> Claude -> Codex
当前视图：驾驶舱
接管现场
```

Assert natural help does not contain:

```text
terminal.enabled
session id
external_session_id
```

- [ ] **Step 4: Run tests and confirm failure**

Run:

```bash
pytest tests/test_workbench_cockpit_menu.py -q
```

- [ ] **Step 5: Update menu and help copy only**

Keep legacy command handlers intact. Change only visible command registration
and help rendering.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_workbench_cockpit_menu.py tests/test_config.py -q
```

Expected: PASS.

## Task 3: Workbench Command Parsing

**Files:**
- Modify: `wlcodex/router.py`
- Test: `tests/test_workbench_commands.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target parse_command --direction upstream
npx gitnexus impact --repo wlcodex --target ModeSwitchCommand --direction upstream
```

- [ ] **Step 2: Write failing command tests**

Add tests for:

```text
/terminal -> open onsite view
/product -> return cockpit view
/settings -> settings command
/codex hello -> Codex direct mode
/claude hello -> Claude direct mode
/auto hello -> orchestrated mode
```

Compatibility assertions:

```text
/terminal claude remains accepted
/terminal codex remains accepted
/terminal tail remains accepted
/terminal pause remains accepted
/terminal detach remains accepted
```

- [ ] **Step 3: Run tests and confirm failure for `/settings` only**

Run:

```bash
pytest tests/test_workbench_commands.py tests/test_router.py -q
```

Expected: existing commands pass, `/settings` fails before implementation.

- [ ] **Step 4: Add settings command parsing**

Add a `SettingsCommand` dataclass and parse `/settings`.

- [ ] **Step 5: Preserve existing command semantics**

Do not remove existing command dataclasses. Do not change `/product`,
`/terminal`, `/codex`, `/claude`, or `/auto` parser outputs unless the tests
require a small compatibility extension.

- [ ] **Step 6: Run parser tests**

Run:

```bash
pytest tests/test_workbench_commands.py tests/test_router.py -q
```

Expected: PASS.

## Task 4: Onsite Session Open, Start Card, Tail, And Leave

**Files:**
- Modify: `wlcodex/surfaces/terminal/manager.py`
- Modify: `wlcodex/surfaces/terminal/router.py`
- Modify: `wlcodex/surfaces/terminal/renderer.py`
- Test: `tests/test_workbench_onsite_terminal.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target TerminalSessionManager --direction upstream
npx gitnexus impact --repo wlcodex --target route_terminal_command --direction upstream
npx gitnexus impact --repo wlcodex --target render_terminal_frame --direction upstream
```

- [ ] **Step 2: Write failing tests for no-dead-session behavior**

Test outcomes:

```text
active_for_conversation returns latest attached session when present
open onsite with no session produces a start-card decision
tail on attached session returns latest frames
leave onsite marks delivery paused without aborting the session
```

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
pytest tests/test_workbench_onsite_terminal.py tests/test_terminal_surface.py -q
```

- [ ] **Step 4: Add minimal Onsite decision model in terminal manager**

Add focused methods without changing adapter contracts:

```text
open_for_conversation(conversation_id, preferred_agent)
record_frame(session_ref, frame)
tail(session_ref, limit)
pause_delivery(session_ref)
resume_delivery(session_ref)
leave_view(session_ref)
```

The manager can keep in-memory frame history for V1 tests. Durable persistence
lands in Task 6.

- [ ] **Step 5: Update terminal renderer language**

Render headers with user-facing "现场" wording while preserving raw frame text.

- [ ] **Step 6: Run terminal tests**

Run:

```bash
pytest tests/test_workbench_onsite_terminal.py tests/test_terminal_surface.py tests/test_terminal_redaction.py -q
```

Expected: PASS.

## Task 5: Execution Mode Behavior

**Files:**
- Modify: `wlcodex/controller.py`
- Test: `tests/test_workbench_execution_modes.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target handle_conversation_text --direction upstream
npx gitnexus impact --repo wlcodex --target handle_codex_direct --direction upstream
npx gitnexus impact --repo wlcodex --target handle_claude_direct --direction upstream
npx gitnexus impact --repo wlcodex --target handle_auto_mode --direction upstream
```

If any symbol is named differently in the current code, use
`mcp__gitnexus__.context` or `rg` to find the exact existing handler before
editing.

- [ ] **Step 2: Write failing execution-mode tests**

Test outcomes:

```text
ordinary Cockpit text starts default orchestrated run
/codex prompt calls Codex only
/claude prompt calls Claude only
/auto prompt calls Codex -> Claude -> Codex
Claude-only completion includes action text for Codex verification
Codex-only response does not enqueue Claude work
```

- [ ] **Step 3: Run tests and confirm current gaps**

Run:

```bash
pytest tests/test_workbench_execution_modes.py tests/test_command_flow.py tests/test_conversation_router.py -q
```

- [ ] **Step 4: Add explicit execution-mode labels to responses**

Keep existing command behavior. Add response metadata or stable text only where
needed for routing and user clarity.

- [ ] **Step 5: Add Claude-only verification affordance**

When a Claude-only run completes with code-changing evidence, include a
"让 Codex 验收" action that maps to the existing `/verify` or `/auto`
verification path.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_workbench_execution_modes.py tests/test_orchestration_runner.py -q
```

Expected: PASS.

## Task 6: Workbench Runtime Events And Recovery Projection

**Files:**
- Modify: `wlcodex/runtime_events.py`
- Modify: `wlcodex/runtime_state.py`
- Modify: `wlcodex/runtime_projector.py`
- Test: `tests/test_workbench_runtime_state.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target EventType --direction upstream
npx gitnexus impact --repo wlcodex --target RuntimeStateSnapshot --direction upstream
npx gitnexus impact --repo wlcodex --target RuntimeProjector --direction upstream
```

- [ ] **Step 2: Write failing replay tests**

Test replay reconstructs:

```text
view = cockpit or onsite
execution_mode = orchestrated, codex_direct, or claude_direct
active onsite agent
onsite cursor
cockpit cursor
orphaned onsite session after recovery event
```

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
pytest tests/test_workbench_runtime_state.py tests/test_runtime_state_replay.py tests/test_runtime_projector.py -q
```

- [ ] **Step 4: Add event constants and projection fields**

Add only the fields required by the tests. Prefer reusing existing
`conversation.mode.switched` and terminal session events where they already
serve the new workbench semantics.

- [ ] **Step 5: Run runtime tests**

Run:

```bash
pytest tests/test_workbench_runtime_state.py tests/test_runtime_state_replay.py tests/test_runtime_projector.py -q
```

Expected: PASS.

## Task 7: Telegram Routing And View Switching

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_workbench_telegram_routing.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target terminal_cmd --direction upstream
npx gitnexus impact --repo wlcodex --target product_cmd --direction upstream
npx gitnexus impact --repo wlcodex --target conversation_text --direction upstream
npx gitnexus impact --repo wlcodex --target _handle_terminal_text --direction upstream
npx gitnexus impact --repo wlcodex --target _apply_mode_switch --direction upstream
```

- [ ] **Step 2: Write failing Telegram routing tests**

Test outcomes:

```text
/terminal with active Claude implementation auto-opens Claude onsite
/terminal with no active session sends start card
Onsite text routes to terminal manager and not controller
/product returns Cockpit and preserves active workbench id
/settings sends settings card
terminal disabled copy does not expose terminal.enabled to normal users
```

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
pytest tests/test_workbench_telegram_routing.py tests/test_telegram_handlers.py tests/test_dual_surface_integration.py -q
```

- [ ] **Step 4: Route `/terminal` through Onsite open decision**

Replace dead-end "no session" copy with start-card behavior. Keep current
compatibility command parsing.

- [ ] **Step 5: Route `/product` through Cockpit return decision**

Record view change, keep workbench id, do not replay raw terminal output.

- [ ] **Step 6: Add `/settings` handler**

Settings card includes:

```text
默认流程
只问 Codex
只叫 Claude
模型
Claude 权限
工作区
```

- [ ] **Step 7: Run Telegram tests**

Run:

```bash
pytest tests/test_workbench_telegram_routing.py tests/test_telegram_handlers.py tests/test_dual_surface_integration.py -q
```

Expected: PASS.

## Task 8: End-To-End Workbench Integration

**Files:**
- Create: `tests/test_workbench_remote_integration.py`

- [ ] **Step 1: Write full closed-loop tests**

Create integration tests for:

```text
ordinary text -> orchestrated run -> onsite -> cockpit -> verify summary
/codex prompt -> Codex-only -> onsite raw Codex view
/claude prompt -> Claude-only -> onsite raw Claude view -> Codex verify action
restart replay restores view and execution mode
approval resolved from Cockpit is reflected in Onsite
approval resolved from Onsite is reflected in Cockpit
```

- [ ] **Step 2: Run integration tests and record failures**

Run:

```bash
pytest tests/test_workbench_remote_integration.py -q
```

- [ ] **Step 3: Patch only the owner track responsible for each failure**

Use this mapping:

```text
menu/help failure -> Task 2 owner
parser failure -> Task 3 owner
terminal attach/tail failure -> Task 4 owner
execution route failure -> Task 5 owner
runtime replay failure -> Task 6 owner
Telegram routing failure -> Task 7 owner
```

- [ ] **Step 4: Run the cross-track regression suite**

Run:

```bash
pytest tests/test_workbench_remote_integration.py tests/test_dual_surface_integration.py tests/test_terminal_surface.py tests/test_conversation_router.py -q
```

Expected: PASS.

## Task 9: Documentation And Config Alignment

**Files:**
- Modify: `README.md`
- Modify: `config/wlcodex.example.toml`

- [ ] **Step 1: Update README product language**

Document:

```text
Remote workbench
Cockpit view
Onsite view
default Codex -> Claude -> Codex workflow
Codex-only direct mode
Claude-only direct mode
```

- [ ] **Step 2: Update example config comments**

Keep config keys stable unless earlier tasks intentionally added new keys.
Change comments so operators understand product behavior without leaking
configuration details into normal user copy.

- [ ] **Step 3: Run doc-adjacent checks**

Run:

```bash
pytest tests/test_config.py tests/test_workbench_cockpit_menu.py -q
```

Expected: PASS.

## Final Verification

- [ ] **Step 1: Run targeted workbench suite**

Run:

```bash
pytest tests/test_workbench_core.py tests/test_workbench_cockpit_menu.py tests/test_workbench_commands.py tests/test_workbench_onsite_terminal.py tests/test_workbench_execution_modes.py tests/test_workbench_runtime_state.py tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py -q
```

Expected: PASS.

- [ ] **Step 2: Run existing related suite**

Run:

```bash
pytest tests/test_terminal_surface.py tests/test_dual_surface_integration.py tests/test_telegram_handlers.py tests/test_conversation_router.py tests/test_orchestration_runner.py tests/test_runtime_state_replay.py tests/test_runtime_projector.py -q
```

Expected: PASS.

- [ ] **Step 3: Run GitNexus change detection**

Run:

```bash
npx gitnexus detect-changes --repo wlcodex --scope all
```

Expected: changed symbols and affected flows match the planned ownership areas.

- [ ] **Step 4: Review user-facing copy**

Confirm normal user copy contains:

```text
驾驶舱
接管现场
默认流程：Codex -> Claude -> Codex
只问 Codex
只叫 Claude
```

Confirm normal user copy does not contain:

```text
terminal.enabled
external_session_id
thread id
session id
projection
runtime_events
```

## Commit Guidance

Commit per task after tests pass. Use messages like:

```text
feat: add remote workbench core contracts
feat: simplify cockpit menu and help
feat: add workbench settings command
feat: make onsite terminal open recoverably
feat: clarify direct execution modes
feat: project workbench view state
feat: route telegram through workbench views
test: cover remote workbench closed loops
docs: document remote workbench UX
```

