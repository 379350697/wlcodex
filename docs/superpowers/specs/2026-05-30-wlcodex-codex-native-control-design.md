# WLCodex Codex Native Control Design

## Status

Drafted on 2026-05-30 after the user clarified that the office view must
include a "Codex干活的" worker that can see and continue work started directly
inside the official Codex IDE/mobile native experience.

This is a business and architecture spec. It does not authorize product code
changes by itself.

## Project Identity

WLCodex repository:

```text
https://github.com/379350697/wlcodex
```

This spec assumes the current WLCodex codebase in that repository. WLCodex
already contains:

- a Codex app-server backend using JSON-RPC;
- runtime event projection through `runtime_events`;
- a Worker Live Stream slice that streams events by `agent_run_id`;
- Telegram/Product/Terminal surfaces;
- role-oriented engineering office direction.

## Background

The current Worker Live Stream implementation is correct for WLCodex-owned
work, but it has a hard source boundary:

```text
WLCodex-created task
  -> WLCodex Codex/Claude backend
      -> WLCodex runtime_events
          -> worker live stream page
```

Work started directly in the official Codex IDE or official Codex mobile remote
does not pass through that path. Therefore it is invisible to WLCodex unless
WLCodex connects to the same official Codex app-server/session layer.

The user specifically wants the office to include a worker named
`Codex干活的`. That worker is not a role-played WLCodex employee. It is the
real official Codex IDE working state, shown inside WLCodex as one clickable
office station.

## Native Codex Control Requirement

The target experience is not passive observation. The first version must allow
phone-side control of official Codex IDE sessions:

- list official Codex sessions;
- open one official Codex session from the WLCodex phone page;
- load the session's history;
- subscribe to live events for that session;
- continue the official Codex thread with a new user message;
- steer an active turn when the official Codex protocol permits same-turn
  steering;
- resolve command, file-change, and permission approvals;
- interrupt a running turn.

From the user's perspective, clicking `Codex干活的` should feel like entering
the official Codex mobile remote stream for that session, with WLCodex adding
only the office navigation and management frame around it.

## Local Findings

On the user's Mac, official Codex is running as an Electron app plus app-server
processes:

```text
/Applications/Codex.app/Contents/MacOS/Codex
/Applications/Codex.app/Contents/Resources/codex app-server --analytics-default-enabled
/Applications/Codex.app/Contents/Resources/codex app-server --listen stdio://
```

The official Codex CLI exposes app-server controls:

```text
codex app-server daemon start
codex app-server daemon enable-remote-control
codex app-server daemon disable-remote-control
codex app-server daemon version
codex app-server proxy
codex app-server generate-json-schema --experimental
```

The generated protocol schema includes the operations WLCodex needs:

```text
thread/list
thread/loaded/list
thread/read
thread/resume
thread/turns/list
thread/turns/items/list
turn/start
turn/steer
turn/interrupt
thread/archive
thread/unarchive
remoteControl/status/read
remoteControl/enable
remoteControl/disable
```

It also includes live notifications:

```text
thread/started
thread/status/changed
turn/started
turn/completed
turn/diff/updated
turn/plan/updated
item/started
item/completed
item/agentMessage/delta
item/commandExecution/outputDelta
item/fileChange/outputDelta
item/fileChange/patchUpdated
item/reasoning/textDelta
item/reasoning/summaryTextDelta
item/tool/requestUserInput
```

And approval server requests:

```text
item/commandExecution/requestApproval
item/fileChange/requestApproval
item/permissions/requestApproval
execCommandApproval
applyPatchApproval
```

This makes protocol-level control the correct integration path. WLCodex should
not scrape the official app UI, simulate clicks, or infer state from screenshots.

## Product Decision

Build **Codex Native Control Bridge**.

The bridge connects WLCodex to the official Codex app-server/daemon session
source and projects those official sessions into the WLCodex office as a fixed
worker station:

```text
Official Codex IDE / mobile native session
  -> official app-server daemon or proxy transport
      -> CodexNativeControlBridge
          -> WLCodex runtime_events + session index
              -> "Codex干活的" office station
                  -> live stream + controls on phone
```

The bridge must preserve the source identity. Events from official Codex native
sessions are not WLCodex-assigned tasks. They are imported/control-proxied
external native sessions.

## User-Facing Model

The office overview gains a fixed worker:

```text
Name: Codex干活的
Role: 官方 Codex IDE 现场
Source: codex_native
Status: connected / disconnected / needs remote control / error
Action: Open
```

Opening it shows:

- official Codex daemon connection status;
- official recent sessions filtered by workspace when possible;
- session title, cwd, model/provider, created/updated time, source kind;
- live status for loaded sessions;
- a session detail/live page.

Session detail page includes:

- event stream;
- turn history;
- diffs and command output as structured blocks;
- approval cards;
- input box for "continue session";
- active-turn steering box when a running turn exists;
- interrupt button for running turns.

The UI must clearly mark:

```text
来源：官方 Codex IDE
```

so the user understands that WLCodex is controlling a native Codex session, not
running a WLCodex-assigned office task.

## Architecture

### 1. Native Transport

Add a transport that can connect to official Codex app-server through the
supported local control path.

Preferred order:

1. `codex app-server proxy` to the official daemon control socket.
2. An explicitly configured `unix://` socket path if the user provides one.
3. A managed loopback `ws://127.0.0.1:<port>` app-server only for WLCodex-owned
   work, not as the primary native IDE bridge.

The native bridge should not kill or restart official Codex IDE processes. It
may report that remote control is disabled or unavailable and expose an operator
action to enable it.

### 2. Native Client

Add `CodexNativeClient` as a protocol-focused client over JSON-RPC:

- initialize the app-server protocol;
- read remote-control status;
- list recent sessions;
- read a session with turns/items;
- resume a session;
- start a new turn;
- steer active turn;
- interrupt active turn;
- resolve held approval requests;
- receive and expose notifications.

This client should reuse the existing `JsonRpcClient` behavior where possible,
including held server requests for approvals.

### 3. Session Projection

Add a small native-session registry in WLCodex:

```text
native_session_id = official Codex thread id
wlcodex_agent_run_id = synthetic WLCodex row/id for streaming
source = codex_native
worker_label = Codex干活的
```

The mapping lets existing Worker Live Stream continue to stream by
`agent_run_id`, while the control bridge retains the official `threadId` and
`turnId` needed for app-server control.

The first implementation adds a dedicated `native_codex_sessions` SQLite table
with a uniqueness constraint on official `thread_id`. This avoids overloading
WLCodex-owned task semantics while still linking each native session to a
synthetic WLCodex `agent_run_id` for live streaming.

### 4. Event Mapping

Official Codex notifications are converted into WLCodex runtime events with:

```text
source = codex
actor = codex_native
payload.source_kind = codex_native
payload.native_thread_id = <official thread id>
payload.native_turn_id = <official turn id, when present>
```

The mapping should extend the existing Codex runtime event mapper rather than
creating a parallel event vocabulary. The live stream UI should receive the same
stream kinds already defined for WLCodex-owned Codex runs:

- lifecycle;
- activity;
- text_delta;
- reasoning_delta;
- command_started;
- command_output;
- command_completed;
- file_changed;
- diff_updated;
- approval_requested;
- approval_resolved;
- completed;
- failed.

### 5. Control Actions

Expose phone-safe HTTP endpoints for native control:

```text
GET  /native/codex
GET  /api/native/codex/status
GET  /api/native/codex/sessions
GET  /api/native/codex/sessions/{native_thread_id}
POST /api/native/codex/sessions/{native_thread_id}/continue
POST /api/native/codex/sessions/{native_thread_id}/steer
POST /api/native/codex/sessions/{native_thread_id}/interrupt
POST /api/native/codex/approvals/{codex_request_id}/resolve
```

For session live streaming, keep the existing worker route:

```text
GET /workers/{agent_run_id}/live
GET /api/workers/{agent_run_id}/stream
```

The native session detail endpoint should return the mapped `agent_run_id` so
the frontend can open the same live stream channel.

### 6. Authentication

This bridge can control the user's Mac. It must not be exposed as an unauthenticated
public page.

Required for any non-loopback access:

- bearer token or password-style shared secret;
- constant-time comparison;
- authentication on HTML, JSON, SSE, and POST endpoints;
- no token in event payloads;
- no token in logs;
- deny requests before touching the native Codex client.

Loopback-only unauthenticated development mode is acceptable only when
`host` is `127.0.0.1` or `localhost`.

### 7. Networking

Cloudflare tunnel or LAN exposure may be used for phone testing, but only after
authentication is enabled.

Direct public binding without auth is explicitly out of scope for the control
bridge.

### 8. Failure Modes

The UI should render actionable states:

- official Codex app-server daemon unavailable;
- remote control disabled;
- proxy socket missing;
- initialization failed;
- session list failed;
- session disappeared or archived;
- active turn not steerable;
- approval request already resolved;
- native protocol version unsupported.

Failures should be normal live stream events where useful, but control errors
must also return clear JSON error responses.

## Scope

### In Scope For First Implementation

- Native Codex app-server/proxy transport.
- Native session list/read/resume/continue.
- Native active-turn steer when `turnId` is known.
- Native turn interrupt.
- Native approval resolution.
- Native event projection into WLCodex `runtime_events`.
- `Codex干活的` fixed worker entry.
- Authenticated local web endpoints for phone testing.
- Tests for session mapping, control calls, event projection, auth, and SSE.

### Out Of Scope For First Implementation

- Pixel-perfect clone of the official Codex mobile UI.
- Voice/realtime audio features.
- WebRTC session control.
- Multi-user access control beyond one shared local token.
- Editing official Codex's Electron UI state.
- Running official Codex through screenshots or UI automation.
- Full Virtual Engineering Office orchestration.
- Antigravity integration.

## Success Criteria

The feature is successful when:

1. WLCodex shows `Codex干活的` in the office/native entry surface.
2. The user can open the entry from a phone browser.
3. WLCodex lists official Codex sessions from the native app-server source.
4. The user can open an official session and see its historical turns/items.
5. The user can continue an official Codex session from the phone.
6. New official Codex notifications appear in WLCodex live stream without a
   second model call.
7. The user can approve/deny native Codex approval requests from the phone.
8. The endpoint is authenticated before any non-loopback exposure.

## Key Risks

1. Official app-server protocol stability:
   The schema includes experimental fields. WLCodex should keep the native
   bridge isolated so future protocol changes do not destabilize WLCodex-owned
   task execution.

2. Session ownership:
   WLCodex must not accidentally mutate all official sessions. Control actions
   require an explicit selected `native_thread_id`.

3. Security:
   This bridge can approve commands and file writes on the user's machine.
   Unauthenticated public exposure is unacceptable.

4. Duplicate event import:
   Reconnecting to a native session can replay history. Event projection must
   be cursor/idempotent enough to avoid repeated live output.

5. Active-turn steering:
   The official protocol may reject `turn/steer` for turns that cannot accept
   same-turn steering. WLCodex should surface that as "cannot steer this turn"
   and still allow normal `continue`.

## Recommended Build Order

1. Add native transport and native client with fake transport tests.
2. Add session list/read methods and native session mapping.
3. Add native event projection into runtime events.
4. Add authenticated HTTP endpoints.
5. Add the `Codex干活的` page and session selector.
6. Add continue/steer/interrupt/approval controls.
7. Test locally on loopback, then behind an authenticated tunnel.
