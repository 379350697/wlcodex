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
from wlcodex.codex_runtime_source import CodexRuntimeSource
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
        interaction_renderer: object | None = None,
        runtime_event_store: object | None = None,
    ) -> None:
        self._service = task_service
        self._backend = backend
        self._ledger = ledger
        self._send_telegram = send_telegram
        self._edit_telegram = edit_telegram
        self._approval_service = approval_service
        self._task_watchdog = task_watchdog
        self._watchdog_interval = watchdog_interval_seconds
        self._interaction_renderer = interaction_renderer
        self._runtime_store = runtime_event_store
        self._status_card_fingerprints: dict[int, tuple[str, str]] = {}
        self._runtime_causation_by_agent_run: dict[int, int] = {}
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

        task = self._task_for_runtime_event(event, thread_id, task_before)
        self._append_runtime_events(event, task)

        # Forward agent message deltas to interaction renderer (before status-card skip)
        if event.event_type == "agent_message_delta":
            await self._forward_agent_delta(event)

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
                await self._forward_terminal_event(task_after)
                await drain_workspace(
                    self._service, self._backend, task_after.workspace_alias
                )

    def _task_for_runtime_event(
        self,
        event: BackendEvent,
        thread_id: str,
        task_before: object | None,
    ) -> object | None:
        if thread_id:
            return self._service._find_by_thread(thread_id)
        codex_request_id = str(event.payload.get("codexRequestId", ""))
        if codex_request_id:
            approval = self._ledger.get_approval_by_codex_id(codex_request_id)
            if approval is not None:
                try:
                    return self._ledger.get_task(approval.task_id)
                except KeyError:
                    return None
        return task_before

    def _append_runtime_events(self, event: BackendEvent, task: object | None) -> None:
        """Append Codex backend events to runtime_events for non-orchestrated tasks."""
        if self._runtime_store is None or task is None:
            return
        task_id = getattr(task, "id", None)
        if task_id is None:
            return
        if self._service.is_orchestration_managed_task(int(task_id)):
            return
        context = self._runtime_context_for_task(int(task_id))
        if context is None:
            return
        source = CodexRuntimeSource(
            correlation_id=context["correlation_id"],
            agent_run_id=context["agent_run_id"],
            conversation_id=context["conversation_id"],
            orchestration_run_id=context["orchestration_run_id"],
            task_id=int(task_id),
        )
        last_id = self._runtime_causation_by_agent_run.get(context["agent_run_id"])
        for runtime_event in source.map_event(event, causation_id=last_id):
            stored = self._runtime_store.append(runtime_event)
            self._runtime_causation_by_agent_run[context["agent_run_id"]] = stored.id

    def _runtime_context_for_task(self, task_id: int) -> dict[str, int | str] | None:
        row = self._ledger._conn.execute(
            """
            SELECT c.id AS conversation_id,
                   ar.id AS agent_run_id,
                   ar.role AS role,
                   o.id AS orchestration_run_id
            FROM conversation_sessions AS c
            LEFT JOIN agent_runs AS ar
              ON ar.conversation_id = c.id
             AND ar.agent = 'codex'
             AND (ar.hidden_task_id = ? OR ar.hidden_task_id IS NULL)
            LEFT JOIN orchestration_runs AS o
              ON o.conversation_id = c.id
             AND o.status = 'running'
            WHERE c.active_codex_task_id = ?
            ORDER BY ar.id DESC, o.id DESC
            LIMIT 1
            """,
            (task_id, task_id),
        ).fetchone()
        if row is None or row["agent_run_id"] is None:
            return None
        correlation_id = f"codex-task-{task_id}"
        last_event = self._runtime_store._conn.execute(
            """
            SELECT correlation_id FROM runtime_events
            WHERE task_id = ?
              AND correlation_id NOT LIKE 'telegram-%'
              AND correlation_id NOT LIKE 'watchdog-%'
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if last_event is not None:
            correlation_id = str(last_event["correlation_id"])
        return {
            "conversation_id": int(row["conversation_id"]),
            "agent_run_id": int(row["agent_run_id"]),
            "orchestration_run_id": (
                int(row["orchestration_run_id"])
                if row["orchestration_run_id"] is not None else None
            ),
            "correlation_id": correlation_id,
        }

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


    async def _forward_agent_delta(self, event: BackendEvent) -> None:
        if self._interaction_renderer is None:
            return
        thread_id = str(event.payload.get("threadId", ""))
        task = self._service._find_by_thread(thread_id)
        if task is None or task.telegram_chat_id is None:
            return
        if self._service.is_orchestration_managed_task(task.id):
            return
        delta = str(event.payload.get("delta", ""))
        if not delta:
            return
        from wlcodex.interaction.events import InteractionEvent

        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="text_delta",
                chat_id=task.telegram_chat_id,
                task_id=task.id,
                thread_id=thread_id,
                text=delta,
            )
        )

    async def _forward_terminal_event(self, task) -> None:
        if self._interaction_renderer is None:
            return
        if task.telegram_chat_id is None:
            return
        from wlcodex.interaction.events import InteractionEvent

        event_type = "run_completed" if task.status == TaskStatus.DONE else "run_failed"
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type=event_type,
                chat_id=task.telegram_chat_id,
                task_id=task.id,
                thread_id=task.codex_thread_id or "",
                text=task.last_error or "",
                metadata={"has_diff": bool(task.changed_file_count)},
            )
        )


def _should_refresh_status_card(event: BackendEvent, status: TaskStatus) -> bool:
    if event.event_type == "turn_started":
        return True
    if status in TERMINAL_TASK_STATUSES:
        return True
    return False
