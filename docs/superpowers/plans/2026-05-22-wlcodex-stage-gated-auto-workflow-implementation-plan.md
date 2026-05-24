# WLCodex Stage-Gated Auto Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `/auto`'s eager full pipeline with a Codex-led, user-gated workflow that matches the user's local Codex-brain / Claude-executor loop.

**Architecture:** Add a small staged-auto workflow layer around existing conversations, `orchestration_runs`, direct Codex turns, and Claude backend calls. `/auto` starts read-only Codex context collection; buttons advance explicit stages for final plan, Claude execution, Codex verification, Claude repair, Codex takeover, and task closure.

**Tech Stack:** Python 3.12, SQLite ledger, Telegram inline callbacks, existing Codex app-server backend, existing Claude backend, pytest, GitNexus MCP for impact checks.

---

## Required Reading

- Spec: `docs/superpowers/specs/2026-05-22-wlcodex-stage-gated-auto-workflow-design.md`
- Controller routing: `wlcodex/controller.py`
- Callback protocol: `wlcodex/conversation_callback.py`
- Context packets: `wlcodex/context_packets.py`
- Codex backend planning modes: `wlcodex/codex_backend.py`
- Current automatic runner: `wlcodex/orchestration_runner.py`
- Runtime bridge: `wlcodex/event_bridge.py`
- Ledger schema and helpers: `wlcodex/db.py`
- Telegram callback handling: `wlcodex/telegram_app.py`
- Existing mode tests: `tests/test_workbench_execution_modes.py`
- Controller callback tests: `tests/test_controller_flow.py`
- Event bridge tests: `tests/test_event_bridge.py`

## Non-Negotiable Engineering Rules

- Do not rely on trigger words to decide execution.
- Do not start Claude from `/auto` until the user clicks `交给 Claude 执行` or `发给 Claude 返工`.
- Do not let Codex write files in `/auto` analysis or verification stages.
- Preserve `/codex` and `/claude` single-agent behavior.
- Preserve `/new` as the workbench boundary.
- Run GitNexus impact analysis before editing any existing function, class, or method.
- Stop and report before editing if impact is HIGH or CRITICAL.
- Write failing tests before implementation.
- Run `gitnexus_detect_changes(scope="all")` before any commit.

## Impact Baseline Commands

Run these immediately before editing the matching symbols:

```text
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "handle_auto_mode",
  "file_path": "wlcodex/controller.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "handle_conversation_text",
  "file_path": "wlcodex/controller.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "handle_conversation_callback",
  "file_path": "wlcodex/controller.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "_sync_direct_agent_run_status",
  "file_path": "wlcodex/event_bridge.py",
  "kind": "Function",
  "direction": "upstream"
})
mcp__gitnexus__.impact({
  "repo": "wlcodex",
  "target": "build_codex_analysis_packet",
  "file_path": "wlcodex/context_packets.py",
  "kind": "Function",
  "direction": "upstream"
})
```

Expected risk: LOW or MEDIUM. If HIGH or CRITICAL, stop and ask for review.

## File Structure

| File | Responsibility |
| --- | --- |
| `wlcodex/auto_workflow.py` | New pure helpers for staged-auto state names, callback actions, button sets, prompt labels, and run lookup predicates. |
| `wlcodex/conversation_callback.py` | Add explicit staged-auto callback constants using existing `conv:{conversation_id}:{action}` protocol. |
| `wlcodex/context_packets.py` | Add staged-auto Codex prompt builders for context collection, final plan, repair prompt, and verification. |
| `wlcodex/controller.py` | Route `/auto`, plain text during staged-auto, and staged-auto callbacks. Start individual Codex/Claude stages only on explicit user actions. |
| `wlcodex/event_bridge.py` | When direct Codex stage tasks complete, update the latest staged orchestration run to the next `needs_user` stage. |
| `wlcodex/status.py` | Render staged-auto status labels clearly in `/status`. |
| `tests/test_auto_workflow.py` | Unit tests for helper constants, stage predicates, and button sets. |
| `tests/test_workbench_execution_modes.py` | `/auto` entry and no-auto-Claude regressions. |
| `tests/test_controller_flow.py` | Callback and mid-analysis user input behavior. |
| `tests/test_event_bridge.py` | Stage updates when Codex stage tasks complete. |
| `tests/test_context_packets.py` | Prompt packet safety and output-shape tests. |
| `tests/test_status_updates.py` | User-visible status rendering for staged-auto phases. |

## Stage Vocabulary

Use these exact `orchestration_runs.current_step` strings:

```python
AUTO_COLLECTING_CONTEXT = "collecting_context"
AUTO_DRAFT_READY = "draft_ready"
AUTO_CLAUDE_RUNNING = "claude_running"
AUTO_CLAUDE_DONE = "claude_done"
AUTO_VERIFYING = "verifying"
AUTO_RETRY_READY = "retry_ready"
AUTO_CODEX_TAKEOVER_RUNNING = "codex_takeover_running"
AUTO_COMPLETED = "completed"
```

Use `orchestration_runs.status = "needs_user"` whenever a button click is
required, and `status = "running"` while Codex or Claude is actively running.

## Task 1: Add Staged-Auto Helper Module

**Files:**
- Create: `wlcodex/auto_workflow.py`
- Create: `tests/test_auto_workflow.py`

- [ ] **Step 1: Write failing tests for stage and button helpers**

Create `tests/test_auto_workflow.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from wlcodex.auto_workflow import (
    AUTO_COLLECTING_CONTEXT,
    AUTO_DRAFT_READY,
    AUTO_RETRY_READY,
    build_auto_stage_buttons,
    is_active_auto_stage,
)


def _labels(buttons: list[list[dict[str, str]]]) -> list[str]:
    return [button["text"] for row in buttons for button in row]


def test_collecting_context_buttons_are_non_executing() -> None:
    labels = _labels(build_auto_stage_buttons(42, AUTO_COLLECTING_CONTEXT))

    assert labels == ["生成最终方案", "查看当前草稿", "取消"]
    assert "交给 Claude 执行" not in labels


def test_draft_ready_buttons_expose_claude_gate() -> None:
    labels = _labels(build_auto_stage_buttons(42, AUTO_DRAFT_READY))

    assert "交给 Claude 执行" in labels
    assert "继续补充" in labels
    assert "Codex 接管修" in labels
    assert "结束任务" in labels


def test_retry_ready_buttons_expose_repair_gate() -> None:
    labels = _labels(build_auto_stage_buttons(42, AUTO_RETRY_READY))

    assert "发给 Claude 返工" in labels
    assert "重写返工提示词" in labels
    assert "Codex 接管修" in labels


def test_is_active_auto_stage_accepts_running_and_needs_user() -> None:
    assert is_active_auto_stage(SimpleNamespace(status="running", current_step=AUTO_COLLECTING_CONTEXT))
    assert is_active_auto_stage(SimpleNamespace(status="needs_user", current_step=AUTO_DRAFT_READY))
    assert not is_active_auto_stage(SimpleNamespace(status="passed", current_step="completed"))
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_auto_workflow.py -q
```

Expected: import failure for `wlcodex.auto_workflow`.

- [ ] **Step 3: Implement helper module**

Create `wlcodex/auto_workflow.py`:

```python
from __future__ import annotations

AUTO_COLLECTING_CONTEXT = "collecting_context"
AUTO_DRAFT_READY = "draft_ready"
AUTO_CLAUDE_RUNNING = "claude_running"
AUTO_CLAUDE_DONE = "claude_done"
AUTO_VERIFYING = "verifying"
AUTO_RETRY_READY = "retry_ready"
AUTO_CODEX_TAKEOVER_RUNNING = "codex_takeover_running"
AUTO_COMPLETED = "completed"

AUTO_STAGE_STEPS = {
    AUTO_COLLECTING_CONTEXT,
    AUTO_DRAFT_READY,
    AUTO_CLAUDE_RUNNING,
    AUTO_CLAUDE_DONE,
    AUTO_VERIFYING,
    AUTO_RETRY_READY,
    AUTO_CODEX_TAKEOVER_RUNNING,
    AUTO_COMPLETED,
}

AUTO_FINAL_PLAN = "auto_final_plan"
AUTO_SHOW_DRAFT = "auto_show_draft"
AUTO_CANCEL = "auto_cancel"
AUTO_SEND_TO_CLAUDE = "auto_send_to_claude"
AUTO_CONTINUE_CONTEXT = "auto_continue_context"
AUTO_REWRITE_PLAN = "auto_rewrite_plan"
AUTO_CODEX_TAKEOVER = "auto_codex_takeover"
AUTO_CLOSE = "auto_close"
AUTO_CODEX_VERIFY = "auto_codex_verify"
AUTO_SEND_REPAIR_TO_CLAUDE = "auto_send_repair_to_claude"
AUTO_REWRITE_REPAIR = "auto_rewrite_repair"


def is_active_auto_stage(run: object | None) -> bool:
    if run is None:
        return False
    return (
        getattr(run, "status", "") in {"running", "needs_user"}
        and getattr(run, "current_step", "") in AUTO_STAGE_STEPS
        and getattr(run, "current_step", "") != AUTO_COMPLETED
    )


def build_auto_stage_buttons(conversation_id: int, stage: str) -> list[list[dict[str, str]]]:
    def button(text: str, action: str) -> dict[str, str]:
        return {"text": text, "callback_data": f"conv:{conversation_id}:{action}"}

    if stage == AUTO_COLLECTING_CONTEXT:
        return [[
            button("生成最终方案", AUTO_FINAL_PLAN),
            button("查看当前草稿", AUTO_SHOW_DRAFT),
        ], [
            button("取消", AUTO_CANCEL),
        ]]
    if stage == AUTO_DRAFT_READY:
        return [[
            button("交给 Claude 执行", AUTO_SEND_TO_CLAUDE),
            button("继续补充", AUTO_CONTINUE_CONTEXT),
        ], [
            button("重写方案", AUTO_REWRITE_PLAN),
            button("Codex 接管修", AUTO_CODEX_TAKEOVER),
        ], [
            button("结束任务", AUTO_CLOSE),
        ]]
    if stage == AUTO_CLAUDE_DONE:
        return [[
            button("Codex 验收", AUTO_CODEX_VERIFY),
            button("查看 diff", "diff"),
        ], [
            button("发给 Claude 返工", AUTO_SEND_REPAIR_TO_CLAUDE),
            button("Codex 接管修", AUTO_CODEX_TAKEOVER),
        ], [
            button("结束任务", AUTO_CLOSE),
        ]]
    if stage == AUTO_RETRY_READY:
        return [[
            button("发给 Claude 返工", AUTO_SEND_REPAIR_TO_CLAUDE),
            button("继续补充", AUTO_CONTINUE_CONTEXT),
        ], [
            button("重写返工提示词", AUTO_REWRITE_REPAIR),
            button("Codex 接管修", AUTO_CODEX_TAKEOVER),
        ], [
            button("结束任务", AUTO_CLOSE),
        ]]
    return [[button("查看状态", "status")]]
```

- [ ] **Step 4: Verify tests pass**

Run:

```bash
pytest tests/test_auto_workflow.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/auto_workflow.py tests/test_auto_workflow.py
git commit -m "feat: add staged auto workflow helpers"
```

## Task 2: Add Staged-Auto Prompt Packets

**Files:**
- Modify: `wlcodex/context_packets.py`
- Modify: `tests/test_context_packets.py`

- [ ] **Step 1: Run GitNexus impact**

Run the `build_codex_analysis_packet` impact command from the Impact Baseline.
Expected: LOW or MEDIUM. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing prompt packet tests**

Append to `tests/test_context_packets.py`:

```python
def test_auto_context_collection_packet_is_read_only_and_not_handoff() -> None:
    from wlcodex.context_packets import build_auto_context_packet

    packet = build_auto_context_packet(
        user_goal="定位偶发失败",
        conversation_summary="用户会继续补充日志",
        workspace="lightfeev2",
    )
    rendered = packet.render()

    assert "只读分析" in rendered
    assert "继续等待用户补充" in rendered
    assert "禁止创建、修改、删除任何工作区文件" in rendered
    assert "Claude handoff packet" not in rendered


def test_auto_final_plan_packet_requests_claude_prompt() -> None:
    from wlcodex.context_packets import build_auto_final_plan_packet

    packet = build_auto_final_plan_packet(
        user_goal="修复登录错误",
        conversation_summary="已确认是空用户路径",
        workspace="wlcodex",
    )
    rendered = packet.render()

    assert "最终方案" in rendered
    assert "给 Claude 的执行提示词" in rendered
    assert "acceptance_criteria" in rendered
    assert "prohibited_changes" in rendered
```

- [ ] **Step 3: Verify failing tests**

Run:

```bash
pytest tests/test_context_packets.py::test_auto_context_collection_packet_is_read_only_and_not_handoff tests/test_context_packets.py::test_auto_final_plan_packet_requests_claude_prompt -q
```

Expected: import failure for new packet builders.

- [ ] **Step 4: Implement packet builders**

Add functions to `wlcodex/context_packets.py` near `build_codex_analysis_packet`:

```python
def build_auto_context_packet(
    user_goal: str,
    conversation_summary: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> CodexAnalysisPacket:
    bgt = budget or ContextBudget()
    return CodexAnalysisPacket(
        mode="auto_collecting_context",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_summary, bgt.conversation_summary_tokens),
        recent_user_constraints=[
            "本轮是 /auto 的 Codex 上下文收集阶段。",
            "只读分析：禁止创建、修改、删除任何工作区文件。",
            "不要启动 Claude，不要输出最终执行包。",
            "如果信息不足，继续等待用户补充；如果已有判断，给出阶段性结论。",
        ],
        token_budget=bgt.codex_analysis_tokens,
        requested_output="中文阶段性分析：当前判断、缺失信息、建议用户补充什么。",
    )


def build_auto_final_plan_packet(
    user_goal: str,
    conversation_summary: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> CodexAnalysisPacket:
    bgt = budget or ContextBudget()
    return CodexAnalysisPacket(
        mode="auto_final_plan",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_summary, bgt.conversation_summary_tokens),
        recent_user_constraints=[
            "输出 /auto 的最终方案，不要改代码。",
            "必须包含给 Claude 的执行提示词。",
            "如果无需实现，明确写 needs_implementation: false，并说明不要交给 Claude。",
            "保留用户补充的约束和禁止事项。",
        ],
        token_budget=bgt.codex_analysis_tokens,
        requested_output=(
            "中文最终方案，包含 diagnosis, confidence, files_to_touch, "
            "claude_prompt, acceptance_criteria, prohibited_changes, verification_plan。"
        ),
    )
```

- [ ] **Step 5: Verify prompt tests**

Run:

```bash
pytest tests/test_context_packets.py -q
```

Expected: all context packet tests pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/context_packets.py tests/test_context_packets.py
git commit -m "feat: add staged auto Codex prompt packets"
```

## Task 3: Make `/auto` Start Context Collection Only

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `tests/test_workbench_execution_modes.py`

- [ ] **Step 1: Run GitNexus impact**

Run impact for `handle_auto_mode` and `_handle_codex_analysis_only`.
Expected: LOW or MEDIUM. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing `/auto` entry test**

Append to `tests/test_workbench_execution_modes.py`:

```python
@pytest.mark.asyncio
async def test_auto_starts_context_collection_without_claude(tmp_path: Path) -> None:
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    response = await ctrl.handle("/auto 查一下登录偶发失败", {"chat_id": 42, "user_id": 1})

    assert runner.starts == []
    assert claude.send_calls == []
    assert "Codex" in response.text
    assert "补充" in response.text or "最终方案" in response.text

    conversation = ctrl._ledger.get_active_conversation(42)
    runs = ctrl._ledger.list_orchestration_runs(conversation.id, limit=1)
    assert len(runs) == 1
    assert runs[0].status == "running"
    assert runs[0].current_step == "collecting_context"

    agent_runs = ctrl._ledger.list_agent_runs(conversation.id, limit=5)
    assert [(run.agent, run.role) for run in agent_runs] == [("codex", "auto_analysis")]
    assert ctrl._backend.prompt_turns[-1][2] == "read_only_analysis"
```

- [ ] **Step 3: Verify test fails**

Run:

```bash
pytest tests/test_workbench_execution_modes.py::test_auto_starts_context_collection_without_claude -q
```

Expected: failure because current `/auto` starts the orchestration runner.

- [ ] **Step 4: Implement `/auto` staged entry**

In `wlcodex/controller.py`:

1. Import `AUTO_COLLECTING_CONTEXT` and `build_auto_stage_buttons`.
2. Import `build_auto_context_packet`.
3. Replace `handle_auto_mode`'s direct `OrchestrationRunner.start_chief_engineer`
   path with:
   - create or reuse active conversation;
   - run workspace busy check;
   - create `orchestration_runs` row with `current_step="collecting_context"`;
   - reserve Codex task with purpose such as `auto_analysis`;
   - create agent run `agent="codex", role="auto_analysis"`;
   - start Codex with `interaction_mode="read_only_analysis"`;
   - return buttons from `build_auto_stage_buttons(conversation.id, AUTO_COLLECTING_CONTEXT)`.

The implementation should reuse `_start_codex_turn_for_conversation` so
conversation `codex_thread_id` continuity still works.

- [ ] **Step 5: Verify focused test**

Run:

```bash
pytest tests/test_workbench_execution_modes.py::test_auto_starts_context_collection_without_claude -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/controller.py tests/test_workbench_execution_modes.py
git commit -m "feat: make auto start staged Codex analysis"
```

## Task 4: Route Plain Text As Auto Context During Collection

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `tests/test_controller_flow.py`

- [ ] **Step 1: Run GitNexus impact**

Run impact for `handle_conversation_text` and `_send_pending_to_current_session`.
Expected: LOW or MEDIUM. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing mid-analysis context test**

Append to `tests/test_controller_flow.py`:

```python
@pytest.mark.asyncio
async def test_plain_text_during_auto_collection_steers_current_codex_analysis(
    ctrl_with_claude: CommandController,
) -> None:
    await ctrl_with_claude.handle("/auto 查一下偶发失败", {"chat_id": 600, "user_id": 700})
    conversation = ctrl_with_claude._ledger.get_active_conversation(600)
    task_id = conversation.active_codex_task_id
    task = ctrl_with_claude._service.get_task(task_id)
    ctrl_with_claude._service.set_task_thread(task.id, "thread-auto")
    ctrl_with_claude._ledger.set_active_turn(task.id, "turn-auto")

    response = await ctrl_with_claude.handle_conversation_text(
        "补充：只在云上出现，本地复现不了",
        {"chat_id": 600, "user_id": 700},
    )

    assert "已补充" in response.text or "Codex" in response.text
    assert ctrl_with_claude._backend.steers[-1] == (
        "thread-auto",
        "turn-auto",
        "补充：只在云上出现，本地复现不了",
    )
```

- [ ] **Step 3: Verify failing test**

Run:

```bash
pytest tests/test_controller_flow.py::test_plain_text_during_auto_collection_steers_current_codex_analysis -q
```

Expected: failure because current text routing does not know staged-auto
collection.

- [ ] **Step 4: Implement staged-auto text routing**

In `wlcodex/controller.py`:

1. Add helper `_latest_active_auto_run(conversation_id)`.
2. At the start of `handle_conversation_text`, if active conversation has latest
   active auto run with `current_step == "collecting_context"`, steer the active
   Codex turn when possible.
3. If no active Codex turn exists but the run is still `needs_user` in
   `collecting_context`, start another read-only Codex turn using
   `build_auto_context_packet` and the same conversation thread.
4. Update conversation summary with `[Auto补充] <text>`.
5. Return a clear acknowledgement with `生成最终方案` buttons.

- [ ] **Step 5: Verify test**

Run:

```bash
pytest tests/test_controller_flow.py::test_plain_text_during_auto_collection_steers_current_codex_analysis -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/controller.py tests/test_controller_flow.py
git commit -m "feat: append plain text to auto Codex analysis"
```

## Task 5: Add Final Plan Callback

**Files:**
- Modify: `wlcodex/conversation_callback.py`
- Modify: `wlcodex/controller.py`
- Modify: `tests/test_controller_flow.py`

- [ ] **Step 1: Run GitNexus impact**

Run impact for `handle_conversation_callback`.
Expected: LOW or MEDIUM. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing final-plan callback test**

Append to `tests/test_controller_flow.py`:

```python
@pytest.mark.asyncio
async def test_auto_final_plan_callback_starts_read_only_codex_final_plan(
    ctrl_with_claude: CommandController,
) -> None:
    from wlcodex.conversation_callback import ConversationCallback
    from wlcodex.auto_workflow import AUTO_FINAL_PLAN

    await ctrl_with_claude.handle("/auto 查一下偶发失败", {"chat_id": 601, "user_id": 701})
    conversation = ctrl_with_claude._ledger.get_active_conversation(601)

    response = await ctrl_with_claude.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_FINAL_PLAN)
    )

    assert "最终方案" in response.text or "Codex" in response.text
    assert ctrl_with_claude._backend.prompt_turns[-1][2] == "read_only_analysis"
    runs = ctrl_with_claude._ledger.list_orchestration_runs(conversation.id, limit=1)
    assert runs[0].status == "running"
    assert runs[0].current_step == "collecting_context"
    agent_runs = ctrl_with_claude._ledger.list_agent_runs(conversation.id, limit=5)
    assert any(run.role == "auto_final_plan" for run in agent_runs)
```

- [ ] **Step 3: Verify failing test**

Run:

```bash
pytest tests/test_controller_flow.py::test_auto_final_plan_callback_starts_read_only_codex_final_plan -q
```

Expected: unknown callback action.

- [ ] **Step 4: Implement final-plan callback**

In `wlcodex/conversation_callback.py`, export staged-auto constants from
`wlcodex.auto_workflow` or define matching constants with the same string values.

In `wlcodex/controller.py`, route `AUTO_FINAL_PLAN`:

1. find latest active auto run;
2. reject if current step is not `collecting_context` or `retry_ready`;
3. create Codex agent run with role `auto_final_plan`;
4. start read-only Codex turn with `build_auto_final_plan_packet`;
5. keep run status `running` until the event bridge observes completion.

- [ ] **Step 5: Verify final-plan test**

Run:

```bash
pytest tests/test_controller_flow.py::test_auto_final_plan_callback_starts_read_only_codex_final_plan -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/conversation_callback.py wlcodex/controller.py tests/test_controller_flow.py
git commit -m "feat: add auto final plan callback"
```

## Task 6: Advance Stages On Codex Stage Completion

**Files:**
- Modify: `wlcodex/event_bridge.py`
- Modify: `tests/test_event_bridge.py`

- [ ] **Step 1: Run GitNexus impact**

Run impact for `_sync_direct_agent_run_status`.
Expected: LOW or MEDIUM. Stop if HIGH or CRITICAL.

- [ ] **Step 2: Write failing event bridge test**

Append to `tests/test_event_bridge.py`:

```python
@pytest.mark.asyncio
async def test_auto_final_plan_completion_sets_draft_ready(tmp_path: Path) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY
    from wlcodex.runtime_event_store import RuntimeEventStore

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    service = TaskService(ledger, [WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)])
    conversation = ledger.create_conversation(
        chat_id=1,
        user_id=2,
        title="auto",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    run = ledger.create_orchestration_run(conversation.id, "查问题")
    ledger.update_orchestration_run(run.id, status="running", current_step="collecting_context")
    task = service.start_task("demo", "final plan", codex_thread_id="thread-auto")
    ledger.set_conversation_active_task(conversation.id, task.id)
    agent_run = ledger.create_agent_run(conversation.id, "codex", "auto_final_plan", hidden_task_id=task.id)
    ledger.update_agent_run_status(agent_run.id, "running")

    bridge = _bridge(service, IdleBackend(), ledger, runtime_event_store=store)
    await bridge.process_event(BackendEvent("turn_started", {"threadId": "thread-auto", "turnId": "turn-auto"}))
    await bridge.process_event(BackendEvent("agent_message_delta", {
        "threadId": "thread-auto",
        "turnId": "turn-auto",
        "delta": "最终方案\n给 Claude 的执行提示词：修复空用户路径",
    }))
    await bridge.process_event(BackendEvent("turn_completed", {"threadId": "thread-auto", "status": "completed"}))

    updated = ledger.get_orchestration_run(run.id)
    assert updated.status == "needs_user"
    assert updated.current_step == AUTO_DRAFT_READY
    assert "给 Claude 的执行提示词" in updated.last_codex_analysis
```

- [ ] **Step 3: Verify failing test**

Run:

```bash
pytest tests/test_event_bridge.py::test_auto_final_plan_completion_sets_draft_ready -q
```

Expected: run remains unchanged.

- [ ] **Step 4: Implement stage completion projection**

In `wlcodex/event_bridge.py`, after direct agent run completion:

1. inspect the completed `agent_runs.role`;
2. for `auto_final_plan`, find latest active orchestration run for the
   conversation;
3. update it to `status="needs_user"`, `current_step="draft_ready"`,
   `last_codex_analysis=<completion summary or buffered text>`;
4. emit a runtime event such as `auto.stage.transitioned`.

Task 8 extends the same projection path for `auto_verification`; Task 9
extends it for `auto_codex_takeover`.

- [ ] **Step 5: Verify event bridge tests**

Run:

```bash
pytest tests/test_event_bridge.py::test_auto_final_plan_completion_sets_draft_ready tests/test_event_bridge.py::test_direct_codex_turn_completion_marks_agent_run_done -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/event_bridge.py tests/test_event_bridge.py
git commit -m "feat: advance auto stage after Codex final plan"
```

## Task 7: Gate Claude Execution And Completion

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `tests/test_controller_flow.py`

- [ ] **Step 1: Run GitNexus impact**

Run impact for `handle_conversation_callback` and the existing Claude direct
helper used by `/claude`. Expected: LOW or MEDIUM.

- [ ] **Step 2: Write failing Claude gate test**

Append to `tests/test_controller_flow.py`:

```python
@pytest.mark.asyncio
async def test_send_to_claude_callback_starts_claude_once_from_draft(
    ctrl_with_claude: CommandController,
) -> None:
    from wlcodex.auto_workflow import AUTO_DRAFT_READY, AUTO_SEND_TO_CLAUDE
    from wlcodex.conversation_callback import ConversationCallback

    conversation = ctrl_with_claude._ledger.create_conversation(
        chat_id=602,
        user_id=702,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    run = ctrl_with_claude._ledger.create_orchestration_run(conversation.id, "修复 bug")
    ctrl_with_claude._ledger.update_orchestration_run(
        run.id,
        status="needs_user",
        current_step=AUTO_DRAFT_READY,
        last_codex_analysis="给 Claude 的执行提示词：请修复空用户路径",
    )

    response = await ctrl_with_claude.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_SEND_TO_CLAUDE)
    )

    assert "Claude" in response.text
    assert len(ctrl_with_claude._claude.send_calls) == 1
    assert "请修复空用户路径" in ctrl_with_claude._claude.send_calls[0].prompt
    updated = ctrl_with_claude._ledger.get_orchestration_run(run.id)
    assert updated.current_step == "claude_running"
```

- [ ] **Step 3: Verify failing test**

Run:

```bash
pytest tests/test_controller_flow.py::test_send_to_claude_callback_starts_claude_once_from_draft -q
```

Expected: unknown callback.

- [ ] **Step 4: Implement Claude gate**

In `wlcodex/controller.py`, route `AUTO_SEND_TO_CLAUDE`:

1. require latest auto run in `draft_ready` or `retry_ready`;
2. extract prompt from `last_codex_analysis`;
3. create Claude agent run with role `auto_implementation` or
   `auto_repair`;
4. update run to `status="running"`, `current_step="claude_running"`;
5. call existing Claude backend with `resume_session_id` from conversation when
   present;
6. on success, update conversation Claude session id and run to
   `status="needs_user"`, `current_step="claude_done"`,
   `last_claude_summary=<summary>`.

- [ ] **Step 5: Verify Claude gate test**

Run:

```bash
pytest tests/test_controller_flow.py::test_send_to_claude_callback_starts_claude_once_from_draft -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/controller.py tests/test_controller_flow.py
git commit -m "feat: gate auto Claude execution by button"
```

## Task 8: Add Codex Verification And Repair Prompt Gate

**Files:**
- Modify: `wlcodex/context_packets.py`
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/event_bridge.py`
- Modify: `tests/test_context_packets.py`
- Modify: `tests/test_controller_flow.py`
- Modify: `tests/test_event_bridge.py`

- [ ] **Step 1: Write failing verification callback test**

Append to `tests/test_controller_flow.py`:

```python
@pytest.mark.asyncio
async def test_auto_codex_verify_starts_read_only_verification(
    ctrl_with_claude: CommandController,
) -> None:
    from wlcodex.auto_workflow import AUTO_CLAUDE_DONE, AUTO_CODEX_VERIFY
    from wlcodex.conversation_callback import ConversationCallback

    conversation = ctrl_with_claude._ledger.create_conversation(
        chat_id=603,
        user_id=703,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    run = ctrl_with_claude._ledger.create_orchestration_run(conversation.id, "修复 bug")
    ctrl_with_claude._ledger.update_orchestration_run(
        run.id,
        status="needs_user",
        current_step=AUTO_CLAUDE_DONE,
        last_codex_analysis="方案",
        last_claude_summary="Claude 改了 auth.py",
    )

    response = await ctrl_with_claude.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_CODEX_VERIFY)
    )

    assert "验收" in response.text or "Codex" in response.text
    assert ctrl_with_claude._backend.prompt_turns[-1][2] == "verification"
    updated = ctrl_with_claude._ledger.get_orchestration_run(run.id)
    assert updated.current_step == "verifying"
```

- [ ] **Step 2: Verify failing test**

Run:

```bash
pytest tests/test_controller_flow.py::test_auto_codex_verify_starts_read_only_verification -q
```

Expected: unknown callback.

- [ ] **Step 3: Implement verification packet and callback**

1. Add `build_auto_verification_packet` to `wlcodex/context_packets.py`.
2. Route `AUTO_CODEX_VERIFY` in `handle_conversation_callback`.
3. Create Codex agent run role `auto_verification`.
4. Increment `verify_round`.
5. Start Codex with `interaction_mode="verification"`.
6. Keep run in `status="running"`, `current_step="verifying"` until completion.

- [ ] **Step 4: Implement verification completion projection**

In `wlcodex/event_bridge.py`:

1. on `auto_verification` completion, inspect verification text;
2. if it contains `decision: pass`, set `status="needs_user"`,
   `current_step="completed"`, `last_verification_result=<text>`;
3. otherwise set `status="needs_user"`, `current_step="retry_ready"`,
   `last_verification_result=<text>`;
4. buttons shown by the final answer should include either `结束任务` or
   `发给 Claude 返工`.

- [ ] **Step 5: Verify tests**

Run:

```bash
pytest tests/test_context_packets.py tests/test_controller_flow.py::test_auto_codex_verify_starts_read_only_verification tests/test_event_bridge.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add wlcodex/context_packets.py wlcodex/controller.py wlcodex/event_bridge.py tests/test_context_packets.py tests/test_controller_flow.py tests/test_event_bridge.py
git commit -m "feat: add gated auto Codex verification"
```

## Task 9: Add Codex Takeover Gate

**Files:**
- Modify: `wlcodex/controller.py`
- Modify: `tests/test_controller_flow.py`

- [ ] **Step 1: Write failing Codex takeover test**

Append to `tests/test_controller_flow.py`:

```python
@pytest.mark.asyncio
async def test_auto_codex_takeover_requires_explicit_callback(
    ctrl_with_claude: CommandController,
) -> None:
    from wlcodex.auto_workflow import AUTO_CODEX_TAKEOVER, AUTO_RETRY_READY
    from wlcodex.conversation_callback import ConversationCallback

    conversation = ctrl_with_claude._ledger.create_conversation(
        chat_id=604,
        user_id=704,
        title="auto",
        mode="chief_engineer",
        workspace_alias="wlcodex",
    )
    run = ctrl_with_claude._ledger.create_orchestration_run(conversation.id, "修复 bug")
    ctrl_with_claude._ledger.update_orchestration_run(
        run.id,
        status="needs_user",
        current_step=AUTO_RETRY_READY,
        last_verification_result="Claude 没修对，需要 Codex 接管",
    )

    response = await ctrl_with_claude.handle_conversation_callback(
        ConversationCallback(conversation.id, AUTO_CODEX_TAKEOVER)
    )

    assert "Codex" in response.text
    updated = ctrl_with_claude._ledger.get_orchestration_run(run.id)
    assert updated.current_step == "codex_takeover_running"
    agent_runs = ctrl_with_claude._ledger.list_agent_runs(conversation.id, limit=5)
    assert any(run.agent == "codex" and run.role == "auto_codex_takeover" for run in agent_runs)
```

- [ ] **Step 2: Verify failing test**

Run:

```bash
pytest tests/test_controller_flow.py::test_auto_codex_takeover_requires_explicit_callback -q
```

Expected: unknown callback.

- [ ] **Step 3: Implement takeover callback**

In `wlcodex/controller.py`, route `AUTO_CODEX_TAKEOVER`:

1. require active auto run in `draft_ready`, `claude_done`, or `retry_ready`;
2. reserve Codex task with purpose `auto_codex_takeover`;
3. create agent run role `auto_codex_takeover`;
4. update run to `status="running"`,
   `current_step="codex_takeover_running"`;
5. start Codex direct turn with workspace-write behavior, using a prompt that
   includes goal, last plan, last Claude summary, and last verification result.

- [ ] **Step 4: Verify takeover test**

Run:

```bash
pytest tests/test_controller_flow.py::test_auto_codex_takeover_requires_explicit_callback -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add wlcodex/controller.py tests/test_controller_flow.py
git commit -m "feat: add explicit auto Codex takeover"
```

## Task 10: Status, Button Language, And End-To-End Regression

**Files:**
- Modify: `wlcodex/status.py`
- Modify: `tests/test_status_updates.py`
- Modify: `tests/test_workbench_execution_modes.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing status test**

Append to `tests/test_status_updates.py`:

```python
def test_status_renders_stage_gated_auto_waiting_state() -> None:
    from datetime import datetime, timezone

    from wlcodex.models import ConversationSession, OrchestrationRun
    from wlcodex.status import render_conversation_status

    now = datetime(2026, 5, 22, tzinfo=timezone.utc)
    session = ConversationSession(
        id=1,
        chat_id=2,
        user_id=3,
        title="新工作台",
        mode="chief_engineer",
        workspace_alias="lightfeev2",
        active_codex_task_id=None,
        active_claude_run_id=None,
        conversation_summary="",
        current_model="",
        created_at=now,
        updated_at=now,
        archived_at=None,
    )
    run = OrchestrationRun(
        id=10,
        conversation_id=1,
        goal="查问题",
        status="needs_user",
        current_step="draft_ready",
        verify_round=0,
        max_verify_rounds=3,
        last_codex_analysis="给 Claude 的执行提示词：修复空用户路径",
        last_claude_summary="",
        last_verification_result="",
        created_at=now,
        updated_at=now,
    )

    text = render_conversation_status(session, orch_run=run)

    assert "等待用户选择" in text
    assert "交给 Claude 执行" in text
    assert "draft_ready" not in text
```

- [ ] **Step 2: Write end-to-end no-hidden-Claude regression**

Add to `tests/test_workbench_execution_modes.py`:

```python
@pytest.mark.asyncio
async def test_auto_does_not_start_claude_until_button_after_context_and_final_plan(
    tmp_path: Path,
) -> None:
    claude = FakeClaudeBackend(enabled=True)
    runner = FakeOrchestrationRunner()
    ctrl = build_controller(tmp_path, claude=claude, orchestrator=runner)

    await ctrl.handle("/auto 查 cloud deploy 是否生效", {"chat_id": 77, "user_id": 88})
    await ctrl.handle_conversation_text("补充：只看 lightfeev2", {"chat_id": 77, "user_id": 88})

    assert runner.starts == []
    assert claude.send_calls == []

    conversation = ctrl._ledger.get_active_conversation(77)
    runs = ctrl._ledger.list_orchestration_runs(conversation.id, limit=1)
    assert runs[0].current_step == "collecting_context"
```

- [ ] **Step 3: Implement status and README updates**

Update status/help text:

- Plain text: `Codex 只读分析`
- `/auto`: `Codex 主导闭环：分析 -> 用户确认 -> Claude 执行 -> 用户确认 -> Codex 验收`
- Avoid saying `/auto` automatically runs Claude.

- [ ] **Step 4: Verify focused tests**

Run:

```bash
pytest tests/test_auto_workflow.py tests/test_workbench_execution_modes.py tests/test_controller_flow.py tests/test_event_bridge.py tests/test_status_updates.py tests/test_context_packets.py -q
```

Expected: pass.

- [ ] **Step 5: Full verification**

Run:

```bash
python3 -m compileall wlcodex
git diff --check
pytest -q
```

Expected:

- compileall exits 0;
- diff check exits 0;
- pytest reports all tests passed.

- [ ] **Step 6: GitNexus change detection**

Run:

```text
mcp__gitnexus__.detect_changes({
  "repo": "wlcodex",
  "scope": "all"
})
```

Expected: no HIGH or CRITICAL unexpected process impact. If HIGH or CRITICAL,
stop and report before deployment.

- [ ] **Step 7: Local deploy and smoke**

Only after all checks pass:

```bash
systemctl --user restart wlcodex.service
systemctl --user status wlcodex.service --no-pager
journalctl --user -u wlcodex.service --since '2 minutes ago' --no-pager
```

Manual Telegram smoke:

1. `/new`
2. switch to `lightfeev2`
3. `/auto 查一下当前结构是否臃肿`
4. send `补充：重点看死代码，不要执行修改`
5. verify no Claude agent run exists
6. click `生成最终方案`
7. verify still no Claude agent run exists
8. click `交给 Claude 执行`
9. verify exactly one Claude run starts

- [ ] **Step 8: Commit**

```bash
git add wlcodex tests README.md
git commit -m "feat: make auto workflow stage gated"
```

## Self-Review

Spec coverage:

- `/auto` starts Codex read-only: Tasks 2 and 3.
- Mid-analysis user insertion: Task 4.
- Final plan and Claude prompt gate: Task 5.
- Claude execution gate: Task 7.
- Codex verification gate and repair prompt: Task 8.
- Codex takeover fallback: Task 9.
- Button wording and status clarity: Task 10.
- No trigger-word routing: covered by Task 3 and Task 10 regression tests.

Placeholder scan:

- The plan contains no `TBD`, no open implementation placeholders, and no
  "write tests for the above" steps without concrete test examples.

Type consistency:

- Stage constants and callback actions originate from `wlcodex/auto_workflow.py`.
- Controller callback routing uses existing `ConversationCallback`.
- Persistent state uses existing `orchestration_runs` fields from `wlcodex/db.py`.
