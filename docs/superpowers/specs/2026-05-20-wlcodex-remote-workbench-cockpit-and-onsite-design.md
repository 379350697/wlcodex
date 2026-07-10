# WLCodex Remote Workbench Cockpit And Onsite Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

## Decision

WLCodex should move from command-shaped dual modes to a remote-workbench
product model.

The user-facing model is:

```text
One local remote workbench
  - shared conversation
  - shared workspace
  - shared Codex session references
  - shared Claude session references
  - shared approvals, diffs, runtime events, and recovery state

Two phone views over that workbench
  - Cockpit view: concise, phone-first progress and decisions
  - Onsite view: raw terminal-style live control
```

The current product/terminal split remains useful as a technical foundation, but
the final interaction should not feel like two systems. A normal user should
feel that work continues in one place and the phone can either show the cockpit
or open the live worksite.

The default business workflow remains:

```text
Codex analysis -> Claude implementation -> Codex verification
```

WLCodex must also support direct execution modes:

```text
Codex-only direct mode
Claude-only direct mode
Default orchestrated mode: Codex -> Claude -> Codex
```

Those are execution modes, not view modes. The user can view any execution mode
from Cockpit or Onsite.

## Why This Exists

The existing dual-surface design has the right architecture direction, but the
interaction is incomplete:

- `/terminal` behaves like a technical attach command.
- Users can enter terminal mode without an attached session.
- Terminal input depends on prior external session ids.
- The menu exposes implementation details instead of common user jobs.
- Product and terminal are presented as separate places instead of two views of
  the same live workbench.

The final design should capture the useful part of Claude Remote Control:

- the local machine owns execution
- the phone is a remote window into the same work
- disconnecting the phone does not kill local work
- returning from the phone restores the same live context
- session ids, thread ids, and attach mechanics stay behind the product surface

## Product Principles

### Complete Closed Loop

Every user job must have a full path:

```text
Start -> understand route -> run -> show progress -> ask for decisions
-> verify or finish -> inspect result -> continue, take over, or start new
```

No flow should end at "no session" without a next action.

### No Drift

There must be one durable source of truth for the workbench. Cockpit and Onsite
render different views of the same facts. They must not keep separate business
state that can disagree.

### Think Before Coding

Code-producing flows must keep the current default discipline:

```text
Codex thinks and scopes
Claude changes code when implementation is needed
Codex verifies the result
```

Claude-only mode is still allowed, but the product must make that explicit.
Codex-only mode is also allowed, and is best for analysis, review, design,
triage, and verification.

### Simplicity First

Expose only the smallest set of daily user actions:

```text
new workbench
status
take over live worksite
view diff
settings
help
```

Everything else can remain available through typed commands or nested settings.

### Precise Changes

Implementation should change the smallest code area needed for each business
behavior. Avoid broad rewrites of router, controller, runtime, or Telegram
delivery layers when a focused adapter or projection can carry the new behavior.

### Goal-Driven Execution

Every implementation task must define the user-visible success condition before
code is changed. Tests should prove routing, view switching, and recovery
behavior in terms of user outcomes.

## User Mental Model

### Names

Use these concepts in user-facing text:

| Internal concept | User-facing concept | Meaning |
| --- | --- | --- |
| Product Surface | Cockpit | concise progress, decisions, summaries |
| Terminal Surface | Onsite | live worksite, raw output, direct input |
| Conversation/session ids | Workbench | the one ongoing local work context |
| Attach/detach | Open/leave live worksite | phone view changes, local work continues |
| Direct agent mode | Ask Codex / Ask Claude | explicit one-agent route |
| Orchestration | Default engineer workflow | Codex -> Claude -> Codex |

The product can still accept `/product` and `/terminal` for compatibility, but
help text and buttons should prefer "驾驶舱" and "接管现场".

## Execution Modes

Execution mode controls who does the work. View controls how the user sees and
steers the work. These dimensions must stay separate.

### Default Orchestrated Mode

Default for ordinary text in Cockpit.

```text
User asks for work
Codex analyzes scope, risk, and plan
Claude implements when code changes are needed
Codex verifies implementation evidence
Cockpit summarizes result and next actions
Onsite can show raw activity at any time
```

User-facing copy:

```text
我会按默认工程流程处理：Codex 先分析，Claude 实施，Codex 再验收。
```

### Codex-Only Direct Mode

Explicit route for analysis, design, review, verification, and light non-code
answers.

Entry points:

```text
/codex <prompt>
settings button: 只问 Codex
route chip on a task card: Codex
```

Behavior:

- Starts or continues a Codex direct run in the same workbench.
- Does not call Claude.
- Can still expose Onsite raw Codex events when available.
- Can transition to default orchestrated mode only if the user explicitly asks
  for implementation.

User-facing copy:

```text
这次只交给 Codex，不会调用 Claude 修改代码。
```

### Claude-Only Direct Mode

Explicit route for hands-on implementation by Claude.

Entry points:

```text
/claude <prompt>
settings button: 只叫 Claude
route chip on a task card: Claude
```

Behavior:

- Starts or continues a Claude direct run in the same workbench.
- Does not ask Codex to analyze first or verify after.
- Cockpit must clearly label that verification is not automatic.
- Onsite should open Claude raw output and direct input naturally.
- Cockpit should offer "让 Codex 验收" after Claude-only work completes.

User-facing copy:

```text
这次直接交给 Claude 实施。完成后你可以点“让 Codex 验收”。
```

### Mode Memory

The workbench should remember the current execution mode only for the active
run. New ordinary text after a completed run returns to default orchestrated
mode unless the user explicitly chooses Codex-only or Claude-only again.

This prevents a direct one-agent task from silently changing the long-term
default.

## Views

### Cockpit View

Cockpit is the default phone view.

It shows:

- current workbench title and workspace
- current execution mode
- current phase
- active agent
- concise progress text
- approval cards
- final result summary
- small action buttons

It hides:

- raw JSON
- internal session ids
- raw app-server thread ids
- long stdout/stderr
- full diffs by default
- raw tool traces unless requested

Example:

```text
WLCodex

任务：重做远程终端手机体验
流程：Codex -> Claude -> Codex
阶段：Claude 正在实施
工作区：wlcodex

Claude 正在修改终端接管体验。当前已完成菜单梳理，正在处理 terminal 接入逻辑。

[接管现场] [查看 diff] [停止] [补充要求]
```

### Onsite View

Onsite is the raw live worksite view.

It shows:

- selected agent
- active phase
- live terminal-style output
- recent command/tool/diff frames
- tail controls
- pause/resume delivery controls
- return-to-cockpit control

It accepts normal text as direct input to the selected live session.

Example:

```text
现场 · Claude · implementation · running

$ pytest tests/test_terminal_surface.py -q
F
...
AssertionError: expected live tail to resume

[回驾驶舱] [暂停推送] [查看尾部] [停止运行]
```

Onsite must not require the user to know whether the backing transport is
Claude Remote Control, stream-json, Codex app-server, JSONL exec, or PTY.

## View Switching

Switching views must never restart work.

### Cockpit To Onsite

User actions:

```text
tap 接管现场
/terminal
```

Routing:

1. If a run is active, select the active agent.
2. If Claude is implementing, select Claude.
3. If Codex is analyzing or verifying, select Codex.
4. If there is an attached onsite session for that agent, resume it.
5. If there is an external session id but no attached onsite reference, attach
   it automatically.
6. If no live session exists, show a start card instead of entering a dead view.

Start card:

```text
当前没有可接管的现场。

你可以：
[启动 Claude 现场] [启动 Codex 现场] [回驾驶舱]
```

### Onsite To Cockpit

User actions:

```text
tap 回驾驶舱
/product
```

Routing:

1. Stop sending raw terminal frames into the current Telegram live feed.
2. Keep the local workbench and underlying process running.
3. Record the Onsite cursor.
4. Render one compact Cockpit status message.
5. Do not replay raw terminal output into Cockpit.

Copy:

```text
已回到驾驶舱。现场仍在运行，我会继续用摘要跟进。
```

## Menu Design

The Telegram bot command menu should optimize for the daily phone experience.

Recommended visible menu:

| Command | Label | Purpose |
| --- | --- | --- |
| `/new` | 新工作台 | start a fresh workbench |
| `/status` | 状态 | show active workbench |
| `/terminal` | 接管现场 | open Onsite |
| `/diff` | 变更 | inspect file changes |
| `/settings` | 设置 | route, model, permissions, workspace |
| `/help` | 帮助 | compact guide |

Typed commands can remain available:

```text
/codex <prompt>
/claude <prompt>
/auto <prompt>
/model
/claude_mode
/sessions
/switch <workspace>
/health
/files
```

Legacy diagnostic commands can remain hidden:

```text
/task /continue /steer /tail /events /pause /abort /archive /fork
```

## Startup And First Use

Startup should present product readiness, not configuration internals.

`/start` or `/help` in the default state:

```text
WLCodex 已连接

默认流程：Codex -> Claude -> Codex
当前视图：驾驶舱
工作区：wlcodex
Codex：可用
Claude：可用
现场接管：可用

直接发消息开始。

[新工作台] [接管现场] [设置]
```

If Claude is disabled:

```text
WLCodex 已连接

默认流程需要 Claude，但当前 Claude 未启用。
你仍然可以只问 Codex，或在设置中启用 Claude 后使用完整工程流程。

[只问 Codex] [设置] [帮助]
```

If Onsite is not available:

```text
现场接管当前不可用。驾驶舱仍可正常工作。
```

Do not surface `terminal.enabled` to normal users. Configuration keys can appear
in operator diagnostics, not primary product copy.

## Workbench State

The remote workbench is the durable shared state behind both views and all
execution modes.

It should include:

- workbench id, mapped to the active conversation id
- chat id
- workspace alias
- active view: cockpit or onsite
- active execution mode: orchestrated, codex_direct, claude_direct
- active phase
- active agent
- active Codex thread id when present
- active Codex turn id when present
- active Claude session id when present
- active onsite session references by agent
- cockpit cursor
- onsite cursor
- latest diff summary
- pending approvals
- pending user context
- latest user-visible message ids when needed for edits

The runtime event log is the source of truth. Projection tables or in-memory
managers are caches.

## Events

Use durable events for workbench facts. Event names can match existing runtime
style.

Required event concepts:

```text
workbench.created
workbench.view.changed
workbench.execution_mode.selected
workbench.route.decided
onsite.session.started
onsite.session.attached
onsite.session.detached
onsite.session.orphaned
onsite.input.sent
onsite.output.frame
onsite.cursor.advanced
cockpit.cursor.advanced
cockpit.summary.rendered
approval.requested
approval.resolved
diff.updated
run.completed
run.failed
```

The final naming can reuse existing event constants where they already exist.
The important requirement is that replay reconstructs the same workbench.

## Routing Rules

### Plain Text In Cockpit

If there is no active run:

```text
ordinary text -> default orchestrated run
/codex text -> Codex-only run
/claude text -> Claude-only run
/auto text -> default orchestrated run
```

If a default orchestrated run is in analysis or waiting for user input:

```text
ordinary text -> Codex context for the current run
```

If a default orchestrated run is in Claude implementation or Codex verification:

```text
ordinary text -> pending context for Codex review at the next safe boundary
```

If a Claude-only direct run is active:

```text
ordinary text -> Claude direct session unless user chooses Cockpit review
```

If a Codex-only direct run is active:

```text
ordinary text -> Codex direct session
```

### Plain Text In Onsite

```text
ordinary text -> selected onsite session input
```

Onsite text must never call the Cockpit product controller unless the user
returns to Cockpit first.

### Commands

Commands are explicit route overrides. They must not be interpreted as normal
terminal input unless the user has a separate "send literal command" affordance.

## Approvals

Approvals are shared workbench decisions.

- Cockpit shows concise approval cards.
- Onsite can show raw command context.
- A decision in either view resolves the same approval id.
- Duplicate or stale callbacks are rejected.
- Approval resolution is recorded before the response goes back to the agent.

## Diff And Inspection

Diffs and touched files are shared workbench facts.

Cockpit:

```text
summary -> file count -> buttons
```

Onsite:

```text
raw or near-raw diff frames, redacted and chunked
```

`/diff` should work from either view and render according to the current view.

## Recovery

On daemon restart:

1. Replay runtime events.
2. Rebuild active workbench state.
3. Restore Cockpit and Onsite cursors.
4. Reattach live sessions when transport supports it.
5. Mark missing local processes as orphaned.
6. Keep Cockpit usable if Onsite reattach fails.
7. Show a concise recovery card.

Recovery copy:

```text
WLCodex 已恢复。

当前工作仍在驾驶舱中可见。
现场会话已断开，可以重新接管或继续从摘要处理。

[接管现场] [状态] [新工作台]
```

## Security

- Keep private-chat and allowlist enforcement.
- Redact tokens, SSH keys, OAuth tokens, API keys, `.env` values, and known
  secret patterns from Onsite frames.
- Cockpit must never summarize raw secrets.
- Onsite must make raw control explicit.
- Starting a live worksite should respect existing sandbox and approval policy.
- No public inbound listener is introduced.
- Official remote-control transports may use outbound services only when the
  operator has configured them.

## Failure Behavior

| Failure | Cockpit behavior | Onsite behavior |
| --- | --- | --- |
| No live session | Show start card | Show start card |
| Attach fails | Stay in Cockpit with next action | Show retry and return controls |
| Onsite output too long | Show compact notice | Chunk or tail |
| Onsite process exits | Show completion or orphan notice | Mark ended and offer restart |
| Cockpit renderer fails | Send short fallback status | No impact |
| Approval stale | Reject button and explain | Reject button and explain |
| Telegram edit fails | Outbox retries or sends fallback | Outbox retries or sends fallback |
| Restart loses process | Rebuild workbench, mark Onsite orphaned | Offer reconnect or new session |

## Migration From Current Dual Surface

Keep the current modules as starting points:

```text
wlcodex/surfaces/core
wlcodex/surfaces/product
wlcodex/surfaces/terminal
```

Rename behavior gradually in product copy:

```text
product -> cockpit
terminal -> onsite
mode switch -> view switch
terminal attach -> open live worksite
terminal detach -> leave live worksite
```

Compatibility commands stay accepted:

```text
/product
/terminal
/terminal claude
/terminal codex
/terminal tail
/terminal pause
/terminal detach
```

The primary product path should prefer:

```text
[接管现场]
[回驾驶舱]
[查看尾部]
[暂停推送]
```

## Acceptance Criteria

1. Sending ordinary text in Cockpit starts the default Codex -> Claude -> Codex
   workflow.
2. `/codex <prompt>` runs Codex-only and never calls Claude.
3. `/claude <prompt>` runs Claude-only and does not trigger automatic Codex
   analysis or verification.
4. Claude-only completion offers a "让 Codex 验收" action.
5. `/terminal` or "接管现场" never leaves the user in a dead session state.
6. Onsite automatically attaches to the active agent when a session exists.
7. Onsite offers start actions when no session exists.
8. Onsite text routes only to the selected live session.
9. Returning to Cockpit does not replay raw terminal output.
10. Cockpit and Onsite maintain independent cursors.
11. Restart recovery reconstructs active view, execution mode, and session
    status from events.
12. Menu contains only daily phone actions.
13. Help text explains the product in user language, not configuration keys.
14. Raw terminal output is redacted before Telegram delivery.
15. Tests prove that view switching does not restart work.

## Non-Goals

- Do not build a web dashboard in this scope.
- Do not replace official Claude Remote Control.
- Do not make Onsite the default view for ordinary users.
- Do not remove legacy commands in the same change.
- Do not rewrite the orchestration runner unless a specific acceptance test
  proves the current boundary cannot support the workbench model.
- Do not expose session ids in primary user copy.
