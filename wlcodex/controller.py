"""Command controller — routes parsed commands to services, returns responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any

from wlcodex.health_snapshot import build_health_snapshot
from wlcodex.inspection import TaskInspector
from wlcodex.models import TaskStatus
from wlcodex.conversation import default_title, mode_from_command
from wlcodex.context_packets import (
    build_codex_analysis_packet,
    build_codex_verification_packet as make_verification_packet,
)
from wlcodex.router import (
    AbortCommand,
    ArchiveCommand,
    AutoModeCommand,
    ClaudeDirectCommand,
    CodexDirectCommand,
    CodexSessionsCommand,
    ContinueCommand,
    DiffCommand,
    EventsCommand,
    FilesCommand,
    ForkCommand,
    HealthCommand,
    HelpCommand,
    ListTasksCommand,
    ModelCommand,
    NewConversationCommand,
    ParseError,
    PauseCommand,
    ShowTaskCommand,
    StartTaskCommand,
    SteerCommand,
    StopCurrentCommand,
    SwitchWorkspaceCommand,
    TailCommand,
    VerifyCommand,
    parse_command,
)
from wlcodex.status import (
    render_conversation_help,
    render_conversation_status,
    render_session_list,
)
from wlcodex.models import ConversationMode
from wlcodex.status import (
    render_task_card,
    render_task_list,
    render_health_card,
    render_help,
    STATUS_LABELS,
)
from wlcodex.task_service import TaskService, drain_workspace
from wlcodex.waiting_callback import (
    ABORT_BLOCKER_CONFIRM,
    ABORT_BLOCKER_START_NEXT,
    CONTINUE_BLOCKER,
    FORCE_PARALLEL_CONFIRM,
    FORCE_PARALLEL_REQUEST,
    KEEP,
    SHOW_BLOCKER,
    WORKTREE_DIFF,
    WORKTREE_DISCARD,
    WORKTREE_ISOLATED,
    WORKTREE_KEEP,
    WORKTREE_MERGE,
    WaitingCallback,
    encode_waiting_callback,
    encode_worktree_done_callback,
)

logger = logging.getLogger(__name__)

HELP_TEXT = render_conversation_help()


@dataclass
class ControllerResponse:
    text: str
    buttons: list[list[dict[str, str]]] = field(default_factory=list)


class CommandController:
    def __init__(
        self,
        task_service: TaskService,
        backend: object,
        inspector: TaskInspector,
        ledger: object | None = None,
        claude_backend: object | None = None,
    ) -> None:
        self._service = task_service
        self._backend = backend
        self._inspector = inspector
        self._ledger = ledger
        self._claude = claude_backend

    async def handle(
        self, text: str, telegram_context: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Parse a text command and return a response. Never injects status/log
        text into Codex prompts."""
        try:
            command = parse_command(text)
        except ParseError as exc:
            return ControllerResponse(str(exc))

        try:
            if isinstance(command, HelpCommand):
                return ControllerResponse(HELP_TEXT)

            elif isinstance(command, HealthCommand):
                if self._ledger is not None:
                    snapshot = build_health_snapshot(self._ledger, self._backend)
                else:
                    snapshot = None
                return ControllerResponse(render_health_card(
                    self._backend.health(), snapshot=snapshot
                ))

            elif isinstance(command, ListTasksCommand):
                # Conversation-first: show conversation status when available
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        runs = self._ledger.list_agent_runs(active.id, limit=1)
                        latest_run = runs[0] if runs else None
                        orch_runs = self._ledger.list_orchestration_runs(active.id, limit=1)
                        orch_run = orch_runs[0] if orch_runs else None
                        return ControllerResponse(
                            render_conversation_status(active, latest_run=latest_run, orch_run=orch_run)
                        )

                # Fall back to task list
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
                return ControllerResponse(render_task_list(tasks, waiting_meta=wmeta or None))

            elif isinstance(command, ShowTaskCommand):
                task = self._service.get_task(command.task_id)
                extra: dict[str, object] = {}
                buttons: list[list[dict[str, str]]] = []
                if task.status == TaskStatus.WAITING_SLOT:
                    blocker = self._service.blocker_for_workspace(task.workspace_alias)
                    if blocker is not None:
                        extra["blocker_id"] = blocker.id
                        extra["blocker_status"] = STATUS_LABELS.get(
                            blocker.status, blocker.status.value
                        )
                    extra["queue_position"] = self._service.waiting_position(task.id)
                    buttons = _build_waiting_buttons(task.id)
                elif task.worktree_path and task.status in (
                    TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ABORTED,
                ):
                    buttons = _build_worktree_done_buttons(task.id)
                return ControllerResponse(render_task_card(task, **extra), buttons=buttons)

            elif isinstance(command, StartTaskCommand):
                return await self._handle_start(command, telegram_context)

            elif isinstance(command, ContinueCommand):
                return await self._handle_continue(command)

            elif isinstance(command, SteerCommand):
                return await self._handle_steer(command)

            elif isinstance(command, TailCommand):
                result = self._inspector.tail(command.task_id)
                return ControllerResponse(
                    f"{result.title}\n\n{result.body}"
                )

            elif isinstance(command, EventsCommand):
                result = self._inspector.events(command.task_id)
                return ControllerResponse(
                    f"{result.title}\n\n{result.body}"
                )

            elif isinstance(command, DiffCommand):
                task_id = command.task_id
                if task_id is None and self._ledger is not None:
                    active = self._ledger.get_active_conversation(
                        telegram_context.get("chat_id", 0) if telegram_context else 0
                    )
                    if active and active.active_codex_task_id:
                        task_id = active.active_codex_task_id
                if task_id is None:
                    return ControllerResponse("请指定任务 ID 或在活跃对话中使用 /diff。")
                task = self._service.get_task(task_id)
                result = self._inspector.diff(task_id, task.workspace_path)
                return ControllerResponse(
                    f"{result.title}\n\n{result.body}"
                )

            elif isinstance(command, FilesCommand):
                task_id = command.task_id
                if task_id is None and self._ledger is not None:
                    active = self._ledger.get_active_conversation(
                        telegram_context.get("chat_id", 0) if telegram_context else 0
                    )
                    if active and active.active_codex_task_id:
                        task_id = active.active_codex_task_id
                if task_id is None:
                    return ControllerResponse("请指定任务 ID 或在活跃对话中使用 /files。")
                result = self._inspector.files(task_id)
                return ControllerResponse(
                    f"{result.title}\n\n{result.body}"
                )

            elif isinstance(command, PauseCommand):
                task = self._service.get_task(command.task_id)
                hint = ""
                if task.active_turn_id:
                    hint = (
                        "\n⚠️ Codex 里仍有活跃 turn。"
                        "可用 /abort 停止它，或稍后用 /continue 继续。"
                    )
                task = self._service.pause_task(command.task_id)
                return ControllerResponse(
                    f"任务 #{command.task_id} 已暂停。{hint}\n\n{render_task_card(task)}"
                )

            elif isinstance(command, AbortCommand):
                task = self._service.get_task(command.task_id)
                if task.active_turn_id and task.codex_thread_id:
                    try:
                        await self._backend.interrupt_turn(
                            task.codex_thread_id, task.active_turn_id
                        )
                    except Exception as exc:
                        logger.warning("interrupt_turn failed: %s", exc)
                workspace_alias = task.workspace_alias
                task = self._service.abort_task(command.task_id)
                # Drain waiting queue if the aborted task freed the workspace
                await drain_workspace(self._service, self._backend, workspace_alias)
                return ControllerResponse(
                    f"任务 #{command.task_id} 已中止。\n\n{render_task_card(task)}"
                )

            elif isinstance(command, ArchiveCommand):
                task = self._service.archive_task(command.task_id)
                return ControllerResponse(
                    f"任务 #{command.task_id} 已归档。\n\n{render_task_card(task)}"
                )

            elif isinstance(command, ForkCommand):
                return await self._handle_fork(command, telegram_context)

            elif isinstance(command, CodexSessionsCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    convos = self._ledger.list_conversations_by_chat(chat_id)
                    if convos:
                        return ControllerResponse(render_session_list(convos))
                # Fall back to task-based sessions
                tasks = self._service.list_tasks(include_archived=True)
                lines = ["Codex 会话：", f"{'ID':>4}  {'状态':<10}  {'Thread ID':<38}  标题"]
                for task in tasks:
                    if task.codex_thread_id:
                        lines.append(
                            f"{task.id:>4}  {STATUS_LABELS.get(task.status, task.status.value):<10}  "
                            f"{task.codex_thread_id:<38}  {task.title[:60]}"
                        )
                return ControllerResponse("\n".join(lines))

            # --- New conversation commands ---

            elif isinstance(command, NewConversationCommand):
                return await self.handle_new_conversation(command, telegram_context)

            elif isinstance(command, CodexDirectCommand):
                return await self.handle_codex_direct(command, telegram_context)

            elif isinstance(command, ClaudeDirectCommand):
                return await self.handle_claude_direct(command, telegram_context)

            elif isinstance(command, AutoModeCommand):
                return await self.handle_auto_mode(command, telegram_context)

            elif isinstance(command, StopCurrentCommand):
                return await self.handle_stop_current(telegram_context)

            elif isinstance(command, SwitchWorkspaceCommand):
                return await self.handle_switch_workspace(command, telegram_context)

            elif isinstance(command, ModelCommand):
                return await self.handle_model(command, telegram_context)

            elif isinstance(command, VerifyCommand):
                return await self.handle_verify(command, telegram_context)

            else:
                return ControllerResponse("未处理的命令类型。")

        except Exception as exc:
            logger.exception("Command handler error")
            return ControllerResponse(f"错误：{exc}")

    # --- Internal handlers ---

    async def _handle_start(
        self, command: StartTaskCommand, ctx: dict[str, Any] | None
    ) -> ControllerResponse:
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
            buttons = _build_waiting_buttons(task.id)
            return ControllerResponse(
                f"任务 #{task.id} — 等待工作区空闲\n"
                f"工作区：{command.workspace_alias}\n"
                f"阻塞者：#{blocker.id}（{STATUS_LABELS.get(blocker.status, blocker.status.value)}）\n"
                f"队列位置：第 {position} 位\n"
                f"标题：{task.title}\n\n"
                f"{render_task_card(task)}",
                buttons=buttons,
            )

        # 1. Reserve local task first (enforces write/availability)
        task = self._service.reserve_task(
            command.workspace_alias,
            command.prompt,
            telegram_chat_id=chat_id,
        )
        workspace = self._service.get_workspace(command.workspace_alias)

        # 2. Create app-server thread + persist + start turn
        try:
            thread_id = await self._backend.create_thread(str(workspace.path))
            self._service.set_task_thread(task.id, thread_id)
            await self._backend.start_turn(thread_id, command.prompt)
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return ControllerResponse(
                f"任务 #{task.id} 启动失败：{exc}\n\n{render_task_card(task)}"
            )

        task = self._service.get_task(task.id)
        return ControllerResponse(
            f"任务 #{task.id} 已启动。\n\n{render_task_card(task)}"
        )

    async def _handle_continue(self, command: ContinueCommand) -> ControllerResponse:
        task = self._service.continue_task(command.task_id, command.prompt)

        if task.codex_thread_id is None:
            self._service.fail_task(task.id, "no codex thread; cannot continue")
            return ControllerResponse(
                f"任务 #{command.task_id} 没有 Codex 线程。"
            )

        try:
            await self._backend.continue_turn(task.codex_thread_id, command.prompt)
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return ControllerResponse(
                f"继续任务失败：{exc}\n\n{render_task_card(task)}"
            )

        task = self._service.get_task(task.id)
        return ControllerResponse(
            f"任务 #{command.task_id} 已继续。\n\n{render_task_card(task)}"
        )

    async def _handle_steer(self, command: SteerCommand) -> ControllerResponse:
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

        return ControllerResponse(
            f"任务 #{command.task_id} 已追加指令。\n\n{render_task_card(task)}"
        )

    async def _handle_fork(
        self, command: ForkCommand, ctx: dict[str, Any] | None
    ) -> ControllerResponse:
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
            return ControllerResponse(
                f"Fork 失败：{exc}\n\n{render_task_card(task)}"
            )

        task = self._service.get_task(task.id)
        return ControllerResponse(
            f"已从任务 #{command.task_id} fork 到任务 #{task.id}。\n\n{render_task_card(task)}"
        )

    # --- Waiting callback handlers ---

    async def handle_waiting_callback(
        self, callback: WaitingCallback
    ) -> ControllerResponse:
        """Route a waiting-slot decision callback to the correct handler."""
        try:
            task = self._service.get_task(callback.task_id)
        except KeyError:
            return ControllerResponse("任务不存在或已被删除。")

        if task.status != TaskStatus.WAITING_SLOT:
            # Task is no longer waiting — check if we can still serve the action
            if callback.action in (SHOW_BLOCKER,):
                return await self._handle_show_blocker(callback.task_id)
            return ControllerResponse(
                f"任务 #{callback.task_id} 已不在等待状态（当前：{STATUS_LABELS.get(task.status, task.status.value)}）。"
            )

        if callback.action == KEEP:
            return await self._handle_keep(callback.task_id)
        elif callback.action == SHOW_BLOCKER:
            return await self._handle_show_blocker(callback.task_id)
        elif callback.action == ABORT_BLOCKER_START_NEXT:
            return await self._handle_abort_blocker_start_next(callback.task_id)
        elif callback.action == ABORT_BLOCKER_CONFIRM:
            return await self._handle_abort_blocker_confirm(callback.task_id)
        elif callback.action == CONTINUE_BLOCKER:
            return await self._handle_continue_blocker(callback.task_id)
        elif callback.action == FORCE_PARALLEL_REQUEST:
            return await self._handle_force_parallel_request(callback.task_id)
        elif callback.action == FORCE_PARALLEL_CONFIRM:
            return await self._handle_force_parallel_confirm(callback.task_id)
        elif callback.action == WORKTREE_ISOLATED:
            return await self._handle_worktree_isolated(callback.task_id)
        else:
            return ControllerResponse(f"未知等待操作：{callback.action}")

    # --- Conversation handlers ---

    async def handle_conversation_text(
        self, text: str, telegram_context: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Handle plain text as conversation message. Never injects status/log
        into model prompts."""
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = telegram_context.get("chat_id", 0) if telegram_context else 0
        user_id = telegram_context.get("user_id", 0) if telegram_context else 0

        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            title = default_title(text)
            active = self._ledger.create_conversation(
                chat_id=chat_id,
                user_id=user_id,
                title=title,
                mode=ConversationMode.CODEX_DIRECT.value,
                workspace_alias="wlcodex",
            )

        # Build compact context packet — never include raw Telegram transcript
        packet = build_codex_analysis_packet(
            user_goal=text,
            conversation_summary=active.conversation_summary,
            constraints=[],
            workspace=active.workspace_alias,
        )

        # Reserve a hidden task through TaskService
        task = self._service.reserve_task(
            active.workspace_alias,
            text,
            telegram_chat_id=chat_id,
        )
        self._ledger.set_conversation_active_task(active.id, task.id)

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
        try:
            thread_id = await self._backend.create_thread(workspace_path)
            self._service.set_task_thread(task.id, thread_id)
            await self._backend.start_turn(thread_id, text)
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return ControllerResponse(
                f"Codex 启动失败：{exc}\n\n任务 #{task.id} 已失败。"
            )

        self._ledger.update_conversation_summary(
            active.id,
            f"用户请求：{text[:120]}",
        )
        self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role="analysis",
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )

        return ControllerResponse(
            f"对话「{active.title}」\n\n"
            f"正在用 Codex 分析你的需求。\n"
            f"工作区：{active.workspace_alias}\n"
            f"后台任务 #{task.id}\n\n"
            f"提示：当 Codex 完成分析后会通知你。"
        )

    async def handle_new_conversation(
        self, command: NewConversationCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        # Archive any active conversation
        old = self._ledger.get_active_conversation(chat_id)
        if old is not None:
            self._ledger.archive_conversation(old.id)

        title = command.title if command.title else "新对话"
        convo = self._ledger.create_conversation(
            chat_id=chat_id,
            user_id=user_id,
            title=title,
            mode=ConversationMode.CODEX_DIRECT.value,
            workspace_alias="wlcodex",
        )
        return ControllerResponse(
            f"新对话已创建：「{convo.title}」\n"
            f"模式：Codex 直聊\n"
            f"工作区：{convo.workspace_alias}\n\n"
            f"直接发消息开始对话，或用 /codex /claude /auto 切换模式。"
        )

    async def handle_codex_direct(
        self, command: CodexDirectCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        return await self.handle_conversation_text(command.prompt, ctx)

    async def handle_claude_direct(
        self, command: ClaudeDirectCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        if self._claude is None or not getattr(self._claude, "enabled", False):
            return ControllerResponse(
                "Claude Code 未启用。请在配置中设置 claude.enabled = true 后重试。"
            )

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            title = default_title(command.prompt)
            active = self._ledger.create_conversation(
                chat_id=chat_id,
                user_id=user_id,
                title=title,
                mode=ConversationMode.CLAUDE_DIRECT.value,
                workspace_alias="wlcodex",
            )

        from wlcodex.agent_backend import AgentRequest

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
        result = await self._claude.send(AgentRequest(
            prompt=command.prompt,
            workspace_path=workspace_path,
        ))

        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="claude",
            role="implementation",
            prompt_packet_summary=command.prompt[:120],
        )
        self._ledger.update_agent_run_status(
            agent_run.id,
            "done",
            token_input=result.token_input,
            token_output=result.token_output,
        )
        self._ledger.set_conversation_active_task(active.id, agent_run.id)

        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看 diff", "callback_data": f"waiting:{active.id}:diff"},
            {"text": "Codex 验收", "callback_data": f"waiting:{active.id}:verify"},
        ]]

        return ControllerResponse(
            f"Claude Code 已完成。\n\n"
            f"对话：{active.title}\n"
            f"工作区：{active.workspace_alias}\n"
            f"Token：{result.token_input} 输入 / {result.token_output} 输出\n\n"
            f"{_trim_result_text(result.text, 2000)}",
            buttons=buttons,
        )

    async def handle_auto_mode(
        self, command: AutoModeCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        if self._claude is None or not getattr(self._claude, "enabled", False):
            return ControllerResponse(
                "总工程师编排需要 Claude Code 后端。\n"
                "请在配置中设置 claude.enabled = true，或使用 /codex 直接对话。"
            )

        from wlcodex.orchestrator import ChiefEngineerOrchestrator

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            title = default_title(command.prompt)
            active = self._ledger.create_conversation(
                chat_id=chat_id,
                user_id=user_id,
                title=title,
                mode=ConversationMode.CHIEF_ENGINEER.value,
                workspace_alias="wlcodex",
            )

        orch = ChiefEngineerOrchestrator(self._backend, self._claude)
        result = await orch.run(command.prompt)

        orch_run = self._ledger.create_orchestration_run(
            conversation_id=active.id,
            goal=command.prompt,
        )
        self._ledger.update_orchestration_run(
            orch_run.id,
            status=result.status,
            verify_round=result.verify_round,
            last_codex_analysis=result.codex_analysis[:500] if result.codex_analysis else "",
            last_claude_summary=result.claude_implementation[:500] if result.claude_implementation else "",
            last_verification_result=result.verification_summary[:500],
        )

        status_labels = {
            "passed": "验收通过",
            "failed": "验证失败",
            "needs_user": "需要用户输入",
        }
        label = status_labels.get(result.status, result.status)

        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看 diff", "callback_data": f"waiting:{active.id}:diff"},
        ]]
        if result.status == "failed":
            buttons[0].append(
                {"text": "继续修改", "callback_data": f"waiting:{active.id}:retry"}
            )

        return ControllerResponse(
            f"总工程师编排完成 — {label}\n\n"
            f"对话：{active.title}\n"
            f"轮次：第 {result.verify_round} 轮\n"
            f"Codex 分析：{_trim_result_text(result.codex_analysis, 300)}\n"
            f"Claude 实施：{_trim_result_text(result.claude_implementation, 300)}\n"
            f"验证结果：{_trim_result_text(result.verification_summary, 300)}",
            buttons=buttons,
        )

    async def handle_verify(
        self, command: VerifyCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if active is None:
            return ControllerResponse("当前没有活跃对话。请先开始对话。")

        runs = self._ledger.list_agent_runs(active.id, limit=5)
        if not runs:
            return ControllerResponse("当前对话没有已完成的运行。")

        latest_run = runs[-1]

        # Build verification packet and call Codex
        verify_payload = command.prompt if command.prompt else active.title
        packet = make_verification_packet(
            user_goal=verify_payload,
            claude_completion_summary=latest_run.prompt_packet_summary,
            workspace=active.workspace_alias,
        )

        # Call Codex backend for verification
        try:
            if active.active_codex_task_id:
                task = self._service.get_task(active.active_codex_task_id)
                if task.codex_thread_id:
                    await self._backend.start_turn(task.codex_thread_id, packet.render())
                    verification_text = f"已向 Codex 发送验收请求。\n" \
                                        f"对话：{active.title}\n" \
                                        f"线程：{task.codex_thread_id}\n" \
                                        f"验收标准：{packet.acceptance_criteria or '由 Codex 分析'}"
                else:
                    verification_text = "Codex 线程未就绪，无法发送验收。"
            else:
                # No existing Codex task — create one for verification
                workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
                thread_id = await self._backend.create_thread(workspace_path)
                task = self._service.reserve_task(
                    active.workspace_alias,
                    f"验证：{verify_payload}",
                    telegram_chat_id=chat_id,
                )
                self._service.set_task_thread(task.id, thread_id)
                self._ledger.set_conversation_active_task(active.id, task.id)
                await self._backend.start_turn(thread_id, packet.render())
                verification_text = f"已创建 Codex 验证任务 #{task.id}。\n" \
                                    f"对话：{active.title}\n" \
                                    f"验收内容：{verify_payload}"
        except Exception as exc:
            logger.warning("verify: Codex call failed: %s", exc)
            verification_text = f"Codex 验收请求失败：{exc}"

        return ControllerResponse(
            f"Codex 验收 — 对话「{active.title}」\n\n"
            f"最近运行：#{latest_run.id}（{latest_run.agent}/{latest_run.status}）\n"
            f"摘要：{latest_run.prompt_packet_summary}\n"
            f"Token：{latest_run.token_input} 输入 / {latest_run.token_output} 输出\n\n"
            f"{verification_text}"
        )

    async def handle_stop_current(
        self, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if active is None:
            return ControllerResponse("当前没有活跃对话。")

        # Abort the active task if any
        if active.active_codex_task_id:
            try:
                task = self._service.get_task(active.active_codex_task_id)
                if task.active_turn_id and task.codex_thread_id:
                    try:
                        await self._backend.interrupt_turn(
                            task.codex_thread_id, task.active_turn_id
                        )
                    except Exception as exc:
                        logger.warning("interrupt_turn failed: %s", exc)
                self._service.abort_task(active.active_codex_task_id)
            except KeyError:
                pass

        return ControllerResponse(
            f"对话「{active.title}」已停止。"
            f"\n后台任务 #{active.active_codex_task_id} 已中止（如果仍在运行）。"
        )

    async def handle_switch_workspace(
        self, command: SwitchWorkspaceCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if active is None:
            return ControllerResponse("当前没有活跃对话。请先创建对话。")

        try:
            self._service.get_workspace(command.workspace_alias)
        except Exception:
            return ControllerResponse(f"工作区 '{command.workspace_alias}' 不存在。")

        updated = self._ledger.set_conversation_workspace(
            active.id, command.workspace_alias
        )
        return ControllerResponse(
            f"对话「{updated.title}」工作区已切换至 {command.workspace_alias}。"
        )

    async def handle_model(
        self, command: ModelCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)

        if command.model_name:
            if active is None:
                return ControllerResponse("当前没有活跃对话。请先创建对话。")
            self._ledger._conn.execute(
                "UPDATE conversation_sessions SET current_model = ?, updated_at = ? WHERE id = ?",
                (command.model_name, datetime.now(timezone.utc).isoformat(), active.id),
            )
            self._ledger._conn.commit()
            return ControllerResponse(
                f"对话「{active.title}」模型偏好已保存为 {command.model_name}。"
                f"\n注意：模型切换需要后端支持，当前仅为偏好保存。"
            )
        else:
            current = active.current_model if active else ""
            if current:
                return ControllerResponse(f"当前偏好模型：{current}\n使用 /model <name> 切换。")
            return ControllerResponse("当前未设置偏好模型。使用 /model <name> 切换。")

    async def handle_worktree_done_callback(
        self, callback: WaitingCallback
    ) -> ControllerResponse:
        """Route a worktree post-completion callback."""
        try:
            task = self._service.get_task(callback.task_id)
        except KeyError:
            return ControllerResponse("任务不存在或已被删除。")

        if not task.worktree_path:
            return ControllerResponse(f"任务 #{callback.task_id} 不是 worktree 任务。")

        if callback.action == WORKTREE_DIFF:
            return await self._handle_worktree_diff(callback.task_id)
        elif callback.action == WORKTREE_MERGE:
            return await self._handle_worktree_merge(callback.task_id)
        elif callback.action == WORKTREE_DISCARD:
            return await self._handle_worktree_discard(callback.task_id)
        elif callback.action == WORKTREE_KEEP:
            return await self._handle_worktree_keep(callback.task_id)
        else:
            return ControllerResponse(f"未知 worktree 操作：{callback.action}")

    # --- Individual waiting action handlers ---

    async def _handle_keep(self, task_id: int) -> ControllerResponse:
        task = self._service.get_task(task_id)
        return ControllerResponse(
            f"任务 #{task_id} 保持排队。\n\n{render_task_card(task)}",
            buttons=_build_waiting_buttons(task_id),
        )

    async def _handle_continue_blocker(self, task_id: int) -> ControllerResponse:
        """Show a hint that continuing needs user text via /continue command."""
        task = self._service.get_task(task_id)
        blocker = self._service.blocker_for_workspace(task.workspace_alias)
        if blocker is None:
            return ControllerResponse(
                f"任务 #{task_id} 的阻塞者已结束。\n\n{render_task_card(task)}",
                buttons=_build_waiting_buttons(task_id),
            )
        return ControllerResponse(
            f"阻塞者 #{blocker.id} 仍在运行，无法通过按钮自动继续。\n"
            f"继续任务需要你提供新的提示词，请使用命令：\n\n"
            f"/continue {blocker.id} <你的提示词>\n\n"
            f"阻塞者信息：\n{render_task_card(blocker)}",
        )

    async def _handle_show_blocker(self, task_id: int) -> ControllerResponse:
        task = self._service.get_task(task_id)
        blocker = self._service.blocker_for_workspace(task.workspace_alias)
        if blocker is None:
            return ControllerResponse(
                f"任务 #{task_id} 的阻塞者已结束。\n\n{render_task_card(task)}"
            )
        card = render_task_card(blocker)
        return ControllerResponse(
            f"任务 #{task_id} 的阻塞者：\n\n{card}"
        )

    async def _handle_abort_blocker_start_next(
        self, task_id: int
    ) -> ControllerResponse:
        """First step: show confirmation card before aborting the blocker."""
        task = self._service.get_task(task_id)
        blocker = self._service.blocker_for_workspace(task.workspace_alias)

        if blocker is None:
            # Blocker already ended — try to drain directly
            self._service._ledger.add_event(task_id, "queue_drained", {
                "reason": "blocker_already_gone",
            })
            promoted = await drain_workspace(
                self._service, self._backend, task.workspace_alias
            )
            if promoted is not None:
                return ControllerResponse(
                    f"阻塞者已结束。任务 #{promoted.id} 已启动。\n\n{render_task_card(promoted)}"
                )
            return ControllerResponse(
                f"阻塞者已结束，但队列为空。\n\n{render_task_card(task)}"
            )

        return ControllerResponse(
            f"⚠️ 确认中止阻塞任务\n\n"
            f"将中止阻塞者 #{blocker.id}（{STATUS_LABELS.get(blocker.status, blocker.status.value)}）\n"
            f"标题：{blocker.title}\n\n"
            f"中止后将自动启动队首等待任务。\n\n"
            f"等待任务 #{task_id}：{task.title}",
            buttons=[[
                {"text": "确认中止阻塞任务", "callback_data": encode_waiting_callback(task_id, ABORT_BLOCKER_CONFIRM)},
                {"text": "取消", "callback_data": encode_waiting_callback(task_id, KEEP)},
            ]],
        )

    async def _handle_abort_blocker_confirm(
        self, task_id: int
    ) -> ControllerResponse:
        """Second step: actually abort the blocker and drain the queue."""
        task = self._service.get_task(task_id)
        blocker = self._service.blocker_for_workspace(task.workspace_alias)

        if blocker is None:
            # Blocker ended between confirmation and this click
            self._service._ledger.add_event(task_id, "queue_drained", {
                "reason": "blocker_already_gone",
            })
            promoted = await drain_workspace(
                self._service, self._backend, task.workspace_alias
            )
            if promoted is not None:
                return ControllerResponse(
                    f"阻塞者已结束。任务 #{promoted.id} 已启动。\n\n{render_task_card(promoted)}"
                )
            return ControllerResponse(
                f"阻塞者已结束，但队列为空。\n\n{render_task_card(task)}"
            )

        # Record abort request
        self._service._ledger.add_event(task_id, "queue_blocker_abort_requested", {
            "blocker_task_id": blocker.id,
        })

        # Interrupt active turn if any
        if blocker.active_turn_id and blocker.codex_thread_id:
            try:
                await self._backend.interrupt_turn(
                    blocker.codex_thread_id, blocker.active_turn_id
                )
            except Exception as exc:
                logger.warning("interrupt_turn failed: %s", exc)

        self._service.abort_task(blocker.id)
        self._service._ledger.add_event(task_id, "queue_blocker_aborted", {
            "blocker_task_id": blocker.id,
        })

        # Drain the queue
        self._service._ledger.add_event(task_id, "queue_drained", {
            "reason": "blocker_aborted",
        })
        promoted = await drain_workspace(
            self._service, self._backend, task.workspace_alias
        )
        if promoted is not None:
            return ControllerResponse(
                f"阻塞者 #{blocker.id} 已中止。队首任务 #{promoted.id} 已启动。\n\n{render_task_card(promoted)}"
            )
        return ControllerResponse(
            f"阻塞者 #{blocker.id} 已中止，但队首任务启动失败。"
        )

    async def _handle_force_parallel_request(
        self, task_id: int
    ) -> ControllerResponse:
        task = self._service.get_task(task_id)
        blocker = self._service.blocker_for_workspace(task.workspace_alias)
        self._service._ledger.add_event(task_id, "force_parallel_requested", {
            "blocker_task_id": blocker.id if blocker else None,
            "workspace_alias": task.workspace_alias,
            "workspace_path": task.workspace_path,
            "telegram_chat_id": task.telegram_chat_id,
        })
        return ControllerResponse(
            f"⚠️ 危险操作：同目录并行\n\n"
            f"这将同时在同一个工作目录运行两个 Codex 任务。"
            f"它们的编辑、命令、审批和本地 git diff 可能互相冲突、覆盖文件或混淆审批。"
            f"只有在你理解风险的情况下才使用此功能。\n\n"
            f"任务 #{task_id}：{task.title}\n"
            f"工作区：{task.workspace_alias}\n",
            buttons=[[
                {"text": "确认并行 — 我了解风险", "callback_data": encode_waiting_callback(task_id, FORCE_PARALLEL_CONFIRM)},
                {"text": "取消", "callback_data": encode_waiting_callback(task_id, KEEP)},
            ]],
        )

    async def _handle_force_parallel_confirm(
        self, task_id: int
    ) -> ControllerResponse:
        task = self._service.get_task(task_id)
        blocker = self._service.blocker_for_workspace(task.workspace_alias)
        if task.status != TaskStatus.WAITING_SLOT:
            # Check if blocker is gone — task may have been auto-drained already
            if blocker is None:
                self._service._ledger.add_event(task_id, "force_parallel_no_longer_needed", {
                    "workspace_alias": task.workspace_alias,
                    "current_status": task.status.value,
                })
                if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                    return ControllerResponse(
                        f"任务 #{task_id} 已通过正常排队启动，无需强制并行。\n\n{render_task_card(task)}"
                    )
                return ControllerResponse(
                    f"任务 #{task_id} 已不在等待状态（当前：{STATUS_LABELS.get(task.status, task.status.value)}）。"
                )
            return ControllerResponse(
                f"任务 #{task_id} 已不在等待状态（当前：{STATUS_LABELS.get(task.status, task.status.value)}）。"
            )

        self._service._ledger.add_event(task_id, "force_parallel_confirmed", {
            "blocker_task_id": blocker.id if blocker else None,
            "workspace_alias": task.workspace_alias,
            "workspace_path": task.workspace_path,
            "telegram_chat_id": task.telegram_chat_id,
        })

        try:
            promoted, prompt = self._service.force_parallel_start(task_id)
        except Exception as exc:
            return ControllerResponse(f"强制并行启动失败：{exc}")

        workspace = self._service.get_workspace(promoted.workspace_alias)
        try:
            thread_id = await self._backend.create_thread(str(workspace.path))
            self._service.set_task_thread(promoted.id, thread_id)
            await self._backend.start_turn(thread_id, prompt)
        except Exception as exc:
            self._service.fail_task(promoted.id, str(exc))
            return ControllerResponse(
                f"强制并行任务启动失败：{exc}\n\n{render_task_card(self._service.get_task(task_id))}"
            )

        task = self._service.get_task(task_id)
        return ControllerResponse(
            f"⚠️ 任务 #{task_id} 已强制并行启动。\n"
            f"注意：两个 Codex 在同一工作目录运行，可能产生冲突。\n\n"
            f"{render_task_card(task)}"
        )

    async def _handle_worktree_isolated(
        self, task_id: int
    ) -> ControllerResponse:
        task = self._service.get_task(task_id)
        if task.status != TaskStatus.WAITING_SLOT:
            return ControllerResponse(
                f"任务 #{task_id} 已不在等待状态。"
            )

        # Slugify the title for branch name
        slug = "".join(c if c.isalnum() else "-" for c in task.title)[:40].strip("-").lower()
        if not slug:
            slug = "task"

        try:
            wt_task, wt_path, branch = self._service.setup_worktree(
                task_id, slug=slug,
            )
        except Exception as exc:
            self._service.fail_task(task_id, str(exc))
            return ControllerResponse(
                f"Worktree 创建失败：{exc}\n\n{render_task_card(self._service.get_task(task_id))}"
            )

        try:
            promoted, prompt, wt_path = self._service.start_worktree_task(task_id)
        except Exception as exc:
            self._service.fail_task(task_id, str(exc))
            return ControllerResponse(
                f"Worktree 任务启动失败：{exc}"
            )

        try:
            thread_id = await self._backend.create_thread(wt_path)
            self._service.set_task_thread(promoted.id, thread_id)
            await self._backend.start_turn(thread_id, prompt)
        except Exception as exc:
            self._service.fail_task(promoted.id, str(exc))
            return ControllerResponse(
                f"Worktree Codex 启动失败：{exc}"
            )

        task = self._service.get_task(task_id)
        return ControllerResponse(
            f"任务 #{task_id} 已在隔离 worktree 中启动。\n"
            f"Worktree 路径：{wt_path}\n"
            f"分支：{branch}\n"
            f"此任务不阻塞原工作区队列。\n\n"
            f"{render_task_card(task)}"
        )

    # --- Worktree post-completion handlers ---

    async def _handle_worktree_diff(self, task_id: int) -> ControllerResponse:
        task = self._service.get_task(task_id)
        result = self._inspector.diff(task_id, task.worktree_path or task.workspace_path)
        buttons = _build_worktree_done_buttons(task_id)
        return ControllerResponse(
            f"{result.title}\n\n{result.body}",
            buttons=buttons,
        )

    async def _handle_worktree_merge(self, task_id: int) -> ControllerResponse:
        try:
            msg = self._service.merge_worktree(task_id)
        except Exception as exc:
            return ControllerResponse(
                f"合并失败：{exc}",
                buttons=_build_worktree_done_buttons(task_id),
            )
        return ControllerResponse(
            f"任务 #{task_id} worktree 合并结果：\n{msg}",
        )

    async def _handle_worktree_discard(self, task_id: int) -> ControllerResponse:
        try:
            msg = self._service.discard_worktree(task_id)
        except Exception as exc:
            return ControllerResponse(f"丢弃 worktree 失败：{exc}")
        return ControllerResponse(
            f"任务 #{task_id}：{msg}"
        )

    async def _handle_worktree_keep(self, task_id: int) -> ControllerResponse:
        task = self._service.get_task(task_id)
        return ControllerResponse(
            f"任务 #{task_id} worktree 已保留。\n"
            f"路径：{task.worktree_path}\n"
            f"分支：{task.worktree_branch}"
        )


# --- Button builders ---


def _build_waiting_buttons(task_id: int) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "保留排队", "callback_data": encode_waiting_callback(task_id, KEEP)},
            {"text": "查看阻塞任务", "callback_data": encode_waiting_callback(task_id, SHOW_BLOCKER)},
        ],
        [
            {"text": "Continue 阻塞任务", "callback_data": encode_waiting_callback(task_id, CONTINUE_BLOCKER)},
            {"text": "中止阻塞并启动队首", "callback_data": encode_waiting_callback(task_id, ABORT_BLOCKER_START_NEXT)},
        ],
        [
            {"text": "危险：同目录并行", "callback_data": encode_waiting_callback(task_id, FORCE_PARALLEL_REQUEST)},
        ],
        [
            {"text": "隔离 worktree 并行", "callback_data": encode_waiting_callback(task_id, WORKTREE_ISOLATED)},
        ],
    ]


def _trim_result_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1].rstrip() + "…"


def _build_worktree_done_buttons(task_id: int) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "查看 diff", "callback_data": encode_worktree_done_callback(task_id, WORKTREE_DIFF)},
            {"text": "合并到主工作区", "callback_data": encode_worktree_done_callback(task_id, WORKTREE_MERGE)},
        ],
        [
            {"text": "丢弃 worktree", "callback_data": encode_worktree_done_callback(task_id, WORKTREE_DISCARD)},
            {"text": "保留 worktree", "callback_data": encode_worktree_done_callback(task_id, WORKTREE_KEEP)},
        ],
    ]
