# WLCodex Virtual Engineering Office Design

## Status

Drafted on 2026-05-29 from the product direction discussed with the user.

This is a business and architecture design document. It is not an
implementation plan and does not authorize code changes by itself.

## Executive Summary

WLCodex should evolve from a Telegram-based remote coding cockpit into a
**Virtual Engineering Office**: a phone-first project management and live
execution surface where the user sees a project as an office full of specialist
programmers.

The important product decision is that the "office" and "programmer" concepts
are a business layer, not a replacement for the underlying live stream.

From the user's perspective:

- the project has an office overview for business management;
- the project flows through chief engineer, architect, developer, tester,
  auditor, chief auditor, and back to chief engineer;
- each visible programmer or role station can be opened;
- opening a programmer shows that worker's real-time stream, terminal output,
  diffs, approvals, test results, and status just like the native Codex mobile
  remote experience;
- the live stream should not be summarized, transformed, or delayed merely
  because it is displayed inside an office metaphor.

The office layer adds organization, hierarchy, status, and business
comprehension. It must not make the real-time layer worse.

## Project Identity

WLCodex repository:

```text
https://github.com/379350697/wlcodex
```

This document assumes that repository as the concrete product and codebase
context. Other models or reviewers should use the repository above when they
need to inspect the current WLCodex implementation, existing Remote Workbench
behavior, adaptive engineering team design, Telegram transport, Codex backend,
Claude backend, runtime event ledger, Cockpit view, and Onsite live stream.

## Background: Current WLCodex

WLCodex is currently a remote workbench for phone-driven software engineering.
One local machine runs the actual work. The phone exposes two views over the
same workbench:

- **Cockpit / 驾驶舱**: concise progress, decisions, approvals, and result
  summaries;
- **Onsite / 现场**: live terminal-style control with raw agent output and
  direct steering.

The existing product model already contains several ideas that the Virtual
Engineering Office should preserve:

- local execution stays on the user's machine;
- the phone is a remote window into that machine;
- switching between Cockpit and Onsite does not restart work;
- leaving Onsite does not kill the underlying agent;
- runtime events and SQLite act as the durable ledger;
- Codex and Claude sessions can be resumed when the backend exposes session
  references;
- natural streaming forwards deltas from the same Codex or Claude run and does
  not double model token usage;
- Onsite frames are redacted before remote delivery.

WLCodex also already has the beginning of a role-aware engineering team model.
The existing adaptive-team configuration separates roles such as director,
architect, implementer, tester, and auditor, and maps those roles to model
profiles such as Codex GPT and Claude DeepSeek. The new design should extend
that model into a user-facing business surface rather than inventing an
unrelated orchestration system.

## Background: Native Codex Mobile Remote

OpenAI describes Codex in the ChatGPT mobile app as a mobile experience for
staying connected to active Codex work across laptops, devboxes, and remote
environments. The official background that matters for WLCodex is:

- the mobile app loads live state from the machine where Codex is running;
- the user can work across active threads, approvals, plugins, and project
  context;
- the phone can review outputs, approve commands, change models, and start new
  work;
- files, credentials, permissions, and local setup stay on the machine where
  Codex operates;
- updates flow back to the phone in real time, including screenshots, terminal
  output, diffs, test results, and approvals;
- a secure relay layer keeps trusted machines reachable across devices without
  exposing them directly to the public internet;
- Remote SSH environments can participate in the same mobile-access pattern.

Sources:

- OpenAI, "Work with Codex from anywhere", 2026-05-14:
  https://openai.com/index/work-with-codex-from-anywhere/
- OpenAI, "Unlocking the Codex harness: how we built the App Server",
  2026-02-04:
  https://openai.com/index/unlocking-the-codex-harness/
- OpenAI Codex product page:
  https://openai.com/codex/

The Virtual Engineering Office should copy this interaction principle, not its
exact product surface. The essential principle is:

```text
local/remote execution environment owns the work
  -> live state and events flow to the phone
  -> the phone can inspect, approve, steer, and start work
  -> sensitive files and credentials remain in the execution environment
```

## Canonical Meaning: Native Codex Mobile Remote Streaming

For this design, "native Codex mobile remote streaming" has a specific meaning.
It does not mean ordinary chat streaming, a Telegram-style edited message, or a
periodic project summary. It means a mobile client is connected to the live
state of a machine where Codex is already operating, and it renders the same
work as structured, real-time execution state.

The canonical experience has these properties:

1. **Machine-owned execution**: Codex runs on a trusted machine such as a Mac,
   laptop, devbox, or Remote SSH environment. The mobile device is not the
   execution host.
2. **Live state loading**: when the phone connects, it loads the current state
   of that environment, including active threads, approvals, plugins, and
   project context.
3. **Structured real-time updates**: the phone receives live updates such as
   screenshots, terminal output, diffs, test results, approvals, and agent
   progress. These are not just assistant text tokens.
4. **Bidirectional control**: the user can review outputs, approve commands,
   change direction, change models, answer questions, and start new work.
5. **Session continuity**: the user can disconnect, switch devices, or return
   later without making the agent restart from scratch.
6. **Secure relay posture**: trusted machines remain reachable across devices
   without being directly exposed to the public internet.
7. **Local custody of sensitive context**: files, credentials, permissions, and
   local setup stay on the machine where Codex operates.

OpenAI's App Server background is relevant because it describes the underlying
shape as a client-friendly, bidirectional JSON-RPC API over the Codex harness.
It frames Codex surfaces as clients that render the same streaming events and
approvals while keeping the agent close to the compute environment. WLCodex does
not need to copy OpenAI's exact private implementation, but it should copy this
conceptual shape:

```text
execution machine
  -> app-server / agent harness / provider adapter
      -> structured live events and approvals
          -> remote client renders state and sends control actions
```

For WLCodex, this means a worker station must behave like a native Codex remote
client:

- it opens the worker's current live session;
- it subscribes to the worker's event cursor;
- it renders deltas, terminal frames, diffs, tests, screenshots, and approvals
  as first-class live evidence;
- it sends user control actions back to the running worker;
- it recovers by loading snapshot plus event cursor, not by asking another
  model to reconstruct the past.

This is the baseline that "the stream should be exactly the same" refers to in
the rest of this document. The Virtual Engineering Office may add job titles,
rooms, routing, and management summaries around the stream, but a clicked
programmer's live station must remain equivalent to this native Codex mobile
remote streaming model.

## Core Product Thesis

The user does not want a chat bot and does not merely want remote terminal
access. The user wants a **business-manageable engineering organization** where
each visible programmer is also a clickable live Codex-style stream.

The office metaphor exists because complex agent work is easier to manage when
it is represented as people, rooms, responsibilities, and handoffs:

```text
Project
  -> Office overview
      -> Chief engineer station
      -> Architecture office
      -> Development area
      -> Testing area
      -> Audit area
      -> Chief audit station
      -> Final chief engineer delivery
```

This metaphor must remain honest. A visible programmer is not an avatar running
a separate fake process. A visible programmer is a business projection of an
actual worker run, backed by a concrete provider, model, workspace, permission
mode, tools, artifacts, and live event stream.

## Product Positioning

WLCodex should become:

```text
An AI engineering office control plane
for managing multi-provider coding agents
with native-Codex-like live mobile inspection.
```

It is not:

- a Telegram bot with nicer messages;
- a pure Web terminal;
- a project management board disconnected from execution;
- a role-play layer that hides or rewrites the real worker stream;
- a Codex-only product.

Codex, Claude Code, Antigravity, and future tools are execution backends.
The WLCodex office is the control plane, project ledger, live state renderer,
business workflow manager, and user steering surface.

## Design Principle: Real-Time Invariance

The most important requirement is **real-time invariance**:

> Clicking into any programmer must show the same kind of real-time stream the
> user would see in native Codex mobile remote. The office metaphor may add
> labels, hierarchy, routing, and summaries around the stream, but it must not
> change the stream's nature.

This means:

- stream deltas remain deltas from the active underlying run;
- terminal output remains terminal output;
- diffs remain diffs;
- test results remain test results;
- approval cards remain approval cards;
- screenshots or browser frames remain live evidence, not a model-written
  retelling;
- the same event stream powers both the office overview and the worker detail
  page;
- UI recovery uses event snapshots and cursors, not model re-prompting.

The office layer may derive status from events, such as "Developer A is testing
auth/session", but it should not ask another model to continuously rewrite the
worker's stream unless the user explicitly requests a summary.

## User Mental Model

### User-Facing Objects

| Object | User meaning | System meaning |
| --- | --- | --- |
| Project | A business task or feature request | A durable top-level run with scope, state, artifacts, and timeline |
| Office | A phase or group of roles | A projection over workers and stage state |
| Programmer / Worker | A visible person doing work | A role-bound agent run backed by a provider/model |
| Station / Workstation | A clickable work area | A route to a worker's live stream and artifacts |
| Chief Engineer | The project coordinator | Director/orchestrator plus optional model-backed reasoning |
| Architect | Design owner | Role job that produces architecture and handoff artifacts |
| Developer | Implementation owner | Role job that modifies code through a selected backend |
| Tester | Verification owner | Role job that runs or designs tests and reports evidence |
| Auditor | Review owner | Role job that checks quality, risk, and acceptance |
| Chief Auditor | Final acceptance owner | Aggregates audit/test evidence and decides pass/retry/stop |
| Live Site / Onsite | Raw real-time view | Existing WLCodex live stream surface |

### User Journey

```text
1. User opens WLCodex mobile/web.
2. User sees an office overview with active projects.
3. User creates or opens a project.
4. User publishes a task to the chief engineer.
5. Chief engineer analyzes scope and splits the work.
6. Architecture work starts when needed.
7. Development workers are assigned, possibly across different backends.
8. Testing workers verify outputs.
9. Audit workers review implementation quality and risks.
10. Chief auditor makes final acceptance decision.
11. Chief engineer returns the final delivery to the user.
12. At any point, the user can click any programmer and see the live stream.
```

The user should feel like they are managing an engineering office, while each
programmer detail screen still feels like native Codex remote live execution.

## Office Workflow

The default project workflow is:

```text
User task
  -> Chief Engineer
      -> scope, classify, assign, choose route
  -> Architect
      -> design, boundaries, risk, acceptance criteria
  -> Developers
      -> one or more implementation workers
  -> Testers
      -> one or more verification workers
  -> Auditors
      -> one or more review workers
  -> Chief Auditor
      -> final acceptance across all evidence
  -> Chief Engineer
      -> final user delivery and next actions
```

The workflow is adaptive:

- small read-only questions may stay with the chief engineer or Codex-only
  analysis;
- direct implementation may use one developer and one auditor;
- broad features may create architect, multiple developers, multiple testers,
  and multiple auditors;
- high-risk changes may require chief auditor approval before final delivery;
- failed tests or audit findings can route work back to the specific developer
  station that owns the failing scope.

## Role And Backend Separation

Roles are business responsibilities. Backends are execution systems. Models are
runtime choices. These must stay separate.

Example workers:

| Visible worker | Role | Backend | Default model | Typical use |
| --- | --- | --- | --- | --- |
| Chief Engineer | director | codex | GPT-5.5 | Project understanding, routing, final synthesis |
| Architect | architect | codex | GPT-5.5 | Architecture and design judgement |
| Claude Developer A | developer | claude_code | DeepSeek V4 Pro | Hands-on implementation |
| Codex Developer B | developer | codex | GPT-5.5 | Implementation, refactor, tests, review-heavy edits |
| Antigravity Developer C | developer | antigravity | configurable | GUI, browser, visual, app-flow heavy work |
| Tester A | tester | codex | GPT-5.5 | Test planning and verification |
| Tester B | tester | claude_code | DeepSeek V4 Pro | Running tests and repair loops |
| Auditor A | auditor | codex | GPT-5.5 | Code review and risk analysis |
| Auditor B | auditor | claude_code | DeepSeek V4 Pro | Independent implementation-aware review |
| Chief Auditor | chief_auditor | codex | GPT-5.5 | Final acceptance decision |

The defaults above are product defaults, not hard rules. Every role should
eventually support:

- backend selection;
- model selection;
- permission mode;
- workspace;
- tools and skills;
- cost/latency preference;
- parallelism policy;
- write-scope policy.

## Provider Adapter Model

Each execution backend must be normalized behind a common worker event
contract.

```text
Codex app-server / CLI
Claude Code
Antigravity
future backend
  -> Provider Adapter
      -> normalized worker events
          -> runtime ledger
              -> office overview
              -> worker live stream
              -> artifacts
              -> recovery
```

The provider adapter is responsible for translating raw backend behavior into
the office system without losing the live feel.

For example:

- Codex app-server notifications become worker deltas, command output, diffs,
  approval requests, screenshots, and test events.
- Claude Code stream-json output becomes worker deltas and artifact evidence.
- Antigravity GUI/browser actions become frames, screenshots, interaction
  steps, and verification evidence.

The phone/web client should not need to know the backend's native protocol.
It should subscribe to a stable worker stream.

## Event Model

The office system should be event-sourced. Events power both business overview
and live worker detail.

Core event families:

```text
project.created
project.scoped
project.stage.started
project.stage.completed
project.blocked
project.completed

office.created
office.updated

worker.assigned
worker.started
worker.status.changed
worker.delta
worker.command.started
worker.command.output
worker.file.changed
worker.diff.updated
worker.test.started
worker.test.result
worker.approval.requested
worker.approval.resolved
worker.artifact.created
worker.blocked
worker.completed
worker.failed

handoff.created
handoff.accepted

chief_audit.started
chief_audit.decision
chief_engineer.delivery.created
```

The office overview reads the same event log and derives:

- which role is active;
- who is blocked;
- which files changed;
- which tests passed or failed;
- which approvals need the user's decision;
- which stage owns the current bottleneck.

The worker detail screen reads the same event log but renders it as a live
Codex-style stream.

## Real-Time Stream Contract

Every worker detail screen should expose these lanes:

1. **Live output lane**: model deltas, command output, terminal frames, and
   provider-native progress.
2. **Evidence lane**: diffs, screenshots, browser frames, test results, touched
   files, artifacts.
3. **Decision lane**: approval requests, clarifying questions, route choices,
   retry/pass/stop decisions.
4. **Input lane**: user can steer, answer, approve, deny, pause, resume, or
   return to overview.

The default view should be the live output lane. The other lanes should be
available without interrupting the run.

The system should avoid continuous LLM rewriting of live streams. If summaries
are needed, they should be explicit artifacts:

- "summarize this worker";
- "summarize this stage";
- "prepare final delivery";
- "explain why this worker is blocked".

## Token And Cost Model

Streaming a worker's live output to the phone should not add model token cost
by itself.

The cost-free path is:

```text
model/backend produces output once
  -> local daemon records normalized events
  -> relay/websocket forwards events
  -> phone/web client renders events
```

Extra token cost appears only when the system asks a model to process the
events again, such as:

- continuous digest rewriting;
- summarizing every stream chunk;
- refeeding historical logs into model context for recovery;
- using model calls for UI actions that should read local state;
- asking a separate supervisor model to narrate every worker in real time.

The architecture should therefore separate:

- **transport and rendering**, which should be token-free;
- **reasoning and summarization**, which intentionally consumes tokens.

## Product Screens

### 1. Project Lobby

Shows all active and recent projects.

Each project card shows:

- project title;
- current phase;
- active office;
- active worker count;
- blocked decisions;
- latest meaningful event;
- risk level;
- completion state.

### 2. Office Overview

Shows one project as a virtual office.

Recommended sections:

- project header: goal, workspace, branch/worktree, current stage, owner;
- phase timeline: chief engineer -> architect -> development -> testing ->
  audit -> chief audit -> delivery;
- office map: grouped worker stations;
- decision inbox: approvals and questions waiting for the user;
- evidence panel: latest diffs, tests, artifacts, and audit findings;
- activity stream: compact timeline of worker events.

The overview is for management. It should not replay all raw output.

### 3. Worker Station

Clicking any programmer opens the live worker station.

Header:

- visible worker name, such as "Claude Developer A";
- role;
- backend;
- model;
- current assignment;
- status;
- workspace;
- permission mode.

Body:

- live stream, matching Codex mobile remote expectations;
- terminal output, diffs, tests, approvals, and screenshots as native evidence;
- handoff artifact when the worker finishes;
- controls: steer, approve, deny, pause delivery, resume, stop, return to
  overview.

The station is the existing Onsite idea generalized from "terminal view" to
"worker live site".

### 4. Stage Room

Optional intermediate screen for a group of workers in the same phase.

Examples:

- Development Room: Developer A, Developer B, Developer C.
- Testing Room: Tester A, Tester B.
- Audit Room: Auditor A, Auditor B, Chief Auditor.

Useful when many workers run in parallel.

### 5. Final Delivery

The final delivery is created by the chief engineer after chief audit.

It should include:

- what was requested;
- what was changed;
- who did what;
- important design decisions;
- tests and verification evidence;
- audit result;
- remaining risks;
- next recommended actions.

## Business Rules

### Rule 1: Office Metaphor Is A Projection

The office view projects existing and future agent runs into business roles.
It must not create a fake second source of truth.

### Rule 2: Worker Identity Is Stable During A Run

If "Claude Developer A" starts a task, the UI should keep that identity through
completion, even if the underlying provider emits multiple internal events or
subprocesses.

### Rule 3: Backends Are Replaceable

Codex, Claude Code, and Antigravity are interchangeable at the role-assignment
layer. A role chooses a backend through policy, user preference, or task type.

### Rule 4: Live Stream Is Not A Summary

The worker detail screen must display live evidence. Summary is an additional
action, not the default stream.

### Rule 5: Parallelism Requires Isolation

Multiple developers can run in parallel only when their write scopes, branches,
worktrees, or task boundaries are isolated enough to avoid collisions.

### Rule 6: Audit Must Be Independent Enough To Matter

The final audit should not simply trust the developer's own conclusion. It
should inspect artifacts, diffs, tests, and acceptance criteria.

### Rule 7: User Decisions Stay Central

Approvals, risky route choices, permission escalations, and unclear product
decisions should surface in the decision inbox and in the relevant worker
station.

## Architecture Overview

At the architecture level:

```text
Mobile/PWA Client
  -> Relay / Local WebSocket Gateway
      -> WLCodex Control Plane
          -> Project + Office Projection
          -> Runtime Event Ledger
          -> Scheduler / Director
          -> Provider Adapters
              -> Codex
              -> Claude Code
              -> Antigravity
              -> Future backends
```

### Client

The client should be a PWA or web app first. It can later become native iOS or
Android if needed. PWA is enough to prove the product because the main need is
live state, event rendering, approvals, and project navigation.

### Relay

The relay keeps the Mac or execution environment reachable without exposing it
directly to the public internet. For personal use, a local network or Tailscale
path may be enough. For native-Codex-like experience, a secure relay is the
right long-term shape.

### WLCodex Control Plane

The control plane owns:

- project lifecycle;
- office projection;
- worker registry;
- role assignment policy;
- provider adapter routing;
- durable event ledger;
- approvals;
- artifacts;
- recovery;
- final delivery.

### Provider Adapters

Adapters own provider-specific details and emit normalized worker events.

### Runtime Event Ledger

The ledger is the durable source of truth. Both the overview and worker station
recover from it. The ledger should support cursors so the client can reconnect
without replaying or model-summarizing everything.

## Role Assignment Policy

The Director should choose workers using a policy like:

```text
task kind
  + risk
  + expected files
  + UI/browser need
  + model preference
  + cost/latency mode
  + workspace isolation availability
  -> role assignments
```

Example defaults:

- architecture-heavy task -> Codex GPT-5.5 architect;
- large implementation -> Claude Code DeepSeek V4 Pro developer;
- review-heavy implementation -> Codex GPT-5.5 developer or auditor;
- browser/GUI-heavy task -> Antigravity worker;
- risky change -> at least one independent auditor plus chief auditor;
- simple answer -> no full office, just chief engineer/Codex analysis.

## Handoff Model

Each role should produce a handoff artifact for downstream roles.

Architect handoff:

- goal;
- design decision;
- impacted modules;
- constraints;
- acceptance criteria;
- risks and non-goals.

Developer handoff:

- changes made;
- files changed;
- commands run;
- tests run;
- unresolved concerns;
- diff summary.

Tester handoff:

- test plan;
- test results;
- failures;
- reproduction steps;
- coverage gaps.

Auditor handoff:

- findings;
- severity;
- evidence;
- pass/retry/stop recommendation.

Chief auditor handoff:

- final decision;
- required fixes if retry;
- acceptance evidence if pass.

Chief engineer delivery:

- user-facing project result;
- concise explanation;
- risk note;
- next actions.

## Recovery And Continuity

The office system must support interruption and reconnection:

- phone closes and reopens;
- relay disconnects;
- Mac daemon restarts;
- provider session resumes;
- a worker becomes orphaned;
- user switches from overview into a worker and back;
- user pauses delivery but lets work continue.

Recovery should follow:

```text
load latest project snapshot
  -> load office projection
  -> load worker statuses
  -> resume event cursor
  -> reattach live provider sessions where possible
  -> mark missing sessions as orphaned
```

The UI should be honest about orphaned workers. It should show their last known
state and offer recovery actions rather than pretending the stream is still
live.

## Security And Privacy

The native Codex mobile remote pattern keeps files, credentials, permissions,
and local setup on the execution machine. WLCodex should preserve that posture.

Security requirements:

- do not expose the Mac daemon directly to the public internet;
- use device pairing and scoped session tokens;
- keep provider credentials local where possible;
- redact secrets in terminal frames and artifacts;
- separate user-facing events from internal diagnostic events;
- log approvals and permission escalations;
- make worker backend/model/permission visible in station headers;
- do not feed remote UI cards back into model context.

## Non-Goals

This design does not require:

- replacing Codex's native mobile app;
- making a full native iOS/Android app first;
- simulating humans with avatars or animations;
- model-summarizing every stream event;
- forcing every task through all roles;
- making Antigravity, Codex, and Claude have identical capabilities;
- doing code-level implementation design.

## MVP Scope

### MVP 1: Office Projection Over Existing Workbench

Goal: prove the product shape without replacing the execution engine.

Capabilities:

- project overview page;
- role/station projection over current WLCodex runs;
- chief engineer, developer, tester/auditor labels;
- click worker -> existing live stream;
- approvals and decisions visible in overview;
- no continuous model summaries.

This can initially use local web/PWA or a simple relay.

### MVP 2: Multi-Provider Worker Registry

Goal: make Codex, Claude Code, and Antigravity configurable workers.

Capabilities:

- worker profile catalog;
- role-to-backend/model assignment policy;
- visible backend/model labels;
- per-worker live session references;
- normalized worker event stream.

### MVP 3: Full Office Workflow

Goal: run a project through the explicit business workflow.

Capabilities:

- chief engineer project split;
- architect stage;
- multiple developers;
- multiple testers;
- multiple auditors;
- chief auditor final acceptance;
- chief engineer final delivery.

### MVP 4: Native-Remote Quality

Goal: approach native Codex mobile remote smoothness.

Capabilities:

- secure relay;
- push notifications;
- reconnect by cursor;
- screenshots/browser frames;
- rich diffs;
- test evidence views;
- mobile-first worker switching;
- multiple active projects.

## Acceptance Criteria

The design is successful when:

1. A user can open a project and understand its state from the office overview
   without reading raw terminal output.
2. A user can click any visible programmer and see that worker's actual live
   stream.
3. The stream shown in a worker station is not a separate summary or replay
   generated by another model.
4. Codex, Claude Code, and Antigravity can be represented as backend choices,
   not as hard-coded roles.
5. Worker headers clearly show role, backend, model, status, and assignment.
6. The overview and worker station are powered by the same event ledger.
7. Streaming to the phone does not add model token cost unless a summary or
   extra reasoning action is explicitly requested.
8. Approvals and user decisions are visible both in the office overview and in
   the relevant worker station.
9. Disconnecting and reconnecting the client restores project, office, and
   worker state without restarting the work.
10. The final delivery is business-readable and backed by role artifacts and
    evidence.

## Open Questions

These are business/product decisions, not implementation blockers:

- Should the first public surface be PWA, local web over Tailscale, or
  relay-backed web?
- Should the office map be literal/floorplan-like or a structured operations
  dashboard using office language?
- How many default workers should exist before the user configures anything?
- Should chief auditor always be separate from auditor, or only for high-risk
  projects?
- Should users be allowed to manually reassign a worker mid-run?
- Should Antigravity be treated as a developer, a browser operator, or a
  separate GUI specialist role?

## Recommended Direction

Build the Virtual Engineering Office as a business projection over WLCodex's
existing workbench and event model.

Do not start by rewriting live streaming. The live stream is the product's most
valuable part and should remain provider-native after normalization. Start by
adding office/project/worker semantics around it:

```text
existing live workbench
  + project overview
  + worker profiles
  + role/backend/model labels
  + event-derived office status
  + clickable worker stations
```

The final product should feel like this:

> I am not chatting with a bot. I am managing an AI engineering office. Every
> programmer has a role and a provider, and when I click into one, I see the
> real work happening live, just like native Codex mobile remote.
