from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from wlcodex.app_server_process import AppServerProcess, AppServerProcessConfig
from wlcodex.approval import ApprovalService
from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
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
from wlcodex.telegram_app import build_application
from wlcodex.watchdog import TaskLivenessConfig, TaskWatchdog

logger = logging.getLogger(__name__)

# Startup retry configuration for Telegram Bot API initialization.
_INITIALIZE_MAX_RETRIES = 3
_INITIALIZE_BACKOFF_BASE = 1.5  # seconds, exponential: 1.5, 2.25, 3.375


async def _initialize_app_with_retry(
    app: object,
    max_retries: int = _INITIALIZE_MAX_RETRIES,
    backoff_base: float = _INITIALIZE_BACKOFF_BASE,
) -> bool:
    """Call app.initialize() with retry/backoff for transient network errors.

    Returns True on success, False if all retries are exhausted.
    Does NOT leak the bot token in log messages.
    """
    import asyncio as _asyncio
    from telegram.error import NetworkError, TimedOut

    for attempt in range(1, max_retries + 1):
        try:
            await app.initialize()
            return True
        except (TimedOut, NetworkError) as exc:
            if attempt < max_retries:
                delay = backoff_base ** attempt
                logger.warning(
                    "Telegram initialize attempt %d/%d timed out, retrying in %.1fs",
                    attempt, max_retries, delay,
                )
                await _asyncio.sleep(delay)
            else:
                logger.error(
                    "Telegram initialize failed after %d attempts: %s",
                    max_retries, exc,
                )
        except Exception as exc:
            # Non-network errors are likely permanent (bad token, etc.) —
            # don't retry.
            logger.error("Telegram initialize failed with non-network error: %s", exc)
            return False

    return False


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
        claude_backend = ClaudeBackend(ClaudeConfig(
            enabled=config.claude.enabled,
            binary=config.claude.binary,
            startup_timeout_seconds=config.claude.startup_timeout_seconds,
            request_timeout_seconds=config.claude.request_timeout_seconds,
            stream_idle_timeout_seconds=config.claude.stream_idle_timeout_seconds,
            permission_mode=claude_permission_mode,
            model=config.claude.model,
            effort=config.claude.effort,
        ), permission_state=claude_permission_state)
        logger.info(
            "Claude backend enabled (binary: %s, model: %s, effort: %s, permission: %s)",
            config.claude.binary,
            config.claude.model,
            config.claude.effort,
            claude_permission_label(claude_permission_mode),
        )
    else:
        logger.info("Claude backend disabled (set claude.enabled = true to enable)")

    # Controller
    controller = CommandController(
        task_service, backend, inspector,
        ledger=ledger, claude_backend=claude_backend,
        claude_permission_state=claude_permission_state,
        default_mode=config.conversation.default_mode,
        default_workspace=config.conversation.default_workspace,
        runtime_event_store=runtime_store,
    )

    # Telegram app
    app, handlers = build_application(
        config,
        token,
        controller,
        ledger,
        approval_service,
        runtime_event_store=runtime_store,
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
        # Wire runtime progress manager for deterministic progress messages
        from wlcodex.interaction.runtime_renderer import RuntimeProgressManager
        from wlcodex.interaction.transport import TelegramTransport

        async def _noop_typing(_chat_id: int) -> object:
            return None

        runtime_progress = RuntimeProgressManager(
            transport=TelegramTransport(
                handlers.send_telegram,
                handlers.edit_telegram,
                _noop_typing,
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
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run() -> None:
        pump_task = asyncio.create_task(event_bridge.run(), name="event-pump")
        app_initialized = False
        app_started = False
        updater_started = False
        logger.info("WLCodex starting. Polling Telegram...")
        try:
            # Initialize with retry for transient Telegram network errors.
            initialized = await _initialize_app_with_retry(app)
            if not initialized:
                logger.critical(
                    "Cannot connect to Telegram Bot API after retries. "
                    "Check network and bot token. Exiting to avoid restart loop."
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
                    edit_telegram=handlers.edit_telegram,
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
