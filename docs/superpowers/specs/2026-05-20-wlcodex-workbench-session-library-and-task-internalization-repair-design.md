# WLCodex Workbench Session Library And Task Internalization Repair Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

## Status

This spec is a repair addendum to:

- `docs/superpowers/specs/2026-05-20-wlcodex-remote-workbench-cockpit-and-onsite-design.md`
- `docs/superpowers/specs/2026-05-19-wlcodex-dual-surface-product-and-terminal-mode-design.md`

It does not replace the Remote Workbench direction. It tightens the user model
after live Telegram testing exposed two product gaps:

- the user expects one continuing Workbench until `/new`
- the user must be able to reopen historical Codex and Claude sessions, review
  them, and continue work from them

## Decision

WLCodex must expose one durable user object:

```text
Remote Workbench
  - Cockpit view
  - Onsite view
  - default engineering flow: Codex -> Claude -> Codex
  - Codex-only execution mode
  - Claude-only execution mode
  - Codex historical sessions
  - Claude historical sessions
```

The internal implementation can keep tasks, agent runs, thread ids, session ids,
runtime events, and queue locks, but these are not user concepts.

Final terminology:

```text
Workbench = the user's continuing work context
Agent Session = a resumable Codex or Claude worksite inside the Workbench
Task = an internal execution ticket and workspace lock record
Execution Lane = the single active run lane for a Workbench
Cockpit = product cockpit view
Onsite = raw live worksite view
```

Only `/new` creates a new Workbench. Commands like `/terminal`, `/product`,
`/sessions`, `/codex`, and `/claude` operate inside the current Workbench unless
the user explicitly starts a new one.

## Problems Found

### Workbench Identity Drift

Live testing showed `/terminal` could respond with "对话不存在或已被删除。"
even after the user had an active Telegram conversation. The underlying product
problem is that some callback paths can use Telegram chat identity where they
must use the active Workbench conversation identity.

User expectation:

```text
/new starts my workbench.
Everything after that stays in the same workbench until I use /new again.
```

### Task Leaks Into User Experience

Live testing showed a user-visible waiting card:

```text
任务 #53 — 等待工作区空闲
阻塞者：#52（排队中）
队列位置：第 1 位
```

That is a diagnostic view, not a product experience. The correct user-facing
message would be about the current Workbench being busy and what the user can
do next.

### Claude-Only Can Leave A Hidden Task Blocking The Workspace

Claude-only execution must be a normal internal execution ticket. It must move
through `queued -> running -> done / failed / aborted / orphaned` and release
the workspace lock on every terminal path.

### Historical Agent Sessions Are Not Productized

The codebase already has primitives for historical session resume:

- Claude stream-json captures a Claude `session_id`
- Claude terminal input can run `claude --resume <session_id> -p <text>`
- Codex app-server can call `thread/resume`
- `agent_runs.external_session_id` can persist external session references
- `/sessions` exists, but currently behaves like a technical command

The missing product capability is a Workbench-level session library: users must
be able to list, inspect, attach, and continue historical Codex and Claude
sessions without seeing raw ids.

## Product Model

### Remote Workbench

The Workbench is the user's session. It persists across ordinary messages,
view switches, execution mode changes, and historical session resumes.

It owns:

- Telegram chat binding
- active conversation id
- workspace alias
- current view mode
- current execution mode
- current execution lane state
- Codex and Claude historical session summaries
- Cockpit cursor
- Onsite cursor
- pending verification actions
- recovery state

### Agent Session

An Agent Session is a user-browsable historical worksite for one agent.

Examples:

```text
Claude 现场 · 今天 11:08 · 修复 Telegram 接管逻辑 · 可继续
Codex 现场 · 今天 10:42 · 验收 Workbench 语义 · 可回顾
Claude 现场 · 昨天 23:10 · 修复 runtime recovery · 可从摘要继续
```

An Agent Session can be backed by:

- a Codex app-server thread id
- a Codex JSONL exec session id
- a Claude session id
- a terminal/PTY session reference
- an agent run projection when only summary/history is available

User-visible session cards must never show the raw backing id.

### Task

Task is strictly internal.

Task responsibilities:

- reserve workspace execution
- serialize work in the Workbench execution lane
- track execution lifecycle
- support restart recovery
- connect agent runs to workspace changes and diagnostics

Task is not:

- a Workbench
- a conversation
- a user session
- a menu item for normal phone users

The `/task` command may remain as a developer/legacy diagnostic command, but it
must not appear as a primary mobile entry point.

### Execution Lane

Each Workbench has one primary execution lane.

Rules:

| Workbench state | Ordinary text behavior |
| --- | --- |
| idle in Cockpit | start default `Codex -> Claude -> Codex` flow |
| running in Cockpit | append/steer current run, or show explicit choices |
| idle in Onsite with selected session | send input to selected session |
| running in Onsite | send input to selected session |
| no Onsite session selected | show session picker or start card |

Explicit commands can request a new execution, but if the lane is busy the user
must see clear product choices:

```text
追加到当前执行
等当前执行结束
停止当前并执行这条
新开 Workbench
```

The system must never silently stack hidden tasks that fight for the same
workspace lock.

## User Flows

### New Workbench

```text
User: /new 修复 Telegram 工作台体验
System: creates Workbench and Cockpit summary
User: ordinary text
System: uses same Workbench
User: /terminal
System: opens Onsite for the same Workbench
User: /product
System: returns to Cockpit for the same Workbench
```

No flow above creates a second Workbench.

### Default Ordinary Text

In Cockpit, ordinary text starts or continues the default engineering flow:

```text
Codex analysis -> Claude implementation -> Codex verification
```

If execution is already active, ordinary text is treated as additional context
or steering input for that lane. It does not create a competing task.

### Codex-Only

`/codex <prompt>` runs Codex-only inside the current Workbench.

Requirements:

- must not call Claude
- creates an Agent Session summary for Codex when a resumable thread/session is
  available
- can be viewed in Cockpit or Onsite
- stays out of automatic Claude implementation unless the user explicitly asks

### Claude-Only

`/claude <prompt>` runs Claude-only inside the current Workbench.

Requirements:

- must not auto-trigger Codex analysis
- must not auto-trigger Codex verification
- must persist the Claude session identity when Claude provides one
- must create or update a Claude Agent Session summary
- after completion, Cockpit offers "让 Codex 验收"

### Open Onsite

`/terminal` or "接管现场" opens Onsite for the current Workbench.

Decision order:

1. If there is an active Onsite session, open it.
2. Else if there is an active agent session from the current execution lane,
   attach it.
3. Else if there are historical Agent Sessions, show the session picker.
4. Else show a start card with "让 Claude 开始", "让 Codex 开始", and "回驾驶舱".

It must not return a dead-session message.

### Historical Session Picker

`/sessions` or "历史现场" shows current Workbench sessions:

```text
历史现场

Claude 现场 · 今天 11:08 · 修复 Telegram 接管逻辑 · 可继续
[查看回顾] [接管现场] [继续修改] [让 Codex 验收]

Codex 现场 · 今天 10:42 · 验收 Workbench 语义 · 可回顾
[查看回顾] [接管现场] [继续修改]
```

Button behavior:

- `查看回顾`: show summary, last known status, and available next actions
- `接管现场`: open Onsite attached to that historical session
- `继续修改`: resume the selected agent session with new user input
- `让 Codex 验收`: only for Claude sessions with completed or changed work
- `从摘要新开`: shown when the raw session cannot be resumed

### Resume Historical Claude Session

When a user continues a historical Claude session:

```text
selected Claude Agent Session -> claude --resume <stored id> -p <new text>
```

The new execution creates a new internal task/run record linked to the selected
Agent Session. It does not create a new Workbench and does not overwrite the
old history.

### Resume Historical Codex Session

When a user continues a historical Codex session:

```text
selected Codex Agent Session -> thread/resume -> turn/start
```

The new execution creates a new internal task/run record linked to the selected
Agent Session. It does not call Claude unless the selected execution mode or
user action requires it.

### Return To Cockpit

`/product` or "回驾驶舱" switches to Cockpit.

It must:

- preserve the Workbench id
- preserve the selected Agent Session
- record Onsite cursor
- create a compact checkpoint
- not replay raw terminal output into product control flow

## Routing Matrix

| Input | View | Lane state | Result |
| --- | --- | --- | --- |
| ordinary text | Cockpit | idle | default `Codex -> Claude -> Codex` |
| ordinary text | Cockpit | running | append/steer or choice card |
| ordinary text | Onsite | selected session | send to selected session |
| ordinary text | Onsite | no selected session | session picker/start card |
| `/codex text` | any | idle | Codex-only in current Workbench |
| `/codex text` | any | running | explicit lane choice card |
| `/claude text` | any | idle | Claude-only in current Workbench |
| `/claude text` | any | running | explicit lane choice card |
| `/terminal` | Cockpit | any | Onsite open decision |
| `/product` | Onsite | any | Cockpit checkpoint |
| `/sessions` | any | any | historical Agent Session library |
| `/new` | any | any | create new Workbench |

## Data Requirements

The implementation may derive the Session Library from existing records first.
It does not need a new durable table unless projection becomes too complex.

Required durable facts:

- Workbench conversation id
- Telegram chat id
- workspace alias
- current view mode
- current execution mode
- Cockpit cursor
- Onsite cursor
- selected agent
- selected Agent Session reference
- Agent Session agent
- Agent Session user-facing title or summary
- Agent Session status: active, completed, failed, orphaned, archived
- resumability: live, resumable, summary-only
- hidden internal backing id for Codex/Claude resume
- linked task/run ids for diagnostics only

Internal ids may exist in storage and logs. They must not appear in ordinary
Telegram user copy.

## Recovery Requirements

Startup recovery must reconstruct enough Workbench state for the user to keep
working:

- active Workbench by chat
- current view mode
- current execution mode
- Cockpit cursor
- Onsite cursor
- selected Agent Session
- active or orphaned task state
- active or orphaned agent run state
- resumable Codex sessions
- resumable Claude sessions
- pending "让 Codex 验收" action after Claude-only completion

If a live process cannot be reattached but a resume id exists, the session is
shown as "可继续". If neither live process nor resume id exists, it is shown as
"可从摘要新开".

## User Copy Requirements

Normal Telegram user copy may use:

```text
工作台
驾驶舱
现场
历史现场
接管现场
回驾驶舱
继续修改
让 Codex 验收
可继续
可回顾
可从摘要新开
```

Normal Telegram user copy must not expose:

```text
terminal.enabled
external_session_id
session id
thread id
runtime_events
agent_run id
task id
blocking task
queue position
```

Developer diagnostics may still include those terms when the user explicitly
uses a diagnostic command.

## Menu Requirements

The mobile menu is a user entrance, not a command catalog.

Primary entries:

```text
新工作台
状态
接管现场
历史现场
让 Codex 验收
设置
帮助
```

Do not present `/task`, `/continue`, `/steer`, raw queue controls, or internal
diagnostic verbs as first-level mobile actions.

## Acceptance Criteria

| # | Criterion | Required evidence |
| --- | --- | --- |
| 1 | `/new` creates the only new user Workbench boundary | targeted Workbench tests |
| 2 | Ordinary messages after `/new` stay in the same Workbench | integration test and live Telegram check |
| 3 | Default ordinary Cockpit text still runs `Codex -> Claude -> Codex` | execution-mode test |
| 4 | `/codex` is Codex-only and does not call Claude | execution-mode test with fake backends |
| 5 | `/claude` is Claude-only and does not auto-trigger Codex | execution-mode test with fake backends |
| 6 | Claude-only completion offers "让 Codex 验收" | Telegram routing/rendering test |
| 7 | `/terminal` never reports a dead missing conversation for an active Workbench | regression test for conversation id callbacks |
| 8 | Start-card callbacks use active Workbench conversation id, not Telegram chat id | callback encoding test |
| 9 | Onsite text routes to selected Agent Session, not Cockpit controller | routing test |
| 10 | `/product` returns to Cockpit without replaying raw terminal | integration test |
| 11 | Running Workbench ordinary text appends/steers or shows explicit choices | execution lane test |
| 12 | Hidden tasks cannot silently pile up and fight for one workspace lock | task lifecycle test |
| 13 | Claude-only task reaches terminal state and releases workspace lock | regression test |
| 14 | Task ids and queue blockers are hidden from normal user copy | rendered copy scan |
| 15 | `/sessions` shows Codex and Claude historical sessions for the Workbench | session library test |
| 16 | Session cards hide raw session/thread/task ids | rendered copy scan |
| 17 | Historical Claude session can be resumed through stored Claude session reference | fake Claude resume test |
| 18 | Historical Codex session can be resumed through stored Codex thread reference | fake Codex resume test |
| 19 | Continuing a historical session creates a new internal task/run linked to that session | ledger/projection test |
| 20 | Continuing a historical session does not create a new Workbench | integration test |
| 21 | `/terminal` with no active session but existing history shows picker | Telegram routing test |
| 22 | `/terminal` with no active or historical session shows start card | Telegram routing test |
| 23 | Restart recovery restores view mode | recovery replay test |
| 24 | Restart recovery restores execution mode | recovery replay test |
| 25 | Restart recovery restores cursor state | recovery replay test |
| 26 | Restart recovery marks orphaned task/agent state safely | recovery test |
| 27 | Restart recovery keeps historical sessions browsable | recovery test |
| 28 | Menu reads like mobile product actions, not technical command list | menu test and copy scan |
| 29 | User copy has no banned internal terms | user copy scan |
| 30 | GitNexus detect_changes reports expected low/medium scoped blast radius | `detect_changes(scope="all")` |
| 31 | Targeted Workbench suite runs completely | test evidence |
| 32 | Existing related suite runs completely, not only happy path | test evidence |
| 33 | Every implementation task has Spec Compliance Reviewer PASS | review docs |
| 34 | Every implementation task has Code Quality Reviewer PASS | review docs |
| 35 | Final Gate uses closed-loop evidence, not "tests passed therefore release" | final review |

## Required Evidence Before Release

Final release cannot be recommended unless all of these are present:

- targeted Workbench suite
- existing related suite
- `git diff --check`
- `git status --short`
- GitNexus `detect_changes(scope="all")`
- rendered user copy internal-term scan
- acceptance criteria comparison table
- task-by-task Spec Compliance Reviewer PASS
- task-by-task Code Quality Reviewer PASS
- live Telegram smoke covering Workbench continuity, Onsite, history picker,
  Claude-only verification action, and historical resume

## Non-Goals

- Do not replace official Claude Remote Control.
- Do not expose raw Claude or Codex ids to normal users.
- Do not make `/task` the primary user workflow.
- Do not make Onsite the default view for ordinary users.
- Do not replay raw terminal history into Cockpit.
- Do not start a new Workbench from `/terminal`, `/product`, `/sessions`,
  `/codex`, or `/claude`.

## Release Note Shape

User-facing release note should be concise:

```text
WLCodex now keeps one continuous remote workbench until you start a new one.
You can switch between the Cockpit and live Onsite view, browse historical
Codex/Claude work sessions, and continue a previous session without seeing
internal task or session ids.
```
