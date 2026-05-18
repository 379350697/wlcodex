from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
from collections.abc import Callable
from typing import Any

from wlcodex.context_packets import ContextBudget, trim_to_budget
from wlcodex.interaction.events import InteractionEvent
from wlcodex.models import ConversationSession, TaskStatus
from wlcodex.orchestrator import ChiefEngineerOrchestrator, OrchestrationProgress
from wlcodex.task_service import TaskService

logger = logging.getLogger(__name__)


def _accepts_keyword(func: object, name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == name
        for parameter in signature.parameters.values()
    )


class _TaskBoundCodexBackend:
    def __init__(self, backend: object, service: TaskService, task_id: int) -> None:
        self._backend = backend
        self._service = service
        self._task_id = task_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._backend, name)

    def _bind_thread(self, thread_id: str) -> None:
        self._service.set_task_thread(self._task_id, thread_id)

    async def send_codex_prompt(
        self,
        workspace_path: str,
        prompt: str,
        **kwargs: object,
    ) -> str:
        send_codex_prompt = getattr(self._backend, "send_codex_prompt")
        supported_kwargs = {
            key: value
            for key, value in kwargs.items()
            if _accepts_keyword(send_codex_prompt, key)
        }
        return await send_codex_prompt(
            workspace_path,
            prompt,
            on_thread_created=self._bind_thread,
            **supported_kwargs,
        )


class OrchestrationRunner:
    """Owns long-running chief-engineer orchestration outside Telegram handlers."""

    def __init__(
        self,
        *,
        task_service: TaskService,
        codex_backend: object,
        claude_backend: object,
        ledger: object,
        interaction_renderer: object | None = None,
        orchestrator_factory: Callable[[object, object], object] | None = None,
    ) -> None:
        self._service = task_service
        self._codex = codex_backend
        self._claude = claude_backend
        self._ledger = ledger
        self._interaction_renderer = interaction_renderer
        self._orchestrator_factory = orchestrator_factory or ChiefEngineerOrchestrator
        self._tasks: set[asyncio.Task[None]] = set()

    def start_chief_engineer(
        self,
        *,
        prompt: str,
        conversation: ConversationSession,
        task_id: int,
        orchestration_run_id: int,
        codex_analysis_run_id: int,
        chat_id: int,
        workspace_path: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._run_chief_engineer(
                prompt=prompt,
                conversation=conversation,
                task_id=task_id,
                orchestration_run_id=orchestration_run_id,
                codex_analysis_run_id=codex_analysis_run_id,
                chat_id=chat_id,
                workspace_path=workspace_path,
            ),
            name=f"chief-engineer-{orchestration_run_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_unexpected_failure)
        return task

    def _log_unexpected_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Background orchestration crashed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _run_chief_engineer(
        self,
        *,
        prompt: str,
        conversation: ConversationSession,
        task_id: int,
        orchestration_run_id: int,
        codex_analysis_run_id: int,
        chat_id: int,
        workspace_path: str,
    ) -> None:
        codex = _TaskBoundCodexBackend(self._codex, self._service, task_id)
        orch = self._orchestrator_factory(codex, self._claude)

        terminal_sent = False
        orch_result_status = "running"
        verify_round = 0
        codex_analysis_text = ""
        claude_implementation_text = ""
        verification_text = ""
        terminal_text = ""
        codex_analysis_status = "running"

        try:
            async for progress in orch.run_streaming(
                prompt,
                conversation_context={"workspace": workspace_path},
            ):
                if terminal_sent:
                    continue

                if progress.phase == OrchestrationProgress.IMPL_DELTA:
                    if progress.text:
                        claude_implementation_text += progress.text
                        await self._emit_text_delta(
                            chat_id, task_id, conversation.id, progress.text
                        )
                elif progress.phase in (
                    OrchestrationProgress.ANALYSIS_STARTED,
                    OrchestrationProgress.ANALYSIS_COMPLETE,
                    OrchestrationProgress.IMPL_COMPLETE,
                    OrchestrationProgress.VERIFY_STARTED,
                    OrchestrationProgress.VERIFY_COMPLETE,
                ):
                    await self._handle_phase_progress(
                        progress=progress,
                        chat_id=chat_id,
                        task_id=task_id,
                        conversation_id=conversation.id,
                    )
                    if progress.phase == OrchestrationProgress.ANALYSIS_COMPLETE:
                        codex_analysis_text = progress.full_text or progress.text
                        self._ledger.update_agent_run_status(
                            codex_analysis_run_id,
                            "done",
                            completion_summary=codex_analysis_text[:2000],
                        )
                        codex_analysis_status = "done"
                    elif progress.phase == OrchestrationProgress.IMPL_COMPLETE:
                        claude_implementation_text = progress.full_text or progress.text
                        claude_run = self._ledger.create_agent_run(
                            conversation_id=conversation.id,
                            agent="claude",
                            role="implementation",
                            prompt_packet_summary=prompt[:120],
                        )
                        self._ledger.update_agent_run_status(
                            claude_run.id,
                            "done",
                            completion_summary=claude_implementation_text[:2000],
                        )
                        self._ledger.set_conversation_active_claude_run(
                            conversation.id, claude_run.id
                        )
                    elif progress.phase == OrchestrationProgress.VERIFY_STARTED:
                        verify_round = progress.round_num or verify_round
                    elif progress.phase == OrchestrationProgress.VERIFY_COMPLETE:
                        verify_round = progress.round_num or verify_round
                        verification_text = progress.full_text or progress.text
                        verify_run = self._ledger.create_agent_run(
                            conversation_id=conversation.id,
                            agent="codex",
                            role="verification",
                            prompt_packet_summary=verification_text[:120],
                        )
                        self._ledger.update_agent_run_status(
                            verify_run.id,
                            "done",
                            completion_summary=verification_text[:2000],
                        )
                elif progress.phase == OrchestrationProgress.FAILED:
                    terminal_sent = True
                    orch_result_status = "failed"
                    verify_round = progress.round_num or verify_round
                    terminal_text = progress.full_text or progress.text
                    self._ledger.set_task_status(task_id, TaskStatus.FAILED)
                    if progress.agent == "claude":
                        claude_run = self._ledger.create_agent_run(
                            conversation_id=conversation.id,
                            agent="claude",
                            role="implementation",
                            prompt_packet_summary=prompt[:120],
                        )
                        self._ledger.update_agent_run_status(
                            claude_run.id,
                            "failed",
                            completion_summary=terminal_text[:2000],
                        )
                        self._ledger.set_conversation_active_claude_run(
                            conversation.id, claude_run.id
                        )
                    elif progress.agent == "codex" and not codex_analysis_text:
                        self._ledger.update_agent_run_status(
                            codex_analysis_run_id,
                            "failed",
                            completion_summary=terminal_text[:2000],
                        )
                        codex_analysis_status = "failed"
                    await self._emit_failed(chat_id, task_id, progress.text)
                elif progress.phase == OrchestrationProgress.COMPLETE:
                    terminal_sent = True
                    orch_result_status = progress.result_status or "passed"
                    verify_round = progress.round_num or verify_round
                    if progress.text or progress.full_text:
                        terminal_text = progress.full_text or progress.text
                    if orch_result_status == "passed":
                        self._ledger.set_task_status(task_id, TaskStatus.DONE)
                        await self._emit_completed(
                            chat_id, task_id, conversation.id, workspace_path
                        )
                    elif orch_result_status == "needs_user":
                        self._ledger.set_task_status(task_id, TaskStatus.FAILED)
                        await self._emit_failed(
                            chat_id,
                            task_id,
                            progress.text or "需要用户输入以继续。",
                        )
                    else:
                        self._ledger.set_task_status(task_id, TaskStatus.FAILED)
                        await self._emit_failed(
                            chat_id,
                            task_id,
                            progress.text or "编排未通过验收。",
                        )
        except Exception as exc:
            logger.exception("Chief engineer background orchestration failed")
            terminal_sent = True
            orch_result_status = "failed"
            terminal_text = str(exc)
            self._ledger.set_task_status(task_id, TaskStatus.FAILED)
            self._ledger.update_orchestration_run(
                orchestration_run_id,
                status="failed",
                current_step="error",
                last_verification_result=str(exc)[:500],
            )
            self._ledger.update_agent_run_status(
                codex_analysis_run_id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            await self._emit_failed(chat_id, task_id, str(exc))

        if not codex_analysis_text and orch_result_status == "failed":
            codex_analysis_text = terminal_text
        if verification_text:
            decision = (
                "verify_passed"
                if orch_result_status == "passed"
                else "verify_failed_retry"
            )
            self._ledger.record_orchestration_decision(
                run_id=orchestration_run_id,
                decision=decision,
                reason=verification_text[:500],
                next_agent="" if orch_result_status == "passed" else "claude",
            )
        self._ledger.update_orchestration_run(
            orchestration_run_id,
            status=orch_result_status if orch_result_status != "running" else "failed",
            verify_round=verify_round,
            current_step="verify" if orch_result_status == "passed" else "retry",
            last_codex_analysis=codex_analysis_text[:500],
            last_claude_summary=claude_implementation_text[:500],
            last_verification_result=(verification_text or terminal_text)[:500],
        )
        codex_run_status = codex_analysis_status
        if codex_run_status == "running":
            codex_run_status = "failed" if orch_result_status == "failed" else "done"
        if codex_analysis_text or codex_run_status == "failed":
            self._ledger.update_agent_run_status(
                codex_analysis_run_id,
                codex_run_status,
                completion_summary=codex_analysis_text[:2000],
            )

        status_labels = {
            "passed": "验收通过",
            "failed": "验证失败",
            "needs_user": "需要用户输入",
        }
        label = status_labels.get(orch_result_status, orch_result_status)
        self._ledger.update_conversation_summary(
            conversation.id,
            trim_to_budget(
                f"总工程师第{verify_round}轮: {label}",
                ContextBudget().conversation_summary_tokens,
            ),
        )

    async def _handle_phase_progress(
        self,
        *,
        progress: OrchestrationProgress,
        chat_id: int,
        task_id: int,
        conversation_id: int,
    ) -> None:
        if progress.text:
            await self._emit_text_delta(
                chat_id,
                task_id,
                conversation_id,
                "\n\n" + progress.text,
            )

    async def _emit_text_delta(
        self,
        chat_id: int,
        task_id: int,
        conversation_id: int,
        text: str,
    ) -> None:
        if self._interaction_renderer is None:
            return
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="text_delta",
                chat_id=chat_id,
                task_id=task_id,
                conversation_id=conversation_id,
                text=text,
            )
        )

    async def _emit_completed(
        self,
        chat_id: int,
        task_id: int,
        conversation_id: int,
        workspace_path: str,
    ) -> None:
        if self._interaction_renderer is None:
            return
        task = self._service.get_task(task_id)
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="run_completed",
                chat_id=chat_id,
                task_id=task_id,
                conversation_id=conversation_id,
                metadata={
                    "has_diff": (
                        _workspace_has_changes(workspace_path)
                        or bool(task.changed_file_count)
                    ),
                },
            )
        )

    async def _emit_failed(self, chat_id: int, task_id: int, text: str) -> None:
        if self._interaction_renderer is None:
            return
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="run_failed",
                chat_id=chat_id,
                task_id=task_id,
                text=text,
            )
        )


def _workspace_has_changes(workspace_path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", workspace_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())
