# WLCodex Stage-Gated Auto Workflow Design

## Status

Drafted from user-approved direction on 2026-05-22.

This spec changes the intended `/auto` product semantics only. It does not
authorize code changes by itself. The implementation must be delivered in a
later task from the paired implementation plan.

## Source Of Truth: Local Human Workflow

The user currently runs Codex and Claude locally with this loop:

1. Talk to Codex first. Codex is the brain.
2. Use Codex for investigation, bug confirmation, architecture judgement, and
   prompt generation.
3. Copy the Codex-generated execution prompt into Claude.
4. Let Claude implement.
5. Ask Codex to verify Claude's work.
6. If verification fails, ask Codex to generate a repair prompt, then copy it
   to Claude.
7. Repeat verification and repair until closed.
8. If Claude still cannot close the loop, explicitly ask Codex to take over and
   fix directly.

Telegram `/auto` should remove the copy/paste burden without removing the
user's control. Codex remains the brain. Claude remains the execution hand.
Codex direct repair remains an explicit fallback, not an automatic default.

## Problem

The current `/auto` behavior is too eager: it tends to treat `/auto <goal>` as
one automatic Codex -> Claude -> Codex pipeline. That is useful when the task is
well specified, but it does not match the user's real workflow for unclear bugs
or exploratory tasks.

The failure modes are severe:

- Codex can decide too early that implementation is needed.
- Claude can start before the user has finished adding context.
- The user cannot clearly tell which step will run next.
- Buttons such as `继续` are ambiguous.
- Safety relies too much on text intent detection and trigger-word heuristics.

The desired fix is to replace heuristic execution with explicit workflow state
and user gates.

## Goals

### G1. `/auto` enters a Codex-led workbench, not an immediate full pipeline

`/auto <goal>` starts a staged auto workflow in the current conversation. The
first stage is Codex analysis and context collection.

Starting `/auto` must not start Claude. Starting `/auto` must not let Codex edit
code. Codex starts in read-only analysis mode.

### G2. The Codex analysis stage supports mid-stream user context

While the workflow is in the Codex analysis stage, ordinary Telegram text from
the user is appended to the current Codex analysis session as additional
context.

The user may add facts, logs, constraints, corrections, or ask Codex to inspect
more evidence. These messages must not create a new task, must not start
Claude, and must not be treated as a new `/auto`.

The user decides when analysis is mature enough by pressing an explicit button
or command:

- `生成最终方案`

### G3. Codex produces a final plan and Claude execution prompt

When the user asks for the final plan, Codex produces:

- concise diagnosis;
- confidence and unresolved assumptions;
- files and areas likely involved;
- explicit Claude execution prompt;
- acceptance criteria;
- prohibited changes;
- verification checklist.

The final plan may say no implementation is needed. In that case the workflow
offers `结束任务` and `继续问 Codex`; it must not offer Claude execution as the
primary next step.

### G4. Claude execution is gated by user action

Claude starts only after the user presses:

- `交给 Claude 执行`

The button sends the current final Claude execution prompt to Claude. It does
not send arbitrary chat history. The prompt should include enough context for
Claude to work without re-deriving the investigation.

### G5. Codex verification is gated by user action

After Claude completes, the workflow enters `claude_done`. It does not auto-run
Codex verification unless the user presses:

- `Codex 验收`

Codex verification is read-only by default. It reviews diff, tests, logs, and
stated acceptance criteria.

### G6. Failed verification creates a repair prompt, not an automatic retry

When Codex verification fails, Codex produces:

- concrete failure summary;
- required fix;
- focused Claude repair prompt;
- acceptance criteria for the repair round.

Claude repair starts only after the user presses:

- `发给 Claude 返工`

The loop may repeat. Each round increments `verify_round` and keeps the full
audit trail visible through status and runtime events.

### G7. Codex takeover is an explicit fallback

Codex may edit code only after the user presses:

- `Codex 接管修`

This is used when:

- Claude failed multiple rounds;
- the fix is small enough for Codex direct mode;
- the user wants the brain to stop delegating and repair directly.

Codex takeover must run through the same task lease, workspace, diff, and status
machinery as `/codex`.

### G8. Ordinary commands keep their existing semantics

- Plain text outside an active staged `/auto`: Codex read-only analysis.
- `/codex <prompt>`: Codex-only direct work, no Claude.
- `/claude <prompt>`: Claude-only direct work, no Codex analysis/verification.
- `/new`: creates a new workbench boundary.
- `/auto <goal>`: stage-gated Codex-led workflow.

The implementation must not reintroduce trigger-word based routing. Safety is
provided by commands, buttons, state, sandbox policy, and workspace leases.

## Non-Goals

- Do not make `/auto` fully automatic by default.
- Do not infer execution permission from words such as "修", "改", "查",
  "执行", or `needs_implementation`.
- Do not remove `/codex` or `/claude`.
- Do not alter Claude permission modes.
- Do not build a new Telegram UI framework.
- Do not rewrite all runtime event projection.
- Do not require a new LLM call just to summarize every message.

## Workflow State Model

The staged workflow can reuse `orchestration_runs` rather than introduce a new
table in the first implementation.

Recommended `orchestration_runs.status` values:

- `running`: active work is happening.
- `needs_user`: waiting for a button or command.
- `passed`: accepted and closed.
- `failed`: ended unsuccessfully.
- `aborted`: stopped by user.

Recommended `orchestration_runs.current_step` values:

- `collecting_context`: Codex is analyzing and user may add more context.
- `draft_ready`: Codex has produced a final plan and Claude prompt.
- `claude_running`: Claude is executing the approved prompt.
- `claude_done`: Claude finished and waits for Codex verification.
- `verifying`: Codex is verifying Claude output.
- `retry_ready`: Codex produced a repair prompt and waits for user approval.
- `codex_takeover_running`: Codex is directly repairing after explicit user
  approval.
- `completed`: workflow is accepted and closed.

Existing text fields should be used deliberately:

- `last_codex_analysis`: current Codex final plan or latest repair analysis.
- `last_claude_summary`: latest Claude implementation/repair result.
- `last_verification_result`: latest Codex verification result.

`verify_round` tracks verification attempts. It should increment when a Codex
verification run starts, not when a Claude run starts.

## Input Routing Rules

### While `collecting_context`

Plain text is sent to the active Codex analysis thread as additional context.
The response should acknowledge the added context or continue analysis.

Buttons:

- `生成最终方案`
- `查看当前草稿`
- `取消`

### While `draft_ready`

Plain text means "continue discussing with Codex" and should return to
`collecting_context` unless the text is an explicit command.

Buttons:

- `交给 Claude 执行`
- `继续补充`
- `重写方案`
- `Codex 接管修`
- `结束任务`

### While `claude_running`

Plain text follows the existing workspace-busy choice model. It must not be
silently interpreted as permission to change the Claude prompt mid-run.

Buttons:

- `查看状态`
- `打断 Claude`

### While `claude_done`

Plain text means a note for Codex verification unless the user chooses another
button.

Buttons:

- `Codex 验收`
- `查看 diff`
- `发给 Claude 返工`
- `Codex 接管修`
- `结束任务`

### While `retry_ready`

Plain text means "revise the repair prompt with this extra context" and should
return to Codex prompt refinement.

Buttons:

- `发给 Claude 返工`
- `继续补充`
- `重写返工提示词`
- `Codex 接管修`
- `结束任务`

## Button Language

Ambiguous labels must be avoided.

Replace vague labels:

- `继续`

With action-specific labels:

- `继续问 Codex`
- `继续补充`
- `生成最终方案`
- `交给 Claude 执行`
- `Codex 验收`
- `生成返工提示词`
- `发给 Claude 返工`
- `Codex 接管修`
- `结束任务`
- `新工作台`

Each button must perform exactly what the text says. No button labeled
`继续` should start hidden implementation or verification work.

## Runtime Events And Auditability

Every stage transition must append an operator-visible runtime event with:

- `conversation_id`;
- `orchestration_run_id`;
- previous stage;
- next stage;
- user action when applicable;
- active agent;
- workspace alias.

The runtime event `aggregate_id` for workspace-related events must use the
actual conversation workspace alias, for example `workspace-lightfeev2`, never
the default workspace when the active conversation is bound elsewhere.

## Safety Invariants

1. Claude never starts from `/auto` without a user click.
2. Codex never writes code in `/auto` analysis or verification stages.
3. Plain text in `collecting_context` is context, not execution permission.
4. Trigger words do not grant execution permission.
5. A workflow stage cannot run if the conversation workspace lease is blocked by
   another running task.
6. `/codex` and `/claude` remain single-agent direct modes.
7. `/new` resets the workbench boundary.
8. The UI must show whether the workflow is waiting for user input, actively
   running Codex, actively running Claude, or closed.

## Testing Requirements

Unit and integration tests must prove:

- `/auto` starts Codex read-only analysis and does not start Claude.
- Plain text during `collecting_context` is appended to the active Codex
  analysis thread.
- `生成最终方案` stores a final plan and shows `交给 Claude 执行`.
- `交给 Claude 执行` starts Claude exactly once.
- Claude completion enters `claude_done` and does not auto-verify.
- `Codex 验收` starts Codex verification exactly once.
- failed verification creates a repair prompt and enters `retry_ready`.
- `发给 Claude 返工` starts Claude repair exactly once.
- `Codex 接管修` starts Codex direct work only after explicit click.
- old trigger-word classification tests are removed or rewritten around stage
  gates.
- button labels match their behavior.
- workspace aggregate ids use the active conversation workspace.

## Rollout Plan

Implement behind the existing `/auto` command directly. No separate feature flag
is needed if tests cover old auto behavior replacement.

Deployment should be local first:

1. run focused tests;
2. run full tests;
3. restart local `wlcodex.service`;
4. live smoke test:
   - `/new`;
   - switch to a non-default workspace;
   - `/auto <ambiguous investigation task>`;
   - send two plain-text context messages;
   - click `生成最终方案`;
   - verify no Claude run exists before clicking `交给 Claude 执行`.

## Open Decisions

The first implementation should keep using `orchestration_runs`. If storing
multiple historical prompts in only `last_codex_analysis` becomes too lossy,
a later migration can add a separate prompt-history table. That is not required
for the first usable version.
