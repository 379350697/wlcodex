# WLCodex Maintainability Optimization Design

> **SUPERSEDED — historical design only.** Do not use this document as current
> product fact; use [the current semantic contract](../../product-semantics.md).

> Review date: 2026-06-04
> Scope: maintainability-focused optimization of frontend delivery, controller boundaries, persistence boundaries, event processing, and Telegram command organization.

## Goal

Reduce the maintenance cost of WLCodex's largest and most coupled modules while preserving current behavior, public APIs, and test coverage.

The optimization is not a rewrite. It is a sequence of narrow extractions that keep existing entry points stable and make the code easier to understand, test, and change safely.

## Verified Baseline

The following facts were verified against the current repository:

- `wlcodex/controller.py` is 268 KB / 6341 lines. `CommandController` spans most of the file and coordinates command routing, conversation/workbench flow, orchestration, settings, permissions, callbacks, and queued runs.
- `wlcodex/live_stream/server.py` is 191 KB / 4428 lines. Large HTML, CSS, and JavaScript pages are embedded in Python string templates, especially `_live_page`.
- `wlcodex/db.py` is 108 KB / 2905 lines. `Ledger` owns schema creation, handwritten migrations, row mapping, and persistence APIs for tasks, events, approvals, conversations, orchestration, team projections, carryovers, and usage.
- `wlcodex/event_bridge.py` is 85 KB / 2228 lines. It consumes backend events and also handles approval notifications, runtime event mapping, direct agent status sync, staged auto transitions, expiry scanning, watchdog lifecycle, and Telegram stage buttons.
- `wlcodex/telegram_app.py` is 107 KB / 2566 lines. `WlCodexHandlers` owns authentication, command handlers, callback routing, send/edit behavior, terminal flows, and approval callbacks.
- `tests/` contains 120 top-level `test*.py` files with broad coverage around the modules above.

Corrections to earlier review assumptions:

- `server.py` is not the largest project file; `controller.py` and `tests/test_controller_flow.py` are larger.
- Current live stream pages already include some ARIA attributes and safe-area handling, so accessibility work should be an audit and completion pass, not a claim of total absence.
- Current live stream JavaScript does not use inline `onclick` attributes.
- Current `EventBridge` creates a separate watchdog task inside `run()`, so watchdog risk is lifecycle coupling and shared dependencies, not simple event-loop iteration blocking.
- Telegram messages are plain text with inline keyboards, not HTML parse-mode messages.

## Principles

1. Preserve current behavior first. Each extraction must keep existing callers working.
2. Keep facade compatibility during transition. Large public objects such as `CommandController` and `Ledger` remain importable while internals move behind smaller components.
3. Use tests as the contract. Every extraction gets focused regression coverage before production code changes.
4. Avoid framework churn. Do not introduce React, Vue, Vite, SQLAlchemy, Alembic, or a large template framework as part of this optimization.
5. Treat performance claims as hypotheses until measured.
6. Run GitNexus impact analysis before editing indexed symbols and warn on high or critical risk.

## P0: Highest-Value Structural Reductions

### Live Stream Static Assets

Move embedded CSS and JavaScript out of `wlcodex/live_stream/server.py` into package-local static assets while keeping the current HTTP server and page behavior.

Target shape:

```text
wlcodex/live_stream/
├── server.py
├── static/
│   ├── native_codex.css
│   ├── native_codex.js
│   ├── live_page.css
│   └── live_page.js
└── templates or small page helpers
```

The first pass should expose a package-local static route and move one small reusable asset before extracting the entire large page. This reduces risk and establishes a tested delivery path.

Acceptance criteria:

- Existing live stream and native route tests continue to pass.
- New tests verify static asset routes return expected content type, cache behavior, and package-local file content.
- `server.py` no longer needs to embed every UI behavior in Python strings after staged extraction.
- No route or API path changes for browser clients.

### CommandController Boundary Split

Split `CommandController` by responsibility while preserving `CommandController.handle()` as the external entry point.

Initial boundaries:

- command dispatch table and parsing handoff
- conversation/workbench commands
- direct agent commands
- staged auto/orchestration commands
- settings/model/permission commands
- callback routing
- queued run processing

Acceptance criteria:

- `CommandController` remains importable and constructible with the current signature.
- Existing tests for controller, command flow, workbench routing, and execution modes pass.
- The first extraction moves routing data and simple dispatch only; business behavior stays in place until covered by narrower tests.

## P1: Persistence And Event Boundaries

### Ledger Facade With Internal Repositories

Keep `Ledger` as the compatibility facade while moving implementation details into smaller modules.

Target modules:

- `db_schema.py`: schema creation and handwritten column upgrades
- `db_rows.py`: row-to-model mappers
- `db_tasks.py`: task, event, approval, touched-file, backend-request APIs
- `db_conversations.py`: conversation, agent-run, orchestration APIs
- `db_team.py`: team projections and memory APIs
- `db_usage.py`: usage event APIs

Acceptance criteria:

- Existing `Ledger` method names remain available.
- `tests/test_db.py` and dependent runtime/projector/recovery tests pass.
- Schema creation remains idempotent.
- Migration behavior remains handwritten and explicit; no external migration framework is introduced in this phase.

### EventBridge Responsibility Split

Keep `EventBridge.run()` and `EventBridge.process_event()` as the coordination surface, but extract pure or narrowly scoped handlers.

Target boundaries:

- approval notification handler
- runtime event mapper/appender
- direct agent status sync
- staged auto transition handler
- lifecycle tasks for expiry and watchdog

Acceptance criteria:

- Existing event bridge, auto workflow, runtime event, and Telegram delivery tests pass.
- Watchdog remains an independent asyncio task managed by bridge lifecycle.
- Event processing remains sequential unless a measured bottleneck justifies concurrency.

## P2: Telegram And Frontend Quality

### Telegram Command Groups

Split `WlCodexHandlers` into command groups while preserving `build_application()`.

Suggested groups:

- auth and event recording
- workbench/history/workspace commands
- execution commands
- terminal/product surface commands
- settings/model/permission commands
- diagnostic commands
- callback routing
- approval callbacks

Acceptance criteria:

- `build_application()` still registers the same commands.
- Command tests continue to pass.
- Inline keyboard construction uses shared helpers for repeated row/dict patterns.

### Live Stream Safety And Accessibility Audit

Audit existing UI code after static asset extraction.

Required outcomes:

- Classify all `innerHTML` use as static markup, escaped dynamic markup, or replaceable DOM construction.
- Replace avoidable dynamic `innerHTML` with DOM APIs or `textContent`.
- Add `:focus-visible` styles for keyboard users.
- Add `aria-live="polite"` to dynamic status/transcript areas where appropriate.
- Add a minimal response header policy for static assets and HTML. CSP can start in report-only or narrow mode if inline scripts remain during transition.

## Non-Goals

- No full UI rewrite.
- No large frontend build system in this change.
- No database ORM or third-party migration framework in this change.
- No broad renames unless performed through GitNexus-aware rename workflow.
- No performance optimizations without measurement or a focused failing test.

## Risks

- `CommandController`, `Ledger`, and `EventBridge` have broad blast radius. Extractions must be incremental and facade-preserving.
- Static asset extraction can break pages if dynamic values are not injected carefully.
- Telegram command splitting can regress callback behavior if callback data contracts change.
- Repository splitting can accidentally change transaction boundaries if commits are moved carelessly.

## Verification Strategy

Each phase must run focused tests before claiming completion:

- Live stream: `tests/test_worker_live_stream_server.py`, `tests/test_worker_live_stream_native_routes.py`, `tests/test_worker_live_stream_native_agent_routes.py`
- Controller: `tests/test_controller_flow.py`, `tests/test_command_flow.py`, `tests/test_workbench_execution_modes.py`, `tests/test_workbench_telegram_routing.py`
- Persistence: `tests/test_db.py`, `tests/test_runtime_event_store.py`, `tests/test_runtime_projector.py`, `tests/test_recovery.py`
- Event bridge: `tests/test_event_bridge.py`, `tests/test_auto_workflow.py`, `tests/test_telegram_runtime_events.py`
- Telegram: `tests/test_telegram_handlers.py`, `tests/test_telegram_conversation_handlers.py`, `tests/test_telegram_outbox.py`

Before committing, run `npx gitnexus detect-changes --repo wlcodex` and verify only expected symbols and flows are affected.
