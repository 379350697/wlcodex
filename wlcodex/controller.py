"""Command controller — routes parsed commands to services, returns responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import logging
import subprocess
import uuid
from typing import Any

from wlcodex.health_snapshot import build_health_snapshot
from wlcodex.interaction.errors import classify_user_error
from wlcodex.inspection import TaskInspector
from wlcodex.models import TaskStatus
from wlcodex.conversation import default_title, mode_from_command
from wlcodex.conversation_callback import (
    CONTINUE,
    DIFF,
    NEW_CONVO,
    RETRY,
    VERIFY,
    ConversationCallback,
    decode_conversation_callback,
    encode_conversation_callback,
)
from wlcodex.context_packets import (
    ContextBudget,
    build_codex_analysis_packet,
    build_codex_verification_packet as make_verification_packet,
    trim_to_budget,
)
from wlcodex.claude_permissions import (
    DEFAULT_CLAUDE_PERMISSION_MODE,
    RUNTIME_CLAUDE_PERMISSION_MODE_KEY,
    ClaudePermissionState,
    build_claude_permission_buttons,
    render_claude_permission_status,
)
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.router import (
    AbortCommand,
    ArchiveCommand,
    AutoModeCommand,
    ClaudeDirectCommand,
    ClaudePermissionCommand,
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
    MODE_LABELS,
    render_conversation_help,
    render_conversation_status,
    render_session_list,
)
from wlcodex.models import ConversationMode
from wlcodex.orchestration_progress_text import render_user_progress_text
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


def _is_lightweight_greeting(text: str) -> bool:
    normalized = text.strip().lower()
    normalized = normalized.strip(" \t\r\n.!?。！？~～")
    return normalized in {
        "hi",
        "hello",
        "hey",
        "你好",
        "您好",
        "哈喽",
        "嗨",
    }


@dataclass
class ControllerResponse:
    text: str
    buttons: list[list[dict[str, str]]] = field(default_factory=list)
    already_rendered: bool = False


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


class CommandController:
    def __init__(
        self,
        task_service: TaskService,
        backend: object,
        inspector: TaskInspector,
        ledger: object | None = None,
        claude_backend: object | None = None,
        claude_permission_state: ClaudePermissionState | None = None,
        default_mode: str = "chief_engineer",
        default_workspace: str = "wlcodex",
        interaction_renderer: object | None = None,
        orchestration_runner: object | None = None,
        runtime_event_store: object | None = None,
    ) -> None:
        self._service = task_service
        self._backend = backend
        self._inspector = inspector
        self._ledger = ledger
        self._claude = claude_backend
        self._claude_permission_state = claude_permission_state
        self._default_mode = default_mode
        self._default_workspace = default_workspace
        self._interaction_renderer = interaction_renderer
        self._orchestration_runner = orchestration_runner
        self._store = runtime_event_store

    def set_interaction_renderer(self, renderer: object) -> None:
        """Set the interaction renderer after construction (created after handlers)."""
        self._interaction_renderer = renderer

    def set_orchestration_runner(self, runner: object | None) -> None:
        """Set the background orchestration runner after runtime wiring."""
        self._orchestration_runner = runner

    def _emit_event(self, event: RuntimeEvent) -> RuntimeEvent:
        if self._store is None:
            return event
        return self._store.append(event)

    def _new_correlation_id(self) -> str:
        return str(uuid.uuid4())

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
                    elif active and active.active_claude_run_id:
                        # Claude run: use workspace git diff directly
                        ws = str(self._service.get_workspace(active.workspace_alias).path)
                        result = self._inspector.diff(
                            active.active_claude_run_id, ws
                        )
                        return ControllerResponse(
                            f"{result.title}\n\n{result.body}"
                        )
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
                    elif active and active.active_claude_run_id:
                        # Claude run: use agent run for files lookup
                        task_id = active.active_claude_run_id
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

            elif isinstance(command, ClaudePermissionCommand):
                return await self.handle_claude_permission(command, telegram_context)

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
        into model prompts.

        Chief-engineer mode (default): full Codex→Claude→Codex closed loop.
        Codex-direct mode: Codex analysis only, no implementation.
        When Claude is disabled, chief_engineer falls back to Codex-only.
        """
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
                mode=self._default_mode,
                workspace_alias=self._default_workspace,
            )

        if _is_lightweight_greeting(text):
            self._ledger.update_conversation_summary(
                active.id,
                trim_to_budget("用户打招呼，等待具体任务。", ContextBudget().conversation_summary_tokens),
            )
            return ControllerResponse("你好！直接说需要我看什么就行。")

        # Emit user.message.received
        cid = self._new_correlation_id()
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.TELEGRAM,
            actor="user",
            visibility=Visibility.USER,
            payload={"text_preview": text[:200]},
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # --- Chief-engineer mode with Claude enabled: full closed loop ---
        claude_ready = self._claude is not None and getattr(self._claude, "enabled", False)
        if active.mode == ConversationMode.CHIEF_ENGINEER.value and claude_ready:
            # Build an AutoModeCommand-equivalent and run the full orchestrator
            from wlcodex.router import AutoModeCommand
            cmd = AutoModeCommand(prompt=text)
            return await self._handle_chief_engineer_impl(
                cmd, active, telegram_context, correlation_id=cid
            )

        # --- Codex-direct or Claude-disabled: Codex analysis only ---
        budget = ContextBudget()
        packet = build_codex_analysis_packet(
            user_goal=text,
            conversation_summary=trim_to_budget(
                active.conversation_summary, budget.conversation_summary_tokens
            ),
            constraints=[],
            workspace=active.workspace_alias,
            budget=budget,
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
            await self._backend.start_turn(thread_id, packet.render())
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return ControllerResponse(classify_user_error(exc))

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(f"用户请求：{text[:200]}", budget.conversation_summary_tokens),
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role="analysis",
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.set_conversation_active_task(active.id, task.id)

        # Inline buttons for next actions
        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(active.id, CONTINUE)},
        ]]

        return ControllerResponse(
            "我先看一下。完成后会把结论发在这里。",
            buttons=buttons,
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
            mode=self._default_mode,
            workspace_alias=self._default_workspace,
        )
        mode_label = MODE_LABELS.get(self._default_mode, self._default_mode)
        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(convo.id, CONTINUE)},
            {"text": "切换模式", "callback_data": encode_conversation_callback(convo.id, NEW_CONVO)},
        ]]
        return ControllerResponse(
            f"新对话已创建：「{convo.title}」\n"
            f"模式：{mode_label}\n"
            f"工作区：{convo.workspace_alias}\n\n"
            f"直接发消息开始对话，或用 /codex /claude /auto 切换模式。",
            buttons=buttons,
        )

    async def handle_codex_direct(
        self, command: CodexDirectCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Codex Direct Mode — analysis only, never enters implementation loop."""
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        user_id = ctx.get("user_id", 0) if ctx else 0

        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            title = default_title(command.prompt)
            active = self._ledger.create_conversation(
                chat_id=chat_id,
                user_id=user_id,
                title=title,
                mode=ConversationMode.CODEX_DIRECT.value,
                workspace_alias=self._default_workspace,
            )

        budget = ContextBudget()
        packet = build_codex_analysis_packet(
            user_goal=command.prompt,
            conversation_summary=trim_to_budget(
                active.conversation_summary, budget.conversation_summary_tokens
            ),
            constraints=[],
            workspace=active.workspace_alias,
            budget=budget,
        )

        task = self._service.reserve_task(
            active.workspace_alias,
            command.prompt,
            telegram_chat_id=chat_id,
        )
        self._ledger.set_conversation_active_task(active.id, task.id)

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
        try:
            thread_id = await self._backend.create_thread(workspace_path)
            self._service.set_task_thread(task.id, thread_id)
            await self._backend.start_turn(thread_id, packet.render())
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            return ControllerResponse(classify_user_error(exc))

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(f"用户请求：{command.prompt[:200]}", budget.conversation_summary_tokens),
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role="analysis",
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.set_conversation_active_task(active.id, task.id)

        # When interaction renderer is active, EventBridge forwards deltas and
        # terminal events — don't send a duplicate static response.
        if self._interaction_renderer is not None:
            # Emit run_started so typing indicator begins
            from wlcodex.interaction.events import InteractionEvent
            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_started",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                )
            )
            return ControllerResponse("", already_rendered=True)

        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(active.id, CONTINUE)},
        ]]

        return ControllerResponse(
            "我先看一下。完成后会把结论发在这里。",
            buttons=buttons,
        )

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
                workspace_alias=self._default_workspace,
            )

        from wlcodex.agent_backend import AgentRequest
        from wlcodex.context_packets import build_claude_handoff_packet

        packet = build_claude_handoff_packet(
            user_goal=command.prompt,
            codex_analysis="",  # Direct mode, no Codex analysis
            workspace=active.workspace_alias,
        )
        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)

        # Create a hidden task for tracking (NOT set as active_codex_task_id)
        task = self._service.reserve_task(
            active.workspace_alias,
            command.prompt,
            telegram_chat_id=chat_id,
        )

        # Use streaming path when interaction renderer is available (natural profile)
        if self._interaction_renderer is not None and hasattr(self._claude, "send_streaming"):
            from wlcodex.interaction.events import InteractionEvent

            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_started",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                )
            )

            accumulated = ""
            had_error = False
            claude_usage: dict | None = None
            try:
                async for stream_event in self._claude.send_streaming(
                    AgentRequest(prompt=packet.render(), workspace_path=workspace_path)
                ):
                    if stream_event.event_type == "error":
                        had_error = True
                        accumulated += stream_event.delta
                        break
                    if stream_event.event_type == "usage" and stream_event.usage:
                        claude_usage = stream_event.usage
                        continue
                    accumulated += stream_event.delta
                    await self._interaction_renderer.handle(
                        InteractionEvent(
                            event_type="text_delta",
                            chat_id=chat_id,
                            task_id=task.id,
                            conversation_id=active.id,
                            text=stream_event.delta,
                        )
                    )
            except Exception as exc:
                logger.exception("Claude streaming failed")
                self._service.fail_task(task.id, str(exc))
                agent_run = self._ledger.create_agent_run(
                    conversation_id=active.id,
                    agent="claude",
                    role="implementation",
                    prompt_packet_summary=command.prompt[:120],
                )
                self._ledger.update_agent_run_status(
                    agent_run.id,
                    "failed",
                    token_input=len(packet.render()) // 4,
                    completion_summary=str(exc)[:2000],
                )
                self._ledger.set_conversation_active_claude_run(active.id, agent_run.id)
                from wlcodex.claude_backend import record_claude_usage_event
                record_claude_usage_event(
                    self._ledger,
                    prompt=packet.render(),
                    output_text=str(exc),
                    conversation_id=active.id,
                    agent_run_id=agent_run.id,
                    task_id=task.id,
                    usage=claude_usage,
                    status="failed",
                )
                await self._interaction_renderer.handle(
                    InteractionEvent(
                        event_type="run_failed",
                        chat_id=chat_id,
                        task_id=task.id,
                        text=str(exc),
                    )
                )
                return ControllerResponse("", already_rendered=True)

            if had_error:
                self._service.fail_task(task.id, accumulated[:500] or "Claude streaming returned error")
                agent_run = self._ledger.create_agent_run(
                    conversation_id=active.id,
                    agent="claude",
                    role="implementation",
                    prompt_packet_summary=command.prompt[:120],
                )
                self._ledger.update_agent_run_status(
                    agent_run.id,
                    "failed",
                    token_input=len(packet.render()) // 4,
                    token_output=len(accumulated) // 4,
                    completion_summary=accumulated[:2000],
                )
                self._ledger.set_conversation_active_claude_run(active.id, agent_run.id)
                from wlcodex.claude_backend import record_claude_usage_event
                record_claude_usage_event(
                    self._ledger,
                    prompt=packet.render(),
                    output_text=accumulated,
                    conversation_id=active.id,
                    agent_run_id=agent_run.id,
                    task_id=task.id,
                    usage=claude_usage,
                    status="failed",
                )
                await self._interaction_renderer.handle(
                    InteractionEvent(
                        event_type="run_failed",
                        chat_id=chat_id,
                        task_id=task.id,
                        text=accumulated[:500] or "Claude 返回了错误。",
                    )
                )
                return ControllerResponse("", already_rendered=True)

            self._ledger.set_task_status(task.id, TaskStatus.DONE)
            agent_run = self._ledger.create_agent_run(
                conversation_id=active.id,
                agent="claude",
                role="implementation",
                prompt_packet_summary=command.prompt[:120],
            )
            self._ledger.update_agent_run_status(
                agent_run.id,
                "done",
                token_input=len(packet.render()) // 4,
                token_output=len(accumulated) // 4,
                completion_summary=accumulated[:2000],
            )
            self._ledger.set_conversation_active_claude_run(active.id, agent_run.id)
            from wlcodex.claude_backend import record_claude_usage_event
            record_claude_usage_event(
                self._ledger,
                prompt=packet.render(),
                output_text=accumulated,
                conversation_id=active.id,
                agent_run_id=agent_run.id,
                task_id=task.id,
                usage=claude_usage,
                status="done",
            )

            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_completed",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                    metadata={
                        "has_diff": (
                            _workspace_has_changes(workspace_path)
                            or bool(task.changed_file_count)
                        ),
                    },
                )
            )
            return ControllerResponse("", already_rendered=True)

        # Legacy path: blocking send()
        result = await self._claude.send(AgentRequest(
            prompt=packet.render(),
            workspace_path=workspace_path,
        ))
        self._ledger.set_task_status(task.id, TaskStatus.DONE)

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
            completion_summary=result.text[:2000],
        )
        self._ledger.set_conversation_active_claude_run(active.id, agent_run.id)
        from wlcodex.claude_backend import record_claude_usage_event
        record_claude_usage_event(
            self._ledger,
            prompt=packet.render(),
            output_text=result.text,
            conversation_id=active.id,
            agent_run_id=agent_run.id,
            task_id=task.id,
            status="done",
        )

        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看 diff", "callback_data": encode_conversation_callback(active.id, DIFF)},
            {"text": "Codex 验收", "callback_data": encode_conversation_callback(active.id, VERIFY)},
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
                workspace_alias=self._default_workspace,
            )

        cid = self._new_correlation_id()
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.TELEGRAM,
            actor="user",
            visibility=Visibility.USER,
            payload={"text_preview": command.prompt[:200]},
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        return await self._handle_chief_engineer_impl(command, active, ctx, correlation_id=cid)

    async def _handle_chief_engineer_impl(
        self,
        command: AutoModeCommand,
        active: object,
        ctx: dict[str, Any] | None = None,
        correlation_id: str = "",
    ) -> ControllerResponse:
        """Shared chief-engineer orchestration loop: Codex→Claude→Codex verify.

        Called by both handle_auto_mode (/auto) and handle_conversation_text
        (default plain-text in chief_engineer mode).

        When the interaction renderer is available (natural profile + streaming
        enabled), uses the streaming orchestrator path so Claude deltas and
        phase transitions are visible in real time.
        """
        from wlcodex.orchestrator import ChiefEngineerOrchestrator, OrchestrationProgress

        cid = correlation_id or self._new_correlation_id()

        # Create orchestration run record
        orch_run = self._ledger.create_orchestration_run(
            conversation_id=active.id,
            goal=command.prompt,
        )

        # Create Codex analysis agent run
        codex_analysis_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role="analysis",
        )
        self._ledger.update_agent_run_status(codex_analysis_run.id, "running")

        # Emit run.requested
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.RUN_REQUESTED,
            aggregate_type=AggregateType.ORCHESTRATION_RUN,
            aggregate_id=str(orch_run.id),
            correlation_id=cid,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={"goal": command.prompt, "max_verify_rounds": 3},
            occurred_at=now_iso(),
            conversation_id=active.id,
            orchestration_run_id=orch_run.id,
        ))

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)

        if (
            self._interaction_renderer is not None
            and self._orchestration_runner is not None
        ):
            from wlcodex.interaction.events import InteractionEvent

            chat_id = ctx.get("chat_id", 0) if ctx else 0
            task = self._service.reserve_task(
                active.workspace_alias,
                command.prompt,
                telegram_chat_id=chat_id,
            )
            self._ledger.set_conversation_active_task(active.id, task.id)
            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_started",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                )
            )
            self._orchestration_runner.start_chief_engineer(
                prompt=command.prompt,
                conversation=active,
                task_id=task.id,
                orchestration_run_id=orch_run.id,
                codex_analysis_run_id=codex_analysis_run.id,
                chat_id=chat_id,
                workspace_path=workspace_path,
                correlation_id=cid,
            )
            return ControllerResponse("", already_rendered=True)

        # Streaming path: forward live progress to interaction renderer
        if self._interaction_renderer is not None:
            from wlcodex.interaction.events import InteractionEvent

            chat_id = ctx.get("chat_id", 0) if ctx else 0
            task = self._service.reserve_task(
                active.workspace_alias,
                command.prompt,
                telegram_chat_id=chat_id,
            )
            self._ledger.set_conversation_active_task(active.id, task.id)
            orch = ChiefEngineerOrchestrator(
                _TaskBoundCodexBackend(self._backend, self._service, task.id),
                self._claude,
            )

            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_started",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                )
            )

            terminal_sent = False
            orch_result_status = "running"
            verify_round = 0
            codex_analysis_text = ""
            claude_implementation_text = ""
            verification_text = ""
            terminal_text = ""
            codex_analysis_status = "running"
            implementation_notice_sent = False
            try:
                async for progress in orch.run_streaming(
                    command.prompt,
                    conversation_context={"workspace": workspace_path},
                ):
                    if terminal_sent:
                        continue
                    if progress.phase == OrchestrationProgress.IMPL_DELTA and progress.text:
                        claude_implementation_text += progress.text
                        if not implementation_notice_sent:
                            implementation_notice_sent = True
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="text_delta",
                                    chat_id=chat_id,
                                    task_id=task.id,
                                    conversation_id=active.id,
                                    text="\n\n" + render_user_progress_text(
                                        progress.phase,
                                        first_impl_delta=True,
                                    ),
                                )
                            )
                    elif progress.phase in (
                        OrchestrationProgress.ANALYSIS_STARTED,
                        OrchestrationProgress.ANALYSIS_COMPLETE,
                        OrchestrationProgress.IMPL_COMPLETE,
                        OrchestrationProgress.VERIFY_STARTED,
                        OrchestrationProgress.VERIFY_COMPLETE,
                    ):
                        user_text = render_user_progress_text(progress.phase)
                        if user_text:
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="text_delta",
                                    chat_id=chat_id,
                                    task_id=task.id,
                                    conversation_id=active.id,
                                    text="\n\n" + user_text,
                                )
                            )
                        if progress.phase == OrchestrationProgress.ANALYSIS_COMPLETE:
                            codex_analysis_text = progress.full_text or progress.text
                            self._ledger.update_agent_run_status(
                                codex_analysis_run.id,
                                "done",
                                completion_summary=codex_analysis_text[:2000],
                            )
                            codex_analysis_status = "done"
                        elif progress.phase == OrchestrationProgress.IMPL_COMPLETE:
                            claude_implementation_text = progress.full_text or progress.text
                            claude_run = self._ledger.create_agent_run(
                                conversation_id=active.id,
                                agent="claude",
                                role="implementation",
                                prompt_packet_summary=command.prompt[:120],
                            )
                            self._ledger.update_agent_run_status(
                                claude_run.id,
                                "done",
                                completion_summary=claude_implementation_text[:2000],
                            )
                            self._ledger.set_conversation_active_claude_run(
                                active.id, claude_run.id
                            )
                        elif progress.phase == OrchestrationProgress.VERIFY_STARTED:
                            verify_round = progress.round_num or verify_round
                        elif progress.phase == OrchestrationProgress.VERIFY_COMPLETE:
                            verify_round = progress.round_num or verify_round
                            verification_text = progress.full_text or progress.text
                            verify_run = self._ledger.create_agent_run(
                                conversation_id=active.id,
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
                        self._ledger.set_task_status(task.id, TaskStatus.FAILED)
                        if progress.agent == "claude":
                            claude_run = self._ledger.create_agent_run(
                                conversation_id=active.id,
                                agent="claude",
                                role="implementation",
                                prompt_packet_summary=command.prompt[:120],
                            )
                            self._ledger.update_agent_run_status(
                                claude_run.id,
                                "failed",
                                completion_summary=terminal_text[:2000],
                            )
                            self._ledger.set_conversation_active_claude_run(
                                active.id, claude_run.id
                            )
                        elif progress.agent == "codex" and not codex_analysis_text:
                            self._ledger.update_agent_run_status(
                                codex_analysis_run.id,
                                "failed",
                                completion_summary=terminal_text[:2000],
                            )
                            codex_analysis_status = "failed"
                        await self._interaction_renderer.handle(
                            InteractionEvent(
                                event_type="run_failed",
                                chat_id=chat_id,
                                task_id=task.id,
                                text=progress.text,
                            )
                        )
                    elif progress.phase == OrchestrationProgress.COMPLETE:
                        terminal_sent = True
                        orch_result_status = getattr(progress, "result_status", "") or "passed"
                        verify_round = progress.round_num or verify_round
                        if progress.text or progress.full_text:
                            terminal_text = progress.full_text or progress.text
                        if orch_result_status == "passed":
                            self._ledger.set_task_status(task.id, TaskStatus.DONE)
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="run_completed",
                                    chat_id=chat_id,
                                    task_id=task.id,
                                    conversation_id=active.id,
                                    metadata={
                                        "has_diff": (
                                            _workspace_has_changes(workspace_path)
                                            or bool(task.changed_file_count)
                                        ),
                                    },
                                )
                            )
                        elif orch_result_status == "needs_user":
                            self._ledger.set_task_status(task.id, TaskStatus.FAILED)
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="run_failed",
                                    chat_id=chat_id,
                                    task_id=task.id,
                                    text=progress.text or "需要用户输入以继续。",
                                )
                            )
                        else:
                            self._ledger.set_task_status(task.id, TaskStatus.FAILED)
                            await self._interaction_renderer.handle(
                                InteractionEvent(
                                    event_type="run_failed",
                                    chat_id=chat_id,
                                    task_id=task.id,
                                    text=progress.text or "编排未通过验收。",
                                )
                            )
            except Exception as exc:
                logger.exception("Chief engineer streaming orchestration failed")
                self._ledger.set_task_status(task.id, TaskStatus.FAILED)
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="failed",
                    current_step="error",
                    last_verification_result=str(exc)[:500],
                )
                self._ledger.update_agent_run_status(
                    codex_analysis_run.id,
                    "failed",
                    completion_summary=str(exc)[:2000],
                )
                return ControllerResponse("", already_rendered=True)

            # Update ledger records
            if not codex_analysis_text and orch_result_status == "failed":
                codex_analysis_text = terminal_text
            if verification_text:
                decision = (
                    "verify_passed"
                    if orch_result_status == "passed"
                    else "verify_failed_retry"
                )
                self._ledger.record_orchestration_decision(
                    run_id=orch_run.id,
                    decision=decision,
                    reason=verification_text[:500],
                    next_agent="" if orch_result_status == "passed" else "claude",
                )
            self._ledger.update_orchestration_run(
                orch_run.id,
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
                    codex_analysis_run.id,
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
                active.id,
                trim_to_budget(
                    f"总工程师第{verify_round}轮: {label}",
                    ContextBudget().conversation_summary_tokens,
                ),
            )
            return ControllerResponse("", already_rendered=True)

        # Legacy path: blocking orchestration
        orch = ChiefEngineerOrchestrator(self._backend, self._claude)
        try:
            result = await orch.run(
                command.prompt,
                conversation_context={"workspace": workspace_path},
            )
        except Exception as exc:
            logger.exception("Chief engineer orchestration failed")
            self._ledger.update_orchestration_run(
                orch_run.id,
                status="failed",
                current_step="error",
                last_verification_result=str(exc)[:500],
            )
            self._ledger.update_agent_run_status(
                codex_analysis_run.id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(
                f"总工程师编排失败：{exc}\n\n"
                f"对话：{active.title}\n"
                f"错误已记录到运行日志。可用 /status 查看详情。"
            )

        # Update Codex analysis run with result
        agent_run_status = "done" if result.status == "passed" else "failed"
        self._ledger.update_agent_run_status(
            codex_analysis_run.id,
            agent_run_status,
            completion_summary=result.codex_analysis[:2000] if result.codex_analysis else "",
        )

        # Create Claude implementation agent runs from steps
        for step in result.steps:
            if step.agent == "claude":
                claude_run = self._ledger.create_agent_run(
                    conversation_id=active.id,
                    agent="claude",
                    role="implementation",
                    prompt_packet_summary=step.summary[:120],
                )
                self._ledger.update_agent_run_status(
                    claude_run.id,
                    "done",
                    completion_summary=step.summary[:2000],
                )
                self._ledger.set_conversation_active_claude_run(active.id, claude_run.id)

        # Create Codex verification agent run
        if result.verification_summary:
            verify_run = self._ledger.create_agent_run(
                conversation_id=active.id,
                agent="codex",
                role="verification",
                prompt_packet_summary=result.verification_summary[:120],
            )
            self._ledger.update_agent_run_status(
                verify_run.id,
                "done",
                completion_summary=result.verification_summary[:2000],
            )

        # Record orchestration decisions
        for step in result.steps:
            if step.step.startswith("verify"):
                decision = "verify_passed" if result.status == "passed" else "verify_failed_retry"
                self._ledger.record_orchestration_decision(
                    run_id=orch_run.id,
                    decision=decision,
                    reason=step.summary[:500],
                    next_agent="claude" if result.status != "passed" else "",
                )

        # Update orchestration run
        self._ledger.update_orchestration_run(
            orch_run.id,
            status=result.status,
            verify_round=result.verify_round,
            current_step="verify" if result.status == "passed" else "retry",
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

        # Build comprehensive buttons for all outcomes
        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看 diff", "callback_data": encode_conversation_callback(active.id, DIFF)},
            {"text": "Codex 验收", "callback_data": encode_conversation_callback(active.id, VERIFY)},
        ]]
        if result.status == "failed":
            buttons.append([
                {"text": "继续修改", "callback_data": encode_conversation_callback(active.id, RETRY)},
            ])
        elif result.status == "needs_user":
            buttons.append([
                {"text": "继续修改", "callback_data": encode_conversation_callback(active.id, RETRY)},
                {"text": "停止", "callback_data": encode_conversation_callback(active.id, CONTINUE)},
            ])

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"总工程师第{result.verify_round}轮: {label}",
                ContextBudget().conversation_summary_tokens,
            ),
        )

        return ControllerResponse(
            f"总工程师编排完成 — {label}\n\n"
            f"对话：{active.title}\n"
            f"轮次：第 {result.verify_round} 轮\n"
            f"步骤数：{len(result.steps)}\n"
            f"Codex 分析：{_telegram_visible_model_summary(result.codex_analysis, 300)}\n"
            f"Claude 实施：{_telegram_visible_model_summary(result.claude_implementation, 300)}\n"
            f"验证结果：{_telegram_visible_model_summary(result.verification_summary, 300)}",
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

        # Find the latest completed Claude run for verification evidence
        latest_claude_run: object | None = None
        for r in reversed(runs):
            if r.agent == "claude" and r.status == "done":
                latest_claude_run = r
                break
        if latest_claude_run is None:
            latest_claude_run = runs[-1]

        # Collect diff evidence from the workspace
        diff_summary = ""
        changed_files: list[str] = []
        test_results = ""
        try:
            workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
            diff_result = self._inspector.diff(
                latest_claude_run.hidden_task_id or 0, workspace_path
            )
            if diff_result and diff_result.body:
                diff_summary = diff_result.body[:1500]
            # Extract file paths from diff or use inspector
            files_result = self._inspector.files(
                latest_claude_run.hidden_task_id or 0
            )
            if files_result and files_result.body:
                for line in files_result.body.split("\n"):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        changed_files.append(stripped[:200])

            # Collect test evidence from workspace
            test_files = [f for f in changed_files if "test" in f.lower()]
            if test_files:
                test_results = (
                    "Tests were modified in this change. "
                    "Test commands were NOT executed — manual verification required. "
                    f"Modified test files: {', '.join(test_files[:10])}"
                )
            else:
                test_results = (
                    "No test files were modified in this change. "
                    "Manual verification of correctness is required."
                )
        except Exception:
            pass

        # Get actual Claude completion output (not the input prompt)
        completion = getattr(latest_claude_run, "completion_summary", "")
        if not completion:
            completion = getattr(latest_claude_run, "prompt_packet_summary", "")

        # Build verification packet with real evidence
        verify_payload = command.prompt if command.prompt else active.title
        codex_plan = ""
        orch_runs = self._ledger.list_orchestration_runs(active.id, limit=1)
        if orch_runs:
            codex_plan = orch_runs[0].last_codex_analysis

        packet = make_verification_packet(
            user_goal=verify_payload,
            codex_plan_summary=codex_plan[:800] if codex_plan else "",
            claude_completion_summary=completion[:1500] if completion else "",
            changed_files=changed_files[:20],
            diff_summary=diff_summary[:1500] if diff_summary else "",
            test_results=test_results,
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
                                        f"变更文件：{len(changed_files)} 个\n" \
                                        f"验收证据：Codex 分析计划 + Claude 输出摘要 + diff"
                else:
                    verification_text = "Codex 线程未就绪，无法发送验收。"
            else:
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
                                    f"验收内容：{verify_payload}\n" \
                                    f"变更文件：{len(changed_files)} 个"
        except Exception as exc:
            logger.warning("verify: Codex call failed: %s", exc)
            verification_text = f"Codex 验收请求失败：{exc}"

        return ControllerResponse(
            f"Codex 验收 — 对话「{active.title}」\n\n"
            f"最近运行：#{latest_claude_run.id}（{latest_claude_run.agent}/{latest_claude_run.status}）\n"
            f"输入摘要：{latest_claude_run.prompt_packet_summary[:200]}\n"
            f"输出摘要：{completion[:200] if completion else '(无)'}\n"
            f"变更文件：{len(changed_files)} 个\n"
            f"Token：{latest_claude_run.token_input} 输入 / {latest_claude_run.token_output} 输出\n\n"
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

        stopped_items: list[str] = []

        # Abort the active Codex task if any
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
                stopped_items.append(f"Codex 任务 #{active.active_codex_task_id}")
            except KeyError:
                pass

        # Interrupt the active Claude run if any
        if active.active_claude_run_id:
            if self._claude is not None:
                try:
                    self._claude.interrupt()
                except Exception as exc:
                    logger.warning("Claude interrupt failed: %s", exc)
            self._ledger.update_agent_run_status(
                active.active_claude_run_id, "aborted"
            )
            stopped_items.append(f"Claude 运行 #{active.active_claude_run_id}")

        detail = "；".join(stopped_items) if stopped_items else "无活跃任务"
        return ControllerResponse(
            f"对话「{active.title}」已停止。\n{detail}"
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

    async def handle_claude_permission(
        self,
        command: ClaudePermissionCommand,
        ctx: dict[str, Any] | None = None,
    ) -> ControllerResponse:
        current = self._current_claude_permission_mode()

        if command.mode_name:
            try:
                current = self._set_claude_permission_mode(command.mode_name)
            except ValueError as exc:
                return ControllerResponse(
                    f"{exc}\n\n{render_claude_permission_status(current)}",
                    buttons=build_claude_permission_buttons(current),
                )

        text = render_claude_permission_status(current)
        if command.mode_name:
            text = f"已切换 Claude 权限模式。\n\n{text}"
        if self._claude is None or not getattr(self._claude, "enabled", False):
            text += "\n\n提示：Claude 后端当前未启用，此设置会在启用后生效。"
        return ControllerResponse(
            text,
            buttons=build_claude_permission_buttons(current),
        )

    def _current_claude_permission_mode(self) -> str:
        if self._claude_permission_state is not None:
            return self._claude_permission_state.get()
        if self._claude is not None and hasattr(self._claude, "permission_mode"):
            return str(getattr(self._claude, "permission_mode"))
        return DEFAULT_CLAUDE_PERMISSION_MODE

    def _set_claude_permission_mode(self, mode_name: str) -> str:
        if self._claude_permission_state is None:
            self._claude_permission_state = ClaudePermissionState(
                self._current_claude_permission_mode()
            )
        current = self._claude_permission_state.set(mode_name)
        if self._claude is not None and hasattr(self._claude, "set_permission_mode"):
            self._claude.set_permission_mode(current)
        if self._ledger is not None and hasattr(self._ledger, "set_runtime_setting"):
            self._ledger.set_runtime_setting(RUNTIME_CLAUDE_PERMISSION_MODE_KEY, current)
        return current

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

    # --- Conversation callback handler ---

    async def handle_conversation_callback(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """Route conversation inline button callbacks (conv:* protocol)."""
        try:
            convo = self._ledger.get_conversation(callback.conversation_id)
        except KeyError:
            return ControllerResponse("对话不存在或已被删除。")

        if callback.action == DIFF:
            return await self.handle(
                "/diff",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == VERIFY:
            return await self.handle(
                "/verify",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == RETRY:
            # Re-run the orchestrator with the conversation's last goal
            orch_runs = self._ledger.list_orchestration_runs(
                callback.conversation_id, limit=1
            )
            if orch_runs:
                goal = orch_runs[0].goal
                return await self.handle(
                    f"/auto {goal}",
                    {"chat_id": convo.chat_id, "user_id": convo.user_id},
                )
            return ControllerResponse("没有找到可重试的编排运行。")
        elif callback.action == CONTINUE:
            return await self.handle(
                "/new",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == NEW_CONVO:
            return await self.handle(
                "/new",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        else:
            return ControllerResponse(f"未知的对话操作：{callback.action}")

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


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _telegram_visible_model_summary(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if not stripped:
        return "暂无内容。"
    if not _contains_cjk(stripped) and any(char.isalpha() for char in stripped):
        return "模型返回了非中文内容，已隐藏原文；详细记录已保留在运行日志中。"
    return _trim_result_text(stripped, max_chars)


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
