# Deep Repair Code Quality Review

Verdict: PASS

Date: 2026-05-20

Scope:
- Product-complete edits across controller/status/menu/recovery/interaction copy.
- Extraction of legacy task command/callback handling into `wlcodex/legacy_diagnostics.py`.
- Introduction of `wlcodex/execution_scheduler.py` as the Workbench-to-internal-lease boundary.
- Introduction of `wlcodex/legacy_task_status.py` so old task-card rendering no longer lives in the default status module.
- Test updates for new Workbench semantics and full-suite reliability.
- Documentation cleanup for superseded old Task-first specs.
- 2026-05-21 cleanup removing unused legacy waiting callback/status-card refresh code and old task-id live smoke tests.

Checks:
- Edits preserve existing module boundaries: controller remains the Workbench command boundary; status/menu own product copy; recovery_notifications owns restart-facing user copy; terminal/session-library internals remain internal.
- No schema churn was introduced during this pass.
- Existing legacy task implementation is isolated as diagnostics; retained usages are diagnostic, tests, or internal persistence compatibility.
- Workbench run startup and historical continuation use `RunIntent`/`ExecutionLease`, keeping TaskService as an internal compatibility mechanism.
- `build_application` derives the scheduler from the controller when it is not passed explicitly, avoiding private controller/service access in Telegram handlers.
- EventBridge approval cards and inspector result titles no longer expose hidden task ids on default user paths.
- EventBridge no longer edits legacy task status cards on backend/terminal status churn; runtime_events remain the fact source.
- Telegram callback routing no longer accepts legacy `waiting:` or `worktree_done:` callback payloads.
- Recovery notifications no longer depend on `telegram_status_message_id` or status-card edits.
- Onsite input events avoid storing raw native session refs in USER-visible payloads.
- Terminal attach events retain native refs for recovery behind OPERATOR visibility, with runtime trace redaction as a display backstop.
- Test stub changes model python-telegram-bot objects more accurately by exposing both `keyboard` and `inline_keyboard`.
- Event loop cleanup in outbox tests uses `asyncio.run` and removes cross-test default-loop coupling.
- User-copy changes are covered by direct tests and a forbidden-word scan.

Evidence:
- `rtk pytest tests/test_workbench_telegram_routing.py::test_terminal_with_active_claude_session_auto_opens_onsite tests/test_workbench_telegram_routing.py::test_onsite_text_routes_to_terminal_manager_not_controller tests/test_runtime_diagnostics.py::test_trace_sanitizes_internal_session_refs -q` -> 3 passed.
- `rtk pytest tests/test_runtime_diagnostics.py tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py tests/test_runtime_state_replay.py -q` -> 128 passed.
- `rtk pytest tests/test_controller_flow.py tests/test_interaction_profiles.py tests/test_conversation_state_machine.py tests/test_recovery_notifications.py tests/test_main_composition.py::test_recovery_notification_sends_for_paused_restart_task tests/test_status_updates.py tests/test_workbench_cockpit_menu.py tests/test_telegram_conversation_handlers.py -q` -> 153 passed.
- `rtk pytest tests/test_surface_commands.py -q` -> 31 passed.
- `rtk pytest tests/test_telegram_outbox.py -q` -> 15 passed.
- `rtk pytest tests/test_execution_scheduler.py -q` -> 1 passed.
- `rtk pytest tests/test_workbench_cockpit_menu.py tests/test_workbench_telegram_routing.py tests/test_workbench_remote_integration.py tests/test_controller_flow.py tests/test_conversation_state_machine.py tests/test_surface_commands.py tests/test_runtime_diagnostics.py tests/test_runtime_state_replay.py tests/test_recovery.py tests/test_recovery_notifications.py tests/test_status.py tests/test_status_updates.py tests/test_runtime_interaction_renderer.py tests/test_interaction_profiles.py tests/test_inspection.py tests/test_telegram_conversation_handlers.py tests/test_telegram_handlers.py tests/test_telegram_runtime_events.py tests/test_telegram_outbox.py tests/test_main_composition.py tests/test_execution_scheduler.py -q` -> 470 passed.
- `rtk pytest -q` -> 1378 passed.
- `rtk pytest tests/test_event_bridge.py tests/test_telegram_handlers.py tests/test_live_telegram_smoke.py tests/test_controller_flow.py tests/test_task_service.py tests/test_router.py tests/test_conversation_router.py tests/test_recovery_notifications.py tests/test_main_composition.py -q` -> 216 passed.
- `rtk pytest -q` after cleanup -> 1322 passed.
- `rtk git diff --check` -> exit 0.
- `git diff --check` after cleanup -> exit 0.
- Static dead-code scan after cleanup -> legacy waiting callback module/tests and fake backend e2e task test deleted; no live handler/controller references to removed callback/status tracking helpers.
- Product-path forbidden wording scan -> no matches.
- USER-visible event/ref scan -> no `Visibility.USER` payload containing `external_session_id`.

Residual risk:
- Live Telegram smoke and deployment gate are intentionally left for the Final Gate Reviewer; this review only covers repository code, tests, docs, and static scans.

Decision:
- Code quality is acceptable for Final Gate review.
