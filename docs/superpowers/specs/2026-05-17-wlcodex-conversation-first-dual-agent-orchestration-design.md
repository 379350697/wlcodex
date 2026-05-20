# WLCodex Conversation-First Dual-Agent Orchestration Design

> Superseded in user semantics by the 2026-05-20 Remote Workbench repair.
> Conversation/task wording below is historical context. The current product
> object is one continuing Workbench until `/new`; tasks are internal execution
> records or legacy diagnostics only.

## Final Decision

WLCodex v2 is a conversation-first engineering cockpit, not a task-first Telegram bot.

The primary product surface is a human-friendly chief-engineer conversation entry. The user talks naturally. WLCodex routes the intent to Codex, Claude Code, or a Codex-led orchestration loop. Existing tasks, SQLite state, approvals, write locks, worktrees, and watchdogs remain the internal runtime. They must not remain the user's primary mental model.

The final direction is:

```text
User conversation
  -> WLCodex conversation layer
  -> Codex chief engineer by default
  -> Claude Code implementation when useful
  -> Codex verification before closure
  -> TaskService, Ledger, approvals, worktree, watchdog as hidden runtime
```

This design intentionally keeps WLCodex as the base. Ductor, TeleCodex, and CodexClaw remain reference material for interaction patterns, provider abstractions, streaming, and menus. They are not the main migration target.

## Product Principles

1. Conversation is the default.
   A normal text message should feel like talking to a capable engineer, not operating a ticket system.

2. Codex is the default brain.
   Codex owns analysis, architecture, prompt shaping, verification criteria, review, and final closure.

3. Claude Code is an implementation engineer.
   Claude Code can be delegated implementation work by Codex or addressed directly by the user.

4. Direct channels stay available.
   The user must be able to talk directly to Codex or Claude Code without triggering the full orchestration loop.

5. Token overhead is a hard constraint.
   Human-friendly Telegram conversation must not mean dumping the full Telegram transcript into Codex or Claude. Model-side inputs must be compact task packets.

6. Task is an internal word.
   Existing task IDs can stay for diagnostics and legacy commands, but new primary UX should say conversation, run, step, review, diff, or workspace.

7. Existing reliability is preserved.
   TaskService, Ledger, JSON-RPC Codex integration, approval flow, workspace locks, worktree isolation, watchdog, and inspection commands are core assets.

## User Modes

### 1. Chief Engineer Mode

Chief Engineer Mode is the default for plain text messages.

Example:

```text
User: 这个 bug 帮我修了

WLCodex: 我先分析原因和验收标准。
Codex: 问题在 auth.py 的空值路径。修复范围是 auth.py 和 tests/test_auth.py。
WLCodex: 我交给 Claude Code 实施。
Claude Code: 已修改 2 个文件，新增 1 个测试。
WLCodex: Codex 正在验收。
Codex: 验收通过。可以查看 diff 或继续修改。
```

Chief Engineer Mode may choose one of three routes:

| Route | When Used | Model Calls |
| --- | --- | --- |
| Codex-only answer | Explanation, design, review, prompt generation, non-writing questions | Codex |
| Claude-only execution | User asks for a simple direct code edit and verification is not required | Claude Code |
| Codex-Claude-Codex loop | Bug fix, feature work, risky refactor, acceptance-required work | Codex analyze, Claude implement, Codex verify |

The default route should prefer Codex analysis when intent is ambiguous. It should not always force three model turns.

### 2. Codex Direct Mode

Codex Direct Mode sends the request only to Codex.

Entry points:

```text
/codex 分析这个模块边界
@codex 这个 PR 风险在哪里
```

Codex Direct Mode is for:

- architecture and design
- debugging analysis
- code review and verification
- writing implementation prompts for Claude Code
- summarizing diffs, tests, and risks

Codex Direct Mode must not automatically call Claude Code.

### 3. Claude Direct Mode

Claude Direct Mode sends the request only to Claude Code.

Entry points:

```text
/claude 按这个方案修改 auth.py
@claude 补一个失败测试
```

Claude Direct Mode is for:

- direct implementation
- small code edits
- test fixes
- mechanical refactors
- applying a prompt prepared by Codex

Claude Direct Mode must not automatically ask Codex to verify unless the user clicks a verification button or uses `/verify`.

### 4. Explicit Orchestration Mode

The user may explicitly start a Codex-led loop.

Entry points:

```text
/auto 修复这个登录 bug
/verify
```

`/auto` forces the Codex-Claude-Codex loop.
`/verify` asks Codex to verify the latest direct Claude run or current workspace diff.

## Human-Friendly Telegram UX

### Plain Text Handling

Non-command text must be accepted as a first-class input.

Rules:

- If there is an active conversation in the chat, continue it.
- If no active conversation exists, create one in the default workspace.
- If the message starts with `/codex`, route to Codex Direct Mode.
- If the message starts with `/claude`, route to Claude Direct Mode.
- If the message starts with `/auto`, route to Chief Engineer Mode with forced orchestration.
- Legacy task commands remain available for diagnostics and compatibility.

### Telegram Menu Commands

WLCodex must register Telegram BotCommands so the user can discover common actions through the Telegram menu.

Primary commands:

| Command | Label | Purpose |
| --- | --- | --- |
| `/new` | 新对话 | Start a new visible conversation |
| `/codex` | 问 Codex | Direct Codex conversation |
| `/claude` | 叫 Claude | Direct Claude Code conversation |
| `/auto` | 总工程师模式 | Force Codex-led orchestration |
| `/stop` | 停止当前运行 | Stop the active run or turn |
| `/status` | 当前状态 | Show the current conversation and active runs |
| `/sessions` | 会话列表 | Show visible conversations and direct agent sessions |
| `/switch` | 切换工作区 | Change the default workspace for the chat |
| `/model` | 切换模型 | Change model or reasoning profile for the active agent |
| `/diff` | 查看 diff | Show diff for the current conversation or latest run |
| `/files` | 相关文件 | Show touched files for the current conversation |
| `/verify` | Codex 验收 | Ask Codex to verify the latest result |
| `/health` | 系统健康 | Show backend health and queue health |
| `/help` | 帮助 | Show concise help |

Legacy commands may remain hidden from the menu:

- `/task`
- `/continue`
- `/steer`
- `/tail`
- `/events`
- `/archive`
- `/fork`
- `/codex-sessions`

The legacy commands remain useful for operator-level debugging, but they should not be the primary user journey.

### Inline Buttons

Model replies and status messages should expose likely next actions as inline buttons.

Common buttons:

| Button | Action |
| --- | --- |
| 查看 diff | Run `/diff` for current conversation |
| 相关文件 | Run `/files` for current conversation |
| Codex 验收 | Run `/verify` |
| 交给 Claude | Convert the current Codex result into a Claude handoff packet |
| 继续修改 | Continue the active conversation |
| 新对话 | Start `/new` |
| 停止 | Stop the current run |
| 保持排队 | Keep a waiting run queued |
| 隔离 worktree 并行 | Start blocked work in an isolated worktree |
| 中止阻塞任务 | Abort the current blocker and start the waiting run |
| 合并 worktree | Merge completed isolated work |
| 丢弃 worktree | Discard isolated work |

Approval buttons remain protocol-critical and should keep their current precision:

- 批准一次
- 本会话批准
- 拒绝
- 取消

### Typing and Streaming

WLCodex should show progress without overwhelming Telegram.

Requirements:

- Send typing indicators while Codex or Claude is active.
- Render agent deltas through throttled message edits.
- Keep Telegram edit frequency within safe limits.
- Fall back to sending a new message if edit fails for a reason other than "message is not modified".
- Do not stream internal status noise into model prompts.

### Humanized Status Text

Status should describe the conversation, not expose the task engine first.

Preferred wording:

```text
当前对话：登录 bug 修复
模式：总工程师
工作区：wlcodex
步骤：Claude Code 正在实施
轮次：第 1 轮
变更文件：2
待审批：0
```

Legacy diagnostic status may still show task IDs behind an "advanced details" section.

## Token Budget Design

Conversation-first UX must not become context-dump UX.

### Separated Logs

There are two different histories:

| History | Stored In | Sent To Models |
| --- | --- | --- |
| Human conversation transcript | Ledger or conversation store | No, except selected recent user intent |
| Agent execution log | Task events and backend events | No, except selected summaries |
| Model context packet | Built per turn | Yes |

The Telegram transcript is user-facing memory. It is not the model prompt.

### Context Packet

Each model call receives a compact packet built for that call.

Common fields:

```text
mode:
workspace:
user_goal:
current_request:
conversation_summary:
relevant_files:
recent_user_constraints:
acceptance_criteria:
token_budget:
```

Codex analysis packet adds:

```text
requested_output:
  - concise root cause or design judgment
  - implementation plan if code changes are needed
  - acceptance criteria for verification
```

Claude implementation packet adds:

```text
handoff_from_codex:
  objective:
  files_to_touch:
  steps:
  constraints:
  acceptance_criteria:
  prohibited_changes:
```

Codex verification packet adds:

```text
original_goal:
codex_plan_summary:
claude_completion_summary:
changed_files:
diff_excerpt_or_summary:
test_results:
verification_question:
```

### Handoff Rules

Codex to Claude:

- Send the objective, exact implementation steps, constraints, and acceptance criteria.
- Do not send the full Codex reasoning unless the reasoning itself is necessary.
- Do not send the full Telegram transcript.
- Prefer paths, functions, and requirements over prose.

Claude to Codex:

- Send completion summary, files changed, tests run, and diff summary.
- Include a diff excerpt only when needed.
- Prefer `git diff --stat` plus targeted hunks over full diffs.
- Include failures and uncertainty explicitly.

### Budget Targets

Default budget targets:

| Packet | Target |
| --- | --- |
| User plain text to Codex | original user text plus at most one compact conversation summary |
| Codex to Claude handoff | under 1,500 tokens |
| Claude to Codex verification packet | under 2,500 tokens unless user requests full review |
| Conversation summary refresh | under 800 tokens |
| Status rendering | never injected into prompts |

Hard limits should be configurable. If a packet exceeds its limit, WLCodex should summarize, trim older context, or ask the user to narrow scope.

## Architecture

### Preserved Core

These existing modules remain foundational:

| Module | Role |
| --- | --- |
| `wlcodex/task_service.py` | Internal run reservation, task lifecycle, workspace queue, worktree operations |
| `wlcodex/db.py` | SQLite ledger, migrations, events, approvals, touched files, Telegram audit |
| `wlcodex/codex_backend.py` | Codex app-server JSON-RPC backend, structured events, thread and turn control |
| `wlcodex/event_bridge.py` | Backend event consumption and status update integration |
| `wlcodex/approval.py` | Approval callback encode, decode, resolve |
| `wlcodex/inspection.py` | Tail, events, diff, file inspection |
| `wlcodex/status.py` | Pure render helpers |
| `wlcodex/telegram_app.py` | Telegram transport and authenticated handlers |

### New Components

| New Module | Responsibility |
| --- | --- |
| `wlcodex/conversation.py` | Conversation state, active mode, direct sessions, current workspace, current visible run |
| `wlcodex/context_packets.py` | Compact model packet building and token-budget enforcement |
| `wlcodex/agent_backend.py` | Shared interface for Codex and Claude agent adapters |
| `wlcodex/claude_backend.py` | Claude Code subprocess adapter and event normalization |
| `wlcodex/orchestrator.py` | Chief Engineer routing, Codex-Claude-Codex loop, verification retry logic |
| `wlcodex/menu.py` | Telegram BotCommands and menu labels |
| `wlcodex/streaming.py` | Throttled streaming renderer for Telegram edits |

### Modified Components

| Existing Module | Required Change |
| --- | --- |
| `wlcodex/models.py` | Add conversation and orchestration data models |
| `wlcodex/db.py` | Add conversation and orchestration tables with migrations |
| `wlcodex/router.py` | Add conversation commands and direct-agent commands while preserving legacy task commands |
| `wlcodex/controller.py` | Keep command controller, add or delegate to conversation controller for new routes |
| `wlcodex/telegram_app.py` | Add non-command MessageHandler, register menu commands, add typing and streaming hooks |
| `wlcodex/status.py` | Add humanized conversation status renderers |
| `wlcodex/config.py` | Add conversation, Claude, orchestration, menu, streaming, and token-budget config |

## Data Model

Add lightweight conversation records without replacing existing tasks.

### ConversationSession

Fields:

```text
id
chat_id
user_id
title
mode
workspace_alias
active_codex_task_id
active_claude_run_id
conversation_summary
current_model
created_at
updated_at
archived_at
```

### AgentRun

Fields:

```text
id
conversation_id
agent
role
status
hidden_task_id
external_session_id
prompt_packet_summary
token_input
token_output
created_at
updated_at
```

For Codex, `hidden_task_id` can point to the existing `tasks.id`.
For Claude Code, it can point to a Claude-specific run record until Claude is fully integrated with TaskService.

### OrchestrationRun

Fields:

```text
id
conversation_id
goal
status
current_step
verify_round
max_verify_rounds
last_codex_analysis
last_claude_summary
last_verification_result
created_at
updated_at
```

### OrchestrationDecision

Fields:

```text
run_id
decision
reason
next_agent
created_at
```

Allowed decisions:

- codex_only
- claude_only
- delegate_to_claude
- verify_passed
- verify_failed_retry
- verify_failed_stop
- needs_user_input

## Routing Rules

### Message Routing

```text
incoming Telegram update
  -> auth guard
  -> audit update in Ledger
  -> command?
       yes: parse command
       no: route as conversation message
  -> conversation controller
  -> mode-specific handler
```

### Default Plain Text Routing

If text is not a command:

1. Load or create the active conversation for `chat_id`.
2. Read the active mode.
3. Build a compact context packet.
4. Route according to mode and intent.
5. Store only the summary needed for future turns.

### Legacy Command Compatibility

Existing commands must continue to work.

Legacy command behavior:

- `/task <workspace> <prompt>` still creates an internal Codex task.
- `/continue <id> <prompt>` still continues the exact task.
- `/steer <id> <prompt>` still steers the exact active turn.
- `/diff <id>` still works by task ID.

New command behavior:

- `/diff` without an ID resolves to the current conversation's latest run.
- `/files` without an ID resolves to the current conversation's latest run.
- `/status` prefers conversation status, with task details available when needed.

## Orchestration Loop

### Default Loop

```text
User goal
  -> Codex analyze
  -> HandoffPacket
  -> Claude implement
  -> ReviewPacket
  -> Codex verify
  -> pass: user-visible closure
  -> fail: feedback packet to Claude
  -> retry until pass or max rounds
```

### Verification Contract

Codex must verify against:

- original user goal
- Codex's own acceptance criteria
- actual files changed
- test results
- constraints from the conversation
- token-budget limits and omitted context disclosures

Codex verification output must be machine-readable enough for WLCodex to route:

```text
decision: pass | retry | stop | need_user
summary:
required_fix:
confidence:
```

The first implementation can parse this with strict text markers. A later version can use JSON if the selected model and backend reliably preserve structured output.

### Retry Rules

Default retry policy:

- `max_verify_rounds = 3`
- retry only when Codex gives actionable feedback
- stop and ask the user when Codex says the goal is ambiguous
- stop and ask the user when the required fix exceeds the current scope
- preserve all retry summaries in Ledger, not in prompt history

## Error Handling

| Failure | Behavior |
| --- | --- |
| Claude Code unavailable | Tell user, offer Codex-only prompt export or retry |
| Codex unavailable | Tell user, offer Claude direct mode or retry |
| Workspace busy | Use existing queue and worktree options through humanized buttons |
| Approval required | Use existing approval cards and callbacks |
| Token budget exceeded | Summarize or ask user to narrow scope |
| Streaming edit fails | Fall back to send-message path |
| Verification inconclusive | Ask user or stop after summary |
| Max verification rounds reached | Show unresolved points and offer continue or stop |

## Configuration

Add config sections:

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

`claude.enabled = false` should keep the system usable as Codex-only until the Claude backend is configured.

## Acceptance Criteria

1. Plain text creates or continues a conversation without requiring `/task`.
2. `/codex` talks only to Codex.
3. `/claude` talks only to Claude Code when enabled.
4. `/auto` runs the Codex-Claude-Codex loop when both agents are available.
5. `/verify` asks Codex to verify the latest Claude or workspace result.
6. Telegram menu commands are registered and ordered around daily use.
7. Replies expose relevant inline buttons.
8. Existing task commands continue to work.
9. Status and help no longer make task IDs the primary daily UX.
10. Model prompts use compact context packets, not full Telegram transcripts.
11. Tests prove status/log text is not injected into model prompts.
12. Existing approvals, workspace queue, worktree actions, and watchdog behavior continue to work.
13. Token usage can be inspected by conversation and by hidden run.
14. The system can operate in Codex-only mode when Claude is disabled.
15. The user can inspect advanced task details when needed.

## Non-Goals

- Do not fork ductor as the main implementation.
- Do not remove the existing TaskService state machine.
- Do not remove legacy commands in the first version.
- Do not send full Telegram history to Codex or Claude by default.
- Do not make `/pipeline` the primary UX.
- Do not require Claude Code for simple Codex-only interactions.
- Do not hide approval requests or weaken permission boundaries.

## Rollout Strategy

1. Add conversation state and menu commands while preserving legacy behavior.
2. Add plain text routing to Codex Direct Mode first.
3. Add compact context packet tests before adding orchestration.
4. Add Claude Direct Mode behind `claude.enabled`.
5. Add Chief Engineer orchestration behind `orchestration.enabled`.
6. Make conversation status the default `/status`.
7. Keep legacy task commands documented under advanced help.
8. Review token usage before making orchestration the default for ambiguous implementation requests.

## Self-Review

- The design preserves WLCodex's existing reliability core.
- The user-facing default is conversation-first.
- Codex and Claude direct channels are explicit requirements.
- Menus, buttons, typing, streaming, and humanized status are included.
- Token overhead is treated as a hard architectural constraint.
- Legacy task commands remain available as an operator path.
- The design avoids making a generic multi-CLI bot the main product shape.
