# Deep Repair Spec Compliance Review

Verdict: PASS

Date: 2026-05-20

Scope:
- Product-path Task semantics cleanup in menu/help/status/new/recovery/verification/busy flows.
- Cockpit/Onsite wording and routing-facing assertions.
- Legacy diagnostics isolation in documentation and test expectations.
- Legacy task command/waiting/worktree handlers isolated behind `LegacyDiagnosticsController`.
- Workbench execution reservations routed through `ExecutionScheduler` / `RunIntent` / `ExecutionLease`.
- Telegram historical continuation now uses the injected scheduler boundary and does not access controller private TaskService state.
- Approval, runtime final, recovery, and inspector copy now use Workbench/run language on default user paths.
- Legacy task status cards moved to `legacy_task_status.py` and are only used by explicit legacy diagnostics.
- Onsite input events no longer put raw `external_session_id` in USER-visible payloads.
- Terminal attach events keep native session refs for recovery but are OPERATOR-visible, and runtime trace display redacts internal refs.
- `/sessions` documentation is product-path historical agent session language, not legacy conversation/thread mapping language.
- Test harness repairs required to run the full regression suite reliably.
- 2026-05-21 dead-code cleanup: removed legacy waiting/worktree callback protocol routing, deleted the waiting callback module/tests, removed legacy Telegram status-card auto-refresh, and converted live Telegram smoke evidence from task-id ledger evidence to Workbench/runtime-event evidence.

Requirements checked:
- Ordinary user paths do not present Workbench work as user-managed tasks.
- `/new` is the only explicit new Workbench command; natural language remains ordinary text.
- `/status`, `/sessions`, help, menu, recovery notices, terminal/product surface tests, and callback responses avoid `/task`, `/continue`, `/steer`, task IDs, queue positions, blocking-task wording, and exposed raw session IDs.
- Claude-only, Codex-only, default orchestration, historical session, and recovery behavior remain covered by the existing workbench suite.
- Legacy `/task` diagnostics remain outside default help/menu/status/live-smoke product paths and outside the main controller command branches.
- Legacy waiting/worktree callbacks are no longer routed by Telegram handlers or controller paths.
- Recovery restart notices send Workbench copy only and no longer edit old task status cards.
- Automated live smoke no longer requires `WLCODEX_LIVE_SMOKE_TASK_ID`; it locates the active Workbench conversation and checks runtime events plus persisted agent session refs.
- Workbench paths no longer call `TaskService.reserve_task` directly; they reserve internal execution leases.
- Superseded historical specs/plans are marked so future repair agents do not reintroduce old Task semantics.
- USER-visible runtime diagnostics redact `external_session_id`, `session_id`, `thread_id`, `hidden_task_id`, and related internal ref fields.

Evidence:
- `rtk pytest tests/test_workbench_core.py tests/test_workbench_cockpit_menu.py tests/test_workbench_commands.py tests/test_workbench_onsite_terminal.py tests/test_workbench_execution_modes.py tests/test_workbench_runtime_state.py tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py tests/test_workbench_session_library.py tests/test_execution_scheduler.py -q` -> 191 passed.
- `rtk pytest tests/test_runtime_diagnostics.py tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py tests/test_runtime_state_replay.py -q` -> 128 passed.
- `rtk pytest tests/test_controller_flow.py tests/test_telegram_handlers.py tests/test_terminal_surface.py tests/test_dual_surface_integration.py tests/test_runtime_projector.py tests/test_runtime_state_replay.py tests/test_recovery.py tests/test_router.py tests/test_status.py tests/test_task_service.py tests/test_surface_commands.py tests/test_telegram_conversation_handlers.py tests/test_recovery_notifications.py tests/test_execution_scheduler.py -q` -> 352 passed.
- `rtk pytest tests/test_workbench_cockpit_menu.py tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py tests/test_controller_flow.py tests/test_conversation_state_machine.py tests/test_surface_commands.py tests/test_runtime_diagnostics.py tests/test_runtime_state_replay.py tests/test_recovery.py tests/test_recovery_notifications.py tests/test_status.py tests/test_status_updates.py tests/test_runtime_interaction_renderer.py tests/test_interaction_profiles.py tests/test_inspection.py tests/test_telegram_conversation_handlers.py tests/test_telegram_handlers.py tests/test_telegram_runtime_events.py tests/test_telegram_outbox.py tests/test_main_composition.py tests/test_execution_scheduler.py -q` -> 470 passed.
- `rtk pytest -q` -> 1378 passed.
- `rtk pytest tests/test_event_bridge.py tests/test_telegram_handlers.py tests/test_live_telegram_smoke.py tests/test_controller_flow.py tests/test_task_service.py tests/test_router.py tests/test_conversation_router.py tests/test_recovery_notifications.py tests/test_main_composition.py -q` -> 216 passed.
- `rtk pytest -q` after dead-code cleanup -> 1322 passed.
- Dead-code scan: no files named `waiting_callback`, `test_waiting_callback`, or `test_e2e_fake_backend` remain; no live code references `_track_status_from_response`, `_store_status_message`, `handle_waiting_callback`, `handle_worktree_done_callback`, or `worktree_done:`.
- README/live-smoke scan: `WLCODEX_LIVE_SMOKE_TASK_ID` remains only in a superseded 2026-05-16 historical plan, not in product docs or active tests.
- Controller structural scan found no legacy command classes, task card/list rendering, direct `reserve_task`, or direct active task assignment in `wlcodex/controller.py`.
- Product-path forbidden wording scan found no old task/menu/status/recovery/inspection copy outside internal session-ref persistence fields.
- USER-visible event/ref scan found no `Visibility.USER` event payloads carrying `external_session_id`.
- Legacy wording scan is confined to `legacy_diagnostics.py`, `legacy_task_status.py`, and legacy inspector-compatible diagnostics.

Decision:
- The implementation satisfies the current Workbench/Cockpit/Onsite repair requirements and does not rely on legacy Task semantics as the product path.
