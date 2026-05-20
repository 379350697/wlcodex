# WLCodex Event-Sourced Conversation State Machine And Telegram Reliability Implementation Plan

> Superseded for product semantics by the 2026-05-20 Remote Workbench repair.
> Runtime events remain the fact source; "new task" and queue wording below is
> historical and must not appear in normal Workbench user paths. Natural
> language like "新任务", "另起一个", or "重新开始" no longer creates a new
> Workbench; only `/new` does.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development` to execute this plan, and use `superpowers:test-driven-development` for every behavior change. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the event-sourced conversation state machine and Telegram reliability behavior described in `docs/superpowers/specs/2026-05-19-wlcodex-event-sourced-conversation-state-machine-and-telegram-reliability-design.md`.

**Architecture:** Route Telegram messages through a replayable conversation state machine backed by `runtime_events`; keep existing task/run tables as projections; isolate Telegram network delivery through an outbox; preserve the Codex -> Claude -> Codex -> Telegram chief workflow.

**Tech Stack:** Python asyncio, SQLite, existing WLCodex runtime event store/projector, python-telegram-bot long polling, pytest, GitNexus.

---

## Execution Rules

- Do not implement `/recent`.
- Do not change unrelated lanes or cleanup unrelated dirty files.
- Before editing any function, class, or method, run GitNexus impact analysis
  for that symbol and report the risk.
- If GitNexus reports HIGH or CRITICAL risk, state the blast radius before
  editing.
- Keep `runtime_events` as the fact source. Old tables are projections.
- Do not allow Telegram or Claude to bypass Codex verification for
  code-producing default workflow runs.
- Before commit, run targeted tests, `git diff --check`, and
  GitNexus `detect_changes`.

## Phase 1: Contract Tests For Conversation Routing

**Write set**

- Tests only at first:
  - `tests/test_conversation_state_machine.py`
  - `tests/test_conversation_router.py`
  - `tests/test_telegram_followup_routing.py`

**Steps**

- [ ] Add a replay test: one chat with no active conversation receives normal
      text and produces `conversation.started`, `conversation.activated`,
      `conversation.message.routed`, and `user.message.received`.
- [ ] Add a replay test: a second normal text while state is `analysis` appends
      `user.context.appended` and does not create another task or conversation.
- [ ] Add a replay test: a second normal text while state is
      `waiting_approval` appends context and supersedes the pending approval.
- [ ] Add a replay test: a second normal text while state is `needs_user`
      appends context and resumes Codex analysis.
- [ ] Add a replay test: a follow-up during `implementation` records pending
      context and requires phase-boundary Codex review.
- [ ] Add a replay test: a follow-up during `verification` records pending
      context and requires Codex decision before final reply.
- [ ] Add a replay test: a natural message after `passed`, `failed`,
      `aborted`, or `done` creates a new conversation.
- [ ] Add route tests for `/new`, `新任务`, `另起一个`, and `重新开始`.
- [ ] Add route tests proving `/status`, `/trace`, `/health`, `/diff`, and
      similar diagnostics do not create work tasks.

**Acceptance**

- Tests fail against the current task-per-message behavior.
- Every expected behavior is expressed in runtime events, not only legacy task
  rows.

## Phase 2: Runtime Event Contract And Reducer

**Likely write set**

- `wlcodex/runtime_events.py`
- `wlcodex/runtime_state.py`
- `wlcodex/runtime_projector.py`
- `tests/test_runtime_state_replay.py`
- `tests/test_runtime_projector.py`

**GitNexus required before edits**

- Run impact for `RuntimeEvent`, `RuntimeProjector`, and any reducer functions
  that are modified or introduced.

**Steps**

- [ ] Add conversation event constants:
      `conversation.started`, `conversation.activated`,
      `conversation.state.changed`, `conversation.closed`,
      `conversation.intent.classified`, `conversation.message.routed`,
      `user.context.appended`, `conversation.pending_context.recorded`, and
      `conversation.pending_context.reviewed`.
- [ ] Add workspace busy event constants:
      `workspace.busy.detected`, `workspace.busy.user_choice.requested`,
      `workspace.busy.user_choice.recorded`, and `run.queued`.
- [ ] Add Telegram delivery and poller event constants from the spec.
- [ ] Implement a pure conversation reducer that reconstructs active
      conversation per chat from runtime events.
- [ ] Enforce one active non-terminal conversation per Telegram chat in the
      reducer/read model.
- [ ] Ensure terminal states are final for routing.
- [ ] Project conversation events into existing compatibility tables only after
      event append.
- [ ] Ensure `run.completed` still requires a prior pass verification decision.

**Acceptance**

- Replay alone can answer: active conversation id, conversation state, pending
  appended context, stale approval status, and whether a new message should
  append or start new.
- Projection writes are caused by events.

## Phase 3: Telegram Conversation Router

**Likely write set**

- New or existing router module for Telegram text routing.
- `wlcodex/controller.py`
- `wlcodex/telegram_app.py`
- `tests/test_conversation_router.py`
- `tests/test_telegram_followup_routing.py`

**GitNexus required before edits**

- Run impact for Telegram message handlers, controller entry points, and any
  existing route parser methods being changed.

**Steps**

- [ ] Route every inbound Telegram text through the conversation state machine.
- [ ] Append `user.message.received` before deciding route.
- [ ] Append `conversation.intent.classified` for `/new`, explicit new-task
      phrases, diagnostic commands, and normal text.
- [ ] Implement append-default behavior for non-terminal active conversations.
- [ ] Make `/new` and explicit start-over phrases create a new conversation.
- [ ] Keep diagnostic commands as reads; they must not create tasks or
      orchestration runs.
- [ ] Return the implementation/verification acknowledgement through Telegram
      outbox when follow-up context is recorded mid-run.
- [ ] Do not inject mid-implementation user context into Claude. Store it for
      Codex phase-boundary review.

**Acceptance**

- A multi-message user request remains one conversation unless an explicit
  new-conversation trigger is present.
- Follow-up messages no longer collide with the workspace lock by default.
- A Telegram user always gets a visible acknowledgement when their follow-up is
  recorded mid-run.

## Phase 4: Approval Supersession And Stale Buttons

**Likely write set**

- Approval handling code in controller/router modules.
- `wlcodex/runtime_projector.py`
- Approval-related tests.

**GitNexus required before edits**

- Run impact for approval request and approval resolution handlers before
  editing.

**Steps**

- [ ] When context arrives in `waiting_approval`, append
      `approval.superseded` with reason `user_context_appended`.
- [ ] Return the conversation to `analysis`.
- [ ] Ensure old approval buttons append `approval.stale_button.ignored` and
      do not approve the superseded plan.
- [ ] Ensure callback answer/edit failure is recorded separately and cannot
      undo the approval decision.
- [ ] Update Telegram copy so superseded approvals are clear to the user.

**Acceptance**

- A user can clarify an incomplete request after an approval prompt.
- Old approval buttons are harmless and auditable.
- Approval summaries remain populated; empty approval summaries are a regression.

## Phase 5: Workspace Busy User Choice

**Likely write set**

- `wlcodex/controller.py`
- `wlcodex/telegram_app.py`
- Any workspace lock or queue module currently raising busy errors.
- `tests/test_workspace_busy_replies.py`

**GitNexus required before edits**

- Run impact for workspace reservation/lock functions and controller busy
  handling before editing.

**Steps**

- [ ] Add tests where a new-work trigger arrives while another conversation
      owns the workspace.
- [ ] Convert busy exceptions into `workspace.busy.detected` and
      `workspace.busy.user_choice.requested`.
- [ ] Reply with blocking task/conversation/run details.
- [ ] Add inline buttons: append to current task, queue new task, cancel.
- [ ] Implement button callbacks as event appends:
      `user.context.appended`, `run.queued`, or
      `workspace.busy.user_choice.recorded`.
- [ ] Ensure no busy path can return without a Telegram delivery request.

**Acceptance**

- Workspace busy is visible, actionable, and replayable.
- The user can append, queue, or cancel without sending another ambiguous text
  message.

## Phase 6: Telegram Delivery Outbox

**Likely write set**

- New outbox module such as `wlcodex/telegram_outbox.py`.
- `wlcodex/telegram_app.py`
- `wlcodex/runtime_events.py`
- `wlcodex/runtime_projector.py`
- `tests/test_telegram_outbox.py`
- `tests/test_delivery_isolation.py`

**GitNexus required before edits**

- Run impact for `send_telegram`, `edit_telegram`, callback handlers, and any
  delivery isolation helpers before editing.

**Steps**

- [ ] Add tests proving every send/edit first appends
      `telegram.delivery.enqueued`.
- [ ] Add tests for successful send -> `telegram.message.sent`.
- [ ] Add tests for successful edit -> `telegram.message.edited`.
- [ ] Add tests for retryable failures -> `telegram.message.failed` and
      `telegram.outbox.retry_scheduled`.
- [ ] Add tests for retry budget exhausted -> `telegram.outbox.gave_up`.
- [ ] Add tests proving callback answer failure only appends
      `telegram.callback.answer.failed` and does not fail approval.
- [ ] Add idempotency keys so delivery replay/retry does not spam messages.
- [ ] Move direct Bot API calls behind the outbox worker.
- [ ] Bound and redact delivery payloads before appending events.

**Acceptance**

- Telegram network problems do not break the chief workflow, approval
  resolution, or projections.
- Delivery success/failure is explainable from `runtime_events`.

## Phase 7: Long Polling Resilience

**Likely write set**

- `wlcodex/telegram_app.py`
- `wlcodex/main.py`
- New poller watchdog helper if needed.
- `tests/test_telegram_polling_resilience.py`

**GitNexus required before edits**

- Run impact for bot startup/composition functions before editing.

**Steps**

- [ ] Configure long polling with explicit connect, read, write, and pool
      timeouts.
- [ ] Configure infinite bootstrap retry for polling startup.
- [ ] Register a global Telegram error handler.
- [ ] Append `telegram.poller.bootstrap.*` events.
- [ ] Append `telegram.poller.error` and `telegram.poller.recovered` events.
- [ ] Add a poller watchdog that records `telegram.poller.watchdog_timeout`
      when polling stops making progress.
- [ ] Ensure watchdog diagnostics do not mutate conversation state directly.

**Acceptance**

- Startup network failures retry instead of silently exiting.
- Runtime polling errors are observable.
- Poller recovery is represented in events and diagnostics.

## Phase 8: Streaming Edit Coalescing

**Likely write set**

- Telegram renderer/interactions modules.
- `wlcodex/telegram_app.py`
- Runtime renderer tests.

**GitNexus required before edits**

- Run impact for streaming render/edit functions before editing.

**Steps**

- [ ] Add tests that routine progress edits are throttled to a 2 to 5 second
      interval.
- [ ] Add tests that phase changes, approval requests, failures, and final
      results bypass routine throttling as milestone edits.
- [ ] Add tests that `message is not modified` becomes
      `telegram.edit.skipped_no_change` or a no-op diagnostic, not a workflow
      failure.
- [ ] Ensure raw Claude deltas are not sent as the final chief-workflow user
      response before Codex verification.

**Acceptance**

- Streaming no longer pounds Telegram with every model delta.
- Important state changes still reach the user promptly.

## Phase 9: Main Composition And Recovery

**Likely write set**

- `wlcodex/main.py`
- `wlcodex/runtime_projector.py`
- Recovery/inspection modules.
- `tests/test_main_composition.py`
- Recovery tests.

**GitNexus required before edits**

- Run impact for main composition functions, recovery functions, and projector
  rebuild functions before editing.

**Steps**

- [ ] Wire the conversation reducer, projector, Telegram router, outbox, and
      poller watchdog in production composition.
- [ ] Ensure recovery appends events before mutating compatibility projections.
- [ ] Rebuild active conversation state from events on startup.
- [ ] Detect impossible multiple active conversations for one chat and emit
      diagnostics with deterministic repair events.
- [ ] Ensure stale active runs after restart are explained through recovery
      events.

**Acceptance**

- Production startup uses the same runtime event path as tests.
- Restart state is explainable from events, not silent legacy table mutation.

## Phase 10: End-To-End Verification

**Test commands**

- [ ] Run conversation/router tests:
      `pytest tests/test_conversation_state_machine.py tests/test_conversation_router.py tests/test_telegram_followup_routing.py -q`
- [ ] Run approval and workspace busy tests:
      `pytest tests/test_workspace_busy_replies.py tests/test_approval*.py -q`
- [ ] Run Telegram reliability tests:
      `pytest tests/test_telegram_outbox.py tests/test_telegram_polling_resilience.py tests/test_delivery_isolation.py -q`
- [ ] Run runtime projection tests:
      `pytest tests/test_runtime_projector.py tests/test_runtime_state_replay.py -q`
- [ ] Run main composition tests:
      `pytest tests/test_main_composition.py -q`
- [ ] Run the existing stable core subset used by the platform work.
- [ ] Run `git diff --check`.
- [ ] Run GitNexus `detect_changes`.

**Human smoke**

- [ ] Send an incomplete code-task request.
- [ ] Send a clarification while Codex is still analyzing.
- [ ] Approve a command.
- [ ] Send a follow-up while Claude is implementing.
- [ ] Confirm the bot replies that context was recorded for Codex phase-boundary
      review.
- [ ] Confirm no second task is spawned.
- [ ] Confirm `/status` and `/trace` show the same conversation.
- [ ] Confirm Codex verification considers pending context before final reply.
- [ ] Clean up the temporary files in the same conversation, or use `/new` to
      start a separate cleanup conversation intentionally.

**Final acceptance**

- One Telegram chat has one active non-terminal conversation.
- Normal follow-ups append by default.
- New work starts only through `/new`, explicit start-over phrasing, no active
  conversation, or terminal current conversation.
- Workspace busy is visible and actionable.
- Telegram delivery failures are isolated and evented.
- The chief workflow remains Codex design, Claude implementation, Codex
  verification, Telegram reply.
- No `/recent` command or tests are added.
