# WLCodex Safe Queue And Explicit Parallelism Design

## Summary

WLCodex should keep the current safety invariant that a normal workspace has
only one active write task by default, but it should stop rejecting new work
with a blunt `WorkspaceBusy` error. When the same workspace is occupied, a new
task is accepted into a safe waiting queue, shown in Telegram with its blocker
and queue position, and started only after the workspace becomes available.

Parallelism becomes an explicit operator choice, not the default. The operator
can keep the new task queued, continue or abort the blocking task, force a
dangerous same-directory run after confirmation, or choose isolated worktree
parallelism once that advanced mode exists.

This follows the useful patterns seen in Telegram-to-agent projects:

- `morse`: small local bridge, queued messages, stale session pruning, local
  state only: https://github.com/ehsuun/morse
- `Telecodex`: topic/session mapping, history import, attachment inbox/outbox,
  SQLite ACLs: https://github.com/Headcrab/telecodex
- `CCGram`: topic-per-agent, terminal/source-of-truth handoff, optional
  worktree topics: https://github.com/alexei-led/ccgram
- `CodexClaw`: `chat + repo` session scope, contention guard, explicit override
  for same-workdir runs: https://github.com/MackDing/CodexClaw

## Goals

- Replace immediate same-workspace rejection with a safe waiting task.
- Preserve the normal one-active-write-task-per-workspace invariant.
- Keep paused tasks in the active write set until the user continues, aborts,
  or explicitly chooses a parallel path.
- Make queued task status clear in Telegram: blocker, queue position, and next
  available actions.
- Start the next waiting task automatically when the workspace lock is released
  by done, failed, aborted, or archived terminal flow.
- Add an explicit dangerous same-directory override with confirmation and audit
  events.
- Treat git worktree isolation as an advanced explicit action, not as the
  default task creation path.
- Prepare Telegram task/session UX for later topic-like history, attachments,
  and artifacts without feeding local status text back into Codex prompts.

## Non-Goals

- Do not remove workspace locks from the default path.
- Do not automatically create a worktree for every task.
- Do not implement multi-user or group-chat collaboration.
- Do not change approval semantics for Codex app-server requests.
- Do not turn WLCodex into a generic Telegram shell bridge.
- Do not auto-merge worktree output without a human decision.

## Terminology

- **Active write task**: a task that may currently own or mutate the configured
  workspace. Current active statuses are `queued`, `running`,
  `waiting_approval`, and `paused`.
- **Waiting-slot task**: a task accepted by Telegram but not yet started in
  Codex because another task owns the workspace. It has no Codex thread and
  does not count as an active write task.
- **Blocker**: the active task that prevents the waiting-slot task from
  starting in the normal safe path.
- **Dangerous same-directory override**: a confirmed operator action that starts
  a second Codex task in the same workspace path despite an existing active
  write task.
- **Isolated parallel task**: a task moved to a dedicated git worktree and run
  there, so it does not share the original workspace write lock.

## State Model

Add a new task status:

```text
waiting_slot
```

`waiting_slot` is intentionally different from `queued`.

- `queued` means WLCodex has accepted a task into the active write path and is
  about to start or has already started a Codex turn.
- `waiting_slot` means WLCodex has accepted a task request, stored the prompt,
  and is waiting for the workspace lock to become available.

The active write set remains:

```text
queued, running, waiting_approval, paused
```

The active write set does not include:

```text
waiting_slot, done, failed, aborted, archived
```

Valid new transitions:

```text
waiting_slot -> queued
waiting_slot -> aborted
waiting_slot -> failed
waiting_slot -> archived
```

The normal queue drain path is:

```text
waiting_slot -> queued -> running -> done|failed|aborted
```

## Data Model

Use the existing `tasks` table for the waiting task. A waiting-slot task has:

- `status = waiting_slot`
- `codex_thread_id = NULL`
- `active_turn_id = NULL`
- `workspace_alias` and `workspace_path` copied from the configured workspace
- `telegram_chat_id` set when created from Telegram
- a `task_reserved` or `task_waiting_slot_created` event containing the full
  original prompt
- a `queue_blocked_by` event containing the blocker task id

Queue position can be derived from:

- same `workspace_alias`
- `status = waiting_slot`
- `created_at ASC, id ASC`

No new queue table is required for the first implementation. A later version
may add explicit queue metadata only if derived ordering becomes insufficient.

## Start Task Flow

When the user sends:

```text
/task wlcodex Fix the parser
```

WLCodex checks the configured workspace:

1. If the workspace is writable and no active write task exists, keep the
   current behavior: create a `queued` task, create a Codex thread, and start
   the turn.
2. If the workspace is writable but an active write task exists, create a
   `waiting_slot` task. Do not create a Codex thread. Do not call
   `turn/start`.
3. Return a Telegram card that shows the new task id, blocker id, and queue
   position.

Example:

```text
Task #12 - Waiting for workspace
Workspace: wlcodex
Blocked by: #8 (paused)
Queue position: 1
Title: Fix the parser
```

## Telegram Decisions

When a task is waiting behind a blocker, the status card should expose these
actions:

- **Keep queued**: no-op; the task waits for automatic queue drain.
- **Show blocker**: render the blocking task card.
- **Continue blocker**: answer the callback with a prompt hint because
  continuing needs user text: `/continue 8 <prompt>`.
- **Abort blocker and start next**: confirmation-required action. It aborts the
  blocker, then starts the first waiting task if the workspace is available.
- **Force same-directory parallel run**: confirmation-required dangerous
  action. It starts the waiting task in the same `workspace_path` even though
  another active write task exists. It records an audit event.
- **Run isolated in worktree**: advanced action. It creates a per-task git
  worktree and starts the task there. This is not the default path.

Button callbacks must be local state transitions only. They must never inject
status text, queue text, or rendered Telegram cards into a Codex prompt.

## Automatic Queue Drain

After a task in the active write set reaches a terminal status, WLCodex checks
for the first `waiting_slot` task for that workspace. If one exists and no
other active write task owns the workspace, WLCodex promotes it:

```text
waiting_slot -> queued
```

Then it creates a Codex thread using the original configured workspace path and
starts the stored prompt.

Queue drain is triggered after:

- backend `turn_completed` marks a task `done`, `failed`, or `aborted`
- user `/abort` marks a task `aborted`
- watchdog marks a task `failed`
- startup recovery leaves a task paused only if it was active before restart;
  recovery does not auto-drain behind a paused blocker

If starting the promoted task fails, that task becomes `failed` and WLCodex may
attempt the next waiting task once. It must not spin forever.

## Dangerous Same-Directory Override

The same-directory override is allowed only through a confirmed callback or an
explicit command added for this purpose. It bypasses the normal workspace
availability check for the chosen waiting task.

It must record:

- `force_parallel_requested`
- `force_parallel_confirmed`
- blocker task id
- requesting Telegram user id if available
- current workspace path

The warning text must be direct:

```text
This will run two Codex tasks in the same working directory. Their edits,
commands, approvals, and local git diff may conflict. Use this only when you
understand the risk.
```

After forced start, later normal tasks should still see the workspace as busy
while any active task remains.

## Worktree Advanced Mode

Worktree mode is not the default. It is an explicit action for a waiting-slot
task.

First complete implementation should support:

- create a branch named `wlcodex/task-<id>-<slug>`
- create a worktree under a configured root such as
  `runtime/worktrees/<workspace-alias>/task-<id>`
- start Codex in the worktree path
- mark the task card as isolated by showing the worktree path
- provide post-completion decisions: show diff, keep, discard

Merge automation is a later follow-up unless the user explicitly asks for it.
The first worktree release should not auto-merge.

To avoid weakening normal workspace locks, implementation should introduce a
lock key concept before worktree start:

- normal task lock key: `workspace:<alias>`
- worktree task lock key: `worktree:<task_id>`

If the implementation does not add a lock key yet, worktree mode must stay
behind a feature flag or remain unimplemented.

## Topic And Session UX Direction

WLCodex should borrow Telecodex-style session ergonomics without requiring
Telegram forum topics in the first pass:

- Every task remains a durable local session with `task_id`, thread id, events,
  logs, touched files, and approvals.
- `/sessions` continues to show known Codex threads.
- Later, a forum topic can map to a task or workspace session, but Telegram
  topic creation is not required for the safe queue feature.
- Attachments and artifacts can later use per-task directories:
  `runtime/tasks/<task-id>/inbox` and `runtime/tasks/<task-id>/out`.

## User Experience Rules

- The user should never lose a task request because a workspace is busy.
- A waiting task should be visible in `/tasks`.
- `/task <id>` should show waiting status, blocker, and queue position.
- `/abort <waiting_task_id>` should remove the waiting task from the queue by
  setting it to `aborted`.
- `/continue <waiting_task_id>` should fail with a clear message: waiting-slot
  tasks have no Codex thread yet.
- `/steer <waiting_task_id>` should fail with a clear message.
- Startup recovery should not convert waiting-slot tasks to paused. They remain
  waiting because they have no active backend turn.

## Error Handling

- `WorkspaceBusy` is no longer logged as an exception for normal `/task`
  requests. It becomes a business condition that creates a waiting-slot task.
- Unknown or stale callbacks should answer the callback query and not raise.
- If a blocker is already terminal when the user clicks a queue decision,
  WLCodex should attempt queue drain instead of failing.
- If a waiting task lacks its original prompt event, it must fail with
  `missing stored prompt` rather than starting Codex with an empty prompt.
- If forced same-directory start is confirmed while the original blocker has
  ended, WLCodex should use the normal safe start path and record
  `force_parallel_no_longer_needed`.

## Testing Requirements

Unit tests must cover:

- `waiting_slot` is not an active write status.
- busy `/task` creates a waiting-slot task without calling `create_thread`.
- queue position is derived correctly.
- terminal active task promotes the first waiting-slot task.
- waiting task can be aborted before it starts.
- paused task still blocks normal start.
- same task continue excludes itself, but a different paused task blocks.
- forced same-directory start records audit events and bypasses only the chosen
  waiting task.
- startup recovery leaves waiting-slot tasks untouched.
- callback handling preserves existing approval callbacks.

Live smoke tests should cover:

- start long task
- start second task in same workspace and verify it waits
- abort or complete first task
- verify second task starts automatically
- verify `/tasks` and status card show the transition

## Rollout

Implement in this order:

1. Safe waiting-slot status and derived queue helpers.
2. Controller start behavior that creates waiting-slot tasks instead of
   returning busy.
3. Queue drain service and integration with terminal task transitions.
4. Telegram queue decision callbacks.
5. Dangerous same-directory override with confirmation.
6. Worktree advanced mode behind explicit action.
7. Topic/session UX improvements for history, attachments, and artifacts.

The first deployable milestone is steps 1 through 4. Steps 5 through 7 can ship
later without changing the safe queue contract.
