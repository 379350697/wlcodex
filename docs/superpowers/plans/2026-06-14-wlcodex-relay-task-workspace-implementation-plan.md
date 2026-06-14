# WLCodex Relay Task Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 流式接力模式 v1 as a task/workspace-driven workflow: `/native/workflows/relay` manages large Relay Tasks, and `/native/workflows/relay/tasks/{task_id}` opens one task's five-role live board.

**Architecture:** Add a relay workflow layer above the existing team tables and `NativeAgentRegistry`. `team_runs` stores the task, `team_agent_jobs` stores the fixed role jobs, `team_context_packets` stores compact role dispatch context, and `team_artifacts` stores RelayBoard, role outputs, and handoff packets. The UI adds a workflow directory, relay task list, and relay task board without hardcoding provider implementations.

**Tech Stack:** Python dataclasses, existing SQLite/Ledger persistence, async route handlers in `wlcodex/live_stream/server.py`, provider dispatch through `NativeAgentRegistry`, vanilla HTML/CSS/JS in the native/live UI templates, SSE, pytest, GitNexus CLI.

---

## Spec Source

Design spec:

```text
docs/superpowers/specs/2026-06-14-wlcodex-relay-task-workspace-design.md
```

## Pre-Implementation Rules

Before editing an existing function, class, or method, run GitNexus impact for
that symbol when GitNexus is available.

Likely symbols to inspect before modification:

```bash
npx gitnexus impact WorkerLiveStreamServer --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _handle_client --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _handle_native_agent_route --repo wlcodex --direction upstream --include-tests
npx gitnexus impact _create_live_stream_components --repo wlcodex --direction upstream --include-tests
npx gitnexus impact Ledger.migrate --repo wlcodex --direction upstream --include-tests
```

If impact returns HIGH or CRITICAL, report the blast radius before editing that
symbol.

Before every commit, run:

```bash
npx gitnexus detect-changes -r wlcodex
```

## File Structure

Create:

- `wlcodex/relay/__init__.py`  
  Public exports for relay models, store, service, context builder, and envelope parser.

- `wlcodex/relay/models.py`  
  Relay constants, dataclasses, statuses, role definitions, API response shapes.

- `wlcodex/relay/context.py`  
  RelayBoard and RoleContextPacket builders with transcript exclusion rules.

- `wlcodex/relay/envelopes.py`  
  Role envelope parsing, validation, and state transition guard helpers.

- `wlcodex/relay/store.py`  
  Persistence adapter over existing `team_runs`, `team_agent_jobs`, `team_context_packets`, and `team_artifacts`.

- `wlcodex/relay/service.py`  
  Task creation, director-first startup, role dispatch, handoff persistence, user message routing, interruption.

- `wlcodex/relay/events.py`  
  Relay event model and SSE event fanout helpers.

- `tests/test_relay_models.py`
- `tests/test_relay_context.py`
- `tests/test_relay_envelopes.py`
- `tests/test_relay_store.py`
- `tests/test_relay_service.py`
- `tests/test_relay_routes.py`
- `tests/test_relay_ui_routes.py`

Modify:

- `wlcodex/db.py`  
  Ensure existing team tables support relay metadata fields through JSON payloads or lightweight additive columns already consistent with the team schema.

- `wlcodex/live_stream/server.py`  
  Add `/native/workflows`, `/native/workflows/relay`, `/native/workflows/relay/tasks/{task_id}`, and `/api/relay/...` routes.

- `wlcodex/main.py`  
  Wire `RelayStore` and `RelayService` when the native agent registry and ledger are available.

- Existing native/live UI template modules in the repo  
  Add workflow card, workflow directory, relay task list, relay task board, and relay CSS/JS using current template conventions.

- Existing native route tests  
  Add navigation coverage for token/cookie preservation through the workflow hierarchy.

## Task 1: Relay Models And Constants

**Files:**

- Create: `wlcodex/relay/__init__.py`
- Create: `wlcodex/relay/models.py`
- Test: `tests/test_relay_models.py`

- [ ] Define fixed relay roles:

```text
director
architect
implementer
tester
auditor
```

- [ ] Define Relay Task statuses:

```text
queued
running
waiting_user
blocked
failed
completed
interrupted
```

- [ ] Define Role Job statuses:

```text
idle
queued
streaming
waiting
passed
failed
blocked
interrupted
```

- [ ] Define artifact types:

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

- [ ] Add dataclasses for:

```text
RelayTask
RelayRoleJob
RelayBoard
RoleContextPacket
RoleEnvelope
HandoffPacket
RelayTaskSummary
RelayTaskDetail
RelaySessionLink
```

- [ ] Tests assert that role order, display names, statuses, and artifact types match the design spec exactly.

## Task 2: Context Packet And Envelope Guardrails

**Files:**

- Create: `wlcodex/relay/context.py`
- Create: `wlcodex/relay/envelopes.py`
- Test: `tests/test_relay_context.py`
- Test: `tests/test_relay_envelopes.py`

- [ ] Build `RelayBoard` from task state, latest user input, confirmed facts, open questions, risks, current dispatch, and next step.

- [ ] Build `RoleContextPacket` with only role-relevant information:

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

- [ ] Add a test proving RoleContextPacket excludes full transcript text even when a caller passes transcript-like data.

- [ ] Add a test proving latest user input wins over stale handoff summaries.

- [ ] Parse `role_envelope` from JSON output and validate required fields:

```text
status
reason
role
artifact_type
handoff_to
summary
evidence_refs
open_questions
next_action
```

- [ ] Add a transition guard: unparseable or invalid envelopes return a validation error and do not advance role or task state.

- [ ] Add tests for default handoff transitions:

```text
architect -> implementer
implementer -> tester
tester -> auditor
auditor -> director
```

## Task 3: Relay Store Over Existing Team Tables

**Files:**

- Create: `wlcodex/relay/store.py`
- Modify: `wlcodex/db.py`
- Test: `tests/test_relay_store.py`

- [ ] Inspect existing `team_runs`, `team_agent_jobs`, `team_context_packets`, and `team_artifacts` schema before editing migrations.

- [ ] Add only additive migration changes needed for relay metadata. Prefer JSON payload fields when the table already has a payload/details column.

- [ ] Implement `create_task()`:
  - creates one `team_runs` row with `route="relay"`
  - creates five `team_agent_jobs` rows, one per fixed role
  - initializes director as `queued`
  - initializes other roles as `idle`

- [ ] Implement `list_tasks(workspace=None, status=None)` for the relay task list page.

- [ ] Implement `get_task_detail(task_id)` returning RelayBoard, role jobs, latest artifacts, latest handoff, and child session links.

- [ ] Implement `save_context_packet(task_id, role, packet)`.

- [ ] Implement `save_artifact(task_id, role, artifact_type, payload)`.

- [ ] Implement `save_handoff_packet(task_id, from_role, to_role, packet)` as a specialized artifact helper.

- [ ] Implement role/job metadata updates for:

```text
provider
provider_engine
model
native_session_id
dispatch_verified
fallback_reason
```

- [ ] Tests verify:
  - relay task id maps to `team_runs.id`
  - creating a task creates exactly five role jobs
  - director starts first
  - handoff packets are persisted in `team_artifacts`
  - no unrelated non-relay team runs appear in `list_tasks()`

## Task 4: Relay Service And Provider Dispatch

**Files:**

- Create: `wlcodex/relay/service.py`
- Create: `wlcodex/relay/events.py`
- Test: `tests/test_relay_service.py`

- [ ] Implement `RelayService.create_task()`:
  - records the task
  - builds the initial RelayBoard
  - queues director dispatch
  - emits `task.created`, `role.queued`, and `artifact.created`

- [ ] Implement `dispatch_role(task_id, role)`:
  - reads the latest RelayBoard and relevant handoff packets
  - builds RoleContextPacket
  - calls the selected provider through `NativeAgentRegistry`
  - records provider metadata and native session id
  - emits `dispatch.verified` when the provider confirms the session

- [ ] Implement compatibility fallback:
  - if provider capability is insufficient, record `fallback_reason`
  - emit `dispatch.fallback`
  - keep task state accurate instead of silently failing

- [ ] Implement role output handling:
  - append stream deltas to the role lane
  - parse final `role_envelope`
  - save role artifact
  - save handoff packet when `handoff_to` is present
  - advance the next role only when the envelope is valid

- [ ] Implement `add_user_message(task_id, text)`:
  - stores the user message as latest input
  - routes the message to director by default
  - does not send the message directly to all child sessions

- [ ] Implement `interrupt(task_id, role=None)`:
  - role-specific interrupt updates only that role job
  - task-level interrupt updates the relay task and all active jobs

- [ ] Tests verify:
  - user follow-up enters director only
  - invalid envelope blocks advancement
  - implementer success queues tester
  - tester success queues auditor
  - auditor success queues director final summary
  - single-role interrupt does not interrupt the whole task

## Task 5: Relay API Routes

**Files:**

- Modify: `wlcodex/live_stream/server.py`
- Test: `tests/test_relay_routes.py`

- [ ] Add route:

```text
GET /api/relay/tasks
```

Returns task summaries grouped or filterable by status/workspace.

- [ ] Add route:

```text
POST /api/relay/tasks
```

Creates a relay task from:

```text
title
prompt
workspace
provider
```

- [ ] Add route:

```text
GET /api/relay/tasks/{task_id}
```

Returns RelayTaskDetail.

- [ ] Add route:

```text
GET /api/relay/tasks/{task_id}/events
```

Streams SSE events with role lane identity and sequence numbers.

- [ ] Add route:

```text
GET /api/relay/tasks/{task_id}/sessions
```

Returns child native session links grouped by role.

- [ ] Add route:

```text
POST /api/relay/tasks/{task_id}/message
```

Routes user input to director.

- [ ] Add route:

```text
POST /api/relay/tasks/{task_id}/interrupt
```

Interrupts a role or the whole task.

- [ ] Add compatibility aliases for the earlier run naming:

```text
POST /api/relay/runs
GET  /api/relay/runs/{run_id}
GET  /api/relay/runs/{run_id}/events
POST /api/relay/runs/{run_id}/message
POST /api/relay/runs/{run_id}/interrupt
```

- [ ] Tests verify HTTP status codes, JSON shapes, SSE lane metadata, token preservation, and task-not-found behavior.

## Task 6: Navigation UI And Workflow Directory

**Files:**

- Modify existing native/live UI template files used by `/native`
- Modify: `wlcodex/live_stream/server.py`
- Test: `tests/test_relay_ui_routes.py`

- [ ] Add `工作流` card to `/native`.

- [ ] Ensure `/native` still shows:

```text
Codex
Claude
Antigravity
议会审核
工作流
```

- [ ] Add `/native/workflows` page with:

```text
流式接力模式
议会审核
Dev Flow
```

- [ ] Link `流式接力模式` to `/native/workflows/relay`.

- [ ] Link `议会审核` to `/council`.

- [ ] Keep Dev Flow as an informational card unless the repo already has a stable route to open.

- [ ] Preserve token query/cookie behavior on all links.

- [ ] Tests verify rendered page text and link targets.

## Task 7: Relay Task List UI

**Files:**

- Modify existing native/live UI template and JS/CSS files
- Test: `tests/test_relay_ui_routes.py`

- [ ] Build `/native/workflows/relay` as a task workspace page, not a session list.

- [ ] Add workspace/project selector using existing native history/workspace conventions.

- [ ] Add publish-task composer:

```text
title
task prompt
workspace
provider
submit
```

- [ ] Render task groups:

```text
running
waiting_user
blocked
completed
interrupted
```

- [ ] Render task cards with:

```text
title
workspace
phase
director decision
five role status chips
latest handoff summary
last activity
open task
```

- [ ] Ensure long titles and summaries wrap without overlapping on mobile.

- [ ] Ensure task cards do not expose every child native session as top-level rows.

- [ ] Tests verify empty state, task list rendering, grouping, and open-task links.

## Task 8: Relay Task Detail UI

**Files:**

- Modify existing native/live UI template and JS/CSS files
- Test: `tests/test_relay_ui_routes.py`

- [ ] Build `/native/workflows/relay/tasks/{task_id}` with:

```text
Task header
RelayBoard
总工程师 panel
架构工程师 panel
开发工程师 panel
测试工程师 panel
审计工程师 panel
bottom composer
```

- [ ] Layout roles as:

```text
总工程师

架构工程师        开发工程师

测试工程师        审计工程师
```

- [ ] Role panels show:

```text
state
provider/model
native_session_id
stream output
latest handoff summary
open questions
open native session link
fallback marker
```

- [ ] Idle roles remain visually idle until dispatched.

- [ ] SSE updates stream into the matching role panel only.

- [ ] Bottom composer posts to `/api/relay/tasks/{task_id}/message`.

- [ ] Interrupt control can target one role or the whole task.

- [ ] Tests verify mobile/desktop markup structure, role lane updates, and idle state rendering.

## Task 9: Verification

**Files:**

- Existing tests and new relay tests

- [ ] Run focused relay tests:

```bash
pytest tests/test_relay_models.py tests/test_relay_context.py tests/test_relay_envelopes.py tests/test_relay_store.py tests/test_relay_service.py tests/test_relay_routes.py tests/test_relay_ui_routes.py
```

- [ ] Run related native workflow tests:

```bash
pytest tests/test_worker_live_stream_native_agent_routes.py tests/test_main_composition.py
```

- [ ] Run lint/type checks used by this repo if present in the project scripts.

- [ ] Open the local app and manually verify:
  - `/native` shows `工作流`
  - `/native/workflows` shows workflow directory
  - `/native/workflows/relay` shows task list
  - creating a task opens `/native/workflows/relay/tasks/{task_id}`
  - five role panels do not overlap
  - director starts before other roles
  - unstarted roles remain idle
  - streaming output stays in the correct role lane

- [ ] Run GitNexus change detection before commit:

```bash
npx gitnexus detect-changes -r wlcodex
```

## Acceptance Criteria

- `/native` remains the top-level native entry and includes `工作流`.
- `/native/workflows` is the workflow directory.
- `/native/workflows/relay` is a Relay Task list/workspace, not the five-role board.
- `/native/workflows/relay/tasks/{task_id}` is the five-role board.
- One Relay Task can own multiple child native agent sessions.
- Director receives new user input by default.
- Role contexts exclude full transcripts.
- Handoff packets are persisted and referenced by the next role.
- Provider dispatch goes through `NativeAgentRegistry`.
- Provider fallback is visible in API and UI.
- Role envelope parse failure blocks state advancement.
- Single-role interruption does not interrupt the whole task.
