"""Background event pump — consumes backend.events(), updates internal state,
sends Telegram approval buttons, and records runtime events.

Status/log data is NEVER fed back into Codex context.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from wlcodex.codex_backend import BackendEvent
from wlcodex.codex_runtime_source import CodexRuntimeSource
from wlcodex.db import Ledger
from wlcodex.models import TaskStatus
from wlcodex.status import render_approval_card
from wlcodex.task_service import TaskService, drain_workspace
from wlcodex.telegram_digest import render_auto_draft_digest

logger = logging.getLogger(__name__)

EXPIRY_SCAN_INTERVAL_SECONDS = 60
TASK_WATCHDOG_INTERVAL_SECONDS = 60
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
        on_workspace_freed: Callable[[str], Coroutine[Any, Any, None]] | None = None,
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
        self._on_workspace_freed = on_workspace_freed
        self._runtime_causation_by_agent_run: dict[int, int] = {}
        self._running = False

    async def run(self) -> None:
        """Run the event loop until cancelled.

        Processes backend events, periodically gives the approval service a
        chance to handle stale approval bookkeeping without ending live holds,
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
                        if self._on_workspace_freed is not None:
                            await self._on_workspace_freed(ws_alias)
            except Exception:
                logger.exception("Task watchdog scan failed")

    async def _expire_stale(self) -> None:
        """Run non-terminal stale approval maintenance."""
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

        # Forward agent message deltas to the interaction renderer.
        if event.event_type == "agent_message_delta":
            await self._forward_agent_delta(event)

        # Handle approval — send Telegram buttons
        if event.event_type == "approval_requested":
            await self._on_approval_requested(event)

        # Trigger queue drain when a task reaches terminal state
        if event.event_type in ("turn_completed", "thread_status_changed") and task_before:
            task_after = self._service._find_by_thread(thread_id)
            if task_after and task_after.status in (
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.ABORTED,
            ):
                self._sync_direct_agent_run_status(task_after)
                # Check for staged-auto workflow transitions
                advanced_stage = self._advance_staged_auto_on_completion(task_after)
                if advanced_stage:
                    await self._send_auto_stage_buttons(task_after, advanced_stage)
                else:
                    await self._forward_terminal_event(task_after)
                await drain_workspace(
                    self._service, self._backend, task_after.workspace_alias
                )
                if self._on_workspace_freed is not None:
                    await self._on_workspace_freed(task_after.workspace_alias)

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

    def _task_agent_message_summary(self, task: object) -> str:
        """Return the assembled agent message text for a completed direct task."""
        task_id = getattr(task, "id", None)
        if task_id is None:
            return str(getattr(task, "last_summary", "") or "")

        chunks: list[str] = []
        for event in self._ledger.list_events(int(task_id), limit=1000):
            if event.event_type != "agent_message_delta":
                continue
            delta = str(event.payload.get("delta", "") or "")
            if delta:
                chunks.append(delta)

        assembled = "".join(chunks).strip()
        if assembled:
            return assembled
        return str(getattr(task, "last_summary", "") or "").strip()

    def _sync_direct_agent_run_status(self, task: object) -> None:
        task_id = getattr(task, "id", None)
        if task_id is None or self._service.is_orchestration_managed_task(int(task_id)):
            return
        status_by_task = {
            TaskStatus.DONE: "done",
            TaskStatus.FAILED: "failed",
            TaskStatus.ABORTED: "aborted",
        }
        agent_status = status_by_task.get(getattr(task, "status", None))
        if agent_status is None:
            return

        rows = self._ledger._conn.execute(
            """
            SELECT id, status, role FROM agent_runs
            WHERE hidden_task_id = ?
            ORDER BY id ASC
            """,
            (int(task_id),),
        ).fetchall()
        for row in rows:
            if row["status"] in {"done", "failed", "aborted"}:
                continue
            summary = (
                getattr(task, "last_error", "")
                if agent_status in {"failed", "aborted"}
                else self._task_agent_message_summary(task)
            )
            self._ledger.update_agent_run_status(
                int(row["id"]),
                agent_status,
                completion_summary=str(summary)[:5000],
            )
            self._append_direct_agent_terminal_event(
                task,
                agent_run_id=int(row["id"]),
                agent_status=agent_status,
                role=str(row["role"] or "implementation"),
                summary=str(summary)[:5000],
            )

    def _append_direct_agent_terminal_event(
        self,
        task: object,
        *,
        agent_run_id: int,
        agent_status: str,
        role: str,
        summary: str,
    ) -> None:
        if self._runtime_store is None:
            return
        task_id = int(getattr(task, "id"))
        context = self._runtime_context_for_task(task_id)
        if context is None:
            return
        from wlcodex.runtime_events import (
            SCHEMA_VERSION,
            AggregateType,
            EventSource,
            EventType,
            RuntimeEvent,
            Visibility,
            now_iso,
        )

        event_type = (
            EventType.AGENT_RUN_COMPLETED
            if agent_status == "done"
            else EventType.AGENT_RUN_FAILED
        )
        last_id = self._runtime_causation_by_agent_run.get(agent_run_id)
        if last_id is None:
            row = self._runtime_store._conn.execute(
                """
                SELECT id FROM runtime_events
                WHERE agent_run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (agent_run_id,),
            ).fetchone()
            if row is not None:
                last_id = int(row["id"])
        stored = self._runtime_store.append(
            RuntimeEvent(
                schema_version=SCHEMA_VERSION,
                event_type=event_type,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=str(context["correlation_id"]),
                causation_id=last_id,
                source=EventSource.CODEX,
                actor="codex",
                visibility=Visibility.OPERATOR,
                payload={
                    "agent": "codex",
                    "role": role,
                    "summary": summary,
                    "completion_summary": summary,
                },
                occurred_at=now_iso(),
                conversation_id=int(context["conversation_id"]),
                orchestration_run_id=(
                    int(context["orchestration_run_id"])
                    if context["orchestration_run_id"] is not None else None
                ),
                agent_run_id=agent_run_id,
                task_id=task_id,
            )
        )
        self._runtime_causation_by_agent_run[agent_run_id] = stored.id

    def _advance_staged_auto_on_completion(self, task: object) -> str | None:
        """When a direct agent run completes, check if it belongs to a staged-auto
        workflow and advance the orchestration run to the next needs_user stage.

        Returns the new current_step if a transition occurred, None otherwise.

        This implements the stage transition logic:
        - auto_analysis/auto_final_plan completion → draft_ready (needs_user)
        - auto_verification pass → completed (needs_user)
        - auto_verification fail → retry_ready (needs_user)
        - auto_implementation/auto_repair completion → claude_done (needs_user)
        - auto_codex_takeover completion → completed (needs_user)
        """
        from wlcodex.auto_workflow import (
            AUTO_COLLECTING_CONTEXT,
            AUTO_DRAFT_READY,
            AUTO_CLAUDE_DONE,
            AUTO_VERIFYING,
            AUTO_RETRY_READY,
            AUTO_CODEX_TAKEOVER_RUNNING,
            AUTO_COMPLETED,
        )

        task_id = int(getattr(task, "id"))
        rows = self._ledger._conn.execute(
            """
            SELECT id, role, status, completion_summary FROM agent_runs
            WHERE hidden_task_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchall()
        if not rows:
            return None

        agent_role = str(rows[0]["role"] or "")
        agent_status = str(rows[0]["status"] or "")
        completion_summary = str(rows[0]["completion_summary"] or "")

        # Find the conversation for this task
        conv_row = self._ledger._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE active_codex_task_id = ? OR active_claude_run_id = (
                SELECT id FROM agent_runs WHERE hidden_task_id = ? LIMIT 1
            )
            ORDER BY id DESC LIMIT 1
            """,
            (task_id, task_id),
        ).fetchone()
        if conv_row is None:
            return None
        conversation_id = int(conv_row["id"])

        # Find the latest active auto run
        auto_run = self._ledger.get_latest_active_auto_run(conversation_id)
        if auto_run is None:
            return None

        current_step = auto_run.current_step
        new_step: str | None = None

        # Advance based on agent role and completion
        if agent_role == "auto_analysis" and agent_status == "done":
            # Context collection or supplement completed → stay in collecting_context
            # The user can still add more context and now click "生成最终方案".
            new_step = AUTO_COLLECTING_CONTEXT
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="needs_user",
                current_step=new_step,
            )

        elif agent_role == "auto_final_plan" and agent_status == "done":
            # Final plan generated → advance to draft_ready
            new_step = AUTO_DRAFT_READY
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="needs_user",
                current_step=new_step,
                last_codex_analysis=completion_summary[:5000] if completion_summary else "",
            )

        elif agent_role in ("auto_implementation", "auto_repair") and agent_status == "done":
            # Claude implementation completed → advance to claude_done
            new_step = AUTO_CLAUDE_DONE
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="needs_user",
                current_step=new_step,
                last_claude_summary=completion_summary[:5000] if completion_summary else "",
            )

        elif agent_role == "auto_verification" and agent_status == "done":
            # Codex verification completed → check pass/fail
            summary_lower = completion_summary.lower()
            if "decision: pass" in summary_lower or "decision:pass" in summary_lower:
                new_step = AUTO_COMPLETED
                self._ledger.update_orchestration_run(
                    auto_run.id,
                    status="needs_user",
                    current_step=new_step,
                    last_verification_result=completion_summary[:5000] if completion_summary else "",
                )
            else:
                new_step = AUTO_RETRY_READY
                self._ledger.update_orchestration_run(
                    auto_run.id,
                    status="needs_user",
                    current_step=new_step,
                    last_verification_result=completion_summary[:5000] if completion_summary else "",
                )

        elif agent_role == "auto_codex_takeover" and agent_status == "done":
            # Codex takeover completed → advance to completed
            new_step = AUTO_COMPLETED
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="passed",
                current_step=new_step,
                last_codex_analysis=completion_summary[:5000] if completion_summary else "",
            )

        return new_step

    async def _send_auto_stage_buttons(
        self, task: object, new_stage: str
    ) -> None:
        """Send stage-appropriate buttons to Telegram after a stage transition."""
        chat_id = getattr(task, "telegram_chat_id", None)
        if chat_id is None:
            return
        task_id = int(getattr(task, "id"))
        # Find conversation
        conv_row = self._ledger._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE active_codex_task_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if conv_row is None:
            return
        conversation_id = int(conv_row["id"])
        auto_run = self._ledger.get_latest_active_auto_run(conversation_id)
        if auto_run is None:
            return

        from wlcodex.auto_workflow import build_auto_stage_buttons, auto_stage_label

        buttons = build_auto_stage_buttons(
            conversation_id, new_stage,
            last_codex_analysis=auto_run.last_codex_analysis or "",
        )
        # Include orch run data in the message for draft_ready
        stage_label = auto_stage_label(new_stage)
        if new_stage == "collecting_context":
            text = (
                "Codex 已完成上下文收集。\n\n"
                "你可以继续补充信息，或生成最终方案。"
            )
        elif new_stage == "draft_ready" and auto_run.last_codex_analysis:
            digest = render_auto_draft_digest(auto_run.last_codex_analysis)
            text = f"最终方案已生成。\n\n{digest}\n\n请选择下一步："
        elif new_stage == "draft_ready":
            text = (
                "最终方案生成完成，但没有收到方案正文。\n\n"
                "为避免黑盒执行，暂不提供 Claude 执行入口。"
                "请重写方案或继续补充上下文。"
            )
        elif new_stage == "claude_done":
            digest = render_auto_draft_digest(auto_run.last_claude_summary or "结论：完成。")
            text = f"Claude 执行完成。\n\n{digest}\n\n请选择下一步："
        elif new_stage == "completed":
            text = "验收通过，任务完成。"
        elif new_stage == "retry_ready":
            digest = render_auto_draft_digest(
                auto_run.last_verification_result or "结论：验收未通过。"
            )
            text = f"验收未通过。\n\n{digest}\n\n请选择下一步："
        else:
            text = f"阶段：{stage_label}\n\n请选择下一步："

        try:
            await self._send_telegram(chat_id, text, buttons)
        except Exception:
            logger.exception("Failed to send auto stage buttons")
