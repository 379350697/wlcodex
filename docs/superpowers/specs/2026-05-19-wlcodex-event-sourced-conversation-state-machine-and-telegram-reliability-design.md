# WLCodex Event-Sourced Conversation State Machine And Telegram Reliability Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

> Superseded in user semantics by the 2026-05-20 Remote Workbench repair.
> Runtime events remain the fact source, but "new task"/queue wording below is
> historical. Current product paths keep Workbench continuity until `/new`;
> natural-language phrases such as "新任务", "另起一个", and "重新开始" are
> ordinary Workbench text unless the user sends `/new`.

## Decision

WLCodex must treat Telegram input as an event-sourced conversation state
machine, not as "one message creates one task".

The event log remains the source of truth. A Telegram chat has one active
conversation at a time. Natural follow-up messages append to that active
conversation unless the user explicitly starts a new conversation or the active
conversation is already terminal.

This design extends:

- `docs/superpowers/specs/2026-05-17-wlcodex-conversation-first-dual-agent-orchestration-design.md`
- `docs/superpowers/specs/2026-05-18-wlcodex-light-event-sourced-agent-platform-design.md`

It does not replace the chief-engineer invariant:

```text
Telegram request
  -> Codex analysis/design
  -> Claude implementation when needed
  -> Codex verification
  -> Telegram reply
```

Claude and Telegram must not bypass Codex verification for code-producing
default workflow runs.

## Problem

The current drift pattern is:

1. A normal follow-up Telegram message is interpreted as a new run.
2. The new run competes with the existing workspace lock.
3. `WorkspaceBusy` or a similar runtime conflict may fail silently or only write
   diagnostics.
4. The user sees no useful response and cannot tell whether their follow-up was
   recorded.
5. Runtime state becomes hard to explain from the event log because the intent
   decision, busy decision, and Telegram delivery outcome are not complete
   append-only facts.

That behavior is wrong for a remote engineering cockpit. Follow-up context is a
first-class part of the active conversation.

## Goals

1. Make natural Telegram follow-ups append to the active conversation by
   default.
2. Ensure one Telegram chat has at most one active non-terminal conversation.
3. Make every routing decision replayable from `runtime_events`.
4. Prevent follow-up messages from reaching Claude directly while Claude is
   implementing or while Codex is verifying.
5. Turn workspace busy conditions into explicit user-visible choices, not
   unhandled exceptions or silence.
6. Keep Telegram long polling, but make it resilient with retry, timeouts, an
   error handler, and a poller watchdog.
7. Send and edit Telegram messages through an outbox so Telegram network
   failures cannot break orchestration or approval semantics.

## Non-Goals

- Do not implement `/recent`.
- Do not move to webhooks as part of this design.
- Do not introduce Kafka, Redis, or an external distributed queue.
- Do not make every Telegram message a separate task.
- Do not dump the full Telegram transcript into Codex or Claude prompts.
- Do not let callback answer/edit failures fail approval decisions.

## Core Concepts

### Chat

A Telegram chat id. Chat state is reconstructed by replaying runtime events.

### Conversation

The user-visible thread of work for one chat. A conversation may have one or
more internal runs, tasks, approvals, and Telegram delivery attempts, but it is
the unit the user is continuing when they send normal text.

### Active Conversation

The latest non-terminal conversation activated for a chat. A chat can have only
one active conversation at a time.

### Terminal Conversation

A conversation whose state is one of:

```text
passed
failed
aborted
done
```

A natural text message after a terminal conversation starts a new conversation.

### Task

An internal compatibility projection. Existing `tasks`, `task_events`,
`agent_runs`, `orchestration_runs`, and `usage_events` remain projection and
operator surfaces. They are not the routing source of truth.

## Conversation State Machine

Allowed conversation states:

```text
new
analysis
waiting_approval
needs_user
implementation
verification
passed
failed
aborted
done
```

State meanings:

| State | Meaning |
| --- | --- |
| `new` | The conversation exists but Codex has not started analysis. |
| `analysis` | Codex is analyzing, planning, or revising a plan. |
| `waiting_approval` | A protocol approval is open. |
| `needs_user` | Codex asked the user for missing information. |
| `implementation` | Claude or another executor is applying changes. |
| `verification` | Codex is validating the result. |
| `passed` | Codex verification passed. |
| `failed` | The run failed with a recorded reason. |
| `aborted` | The user or operator aborted the conversation. |
| `done` | The conversation completed without a pass/fail distinction, for non-code or informational work. |

Terminal states are final for routing. Continuing after terminal creates a new
conversation unless the user explicitly reopens an old conversation through a
separate operator action.

## Message Routing Rules

Every inbound Telegram update first appends `user.message.received`.

The router then appends an explicit routing event. It must never create or
modify a task without a preceding runtime event that explains why.

### New Conversation Triggers

A new conversation is created only when one of these is true:

1. The message is `/new`.
2. The user explicitly asks to start over, using phrases such as
   `新任务`, `另起一个`, or `重新开始`.
3. The chat has no active conversation.
4. The active conversation is terminal: `passed`, `failed`, `aborted`, or
   `done`.

### Append Default

If the chat has a non-terminal active conversation, ordinary text appends to
that conversation.

The router records:

- `conversation.message.routed`
- `user.context.appended`

The payload must include the active conversation id, current conversation state,
the inbound Telegram message id, and the chosen route.

### Inspection Commands

Inspection commands such as `/status`, `/trace`, `/health`, `/diff`, and
similar diagnostics must not create new work conversations. They read from
events and projections, append command-observation events when useful, and
return Telegram output through the delivery outbox.

## Follow-Up Behavior Matrix

| Current state | Follow-up behavior |
| --- | --- |
| `new` | Append context and start or continue Codex analysis. |
| `analysis` | Append `user.context.appended`; Codex re-collects context and revises the plan. |
| `waiting_approval` | Append context, supersede the pending approval, and return to Codex analysis. Old approval buttons become stale. |
| `needs_user` | Append context and resume Codex analysis. |
| `implementation` | Append context, acknowledge receipt, and mark it pending for Codex review at the next phase boundary. Do not send it directly to Claude. |
| `verification` | Append context, acknowledge receipt, and let Codex decide whether to pass, fail, interrupt, or request rework. |
| `passed` / `failed` / `aborted` / `done` | Create a new conversation unless the user used an explicit reopen action. |

The required user acknowledgement during `implementation` and `verification`
is:

```text
已记录，当前阶段结束后由 Codex 判断是否中断/重跑。
```

Equivalent wording is acceptable, but it must clearly say the message was
recorded and will be handled by Codex, not injected into Claude.

## Workspace Busy Behavior

Workspace busy is a normal state, not an exception surface.

When the user sends a message that cannot start a new run because the workspace
is busy, WLCodex must:

1. Append `workspace.busy.detected`.
2. Append `workspace.busy.user_choice.requested`.
3. Reply through the Telegram outbox with the busy task/conversation/run id.
4. Show buttons:
   - append to current task
   - queue new task
   - cancel

Button behavior:

| Button | Event behavior |
| --- | --- |
| Append to current task | Append `user.context.appended` to the active conversation. |
| Queue new task | Append `run.queued` with the requested work and the blocking run id. |
| Cancel | Append `workspace.busy.user_choice.recorded` with `decision = cancel`. |

No workspace busy path may leave the user without a Telegram response.

## Runtime Event Contract Additions

These event names extend the 2026-05-18 runtime event contract.

Conversation routing:

- `conversation.started`
- `conversation.activated`
- `conversation.state.changed`
- `conversation.closed`
- `conversation.intent.classified`
- `conversation.message.routed`
- `user.context.appended`
- `conversation.pending_context.recorded`
- `conversation.pending_context.reviewed`

Workspace busy:

- `workspace.busy.detected`
- `workspace.busy.user_choice.requested`
- `workspace.busy.user_choice.recorded`
- `run.queued`

Approval supersession:

- `approval.superseded`
- `approval.stale_button.ignored`

Telegram delivery:

- `telegram.delivery.enqueued`
- `telegram.delivery.started`
- `telegram.message.sent`
- `telegram.message.edited`
- `telegram.message.failed`
- `telegram.edit.skipped_no_change`
- `telegram.outbox.retry_scheduled`
- `telegram.outbox.gave_up`
- `telegram.callback.answer.failed`
- `telegram.callback.edit.failed`

Telegram polling:

- `telegram.poller.bootstrap.started`
- `telegram.poller.bootstrap.succeeded`
- `telegram.poller.bootstrap.failed`
- `telegram.poller.bootstrap.retrying`
- `telegram.poller.error`
- `telegram.poller.recovered`
- `telegram.poller.watchdog_timeout`

## Event Payload Requirements

`conversation.message.routed` payload:

```json
{
  "chat_id": "telegram-chat-id",
  "telegram_message_id": 123,
  "route": "append_active_conversation",
  "reason": "active_conversation_non_terminal",
  "conversation_state": "analysis",
  "conversation_id": 42
}
```

`user.context.appended` payload:

```json
{
  "chat_id": "telegram-chat-id",
  "telegram_message_id": 124,
  "text_preview": "bounded redacted preview",
  "full_text_ref": "inline-or-redacted-storage-reference",
  "conversation_state_at_append": "implementation",
  "delivery_policy": "codex_phase_boundary_review"
}
```

`workspace.busy.detected` payload:

```json
{
  "chat_id": "telegram-chat-id",
  "blocking_task_id": 42,
  "blocking_conversation_id": 17,
  "blocking_orchestration_run_id": 29,
  "blocking_state": "implementation",
  "requested_route": "new_conversation"
}
```

`telegram.message.failed` payload:

```json
{
  "chat_id": "telegram-chat-id",
  "delivery_id": "stable-delivery-id",
  "operation": "send",
  "attempt": 3,
  "error_type": "TimedOut",
  "retryable": true,
  "next_retry_at": "2026-05-19T00:00:00+08:00"
}
```

Payloads must be bounded and redacted before append.

## Projections

Existing tables remain compatibility projections:

- `tasks`
- `task_events`
- `agent_runs`
- `orchestration_runs`
- `usage_events`
- approvals tables

Conversation state must be reconstructable from `runtime_events` alone. Any
conversation-active read model is a projection and can be rebuilt.

Projection invariants:

- A task may receive multiple user messages through the same conversation.
- Projection writes must be caused by runtime events.
- A stale approval button after `approval.superseded` must not approve the new
  plan.
- `run.completed` cannot project to a successful terminal state without
  `verification.decision.recorded` with `decision = pass`.
- Projection failures must append diagnostics or return errors without mutating
  historical events.

## Telegram Long Polling Reliability

WLCodex should keep long polling in production, but configure it as a resilient
runtime component.

The Python Telegram Bot application supports polling/webhook lifecycle
configuration, error handlers, and timeout parameters:

https://docs.python-telegram-bot.org/en/v20.7/telegram.ext.application.html

Required behavior:

1. Register a global error handler.
2. Use infinite bootstrap retry for polling startup.
3. Set connect, read, write, and pool timeouts explicitly.
4. Append poller lifecycle and error events.
5. Add a poller watchdog that records if polling stops making progress.
6. Recovery should restart polling or fail loudly through operator diagnostics,
   but should not corrupt conversation state.

## Telegram Delivery Outbox

Every Telegram send, edit, and callback answer is a delivery request.

Flow:

```text
append telegram.delivery.enqueued
  -> outbox worker attempts Telegram API call
  -> append telegram.message.sent / telegram.message.edited on success
  -> append telegram.message.failed on failure
  -> retry transient failures with exponential backoff and jitter
  -> append telegram.outbox.gave_up after the retry budget
```

Rules:

- Telegram network failures must not fail Codex, Claude, approval resolution, or
  projection.
- Callback answer failures are recorded as events only.
- Callback message edit failures are recorded as events only.
- Approval button decisions remain protocol facts even if Telegram cannot edit
  the original message.
- Delivery operations require idempotency keys so replay/retry does not spam the
  user.

## Streaming And Edit Throttling

Streaming output must be coalesced before touching Telegram.

Rules:

- Edit at most once every 2 to 5 seconds for routine progress.
- Send milestone edits immediately for phase changes, approval requests, final
  results, and failures.
- Ignore or record `message is not modified` without treating it as failure.
- Raw Claude text deltas are not the final user response in the chief workflow.
  They are runtime data until Codex verification produces a user-facing result.

## Acceptance Criteria

1. Sending two ordinary Telegram messages in the same chat while the first
   conversation is non-terminal creates one active conversation and appends the
   second message as `user.context.appended`.
2. A follow-up during `analysis`, `waiting_approval`, or `needs_user` causes
   Codex to re-evaluate the plan from the appended context.
3. A follow-up during `implementation` or `verification` is recorded, receives a
   Telegram acknowledgement, and is reviewed by Codex at a phase boundary. It is
   not sent directly to Claude.
4. A follow-up after `passed`, `failed`, `aborted`, or `done` starts a new
   conversation unless explicitly reopened.
5. Workspace busy returns a Telegram message with the blocking task/run and
   buttons for append, queue, and cancel.
6. `/status`, `/trace`, and similar diagnostics do not create work tasks.
7. Pending approvals are superseded when new user context arrives in
   `waiting_approval`; old buttons are rejected as stale.
8. Telegram send/edit/callback failures append events and retry when
   appropriate, without failing approval or orchestration.
9. Polling startup and runtime errors are observable through runtime events and
   operator diagnostics.
10. A replay of `runtime_events` explains conversation state, routing decisions,
    workspace busy decisions, approval state, Telegram delivery state, and final
    run outcome.
11. The chief-engineer workflow still requires Codex verification before a
    code-producing default run is marked passed or presented as complete.
12. No `/recent` command or test is introduced.

## Required Human Smoke Scenario

Use a temporary, easy-to-clean code task.

1. Send an incomplete request in Telegram.
2. Send a follow-up clarification before Codex finishes analysis.
3. Approve a command when requested.
4. Send another follow-up while Claude is implementing.
5. Confirm WLCodex acknowledges the follow-up and does not start a second task.
6. Confirm Codex reviews the pending context before final closure.
7. Confirm `/status` and `/trace` explain the same conversation.
8. Clean up the temporary files and confirm cleanup is part of the same
   conversation unless `/new` is used.
