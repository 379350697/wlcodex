# WLCodex Workbench Session Library And Task Internalization Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the Remote Workbench so one user Workbench persists until `/new`, tasks are internal execution tickets, Onsite never opens a dead session, and historical Codex/Claude sessions can be browsed, resumed, and continued.

**Architecture:** Keep the existing controller, runtime event store, Telegram app, terminal surface, Codex app-server backend, and Claude stream-json backend. Add a small Workbench session-library layer over existing `agent_runs` and terminal session refs, then route Telegram commands through the current Workbench identity, execution lane, and selected Agent Session.

**Tech Stack:** Python, pytest, SQLite ledger/runtime projections, Telegram bot handlers, existing WLCodex controller/orchestration runner, Codex app-server `thread/resume`, Claude Code `--resume`, GitNexus MCP/CLI for impact and change detection.

---

## Required Reading

- Spec: `docs/superpowers/specs/2026-05-20-wlcodex-workbench-session-library-and-task-internalization-repair-design.md`
- Existing Workbench spec: `docs/superpowers/specs/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-design.md`
- Existing Workbench plan: `docs/superpowers/plans/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-parallel-plan.md`
- Workbench models: `wlcodex/workbench/models.py`
- Workbench routing: `wlcodex/workbench/routing.py`
- Conversation callbacks: `wlcodex/conversation_callback.py`
- Telegram routing: `wlcodex/telegram_app.py`
- Controller: `wlcodex/controller.py`
- Task service: `wlcodex/task_service.py`
- Ledger: `wlcodex/db.py`
- Terminal manager: `wlcodex/surfaces/terminal/manager.py`
- Claude terminal adapter: `wlcodex/surfaces/terminal/claude_remote.py`
- Codex terminal adapter: `wlcodex/surfaces/terminal/codex_terminal.py`
- Runtime replay/recovery: `wlcodex/runtime_events.py`, `wlcodex/runtime_state.py`, `wlcodex/runtime_projector.py`, `wlcodex/recovery.py`, `wlcodex/main.py`

## Non-Negotiable Engineering Rules

- Run GitNexus impact analysis before editing any existing function, class, or method.
- Stop and report before editing if impact is HIGH or CRITICAL.
- Write the failing test before implementation.
- Do not change product semantics because a test is easier another way.
- Keep Task as internal execution state; do not add user copy that teaches users to manage task ids.
- Keep Workbench, Agent Session, Task, view mode, and execution mode as separate concepts.
- Do not use "tests passed" as release approval. Final Gate needs the full evidence list in this plan.

## Parallelization Model

Task 1 lands first because it fixes Workbench identity. Tasks 2 through 6 can run after Task 1 with disjoint ownership. Tasks 7 and 8 integrate and verify.

| Task | Purpose | Write ownership | Depends on |
| --- | --- | --- | --- |
| 1 | Workbench identity and callback actions | `wlcodex/telegram_app.py`, `wlcodex/conversation_callback.py`, `tests/test_workbench_telegram_routing.py` | none |
| 2 | Execution lane and Task internalization | `wlcodex/controller.py`, `wlcodex/task_service.py`, `tests/test_workbench_execution_modes.py`, `tests/test_task_service.py` | 1 |
| 3 | Agent Session Library projection | `wlcodex/workbench/sessions.py`, `wlcodex/workbench/rendering.py`, `tests/test_workbench_session_library.py` | 1 |
| 4 | Historical attach/resume | `wlcodex/surfaces/terminal/manager.py`, `wlcodex/surfaces/terminal/claude_remote.py`, `wlcodex/surfaces/terminal/codex_terminal.py`, `tests/test_workbench_onsite_terminal.py` | 3 |
| 5 | `/sessions`, menu, and user copy | `wlcodex/router.py`, `wlcodex/menu.py`, `wlcodex/status.py`, `wlcodex/telegram_app.py`, `tests/test_workbench_cockpit_menu.py`, `tests/test_telegram_handlers.py` | 3 |
| 6 | Execution-mode session persistence | `wlcodex/controller.py`, `wlcodex/runtime_projector.py`, `tests/test_workbench_execution_modes.py`, `tests/test_runtime_projector.py` | 2, 3 |
| 7 | Recovery and restart state | `wlcodex/runtime_events.py`, `wlcodex/runtime_state.py`, `wlcodex/recovery.py`, `wlcodex/main.py`, `tests/test_workbench_runtime_state.py`, `tests/test_recovery.py` | 3, 4, 6 |
| 8 | End-to-end closure and Final Gate evidence | integration tests, review docs, smoke docs | 1-7 |

## Task 1: Workbench Identity And Callback Actions

**Files:**
- Modify: `wlcodex/telegram_app.py`
- Modify only if needed: `wlcodex/conversation_callback.py`
- Test: `tests/test_workbench_telegram_routing.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target handle_conversation_callback --direction upstream
npx gitnexus impact --repo wlcodex --target TelegramHandlers --direction upstream
```

Expected: record direct callers, affected processes, and risk. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing regression for start-card identity**

Add this test to `tests/test_workbench_telegram_routing.py`:

```python
def test_start_card_callbacks_use_active_conversation_id_not_chat_id():
    from wlcodex.conversation_callback import decode_conversation_callback
    from wlcodex.telegram_app import TelegramHandlers

    handlers = TelegramHandlers.__new__(TelegramHandlers)
    buttons = handlers._render_start_card_buttons(conversation_id=99)

    callbacks = [
        decode_conversation_callback(button["callback_data"])
        for row in buttons
        for button in row
    ]

    assert {cb.conversation_id for cb in callbacks if cb is not None} == {99}
    assert {cb.action for cb in callbacks if cb is not None} == {
        "start_claude_onsite",
        "start_codex_onsite",
        "return_cockpit",
    }
```

Expected before implementation: FAIL if `_render_start_card_buttons` still accepts `chat_id` or embeds Telegram chat id.

- [ ] **Step 3: Write failing regression for known Onsite actions**

Add:

```python
@pytest.mark.asyncio
async def test_onsite_start_card_actions_are_recognized_by_telegram_callback():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from wlcodex.conversation_callback import encode_conversation_callback
    from wlcodex.telegram_app import TelegramHandlers

    query = SimpleNamespace(
        data=encode_conversation_callback(42, "return_cockpit"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=7001, message_id=10),
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=7001))

    handlers = TelegramHandlers.__new__(TelegramHandlers)
    handlers._ledger = MagicMock()
    handlers._controller = MagicMock()
    handlers._controller.handle_conversation_callback = AsyncMock(
        return_value=SimpleNamespace(text="已回到驾驶舱。", buttons=[])
    )
    handlers._terminal_manager = None
    handlers._runtime_store = None
    handlers.send_telegram = AsyncMock()

    await handlers._conversation_callback_impl(update, query, query.data)

    assert query.answer.await_count == 1
    assert "未知" not in query.edit_message_text.call_args.kwargs.get("text", "")
```

Expected before implementation: FAIL if these actions are treated as unknown or routed to the wrong conversation.

- [ ] **Step 4: Implement minimal identity fix**

Change `_render_start_card_buttons` to accept `conversation_id: int` and encode callback data with `encode_conversation_callback(conversation_id, action)`.

Required actions:

```python
"start_claude_onsite"
"start_codex_onsite"
"return_cockpit"
```

Do not use Telegram chat id as a conversation id.

- [ ] **Step 5: Route known start-card actions**

Handle the three actions in the Telegram callback path or controller callback path:

```text
start_claude_onsite -> start/attach Claude Onsite for this Workbench
start_codex_onsite -> start/attach Codex Onsite for this Workbench
return_cockpit -> switch view to Cockpit for this Workbench
```

If the implementation cannot start a real session yet, the action must return a product next-step card, not "unknown action".

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_workbench_telegram_routing.py -q
```

Expected: PASS.

## Task 2: Execution Lane And Task Internalization

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/task_service.py`
- Test: `tests/test_workbench_execution_modes.py`
- Test: `tests/test_task_service.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target handle_conversation_text --direction upstream
npx gitnexus impact --repo wlcodex --target handle_codex_direct --direction upstream
npx gitnexus impact --repo wlcodex --target handle_claude_direct --direction upstream
npx gitnexus impact --repo wlcodex --target reserve_task --direction upstream
```

Expected: record risk. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing lane tests**

Add:

```python
def test_running_cockpit_text_does_not_create_competing_hidden_task(controller_with_busy_workbench):
    ctrl, ledger, service, chat_id, active = controller_with_busy_workbench

    before = service.list_tasks()
    response = ctrl.handle_conversation_text("补充：顺便检查 /terminal", {"chat_id": chat_id})
    after = service.list_tasks()

    assert len(after) == len(before)
    assert "追加" in response.text or "选择" in response.text


def test_workspace_busy_user_copy_hides_task_ids(controller_with_busy_workbench):
    ctrl, ledger, service, chat_id, active = controller_with_busy_workbench

    response = ctrl.handle("/claude 继续改", {"chat_id": chat_id})

    forbidden = ["任务 #", "阻塞者", "队列位置", "task #", "blocking task"]
    assert all(term not in response.text for term in forbidden)
```

Use the existing fake controller fixtures in `tests/test_workbench_execution_modes.py`; if the exact fixture does not exist, create a local fixture that reserves one running task in the active Workbench.

- [ ] **Step 3: Write failing lifecycle test**

Add:

```python
@pytest.mark.asyncio
async def test_claude_direct_hidden_task_always_releases_workspace_lock(ctrl_with_claude):
    ctrl, ledger, service, chat_id = ctrl_with_claude

    response = await ctrl.handle_claude_direct_text("修一下现场入口", {"chat_id": chat_id})
    tasks = service.list_tasks()
    hidden = [task for task in tasks if "修一下现场入口" in task.title or "修一下现场入口" in task.prompt]

    assert hidden
    assert hidden[-1].status in {"done", "failed", "aborted", "orphaned"}
    assert service.blocker_for_workspace("wlcodex") is None
```

Expected before implementation: FAIL if hidden Claude-only tasks remain queued/running or keep the workspace blocked.

- [ ] **Step 4: Implement execution lane policy**

Implement a single helper in `controller.py`:

```python
def _execution_lane_decision(self, active, incoming_kind: str) -> str:
    """Return idle, append, explicit_choice, or onsite_input for this Workbench."""
```

Rules:

```text
Cockpit ordinary text + idle -> idle
Cockpit ordinary text + active task/run -> append
explicit /codex or /claude + active task/run -> explicit_choice
Onsite text + selected session -> onsite_input
```

Keep it small and covered by tests.

- [ ] **Step 5: Hide task internals from normal busy copy**

Replace normal user busy copy with product copy:

```text
当前工作台正在执行。你可以追加到当前执行，等它结束，停止当前后执行，或新开工作台。
```

Do not mention task ids, blockers, or queue positions outside diagnostic commands.

- [ ] **Step 6: Close lifecycle paths**

Ensure every internal task path used by direct Codex/Claude moves to a terminal status on success, error, cancellation, and restart orphaning.

- [ ] **Step 7: Run focused tests**

Run:

```bash
pytest tests/test_workbench_execution_modes.py tests/test_task_service.py -q
```

Expected: PASS.

## Task 3: Agent Session Library Projection

**Files:**
- Create: `wlcodex/workbench/sessions.py`
- Modify: `wlcodex/workbench/rendering.py`
- Test: `tests/test_workbench_session_library.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target list_recent_agent_runs --direction upstream
npx gitnexus impact --repo wlcodex --target update_agent_run_status --direction upstream
```

Expected: record risk. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing projection tests**

Create `tests/test_workbench_session_library.py`:

```python
from types import SimpleNamespace

from wlcodex.workbench.sessions import (
    AgentSessionLibrary,
    AgentSessionResumability,
)


class FakeLedger:
    def list_recent_agent_runs(self, conversation_id, limit=50):
        assert conversation_id == 42
        return [
            SimpleNamespace(
                id=9,
                agent="claude",
                role="implementation",
                status="done",
                external_session_id="cl-secret-1",
                completion_summary="修复 Telegram 接管逻辑",
                created_at="2026-05-20T11:08:00+08:00",
                updated_at="2026-05-20T11:12:00+08:00",
            ),
            SimpleNamespace(
                id=8,
                agent="codex",
                role="verification",
                status="done",
                external_session_id="cx-secret-1",
                completion_summary="验收 Workbench 语义",
                created_at="2026-05-20T10:42:00+08:00",
                updated_at="2026-05-20T10:45:00+08:00",
            ),
        ]


def test_session_library_lists_user_safe_agent_sessions():
    library = AgentSessionLibrary(FakeLedger())

    sessions = library.list_for_workbench(42)

    assert [s.agent for s in sessions] == ["claude", "codex"]
    assert sessions[0].title == "修复 Telegram 接管逻辑"
    assert sessions[0].resumability is AgentSessionResumability.RESUMABLE
    assert "cl-secret-1" not in sessions[0].user_label
    assert "cx-secret-1" not in sessions[1].user_label


def test_session_library_returns_summary_only_when_no_resume_reference():
    class LedgerWithoutIds(FakeLedger):
        def list_recent_agent_runs(self, conversation_id, limit=50):
            run = super().list_recent_agent_runs(conversation_id, limit)[0]
            run.external_session_id = ""
            return [run]

    sessions = AgentSessionLibrary(LedgerWithoutIds()).list_for_workbench(42)

    assert sessions[0].resumability is AgentSessionResumability.SUMMARY_ONLY
```

Expected before implementation: FAIL because `wlcodex.workbench.sessions` does not exist.

- [ ] **Step 3: Implement session models**

Create:

```python
from dataclasses import dataclass
from enum import Enum


class AgentSessionResumability(Enum):
    LIVE = "live"
    RESUMABLE = "resumable"
    SUMMARY_ONLY = "summary_only"


@dataclass(frozen=True)
class AgentSessionSummary:
    conversation_id: int
    agent: str
    title: str
    status: str
    resumability: AgentSessionResumability
    user_label: str
    internal_ref: str
    source_run_id: int
```

`internal_ref` is internal only. Renderers must not print it.

- [ ] **Step 4: Implement `AgentSessionLibrary`**

Required methods:

```python
class AgentSessionLibrary:
    def __init__(self, ledger):
        self._ledger = ledger

    def list_for_workbench(self, conversation_id: int, limit: int = 20) -> list[AgentSessionSummary]:
        ...

    def get_for_workbench(self, conversation_id: int, source_run_id: int) -> AgentSessionSummary | None:
        ...
```

Projection rules:

```text
agent must be codex or claude
external session/thread reference present -> RESUMABLE
active terminal ref, when supplied by Task 4 -> LIVE
no resume reference -> SUMMARY_ONLY
title comes from completion_summary, prompt_packet_summary, role, or safe fallback
newest first
do not duplicate same agent + same internal_ref in one list
```

- [ ] **Step 5: Add user-safe renderer**

Add a pure render helper in `wlcodex/workbench/rendering.py`:

```python
def render_session_library(sessions: list[AgentSessionSummary]) -> str:
    ...
```

Rendered text must use "历史现场", "Claude 现场", "Codex 现场", "可继续", "可回顾", and "可从摘要新开".

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_workbench_session_library.py -q
```

Expected: PASS.

## Task 4: Historical Attach And Resume

**Files:**
- Modify: `wlcodex/surfaces/terminal/manager.py`
- Modify if needed: `wlcodex/surfaces/terminal/claude_remote.py`
- Modify if needed: `wlcodex/surfaces/terminal/codex_terminal.py`
- Test: `tests/test_workbench_onsite_terminal.py`
- Test: `tests/test_terminal_surface.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target TerminalSessionManager --direction upstream
npx gitnexus impact --repo wlcodex --target ClaudeTerminalAdapter --direction upstream
npx gitnexus impact --repo wlcodex --target CodexTerminalAdapter --direction upstream
```

Expected: record risk. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing tests for picker and attach**

Add to `tests/test_workbench_onsite_terminal.py`:

```python
def test_open_onsite_with_history_returns_session_picker(fake_terminal_manager, fake_session_library):
    decision = fake_terminal_manager.open_for_conversation(
        conversation_id=42,
        preferred_agent="",
        historical_sessions=fake_session_library.list_for_workbench(42),
    )

    assert decision.kind.value == "session_picker"
    assert [s.agent for s in decision.available_sessions] == ["claude", "codex"]


def test_attach_historical_session_creates_terminal_ref(fake_terminal_manager, claude_session_summary):
    ref = fake_terminal_manager.attach_historical(
        conversation_id=42,
        session=claude_session_summary,
    )

    assert ref.conversation_id == 42
    assert ref.agent == "claude"
    assert ref.external_session_id == claude_session_summary.internal_ref
    assert ref.status == "attached"
```

Expected before implementation: FAIL because `session_picker` and `attach_historical` are absent.

- [ ] **Step 3: Write failing tests for resume adapters**

Add:

```python
@pytest.mark.asyncio
async def test_historical_claude_resume_uses_stored_session_id(fake_claude_backend):
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter

    adapter = ClaudeTerminalAdapter(fake_claude_backend)

    await adapter.send_input_by_session_id("cl-secret-1", "继续修菜单")

    assert fake_claude_backend.inputs == [("cl-secret-1", "继续修菜单")]


@pytest.mark.asyncio
async def test_historical_codex_resume_uses_stored_thread_id(fake_codex_backend):
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

    adapter = CodexTerminalAdapter(fake_codex_backend)

    await adapter.send_input_by_thread_id("cx-thread-1", "继续验收")

    assert fake_codex_backend.steered == [("cx-thread-1", "继续验收")]
```

Expected: Claude/Codex adapter convenience methods may already pass. Keep these tests as contract coverage.

- [ ] **Step 4: Extend Onsite decision model**

Add decision kind:

```python
SESSION_PICKER = "session_picker"
```

Extend `OnsiteDecision` with:

```python
available_sessions: tuple[AgentSessionSummary, ...] = ()
```

`open_for_conversation` decision order:

```text
attached session -> AUTO_OPEN
historical resumable sessions -> SESSION_PICKER
no sessions -> START_CARD
```

- [ ] **Step 5: Add historical attach**

Add:

```python
def attach_historical(self, *, conversation_id: int, session: AgentSessionSummary) -> TerminalSessionRef:
    ...
```

Rules:

```text
SUMMARY_ONLY sessions cannot attach directly
claude uses strategy "stream_json"
codex uses strategy "app_server"
external_session_id is the internal resume reference
existing attached ref for same agent/ref is reused
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_workbench_onsite_terminal.py tests/test_terminal_surface.py -q
```

Expected: PASS.

## Task 5: `/sessions`, Menu, And User Copy

**Files:**
- Modify: `wlcodex/router.py`
- Modify: `wlcodex/menu.py`
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_workbench_cockpit_menu.py`
- Test: `tests/test_telegram_handlers.py`
- Test: `tests/test_router.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target parse_command --direction upstream
npx gitnexus impact --repo wlcodex --target render_help --direction upstream
npx gitnexus impact --repo wlcodex --target codex_sessions --direction upstream
```

Expected: record risk. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing menu/copy tests**

Add:

```python
def test_mobile_menu_uses_product_entries_not_technical_task_list():
    from wlcodex.menu import render_main_menu

    text = render_main_menu()

    assert "新工作台" in text
    assert "接管现场" in text
    assert "历史现场" in text
    assert "/task" not in text
    assert "队列位置" not in text


def test_sessions_output_hides_internal_ids(fake_handlers_with_session_library):
    text = fake_handlers_with_session_library.render_sessions_for_chat(chat_id=7001)

    assert "历史现场" in text
    assert "Claude 现场" in text
    assert "cl-secret-1" not in text
    assert "external_session_id" not in text
    assert "thread id" not in text
```

Expected before implementation: FAIL if menu still presents a technical command catalog or session output prints internal ids.

- [ ] **Step 3: Preserve command compatibility**

Keep parser support for:

```text
/sessions
/codex-sessions
```

But help/menu should prefer "历史现场".

- [ ] **Step 4: Route `/sessions` through Session Library**

`TelegramHandlers.codex_sessions` should render the Workbench session library for the active conversation. If no Workbench exists:

```text
当前还没有工作台。发送 /new 开始一个新的工作台。
```

If Workbench exists but no sessions:

```text
这个工作台还没有历史现场。你可以先让 Codex 分析，或让 Claude 开始执行。
```

- [ ] **Step 5: Add session action buttons**

For each session:

```text
查看回顾
接管现场
继续修改
让 Codex 验收
从摘要新开
```

Only show actions that apply to the session status/resumability.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_workbench_cockpit_menu.py tests/test_telegram_handlers.py tests/test_router.py -q
```

Expected: PASS.

## Task 6: Execution-Mode Session Persistence

**Files:**
- Modify: `wlcodex/controller.py`
- Modify if needed: `wlcodex/runtime_projector.py`
- Test: `tests/test_workbench_execution_modes.py`
- Test: `tests/test_runtime_projector.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target handle_claude_direct --direction upstream
npx gitnexus impact --repo wlcodex --target handle_codex_direct --direction upstream
npx gitnexus impact --repo wlcodex --target _project_session_id --direction upstream
```

Expected: record risk. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing Claude-only persistence test**

Add:

```python
@pytest.mark.asyncio
async def test_claude_direct_persists_session_id_and_offers_codex_verification(ctrl_with_streaming_claude):
    ctrl, ledger, chat_id = ctrl_with_streaming_claude

    response = await ctrl.handle_claude_direct_text("修复历史现场", {"chat_id": chat_id})
    runs = ledger.list_recent_agent_runs(response.conversation_id, limit=5)
    claude_runs = [run for run in runs if run.agent == "claude"]

    assert claude_runs[0].external_session_id == "cl-session-123"
    assert "让 Codex 验收" in response.text
    assert not any(run.agent == "codex" and run.role == "verification" for run in runs)
```

Expected before implementation: FAIL if Claude session id is not copied from stream/result into `agent_runs`.

- [ ] **Step 3: Write failing Codex session persistence test**

Add:

```python
@pytest.mark.asyncio
async def test_codex_direct_persists_thread_reference_for_history(ctrl_with_codex_app_server):
    ctrl, ledger, chat_id = ctrl_with_codex_app_server

    response = await ctrl.handle_codex_direct_text("只分析现场库", {"chat_id": chat_id})
    runs = ledger.list_recent_agent_runs(response.conversation_id, limit=5)
    codex_runs = [run for run in runs if run.agent == "codex"]

    assert codex_runs[0].external_session_id == "cx-thread-123"
    assert not any(run.agent == "claude" for run in runs)
```

Expected before implementation: FAIL if Codex-only does not persist thread/session reference.

- [ ] **Step 4: Persist Claude session id**

In Claude direct streaming and non-streaming paths:

```text
track latest non-empty stream event session_id
use final AgentResult.session_id when present
call update_agent_run_status(..., external_session_id=session_id)
emit runtime event payload with session_id or external_session_id
```

Do not expose the raw id in Telegram text.

- [ ] **Step 5: Persist Codex thread reference**

When Codex direct starts or resumes an app-server thread:

```text
store thread id as the internal session reference for that Codex agent run
keep turn id separate if needed for steering
do not print raw thread id in normal user copy
```

- [ ] **Step 6: Keep direct-mode semantics strict**

Assertions:

```text
/codex does not call Claude
/claude does not call Codex analysis
/claude completion does not auto-start Codex verification
```

Only the explicit "让 Codex 验收" action starts verification.

- [ ] **Step 7: Run focused tests**

Run:

```bash
pytest tests/test_workbench_execution_modes.py tests/test_runtime_projector.py -q
```

Expected: PASS.

## Task 7: Recovery And Restart State

**Files:**
- Modify: `wlcodex/runtime_events.py`
- Modify: `wlcodex/runtime_state.py`
- Modify: `wlcodex/recovery.py`
- Modify: `wlcodex/main.py`
- Test: `tests/test_workbench_runtime_state.py`
- Test: `tests/test_recovery.py`

- [ ] **Step 1: Run impact analysis**

Run:

```bash
npx gitnexus impact --repo wlcodex --target replay_runtime_state --direction upstream
npx gitnexus impact --repo wlcodex --target find_non_terminal_agent_runs --direction upstream
npx gitnexus impact --repo wlcodex --target mark_startup_recovery --direction upstream
```

Expected: record risk. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing recovery tests**

Add:

```python
def test_restart_recovery_restores_workbench_view_mode_and_selected_session(runtime_store_with_workbench_events):
    state = replay_runtime_state(runtime_store_with_workbench_events, conversation_id=42)

    assert state.view.value == "onsite"
    assert state.execution_mode.value == "claude_direct"
    assert state.onsite_external_session_id == "cl-session-123"
    assert state.onsite_cursor > 0


def test_recovery_keeps_historical_sessions_browsable_after_orphaning(ledger_with_orphaned_agent_run):
    from wlcodex.workbench.sessions import AgentSessionLibrary, AgentSessionResumability

    sessions = AgentSessionLibrary(ledger_with_orphaned_agent_run).list_for_workbench(42)

    assert sessions
    assert sessions[0].agent == "claude"
    assert sessions[0].resumability in {
        AgentSessionResumability.RESUMABLE,
        AgentSessionResumability.SUMMARY_ONLY,
    }
```

Expected before implementation: FAIL if recovery drops selected session or history.

- [ ] **Step 3: Emit and replay selected session events**

Runtime state must preserve:

```text
view mode
execution mode
cockpit cursor
onsite cursor
selected agent
selected Agent Session internal reference
orphaned session status
```

Use existing runtime event types when possible. Add narrow event types only if current events cannot express the state.

- [ ] **Step 4: Mark orphaned without deleting history**

On startup:

```text
non-terminal task/run -> failed or orphaned
workspace lock released
Agent Session remains browsable
resume id remains available when present
summary-only fallback remains available when no resume id exists
```

- [ ] **Step 5: Restore pending verification action**

If the latest Claude-only run completed and no Codex verification has been started, Cockpit state after restart must still be able to render "让 Codex 验收".

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_workbench_runtime_state.py tests/test_recovery.py -q
```

Expected: PASS.

## Task 8: End-To-End Closure And Final Gate Evidence

**Files:**
- Modify tests only unless Task 1-7 revealed missing coverage:
  - `tests/test_workbench_remote_integration.py`
  - `tests/test_dual_surface_integration.py`
  - `tests/test_live_telegram_smoke.py`
- Create review docs under `docs/superpowers/reviews/` after implementation.
- Create smoke notes under `docs/smoke/` after live verification.

- [ ] **Step 1: Write end-to-end Workbench continuity test**

Add an integration test:

```python
@pytest.mark.asyncio
async def test_workbench_continues_until_new_across_cockpit_onsite_and_sessions(app_harness):
    chat_id = 7001

    first = await app_harness.send(chat_id, "/new 修复工作台")
    conv_id = first.conversation_id

    await app_harness.send(chat_id, "先按默认流程分析")
    await app_harness.send(chat_id, "/terminal")
    await app_harness.send(chat_id, "/product")
    await app_harness.send(chat_id, "/sessions")

    assert app_harness.active_conversation_id(chat_id) == conv_id

    second = await app_harness.send(chat_id, "/new 新问题")
    assert second.conversation_id != conv_id
```

- [ ] **Step 2: Write end-to-end historical resume test**

Add:

```python
@pytest.mark.asyncio
async def test_historical_claude_session_can_be_selected_and_continued(app_harness):
    chat_id = 7001

    await app_harness.send(chat_id, "/new 历史现场")
    await app_harness.send(chat_id, "/claude 修复菜单")
    sessions = await app_harness.send(chat_id, "/sessions")

    assert "Claude 现场" in sessions.text
    assert "让 Codex 验收" in sessions.text

    await app_harness.tap(chat_id, "继续修改", session_agent="claude")

    assert app_harness.fake_claude_backend.inputs[-1][0] == "cl-session-123"
    assert app_harness.active_conversation_title(chat_id) == "历史现场"
```

- [ ] **Step 3: Write final user-copy scan**

Add or update a rendered-copy scan test:

```python
def test_normal_user_copy_does_not_expose_internal_terms(rendered_user_copy_samples):
    forbidden = [
        "terminal.enabled",
        "external_session_id",
        "session id",
        "thread id",
        "runtime_events",
        "agent_run",
        "task #",
        "任务 #",
        "阻塞者",
        "队列位置",
    ]

    combined = "\n".join(rendered_user_copy_samples)

    for term in forbidden:
        assert term not in combined
```

- [ ] **Step 4: Run targeted Workbench suite**

Run:

```bash
pytest \
  tests/test_workbench_core.py \
  tests/test_workbench_cockpit_menu.py \
  tests/test_workbench_commands.py \
  tests/test_workbench_onsite_terminal.py \
  tests/test_workbench_execution_modes.py \
  tests/test_workbench_runtime_state.py \
  tests/test_workbench_telegram_routing.py \
  tests/test_workbench_remote_integration.py \
  tests/test_workbench_session_library.py \
  -q
```

Expected: all pass.

- [ ] **Step 5: Run existing related suite**

Run:

```bash
pytest \
  tests/test_controller_flow.py \
  tests/test_telegram_handlers.py \
  tests/test_terminal_surface.py \
  tests/test_dual_surface_integration.py \
  tests/test_runtime_projector.py \
  tests/test_runtime_state_replay.py \
  tests/test_recovery.py \
  tests/test_router.py \
  tests/test_status.py \
  tests/test_task_service.py \
  -q
```

Expected: all pass.

- [ ] **Step 6: Run hygiene checks**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` has no output. `git status --short` lists only expected implementation, test, spec, plan, review, and smoke files.

- [ ] **Step 7: Run GitNexus change detection**

Run GitNexus MCP:

```text
detect_changes(repo="wlcodex", scope="all")
```

Expected: affected scope matches Workbench/Telegram/controller/runtime/terminal/session-library repair. Any HIGH or CRITICAL risk must be reviewed before Final Gate.

- [ ] **Step 8: Live Telegram smoke**

Run live smoke after deployment:

```text
/new 真人历史现场 smoke
普通文本：按默认流程分析并修改一个小问题
/terminal
/product
/claude Reply exactly with: claude only ok
点击：让 Codex 验收
/sessions
点击最近 Claude 现场：查看回顾
点击最近 Claude 现场：接管现场
输入：continue from this historical session
/product
/codex Reply exactly with: codex only ok
/sessions
/new 第二个工作台
```

Expected:

```text
same Workbench id before second /new
no dead conversation message
no task/blocker/queue ids in user copy
/claude does not auto-run Codex until verification action
/codex does not call Claude
historical Claude session resumes through stored session reference
historical Codex session appears in library
Onsite input does not enter Cockpit controller
Cockpit return does not replay raw terminal
```

- [ ] **Step 9: Independent reviews**

For each Task 1-8 create two review docs:

```text
docs/superpowers/reviews/2026-05-20-task<N>-spec-compliance-review.md
docs/superpowers/reviews/2026-05-20-task<N>-code-quality-review.md
```

Each must contain:

```text
Verdict: PASS or BLOCKED
Scope reviewed
Spec criteria checked
Files reviewed
Evidence checked
Risks
Required fixes if BLOCKED
```

No task can be Final Gate PASS without both independent PASS reviews.

- [ ] **Step 10: Acceptance criteria comparison table**

Create or update:

```text
docs/superpowers/reviews/2026-05-20-workbench-session-library-final-gate.md
```

Include all criteria from the spec table with PASS/FAIL and evidence links.

- [ ] **Step 11: Final Gate decision rule**

Final Gate output must be:

```text
Verdict: RELEASE_CANDIDATE or BLOCKED
Closed-loop checklist: PASS/FAIL per item
Test evidence: command and result summary
Semantic drift review: none or details
Remaining blockers: task owner
Release note summary: user-facing change
```

If any required evidence is missing, the only valid verdict is `BLOCKED`.

## Review Boundaries

Spec Compliance Reviewer checks:

```text
Does the implementation satisfy the exact Workbench/Agent Session/Task semantics?
Does it preserve default Codex -> Claude -> Codex?
Does it keep /codex and /claude strict?
Does it support historical session browse/resume?
Does it hide internal ids from normal user copy?
```

Code Quality Reviewer checks:

```text
Are changes minimal and locally coherent?
Are lifecycle paths terminal and lock-safe?
Are callbacks using conversation identity correctly?
Are tests meaningful beyond happy path?
Are runtime/recovery changes replay-safe?
```

## Final Evidence Commands

These exact evidence categories are mandatory:

```bash
pytest tests/test_workbench_core.py tests/test_workbench_cockpit_menu.py tests/test_workbench_commands.py tests/test_workbench_onsite_terminal.py tests/test_workbench_execution_modes.py tests/test_workbench_runtime_state.py tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py tests/test_workbench_session_library.py -q
pytest tests/test_controller_flow.py tests/test_telegram_handlers.py tests/test_terminal_surface.py tests/test_dual_surface_integration.py tests/test_runtime_projector.py tests/test_runtime_state_replay.py tests/test_recovery.py tests/test_router.py tests/test_status.py tests/test_task_service.py -q
git diff --check
git status --short
```

And GitNexus MCP:

```text
detect_changes(repo="wlcodex", scope="all")
```

And rendered user-copy scan:

```text
forbidden terms absent from normal Telegram copy:
terminal.enabled
external_session_id
session id
thread id
runtime_events
agent_run
task #
任务 #
阻塞者
队列位置
```

## Handoff Summary

Implement this plan as a repair, not a rewrite.

The final product behavior is:

```text
One Workbench until /new.
Many historical Codex/Claude Agent Sessions inside that Workbench.
Many internal Tasks underneath those sessions.
One execution lane prevents workspace fights.
Cockpit and Onsite are views, not separate sessions.
Users can browse and resume history without seeing internal ids.
```
