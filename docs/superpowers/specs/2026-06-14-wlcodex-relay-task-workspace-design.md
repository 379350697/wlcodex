# WLCodex Relay Task Workspace Design

## Status

Drafted on 2026-06-14 after the user corrected the product shape: relay mode
must start from a task/workspace list, not from a direct five-role session page.

This is a product and architecture design spec. It does not authorize product
code changes by itself.

## Product Goal

Add a new workflow-class mode called "流式接力模式" for large tasks that need
multiple native agents to work in sequence while the user keeps one task-level
control surface.

The key distinction is:

```text
Relay Task != native session
```

A Relay Task is the durable work item the user manages. Each task may create
many child native sessions across the fixed relay roles:

```text
总工程师 -> 架构工程师 -> 开发工程师 -> 测试工程师 -> 审计工程师
```

The UI must therefore expose a task list/workspace before opening any concrete
five-role board. This mirrors the useful part of Codex-style history: the user
can manage big tasks first, then drill into the many agent sessions inside one
task.

## Navigation Model

The native home remains the provider and capability entry:

```text
/native
```

It shows:

```text
Codex
Claude
Antigravity
议会审核
工作流
```

The new workflow directory is:

```text
/native/workflows
```

It shows workflow-class choices:

```text
流式接力模式
议会审核
Dev Flow
```

The relay entry is a task list/workspace:

```text
/native/workflows/relay
```

A concrete task opens at:

```text
/native/workflows/relay/tasks/{task_id}
```

The existing council route remains valid:

```text
/council
```

The workflow directory also links to `/council` so the user can choose council
review from the workflow category without losing the existing direct route.

## Product Pages

### `/native`

Purpose: high-level native capability entry.

Required behavior:

- Keep provider entries for Codex, Claude, and Antigravity.
- Keep or link existing council access.
- Add a `工作流` entry.
- Preserve token query and cookie behavior when navigating to deeper pages.

Suggested card copy:

```text
工作流
多角色接力、议会、Dev Flow 类流程
```

### `/native/workflows`

Purpose: second-level workflow directory.

Required cards:

- `流式接力模式`: 总工程师调度，多角色实时协作。
- `议会审核`: open existing `/council`.
- `Dev Flow`: explain that this is a later workflow-class entry if the product
  has no stable UI implementation yet.

This page answers:

```text
我要进入哪类协作方式？
```

### `/native/workflows/relay`

Purpose: relay task workspace and task management list.

This page is not a native session list. It manages task-level work items.

Required components:

- Workspace/project selector using the same spirit as the existing native
  history/workspace surface.
- New task composer for publishing a large task.
- Task groups:
  - `running`
  - `waiting_user`
  - `blocked`
  - `completed`
  - `interrupted`
- Task cards showing:
  - task title
  - workspace/cwd
  - current phase
  - director decision summary
  - five role status chips
  - latest handoff summary
  - last activity time
  - open task action

This page answers:

```text
我有哪些大任务？哪个任务在等谁？哪个任务需要我介入？
```

### `/native/workflows/relay/tasks/{task_id}`

Purpose: concrete five-role relay board for one large task.

Layout:

```text
Task header and RelayBoard

总工程师

架构工程师        开发工程师

测试工程师        审计工程师

User input composer
```

Each role panel shows:

- role name
- role state
- provider/model
- native session id
- stream output
- latest handoff summary
- open questions
- link to the child native session when available

The user input composer sends new user input to the total engineer/director by
default. Role-specific direct replies can be added later, but v1 keeps the user
control path simple.

## Relay Roles

v1 has five fixed roles:

```text
director
architect
implementer
tester
auditor
```

Chinese display names:

```text
总工程师
架构工程师
开发工程师
测试工程师
审计工程师
```

Responsibilities:

- `director`: receive user task, maintain RelayBoard, decide dispatch, verify
  handoff envelopes, summarize completion, ask the user when blocked.
- `architect`: produce architecture plan, risk map, implementation boundaries,
  and handoff packet for implementation.
- `implementer`: perform scoped implementation through the selected native
  provider and return implementation evidence.
- `tester`: run or design verification, report pass/fail evidence, and return
  unresolved risks.
- `auditor`: review correctness, security, regression risk, and final readiness.

Default flow:

```text
user -> director -> architect -> implementer -> tester -> auditor -> director
```

The director may dispatch the auditor earlier for high-risk design or security
questions.

## Data Model

Reuse the existing team foundation instead of creating a large independent
database model.

### `team_runs`

Use one `team_runs` row as the Relay Task main record.

Required semantics:

```text
team_runs.id == public task_id
team_runs.route == relay
team_runs.status == relay task status
```

Relay Task statuses:

```text
queued | running | waiting_user | blocked | failed | completed | interrupted
```

### `team_agent_jobs`

Use one job per fixed role per task.

Role job statuses:

```text
idle | queued | streaming | waiting | passed | failed | blocked | interrupted
```

Each role job may link to a child native agent session:

```text
native_session_id
agent_run_id
provider
provider_engine
model
```

### `team_context_packets`

Store compact context packets produced for each role dispatch.

A packet is scoped to one role and one dispatch. It must not contain the full
conversation transcript. It contains the current task state, relevant facts,
latest user instruction, prior handoff summaries, and role-specific constraints.

### `team_artifacts`

Store relay boards, routing decisions, role outputs, test/audit reports, handoff
packets, and final summaries.

Standard artifact types:

```text
relay_board
routing_decision
architecture_plan
implementation_report
test_report
audit_report
handoff_packet
final_summary
```

Artifact payloads add relay metadata:

```text
relay_role
handoff_to
dispatch_verified
native_session_id
fallback_reason
provider
provider_engine
```

## Context Control

The relay system does not share full transcripts between roles.

The director maintains a task-level RelayBoard:

```text
current_goal
phase
confirmed_facts
open_questions
risks
current_dispatch
next_step
latest_user_input
```

Before a role starts or resumes, the director/service creates a
RoleContextPacket:

```text
task_id
role
workspace
current_goal
phase
latest_user_input
confirmed_facts
role_relevant_artifacts
handoff_summaries
constraints
expected_output_envelope
```

Rules:

- The latest user input has priority over old summaries.
- Old handoff information is advisory background.
- No role inherits old permissions from another role.
- No role inherits old execution state from another role.
- A failed or unparseable role envelope does not advance the state machine.

## Handoff Protocol

Each role output must be normalized into a `role_envelope`:

```json
{
  "status": "passed",
  "reason": "implementation completed with test evidence",
  "role": "implementer",
  "artifact_type": "implementation_report",
  "handoff_to": "tester",
  "summary": "Implemented the requested route and UI links.",
  "evidence_refs": ["tests/test_relay_routes.py::test_create_task"],
  "open_questions": [],
  "next_action": "run relay API and UI tests"
}
```

When one role hands work to another, store a `handoff_packet` artifact:

```json
{
  "from_role": "implementer",
  "to_role": "tester",
  "summary": "Implementation is ready for verification.",
  "confirmed_facts": ["Relay task route creates a director job first."],
  "open_questions": [],
  "evidence_refs": ["wlcodex/relay/service.py"],
  "next_action": "Verify SSE lane events and interruption behavior."
}
```

The next role receives the latest relevant `handoff_packet` through its
RoleContextPacket, not by reading the previous role's entire transcript.

## Provider Dispatch

Relay dispatch uses `NativeAgentRegistry`.

The relay business logic must not hardcode Codex, Claude, or Antigravity.

v1 default:

```text
All roles use the currently selected provider unless the task was created with
a provider override.
```

Each dispatch records:

```text
provider
provider_engine
native_session_id
dispatch_verified
fallback_reason
```

If a provider cannot support a needed operation, the task continues in
compatibility mode when possible. The UI must show the fallback marker and the
reason instead of hiding the degradation.

## Public API

Task workspace:

```text
GET  /api/relay/tasks
POST /api/relay/tasks
```

Task detail:

```text
GET /api/relay/tasks/{task_id}
GET /api/relay/tasks/{task_id}/events
GET /api/relay/tasks/{task_id}/sessions
```

Task interaction:

```text
POST /api/relay/tasks/{task_id}/message
POST /api/relay/tasks/{task_id}/interrupt
```

API compatibility alias:

```text
POST /api/relay/runs
GET  /api/relay/runs/{run_id}
GET  /api/relay/runs/{run_id}/events
POST /api/relay/runs/{run_id}/message
POST /api/relay/runs/{run_id}/interrupt
```

The alias keeps the earlier run-oriented naming usable internally, but product
copy and navigation should call these records tasks.

## Event Stream

`GET /api/relay/tasks/{task_id}/events` uses SSE and emits role-lane events.

Core event types:

```text
task.created
task.updated
role.queued
role.streaming
role.output_delta
role.envelope
role.status
artifact.created
handoff.created
dispatch.verified
dispatch.fallback
task.completed
task.interrupted
```

Each event includes:

```text
task_id
role
job_id
event_type
sequence
created_at
payload
```

The UI routes each output delta to the matching role panel.

## Test Plan

Navigation tests:

- `/native` shows `工作流`.
- `/native/workflows` shows `流式接力模式` and `议会审核`.
- `/native/workflows/relay` opens as a task list/workspace.
- `/native/workflows/relay/tasks/{task_id}` opens the five-role board.
- token query and cookie behavior survive all deeper navigation.

API tests:

- Creating a relay task creates a `team_runs` relay record.
- Creating a relay task creates five role jobs.
- The director job enters `queued` or `streaming` before other roles.
- User follow-up messages are routed to the director by default.
- SSE events include role lane identity and ordered sequence numbers.
- Interrupting one role does not interrupt the whole task unless requested.

Context tests:

- RoleContextPacket does not contain the full transcript.
- The latest user input overrides stale handoff text.
- An invalid `role_envelope` does not advance the state machine.
- A handoff packet is written to `team_artifacts` and referenced by the next
  role context packet.

Provider tests:

- Relay dispatch goes through `NativeAgentRegistry`.
- Provider/model/session metadata is recorded per role job.
- Capability fallback writes `fallback_reason` and is visible in task detail.

UI acceptance:

- Task list is visually distinct from session history.
- Task cards summarize role state without exposing every child session as a
  top-level row.
- The five role panels do not overlap on desktop or mobile.
- Idle roles remain idle until dispatched.
- Streaming roles update their own panel in real time.
- Every role panel shows latest handoff summary and session status.

## Assumptions

- v1 fixes the role set to five roles.
- v1 task list is required before the task detail board.
- v1 does not require all roles to start simultaneously.
- v1 does not auto-commit or auto-deploy.
- v1 reuses existing `team_*` tables.
- v1 ships before role-provider assignment UI; all roles default to the current
  provider unless a backend override already exists.
