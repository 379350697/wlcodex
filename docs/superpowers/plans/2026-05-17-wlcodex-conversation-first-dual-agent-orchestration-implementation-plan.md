# WLCodex Conversation-First Dual-Agent Orchestration Implementation Plan

> Superseded for product implementation: follow the 2026-05-20 Remote
> Workbench repair plans instead. "Conversation" copy below maps to the
> current Workbench model; task-led user flows are legacy diagnostics only.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build WLCodex v2 as a conversation-first chief-engineer Telegram cockpit with Codex direct mode, Claude direct mode, Codex-led orchestration, humanized menus, streaming, and compact model context packets.

**Architecture:** Preserve TaskService, Ledger, AppServerCodexBackend, approvals, locks, worktrees, and inspection as the internal runtime. Add a conversation layer outside the existing command controller, a compact context packet builder, direct-agent adapters, and a Codex-Claude-Codex orchestrator. Keep legacy task commands working while making plain text and menu commands the primary UX.

**Tech Stack:** Python 3.12, python-telegram-bot, SQLite via existing `Ledger`, Codex app-server JSON-RPC, Claude Code subprocess adapter, pytest, existing fake backend test style.

---

## File Structure

Create:

- `wlcodex/conversation.py` - conversation state dataclasses, modes, helper decisions.
- `wlcodex/context_packets.py` - compact prompt packet builders and budget enforcement.
- `wlcodex/agent_backend.py` - shared agent result and agent backend protocol.
- `wlcodex/claude_backend.py` - Claude Code subprocess adapter and fake-friendly event normalization.
- `wlcodex/orchestrator.py` - chief-engineer routing and verification loop.
- `wlcodex/menu.py` - Telegram BotCommands definitions and registration helper.
- `wlcodex/streaming.py` - throttled Telegram edit renderer.
- `tests/test_conversation_state.py`
- `tests/test_context_packets.py`
- `tests/test_conversation_router.py`
- `tests/test_telegram_conversation_handlers.py`
- `tests/test_agent_backend.py`
- `tests/test_claude_backend.py`
- `tests/test_orchestrator.py`
- `tests/test_streaming.py`

Modify:

- `wlcodex/models.py` - add conversation, agent run, orchestration run dataclasses and enums.
- `wlcodex/db.py` - add SQLite tables and Ledger methods for conversations and orchestration records.
- `wlcodex/config.py` - add conversation, orchestration, Claude, context budget, streaming, menu config.
- `wlcodex/router.py` - add new command dataclasses and parsing for `/new`, `/codex`, `/claude`, `/auto`, `/stop`, `/switch`, `/model`, `/verify`.
- `wlcodex/controller.py` - keep legacy command handling and delegate new conversation commands.
- `wlcodex/telegram_app.py` - register BotCommands, add non-command MessageHandler, add typing/streaming integration.
- `wlcodex/status.py` - add conversation status, session list, and concise help renderers.
- `wlcodex/main.py` - wire new services when config enables them.
- `config/wlcodex.example.toml` - document new config sections.
- `README.md` - explain new primary conversation UX and legacy advanced commands.

Do not modify:

- `wlcodex/jsonrpc.py` unless a test proves the existing backend cannot support the new path.
- `wlcodex/task_service.py` unless a small adapter method is required for a current-conversation lookup.
- Existing tests except to update expected help/status text when new UX intentionally changes it.

## Task 1: Add Conversation Data Models and Ledger Persistence

**Files:**

- Modify: `wlcodex/models.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_conversation_state.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Add failing tests for conversation persistence**

Add tests proving Ledger can create, retrieve, update, and archive a conversation session:

```python
def test_ledger_creates_and_updates_conversation(tmp_path):
    ledger = Ledger(tmp_path / "wlcodex.sqlite3").open()
    convo = ledger.create_conversation(
        chat_id=100,
        user_id=200,
        title="登录 bug",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )

    assert convo.id > 0
    assert convo.chat_id == 100
    assert convo.mode == "chief_engineer"

    updated = ledger.update_conversation_summary(
        convo.id,
        "用户要修复登录空指针，要求 Codex 验收。",
    )

    assert updated.conversation_summary.startswith("用户要修复")
```

Expected initial result: FAIL because the Ledger methods and models do not exist.

- [ ] **Step 2: Add model enums and dataclasses**

Add these concepts to `wlcodex/models.py`:

```python
class ConversationMode(StrEnum):
    CHIEF_ENGINEER = "chief_engineer"
    CODEX_DIRECT = "codex_direct"
    CLAUDE_DIRECT = "claude_direct"

class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"

class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"

class OrchestrationStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_USER = "needs_user"
    ABORTED = "aborted"
```

Add dataclasses:

- `ConversationSession`
- `AgentRun`
- `OrchestrationRun`
- `OrchestrationDecision`

Use the fields listed in the design spec.

- [ ] **Step 3: Add SQLite migrations and Ledger methods**

Add tables:

- `conversation_sessions`
- `agent_runs`
- `orchestration_runs`
- `orchestration_decisions`

Add Ledger methods:

- `create_conversation(...)`
- `get_conversation(conversation_id)`
- `get_active_conversation(chat_id)`
- `set_active_conversation_mode(conversation_id, mode)`
- `set_conversation_workspace(conversation_id, workspace_alias)`
- `update_conversation_summary(conversation_id, summary)`
- `archive_conversation(conversation_id)`
- `create_agent_run(...)`
- `update_agent_run_status(...)`
- `create_orchestration_run(...)`
- `record_orchestration_decision(...)`

- [ ] **Step 4: Run persistence tests**

Run:

```bash
pytest tests/test_conversation_state.py tests/test_db.py -q
```

Expected: PASS for new conversation persistence tests and existing DB tests.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/models.py wlcodex/db.py tests/test_conversation_state.py tests/test_db.py
git commit -m "feat: add conversation persistence"
```

## Task 2: Add Compact Context Packet Builders

**Files:**

- Create: `wlcodex/context_packets.py`
- Test: `tests/test_context_packets.py`

- [ ] **Step 1: Add failing tests for packet trimming and no transcript dump**

Test cases:

```python
def test_codex_to_claude_packet_excludes_full_transcript():
    packet = build_claude_handoff_packet(
        user_goal="修复登录 bug",
        codex_analysis="根因是 auth.py 没有处理 user.name 为空。",
        implementation_steps=["修改 auth.py", "补 tests/test_auth.py"],
        acceptance_criteria=["空用户不崩溃", "测试通过"],
        telegram_transcript="USER: " + "很长聊天 " * 1000,
        budget=ContextBudget(codex_to_claude_tokens=300),
    )

    rendered = packet.render()
    assert "很长聊天" not in rendered
    assert "修复登录 bug" in rendered
    assert "修改 auth.py" in rendered
```

Expected initial result: FAIL because the packet module does not exist.

- [ ] **Step 2: Implement packet dataclasses**

Create:

- `ContextBudget`
- `ContextPacket`
- `CodexAnalysisPacket`
- `ClaudeHandoffPacket`
- `CodexVerificationPacket`

Each packet exposes:

- `render() -> str`
- `summary() -> str`
- `within_budget() -> bool`

Budget enforcement can start with a deterministic character-to-token approximation:

```python
def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)
```

- [ ] **Step 3: Implement builders**

Functions:

- `build_codex_analysis_packet(...)`
- `build_claude_handoff_packet(...)`
- `build_codex_verification_packet(...)`
- `trim_to_budget(text, max_tokens)`

Rules:

- Never include raw Telegram transcript by default.
- Include user goal, current request, workspace, constraints, relevant files, and acceptance criteria.
- Include diff summaries or targeted excerpts, not full diffs by default.

- [ ] **Step 4: Run packet tests**

Run:

```bash
pytest tests/test_context_packets.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/context_packets.py tests/test_context_packets.py
git commit -m "feat: add compact context packets"
```

## Task 3: Add Conversation Commands and Routing

**Files:**

- Create: `wlcodex/conversation.py`
- Modify: `wlcodex/router.py`
- Test: `tests/test_conversation_router.py`
- Test: `tests/test_router.py`

- [ ] **Step 1: Add failing parser tests**

Test these cases:

```python
def test_parse_new_conversation_command():
    assert parse_command("/new").__class__.__name__ == "NewConversationCommand"

def test_parse_codex_direct_command():
    cmd = parse_command("/codex 分析这个模块")
    assert cmd.prompt == "分析这个模块"

def test_parse_claude_direct_command():
    cmd = parse_command("/claude 修改 README")
    assert cmd.prompt == "修改 README"

def test_parse_auto_command():
    cmd = parse_command("/auto 修复登录 bug")
    assert cmd.prompt == "修复登录 bug"

def test_legacy_task_command_still_works():
    cmd = parse_command("/task wlcodex 修复 bug")
    assert cmd.workspace_alias == "wlcodex"
```

Expected initial result: FAIL for new commands and PASS for the legacy control case.

- [ ] **Step 2: Add command dataclasses**

Add:

- `NewConversationCommand`
- `CodexDirectCommand`
- `ClaudeDirectCommand`
- `AutoModeCommand`
- `StopCurrentCommand`
- `SwitchWorkspaceCommand`
- `ModelCommand`
- `VerifyCommand`

- [ ] **Step 3: Implement parser branches**

Rules:

- `/new` accepts optional title text.
- `/codex <prompt>` requires a prompt.
- `/claude <prompt>` requires a prompt.
- `/auto <prompt>` requires a prompt.
- `/stop` maps to current conversation stop, not task ID.
- `/switch <workspace>` accepts a workspace alias.
- `/model` with no argument shows selection; `/model <name>` sets active model.
- `/verify` accepts optional text and targets the latest conversation run by default.

- [ ] **Step 4: Add conversation helpers**

`wlcodex/conversation.py` should contain pure helpers:

- `default_title(prompt: str) -> str`
- `mode_from_command(command) -> ConversationMode`
- `is_direct_agent_mode(mode) -> bool`

- [ ] **Step 5: Run router tests**

Run:

```bash
pytest tests/test_conversation_router.py tests/test_router.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/conversation.py wlcodex/router.py tests/test_conversation_router.py tests/test_router.py
git commit -m "feat: add conversation command routing"
```

## Task 4: Add Menu Commands and Humanized Renderers

**Files:**

- Create: `wlcodex/menu.py`
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_telegram_conversation_handlers.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Add failing tests for menu contents**

Assert menu order:

```python
def test_primary_bot_commands_are_human_first():
    commands = build_bot_commands()
    names = [cmd.command for cmd in commands]
    assert names[:5] == ["new", "codex", "claude", "auto", "stop"]
    assert "task" not in names
```

Expected initial result: FAIL because `wlcodex/menu.py` does not exist.

- [ ] **Step 2: Implement menu definitions**

Create `build_bot_commands()` returning Telegram `BotCommand` objects or plain command pairs behind a compatibility function.

Menu order:

1. `new`
2. `codex`
3. `claude`
4. `auto`
5. `stop`
6. `status`
7. `sessions`
8. `switch`
9. `model`
10. `diff`
11. `files`
12. `verify`
13. `health`
14. `help`

- [ ] **Step 3: Add humanized renderers**

Add renderers:

- `render_conversation_status(session, latest_run=None)`
- `render_conversation_help()`
- `render_session_list(sessions)`
- `render_agent_result_summary(result)`

Rules:

- Do not lead with "任务 #".
- Include task ID only in advanced details when available.
- Mention mode, workspace, step, round, changed files, approvals, and token usage.

- [ ] **Step 4: Register menu commands in Telegram startup**

In `build_application`, register menu commands when config enables it. Use python-telegram-bot's post-init hook or an async startup helper, matching the existing application style.

- [ ] **Step 5: Run menu and status tests**

Run:

```bash
pytest tests/test_telegram_conversation_handlers.py tests/test_status.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/menu.py wlcodex/status.py wlcodex/telegram_app.py tests/test_telegram_conversation_handlers.py tests/test_status.py
git commit -m "feat: add humanized Telegram menus"
```

## Task 5: Add Conversation Controller for Plain Text and Codex Direct Mode

**Files:**

- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_telegram_conversation_handlers.py`

- [ ] **Step 1: Add failing tests for plain text routing**

Test behavior:

```python
async def test_plain_text_creates_codex_conversation(fake_controller):
    response = await fake_controller.handle_conversation_text(
        "帮我分析 router.py",
        {"chat_id": 100, "user_id": 200},
    )
    assert "Codex" in response.text or "正在" in response.text
    assert fake_controller.last_prompt_did_not_include_status_noise
```

Expected initial result: FAIL because conversation handling does not exist.

- [ ] **Step 2: Add conversation controller methods**

Either add a `ConversationController` class or add delegated methods to `CommandController`:

- `handle_conversation_text(text, telegram_context)`
- `handle_new_conversation(command, telegram_context)`
- `handle_codex_direct(command, telegram_context)`
- `handle_current_status(telegram_context)`
- `handle_stop_current(telegram_context)`

Keep legacy `handle(text, ctx)` intact.

- [ ] **Step 3: Implement Codex Direct Mode through existing TaskService**

For the first working slice:

1. Create or load the active conversation.
2. Build a Codex analysis packet.
3. Reserve a hidden task through TaskService.
4. Create or continue a Codex app-server thread.
5. Store the hidden task ID on the conversation.
6. Return a humanized response.

Do not inject rendered status cards into the Codex prompt.

- [ ] **Step 4: Add non-command MessageHandler**

In `telegram_app.py`, add a handler for text messages that are not commands. The handler must:

1. Reuse `_guard`.
2. Record the Telegram update through Ledger.
3. Send typing action.
4. Call conversation controller.
5. Reply with buttons when returned.

- [ ] **Step 5: Run conversation controller tests**

Run:

```bash
pytest tests/test_controller_flow.py tests/test_telegram_conversation_handlers.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/controller.py wlcodex/telegram_app.py tests/test_controller_flow.py tests/test_telegram_conversation_handlers.py
git commit -m "feat: route plain text to conversations"
```

## Task 6: Add Agent Backend Interface and Claude Direct Mode

**Files:**

- Create: `wlcodex/agent_backend.py`
- Create: `wlcodex/claude_backend.py`
- Modify: `wlcodex/config.py`
- Modify: `wlcodex/controller.py`
- Test: `tests/test_agent_backend.py`
- Test: `tests/test_claude_backend.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing tests for disabled Claude**

Test:

```python
async def test_claude_direct_reports_disabled_when_not_configured(controller):
    response = await controller.handle("/claude 修改 README", {"chat_id": 1, "user_id": 2})
    assert "Claude Code 未启用" in response.text
```

Expected initial result: FAIL until command handling is implemented.

- [ ] **Step 2: Add backend protocol**

Define:

- `AgentRequest`
- `AgentResult`
- `AgentStreamEvent`
- `AgentBackend`

Required backend methods:

- `send(request: AgentRequest) -> AgentResult`
- `send_streaming(request: AgentRequest) -> AsyncIterator[AgentStreamEvent]`
- `interrupt(session_id: str | None) -> None`
- `health() -> object`

- [ ] **Step 3: Add Claude config**

Add:

- `ClaudeConfig.enabled`
- `ClaudeConfig.binary`
- `ClaudeConfig.startup_timeout_seconds`
- `ClaudeConfig.request_timeout_seconds`

Update example config.

- [ ] **Step 4: Implement Claude backend skeleton**

The first implementation may be line-buffered subprocess execution. It must:

- Accept a compact prompt packet.
- Set cwd to the selected workspace.
- Capture stdout and stderr.
- Return exit status and summary.
- Avoid shell=True.
- Expose a fake-friendly interface for tests.

- [ ] **Step 5: Wire Claude Direct Mode**

When `/claude <prompt>` is called:

1. Load active conversation.
2. Build a Claude handoff packet directly from user prompt.
3. If Claude disabled, return a clear message and no side effect.
4. If enabled, call Claude backend.
5. Store an AgentRun.
6. Offer `Codex 验收` and `查看 diff` buttons.

- [ ] **Step 6: Run Claude tests**

Run:

```bash
pytest tests/test_agent_backend.py tests/test_claude_backend.py tests/test_controller_flow.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add wlcodex/agent_backend.py wlcodex/claude_backend.py wlcodex/config.py wlcodex/controller.py tests/test_agent_backend.py tests/test_claude_backend.py tests/test_controller_flow.py config/wlcodex.example.toml
git commit -m "feat: add Claude direct mode"
```

## Task 7: Add Codex-Led Orchestrator

**Files:**

- Create: `wlcodex/orchestrator.py`
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/status.py`
- Test: `tests/test_orchestrator.py`
- Test: `tests/test_controller_flow.py`

- [ ] **Step 1: Add failing tests for orchestration pass and retry**

Test pass flow:

```python
async def test_orchestrator_passes_after_codex_verification(fake_codex, fake_claude):
    orchestrator = ChiefEngineerOrchestrator(fake_codex, fake_claude, max_verify_rounds=3)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "passed"
    assert result.verify_round == 1
    assert fake_codex.calls == ["analyze", "verify"]
    assert fake_claude.calls == ["implement"]
```

Test retry flow:

```python
async def test_orchestrator_retries_when_codex_rejects(fake_codex, fake_claude):
    fake_codex.verify_decisions = ["retry", "pass"]
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "passed"
    assert result.verify_round == 2
    assert fake_claude.implement_call_count == 2
```

Expected initial result: FAIL because orchestrator does not exist.

- [ ] **Step 2: Implement orchestration result types**

Types:

- `OrchestrationResult`
- `OrchestrationStepResult`
- `VerificationDecision`

Decision values:

- `pass`
- `retry`
- `stop`
- `need_user`

- [ ] **Step 3: Implement `ChiefEngineerOrchestrator`**

Core methods:

- `run(user_goal, conversation_context)`
- `_analyze_with_codex(...)`
- `_implement_with_claude(...)`
- `_verify_with_codex(...)`
- `_build_retry_packet(...)`

Use context packet builders for every model call.

- [ ] **Step 4: Wire `/auto` and default orchestration**

Rules:

- `/auto <prompt>` always uses the orchestrator.
- Plain text uses orchestrator only when intent classification says implementation plus verification is needed.
- If Claude is disabled, `/auto` returns a clear message and offers Codex-only analysis.

- [ ] **Step 5: Add verification command**

`/verify` should:

1. Resolve the current conversation.
2. Find latest Claude or workspace run.
3. Build a Codex verification packet.
4. Ask Codex to verify.
5. Store the decision.
6. Render pass, retry, stop, or need-user status.

- [ ] **Step 6: Run orchestrator tests**

Run:

```bash
pytest tests/test_orchestrator.py tests/test_controller_flow.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add wlcodex/orchestrator.py wlcodex/controller.py wlcodex/status.py tests/test_orchestrator.py tests/test_controller_flow.py
git commit -m "feat: add chief engineer orchestration"
```

## Task 8: Add Streaming Renderer and Typing Integration

**Files:**

- Create: `wlcodex/streaming.py`
- Modify: `wlcodex/event_bridge.py`
- Modify: `wlcodex/telegram_app.py`
- Test: `tests/test_streaming.py`
- Test: `tests/test_event_bridge.py`

- [ ] **Step 1: Add failing tests for throttled edits**

Test:

```python
async def test_streaming_renderer_throttles_edits(fake_bot, fake_clock):
    renderer = StreamingRenderer(fake_bot, min_interval_seconds=1.0, clock=fake_clock)
    await renderer.append("a")
    await renderer.append("b")
    assert fake_bot.edit_count == 1
    fake_clock.advance(1.1)
    await renderer.append("c")
    assert fake_bot.edit_count == 2
```

Expected initial result: FAIL because streaming renderer does not exist.

- [ ] **Step 2: Implement StreamingRenderer**

Responsibilities:

- Maintain a message buffer.
- Edit existing Telegram message no more often than configured interval.
- Ignore "message is not modified" errors.
- Fall back to send a new message if the edit target is invalid.
- Expose `finish(final_buttons)` for final action buttons.

- [ ] **Step 3: Integrate Codex deltas**

Use existing BackendEvent types:

- `agent_message_delta`
- `command_output_delta`
- `file_change_delta`
- `plan_updated`
- `diff_updated`

Render human-useful deltas and suppress noisy internal details.

- [ ] **Step 4: Add typing indicator**

For active model calls:

- Start typing indicator loop.
- Stop it when model call ends or fails.
- Ensure exceptions do not leave background tasks running.

- [ ] **Step 5: Run streaming tests**

Run:

```bash
pytest tests/test_streaming.py tests/test_event_bridge.py tests/test_telegram_conversation_handlers.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/streaming.py wlcodex/event_bridge.py wlcodex/telegram_app.py tests/test_streaming.py tests/test_event_bridge.py tests/test_telegram_conversation_handlers.py
git commit -m "feat: add conversation streaming"
```

## Task 9: Add Workspace, Model, Session, and Current-Run UX

**Files:**

- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Add failing tests for current-context commands**

Cases:

- `/switch wlcodex` updates the active conversation workspace.
- `/model gpt-5.2` stores current model preference.
- `/sessions` lists conversations, not only Codex thread IDs.
- `/diff` without task ID resolves to latest hidden run.
- `/files` without task ID resolves to latest hidden run.

Expected initial result: FAIL for new behavior and PASS for legacy task-ID variants.

- [ ] **Step 2: Implement `/switch`**

Validate workspace alias through existing config workspace map. Store it on active conversation.

- [ ] **Step 3: Implement `/model`**

Store model preference on active conversation. Do not force backend model switching until Codex backend supports model parameters. Render a clear "saved preference" message if backend support is not active.

- [ ] **Step 4: Implement conversation `/sessions`**

Show:

- conversation ID
- mode
- workspace
- title
- active step
- last updated time

Include advanced task ID only when available.

- [ ] **Step 5: Implement current `/diff` and `/files`**

When no task ID is supplied, resolve latest hidden task from the active conversation and reuse `TaskInspector`.

- [ ] **Step 6: Run UX command tests**

Run:

```bash
pytest tests/test_controller_flow.py tests/test_status.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add wlcodex/controller.py wlcodex/status.py wlcodex/db.py tests/test_controller_flow.py tests/test_status.py
git commit -m "feat: add current conversation controls"
```

## Task 10: Add Config, Main Wiring, Documentation, and Regression Tests

**Files:**

- Modify: `wlcodex/config.py`
- Modify: `wlcodex/main.py`
- Modify: `config/wlcodex.example.toml`
- Modify: `README.md`
- Test: `tests/test_config.py`
- Test: `tests/test_main_composition.py`
- Test: `tests/test_command_flow.py`
- Test: `tests/test_e2e_fake_backend.py`

- [ ] **Step 1: Add failing config tests**

Verify defaults:

```python
def test_conversation_config_defaults():
    config = load_config(path_to_minimal_config)
    assert config.conversation.enabled is True
    assert config.conversation.default_mode == "chief_engineer"
    assert config.claude.enabled is False
    assert config.context_budget.codex_to_claude_tokens == 1500
```

Expected initial result: FAIL until config dataclasses exist.

- [ ] **Step 2: Add config dataclasses**

Add:

- `ConversationConfig`
- `OrchestrationConfig`
- `ClaudeConfig`
- `ContextBudgetConfig`
- `StreamingConfig`
- `MenuConfig`

Defaults must keep Codex-only operation working.

- [ ] **Step 3: Wire services in `main.py`**

Instantiate:

- conversation store through Ledger methods
- context packet budget
- Claude backend only when enabled
- orchestrator only when enabled
- menu registration only when enabled
- streaming renderer only when enabled

- [ ] **Step 4: Update example config**

Add:

```toml
[conversation]
enabled = true
default_mode = "chief_engineer"
default_workspace = "wlcodex"
summary_max_tokens = 800

[orchestration]
enabled = true
max_verify_rounds = 3
auto_delegate_simple_edits = false

[claude]
enabled = false
binary = "claude"
startup_timeout_seconds = 15
request_timeout_seconds = 600

[context_budget]
codex_analysis_tokens = 2500
codex_to_claude_tokens = 1500
claude_to_codex_tokens = 2500
conversation_summary_tokens = 800

[streaming]
enabled = true
edit_min_interval_seconds = 1.0

[menu]
register_bot_commands = true
```

- [ ] **Step 5: Update README**

Document:

- conversation-first default
- `/codex`, `/claude`, `/auto`, `/verify`
- compact context policy
- legacy advanced commands
- Claude disabled-by-default behavior

- [ ] **Step 6: Run full relevant regression suite**

Run:

```bash
pytest tests/test_config.py tests/test_main_composition.py tests/test_command_flow.py tests/test_e2e_fake_backend.py tests/test_router.py tests/test_controller_flow.py tests/test_telegram_handlers.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add wlcodex/config.py wlcodex/main.py config/wlcodex.example.toml README.md tests/test_config.py tests/test_main_composition.py tests/test_command_flow.py tests/test_e2e_fake_backend.py
git commit -m "feat: wire conversation-first cockpit"
```

## Cross-Cutting Verification

Before declaring the feature complete:

- [ ] Run all new tests:

```bash
pytest tests/test_conversation_state.py tests/test_context_packets.py tests/test_conversation_router.py tests/test_telegram_conversation_handlers.py tests/test_agent_backend.py tests/test_claude_backend.py tests/test_orchestrator.py tests/test_streaming.py -q
```

- [ ] Run core regression tests:

```bash
pytest tests/test_router.py tests/test_controller_flow.py tests/test_command_flow.py tests/test_task_service.py tests/test_db.py tests/test_approval.py tests/test_event_bridge.py tests/test_status.py -q
```

- [ ] Prove no status/log prompt injection:

```bash
pytest tests/test_context_packets.py::test_codex_to_claude_packet_excludes_full_transcript -q
```

- [ ] Run an e2e fake backend flow:

```bash
pytest tests/test_e2e_fake_backend.py -q
```

- [ ] Inspect changed prompts manually:

Confirm Codex and Claude prompts contain compact packets and do not contain rendered Telegram status cards, task lists, or full transcripts.

## Implementation Order

Recommended order:

1. Task 1 - persistence
2. Task 2 - context packets
3. Task 3 - commands
4. Task 4 - menus and renderers
5. Task 5 - plain text and Codex direct
6. Task 6 - Claude direct
7. Task 7 - orchestration
8. Task 8 - streaming
9. Task 9 - current conversation controls
10. Task 10 - config, wiring, docs

This order keeps the system working after each slice and prevents orchestration from being built before token-budget controls exist.

## Risk Controls

- Keep legacy command tests passing after every task.
- Keep `claude.enabled = false` as the default until local Claude behavior is proven.
- Do not make `/auto` the implicit path for every implementation request until token usage is measured.
- Treat `ContextPacket.render()` output as the only allowed model prompt surface for new flows.
- Keep approval callbacks unchanged except for humanized surrounding text.
- Put task IDs in advanced status details, not primary copy.

## Self-Review

- Every spec requirement has a corresponding implementation task.
- The plan starts with persistence and context-budget controls before orchestration.
- Direct Codex and direct Claude modes are implemented separately from Chief Engineer Mode.
- Telegram menu, buttons, typing, streaming, and current-run commands are covered.
- Legacy commands and existing runtime safety mechanisms remain protected.
- There are no placeholder implementation steps.
