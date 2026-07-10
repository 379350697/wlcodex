# WLCodex Native Agent Providers Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

## Status

Drafted on 2026-06-01 after the user confirmed that WLCodex should extend the
current Codex native web control experience to Claude Code and Antigravity, with
the same business behavior and a decoupled backend implementation.

This is an architecture and product design spec. It does not authorize product
code changes by itself.

## Project Context

WLCodex already has:

- a Codex native control path backed by the official Codex app-server protocol;
- a local worker live stream UI that renders runtime events;
- runtime event projection for Codex and Claude output;
- Claude Code CLI subprocess support using `stream-json`;
- hardcoded `/native/codex` and `/api/native/codex/...` web routes;
- a `native_codex_sessions` session store.

The current implementation proves the product shape, but it is still
Codex-specific. The next step is to turn this into a native-agent surface where
Codex, Claude Code, and Antigravity share one business contract while keeping
provider-specific execution code isolated.

## Product Goal

The user should be able to open the local WLCodex native web surface and choose
among supported native agents:

```text
codex
claude
antigravity
```

Each provider should feel like the existing Codex native page:

- list or create sessions where the backend supports it;
- open a session;
- load history;
- stream live events;
- continue a session with a user message;
- show commands, file changes, reasoning, and assistant text as the same UI
  concepts;
- show approval or permission actions when the backend exposes them;
- show clear capability-disabled states when a provider cannot support a
  control.

The user-facing business concepts must stay stable even if the backend
transport is different.

## Core Decision

Introduce a generic native-agent provider layer:

```text
NativeAgentProvider
  -> CodexAppServerProvider
  -> ClaudeCodeProvider
       -> ClaudeCodeCliLocalEngine
       -> ClaudeAgentSdkDeepSeekEngine
  -> AntigravitySdkProvider
```

`codex`, `claude`, and `antigravity` are the business providers. The UI, routes,
session store, and runtime event projection should key off this provider value.
There must not be separate business providers or pages named `claude-cli` and
`claude-deepseek`.

Claude is one business provider with one active engine at a time. The two Claude
engines are mutually exclusive branches:

```text
claude.engine = "cli-local"
claude.engine = "sdk-deepseek"
```

Only one engine may be active in a running WLCodex process. If legacy config,
environment overrides, or future compatibility shims attempt to enable both,
startup must fail with a configuration error instead of silently choosing one.

## Provider Contract

Every provider implements the same high-level contract:

```text
status()
capabilities()
list_sessions()
list_models()
start_session(request)
read_session(native_session_id)
attach_session(native_session_id)
sync_session(native_session_id, cursor)
continue_session(native_session_id, message)
steer_session(native_session_id, message)
interrupt_session(native_session_id)
resolve_approval(request_id, decision)
```

Providers return normalized data objects, not raw SDK or protocol payloads.
Provider-specific payloads may be preserved under a debug field, but UI code
must not depend on them.

Capabilities are explicit booleans:

```text
can_list_sessions
can_list_models
can_start_session
can_resume_session
can_read_history
can_stream_events
can_continue_session
can_steer_active_turn
can_interrupt
can_resolve_approval
can_apply_file_edits
can_run_shell_commands
```

The UI renders the same controls for every provider and disables unsupported
ones with a provider-supplied reason.

## Provider: Codex

`CodexAppServerProvider` wraps the existing Codex native controller/client.

Its first implementation should be a move and adapter extraction rather than a
behavior rewrite:

```text
current CodexNativeController
  -> NativeAgentController
      -> CodexAppServerProvider
```

The existing `/native/codex` route remains a compatibility alias. New generic
routes should use:

```text
/native/{provider}
/api/native/{provider}/...
```

There should be no generic route variant that exposes Claude engines as
top-level providers.

Codex remains the highest-fidelity provider because the official app-server
already exposes session listing, turn reads, steering, interrupt, approvals, and
structured notifications.

## Provider: Claude

Claude appears as one provider in the product:

```text
provider = "claude"
```

Its backend engine is selected by configuration:

```text
claude.engine = "cli-local"     # reuse local Claude Code and ccswitch behavior
claude.engine = "sdk-deepseek"  # use DeepSeek API key through Claude Agent SDK
```

### ClaudeCodeCliLocalEngine

This engine uses the installed Claude Code CLI as the stable local runtime
boundary.

It is the correct engine when the user wants to reuse local Claude Code state,
including `ccswitch` routing to DeepSeek or other configured providers.

The engine should:

- resolve the local `claude` binary from explicit config, env, PATH, and known
  user locations such as `~/.local/bin/claude`;
- call Claude Code with `--output-format stream-json` for live event parsing;
- use `--resume` for continuation when a session id is known;
- preserve the user's model/provider routing by default;
- not pass `--model` unless the WLCodex config explicitly sets a non-empty
  model override;
- sanitize only WLCodex-owned secrets that should never be exposed to a model
  subprocess;
- map Claude stream-json events into native-agent events.

This is a first-class backend, not a temporary fallback, because Claude Code CLI
officially exposes machine-readable streaming output.

### ClaudeAgentSdkDeepSeekEngine

This engine ignores local `ccswitch` state and connects directly to DeepSeek by
API key through the Claude Agent SDK / Anthropic-compatible API path.

It is the correct engine when the user wants a project-managed SDK runtime
instead of local Claude Code login/provider state.

Configuration:

```text
claude.engine = "sdk-deepseek"
claude.sdk_deepseek.api_key_env = "DEEPSEEK_API_KEY"
claude.sdk_deepseek.base_url = "https://api.deepseek.com/anthropic"
claude.sdk_deepseek.model = "deepseek-v4-pro"
```

The implementation should use the Claude Agent SDK when it can operate cleanly
against DeepSeek's Anthropic-compatible endpoint. If SDK behavior proves
incompatible with DeepSeek-specific constraints during smoke testing, the design
does not permit silently falling back to the CLI engine. Instead, this engine
must fail clearly and report the incompatibility.

The engine should not use deprecated DeepSeek model names as defaults.

### Claude Mutual Exclusion

The active Claude engine is a single enum, not two enabled flags.

Valid states:

```text
claude.enabled = false
claude.enabled = true, claude.engine = "cli-local"
claude.enabled = true, claude.engine = "sdk-deepseek"
```

Invalid states:

```text
claude.cli_local.enabled = true
claude.sdk_deepseek.enabled = true
```

The invalid shape should not exist in the final config schema.

## Provider: Antigravity

`AntigravitySdkProvider` is the primary Antigravity integration path.

The installed `agy` CLI is useful for local probing and human terminal use, but
it is not the provider boundary for WLCodex because it does not expose the same
stable structured event contract needed by the native web surface.

The provider should use the official Antigravity SDK as the backend integration:

- create or resume SDK sessions;
- stream agent events;
- map assistant text, reasoning, command output, file changes, and approvals
  into native-agent events;
- expose SDK policy and human-in-the-loop controls through provider
  capabilities;
- report SDK import/auth/config errors in `status()`.

Antigravity should not depend on the system-level `agy` binary for normal
operation.

## Data Model

Replace Codex-specific native session storage with generic native-agent session
storage.

New logical table:

```text
native_agent_sessions
```

Required fields:

```text
id
provider
provider_engine
native_session_id
agent_run_id
workspace_path
title
status
created_at
updated_at
last_event_cursor
metadata_json
```

Uniqueness:

```text
(provider, provider_engine, native_session_id)
```

`provider_engine` is required because Claude has mutually exclusive engines
whose native session ids may not be comparable.

The existing `native_codex_sessions` data should be migrated or read through a
compatibility adapter. The implementation plan can choose a conservative
two-step migration if that reduces risk.

## Runtime Events

Add normalized source identity for every provider:

```text
source = "codex"
source = "claude"
source = "antigravity"
```

Provider-specific provenance should be carried separately:

```text
source_kind = "codex_native"
source_kind = "claude_cli_local"
source_kind = "claude_sdk_deepseek"
source_kind = "antigravity_sdk"
```

The UI should render by normalized event kinds:

```text
lifecycle
text_delta
reasoning_delta
command_started
command_output
command_completed
file_changed
diff_updated
approval_requested
approval_resolved
completed
failed
```

It should not need to know which backend emitted the original event.

## Routes

New generic route family:

```text
GET  /native
GET  /native/{provider}
GET  /native/{provider}/login

GET  /api/native/{provider}/status
GET  /api/native/{provider}/capabilities
GET  /api/native/{provider}/sessions
POST /api/native/{provider}/sessions/start
GET  /api/native/{provider}/sessions/{native_session_id}
POST /api/native/{provider}/sessions/{native_session_id}/attach
POST /api/native/{provider}/sessions/{native_session_id}/continue
POST /api/native/{provider}/sessions/{native_session_id}/steer
POST /api/native/{provider}/sessions/{native_session_id}/interrupt
POST /api/native/{provider}/approvals/{request_id}/resolve
```

Compatibility route:

```text
/native/codex
/api/native/codex/...
```

The compatibility route should internally dispatch through the same provider
registry as every other provider.

## UI Model

The first screen should show native providers as selectable worker stations:

```text
Codex
Claude
Antigravity
```

Each station shows:

- connection status;
- active engine where relevant;
- last known session count;
- primary action to open the provider page.

Claude should show the active engine explicitly:

```text
Claude: local CLI
Claude: DeepSeek SDK
```

The session detail page should keep the same layout and interaction model as
the current Codex native page. Differences should come from capability state,
not from separate page implementations.

## Configuration

Suggested config shape:

```toml
[native_agents]
enabled = true
default_provider = "codex"

[native_agents.codex]
enabled = true

[native_agents.claude]
enabled = true
engine = "cli-local"

[native_agents.claude.cli_local]
binary = "auto"
model = ""
permission_mode = "acceptEdits"

[native_agents.claude.sdk_deepseek]
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"

[native_agents.antigravity]
enabled = true
engine = "sdk"
```

`model = ""` for the CLI engine means "do not pass `--model`". This preserves
local Claude Code or `ccswitch` provider routing.

## Error Handling

Provider errors should be normalized:

```text
disabled
binary_not_found
sdk_not_installed
missing_api_key
auth_failed
capability_unsupported
session_not_found
provider_timeout
provider_protocol_error
provider_runtime_error
```

Status endpoints should return both machine-readable codes and user-facing
messages.

Provider startup errors should not crash the whole WLCodex process unless the
config itself is invalid. A missing optional SDK should make only that provider
unavailable.

The Claude mutual-exclusion rule is config-invalid and should fail startup.

## Testing

Tests should cover:

- provider registry dispatch;
- Codex compatibility routes still hitting the Codex provider;
- generic route auth and ticket behavior;
- session store uniqueness by provider and engine;
- Claude engine config validation;
- Claude CLI model override behavior, including empty model meaning no
  `--model` argument;
- Claude SDK DeepSeek config validation for API key env, base URL, and model;
- Antigravity SDK missing dependency and missing auth status;
- event normalization from Codex, Claude CLI, Claude SDK, and Antigravity SDK
  sample events;
- UI capability rendering for unsupported actions.

Smoke tests should be separate from unit tests because Claude SDK DeepSeek and
Antigravity SDK require external credentials and network access.

## Out Of Scope

This design does not include:

- installing Antigravity or Claude system apps;
- implementing a custom model proxy;
- running both Claude engines at once;
- scraping GUI applications;
- replacing the existing Codex app-server bridge;
- exposing remote internet access for the native web page;
- committing provider API keys or user auth state into the project.

## Acceptance Criteria

The implementation is complete when:

- `/native/codex` keeps working through the generic provider registry;
- `/native/claude` works with exactly one configured Claude engine;
- `/native/antigravity` uses the SDK provider path, not `agy` CLI;
- all providers use the same session detail page and event rendering path;
- unsupported controls are disabled by provider capabilities;
- normalized runtime events include provider and provider-engine provenance;
- tests prove Claude CLI and Claude SDK DeepSeek are mutually exclusive.

## References

- Claude Code headless and `stream-json` CLI behavior:
  `https://code.claude.com/docs/en/headless`
- Claude Code authentication and local configuration:
  `https://code.claude.com/docs/en/authentication`
- Claude Agent SDK:
  `https://code.claude.com/docs/en/agent-sdk/overview`
- DeepSeek Anthropic-compatible API:
  `https://api-docs.deepseek.com/guides/anthropic_api`
- Antigravity SDK:
  `https://antigravity.google/docs/sdk-overview`
