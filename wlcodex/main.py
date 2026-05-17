from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from wlcodex.app_server_process import AppServerProcess, AppServerProcessConfig
from wlcodex.approval import ApprovalService
from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
from wlcodex.codex_backend import AppServerCodexBackend, FakeCodexBackend
from wlcodex.config import load_config
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.event_bridge import EventBridge
from wlcodex.inspection import TaskInspector
from wlcodex.menu import build_bot_commands
from wlcodex.recovery_notifications import notify_recovery_paused_tasks
from wlcodex.task_service import TaskService
from wlcodex.telegram_app import build_application
from wlcodex.watchdog import TaskLivenessConfig, TaskWatchdog

logger = logging.getLogger(__name__)


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

    # Recovery: pause any active tasks from previous run
    paused_ids = ledger.mark_active_tasks_recovery_paused()
    if paused_ids:
        logger.info(
            "Recovery: paused %d active task(s): %s", len(paused_ids), paused_ids
        )

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

    # Claude backend (adapter for Claude Code subprocess)
    claude_backend = None
    if config.claude.enabled:
        claude_backend = ClaudeBackend(ClaudeConfig(
            enabled=config.claude.enabled,
            binary=config.claude.binary,
            startup_timeout_seconds=config.claude.startup_timeout_seconds,
            request_timeout_seconds=config.claude.request_timeout_seconds,
        ))
        logger.info("Claude backend enabled (binary: %s)", config.claude.binary)
    else:
        logger.info("Claude backend disabled (set claude.enabled = true to enable)")

    # Controller
    controller = CommandController(
        task_service, backend, inspector,
        ledger=ledger, claude_backend=claude_backend,
        default_mode=config.conversation.default_mode,
        default_workspace=config.conversation.default_workspace,
    )

    # Telegram app
    app, handlers = build_application(config, token, controller, ledger, approval_service)

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
    task_watchdog = TaskWatchdog(ledger, backend, liveness_config)
    event_bridge = EventBridge(
        task_service=task_service,
        backend=backend,
        ledger=ledger,
        send_telegram=handlers.send_telegram,
        edit_telegram=handlers.edit_telegram,
        approval_service=approval_service,
        task_watchdog=task_watchdog,
        watchdog_interval_seconds=config.task.watchdog_interval_seconds,
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
            await app.initialize()
            app_initialized = True
            # Register Telegram BotCommands menu
            if getattr(config, "menu", None) and getattr(config.menu, "register_bot_commands", False):
                from telegram import BotCommand
                commands = [
                    BotCommand(cmd, desc) for cmd, desc in build_bot_commands()
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
