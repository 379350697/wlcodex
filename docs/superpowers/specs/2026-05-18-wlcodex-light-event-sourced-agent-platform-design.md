# WLCodex Lightweight Event-Sourced Agent Platform Design

## Decision

Build WLCodex into a personal-use, lightweight event-sourced agent platform.

This is not a distributed platform and not a rewrite into Kafka, workers, or a
cloud control plane. The final shape is a local SQLite-backed append-only event
log that becomes the source of truth for Codex, Claude, orchestration,
Telegram rendering, approvals, usage, recovery, and diagnostics.

Existing tables such as `tasks`, `task_events`, `agent_runs`,
`orchestration_runs`, `usage_events`, approvals, and conversation sessions stay
available as projections and compatibility surfaces. New runtime behavior should
be explained by replaying the event log first, then by looking at projections.

## Goals

1. Make every meaningful runtime fact observable while it happens.
   Claude must no longer be a subprocess black box that only returns final text.

2. Make state reconstructable.
   A service restart should be able to rebuild current run state from events and
   explain whether an agent is running, waiting, failed, timed out, or orphaned.

3. Preserve the chief-engineer loop.
   The canonical workflow remains Codex design, Claude implementation, Codex
   verification, Telegram response.

4. Stop raw agent chatter from becoming the user experience.
   Telegram should render deterministic human progress and concise final
   summaries from events. Raw model text is data, not the default UI.

5. Keep the personal version small.
   One process, SQLite, asyncio, existing Telegram bot, existing Codex app-server
   backend, existing Claude CLI backend.

## Non-Goals

- Multi-user SaaS tenancy.
- Distributed queues or external brokers.
- Replacing SQLite.
- A generic plugin marketplace.
- Letting Telegram users bypass Codex verification and drive Claude directly as
  the main path.
- Full terminal read-screen control as the primary architecture.
- Event deletion. Redaction and snapshotting are allowed; mutation of historical
  facts is not.

## Current Baseline

WLCodex already has several pieces of the final platform:

- `agent_runs` tracks Codex and Claude runs, but Claude orchestration runs are
  currently created too late in parts of the workflow.
- `task_events` records Codex app-server events such as message deltas, item
  lifecycle, approvals, command output, and token usage.
- `usage_events` records richer token/cost data.
- `EventBridge` and the interaction layer already separate runtime events from
  Telegram rendering for some Codex paths.
- `ClaudeBackend.send_streaming()` uses Claude `stream-json`, but currently
  reduces most information to `text`, `usage`, or `error`.

The missing piece is a single runtime event contract that both Codex and Claude
adapters emit and every downstream component consumes.

## Core Model

### Runtime Event

Every event is immutable and append-only.

Required envelope fields:

| Field | Meaning |
| --- | --- |
| `id` | Monotonic SQLite event id |
| `schema_version` | Runtime event schema version, initially `1` |
| `event_type` | Dot-separated event name |
| `aggregate_type` | `conversation`, `orchestration_run`, `agent_run`, `approval`, `telegram_message`, or `system` |
| `aggregate_id` | Stable id for the aggregate |
| `conversation_id` | Existing conversation id when available |
| `orchestration_run_id` | Existing orchestration run id when available |
| `agent_run_id` | Existing agent run id when available |
| `task_id` | Existing task id when available |
| `correlation_id` | Groups all events from one user request |
| `causation_id` | Prior event that caused this event, when known |
| `source` | `telegram`, `controller`, `orchestrator`, `codex`, `claude`, `projector`, `watchdog`, or `system` |
| `actor` | Human-readable actor such as `user`, `codex`, `claude`, `telegram_bot` |
| `visibility` | `internal`, `operator`, or `user` |
| `payload_json` | Event payload, JSON object |
| `occurred_at` | Event creation time in ISO format |

Recommended storage:

```text
runtime_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  schema_version INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  conversation_id INTEGER,
  orchestration_run_id INTEGER,
  agent_run_id INTEGER,
  task_id INTEGER,
  correlation_id TEXT NOT NULL,
  causation_id INTEGER,
  source TEXT NOT NULL,
  actor TEXT NOT NULL,
  visibility TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL
)
```

Indexes:

- `(correlation_id, id)`
- `(aggregate_type, aggregate_id, id)`
- `(conversation_id, id)`
- `(orchestration_run_id, id)`
- `(agent_run_id, id)`
- `(event_type, id)`

### Event Families

User and Telegram:

- `user.message.received`
- `telegram.message.sent`
- `telegram.message.edited`
- `telegram.message.failed`
- `telegram.callback.received`

Run lifecycle:

- `run.requested`
- `run.started`
- `run.phase.changed`
- `run.completed`
- `run.failed`
- `run.cancel.requested`
- `run.cancelled`

Agent lifecycle:

- `agent.run.queued`
- `agent.run.started`
- `agent.run.activity`
- `agent.run.heartbeat`
- `agent.run.waiting_for_approval`
- `agent.run.completed`
- `agent.run.failed`
- `agent.run.timed_out`
- `agent.run.orphaned`

Model and message output:

- `model.text.delta`
- `model.message.completed`
- `model.reasoning.delta`
- `model.usage.updated`
- `model.api.retry`

Tool, command, file, and approval:

- `tool.call.started`
- `tool.call.progress`
- `tool.call.completed`
- `tool.call.failed`
- `command.started`
- `command.output.delta`
- `command.completed`
- `command.failed`
- `file.read`
- `file.changed`
- `diff.updated`
- `approval.requested`
- `approval.resolved`
- `approval.expired`

Verification:

- `verification.started`
- `verification.decision.recorded`
- `verification.completed`
- `verification.retry.requested`

Timeout and recovery:

- `watchdog.idle_timeout`
- `watchdog.hard_timeout`
- `system.started`
- `system.recovery.started`
- `system.recovery.completed`
- `projection.rebuilt`

## State Machines

### Orchestration Run

Allowed states:

```text
queued -> running_analysis -> running_implementation -> running_verification
       -> retrying_implementation -> completed
       -> failed
       -> cancelled
```

Rules:

- A run cannot enter `completed` without `verification.decision.recorded` with
  `decision = pass`.
- A run cannot enter `failed` without a failure event carrying a human-readable
  reason and the last active agent.
- A retry must point to the verification decision that caused it.
- Terminal states are final unless an operator starts a new run.

### Agent Run

Allowed states:

```text
queued -> running -> waiting_for_approval -> running
       -> completed
       -> failed
       -> timed_out
       -> cancelled
       -> orphaned
```

Rules:

- `agent.run.started` must be emitted before launching a subprocess, app-server
  turn, background session, or equivalent external action.
- Any raw provider event, hook event, stdout line, transcript update, or tool
  event may emit `agent.run.activity`.
- Idle timeout is based on absence of activity events.
- Hard timeout is based on wall-clock duration and is never refreshed.
- A completed agent run must include a completion summary or a pointer to final
  message events.

### Approval

Allowed states:

```text
requested -> resolved
requested -> expired
requested -> cancelled
```

Rules:

- Approval events are security-critical and must retain the tool name, command
  or file summary, decision, resolver, and Telegram callback id when available.
- Auto approval still emits `approval.requested` and `approval.resolved`.

## Projections

Projection tables are derived from events. They exist for speed and backwards
compatibility.

Required projections:

| Projection | Purpose |
| --- | --- |
| `tasks` | Existing task list and Telegram status compatibility |
| `agent_runs` | Fast active agent/run status |
| `orchestration_runs` | Fast chief-engineer run summary |
| `usage_events` | Token/cost reporting |
| `task_events` | Backwards-compatible event inspection |
| Runtime status view | Latest phase, last activity, timeout clocks, last visible user message |
| Runtime trace view | Sanitized chronological timeline for `/trace` or diagnostics |

Projector rules:

- Projectors may update existing mutable tables.
- Projectors must never mutate `runtime_events`.
- Projection failures emit `projection.failed` and do not block event append.
- Rebuilding projections from the event log must be supported for personal
  diagnostics.

## Adapter Contracts

### Codex Adapter

Codex app-server events map into runtime events:

| Codex source | Runtime event |
| --- | --- |
| `thread/started` | `agent.run.started` or `agent.run.activity` |
| `turn/started` | `run.phase.changed` or `agent.run.activity` |
| `item/started` command | `command.started` |
| `item/completed` command | `command.completed` |
| `item/commandExecution/outputDelta` | `command.output.delta` |
| `item/fileChange/outputDelta` | `file.changed` |
| `item/agentMessage/delta` | `model.text.delta` |
| `thread/tokenUsage/updated` | `model.usage.updated` |
| approval request | `approval.requested` |
| approval resolution | `approval.resolved` |

### Claude Adapter

Claude CLI stream and hook events map into runtime events:

| Claude source | Runtime event |
| --- | --- |
| process launch | `agent.run.started` |
| any JSON line | `agent.run.activity` |
| `stream_event` text delta | `model.text.delta` |
| assistant text completion | `model.message.completed` |
| assistant `tool_use` block | `tool.call.started` |
| result usage | `model.usage.updated` |
| `system/api_retry` | `model.api.retry` |
| hook started/progress/response | `tool.call.progress`, `approval.requested`, or `agent.run.activity` |
| process exit success | `agent.run.completed` |
| non-zero exit | `agent.run.failed` |
| idle timeout | `watchdog.idle_timeout` and `agent.run.timed_out` |
| hard timeout | `watchdog.hard_timeout` and `agent.run.timed_out` |

Claude must be invoked with streaming and hook visibility enabled when
supported:

```text
claude -p <prompt> --output-format stream-json --verbose
  --include-partial-messages --include-hook-events
```

If a local Claude version does not support hook events, the adapter degrades to
stream-json and emits a `runtime.capability.missing` event.

## Telegram Rendering

Telegram consumes user-visible and operator-visible projections, not raw events
directly.

Default personal profile:

- Sends no mechanical ACK for normal text.
- Starts typing on `run.started`.
- Opens or edits one progress message per active run.
- Uses deterministic templates for progress:
  - `Codex 正在拆解需求`
  - `Claude 开始实施`
  - `Claude 正在读取文件`
  - `Claude 正在修改文件`
  - `Claude 正在运行命令`
  - `Codex 正在验收`
  - `还在执行，最近活动：...`
- Shows raw model text only for final assistant response or explicit verbose
  diagnostics.
- Provides `/status` and `/trace` for operator detail.

Verbosity:

| Level | Behavior |
| --- | --- |
| `0` | Final response plus critical approval/failure only |
| `1` | Human progress milestones and long-running heartbeats |
| `2` | Tool names, command summaries, file summaries, retries, token updates |

Telegram delivery itself emits events so failed edits, rate limits, duplicate
edits, and fallback sends are auditable.

## Timeout Policy

Each agent run has three clocks:

| Clock | Refreshes? | Purpose |
| --- | --- | --- |
| Hard timeout | No | Absolute budget for the agent run |
| Idle timeout | Yes, on `agent.run.activity` | Detects real stalls |
| User heartbeat interval | Yes | Controls Telegram "still working" updates |

Timeout decisions must record:

- agent name
- run id
- last event id
- last event type
- elapsed hard time
- elapsed idle time
- subprocess or external session status when known

## Recovery

Startup recovery appends events rather than silently mutating state:

```text
system.started
system.recovery.started
agent.run.orphaned / run.failed / projection.rebuilt
system.recovery.completed
```

Recovery behavior:

- Rebuild runtime status projection from `runtime_events`.
- Find non-terminal runs.
- If the external process/session is gone, emit `agent.run.orphaned`.
- If a run cannot safely resume, emit `run.failed` with `reason =
  service_restart_orphaned_run`.
- Existing recovery that marks running `agent_runs` as failed becomes a
  projector effect of those recovery events.

## Diagnostics

Required operator views:

- `/status`: current run, phase, active agent, last activity, idle clock, hard
  clock, token summary, last visible event.
- `/trace`: last N sanitized runtime events for the active conversation.
- `/runs`: recent orchestration and direct agent runs.
- `/events <run>`: detailed chronological event list.

The same data should be queryable from SQLite without Telegram:

```text
SELECT id, event_type, source, actor, occurred_at, payload_json
FROM runtime_events
WHERE correlation_id = ?
ORDER BY id;
```

## Security And Privacy

Personal use still needs clear boundaries:

- Telegram user allowlist remains mandatory.
- Raw command output is stored with length caps.
- Payloads containing tokens, auth headers, or secrets are redacted before
  append.
- Approval decisions include the resolver and exact decision.
- Runtime events are never appended to model prompts unless a specific adapter
  explicitly includes sanitized summaries.
- `operator` visibility events may appear in `/trace`; `internal` events stay
  out of normal Telegram chat.

## Acceptance Criteria

The final platform is done when these are true:

1. A live chief-engineer run creates a running Claude `agent_run` before Claude
   starts.
2. During Claude execution, SQLite shows `agent.run.activity` and tool/model
   events without waiting for Claude to finish.
3. Idle timeout is refreshed by real Claude activity and does not fire merely
   because no user-facing text was produced.
4. Hard timeout still stops truly over-budget runs.
5. Codex events and Claude events share one runtime event envelope.
6. Telegram progress is deterministic and concise by default.
7. `/status` can explain what the active run is doing.
8. `/trace` can show the last meaningful events for diagnosis.
9. Service restart marks orphaned in-flight runs through appended recovery
   events.
10. Existing tests for Codex approvals, streaming, orchestration, and usage still
    pass after projections are wired.

## Final Architecture

```text
Telegram input
  -> CommandController / OrchestrationRunner
  -> RuntimeEventStore.append(user/run events)
  -> CodexRuntimeSource / ClaudeRuntimeSource
  -> RuntimeEventStore.append(agent/tool/model/usage events)
  -> RuntimeProjector updates compatibility tables and runtime status views
  -> InteractionRenderer renders Telegram messages from projections
  -> Telegram delivery events append back into RuntimeEventStore
```

The event log is the ground truth. Everything else is a view.
