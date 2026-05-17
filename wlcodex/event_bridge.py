"""Background event pump — consumes backend.events(), updates task state,
sends Telegram approval buttons, and edits status cards.

Status/log data is NEVER fed back into Codex context.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from wlcodex.codex_backend import BackendEvent
from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.status import render_approval_card, render_task_card
from wlcodex.task_service import TaskService, drain_workspace

logger = logging.getLogger(__name__)

EXPIRY_SCAN_INTERVAL_SECONDS = 60
TASK_WATCHDOG_INTERVAL_SECONDS = 60
TERMINAL_TASK_STATUSES = {
    TaskStatus.DONE,
    TaskStatus.FAILED,
    TaskStatus.ABORTED,
}

# Callback: send_telegram(chat_id, text, buttons) -> message_id
SendTelegram = Callable[[int, str, list[list[dict[str, str]]] | None], Coroutine[Any, Any, int]]
# Callback: edit_telegram(chat_id, message_id, text, buttons=None) -> None
EditTelegram = Callable[[int, int, str, list[list[dict[str, str]]] | None], Coroutine[Any, Any, None]]


class EventBridge:
    """Consumes backend events and drives local state + Telegram notifications."""

    def __init__(
        self,
        task_service: TaskService,
        backend: object,
        ledger: Ledger,
        send_telegram: SendTelegram,
        edit_telegram: EditTelegram,
        approval_service: object,
        task_watchdog: object | None = None,
        watchdog_interval_seconds: int = TASK_WATCHDOG_INTERVAL_SECONDS,
    ) -> None:
        self._service = task_service
        self._backend = backend
        self._ledger = ledger
        self._send_telegram = send_telegram
        self._edit_telegram = edit_telegram
        self._approval_service = approval_service
        self._task_watchdog = task_watchdog
        self._watchdog_interval = watchdog_interval_seconds
        self._status_card_fingerprints: dict[int, tuple[str, str]] = {}
        self._running = False

    async def run(self) -> None:
        """Run the event loop until cancelled.

        Processes backend events, periodically expires stale approvals
        so the Codex app-server is never stuck waiting on an expired hold,
        and runs the task liveness watchdog when configured.
        """
        self._running = True
        expiry_task = asyncio.create_task(
            self._expiry_loop(), name="approval-expiry-scan"
        )
        watchdog_task: asyncio.Task[None] | None = None
        if self._task_watchdog is not None:
            watchdog_task = asyncio.create_task(
                self._task_watchdog_loop(), name="task-liveness-watchdog"
            )
        try:
            async for event in self._backend.events():
                await self.process_event(event)
        except asyncio.CancelledError:
            pass
        finally:
            expiry_task.cancel()
            try:
                await expiry_task
            except asyncio.CancelledError:
                pass
            if watchdog_task is not None:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            self._running = False

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(EXPIRY_SCAN_INTERVAL_SECONDS)
            await self._expire_stale()

    async def _task_watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self._watchdog_interval)
            try:
                changed = self._task_watchdog.scan_once()
                if changed > 0:
                    for ws_alias in list(self._service._workspaces):
                        await drain_workspace(self._service, self._backend, ws_alias)
            except Exception:
                logger.exception("Task watchdog scan failed")

    async def _expire_stale(self) -> None:
        """Expire any pending approvals past the callback timeout."""
        try:
            await self._approval_service.expire_stale_approvals(
                self._ledger, self._backend
            )
        except Exception:
            logger.exception("Stale approval expiry scan failed")

    async def process_event(self, event: BackendEvent) -> None:
        """Process a single backend event."""
        thread_id = str(event.payload.get("threadId", ""))
        task_before = self._service._find_by_thread(thread_id) if thread_id else None

        try:
            self._service.apply_backend_event(event)
        except Exception:
            logger.exception("Failed to apply backend event: %s", event.event_type)
            return

        # Handle approval — send Telegram buttons
        if event.event_type == "approval_requested":
            await self._on_approval_requested(event)

        # Update status card
        await self._update_status_card(event)

        # Trigger queue drain when a task reaches terminal state
        if event.event_type in ("turn_completed", "thread_status_changed") and task_before:
            task_after = self._service._find_by_thread(thread_id)
            if task_after and task_after.status in (
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.ABORTED,
            ):
                await drain_workspace(
                    self._service, self._backend, task_after.workspace_alias
                )

    async def _on_approval_requested(self, event: BackendEvent) -> None:
        payload = event.payload
        thread_id = str(payload.get("threadId", ""))
        codex_request_id = str(payload.get("codexRequestId", ""))
        kind = str(payload.get("kind", "command"))

        task = self._service._find_by_thread(thread_id)
        if task is None:
            logger.warning("approval_requested for unknown thread: %s", thread_id)
            return

        if task.telegram_chat_id is None:
            logger.warning("task #%d has no telegram_chat_id", task.id)
            return

        # Find the approval row just created by apply_backend_event
        approval = self._ledger.get_approval_by_codex_id(
            codex_request_id, task_id=task.id
        )
        if approval is None:
            logger.warning("No approval row for codex_request_id: %s", codex_request_id)
            return

        from wlcodex.approval import encode_approval_callback

        allow_session = getattr(self._approval_service, "_allow_session_approval", True)

        if allow_session:
            buttons = [[
                {"text": "批准一次", "callback_data": encode_approval_callback(approval.id, "approve_once")},
                {"text": "本会话批准", "callback_data": encode_approval_callback(approval.id, "approve_session")},
            ], [
                {"text": "拒绝", "callback_data": encode_approval_callback(approval.id, "deny")},
                {"text": "取消", "callback_data": encode_approval_callback(approval.id, "cancel")},
            ]]
        else:
            buttons = [[
                {"text": "批准", "callback_data": encode_approval_callback(approval.id, "approve_once")},
                {"text": "拒绝", "callback_data": encode_approval_callback(approval.id, "deny")},
            ], [
                {"text": "取消", "callback_data": encode_approval_callback(approval.id, "cancel")},
            ]]

        card = render_approval_card(task.id, approval.id, kind, approval.summary)
        try:
            msg_id = await self._send_telegram(task.telegram_chat_id, card, buttons)
            # Store the telegram message id on the approval row (via a direct update)
            self._ledger._conn.execute(
                "UPDATE approval_requests SET telegram_message_id = ? WHERE id = ?",
                (msg_id, approval.id),
            )
            self._ledger._conn.commit()
        except Exception:
            logger.exception("Failed to send approval Telegram message")

    async def _update_status_card(self, event: BackendEvent) -> None:
        """Edit the task's status card if it has one."""
        thread_id = str(event.payload.get("threadId", ""))
        task = self._service._find_by_thread(thread_id)
        if task is None:
            return
        if task.telegram_chat_id is None or task.telegram_status_message_id is None:
            return

        # Throttle: skip noisy deltas and approval/status churn.  Approval
        # requests already get their own card; active phase changes tend to
        # produce Telegram noise without adding useful action.
        if event.event_type in (
            "command_output_delta",
            "agent_message_delta",
            "item_started",
            "item_completed",
            "approval_requested",
        ):
            return

        try:
            current = self._service.get_task(task.id)
            if not _should_refresh_status_card(event, current.status):
                return
            text = render_task_card(current)
            buttons = None
            # Attach worktree post-completion buttons when a worktree task
            # reaches a terminal state.
            if (
                current.worktree_path
                and current.status in TERMINAL_TASK_STATUSES
            ):
                from wlcodex.controller import _build_worktree_done_buttons
                buttons = _build_worktree_done_buttons(current.id)
            fingerprint = (
                text,
                json.dumps(buttons or [], ensure_ascii=False, sort_keys=True),
            )
            if self._status_card_fingerprints.get(current.id) == fingerprint:
                return
            await self._edit_telegram(
                task.telegram_chat_id, task.telegram_status_message_id, text,
                buttons=buttons,
            )
            self._status_card_fingerprints[current.id] = fingerprint
        except Exception:
            logger.exception("Failed to edit status card for task #%d", task.id)


def _should_refresh_status_card(event: BackendEvent, status: TaskStatus) -> bool:
    if event.event_type == "turn_started":
        return True
    if status in TERMINAL_TASK_STATUSES:
        return True
    return False
