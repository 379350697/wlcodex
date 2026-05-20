# WLCodex Dual Surface Product And Terminal Mode Implementation Plan

> Superseded for product implementation: follow the 2026-05-20 Remote
> Workbench repair plans instead. Product/terminal mode wording below maps to
> Cockpit/Onsite views over one Workbench.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build two independent Telegram surfaces over one shared conversation core: Product Surface for event-driven phone UX and Terminal Surface for Claude Remote style raw session control.

**Architecture:** The work is split into core contracts, persistence, product surface, terminal surface, Telegram commands, recovery, and integration tests. Product and terminal surfaces share only durable conversation state and cursors; they do not call each other or share renderer buffers.

**Tech Stack:** Python, pytest, SQLite-backed WLCodex stores, existing runtime event log, existing Telegram outbox/transport, Codex app-server events, Claude Code stream-json/Remote Control concepts.

---

## Parallelization Model

The plan is designed for parallel implementation after Task 1 lands. Each
parallel track owns a separate file set.

| Track | Tasks | Write Ownership | Can Run In Parallel After |
| --- | --- | --- | --- |
| Core contracts | 1 | `wlcodex/surfaces/core/*`, `tests/test_surface_core.py` | start |
| Persistence | 2 | `wlcodex/surfaces/core/store.py`, migration files, `tests/test_surface_store.py` | Task 1 |
| Product surface | 3 | `wlcodex/surfaces/product/*`, `tests/test_product_surface.py` | Task 1 |
| Terminal surface | 4, 5, 6 | `wlcodex/surfaces/terminal/*`, `tests/test_terminal_surface*.py` | Task 1 |
| Telegram commands | 7 | `wlcodex/telegram_app.py`, `wlcodex/router.py`, `tests/test_surface_commands.py` | Tasks 1, 2 |
| Recovery | 8 | `wlcodex/recovery.py`, `wlcodex/runtime_state.py`, `tests/test_surface_recovery.py` | Tasks 1, 2, 4 |
| Integration | 9 | `tests/test_dual_surface_integration.py` | Tasks 2, 3, 4, 7 |
| Docs/config | 10 | `README.md`, config docs/tests | Tasks 1-9 |

Do not assign two workers to the same write ownership set. Workers must be told
they are not alone in the codebase and must not revert edits from other tracks.

## File Structure

Create:

```text
wlcodex/surfaces/__init__.py
wlcodex/surfaces/core/__init__.py
wlcodex/surfaces/core/events.py
wlcodex/surfaces/core/models.py
wlcodex/surfaces/core/router.py
wlcodex/surfaces/core/store.py
wlcodex/surfaces/product/__init__.py
wlcodex/surfaces/product/events.py
wlcodex/surfaces/product/renderer.py
wlcodex/surfaces/product/router.py
wlcodex/surfaces/product/speaker.py
wlcodex/surfaces/terminal/__init__.py
wlcodex/surfaces/terminal/claude_remote.py
wlcodex/surfaces/terminal/codex_terminal.py
wlcodex/surfaces/terminal/manager.py
wlcodex/surfaces/terminal/models.py
wlcodex/surfaces/terminal/redaction.py
wlcodex/surfaces/terminal/renderer.py
wlcodex/surfaces/terminal/router.py
tests/test_surface_core.py
tests/test_surface_store.py
tests/test_product_surface.py
tests/test_terminal_surface.py
tests/test_terminal_redaction.py
tests/test_surface_commands.py
tests/test_surface_recovery.py
tests/test_dual_surface_integration.py
```

Modify later, only in the named tasks:

```text
wlcodex/runtime_events.py
wlcodex/runtime_state.py
wlcodex/runtime_projector.py
wlcodex/telegram_app.py
wlcodex/router.py
wlcodex/main.py
wlcodex/config.py
wlcodex/recovery.py
```

## Task 1: Core Surface Contracts

**Files:**
- Create: `wlcodex/surfaces/__init__.py`
- Create: `wlcodex/surfaces/core/__init__.py`
- Create: `wlcodex/surfaces/core/events.py`
- Create: `wlcodex/surfaces/core/models.py`
- Create: `wlcodex/surfaces/core/router.py`
- Test: `tests/test_surface_core.py`

- [ ] **Step 1: Run impact analysis before editing symbols**

Run:

```bash
npx gitnexus impact --repo wlcodex --target RuntimeEvent --direction upstream
npx gitnexus impact --repo wlcodex --target TelegramUpdate --direction upstream
```

Expected: Record the risk level and direct callers in the implementation notes.
If risk is HIGH or CRITICAL, stop and warn before editing.

- [ ] **Step 2: Write failing tests for mode state and routing**

Create `tests/test_surface_core.py` with tests that assert:

```python
from wlcodex.surfaces.core.models import (
    ModeSwitchCheckpoint,
    SurfaceCursor,
    SurfaceMode,
    SurfaceRouteDecision,
)
from wlcodex.surfaces.core.router import route_text_by_mode


def test_mode_switch_checkpoint_preserves_external_sessions():
    checkpoint = ModeSwitchCheckpoint(
        conversation_id=42,
        chat_id=100,
        from_mode=SurfaceMode.PRODUCT,
        to_mode=SurfaceMode.TERMINAL,
        active_agent="claude",
        active_phase="implementation",
        workspace_alias="wlcodex",
        codex_thread_id="thr_1",
        codex_turn_id="turn_1",
        claude_session_id="claude_1",
        product_cursor=SurfaceCursor(surface="product", position=10),
        terminal_cursor=SurfaceCursor(surface="terminal", position=3),
    )

    assert checkpoint.to_mode is SurfaceMode.TERMINAL
    assert checkpoint.claude_session_id == "claude_1"
    assert checkpoint.product_cursor.position == 10
    assert checkpoint.terminal_cursor.position == 3


def test_product_mode_routes_text_to_product_controller():
    decision = route_text_by_mode(
        mode=SurfaceMode.PRODUCT,
        text="继续刚才的修改",
        selected_terminal_agent="claude",
    )

    assert decision == SurfaceRouteDecision.PRODUCT_CONVERSATION


def test_terminal_mode_routes_text_to_terminal_session():
    decision = route_text_by_mode(
        mode=SurfaceMode.TERMINAL,
        text="pytest -q",
        selected_terminal_agent="claude",
    )

    assert decision == SurfaceRouteDecision.TERMINAL_INPUT
```

- [ ] **Step 3: Run tests and confirm they fail**

Run:

```bash
pytest tests/test_surface_core.py -q
```

Expected: FAIL because `wlcodex.surfaces.core` modules do not exist.

- [ ] **Step 4: Implement core dataclasses and pure routing**

Implement `SurfaceMode`, `SurfaceCursor`, `ModeSwitchCheckpoint`, and
`SurfaceRouteDecision`. Implement `route_text_by_mode()` as a pure function.

Required behavior:

```text
mode=product  -> PRODUCT_CONVERSATION
mode=terminal -> TERMINAL_INPUT
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_surface_core.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit only this task's files**

Run:

```bash
git add wlcodex/surfaces tests/test_surface_core.py
git commit -m "feat: add dual surface core contracts"
```

## Task 2: Surface Event Store And Cursor Projection

**Files:**
- Create/modify: `wlcodex/surfaces/core/store.py`
- Modify: `wlcodex/runtime_events.py`
- Modify: `wlcodex/runtime_state.py`
- Modify: `wlcodex/runtime_projector.py`
- Test: `tests/test_surface_store.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target replay_events --direction upstream
npx gitnexus impact --repo wlcodex --target RuntimeStateSnapshot --direction upstream
npx gitnexus impact --repo wlcodex --target RuntimeProjector --direction upstream
```

Expected: Record blast radius. Warn before editing on HIGH or CRITICAL risk.

- [ ] **Step 2: Write failing projection tests**

Create `tests/test_surface_store.py` with tests that assert:

```python
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.surfaces.core.store import replay_surface_state


def _event(event_type, payload, event_id=1):
    event = RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.CONVERSATION,
        aggregate_id="42",
        correlation_id="corr",
        source=EventSource.TELEGRAM,
        actor="user",
        visibility=Visibility.OPERATOR,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=42,
    )
    event.id = event_id
    return event


def test_replay_surface_state_tracks_active_mode_and_cursors():
    state = replay_surface_state([
        _event("conversation.mode.switched", {
            "chat_id": 100,
            "conversation_id": 42,
            "from_mode": "product",
            "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event("surface.cursor.advanced", {
            "chat_id": 100,
            "conversation_id": 42,
            "surface": "terminal",
            "position": 22,
        }, event_id=11),
    ])

    surface = state.by_chat[100]
    assert surface.active_mode == "terminal"
    assert surface.selected_terminal_agent == "claude"
    assert surface.cursors["terminal"].position == 22
```

- [ ] **Step 3: Run failing test**

Run:

```bash
pytest tests/test_surface_store.py -q
```

Expected: FAIL because `replay_surface_state` is missing.

- [ ] **Step 4: Implement replay-only surface state**

Implement a pure replay function first. It must not require SQLite. It should
consume runtime events and return:

```text
by_chat[chat_id].active_mode
by_chat[chat_id].selected_terminal_agent
by_chat[chat_id].cursors[surface].position
```

- [ ] **Step 5: Add event constants**

Add named event constants for:

```text
CONVERSATION_MODE_SWITCHED
SURFACE_CURSOR_ADVANCED
TERMINAL_SESSION_ATTACHED
TERMINAL_SESSION_DETACHED
TERMINAL_SESSION_INPUT_SENT
TERMINAL_SESSION_OUTPUT_FRAME
PRODUCT_DISPLAY_FRAME
PRODUCT_PENDING_CONTEXT_RECORDED
```

Use existing runtime event naming style.

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_surface_store.py tests/test_runtime_state_replay.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit only this task's files**

Run:

```bash
git add wlcodex/surfaces/core/store.py wlcodex/runtime_events.py wlcodex/runtime_state.py wlcodex/runtime_projector.py tests/test_surface_store.py
git commit -m "feat: project dual surface mode state"
```

## Task 3: Product Surface Renderer And Speaker Labels

**Files:**
- Create: `wlcodex/surfaces/product/__init__.py`
- Create: `wlcodex/surfaces/product/events.py`
- Create: `wlcodex/surfaces/product/renderer.py`
- Create: `wlcodex/surfaces/product/router.py`
- Create: `wlcodex/surfaces/product/speaker.py`
- Test: `tests/test_product_surface.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target InteractionRenderer --direction upstream
npx gitnexus impact --repo wlcodex --target StreamingRenderer --direction upstream
```

Expected: Record risk before editing. This task should create new product files
and avoid editing existing renderers unless absolutely required.

- [ ] **Step 2: Write failing product rendering tests**

Create `tests/test_product_surface.py` with tests that assert:

```python
from wlcodex.surfaces.product.speaker import product_speaker_line
from wlcodex.surfaces.product.events import ProductDisplayEvent


def test_codex_analysis_line_has_speaker_label():
    event = ProductDisplayEvent(
        agent="codex",
        phase="analysis",
        text="我开始分析这个需求。",
    )

    assert product_speaker_line(event) == "codex: 我开始分析这个需求。"


def test_claude_implementation_line_has_speaker_label():
    event = ProductDisplayEvent(
        agent="claude",
        phase="implementation",
        text="现在开始实现。",
    )

    assert product_speaker_line(event) == "claude: 现在开始实现。"


def test_product_line_hides_raw_diff_by_default():
    event = ProductDisplayEvent(
        agent="codex",
        phase="verification",
        text="diff --git a/secret.py b/secret.py\n+TOKEN=abc",
        raw_kind="diff",
    )

    assert product_speaker_line(event) == "codex: 代码改动已记录，可点 查看 diff。"
```

- [ ] **Step 3: Run failing test**

Run:

```bash
pytest tests/test_product_surface.py -q
```

Expected: FAIL because product modules do not exist.

- [ ] **Step 4: Implement product event and speaker formatting**

Implement `ProductDisplayEvent` and `product_speaker_line()`.

Rules:

```text
agent must be codex, claude, system, or user
raw_kind=diff -> summarized diff line
raw_kind=tool_output -> summarized tool line
ordinary text -> "{agent}: {text}"
```

- [ ] **Step 5: Add product route guard**

Implement a pure function in `wlcodex/surfaces/product/router.py`:

```text
phase=implementation -> record pending context
phase=verification -> record pending context
otherwise -> continue product conversation
```

Add tests in `tests/test_product_surface.py` for implementation and
verification phases.

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_product_surface.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit only this task's files**

Run:

```bash
git add wlcodex/surfaces/product tests/test_product_surface.py
git commit -m "feat: add product surface speaker rendering"
```

## Task 4: Terminal Surface Models, Redaction, And Renderer

**Files:**
- Create: `wlcodex/surfaces/terminal/__init__.py`
- Create: `wlcodex/surfaces/terminal/models.py`
- Create: `wlcodex/surfaces/terminal/redaction.py`
- Create: `wlcodex/surfaces/terminal/renderer.py`
- Test: `tests/test_terminal_surface.py`
- Test: `tests/test_terminal_redaction.py`

- [ ] **Step 1: Write failing terminal model tests**

Create `tests/test_terminal_surface.py` with tests that assert:

```python
from wlcodex.surfaces.terminal.models import TerminalFrame, TerminalSessionRef
from wlcodex.surfaces.terminal.renderer import render_terminal_frame


def test_terminal_frame_renders_agent_phase_prefix():
    frame = TerminalFrame(
        conversation_id=42,
        agent="claude",
        phase="implementation",
        text="Running pytest -q",
        frame_kind="stdout",
        sequence=7,
    )

    assert render_terminal_frame(frame) == "[claude:implementation] Running pytest -q"


def test_terminal_session_ref_keeps_strategy():
    ref = TerminalSessionRef(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
        status="attached",
    )

    assert ref.strategy == "stream_json"
    assert ref.status == "attached"
```

- [ ] **Step 2: Write failing redaction tests**

Create `tests/test_terminal_redaction.py`:

```python
from wlcodex.surfaces.terminal.redaction import redact_terminal_text


def test_redacts_known_secret_names():
    text = "TELEGRAM_BOT_TOKEN=123\nOPENAI_API_KEY=sk-test"

    redacted = redact_terminal_text(text)

    assert "123" not in redacted
    assert "sk-test" not in redacted
    assert "TELEGRAM_BOT_TOKEN=<redacted>" in redacted
    assert "OPENAI_API_KEY=<redacted>" in redacted
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
pytest tests/test_terminal_surface.py tests/test_terminal_redaction.py -q
```

Expected: FAIL because terminal modules do not exist.

- [ ] **Step 4: Implement models, renderer, and redaction**

Implement:

```text
TerminalSessionRef
TerminalFrame
render_terminal_frame()
redact_terminal_text()
```

Redact at least:

```text
TELEGRAM_BOT_TOKEN
OPENAI_API_KEY
ANTHROPIC_API_KEY
CLAUDE_CODE_OAUTH_TOKEN
WLCODEX_TELEGRAM_BOT_TOKEN
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_terminal_surface.py tests/test_terminal_redaction.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit only this task's files**

Run:

```bash
git add wlcodex/surfaces/terminal tests/test_terminal_surface.py tests/test_terminal_redaction.py
git commit -m "feat: add terminal surface rendering primitives"
```

## Task 5: Terminal Session Manager With Fake Adapters

**Files:**
- Create: `wlcodex/surfaces/terminal/manager.py`
- Create: `wlcodex/surfaces/terminal/router.py`
- Test: `tests/test_terminal_surface.py`

- [ ] **Step 1: Write failing manager tests**

Extend `tests/test_terminal_surface.py`:

```python
from wlcodex.surfaces.terminal.manager import TerminalSessionManager


class FakeTerminalAdapter:
    def __init__(self):
        self.inputs = []

    async def send_input(self, session_ref, text):
        self.inputs.append((session_ref.external_session_id, text))


async def test_terminal_manager_sends_input_to_selected_session():
    adapter = FakeTerminalAdapter()
    manager = TerminalSessionManager(adapters={"claude": adapter})
    ref = manager.attach(
        conversation_id=42,
        agent="claude",
        strategy="stream_json",
        external_session_id="claude_1",
    )

    await manager.send_input(ref, "continue")

    assert adapter.inputs == [("claude_1", "continue")]
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/test_terminal_surface.py::test_terminal_manager_sends_input_to_selected_session -q
```

Expected: FAIL because manager is missing.

- [ ] **Step 3: Implement manager with adapter protocol**

Implement:

```text
TerminalSessionManager.attach()
TerminalSessionManager.detach()
TerminalSessionManager.send_input()
TerminalSessionManager.active_for_conversation()
```

The manager should not know Telegram. It only manages session refs and delegates
to adapters.

- [ ] **Step 4: Add terminal routing tests**

Test that `route_terminal_command("/terminal agent codex")` selects Codex and
`route_terminal_command("/terminal product")` requests a mode switch to
product.

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_terminal_surface.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit only this task's files**

Run:

```bash
git add wlcodex/surfaces/terminal/manager.py wlcodex/surfaces/terminal/router.py tests/test_terminal_surface.py
git commit -m "feat: add terminal session manager"
```

## Task 6: Claude And Codex Terminal Adapters

**Files:**
- Create: `wlcodex/surfaces/terminal/claude_remote.py`
- Create: `wlcodex/surfaces/terminal/codex_terminal.py`
- Test: `tests/test_terminal_surface.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target ClaudeBackend --direction upstream
npx gitnexus impact --repo wlcodex --target AppServerCodexBackend --direction upstream
```

Expected: Prefer adapter wrappers over edits to existing backends. Warn on HIGH
or CRITICAL risk.

- [ ] **Step 2: Write failing adapter tests with fakes**

Extend `tests/test_terminal_surface.py`:

```python
from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter
from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter


class FakeClaudeBackend:
    def __init__(self):
        self.received = []

    async def send_terminal_input(self, session_id, text):
        self.received.append((session_id, text))


async def test_claude_terminal_adapter_delegates_input():
    backend = FakeClaudeBackend()
    adapter = ClaudeTerminalAdapter(backend)

    await adapter.send_input_by_session_id("claude_1", "next")

    assert backend.received == [("claude_1", "next")]


class FakeCodexBackend:
    def __init__(self):
        self.received = []

    async def steer_thread(self, thread_id, text):
        self.received.append((thread_id, text))


async def test_codex_terminal_adapter_delegates_input():
    backend = FakeCodexBackend()
    adapter = CodexTerminalAdapter(backend)

    await adapter.send_input_by_thread_id("thr_1", "inspect diff")

    assert backend.received == [("thr_1", "inspect diff")]
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
pytest tests/test_terminal_surface.py::test_claude_terminal_adapter_delegates_input tests/test_terminal_surface.py::test_codex_terminal_adapter_delegates_input -q
```

Expected: FAIL because adapters are missing.

- [ ] **Step 4: Implement adapter shells**

Implement thin adapters. They should support fake backends first and avoid
assuming the final real transport. The real implementation can later map to:

```text
Claude: official remote-control, stream-json, or PTY strategy
Codex: app-server thread/turn, exec --json, or PTY strategy
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_terminal_surface.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit only this task's files**

Run:

```bash
git add wlcodex/surfaces/terminal/claude_remote.py wlcodex/surfaces/terminal/codex_terminal.py tests/test_terminal_surface.py
git commit -m "feat: add terminal agent adapters"
```

## Task 7: Telegram Mode Commands

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Modify: `wlcodex/router.py`
- Modify: `wlcodex/main.py`
- Test: `tests/test_surface_commands.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target WlCodexHandlers --direction upstream
npx gitnexus impact --repo wlcodex --target CommandRouter --direction upstream
```

Expected: Record blast radius. This is likely medium/high because Telegram
handlers are central; warn before editing if required.

- [ ] **Step 2: Write failing command tests**

Create `tests/test_surface_commands.py`:

```python
from wlcodex.router import parse_command


def test_product_command_parses_mode_switch():
    command = parse_command("/product")

    assert command.kind == "mode_switch"
    assert command.mode == "product"


def test_terminal_command_parses_mode_switch():
    command = parse_command("/terminal claude")

    assert command.kind == "mode_switch"
    assert command.mode == "terminal"
    assert command.agent == "claude"
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
pytest tests/test_surface_commands.py -q
```

Expected: FAIL because router command shapes do not exist.

- [ ] **Step 4: Add parser support**

Add command parsing for:

```text
/mode
/product
/terminal
/terminal claude
/terminal codex
/terminal tail
/terminal detach
/terminal product
```

Parser output must be deterministic and testable without Telegram.

- [ ] **Step 5: Wire Telegram handler to core mode switch service**

In `WlCodexHandlers`, route parsed mode commands to the surface core service.
The handler should send a short confirmation:

```text
已切到 product 模式。
已切到 terminal 模式，当前 agent: claude。
```

Do not start a new task as part of mode switching.

- [ ] **Step 6: Run command tests and existing Telegram tests**

Run:

```bash
pytest tests/test_surface_commands.py tests/test_telegram_handlers.py tests/test_telegram_conversation_handlers.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit only this task's files**

Run:

```bash
git add wlcodex/router.py wlcodex/telegram_app.py wlcodex/main.py tests/test_surface_commands.py
git commit -m "feat: add Telegram dual surface commands"
```

## Task 8: Restart Recovery And Reattach Semantics

**Files:**
- Modify: `wlcodex/recovery.py`
- Modify: `wlcodex/runtime_state.py`
- Test: `tests/test_surface_recovery.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target RecoveryManager --direction upstream
npx gitnexus impact --repo wlcodex --target replay_events --direction upstream
```

Expected: Record blast radius and warn if HIGH or CRITICAL.

- [ ] **Step 2: Write failing recovery tests**

Create `tests/test_surface_recovery.py`:

```python
from wlcodex.surfaces.core.store import replay_surface_state
from tests.test_surface_store import _event


def test_recovery_marks_missing_terminal_session_detached():
    state = replay_surface_state([
        _event("conversation.mode.switched", {
            "chat_id": 100,
            "conversation_id": 42,
            "from_mode": "product",
            "to_mode": "terminal",
            "active_agent": "claude",
        }, event_id=10),
        _event("terminal.session.attached", {
            "chat_id": 100,
            "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "status": "attached",
        }, event_id=11),
        _event("terminal.session.detached", {
            "chat_id": 100,
            "conversation_id": 42,
            "agent": "claude",
            "external_session_id": "claude_1",
            "status": "orphaned",
        }, event_id=12),
    ])

    surface = state.by_chat[100]
    assert surface.active_mode == "terminal"
    assert surface.terminal_sessions["claude"].status == "orphaned"
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
pytest tests/test_surface_recovery.py -q
```

Expected: FAIL until replay supports terminal attach/detach state.

- [ ] **Step 4: Extend replay for terminal session lifecycle**

Handle:

```text
terminal.session.attached
terminal.session.detached
terminal.session.aborted
```

Terminal failure must not change product mode state except when mode itself is
switched.

- [ ] **Step 5: Add recovery policy**

On startup:

```text
attached session + process alive -> status attached
attached session + process missing -> append detached/orphaned event
product mode active -> keep product mode usable even if terminal is orphaned
```

- [ ] **Step 6: Run recovery tests**

Run:

```bash
pytest tests/test_surface_recovery.py tests/test_recovery.py tests/test_runtime_state_replay.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit only this task's files**

Run:

```bash
git add wlcodex/recovery.py wlcodex/runtime_state.py wlcodex/surfaces/core/store.py tests/test_surface_recovery.py
git commit -m "feat: recover dual surface sessions"
```

## Task 9: End-To-End Dual Surface Integration

**Files:**
- Create: `tests/test_dual_surface_integration.py`

- [ ] **Step 1: Write integration tests**

Create tests with fake product controller, fake terminal manager, fake Telegram
transport, and fake runtime store. Cover:

```python
async def test_product_to_terminal_to_product_keeps_conversation_id():
    ...

async def test_terminal_input_does_not_call_product_orchestrator():
    ...

async def test_product_followup_during_implementation_records_pending_context():
    ...

async def test_approval_resolution_is_shared_between_surfaces():
    ...
```

Each fake should record calls in memory. The test must assert no cross-surface
calls occur.

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_dual_surface_integration.py -q
```

Expected: FAIL until tasks 2, 3, 4, and 7 are wired together.

- [ ] **Step 3: Wire integration adapters**

Add only the minimal glue needed to make the tested flow work:

```text
Telegram command -> core mode switch
product text -> product controller
terminal text -> terminal manager
mode switch -> independent cursor checkpoint
```

- [ ] **Step 4: Run integration and focused regression tests**

Run:

```bash
pytest tests/test_dual_surface_integration.py tests/test_surface_core.py tests/test_product_surface.py tests/test_terminal_surface.py tests/test_surface_commands.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit only this task's files**

Run:

```bash
git add tests/test_dual_surface_integration.py wlcodex/surfaces wlcodex/telegram_app.py wlcodex/router.py
git commit -m "test: cover dual surface switching"
```

## Task 10: Config, Help Text, And Operator Docs

**Files:**
- Modify: `wlcodex/config.py`
- Modify: `wlcodex/telegram_app.py`
- Modify: `README.md` or `docs/`
- Test: `tests/test_surface_commands.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target AppConfig --direction upstream
npx gitnexus impact --repo wlcodex --target help_cmd --direction upstream
```

Expected: Record blast radius.

- [ ] **Step 2: Add config tests**

Extend `tests/test_surface_commands.py` or create a config-focused test:

```python
def test_default_surface_mode_is_product(config_factory):
    config = config_factory()

    assert config.telegram.default_surface_mode == "product"
```

If no `config_factory` exists, use the repo's existing config test pattern.

- [ ] **Step 3: Add config**

Add:

```text
telegram.default_surface_mode = product
terminal.enabled = false by default until live smoke passes
terminal.default_agent = claude
terminal.max_frame_chars = 3500
terminal.redaction_enabled = true
```

- [ ] **Step 4: Update help text**

Help should mention:

```text
/product - 切到手机端产品模式
/terminal - 切到远程终端模式
/terminal claude - 接入 Claude 终端
/terminal codex - 接入 Codex 终端
/terminal detach - 停止终端推送但保留会话
```

- [ ] **Step 5: Update docs**

Document:

```text
Product mode is the default.
Terminal mode is raw and may show large output.
Switching modes does not create a new conversation.
Terminal detach does not abort the underlying run.
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_surface_commands.py tests/test_config.py tests/test_telegram_handlers.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit only this task's files**

Run:

```bash
git add wlcodex/config.py wlcodex/telegram_app.py README.md docs tests/test_surface_commands.py
git commit -m "docs: document dual surface modes"
```

## Final Verification

- [ ] **Step 1: Run detect changes before final commit or PR**

Run:

```bash
npx gitnexus detect-changes --repo wlcodex --scope all
```

Expected: Changed symbols match the dual-surface scope.

- [ ] **Step 2: Run focused suite**

Run:

```bash
pytest tests/test_surface_core.py tests/test_surface_store.py tests/test_product_surface.py tests/test_terminal_surface.py tests/test_terminal_redaction.py tests/test_surface_commands.py tests/test_surface_recovery.py tests/test_dual_surface_integration.py -q
```

Expected: PASS.

- [ ] **Step 3: Run broader Telegram/runtime regression suite**

Run:

```bash
pytest tests/test_telegram_handlers.py tests/test_telegram_conversation_handlers.py tests/test_runtime_state_replay.py tests/test_runtime_projector.py tests/test_orchestration_runner.py -q
```

Expected: PASS.

- [ ] **Step 4: Manual smoke test**

Run the bot in a test chat and verify:

```text
普通消息 -> product mode
/terminal claude -> terminal mode
terminal input -> raw Claude/Codex path
/product -> product mode
next message -> product conversation path
```

Expected: same conversation id throughout; product and terminal messages do not
edit each other.

## Implementation Notes For Parallel Workers

- Product Surface workers must not edit `wlcodex/surfaces/terminal/*`.
- Terminal Surface workers must not edit `wlcodex/surfaces/product/*`.
- Core workers must keep APIs small and pure where possible.
- Telegram command workers must avoid changing rendering internals.
- Recovery workers must use event replay as source of truth.
- Any worker editing existing functions/classes must run GitNexus impact first.
- No worker may use find-and-replace for symbol renames; use GitNexus rename.
- Do not commit unrelated existing untracked files.
