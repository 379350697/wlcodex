from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from wlcodex.app_server_process import AppServerProcess, AppServerProcessConfig
from wlcodex.approval import ApprovalService
from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
from wlcodex.claude_binary import resolve_claude_binary
from wlcodex.claude_permissions import (
    RUNTIME_CLAUDE_PERMISSION_MODE_KEY,
    ClaudePermissionState,
    claude_permission_label,
    normalize_claude_permission_mode,
)
from wlcodex.codex_backend import AppServerCodexBackend, FakeCodexBackend
from wlcodex.config import load_config
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.event_bridge import EventBridge
from wlcodex.execution_scheduler import ExecutionScheduler
from wlcodex.inspection import TaskInspector
from wlcodex.menu import build_bot_commands
from wlcodex.orchestration_runner import OrchestrationRunner
from wlcodex.recovery_notifications import notify_recovery_paused_tasks
from wlcodex.runtime_diagnostics import (
    append_startup_recovery_events,
    append_recovery_events,
    find_non_terminal_agent_runs,
)
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_projector import RuntimeProjector
from wlcodex.task_service import TaskService
from wlcodex.team_model_settings import load_runtime_assignments
from wlcodex.telegram_app import build_application
from wlcodex.watchdog import TaskLivenessConfig, TaskWatchdog

logger = logging.getLogger(__name__)

# Startup retry configuration for Telegram Bot API initialization.
# Infinite bootstrap retry per spec — never give up on initialization.
_INITIALIZE_MAX_RETRIES = None  # infinite
_INITIALIZE_BACKOFF_BASE = 1.5  # seconds, exponential
_INITIALIZE_BACKOFF_CAP = 120.0  # cap delay at 2 minutes


async def _initialize_app_with_retry(
    app: object,
    max_retries: int | None = _INITIALIZE_MAX_RETRIES,
    backoff_base: float = _INITIALIZE_BACKOFF_BASE,
    backoff_cap: float = _INITIALIZE_BACKOFF_CAP,
    runtime_store: object | None = None,
) -> bool:
    """Call app.initialize() with retry/backoff for transient network errors.

    When *max_retries* is None, retries forever (infinite bootstrap retry).
    Returns True on success. Only returns False on non-network permanent errors.
    Does NOT leak the bot token in log messages.
    """
    import asyncio as _asyncio
    from telegram.error import NetworkError, TimedOut

    from wlcodex.runtime_events import (
        AggregateType, EventSource, EventType, RuntimeEvent, Visibility, now_iso,
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            if runtime_store is not None:
                try:
                    runtime_store.append(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.TELEGRAM_POLLER_BOOTSTRAP_STARTED,
                        aggregate_type=AggregateType.SYSTEM,
                        aggregate_id="telegram-poller",
                        correlation_id=f"bootstrap-{attempt}",
                        source=EventSource.SYSTEM,
                        actor="system",
                        visibility=Visibility.OPERATOR,
                        payload={"attempt": attempt},
                        occurred_at=now_iso(),
                    ))
                except Exception:
                    pass
            await app.initialize()
            if runtime_store is not None:
                try:
                    runtime_store.append(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.TELEGRAM_POLLER_BOOTSTRAP_SUCCEEDED,
                        aggregate_type=AggregateType.SYSTEM,
                        aggregate_id="telegram-poller",
                        correlation_id=f"bootstrap-{attempt}",
                        source=EventSource.SYSTEM,
                        actor="system",
                        visibility=Visibility.OPERATOR,
                        payload={"attempt": attempt},
                        occurred_at=now_iso(),
                    ))
                except Exception:
                    pass
            return True
        except (TimedOut, NetworkError) as exc:
            exhausted = max_retries is not None and attempt >= max_retries
            if exhausted:
                logger.error(
                    "Telegram initialize failed after %d attempts: %s",
                    max_retries, exc,
                )
                if runtime_store is not None:
                    try:
                        runtime_store.append(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.TELEGRAM_POLLER_BOOTSTRAP_FAILED,
                            aggregate_type=AggregateType.SYSTEM,
                            aggregate_id="telegram-poller",
                            correlation_id=f"bootstrap-{attempt}",
                            source=EventSource.SYSTEM,
                            actor="system",
                            visibility=Visibility.OPERATOR,
                            payload={"attempt": attempt, "error": str(exc)},
                            occurred_at=now_iso(),
                        ))
                    except Exception:
                        pass
                return False
            delay = min(backoff_base ** attempt, backoff_cap)
            logger.warning(
                "Telegram initialize attempt %d timed out, retrying in %.1fs",
                attempt, delay,
            )
            if runtime_store is not None:
                try:
                    runtime_store.append(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.TELEGRAM_POLLER_BOOTSTRAP_RETRYING,
                        aggregate_type=AggregateType.SYSTEM,
                        aggregate_id="telegram-poller",
                        correlation_id=f"bootstrap-{attempt}",
                        source=EventSource.SYSTEM,
                        actor="system",
                        visibility=Visibility.OPERATOR,
                        payload={"attempt": attempt, "delay_seconds": round(delay, 2),
                                 "error": str(exc)},
                        occurred_at=now_iso(),
                    ))
                except Exception:
                    pass
            await _asyncio.sleep(delay)
        except Exception as exc:
            # Non-network errors are likely permanent (bad token, etc.) —
            # don't retry.
            logger.error("Telegram initialize failed with non-network error: %s", exc)
            if runtime_store is not None:
                try:
                    runtime_store.append(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.TELEGRAM_POLLER_BOOTSTRAP_FAILED,
                        aggregate_type=AggregateType.SYSTEM,
                        aggregate_id="telegram-poller",
                        correlation_id=f"bootstrap-{attempt}",
                        source=EventSource.SYSTEM,
                        actor="system",
                        visibility=Visibility.OPERATOR,
                        payload={"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"},
                        occurred_at=now_iso(),
                    ))
                except Exception:
                    pass
            return False


def _create_terminal_manager(
    config: object,
    *,
    claude_backend: object | None = None,
    codex_backend: object,
) -> object | None:
    """Create a TerminalSessionManager when the terminal surface is enabled.

    Returns ``None`` when ``config.terminal.enabled`` is ``False``.
    Otherwise wires the Claude and Codex adapters registered for the
    given backends and returns a ready-to-use manager.
    """
    if not config.terminal.enabled:
        logger.info("Terminal surface disabled (set terminal.enabled = true to enable)")
        return None

    from wlcodex.surfaces.terminal.manager import TerminalSessionManager
    from wlcodex.surfaces.terminal.claude_remote import ClaudeTerminalAdapter
    from wlcodex.surfaces.terminal.codex_terminal import CodexTerminalAdapter

    adapters: dict[str, object] = {}
    if claude_backend is not None:
        adapters["claude"] = ClaudeTerminalAdapter(claude_backend)
    adapters["codex"] = CodexTerminalAdapter(codex_backend)
    manager = TerminalSessionManager(adapters=adapters)
    logger.info("Terminal surface enabled (agents: %s)", list(adapters.keys()))
    return manager


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx logs full Telegram Bot API URLs at INFO, including the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="WLCodex — Telegram Codex Cockpit")
    parser.add_argument("--config", default="config/wlcodex.toml")
    parser.add_argument(
        "--fake-backend", action="store_true", help="Use fake backend (testing)"
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))

    token = os.environ.get(config.telegram.bot_token_env)
    if not token:
        raise SystemExit(
            f"Missing Telegram token env variable: {config.telegram.bot_token_env}"
        )

    # Storage
    ledger = Ledger.open(config.storage.sqlite_path)
    ledger.migrate()

    # Runtime event store — append-only source of truth
    runtime_store = RuntimeEventStore(ledger._conn)

    # Runtime projector — updates compatibility tables from runtime events
    runtime_projector = RuntimeProjector(ledger._conn, store=runtime_store)
    runtime_store.add_projector(runtime_projector.apply)

    # Telegram delivery outbox — isolates network failures from orchestration
    from wlcodex.telegram_outbox import TelegramOutbox
    telegram_outbox = TelegramOutbox(store=runtime_store)

    paused_ids: list[int] = []
    try:
        recovery = append_startup_recovery_events(runtime_store, ledger)
        if any(recovery.values()):
            logger.info(
                "Runtime recovery: tasks=%s orchestration_runs=%s agent_runs=%s",
                recovery["task_ids"],
                recovery["orchestration_run_ids"],
                recovery["agent_run_ids"],
            )
    except Exception as exc:
        logger.warning("Runtime startup recovery failed (non-fatal): %s", exc)

    # Runtime recovery: find and mark orphaned agent runs
    try:
        orphaned_ids = find_non_terminal_agent_runs(runtime_store)
        if orphaned_ids:
            recovered = append_recovery_events(
                runtime_store,
                orphaned_agent_run_ids=orphaned_ids,
            )
            logger.info(
                "Runtime recovery: %d orphaned agent run(s) found, %d recovery events appended",
                len(orphaned_ids), len(recovered),
            )
    except Exception as exc:
        logger.warning("Runtime recovery scan failed (non-fatal): %s", exc)

    config.storage.task_log_dir.mkdir(parents=True, exist_ok=True)

    # Backend & process lifecycle
    _managed_process: AppServerProcess | None = None

    if args.fake_backend:
        backend = FakeCodexBackend()
        logger.info("Using fake backend (--fake-backend)")
    else:
        process = AppServerProcess(AppServerProcessConfig(
            binary=config.codex.binary,
            host=config.codex.app_server_host,
            port=config.codex.app_server_port,
            startup_timeout_seconds=config.backend.startup_timeout_seconds,
            codex_home=config.codex.codex_home,
        ))
        backend = AppServerCodexBackend(
            endpoint=process.endpoint,
            approval_policy=config.codex.approval_policy,
            sandbox=config.codex.sandbox,
            request_timeout_seconds=config.backend.request_timeout_seconds,
            codex_prompt_idle_timeout_seconds=(
                config.backend.codex_prompt_idle_timeout_seconds
            ),
            codex_analysis_hard_timeout_seconds=(
                config.backend.codex_analysis_hard_timeout_seconds
            ),
            codex_verification_hard_timeout_seconds=(
                config.backend.codex_verification_hard_timeout_seconds
            ),
        )
        backend.set_process_manager(process)
        logger.info(
            "Using app-server backend at %s:%s",
            config.codex.app_server_host,
            config.codex.app_server_port,
        )

        # Try connecting to an already-running loopback app-server
        import asyncio as _asyncio
        _tmp_loop = _asyncio.new_event_loop()
        try:
            if _tmp_loop.run_until_complete(process.wait_ready_async()):
                # Reusing an existing external process
                process.external_process = True
                logger.info("Reusing existing app-server at %s", process.endpoint)
            else:
                process.start()
                if _tmp_loop.run_until_complete(process.wait_ready_async()):
                    logger.info("Managed app-server started at %s", process.endpoint)
                else:
                    backend.set_health_error(
                        "app-server startup timed out; bot remains alive"
                    )
        finally:
            _tmp_loop.close()

        _managed_process = process

    # Services
    task_service = TaskService(
        ledger, config.workspaces,
        task_log_dir=config.storage.task_log_dir,
        worktree_root=str(config.storage.worktree_root),
    )
    inspector = TaskInspector(
        ledger,
        config.storage.task_log_dir,
        tail_lines=config.display.tail_lines,
        diff_max_chars=config.display.diff_max_chars,
    )
    approval_service = ApprovalService(
        callback_timeout_seconds=config.approval.callback_timeout_seconds,
        allow_session_approval=config.approval.allow_session_approval,
        workspaces={ws.alias: ws for ws in config.workspaces},
    )

    stored_permission_mode = ledger.get_runtime_setting(
        RUNTIME_CLAUDE_PERMISSION_MODE_KEY
    )
    try:
        claude_permission_mode = normalize_claude_permission_mode(
            stored_permission_mode or config.claude.permission_mode
        )
    except ValueError:
        logger.warning(
            "Ignoring invalid stored Claude permission mode: %s",
            stored_permission_mode,
        )
        claude_permission_mode = config.claude.permission_mode
    if stored_permission_mode != claude_permission_mode:
        ledger.set_runtime_setting(
            RUNTIME_CLAUDE_PERMISSION_MODE_KEY,
            claude_permission_mode,
        )
    claude_permission_state = ClaudePermissionState(claude_permission_mode)

    # Claude backend (adapter for Claude Code subprocess)
    claude_backend = None
    if config.claude.enabled:
        resolution = resolve_claude_binary(config.claude.binary)
        binary_resolution_error = ""
        if resolution.warning:
            logger.warning("Claude binary resolution warning: %s", resolution.warning)
        if not resolution.binary:
            attempted = (
                "WLCODEX_CLAUDE_BINARY",
                *(
                    item
                    for item in resolution.attempted
                    if item != "WLCODEX_CLAUDE_BINARY"
                ),
            )
            binary_resolution_error = (
                "Claude binary not found.\n"
                f"Tried: {', '.join(attempted)}.\n"
                "Set WLCODEX_CLAUDE_BINARY or install Claude Code CLI."
            )
            logger.error(
                "Claude binary not found. Tried: %s",
                ", ".join(attempted),
            )
        claude_backend = ClaudeBackend(ClaudeConfig(
            enabled=config.claude.enabled,
            binary=resolution.binary or config.claude.binary,
            startup_timeout_seconds=config.claude.startup_timeout_seconds,
            request_timeout_seconds=config.claude.request_timeout_seconds,
            stream_idle_timeout_seconds=config.claude.stream_idle_timeout_seconds,
            permission_mode=claude_permission_mode,
            model=config.claude.model,
            effort=config.claude.effort,
            binary_resolution_error=binary_resolution_error,
        ), permission_state=claude_permission_state)
        logger.info(
            "Claude backend enabled (binary: %s, source: %s, model: %s, effort: %s, permission: %s)",
            resolution.binary or config.claude.binary,
            resolution.source,
            config.claude.model,
            config.claude.effort,
            claude_permission_label(claude_permission_mode),
        )
    else:
        logger.info("Claude backend disabled (set claude.enabled = true to enable)")

    execution_scheduler = ExecutionScheduler(task_service, ledger)

    # Controller
    team_assignments = load_runtime_assignments(
        ledger,
        config.adaptive_team.assignments,
        config.adaptive_team.model_profiles,
    )
    team_model_profiles = config.adaptive_team.model_profiles
    implementer_model_profiles = team_assignments.get(
        "implementer",
        ("claude_deepseek", "codex_gpt"),
    )
    codex_implementer_enabled = config.adaptive_team.enabled and any(
        team_model_profiles.get(profile, profile).lower() == "codex"
        for profile in implementer_model_profiles
    )
    controller = CommandController(
        task_service, backend, inspector,
        ledger=ledger, claude_backend=claude_backend,
        claude_permission_state=claude_permission_state,
        default_mode=config.conversation.default_mode,
        default_workspace=config.conversation.default_workspace,
        runtime_event_store=runtime_store,
        execution_scheduler=execution_scheduler,
        adaptive_team_enabled=config.adaptive_team.enabled,
        implementer_model_profiles=implementer_model_profiles,
        adaptive_team_model_profiles=team_model_profiles,
        adaptive_team_role_skills=config.adaptive_team.role_skills,
        adaptive_team_role_capabilities=config.adaptive_team.role_capabilities,
        director_model_profile=(
            team_assignments.get("director", ("codex_gpt",)) or ("codex_gpt",)
        )[0],
        architect_model_profile=(
            team_assignments.get("architect", ("codex_gpt",)) or ("codex_gpt",)
        )[0],
        investigator_model_profile=(
            team_assignments.get("investigator", ("codex_gpt",)) or ("codex_gpt",)
        )[0],
        tester_model_profile=(
            team_assignments.get("tester", ("codex_gpt",)) or ("codex_gpt",)
        )[0],
        auditor_model_profile=(
            team_assignments.get("auditor", ("codex_gpt",)) or ("codex_gpt",)
        )[0],
    )

    # Terminal surface — wire terminal session manager when enabled.
    terminal_manager = _create_terminal_manager(
        config, claude_backend=claude_backend, codex_backend=backend,
    )

    # Telegram app
    app, handlers = build_application(
        config,
        token,
        controller,
        ledger,
        approval_service,
        runtime_event_store=runtime_store,
        outbox=telegram_outbox,
        terminal_manager=terminal_manager,
        execution_scheduler=execution_scheduler,
    )

    if handlers is None:
        logger.warning("Running in skeleton mode (no controller/ledger).")
        app.run_polling()
        return

    # Event bridge — consumes backend events, updates state + Telegram
    liveness_config = TaskLivenessConfig(
        max_running_seconds=config.task.max_running_seconds,
        max_queued_seconds=config.task.max_queued_seconds,
        max_waiting_approval_seconds=config.task.max_waiting_approval_seconds,
        backend_dead_grace_seconds=config.task.backend_dead_grace_seconds,
    )
    task_watchdog = TaskWatchdog(
        ledger,
        backend,
        liveness_config,
        runtime_store=runtime_store,
    )
    # Only wire interaction streaming when profile is natural AND streaming enabled.
    # Legacy/cockpit profiles and streaming_enabled=false must not receive
    # streaming deltas in Telegram.
    interaction_renderer = None
    if (
        config.interaction.profile == "natural"
        and config.interaction.streaming_enabled
    ):
        interaction_renderer = handlers.create_interaction_renderer()
        if not interaction_renderer.has_runtime_status_surface():
            # Wire legacy progress only when the readable output manager does not
            # already own the Telegram status bubble.
            from wlcodex.interaction.runtime_renderer import RuntimeProgressManager
            from wlcodex.interaction.transport import TelegramTransport

            async def _noop_typing(_chat_id: int) -> object:
                return None

            runtime_progress = RuntimeProgressManager(
                transport=TelegramTransport(
                    handlers.send_telegram,
                    handlers.edit_telegram,
                    _noop_typing,
                    outbox=telegram_outbox,
                ),
                verbosity=1,
                min_edit_interval=float(
                    getattr(config.interaction, "edit_min_interval_seconds", 2.0)
                ),
            )
            interaction_renderer._runtime_progress = runtime_progress
    controller.set_interaction_renderer(interaction_renderer)
    if claude_backend is not None:
        controller.set_orchestration_runner(
            OrchestrationRunner(
                task_service=task_service,
                codex_backend=backend,
                claude_backend=claude_backend,
                ledger=ledger,
                interaction_renderer=interaction_renderer,
                runtime_event_store=runtime_store,
            )
        )
    event_bridge = EventBridge(
        task_service=task_service,
        backend=backend,
        ledger=ledger,
        send_telegram=handlers.send_telegram,
        edit_telegram=handlers.edit_telegram,
        approval_service=approval_service,
        task_watchdog=task_watchdog,
        watchdog_interval_seconds=config.task.watchdog_interval_seconds,
        interaction_renderer=interaction_renderer,
        runtime_event_store=runtime_store,
        on_workspace_freed=controller.process_queued_runs,
        codex_implementer_enabled=codex_implementer_enabled,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        pump_task = asyncio.create_task(event_bridge.run(), name="event-pump")
        # Outbox delivery task — processes queued Telegram sends/edits with retry.
        async def _process_outbox() -> None:
            while True:
                try:
                    await telegram_outbox.process_all()
                except Exception:
                    logger.debug("Outbox process error", exc_info=True)
                await asyncio.sleep(0.5)
        outbox_task = asyncio.create_task(_process_outbox(), name="outbox-processor")
        app_initialized = False
        app_started = False
        updater_started = False
        logger.info("WLCodex starting. Polling Telegram...")
        try:
            # Initialize with infinite bootstrap retry for Telegram network errors.
            initialized = await _initialize_app_with_retry(
                app, runtime_store=runtime_store,
            )
            if not initialized:
                logger.critical(
                    "Cannot connect to Telegram Bot API — permanent error. "
                    "Check bot token. Exiting."
                )
                return
            app_initialized = True
            # Register Telegram BotCommands menu
            if getattr(config, "menu", None) and getattr(config.menu, "register_bot_commands", False):
                from telegram import BotCommand
                commands = [
                    BotCommand(cmd, desc)
                    for cmd, desc in build_bot_commands(
                        getattr(config.interaction, "profile", "natural")
                    )
                ]
                await app.bot.set_my_commands(commands)
                logger.info("Registered %d bot commands in Telegram menu", len(commands))
            await app.start()
            app_started = True
            await app.updater.start_polling()
            updater_started = True
            # Recovery: notify about tasks paused by restart
            if paused_ids:
                await notify_recovery_paused_tasks(
                    ledger=ledger,
                    paused_ids=paused_ids,
                    send_telegram=handlers.send_telegram,
                )
            # Keep running until cancelled
            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            logger.info("Shutting down.")
        finally:
            # 1. Stop Telegram polling
            await _shutdown_telegram_app(
                app,
                app_initialized=app_initialized,
                app_started=app_started,
                updater_started=updater_started,
            )
            # 2. Stop event pump
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            # 2b. Stop outbox processor
            outbox_task.cancel()
            try:
                await outbox_task
            except asyncio.CancelledError:
                pass
            # 3. Close WebSocket and cancel pending JSON-RPC requests
            if hasattr(backend, "close"):
                await backend.close()
            # 4. Stop managed app-server process (only if we started it)
            if _managed_process is not None and not _managed_process.external_process:
                _managed_process.shutdown()

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


async def _shutdown_telegram_app(
    app: object,
    *,
    app_initialized: bool,
    app_started: bool,
    updater_started: bool,
) -> None:
    if updater_started or getattr(app.updater, "running", False):
        try:
            await app.updater.stop()
        except RuntimeError:
            logger.warning("Telegram updater was not running during shutdown")
    if app_started or getattr(app, "running", False):
        try:
            await app.stop()
        except RuntimeError:
            logger.warning("Telegram application was not running during shutdown")
    if app_initialized:
        await app.shutdown()


if __name__ == "__main__":
    main()
