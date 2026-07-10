# WLCodex Interaction Layer Natural Streaming Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

> Superseded in user semantics by the 2026-05-20 Remote Workbench repair.
> "new conversation" and task-led copy below is historical context. Current
> normal Telegram copy must say Workbench/Cockpit/Onsite and must not expose
> task ids, queue blockers, session ids, or thread ids.

## Final Decision

WLCodex should split Telegram interaction behavior into an independent
interaction layer before deepening the natural chat experience.

The product direction is:

```text
Controller / runtime
  -> create conversations, tasks, approvals, Codex/Claude runs
  -> emit task and agent events

Interaction layer
  -> choose profile
  -> render typing, streaming, messages, buttons, errors
  -> keep status and debug details out of normal chat

Telegram transport
  -> send, edit, typing, callback answers
```

The first profile is `natural`. It implements the B direction from the
brainstorming session: natural conversation plus full-chain streaming. The
future C direction becomes a separate `cockpit` profile that can expose richer
remote-control UI without changing Codex/Claude orchestration.

This design is intentionally not a rewrite of TaskService, Ledger, approval
handling, app-server JSON-RPC, Claude backend, or existing command routing.
Those pieces remain the runtime. The new layer owns only user-facing
presentation and Telegram interaction rhythm.

## Problems Being Solved

The current Telegram surface still feels too much like an AI task system:

- Plain text immediately receives mechanical ACK messages such as "正在处理你的消息，请稍候".
- Natural conversation replies include title, workspace, mode, and task-shaped
  metadata that the user did not ask for.
- Streaming exists but is not the default output path for Codex conversation
  results.
- `EventBridge` receives `agent_message_delta` events but deliberately skips
  them for status-card refreshes, so user-visible natural streaming is not
  wired through.
- `send_telegram` is a shared high-risk utility, so presentation changes should
  not be made by repeatedly editing it in place.
- Future cockpit-style interaction should not require reversing natural-chat
  work.

## Product Principles

1. Conversation first.
   A normal Telegram text message should feel like talking to a capable
   engineering partner.

2. Runtime hidden by default.
   Task IDs, thread IDs, token counts, workspace, and mode stay available in
   `/status`, diagnostics, or cockpit views. They do not appear in every normal
   reply.

3. Streaming is a tap, not a rerun.
   Natural streaming forwards deltas from the same Codex/Claude run. It must not
   launch a second model call, summarize with a second model call, or re-feed
   Telegram-visible status text into prompts.

4. Profiles are swappable.
   `natural`, `legacy`, and future `cockpit` behavior share the same interaction
   event protocol and transport wrapper.

5. Approval remains precise.
   Approval cards are security-critical. Their buttons and wording can be
   polished, but they must remain explicit and auditable.

6. Existing reliability wins.
   Network resilience, "message is not modified" handling, status-card
   throttling, queue draining, watchdog behavior, and recovery notifications
   must keep working.

## Interaction Profiles

### Natural Profile

The `natural` profile is the default product UX.

Behavior:

- No textual ACK for normal text messages.
- Start Telegram typing while the controller/runtime is preparing a run.
- Open a single stream message when the first visible model delta arrives.
- Edit that same message at a throttled interval while deltas arrive.
- On completion, flush the final message with a small deterministic action row.
- Hide mode, workspace, thread ID, task ID, token count, and internal run IDs
  from the normal response body.
- Keep greeting responses short.
- Show friendly errors for common startup, auth, rate limit, context, and
  network failures.

Default buttons after a normal completed reply:

| Button | Meaning |
| --- | --- |
| 继续 | Continue the active conversation |
| 查看 diff | Show current conversation diff when changed files exist |
| 状态 | Open the current conversation status view |
| 新对话 | Start a fresh visible conversation |

The profile may omit buttons that are not meaningful for the current run. For
example, `查看 diff` is hidden when there are no changed files.

### Legacy Profile

The `legacy` profile preserves current task-card behavior for compatibility and
operator use.

Behavior:

- Existing ACK/status card patterns are allowed.
- Existing help/status wording can remain technical.
- Existing hidden task commands continue to work.
- This profile is useful as a rollback target while natural streaming is being
  introduced.

### Cockpit Profile

The `cockpit` profile is reserved for the future C direction.

Behavior:

- Richer activity feed is allowed.
- Workspace, model, queue, approvals, and run details can be visible by default.
- It may expose remote-control cards, process controls, and diagnostics.
- It must still consume the same interaction events and Telegram transport as
  `natural`.

## Standard Interaction Events

The interaction layer consumes presentation events. These events are not model
prompts and must never be appended to Codex/Claude context.

```python
from dataclasses import dataclass, field
from typing import Any, Literal

InteractionEventType = Literal[
    "run_started",
    "text_delta",
    "tool_activity",
    "approval_requested",
    "run_completed",
    "run_failed",
    "status_refresh",
]

@dataclass(frozen=True)
class InteractionEvent:
    event_type: InteractionEventType
    chat_id: int
    conversation_id: int | None = None
    task_id: int | None = None
    thread_id: str = ""
    text: str = ""
    summary: str = ""
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

Event mapping:

| Runtime source | Interaction event |
| --- | --- |
| Plain text handler accepted a message | `run_started` |
| Codex `agent_message_delta` | `text_delta` |
| Claude `AgentStreamEvent(event_type="text")` | `text_delta` |
| Command/file activity that should be visible in cockpit only | `tool_activity` |
| Backend `approval_requested` | `approval_requested` |
| Backend terminal task state | `run_completed` or `run_failed` |
| User requested `/status` | `status_refresh` |

## Transport Boundary

The current `WlCodexHandlers.send_telegram`, `edit_telegram`, and `_start_typing`
functions already contain important Telegram-specific resilience. The
interaction layer should wrap them instead of replacing them directly.

```python
class TelegramTransport:
    def __init__(self, send_fn, edit_fn, typing_fn, answer_callback_fn=None) -> None:
        self._send = send_fn
        self._edit = edit_fn
        self._typing = typing_fn
        self._answer_callback = answer_callback_fn

    async def send(self, chat_id: int, text: str, buttons=None) -> int:
        return await self._send(chat_id, text, buttons)

    async def edit(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        await self._edit(chat_id, message_id, text, buttons)

    async def typing(self, chat_id: int):
        return await self._typing(chat_id)
```

This keeps the high-risk `send_telegram` behavior stable while the new UX is
introduced.

## Streaming Lifecycle

Natural streaming follows one message lifecycle:

```text
plain text received
  -> profile.on_user_message_started()
  -> transport.typing(chat_id)
  -> controller starts the same Codex/Claude run as today
  -> EventBridge maps agent_message_delta to InteractionEvent("text_delta")
  -> NaturalChatProfile appends delta into a renderer buffer
  -> renderer edits the same Telegram message at a safe interval
  -> terminal event flushes final text and deterministic buttons
```

The renderer must:

- throttle edits;
- avoid sending empty messages;
- fall back to send when the edit target is missing;
- preserve existing "message is not modified" behavior;
- truncate or split messages before Telegram length limits are hit;
- never trigger another model call.

## Error Presentation

The profile converts common technical errors into short, useful text.

Examples:

| Error pattern | Natural message |
| --- | --- |
| Codex startup failure | "Codex 没启动起来。我保留了这次请求，可以稍后重试。" |
| Auth or token failure | "认证看起来失效了，需要先检查登录状态。" |
| Rate limit | "现在被限流了，等一会儿再继续会更稳。" |
| Context too long | "上下文太长了。我建议开新对话或先让我压缩范围。" |
| Telegram network error | no duplicate user spam; log and retry through existing send/edit resilience |

Detailed exception strings stay in logs and diagnostics.

## Configuration

Add a small interaction config section:

```toml
[interaction]
profile = "natural"
streaming_enabled = true
show_footer = false
edit_min_interval_seconds = 1.0
```

Accepted profiles:

- `natural`
- `legacy`
- `cockpit`

`cockpit` may initially resolve to `legacy` until the profile is implemented,
but the config parser should accept it so future migration is smooth.

## Non-Goals

- Do not rewrite Codex or Claude backend execution.
- Do not add AI-generated Telegram buttons in the first implementation.
- Do not add per-message token/cost footers in the natural profile by default.
- Do not make streaming by starting a second model run.
- Do not remove legacy task commands.
- Do not change approval decision semantics.
- Do not inject Telegram status, logs, or rendered UI text into model prompts.

## Risk Notes From GitNexus

Impact analysis before this spec showed:

- `WlCodexHandlers.send_telegram` is HIGH risk. Direct callers include
  `claude_cmd`, `auto_cmd`, `conversation_text`, `_edit_or_send_result`, and
  network resilience tests.
- `EventBridge.process_event` is MEDIUM risk. It participates in the main
  runtime flow and several event-bridge tests.
- `StreamingRenderer` is LOW risk but imported by Telegram handlers and tests.
- `CommandController.handle_conversation_text` and
  `WlCodexHandlers.conversation_text` were low risk in graph terms, but they are
  user-facing and should still be changed cautiously.

The implementation plan should therefore introduce new files and adapter seams
first, then wire them into high-risk functions in small tested steps.

## Acceptance Criteria

1. Natural profile can be selected through config.
2. Plain text in natural profile no longer sends a textual ACK before the run.
3. Lightweight greetings return a short human response without workspace/mode
   metadata.
4. Codex `agent_message_delta` events can stream into one throttled Telegram
   message for the active task.
5. Streaming uses the same existing run and does not call Codex/Claude again.
6. Existing approval cards still send buttons and do not also spam status edits.
7. Existing send/edit network resilience tests still pass.
8. Legacy command behavior remains available.
9. The interaction event protocol can support a future cockpit profile without
   changing controller/runtime APIs again.
10. Documentation explains that natural streaming changes Telegram rendering,
    not model token consumption.
