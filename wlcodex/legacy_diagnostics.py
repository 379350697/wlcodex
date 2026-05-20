"""Legacy diagnostics adapter for raw task commands.

This module intentionally contains the old TaskService-facing command surface.
Normal Workbench/Cockpit flows should not call it directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from wlcodex.models import TaskStatus
from wlcodex.router import (
    AbortCommand,
    ArchiveCommand,
    ContinueCommand,
    DiffCommand,
    EventsCommand,
    FilesCommand,
    ForkCommand,
    ListTasksCommand,
    PauseCommand,
    ShowTaskCommand,
    StartTaskCommand,
    SteerCommand,
    TailCommand,
)
from wlcodex.legacy_task_status import (
    STATUS_LABELS,
    render_task_card,
    render_task_list,
)
from wlcodex.task_service import drain_workspace

logger = logging.getLogger(__name__)


@dataclass
class LegacyDiagnosticResponse:
    text: str
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    already_rendered: bool = False


ControllerResponse = LegacyDiagnosticResponse


class LegacyDiagnosticsController:
    """Owns the raw task diagnostic command surface."""

    def __init__(self, task_service: object, backend: object, inspector: object) -> None:
        self._service = task_service
        self._backend = backend
        self._inspector = inspector

    def can_handle(self, command: object) -> bool:
        if isinstance(command, (DiffCommand, FilesCommand)):
            return command.task_id is not None
        return isinstance(
            command,
            (
                ListTasksCommand,
                ShowTaskCommand,
                StartTaskCommand,
                ContinueCommand,
                SteerCommand,
                TailCommand,
                EventsCommand,
                PauseCommand,
                AbortCommand,
                ArchiveCommand,
                ForkCommand,
            ),
        )

    async def handle(
        self, command: object, telegram_context: dict[str, Any] | None = None
    ) -> LegacyDiagnosticResponse:
        if isinstance(command, ListTasksCommand):
            return self._handle_list_tasks()
        if isinstance(command, ShowTaskCommand):
            return self._handle_show_task(command)
        if isinstance(command, StartTaskCommand):
            return await self._handle_start(command, telegram_context)
        if isinstance(command, ContinueCommand):
            return await self._handle_continue(command)
        if isinstance(command, SteerCommand):
            return await self._handle_steer(command)
        if isinstance(command, TailCommand):
            result = self._inspector.tail(command.task_id)
            return LegacyDiagnosticResponse(f"{result.title}\n\n{result.body}")
        if isinstance(command, EventsCommand):
            result = self._inspector.events(command.task_id)
            return LegacyDiagnosticResponse(f"{result.title}\n\n{result.body}")
        if isinstance(command, DiffCommand) and command.task_id is not None:
            task = self._service.get_task(command.task_id)
            result = self._inspector.diff(command.task_id, task.workspace_path)
            return LegacyDiagnosticResponse(f"{result.title}\n\n{result.body}")
        if isinstance(command, FilesCommand) and command.task_id is not None:
            result = self._inspector.files(command.task_id)
            return LegacyDiagnosticResponse(f"{result.title}\n\n{result.body}")
        if isinstance(command, PauseCommand):
            return self._handle_pause(command)
        if isinstance(command, AbortCommand):
            return await self._handle_abort(command)
        if isinstance(command, ArchiveCommand):
            task = self._service.archive_task(command.task_id)
            return LegacyDiagnosticResponse(
                f"任务 #{command.task_id} 已归档。\n\n{render_task_card(task)}"
            )
        if isinstance(command, ForkCommand):
            return await self._handle_fork(command, telegram_context)
        raise TypeError(f"unsupported legacy diagnostic command: {type(command).__name__}")

    def _handle_list_tasks(self) -> LegacyDiagnosticResponse:
        tasks = self._service.list_tasks()
        wmeta: dict[int, tuple[int, str, int]] = {}
        for t in tasks:
            if t.status == TaskStatus.WAITING_SLOT:
                blocker = self._service.blocker_for_workspace(t.workspace_alias)
                if blocker is not None:
                    wmeta[t.id] = (
                        blocker.id,
                        STATUS_LABELS.get(blocker.status, blocker.status.value),
                        self._service.waiting_position(t.id),
                    )
        return LegacyDiagnosticResponse(render_task_list(tasks, waiting_meta=wmeta or None))

    def _handle_show_task(self, command: ShowTaskCommand) -> LegacyDiagnosticResponse:
        task = self._service.get_task(command.task_id)
        extra: dict[str, object] = {}
        if task.status == TaskStatus.WAITING_SLOT:
            blocker = self._service.blocker_for_workspace(task.workspace_alias)
            if blocker is not None:
                extra["blocker_id"] = blocker.id
                extra["blocker_status"] = STATUS_LABELS.get(
                    blocker.status, blocker.status.value
                )
            extra["queue_position"] = self._service.waiting_position(task.id)
        return LegacyDiagnosticResponse(render_task_card(task, **extra))

    async def _handle_start(
        self, command: StartTaskCommand, ctx: dict[str, Any] | None
    ) -> LegacyDiagnosticResponse:
        chat_id = ctx.get("chat_id") if ctx else None
        blocker = self._service.blocker_for_workspace(command.workspace_alias)
        if blocker is not None:
            task = self._service.reserve_waiting_task(
                command.workspace_alias,
                command.prompt,
                telegram_chat_id=chat_id,
                blocker_task_id=blocker.id,
            )
            position = self._service.waiting_position(task.id)
            return LegacyDiagnosticResponse(
                f"任务 #{task.id} — 等待工作区空闲\n"
                f"工作区：{command.workspace_alias}\n"
                f"阻塞者：#{blocker.id}（{STATUS_LABELS.get(blocker.status, blocker.status.value)}）\n"
                f"队列位置：第 {position} 位\n"
                f"标题：{task.title}\n\n"
                f"{render_task_card(task)}"
            )

        task = self._service.reserve_task(
            command.workspace_alias,
            command.prompt,
            telegram_chat_id=chat_id,
        )
        workspace = self._service.get_workspace(command.workspace_alias)
        try:
            thread_id = await self._backend.create_thread(str(workspace.path))
            self._service.set_task_thread(task.id, thread_id)
            await self._backend.start_turn(thread_id, command.prompt)
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return LegacyDiagnosticResponse(
                f"任务 #{task.id} 启动失败：{exc}\n\n{render_task_card(task)}"
            )

        task = self._service.get_task(task.id)
        return LegacyDiagnosticResponse(
            f"任务 #{task.id} 已启动。\n\n{render_task_card(task)}"
        )

    async def _handle_continue(
        self, command: ContinueCommand
    ) -> LegacyDiagnosticResponse:
        task = self._service.continue_task(command.task_id, command.prompt)
        if task.codex_thread_id is None:
            self._service.fail_task(task.id, "no codex thread; cannot continue")
            return LegacyDiagnosticResponse(f"任务 #{command.task_id} 没有 Codex 线程。")

        try:
            await self._backend.continue_turn(task.codex_thread_id, command.prompt)
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return LegacyDiagnosticResponse(
                f"继续任务失败：{exc}\n\n{render_task_card(task)}"
            )

        task = self._service.get_task(task.id)
        return LegacyDiagnosticResponse(
            f"任务 #{command.task_id} 已继续。\n\n{render_task_card(task)}"
        )

    async def _handle_steer(self, command: SteerCommand) -> LegacyDiagnosticResponse:
        task = self._service.steer_task(command.task_id, command.prompt)
        if task.codex_thread_id is None:
            raise RuntimeError(f"task #{task.id} has no codex thread")
        if task.active_turn_id is None:
            raise RuntimeError(
                f"task #{task.id} has no active turn. Use /continue to start a new turn."
            )
        await self._backend.steer_turn(
            task.codex_thread_id, task.active_turn_id, command.prompt
        )
        return LegacyDiagnosticResponse(
            f"任务 #{command.task_id} 已追加指令。\n\n{render_task_card(task)}"
        )

    def _handle_pause(self, command: PauseCommand) -> LegacyDiagnosticResponse:
        task = self._service.get_task(command.task_id)
        hint = ""
        if task.active_turn_id:
            hint = (
                "\n⚠️ Codex 里仍有活跃 turn。"
                "可用 /abort 停止它，或稍后用 /continue 继续。"
            )
        task = self._service.pause_task(command.task_id)
        return LegacyDiagnosticResponse(
            f"任务 #{command.task_id} 已暂停。{hint}\n\n{render_task_card(task)}"
        )

    async def _handle_abort(self, command: AbortCommand) -> LegacyDiagnosticResponse:
        task = self._service.get_task(command.task_id)
        if task.active_turn_id and task.codex_thread_id:
            try:
                await self._backend.interrupt_turn(task.codex_thread_id, task.active_turn_id)
            except Exception as exc:
                logger.warning("interrupt_turn failed: %s", exc)
        workspace_alias = task.workspace_alias
        task = self._service.abort_task(command.task_id)
        await drain_workspace(self._service, self._backend, workspace_alias)
        return LegacyDiagnosticResponse(
            f"任务 #{command.task_id} 已中止。\n\n{render_task_card(task)}"
        )

    async def _handle_fork(
        self, command: ForkCommand, ctx: dict[str, Any] | None
    ) -> LegacyDiagnosticResponse:
        parent = self._service.get_task(command.task_id)
        chat_id = ctx.get("chat_id") if ctx else None

        if parent.codex_thread_id is None:
            raise RuntimeError(
                f"task #{command.task_id} has no codex thread; cannot fork"
            )

        workspace = self._service.ensure_workspace_writable(parent.workspace_alias)
        self._service.ensure_workspace_available(parent.workspace_alias)
        task = self._service.reserve_task(
            parent.workspace_alias,
            command.prompt,
            telegram_chat_id=chat_id,
            parent_task_id=command.task_id,
        )

        try:
            new_thread_id = await self._backend.fork_thread(
                parent.codex_thread_id, str(workspace.path)
            )
            self._service.set_task_thread(task.id, new_thread_id)
            await self._backend.start_turn(new_thread_id, command.prompt)
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return LegacyDiagnosticResponse(
                f"Fork 失败：{exc}\n\n{render_task_card(task)}"
            )

        task = self._service.get_task(task.id)
        return LegacyDiagnosticResponse(
            f"已从任务 #{command.task_id} fork 到任务 #{task.id}。\n\n{render_task_card(task)}"
        )
