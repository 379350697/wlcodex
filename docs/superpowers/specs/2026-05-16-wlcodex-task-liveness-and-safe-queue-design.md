# WLCodex Task Liveness And Safe Queue Design

## Summary

This design tightens WLCodex task lifecycle safety without weakening the current V1 invariant that one workspace may have only one active write task. It fixes the immediate paused-task self-lock bug, adds a real escape hatch for paused tasks, introduces task liveness watchdogs, detects dead app-server sessions conservatively, and sends recovery notifications after daemon restart.

The design intentionally does not remove the application-level write lock. Open-source Telegram-to-agent bridges such as morse, telecodex, ccgram, OpenClaw, and CodexClaw show useful patterns around session queues, per-session process isolation, topic/session routing, stale-session cleanup, and access control. WLCodex should adopt those patterns in the order that matches its safety model: liveness and recovery first, safe per-workspace queue second, multi app-server per workspace third, and same-repo parallelism only after git worktree isolation exists.

## Goals

- Allow a paused task to continue itself without being blocked by its own workspace lock.
- Allow a paused task to be aborted, so the user always has a Telegram-side escape hatch.
- Keep the existing single active write task invariant for each configured workspace.
- Add bounded task liveness checks so running or waiting tasks cannot block a workspace forever.
- Detect app-server death or lost backend health and release affected tasks with explicit ledger evidence.
- Notify Telegram after startup recovery pauses tasks.
- Prepare the data model for a later safe queue that accepts work while a workspace is busy but starts only the queue head.
- Keep all monitoring, recovery, and status text out of Codex prompt context.

## Non-Goals

- No immediate removal of application-level workspace write locks.
- No same-repo concurrent writes until worktree-backed task isolation exists.
- No public network listener.
- No multi-user mode.
- No multi-agent orchestration inside this change.
- No tmux bridge replacement for the app-server backend.
- No automatic merge or branch management.

## Current Problems

`PAUSED` is currently treated as an active write status, which is correct for safety, but `continue_task()` checks workspace availability without excluding the task being continued. That means a paused task can block itself. `abort_task()` also rejects `PAUSED`, leaving no clean Telegram escape hatch for a recovery-paused task.

The event bridge expires stale approvals, but it does not expire stale tasks. A `RUNNING`, `QUEUED`, or `WAITING_APPROVAL` task can remain in the active write set indefinitely if app-server events stop arriving or if the app-server process dies outside the normal event stream. Startup recovery already pauses active tasks, but the user currently sees only local logs unless they manually inspect Telegram.

## Design Decisions

### Keep The Lock As The Safety Boundary

WLCodex keeps `ACTIVE_WRITE_STATUSES = {queued, running, waiting_approval, paused}`. This remains the correct boundary until write tasks run in separate git worktrees. A separate app-server process isolates protocol state and failure domains, but it does not prevent two tasks from editing the same files in the same workspace.

The lock helper gains an optional `exclude_task_id`. Callers that are resuming or aborting the current task can ignore that same task. New tasks still see the paused task as the active lock holder.

### Paused Continue And Abort

`continue_task(task_id, prompt)` will:

1. Reject archived, queued, running, and waiting-approval tasks as it does today.
2. Accept paused, done, failed, and aborted tasks when they have a Codex thread id.
3. Check workspace writability.
4. Check workspace availability while excluding `task_id`.
5. Transition the task to `queued`, clear the active turn, and record `user_continue`.

`abort_task(task_id)` will accept `queued`, `running`, `waiting_approval`, and `paused`. It records `user_aborted`, clears the active turn if needed, and releases the workspace lock by entering `aborted`.

### Task Liveness Watchdog

Add a `[task]` config section:

```toml
[task]
max_running_seconds = 7200
max_queued_seconds = 1800
max_waiting_approval_seconds = 3600
watchdog_interval_seconds = 60
backend_dead_grace_seconds = 120
```

The defaults are conservative. They protect unattended operation without surprising short-running tasks. The watchdog checks ledger state on a timer independent of backend events. It marks stale tasks as failed with explicit event records:

- `task_timeout` for stale `queued`, `running`, or `waiting_approval`.
- `backend_dead` when a task has a Codex thread id, the backend reports unhealthy for longer than the configured grace period, and the task is still active.

Timeouts use ledger timestamps, not Telegram message timestamps. The implementation should prefer `updated_at` for the first iteration and add `last_event_at` only if the current tests reveal ambiguous behavior. The event payload records the status, age, and threshold used.

### Backend Death Detection

The watchdog reads `backend.health()` when available. It treats a backend as dead only when `is_healthy` exists and returns false, or when a `summary()` string exists and has been unhealthy for the grace window. Startup lazy-connect behavior should not immediately fail tasks. A single unhealthy sample starts a grace timer; only sustained unhealthy state produces `backend_dead`.

If the backend later becomes healthy before the grace expires, the grace timer resets.

### Recovery Telegram Notification

Startup already calls `mark_active_tasks_recovery_paused()`. After Telegram handlers are available, WLCodex sends a concise notification for each paused task that still has `telegram_chat_id`:

```text
任务 #42 已因 WLCodex 重启暂停。
可用 /continue 42 <prompt> 继续，或 /abort 42 释放工作区。
```

This notification is local-state only. It is never sent to Codex. It should also edit the task status card if `telegram_status_message_id` exists.

### Safe Queue Direction

The later queue change should not allow concurrent writes in the same workspace. It should only replace immediate rejection with acceptance into a per-workspace FIFO:

- Busy workspace: create a task in `queued` and record `queue_blocked_by_task_id`.
- Queue head starts only after the previous active task reaches a terminal status.
- `/abort <queued_id>` removes the queued task.
- `/tasks` shows queue position.

This design does not implement full queue draining in the immediate liveness change. It reserves the model direction so the implementation plan can avoid introducing incompatible state.

## Data Model

Immediate fields can be derived from existing `tasks.updated_at`, `status`, `codex_thread_id`, and event rows. The plan may add helper methods on `Ledger` but should avoid schema churn unless tests need precise `last_event_at`.

Recommended helper methods:

- `list_active_tasks(limit=100) -> list[Task]`
- `mark_task_timeout(task_id, status, age_seconds, threshold_seconds) -> Task`
- `mark_backend_dead(task_id, summary) -> Task`
- `tasks_with_telegram_chats(task_ids) -> list[Task]`

These helpers keep watchdog and recovery notification code out of raw SQL.

## User Experience

- `/continue <paused_id> <prompt>` succeeds when no other task owns the workspace.
- `/abort <paused_id>` succeeds and releases the workspace.
- `/tasks` continues to show paused tasks as active enough to block new writes.
- When a stale task fails, the status card shows the failure reason and the workspace becomes available.
- On daemon restart, Telegram receives a recovery-paused notification for affected tasks.

## Safety Rules

- Status cards, logs, watchdog messages, recovery notices, and local events are never injected into Codex prompts.
- A task timeout never sends a prompt or hidden instruction to Codex.
- Backend death cleanup is conservative and evidence-recorded.
- New write tasks still require `allow_write = true`.
- The app-server remains loopback-only.

## Testing Strategy

- Unit tests for lock self-exclusion and paused abort.
- Unit tests for config parsing and default task liveness settings.
- Ledger tests for active task listing and timeout/dead-backend event records.
- Watchdog tests using fake ledger/backend clocks.
- Event bridge tests proving watchdog loops run without backend events.
- Main/recovery tests proving paused task notifications are sent after handlers exist.
- Regression tests proving a second new task is still rejected while a different paused task owns the same workspace.

## Acceptance Criteria

- Paused task can be continued without `WorkspaceBusy` caused by itself.
- Paused task can be aborted.
- Another new write task is still blocked by a different paused task in the same workspace.
- A stale running task is marked failed and records `task_timeout`.
- A sustained unhealthy backend marks active tasks failed with `backend_dead`.
- Startup recovery pauses active tasks and sends Telegram notifications for tasks with chat ids.
- Full local test suite passes with the new tests.
