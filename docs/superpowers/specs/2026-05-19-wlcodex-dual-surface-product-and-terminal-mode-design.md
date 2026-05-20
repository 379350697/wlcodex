# WLCodex Dual Surface Product And Terminal Mode Design

## Decision

WLCodex should expose two independent Telegram-facing surfaces over one shared
conversation core:

```text
Telegram input
  -> Conversation Core
       events, external session ids, mode state, cursors, approvals
       |
       +-> Product Surface
       |     event-driven phone product, low noise, Codex/Claude speaker labels
       |
       +-> Terminal Surface
             remote terminal, Claude Remote style, raw stream, attach/detach
```

The two surfaces must not call each other, share renderer state, or reinterpret
each other's output. They share only durable conversation facts and external
agent session references. Switching mode is a view and input-routing change,
not a task restart.

Default mode is `product`. The user can switch to `terminal` at any time to see
and steer the raw Codex or Claude session. The user can switch back to
`product` and immediately continue the same conversation through the
Codex-Claude-Codex chief-engineer workflow.

## Source Evidence

This design is based on current WLCodex code and external references.

### Existing WLCodex Evidence

- `wlcodex/orchestrator.py` already owns the Codex -> Claude -> Codex
  chief-engineer loop.
- `wlcodex/codex_backend.py` already consumes Codex app-server events such as
  `thread/started`, `turn/started`, `turn/diff/updated`,
  `item/agentMessage/delta`, and approval requests.
- `wlcodex/claude_backend.py` already runs Claude Code with
  `--output-format stream-json`, parses stream events, and captures Claude
  `session_id`.
- `wlcodex/runtime_events.py`, `wlcodex/runtime_state.py`,
  `wlcodex/runtime_projector.py`, and `wlcodex/conversation_state_machine.py`
  already form an event-sourced runtime state layer.
- `wlcodex/interaction/renderer.py`, `wlcodex/streaming.py`, and
  `wlcodex/telegram_outbox.py` already provide Telegram streaming, throttled
  edits, and reliable delivery primitives.

### Official Reference Evidence

- Claude Code Remote Control keeps work running on the local machine and lets
  phone, browser, and terminal clients stay synchronized over the same local
  session. It supports `claude remote-control`, `claude --remote-control`, and
  `/remote-control` from an existing session.
  Source: https://code.claude.com/docs/en/remote-control
- Claude Remote Control explicitly separates the local execution process from
  the mobile/web interface: the web and mobile interfaces are a window into the
  local session, while filesystem, MCP servers, tools, and project config remain
  local.
  Source: https://code.claude.com/docs/en/remote-control
- Claude Code supports programmatic `stream-json` output and streaming JSON
  input for multiple turns without relaunching the binary. It also returns a
  `session_id` that can be used with `--resume`.
  Source: https://code.claude.com/docs/en/headless
- Claude Code hooks expose session metadata, transcript paths, tool events,
  notifications, permission decisions, and prompt submission events.
  Source: https://code.claude.com/docs/en/hooks
- Codex app-server exposes `thread/start`, `thread/resume`, `thread/fork`,
  turn notifications, item notifications, and diff/file-change events.
  Source: https://developers.openai.com/codex/app-server
- Codex non-interactive mode supports `codex exec --json`, where stdout is a
  JSONL stream containing events such as `thread.started`, `turn.started`,
  `turn.completed`, `turn.failed`, `item.*`, and `error`.
  Source: https://developers.openai.com/codex/noninteractive

### Telegram Remote Implementation Evidence

- CCGram uses Telegram as a remote bridge for Claude Code, including permission
  approvals, question answering, session resume, terminal/tmux integration, and
  hook-based `updatedInput`.
  Source: https://github.com/jsayubi/ccgram
- The tmux-oriented CCGram fork treats each Telegram topic/window as an attached
  terminal for Claude Code, Codex CLI, Gemini CLI, or other agent CLIs.
  Source: https://github.com/alexei-led/ccgram
- HeyAgent positions itself as a bidirectional Telegram bridge for Claude Code
  and Codex CLI.
  Source: https://github.com/gergomiklos/heyagent
- CodexClaw supports Telegram access to Codex with separate SDK and CLI/PTY
  backends, access control, MCP routing, and multi-agent concepts.
  Source: https://github.com/MackDing/CodexClaw

## Goals

1. Provide a product-grade phone surface for normal WLCodex work.
2. Provide a remote-terminal surface that feels close to Claude Remote Control
   and raw Codex/Claude CLI output.
3. Keep both surfaces independent so changes to one do not affect the other.
4. Allow instant mode switching without losing the active conversation,
   workspace, agent run, external session id, or pending context.
5. Preserve the Codex -> Claude -> Codex invariant for code-producing product
   workflow runs.
6. Keep raw diff/tool/stdout detail out of Product Surface by default, while
   making it available in Terminal Surface and inspection commands.
7. Make every route, mode switch, terminal attach, detach, and surface delivery
   replayable from runtime events.
8. Support future parallel implementation by splitting core, terminal, product,
   Telegram command, persistence, and testing work into separate ownership
   areas.

## Non-Goals

- Do not replace Claude Code's official Remote Control service.
- Do not forward Telegram messages into the Anthropic cloud unless Claude Code
  itself does so as part of official Remote Control or normal Claude Code use.
- Do not expose a public inbound network port.
- Do not dump Product Surface summaries back into Codex or Claude prompts.
- Do not force Terminal Surface users through the chief-engineer workflow when
  they explicitly attach to a raw terminal session.
- Do not make Terminal Surface the default for ordinary Telegram messages.
- Do not mix raw terminal frames into Product Surface message buffers.
- Do not make a web dashboard in this scope.

## Definitions

### Conversation Core

The durable shared layer for a Telegram chat's active work. It stores:

- conversation id
- chat id
- workspace alias/path
- active mode
- active product run, if any
- active terminal session, if any
- external Codex thread/turn ids
- external Claude session id
- per-surface cursors
- pending user context
- routing and delivery events

The core never renders Telegram text. It only records and replays facts.

### Product Surface

The event-driven phone product. It renders concise, human-readable updates:

```text
codex: 我开始分析这个需求，会先确认影响范围。
claude: 现在开始实现，代码细节已记录到本地。
codex: 我开始验收 Claude 的改动。
codex: 验收通过，可以查看 diff。
```

It hides raw tool output, JSON, and full diffs unless the user taps or runs an
inspection command.

### Terminal Surface

The raw remote terminal view. It is inspired by Claude Remote Control:

- local process keeps running
- remote view attaches to the session
- input from phone continues the same session
- raw stream and terminal-like output are visible
- permission prompts and questions can be answered remotely
- disconnecting the phone view does not necessarily kill the local process

For Claude, this should prefer official Remote Control when available and
otherwise fall back to stream-json or PTY/tmux-style capture. For Codex, this
should prefer app-server event streams and fall back to `codex exec --json` or
PTY/tmux capture only when needed.

### Surface Cursor

A per-surface checkpoint into the shared timeline:

```text
product_cursor: last runtime event rendered by Product Surface
terminal_cursor: last terminal frame rendered by Terminal Surface
```

Each surface can catch up independently. Product Surface does not need to render
all terminal frames. Terminal Surface does not need to render all product
summary messages.

### Mode Switch Checkpoint

An event recorded when the user switches surfaces:

```json
{
  "from_mode": "product",
  "to_mode": "terminal",
  "active_agent": "claude",
  "active_phase": "implementation",
  "conversation_id": 42,
  "workspace_alias": "wlcodex",
  "codex_thread_id": "thr_...",
  "codex_turn_id": "turn_...",
  "claude_session_id": "abc123",
  "product_cursor": 1201,
  "terminal_cursor": 94
}
```

The checkpoint is the handoff marker. It must not inject summary text into the
agent context by itself.

## Product Requirements

### Product Surface Requirements

1. A normal Telegram text message in product mode starts or continues the
   conversation-first workflow.
2. Product mode keeps the chief-engineer invariant:

   ```text
   Telegram request
     -> Codex analysis
     -> Claude implementation when code changes are needed
     -> Codex verification
     -> Telegram response
   ```

3. Agent updates are labeled with the active agent:

   ```text
   codex: 我开始分析...
   claude: 现在开始实现...
   codex: 开始验收...
   ```

4. Product mode sends one compact live stream or status message per run, edited
   at a safe interval.
5. Product mode may show deterministic action buttons:

   ```text
   继续 | 查看 diff | 状态 | 日志 | 切到终端 | 新对话
   ```

6. Product mode must never show full diff by default.
7. Product mode must acknowledge user input that arrives during implementation
   or verification:

   ```text
   已记录，当前阶段结束后由 Codex 判断是否中断/重跑。
   ```

8. Product mode must never route implementation-stage user follow-up directly
   to Claude. It records pending context for Codex review.
9. Product mode must continue to use the Telegram outbox for sends, edits, and
   callback answers.

### Terminal Surface Requirements

1. `/terminal` attaches the current conversation to terminal mode.
2. `/terminal claude` attaches to or starts the Claude terminal session for the
   current conversation.
3. `/terminal codex` attaches to or starts the Codex terminal session for the
   current conversation.
4. Terminal mode forwards user messages to the selected raw session rather than
   the product chief-engineer router.
5. Terminal mode displays raw session output in terminal-style frames.
6. Terminal mode keeps separate terminal cursors so the user can switch away and
   return without losing the live tail.
7. Terminal mode should provide:

   ```text
   /terminal tail
   /terminal pause
   /terminal detach
   /terminal product
   /terminal agent codex
   /terminal agent claude
   ```

8. Detaching from terminal mode stops Telegram live streaming but does not kill
   the underlying process.
9. Aborting from terminal mode is explicit and must record a runtime event.
10. Terminal mode may show raw diffs and command output, but must still redact
    configured secrets before sending to Telegram.

### Smooth Switching Requirements

1. `/product` switches to product mode immediately.
2. `/terminal` switches to terminal mode immediately.
3. Switches never create a new conversation unless the user also asks for
   `/new`.
4. Switches record a `conversation.mode.switched` event.
5. Switches update only the chat's active mode and the relevant surface cursor.
6. The next Telegram message uses the new mode's input router.
7. Switching from product to terminal during Claude implementation attaches to
   the active Claude session if one exists. If no live terminal stream exists,
   terminal mode starts at a live-tail boundary and offers `/terminal tail`.
8. Switching from terminal to product does not replay the entire raw terminal
   transcript into product mode. It creates a compact checkpoint and resumes the
   product workflow from the shared conversation facts.
9. Product mode and terminal mode must be able to render different messages for
   the same underlying event without editing each other's Telegram messages.

## Architecture

### Shared Core Layer

New conceptual modules:

```text
wlcodex/surfaces/core/models.py
wlcodex/surfaces/core/store.py
wlcodex/surfaces/core/router.py
wlcodex/surfaces/core/events.py
wlcodex/surfaces/core/cursors.py
```

Responsibilities:

- define `SurfaceMode`, `SurfaceCursor`, `TerminalSessionRef`,
  `ProductRunRef`, and `ModeSwitchCheckpoint`
- append mode events to the runtime event store
- replay active surface state by chat/conversation
- route inbound Telegram text by active mode
- expose a small interface consumed by product and terminal surfaces

This layer may call existing conversation/task/runtime stores. It must not call
Telegram send/edit directly.

### Product Surface Layer

New conceptual modules:

```text
wlcodex/surfaces/product/events.py
wlcodex/surfaces/product/renderer.py
wlcodex/surfaces/product/speaker.py
wlcodex/surfaces/product/buttons.py
wlcodex/surfaces/product/router.py
```

Responsibilities:

- map runtime events to product display events
- label visible events by agent and phase
- hide raw detail unless explicitly requested
- reuse `TelegramTransport`, `StreamingRenderer`, and `TelegramOutbox`
- preserve the Codex-Claude-Codex invariant

### Terminal Surface Layer

New conceptual modules:

```text
wlcodex/surfaces/terminal/models.py
wlcodex/surfaces/terminal/manager.py
wlcodex/surfaces/terminal/claude_remote.py
wlcodex/surfaces/terminal/codex_terminal.py
wlcodex/surfaces/terminal/renderer.py
wlcodex/surfaces/terminal/redaction.py
wlcodex/surfaces/terminal/router.py
```

Responsibilities:

- manage terminal session refs
- attach to active Claude or Codex sessions
- provide raw session input
- capture raw output frames
- emit terminal frames to the terminal cursor
- redact secrets before Telegram delivery
- render terminal frames independently from product rendering

### Telegram Command Layer

New or modified command behavior:

```text
/mode
/product
/terminal
/terminal claude
/terminal codex
/terminal tail
/terminal detach
/terminal pause
/terminal product
```

The command layer changes active mode or terminal agent selection. It does not
own session execution.

### Persistence Layer

Store mode state as runtime events first. Projection tables are allowed for fast
lookup:

```text
surface_sessions
surface_cursors
terminal_frames
```

Projection tables are not the source of truth. If they disagree with the
runtime event log, replay wins.

## Runtime Events

Add these event types conceptually:

```text
conversation.mode.switched
surface.cursor.advanced
terminal.session.attached
terminal.session.detached
terminal.session.input.sent
terminal.session.output.frame
terminal.session.aborted
product.display.frame
product.pending_context.recorded
surface.delivery.sent
surface.delivery.edited
surface.delivery.failed
```

Event payloads should include:

- `conversation_id`
- `chat_id`
- `mode`
- `surface`
- `agent`
- `phase`
- `external_session_id`
- `codex_thread_id`
- `codex_turn_id`
- `claude_session_id`
- `cursor`
- `telegram_message_id`, when known
- redaction metadata for terminal frames

## Input Routing

### Product Mode Routing

```text
inbound Telegram text
  -> append user.message.received
  -> active mode is product
  -> route to conversation-first controller
  -> if phase is implementation or verification:
       append pending context
       acknowledge
     else:
       start/continue Codex analysis
```

### Terminal Mode Routing

```text
inbound Telegram text
  -> append user.message.received
  -> active mode is terminal
  -> selected terminal agent is claude or codex
  -> append terminal.session.input.sent
  -> send text to raw session adapter
  -> raw output frames append terminal.session.output.frame
  -> Terminal Surface renderer sends/edits Telegram terminal feed
```

Terminal routing must not call the product chief-engineer router unless the
user switches back to product mode.

## Terminal Session Strategy

### Claude Terminal Strategy

Preferred order:

1. Attach to an official Claude Remote Control session when one already exists
   and the environment supports it.
2. Start or expose a local interactive session with remote-control semantics:
   local process remains the execution owner, phone is a view/input device.
3. Fall back to `claude -p --output-format stream-json --input-format
   stream-json --verbose` for a non-PTY multi-turn stream.
4. Fall back to PTY/tmux capture only when raw terminal fidelity is required and
   official/programmatic streams are insufficient.

The implementation must record which strategy was chosen:

```text
strategy=official_remote_control | stream_json | pty
```

### Codex Terminal Strategy

Preferred order:

1. Attach to an existing app-server thread/turn and render app-server
   notifications as terminal frames.
2. Use `codex exec --json` for JSONL event stream sessions when app-server is
   unavailable.
3. Fall back to PTY/tmux capture only for full terminal-fidelity sessions.

The implementation must record:

```text
strategy=app_server | exec_json | pty
```

## Rendering Rules

### Product Rendering

Product rendering turns internal events into concise text:

```text
codex: 我开始分析这个需求。
claude: 开始实现，细节会记录到本地。
codex: 开始验收。
codex: 验收通过。改动集中在 3 个文件。
```

Rules:

- no raw JSON
- no full diff
- no long stdout/stderr
- no internal id unless requested
- one compact stream message per active run
- action buttons after terminal states
- speaker label is required for agent-originated updates

### Terminal Rendering

Terminal rendering turns raw frames into terminal-style Telegram messages:

```text
[claude:implementation] $ Write src/foo.py
...
[tool] Bash(pytest -q)
...
[diff] src/foo.py +12 -3
```

Rules:

- raw output can be shown, subject to Telegram length limits
- frames are chunked and redacted
- cursor must advance only after successful delivery or queued outbox delivery
- if Telegram truncates a frame, local trace remains complete and the message
  shows how to fetch the tail

## Approval Handling

Approval cards are shared security objects, not owned by either surface.

- Product Surface shows concise approval cards.
- Terminal Surface may show raw approval context and the same buttons.
- A decision in either surface resolves the same approval id.
- Duplicate or stale callbacks must be rejected.
- Approval resolution must be recorded before any response is sent to the agent.

## Diff And Detail Handling

Diffs are stored once and exposed in two ways:

- Product Surface: summary plus `查看 diff` button.
- Terminal Surface: raw or near-raw diff frames, subject to redaction and
  Telegram length limits.

The shared core records diff availability and file counts, but not surface
rendering choices.

## Recovery

On daemon restart:

1. Replay runtime events.
2. Reconstruct active conversation and active mode.
3. Reconstruct product and terminal cursors.
4. Reattach terminal sessions when possible.
5. Mark terminal sessions as `detached` or `orphaned` when the local process is
   gone.
6. Product mode remains usable even if terminal reattach fails.
7. Terminal mode reports reattach failure without affecting product mode.

## Security

- Keep existing Telegram allowlist and private-chat checks.
- Redact Telegram bot tokens, API keys, OAuth tokens, SSH keys, and `.env`
  values from terminal frames.
- Terminal mode must make risky raw control explicit.
- Product mode must not expose raw secrets by summarizing terminal output.
- Claude subprocesses must continue to receive sanitized environments.
- No inbound public listener is introduced.
- Official Claude Remote Control may use outbound HTTPS through Anthropic's
  Remote Control service if the user explicitly configures and starts it.

## Failure Behavior

| Failure | Product Surface | Terminal Surface |
| --- | --- | --- |
| Product renderer error | Send short error, keep conversation active | No impact |
| Terminal renderer error | No impact | Send terminal delivery error if possible |
| Terminal process exits | Product can continue from shared state | Mark detached/exited |
| Mode switch fails | Stay in previous mode | Stay in previous mode |
| Approval callback fails | Do not resolve approval | Do not resolve approval |
| Telegram send/edit fails | Outbox retries | Outbox retries |
| Raw output exceeds limit | Show summary and tail command | Chunk or tail |

## Testing Requirements

Unit tests:

- mode switch records checkpoint
- product and terminal cursors advance independently
- product mode hides raw diff
- terminal mode shows terminal frames
- terminal input does not call product orchestrator
- product follow-up during implementation becomes pending context
- switching modes changes next-message routing immediately
- approval resolution is shared
- terminal frame redaction removes configured secrets

Integration tests:

- product -> terminal -> product switch during Claude implementation
- terminal -> product switch after raw Codex output
- daemon restart reconstructs active mode and cursors
- terminal detach does not abort product run
- product failure does not break terminal session
- terminal failure does not break product conversation

## Rollout Plan

1. Add shared surface contracts and event types behind tests.
2. Add persistence projections for mode and cursors.
3. Add Product Surface adapter around the existing interaction renderer.
4. Add Terminal Surface manager with fake backends first.
5. Add Telegram mode-switch commands.
6. Add Claude terminal strategy.
7. Add Codex terminal strategy.
8. Add recovery and reattach behavior.
9. Enable for one authorized user only.
10. Keep legacy behavior available through a config flag until the dual-surface
    path passes live smoke tests.

## Open Decisions

These should be settled during implementation planning, not by changing this
product direction:

1. Whether Terminal Surface V1 should implement official Claude Remote Control
   start/attach first, or start with stream-json because it is already close to
   existing `ClaudeBackend`.
2. Whether Codex Terminal Surface V1 should render app-server notifications as
   terminal frames first, or add a PTY/tmux path for maximum CLI fidelity.
3. How much terminal history to replay on Telegram when reattaching:
   recommended default is live tail plus `/terminal tail`.

## Acceptance Criteria

The feature is complete when:

1. A user can start in product mode and see labeled Codex/Claude/Codex progress.
2. The same conversation can switch to terminal mode without restarting the
   underlying work.
3. Terminal mode can accept input and show raw session output.
4. The user can switch back to product mode and continue the same conversation.
5. Product mode and terminal mode maintain independent cursors and messages.
6. Raw diffs never appear in product mode by default.
7. Approval decisions work from either mode.
8. Restart recovery preserves mode state and handles missing terminal sessions
   clearly.
9. Tests prove product and terminal surfaces do not call or mutate each other's
   renderers.
