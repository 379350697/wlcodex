# WLCodex Codex App-Server Backend Implementation Plan

Goal: implement the real `AppServerCodexBackend` behind the existing `CodexBackend` protocol without changing Telegram task isolation rules.

## Protocol Inputs

- WebSocket command: `codex app-server --listen ws://127.0.0.1:17431`
- Schema source: `runtime/protocol`
- New thread request: `thread/start`
- Explicit resume request: `thread/resume`
- Turn start request: `turn/start`
- Steering request: `turn/steer`
- Thread events: `thread/started`, `thread/status/changed`, `thread/closed`
- Turn events: `turn/started`, `turn/completed`, `turn/diff/updated`, `turn/plan/updated`
- Item events: `item/started`, `item/completed`, `item/agentMessage/delta`, `item/commandExecution/outputDelta`, `item/fileChange/outputDelta`
- Approval requests: `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`
- Approval resolution: send a JSON-RPC response with the same request `id`; command/file responses use `{"decision": "accept" | "acceptForSession" | "decline" | "cancel"}`, permissions responses use `{"permissions": ..., "scope": "turn" | "session"}`

## Tasks

- [ ] Add focused tests for JSON-RPC request id generation and response dispatch.
- [ ] Implement an app-server process manager that starts `codex app-server --listen ws://127.0.0.1:17431` and shuts it down cleanly.
- [ ] Implement a WebSocket JSON-RPC client with request/response correlation.
- [ ] Implement `create_thread(workspace_path)` using `thread/start` with `cwd`, configured `approvalPolicy`, and configured sandbox.
- [ ] Implement `start_turn(thread_id, prompt)` using `turn/start` with a single text user input item.
- [ ] Implement `continue_turn(thread_id, prompt)` using `thread/resume` followed by `turn/start`.
- [ ] Translate server notifications into `BackendEvent` values for local ledger/status rendering only.
- [ ] Translate approval server requests into pending approval events and expose an explicit approval resolution method.
- [ ] Keep `thread/shellCommand` unsupported in WLCodex V1.
- [ ] Add an integration test gated by `WLCODEX_RUN_CODEX_INTEGRATION=1`.

## Integration Test

The integration test must:

- start the app server on loopback only
- create a temporary workspace
- create a new thread
- start one turn with this harmless prompt:

```text
Reply with exactly: wlcodex integration ok
```

- assert the assistant response contains exactly `wlcodex integration ok`
- skip unless `WLCODEX_RUN_CODEX_INTEGRATION=1`
