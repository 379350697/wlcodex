# WLCodex Multi-Agent Workflow Design

## Status

Drafted on 2026-06-04 after the user selected the workflow-first option for
multi-agent collaboration.

This is a product and architecture design spec. It does not authorize product
code changes by itself.

## Project Context

WLCodex now has a provider-parametric native agent surface:

```text
codex
claude
antigravity
```

The live page can open provider-specific sessions through
`/api/native/{provider}/...`, and each provider is isolated behind
`NativeAgentRegistry` and the `NativeAgentProvider` protocol.

The current handoff need is broader than "Antigravity gives a prompt to
Claude." The long-term product direction is multi-agent collaboration:

```text
design -> implement -> verify -> deploy
```

Different steps may run on different providers. The implementation must not
hardcode one provider pair or one live-page button as the whole feature.

## Product Goal

Add a reusable workflow layer for multi-agent collaboration.

The first workflow type is "handoff execution":

```text
current provider session -> preview handoff -> user confirms -> target provider starts a new session
```

The UI entry is a new button near the existing "默认权限" control:

```text
接棒执行
```

The user can choose the target provider:

```text
codex
claude
antigravity
```

Before execution, WLCodex shows an editable prompt preview. The generated prompt
adapts to the current situation:

- a spec and plan are ready, and the next agent should execute them;
- a bug was found, and the next agent should investigate and fix it;
- a small feature request has no formal documents, and the next agent should
  implement it directly;
- the current context is ambiguous, and the user can edit the prompt before
  starting the target agent.

## Core Decision

Build option C: a workflow-first collaboration layer.

The first shipped workflow is handoff execution, but the core API and data
model must be reusable by later workflows such as:

```text
Antigravity designs -> Claude implements -> Codex verifies
Antigravity explores -> Codex fixes -> Claude reviews
Codex verifies -> Antigravity summarizes -> Codex deploys
```

This keeps the business behavior coupled to WLCodex collaboration concepts while
keeping provider implementation loose and replaceable.

## Plain-Language Boundary

Option B is a handoff tool:

```text
Who should take over this specific thing now?
```

Option C is a collaboration system:

```text
How should this work move through multiple agents until it is complete?
```

The first implementation should feel small, but it must sit on the option C
foundation.

## Architecture

Introduce a collaboration workflow layer above native providers:

```text
Live UI
  -> Workflow API
      -> WorkflowService
          -> HandoffPromptBuilder
          -> WorkflowRunStore
          -> NativeAgentRegistry
              -> codex provider
              -> claude provider
              -> antigravity provider
```

Provider implementations do not know about workflows. They only receive normal
`start_session` or `continue_session` calls.

The workflow layer knows WLCodex business concepts:

```text
workflow type
step
intent
artifact
handoff prompt
target provider
approval gate
workflow state
```

## Data Model

### WorkflowRun

Represents one multi-agent collaboration run.

```text
workflow_run_id
workflow_type
status
source_provider
source_thread_id
source_turn_id
cwd
created_at
updated_at
```

`workflow_type` initially supports:

```text
handoff_execution
```

Future values may include:

```text
design_implement_verify
bug_triage_fix_verify
review_then_deploy
```

### WorkflowStep

Represents one unit in the workflow.

```text
step_id
workflow_run_id
step_type
status
assigned_provider
native_thread_id
input_prompt
output_summary
created_at
updated_at
```

Initial step types:

```text
preview_handoff
execute_handoff
```

Future step types:

```text
design
implement
verify
review
deploy
summarize
```

### HandoffIntent

Classifies why work is being handed off.

```text
execute_plan
fix_bug
implement_feature
continue_work
custom
```

Intent is auto-detected during preview and editable by the user before
execution.

### HandoffArtifact

Represents files or references the target agent should read.

```text
kind
path
title
source
confidence
```

Initial artifact kinds:

```text
spec
plan
bug_report
diff
log
note
unknown
```

Artifacts are references, not copied transcript blobs.

### PromptPreview

Represents the generated prompt before execution.

```text
preview_id
workflow_run_id
intent
target_provider
prompt
artifacts
warnings
```

The prompt is editable in the UI. If the user edits it, execution uses the
edited prompt and records both the generated and submitted prompt metadata.

## API Design

Use workflow routes rather than provider-pair routes.

### Preview

```text
POST /api/native/workflows/handoffs/preview
```

Request:

```json
{
  "source_provider": "antigravity",
  "source_thread_id": "session-1",
  "source_turn_id": "",
  "target_provider": "claude",
  "cwd": "/Users/wl/projects/wlcodex",
  "intent": "auto",
  "user_note": ""
}
```

Response:

```json
{
  "workflow_run_id": "wf_...",
  "preview_id": "preview_...",
  "intent": "execute_plan",
  "target_provider": "claude",
  "prompt": "...",
  "artifacts": [
    {"kind": "spec", "path": "docs/superpowers/specs/..."},
    {"kind": "plan", "path": "docs/superpowers/plans/..."}
  ],
  "warnings": []
}
```

Preview must not start a target provider session.

### Execute

```text
POST /api/native/workflows/handoffs/execute
```

Request:

```json
{
  "workflow_run_id": "wf_...",
  "preview_id": "preview_...",
  "target_provider": "claude",
  "cwd": "/Users/wl/projects/wlcodex",
  "prompt": "edited or generated prompt"
}
```

Response:

```json
{
  "workflow_run_id": "wf_...",
  "step_id": "step_...",
  "target_provider": "claude",
  "target_thread_id": "native-session-id",
  "target_url": "/workers/128/live?native_provider=claude&native_thread_id=native-session-id",
  "status": "running"
}
```

Execution starts a new target provider session through
`NativeAgentRegistry.get(target_provider).start_session(...)`.

## Prompt Generation

Prompt generation is a separate module, not live-page JavaScript.

Suggested module:

```text
wlcodex/collaboration/handoff_prompts.py
```

It receives structured input:

```text
source provider
target provider
intent
cwd
session summary
recent user request
artifacts
user note
```

It returns a structured preview:

```text
intent
prompt
artifacts
warnings
```

### Execute Plan Prompt

Use when both a spec and plan are available or when the session clearly says
the next agent should execute a plan.

Prompt requirements:

- tell the target agent to read the exact spec and plan paths;
- tell it to execute, not rewrite the plan;
- include workspace path and current objective;
- require tests or verification;
- require preserving unrelated changes;
- require a concise final summary with changed files and verification result.

### Fix Bug Prompt

Use when the source context describes an error, failed test, regression,
unexpected behavior, stack trace, or user-reported bug.

Prompt requirements:

- state the observed symptom;
- ask the target agent to reproduce or inspect evidence first;
- prefer failing test before fix when practical;
- preserve unrelated changes;
- report root cause, files changed, and verification.

### Implement Feature Prompt

Use when the user requested a small feature and no formal spec or plan artifact
is detected.

Prompt requirements:

- state the user request and workspace;
- ask the target agent to inspect existing code patterns first;
- keep scope narrow;
- add focused tests where risk justifies them;
- report changed files and verification.

### Continue Work Prompt

Use when the session has work in progress but the next action is not clearly a
plan execution, bug fix, or feature implementation.

Prompt requirements:

- summarize current state;
- list known files or artifacts;
- ask the target agent to continue from the newest user request;
- avoid replaying old assistant output as fresh instructions.

### Custom Prompt

Use when the user edits the preview substantially or explicitly chooses custom.

The system should still include safe metadata such as workspace, source
provider, source thread, and target provider, but it should not override the
user's edited task.

## Intent Detection

Intent detection must be simple and explainable.

Initial rules:

1. If recent context or artifacts include both `docs/superpowers/specs/` and
   `docs/superpowers/plans/`, choose `execute_plan`.
2. If recent user text or session summary contains bug indicators such as
   error, failure, regression, unexpected behavior, traceback, stack trace, or
   failed test, choose `fix_bug`.
3. If recent user text requests adding, building, implementing, or changing a
   small capability without spec or plan artifacts, choose `implement_feature`.
4. Otherwise choose `continue_work`.

The preview response should include a human-readable reason or warning when the
classification confidence is low.

## UI Design

Add a "接棒执行" control next to "默认权限" in the live page composer settings.

Interaction flow:

1. User clicks "接棒执行".
2. A compact panel opens.
3. User chooses target provider: `codex`, `claude`, or `antigravity`.
4. User optionally chooses intent or leaves it as auto.
5. UI calls preview API.
6. Panel shows detected intent, artifacts, warnings, and editable prompt.
7. User confirms.
8. UI calls execute API.
9. WLCodex opens the new target provider session URL.

The panel must not require the user to copy-paste prompt text manually.

The button is disabled while the current source provider session is unknown.
If the source turn is still running, preview may be allowed, but execution should
show a warning that the handoff may miss the newest output.

## Provider Neutrality

Workflow code must not contain special-case branches such as:

```text
if source == "antigravity" and target == "claude"
```

Allowed provider-aware behavior:

- validate that a provider exists in `NativeAgentRegistry`;
- read provider capabilities;
- apply small target-agent prompt style profiles;
- format provider labels for UI.

Disallowed provider coupling:

- reading provider-specific private files from the workflow layer;
- importing provider implementation modules from prompt builders;
- treating Claude engines as separate business providers;
- sharing one provider's native thread id as another provider's session id.

## State And Storage

Add a lightweight workflow run store.

Suggested module:

```text
wlcodex/collaboration/workflow_store.py
```

The store records workflow runs, previews, steps, and source-target links. It
can use the same SQLite path family as native agent sessions, but should have
its own table names.

Minimum persistence:

- workflow run id;
- source provider and thread id;
- target provider and thread id;
- generated prompt hash or preview text;
- edited prompt text when submitted;
- status and timestamps.

This makes later workflow views possible without changing provider sessions.

## Error Handling

Preview errors:

- unknown source provider: 404;
- unknown target provider: 404;
- unreadable source session: 404 with clear message;
- missing target provider capability: 409;
- low-confidence classification: 200 with warning.

Execute errors:

- stale or missing preview: 404;
- target provider cannot start sessions: 409;
- source turn still running: 409 unless the request explicitly confirms;
- target provider start failure: 502 with provider error summary.

Execution must never mutate source provider session state.

## Security And Safety

The workflow layer must not copy secrets, bearer tokens, or full raw session
transcripts into prompts.

Prompt previews should include:

- exact artifact paths;
- compact session summary;
- newest user request;
- constraints and acceptance criteria.

Prompt previews should not include:

- complete historical transcript;
- environment variables;
- authentication tokens;
- unrelated hidden runtime metadata.

## Non-Goals For The First Version

The first version does not implement:

- a full visual workflow builder;
- parallel step execution;
- automatic provider selection;
- lossless conversation import between providers;
- remote workspace scheduling;
- automatic commit, push, or deploy chains.

These features should fit the workflow model later, but they are not required to
ship the first "接棒执行" workflow.

## Testing Strategy

Unit tests:

- intent detection chooses `execute_plan` when spec and plan artifacts exist;
- intent detection chooses `fix_bug` for bug evidence;
- intent detection chooses `implement_feature` for small feature requests;
- prompt builder emits different templates for each intent;
- prompt builder never includes raw full transcript input;
- workflow store records source and target session links.

Route tests:

- preview route returns prompt, intent, artifacts, and warnings without starting
  a target session;
- execute route starts a new session on the selected provider;
- execute route rejects unknown providers and providers without start-session
  capability;
- execute route preserves edited prompt text.

UI tests:

- live page contains the "接棒执行" control next to the permission control;
- the panel exposes target providers and editable preview text;
- confirm calls the workflow execute endpoint and navigates to the target
  session URL.

Regression tests:

- existing `/api/native/{provider}/...` routes keep working;
- Codex, Claude, and Antigravity provider pages still load;
- existing model settings and permission controls are not displaced on mobile.

## Rollout Plan

Phase 1: Workflow foundation

- add collaboration package;
- add workflow data objects;
- add intent detector and prompt builder;
- add workflow store;
- add preview route tests.

Phase 2: Handoff execution

- add execute route;
- call `NativeAgentRegistry` to start the target provider session;
- persist source-target links;
- return target live URL.

Phase 3: Live UI entry

- add "接棒执行" button near "默认权限";
- add target provider selector;
- add editable prompt preview panel;
- add confirm and navigation behavior.

Phase 4: Reusable workflow expansion

- add workflow status view;
- add chained steps such as design, implement, verify;
- let future workflow definitions reuse the same preview and execute primitives.

## Acceptance Criteria

- The user can click "接棒执行" in a live native session.
- The user can choose `codex`, `claude`, or `antigravity` as the target.
- WLCodex generates a prompt preview before execution.
- The preview adapts to plan execution, bug fixing, feature implementation, or
  continuation contexts.
- The user can edit the prompt before confirming.
- Confirming starts a new target provider session in the same workspace.
- The response links source and target sessions through workflow metadata.
- No provider implementation imports workflow code.
- Existing provider routes and live session behavior remain compatible.
- Tests cover prompt generation, workflow routes, workflow storage, and the live
  UI entry.
