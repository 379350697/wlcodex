# WLCodex Worker Live Stream Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

## Status

Drafted on 2026-05-29 as the first implementation slice for the Virtual
Engineering Office direction.

This is a focused feature spec. It intentionally excludes the full office
overview, role orchestration, relay service, and Antigravity integration.

## Project Context

Repository:

```text
https://github.com/379350697/wlcodex
```

WLCodex already has:

- a Codex app-server backend that emits backend events;
- Claude Code streaming that is converted into runtime events;
- an append-only `runtime_events` ledger;
- Telegram Cockpit and Onsite surfaces;
- natural streaming into Telegram without duplicating model token usage;
- role-aware adaptive-team groundwork.

The next architectural step is to extract the live-stream capability from
Telegram and make it available as an independent local web stream. This gives
the future office UI a stable, reusable live worker station.

## Decision

Build **Worker Live Stream** first.

Worker Live Stream is a local, deterministic live stream surface for one
runtime worker/run/session. It streams existing WLCodex runtime events to a
browser/PWA client without invoking any model, summarizer, or office workflow.

The first version should support:

```text
RuntimeEventStore
  -> WorkerLiveStreamHub
      -> local HTTP/SSE stream
          -> minimal browser worker live page
```

SSE is the preferred first transport because the initial stream is one-way:
runtime events flow from WLCodex to the browser. User control actions can stay
out of scope for this slice and later use HTTP POST or WebSocket when needed.

## Why This Comes First

The Virtual Engineering Office depends on live worker stations. The office
metaphor can be added later, but every programmer card must eventually open the
same live stream. Therefore the live stream is the shared technical substrate.

This slice is independent because it does not require:

- project overview;
- office floorplan UI;
- role assignment policy;
- multi-worker scheduling;
- relay infrastructure;
- Antigravity integration;
- replacing Telegram;
- changing Codex or Claude execution behavior.

## Product Goal

A user or developer can open a local web page for an active worker and see its
runtime events arrive live:

- model text deltas;
- command starts;
- command output deltas;
- file/diff updates;
- approval requests;
- lifecycle completion/failure.

The stream should match the native Codex mobile remote concept defined in
`2026-05-29-wlcodex-virtual-engineering-office-design.md`: it is live execution
state, not a periodic summary and not a second model-generated narration.

## Scope

### In Scope

- Runtime-event query by `agent_run_id` and cursor.
- In-process live event fan-out from `RuntimeEventStore.append`.
- A normalized worker stream event JSON contract.
- Local HTTP endpoints for health, snapshot, and SSE stream.
- A minimal browser page that renders the stream.
- Configuration for enabling/disabling the local live stream server.
- Tests proving snapshot, cursor, live fan-out, and SSE formatting.

### Out Of Scope

- Full Virtual Engineering Office UI.
- Authentication and cloud relay.
- Push notifications.
- Bidirectional worker steering.
- Mobile install/PWA manifest.
- Antigravity adapter.
- Rich diff viewer.
- Screenshots and browser frames, except preserving event contract room for
  them later.
- Continuous LLM digest or summary generation.

## Canonical Stream Semantics

Worker Live Stream must be event-backed:

```text
backend/provider output
  -> normalized RuntimeEvent
      -> persisted runtime_events row
          -> streamed to subscribed clients
```

It must not be:

- polling Telegram output;
- scraping terminal text from a rendered message;
- refeeding logs into a model;
- asking a supervisor model to summarize every chunk;
- duplicating model runs.

## Worker Identity

The first slice identifies a worker by `agent_run_id`.

Reason:

- Codex and Claude runtime events already use `agent_run_id`;
- it is narrow enough to represent one worker station;
- it maps naturally to future programmer cards.

Later versions can add aliases such as `worker_id`, `project_id`, or
`conversation_id`, but `agent_run_id` is the correct minimal stable key.

## Event Contract

The stream sends JSON objects. Every object has:

```json
{
  "id": 123,
  "type": "model.text.delta",
  "kind": "text_delta",
  "agent_run_id": 42,
  "conversation_id": 7,
  "occurred_at": "2026-05-29T...",
  "source": "codex",
  "actor": "codex",
  "visibility": "user",
  "payload": {}
}
```

`type` is the original WLCodex runtime event type.

`kind` is a UI-friendly category:

| Runtime event type | Stream kind |
| --- | --- |
| `agent.run.started` | `lifecycle` |
| `agent.run.activity` | `activity` |
| `model.text.delta` | `text_delta` |
| `model.reasoning.delta` | `reasoning_delta` |
| `command.started` | `command_started` |
| `command.output.delta` | `command_output` |
| `command.completed` | `command_completed` |
| `command.failed` | `command_failed` |
| `file.changed` | `file_changed` |
| `diff.updated` | `diff_updated` |
| `approval.requested` | `approval_requested` |
| `approval.resolved` | `approval_resolved` |
| `agent.run.completed` | `completed` |
| `agent.run.failed` | `failed` |
| unknown supported event | `event` |

The payload should remain the stored redacted runtime payload. The live stream
must not bypass existing redaction.

## Cursor And Recovery

The stream is cursor-based.

- `after=0` returns all retained events for that worker.
- `after=<event_id>` returns events with `id > after`.
- SSE events use `id: <runtime_event_id>` so browser reconnect can provide
  `Last-Event-ID`.
- On reconnect, the server sends missed snapshot events before live events.
- Events are ordered by ascending runtime event id.

This is enough for the browser to refresh or reconnect without asking a model
to reconstruct the past.

## Local HTTP Contract

First version endpoints:

```text
GET /health
GET /workers/{agent_run_id}/live
GET /api/workers/{agent_run_id}/events?after=<id>&limit=<n>
GET /api/workers/{agent_run_id}/stream?after=<id>
```

Responses:

- `/health`: JSON health and server version.
- `/workers/{agent_run_id}/live`: minimal HTML client.
- `/api/.../events`: JSON snapshot.
- `/api/.../stream`: `text/event-stream` SSE response.

Default binding must be loopback:

```toml
[live_stream]
enabled = false
host = "127.0.0.1"
port = 18731
```

Loopback default preserves the existing safety posture. LAN/Tailscale binding
can be a later explicit operator choice.

## Browser Page

The first browser page should be intentionally small:

- header: worker id, connection state, last event id;
- event list with newest appended at bottom;
- text deltas rendered inline;
- command output in preformatted blocks;
- approvals highlighted;
- completed/failed status visible.

This page is a proof of the stream substrate, not the final office UI.

## Security

First slice security constraints:

- disabled by default;
- loopback-only by default;
- reject non-loopback host values unless an explicit later design permits them;
- stream only redacted stored runtime events;
- no model calls;
- no direct file reads;
- no command execution;
- no approval resolution endpoint yet;
- no user steering endpoint yet.

## Acceptance Criteria

1. A test can append historical events for `agent_run_id=42` and query them
   through the live stream snapshot in id order.
2. A subscriber receives new events appended after subscription without polling.
3. Cursor `after=<id>` excludes already-seen events.
4. SSE output includes `id:` and `data:` lines and uses normalized JSON.
5. The minimal browser page can connect to the SSE stream for a worker.
6. The live stream server is disabled unless `[live_stream].enabled = true`.
7. The server defaults to `127.0.0.1` and rejects unsafe bind hosts.
8. No implementation path invokes a model or consumes extra model tokens.
9. Existing Telegram behavior remains unchanged.

## Follow-Up Slices

After Worker Live Stream is stable:

1. Worker Detail Page: richer UI for a single worker.
2. Office Overview: project-level worker cards that link to live streams.
3. Worker Registry: role/backend/model labels.
4. Relay: secure remote access from phone outside local network.
5. Bidirectional Control: approve, steer, pause, resume, stop from web.
