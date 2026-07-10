# WLCodex Telegram Readable Streaming Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

## Status

Approved direction from user on 2026-05-21:

1. Running work maintains one status / preview bubble.
2. Body text is not delivered token-by-token; it is delivered by semantic blocks.
3. Task completion sends a final organized answer.
4. Terminal onsite and product cockpit use different output policies.

This spec is intentionally narrow. It does not change agent routing, busy-choice semantics, workspace leases, `/codex`, `/claude`, or `/auto` execution behavior.

## Problem

The current live Telegram output can split one assistant answer into many tiny messages:

- short fragments such as `我`;
- broken Chinese sentences;
- Markdown links split across messages;
- list items split midway;
- final answer mixed with ongoing tool/search output.

The observed root cause is not Telegram transport loss. The local renderer falls back from editable preview to append-only message sends when the outbox returns the placeholder id `-1` instead of a real Telegram `message_id`. Once that happens, every flush becomes another `sendMessage`.

Telegram is a chat surface, not a terminal framebuffer. The UX target is therefore:

- visible progress while a task runs;
- readable answers when content appears;
- no token-fragment spam;
- no loss of interrupt / append / queue / new-session controls.

## External Reference Pattern

TeleCodex, CodexClaw, and OpenClaw all converge on the same pattern:

- use `sendMessage` plus throttled `editMessageText` for a live preview;
- chunk long output to Telegram-safe sizes;
- avoid token-delta messages as permanent chat history;
- keep tool progress, reasoning, and final answer distinct;
- keep sessions scoped so parallel or interrupted work does not share one output slot.

References:

- TeleCodex: https://github.com/benedict2310/telecodex
- CodexClaw: https://github.com/MackDing/CodexClaw
- OpenClaw streaming: https://xlongxia.mintlify.app/concepts/streaming
- OpenClaw Telegram channel docs: https://github.com/openclaw/openclaw/blob/main/docs/channels/telegram.md

## Goals

### G1. One status / preview bubble per active run

Each active run has one editable status message keyed by:

```text
(chat_id, conversation_id, run_id)
```

Where `run_id` is the hidden Codex task id when present, otherwise the Claude agent run id, otherwise a generated interaction id.

The status bubble is used for:

- run started;
- phase progress;
- tool / web search / command status;
- heartbeat while the agent is active;
- completion, failure, or interruption status.

The status bubble must not be used for final answer body text in product mode.

### G2. Body text is semantic, not token-level

Assistant body text is buffered and emitted only at semantic boundaries:

Priority order:

1. paragraph boundary;
2. Markdown list item boundary;
3. sentence boundary;
4. whitespace;
5. hard split only when unavoidable.

Chunking must respect:

- Telegram text limit, using a default safe cap of `3900` characters;
- Markdown links, never splitting inside `[label](url)` when avoidable;
- fenced code blocks, never splitting inside fences when avoidable;
- if a hard split inside a code fence is unavoidable, close and reopen the fence so each Telegram message remains readable.

### G3. Final answer is complete and organized

On `run_completed`, the renderer sends the completed assistant answer as normal Telegram message(s).

Rules:

- if final answer fits in one Telegram-safe chunk, send one message;
- if too long, split by semantic chunking and prefix chunks as `1/N`, `2/N`, etc.;
- completion buttons attach to the last final chunk;
- final text is assembled from the full body buffer, not from the last streamed fragment;
- no additional model call is required for the final answer.

### G4. Product cockpit and terminal onsite differ intentionally

Product cockpit:

- running: one editable status bubble only;
- body: buffered silently during the run;
- completed: final organized answer;
- errors: status bubble becomes failure summary, plus actionable error text when needed.

Terminal onsite:

- running: one editable status bubble;
- body: semantic block streaming is allowed;
- completed: remaining buffered body is flushed, then final buttons / terminal controls are shown;
- raw terminal / token-like streaming is opt-in only and out of scope for this implementation.

### G5. Busy choices and mid-run input remain untouched

The output renderer must not change the input control model.

These remain exactly as designed:

- 发给当前 Codex / Claude;
- 打断并执行这句;
- 排队稍后;
- 新开隔离现场;
- 先不处理.

When a user interrupts:

- the old run status bubble is edited to `已打断`;
- old body buffering stops;
- the new run receives a new render session key.

When a user appends to current:

- no new output session is created;
- the current run continues using its existing status bubble and body buffer;
- the user gets a confirmation message only once.

When a user queues:

- the current render session continues;
- the queued run starts its own render session later.

When a user opens a new isolated session:

- no output from the existing run is mixed into the new session;
- render keys differ by conversation id and run id.

## Non-Goals

- Do not rewrite orchestration.
- Do not change `/codex`, `/claude`, or `/auto` task routing.
- Do not introduce another LLM call to summarize final answers.
- Do not expose raw reasoning by default.
- Do not implement native Telegram `sendMessageDraft` in this phase.
- Do not remove the Telegram outbox reliability layer.

## Architecture

### Components

#### `TelegramOutbox`

Add a delivery result path so preview send operations can obtain the real Telegram `message_id`.

Required behavior:

- `enqueue_send_wait(...)` or equivalent async wrapper returns the delivered `message_id`;
- it records the same delivery events as existing `enqueue_send`;
- it has a timeout and safe failure behavior;
- it does not block orchestration forever;
- existing fire-and-forget `send_telegram` behavior remains available for ordinary messages.

#### `SemanticChunker`

New pure module responsible for safe text buffering and splitting.

Responsibilities:

- accept text deltas;
- determine if a semantic chunk is ready;
- flush all buffered text on completion;
- protect Markdown links and fenced code blocks;
- return chunks that are safe for Telegram message size.

#### `TelegramOutputSession`

New stateful render-session object keyed by run.

Responsibilities:

- own the status bubble message id;
- own the body buffer;
- choose product or terminal policy;
- edit status bubble through a transport that can resolve preview message ids;
- send final answer chunks through existing reliable send path;
- mark itself completed, failed, or interrupted.

#### `InteractionRenderer`

Keep the current event-facing contract, but delegate output behavior to `TelegramOutputSession`.

Responsibilities:

- create / find output session by `(chat_id, conversation_id, task_id or agent_run_id)`;
- resolve surface mode through a callable supplied by `telegram_app`;
- route `text_delta`, `runtime_progress`, `run_completed`, and `run_failed`;
- clean sessions after terminal state.

#### `RuntimeProgressManager`

Either reuse its status text templates inside `TelegramOutputSession`, or make it share the same preview transport. It must no longer create repeated status messages when outbox returns `-1`.

## Data Flow

### Product cockpit

```text
run_started
  -> create OutputSession(product)
  -> send/edit status bubble: "Codex 正在处理..."

text_delta
  -> append to body buffer only

runtime_progress
  -> edit same status bubble

run_completed
  -> edit status bubble to "运行完成"
  -> semantic split full body
  -> send final chunk(s), buttons on last chunk
  -> close OutputSession
```

### Terminal onsite

```text
run_started
  -> create OutputSession(terminal)
  -> send/edit status bubble

text_delta
  -> append to body buffer
  -> if semantic chunk ready, send block chunk

runtime_progress
  -> edit same status bubble

run_completed
  -> flush remaining body
  -> edit status bubble to "运行完成"
  -> send final controls if needed
  -> close OutputSession
```

### Interrupt

```text
busy_interrupt selected
  -> abort active execution
  -> renderer receives failed/cancelled/interrupt state
  -> old OutputSession edits status bubble to "已打断"
  -> old OutputSession closes
  -> new command starts new OutputSession
```

## Configuration

Add a small explicit config section:

```toml
[telegram_output]
preview_enabled = true
preview_edit_min_interval_seconds = 2.0
preview_send_timeout_seconds = 5.0
product_body_mode = "final"
terminal_body_mode = "semantic_blocks"
semantic_min_chars = 900
semantic_max_chars = 3200
final_chunk_chars = 3900
terminal_block_idle_seconds = 2.0
```

Allowed body modes:

- `final`: buffer until run completion;
- `semantic_blocks`: emit readable semantic blocks while running.

Default mapping:

- product cockpit: `final`;
- terminal onsite: `semantic_blocks`.

## Acceptance Criteria

1. A `/codex` answer like the observed gold-price example is not split into token fragments.
2. Product cockpit sends one status bubble during the run and one readable final answer at completion, unless final answer exceeds Telegram-safe length.
3. Terminal onsite may send progressive body messages, but every message is a semantic block.
4. Markdown links are never split into `.cn/)` style fragments when avoidable.
5. List items and Chinese sentences are not split mid-token when avoidable.
6. Outbox-backed preview rendering edits the same Telegram message after the first preview send.
7. Outbox delivery failures do not break orchestration.
8. Existing busy-choice buttons still work.
9. `发给当前` does not create a new output session.
10. `打断并执行这句` closes the old output session and starts a new one.
11. Product and terminal policies are both covered by tests.
12. Runtime event delivery evidence remains present for sends, edits, and failures.

## Test Strategy

### Unit tests

- semantic chunker split priority;
- Markdown link preservation;
- fenced code preservation;
- forced long split behavior;
- product policy buffers until completion;
- terminal policy emits semantic blocks;
- output session keys prevent run mixing;
- outbox wait returns real message id.

### Integration tests

- natural interaction renderer with outbox does not emit one message per token;
- product cockpit final-only answer delivery;
- terminal onsite block-streaming delivery;
- interrupt closes previous status bubble;
- append-to-current does not create a new status bubble.

### Live smoke

Run a Telegram live smoke that sends a long answer request and asserts:

- no sequence of tiny body messages under a configured threshold;
- final text contains complete Markdown links;
- task reaches `done`;
- delivery events include one preview send, preview edits, and final send(s).

## Risks

### R1. Waiting for outbox message id can stall preview creation

Mitigation:

- bounded timeout;
- if timed out, skip preview edits for that run and still deliver final answer;
- record an internal runtime event.

### R2. Chunker over-protects Markdown and delays terminal output

Mitigation:

- max char cap always wins;
- terminal idle flush forces a readable chunk;
- tests cover long link and long code fence cases.

### R3. Duplicate status systems

Mitigation:

- status bubble ownership moves into one output session path;
- RuntimeProgressManager either delegates to that path or uses the same preview transport.

### R4. User interrupts while final chunks are being delivered

Mitigation:

- output session terminal state is idempotent;
- final delivery is keyed to old run id;
- new run uses a separate key, so messages never mix.
