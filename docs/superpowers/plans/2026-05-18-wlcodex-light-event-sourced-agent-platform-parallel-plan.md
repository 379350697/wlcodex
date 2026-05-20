# WLCodex Lightweight Event-Sourced Agent Platform Parallel Plan

> Superseded for product semantics by the 2026-05-20 Remote Workbench repair.
> Runtime events remain the fact source; task/agent_run names below are
> internal projection details, not user-facing Workbench concepts.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the personal-use event-sourced agent platform described in `docs/superpowers/specs/2026-05-18-wlcodex-light-event-sourced-agent-platform-design.md`.

**Architecture:** Add a single SQLite append-only runtime event log, normalize Codex and Claude activity into that log, project existing mutable tables from events, and render Telegram status from projections. The final platform keeps one local process and existing backends, but makes event replay the source of truth for run state.

**Tech Stack:** Python asyncio, SQLite, existing WLCodex ledger, existing Codex app-server backend, Claude Code CLI stream-json, Telegram interaction layer, pytest, GitNexus.

---

## Parallel Execution Contract

This plan is meant for concurrent workers in separate worktrees.

Rules:

- Each lane owns its write set. Do not edit files owned by another lane.
- Shared contract names come from the spec and must not be renamed by individual
  workers.
- If a lane needs shared behavior, it creates a new adapter file in its own
  write set and exposes a small function or class for the integration lane.
- The integration lane owns final wiring in controller/composition files.
- Merge order should put Lane A first because it defines the event store module,
  but lanes B-G can be developed simultaneously against the frozen contract in
  the spec.

## Lane Overview

| Lane | Scope | Primary write set | Can run in parallel? |
| --- | --- | --- | --- |
| A | Event envelope, store, append API | `runtime_events.py`, `runtime_event_store.py`, `db.py`, `models.py` | Yes, merge first |
| B | Claude runtime event source | `claude_stream_parser.py`, `claude_runtime_source.py`, `claude_backend.py`, Claude tests | Yes |
| C | Codex runtime event source | `codex_runtime_source.py`, `codex_backend.py`, Codex event tests | Yes |
| D | Projectors and replayed state | `runtime_projector.py`, `runtime_state.py`, projector tests | Yes |
| E | Telegram runtime rendering | `interaction/runtime_renderer.py`, interaction tests | Yes |
| F | Diagnostics and recovery views | `runtime_diagnostics.py`, `status.py`, `inspection.py`, recovery tests | Yes |
| G | Chief-engineer integration | `orchestration_runner.py`, `controller.py`, `main.py`, integration tests | Yes, merge late |

## Lane A: Event Envelope And Store

**Owned Files**

- Create: `wlcodex/runtime_events.py`
- Create: `wlcodex/runtime_event_store.py`
- Modify: `wlcodex/db.py`
- Modify: `wlcodex/models.py`
- Test: `tests/test_runtime_event_store.py`

**Responsibilities**

- Define the event envelope dataclass.
- Define event type constants or literals used by all lanes.
- Add the `runtime_events` table and indexes.
- Provide append and query APIs.
- Guarantee append-only behavior.
- Provide redaction and payload length caps at append time.

**Steps**

- [ ] Add tests for appending one event and reading it back by id.
- [ ] Add tests for querying by `correlation_id`.
- [ ] Add tests for querying by `agent_run_id`.
- [ ] Add tests that event payloads are copied, not mutated after append.
- [ ] Add tests that secret-looking payload keys are redacted.
- [ ] Add schema migration in `db.py`.
- [ ] Implement `RuntimeEvent` envelope in `runtime_events.py`.
- [ ] Implement `RuntimeEventStore.append()`.
- [ ] Implement `RuntimeEventStore.list_by_correlation()`.
- [ ] Implement `RuntimeEventStore.list_by_agent_run()`.
- [ ] Implement `RuntimeEventStore.list_recent_for_conversation()`.
- [ ] Run `pytest tests/test_runtime_event_store.py -q`.

**Acceptance**

- Appending an event does not require any existing task event.
- Reading by correlation returns events in id order.
- Redaction happens before payload JSON reaches SQLite.
- No lane outside A needs to know raw SQL for runtime events.

## Lane B: Claude Runtime Event Source

**Owned Files**

- Create: `wlcodex/claude_stream_parser.py`
- Create: `wlcodex/claude_runtime_source.py`
- Modify: `wlcodex/claude_backend.py`
- Test: `tests/test_claude_runtime_events.py`
- Test: `tests/test_claude_backend.py`

**Responsibilities**

- Parse Claude stream-json lines into normalized runtime events.
- Include hook events when supported.
- Separate visible text from activity/tool/usage events.
- Refresh idle activity on every valid Claude JSON line.
- Preserve existing `AgentStreamEvent` compatibility until integration removes
  callers that only understand text.

**Steps**

- [ ] Add parser tests for `stream_event` text delta.
- [ ] Add parser tests for assistant text completion.
- [ ] Add parser tests for assistant `tool_use` content blocks.
- [ ] Add parser tests for `system/api_retry`.
- [ ] Add parser tests for `result` usage.
- [ ] Add parser tests for hook lifecycle messages.
- [ ] Add parser tests for unsupported hook capability events.
- [ ] Implement pure parser functions in `claude_stream_parser.py`.
- [ ] Implement `ClaudeRuntimeSource` that wraps parser output with run ids and
      correlation ids.
- [ ] Update `ClaudeBackend._prompt_args()` to request
      `--include-hook-events` when stream-json is enabled.
- [ ] Ensure unsupported CLI flags degrade to a clear capability event instead
      of a silent failure.
- [ ] Run `pytest tests/test_claude_runtime_events.py tests/test_claude_backend.py -q`.

**Acceptance**

- A Claude run can emit `agent.run.activity` without emitting visible text.
- Tool use and API retry are observable in SQLite-ready event objects.
- Text intended for Codex verification is not polluted by progress templates.
- Existing direct Claude tests still pass.

## Lane C: Codex Runtime Event Source

**Owned Files**

- Create: `wlcodex/codex_runtime_source.py`
- Modify: `wlcodex/codex_backend.py`
- Test: `tests/test_codex_runtime_events.py`
- Test: `tests/test_codex_backend_events.py`

**Responsibilities**

- Map existing Codex JSON-RPC backend events into runtime events.
- Keep the existing `BackendEvent` fan-out behavior intact.
- Preserve current approval request behavior.
- Emit model/tool/command/file/usage events using the shared envelope contract.

**Steps**

- [ ] Add tests mapping `thread/started` to runtime activity.
- [ ] Add tests mapping `turn/started` and `turn/completed`.
- [ ] Add tests mapping command item start/complete.
- [ ] Add tests mapping command output deltas.
- [ ] Add tests mapping file change deltas.
- [ ] Add tests mapping `agent_message_delta`.
- [ ] Add tests mapping token usage.
- [ ] Add tests mapping approval requests.
- [ ] Implement `codex_runtime_source.py`.
- [ ] Add optional runtime-event callback emission in `codex_backend.py`.
- [ ] Prove existing `BackendEvent` subscribers still receive events.
- [ ] Run `pytest tests/test_codex_runtime_events.py tests/test_codex_backend_events.py -q`.

**Acceptance**

- Codex app-server events can feed both old `TaskService` and new runtime log.
- Approval semantics are unchanged.
- Command output continues to append to the task log through existing paths.
- Runtime event generation is pure enough to test without a live app-server.

## Lane D: Projectors And Replayed State

**Owned Files**

- Create: `wlcodex/runtime_state.py`
- Create: `wlcodex/runtime_projector.py`
- Test: `tests/test_runtime_projector.py`
- Test: `tests/test_runtime_state_replay.py`

**Responsibilities**

- Rebuild current run state from runtime events.
- Update compatibility projections such as `agent_runs`,
  `orchestration_runs`, `usage_events`, and `task_events`.
- Define read models used by Telegram and diagnostics.
- Never mutate the runtime event log.

**Steps**

- [ ] Add replay tests for agent run lifecycle.
- [ ] Add replay tests for orchestration run pass/fail/retry.
- [ ] Add replay tests for approval requested/resolved.
- [ ] Add replay tests for idle and hard timeout events.
- [ ] Add projection tests that `agent_runs.status` follows runtime events.
- [ ] Add projection tests that `usage_events` records token updates.
- [ ] Add projection tests that legacy `task_events` receives a compatible
      summary for important runtime events.
- [ ] Implement `RuntimeAgentState` and `RuntimeRunState`.
- [ ] Implement pure replay reducer functions.
- [ ] Implement `RuntimeProjector.apply(event)`.
- [ ] Implement projection rebuild from event id zero.
- [ ] Run `pytest tests/test_runtime_projector.py tests/test_runtime_state_replay.py -q`.

**Acceptance**

- State can be reconstructed from events alone.
- A terminal run state cannot be overwritten by later non-terminal activity.
- Projection failures do not delete or mutate events.
- Existing tables remain useful for current `/status` and tests.

## Lane E: Telegram Runtime Renderer

**Owned Files**

- Create: `wlcodex/interaction/runtime_renderer.py`
- Modify: `wlcodex/interaction/renderer.py`
- Modify: `wlcodex/interaction/events.py`
- Test: `tests/test_runtime_interaction_renderer.py`
- Test: `tests/test_event_bridge.py`

**Responsibilities**

- Render runtime projections into deterministic Telegram messages.
- Implement verbosity levels `0`, `1`, and `2`.
- Throttle edits and avoid `message is not modified` churn.
- Keep raw model text out of progress updates.
- Leave security approvals explicit and auditable.

**Steps**

- [ ] Add tests for verbosity 0 final-only behavior.
- [ ] Add tests for verbosity 1 milestone progress.
- [ ] Add tests for verbosity 2 tool and retry detail.
- [ ] Add tests that repeated activity updates are throttled.
- [ ] Add tests that raw Claude/Codex progress text is not blindly copied into
      Telegram progress messages.
- [ ] Add tests that final text is flushed once.
- [ ] Implement runtime rendering templates.
- [ ] Add interaction event type extensions needed by runtime projections.
- [ ] Wire renderer methods without changing Telegram transport resilience.
- [ ] Run `pytest tests/test_runtime_interaction_renderer.py tests/test_event_bridge.py -q`.

**Acceptance**

- Telegram can say what is happening without echoing raw model chatter.
- Long-running activity produces useful heartbeat updates.
- Approval cards remain clear and unchanged in decision semantics.
- Renderer can run from projected state and does not query provider internals.

## Lane F: Diagnostics And Recovery

**Owned Files**

- Create: `wlcodex/runtime_diagnostics.py`
- Modify: `wlcodex/status.py`
- Modify: `wlcodex/inspection.py`
- Modify: `wlcodex/watchdog.py`
- Modify: `wlcodex/recovery_notifications.py`
- Test: `tests/test_runtime_diagnostics.py`
- Test: `tests/test_recovery.py`

**Responsibilities**

- Provide `/status`, `/trace`, and SQLite-friendly runtime summaries.
- Append recovery events on startup.
- Mark orphaned in-flight runs through events.
- Explain timeout decisions with last event and elapsed clocks.

**Steps**

- [ ] Add diagnostic tests for active run summary.
- [ ] Add diagnostic tests for last N events trace.
- [ ] Add diagnostic tests for redacted event payload display.
- [ ] Add recovery tests for non-terminal agent run with missing process.
- [ ] Add recovery tests for projection rebuild event.
- [ ] Add timeout explanation tests.
- [ ] Implement `runtime_diagnostics.py`.
- [ ] Extend status formatting to include runtime state when present.
- [ ] Extend inspection to show runtime traces.
- [ ] Update watchdog to append timeout events through the runtime store.
- [ ] Update recovery notifications to report event-sourced recovery outcomes.
- [ ] Run `pytest tests/test_runtime_diagnostics.py tests/test_recovery.py -q`.

**Acceptance**

- `/status` can explain active agent, phase, last event, idle clock, and hard
  clock.
- `/trace` can show a useful sanitized timeline.
- Restart recovery is visible as events.
- Existing recovery tests still pass.

## Lane G: Chief-Engineer Integration

**Owned Files**

- Modify: `wlcodex/orchestration_runner.py`
- Modify: `wlcodex/controller.py`
- Modify: `wlcodex/main.py`
- Modify: `wlcodex/agent_backend.py`
- Test: `tests/test_orchestration_runner.py`
- Test: `tests/test_controller_flow.py`
- Test: `tests/test_telegram_handlers.py`
- Test: `tests/test_main_composition.py`

**Responsibilities**

- Create runtime correlation ids for every user request.
- Create Claude `agent_run` before launching Claude.
- Append run and agent lifecycle events throughout the chief-engineer loop.
- Feed projected runtime state into Telegram renderer.
- Keep Codex verification as the only pass gate.

**Steps**

- [ ] Add integration tests proving Claude `agent_run` exists with `running`
      status before the first Claude stream event completes.
- [ ] Add integration tests proving Claude activity events prevent idle timeout.
- [ ] Add integration tests proving hard timeout still terminates a run.
- [ ] Add integration tests proving verification `retry` cannot become
      `completed`.
- [ ] Add integration tests proving Telegram receives deterministic progress.
- [ ] Extend `AgentStreamEvent` or replace its usage with runtime event
      emission while preserving existing callers.
- [ ] Append `run.requested` and `run.started` from controller entrypoints.
- [ ] Append `run.phase.changed` for analysis, implementation, verification,
      retry, completion, and failure.
- [ ] Append Claude lifecycle events before and after `_call_claude_streaming`.
- [ ] Feed runtime projections into the interaction renderer.
- [ ] Run selected integration tests.

**Acceptance**

- The live chief-engineer loop is explainable from `runtime_events`.
- Claude cannot run invisibly.
- Telegram final answer is emitted only after Codex verification pass.
- Existing command paths still work.

## Cross-Lane Merge Checklist

- [ ] Lane A merged first.
- [ ] All lanes rebased on Lane A.
- [ ] No lane edits another lane's owned files.
- [ ] Run `git diff --check`.
- [ ] Run lane-specific tests.
- [ ] Run integration tests from Lane G.
- [ ] Run full test suite.
- [ ] Run GitNexus `detect_changes(scope="all")`.
- [ ] Confirm affected scope matches runtime events, adapters, projectors,
      interaction renderer, diagnostics, and orchestration integration.

## Final Verification Scenario

Use a real Telegram prompt that requires code changes and test execution.

Expected event timeline:

```text
user.message.received
run.requested
run.started
run.phase.changed(running_analysis)
agent.run.started(codex:analysis)
model.text.delta(...)
agent.run.completed(codex:analysis)
run.phase.changed(running_implementation)
agent.run.started(claude:implementation)
agent.run.activity(claude)
tool.call.started(...)
file.changed(...)
command.started(...)
command.completed(...)
agent.run.completed(claude:implementation)
run.phase.changed(running_verification)
agent.run.started(codex:verification)
verification.decision.recorded(pass)
run.completed
telegram.message.sent
```

Failure investigation must be possible with:

```text
/status
/trace
sqlite query by correlation_id
journalctl only after event trace points to process-level failure
```

That is the bar for the lightweight event-sourced platform.
