"""Command controller — routes parsed commands to services, returns responses."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import subprocess
import uuid
from typing import Any

from wlcodex.health_snapshot import build_health_snapshot
from wlcodex.execution_scheduler import ExecutionScheduler, RunIntent
from wlcodex.interaction.errors import classify_user_error
from wlcodex.inspection import TaskInspector
from wlcodex.legacy_diagnostics import LegacyDiagnosticsController
from wlcodex.models import TaskStatus
from wlcodex.conversation_state_machine import (
    RouteDecision,
    classify_intent,
    route_message,
    MID_RUN_ACKNOWLEDGEMENT,
    build_workspace_busy_buttons,
    decode_busy_callback,
    BUSY_APPEND,
    BUSY_INTERRUPT,
    BUSY_QUEUE,
    BUSY_CANCEL,
    BUSY_NEW_SESSION,
)
from wlcodex.conversation import default_title, mode_from_command
from wlcodex.conversation_callback import (
    CARRY_CANCEL,
    CARRY_REFRESH,
    CARRY_SHOW,
    CARRY_START,
    CONTINUE,
    DIFF,
    NEW_CONVO,
    RESTORE_WORKBENCH,
    RETRY,
    STATUS,
    VERIFY,
    AUTO_CALLBACK_ACTIONS,
    ConversationCallback,
    decode_conversation_callback,
    encode_conversation_callback,
)
from wlcodex.context_packets import (
    ContextBudget,
    build_codex_analysis_packet,
    build_codex_verification_packet as make_verification_packet,
    build_auto_context_packet,
    build_auto_final_plan_packet,
    build_auto_verification_packet,
    build_auto_repair_packet,
    trim_to_budget,
)
from wlcodex.auto_workflow import (
    AUTO_COLLECTING_CONTEXT,
    AUTO_DRAFT_READY,
    AUTO_CLAUDE_RUNNING,
    AUTO_CLAUDE_DONE,
    AUTO_VERIFYING,
    AUTO_RETRY_READY,
    AUTO_CODEX_TAKEOVER_RUNNING,
    AUTO_COMPLETED,
    AUTO_FINAL_PLAN,
    AUTO_SHOW_DRAFT,
    AUTO_CANCEL,
    AUTO_SEND_TO_CLAUDE,
    AUTO_CONTINUE_CONTEXT,
    AUTO_REWRITE_PLAN,
    AUTO_CODEX_TAKEOVER,
    AUTO_CLOSE,
    AUTO_CODEX_VERIFY,
    AUTO_SEND_REPAIR_TO_CLAUDE,
    AUTO_REWRITE_REPAIR,
    AUTO_INTERRUPT_CLAUDE,
    AUTO_VIEW_DIFF,
    AUTO_VIEW_STATUS,
    ROLE_AUTO_ANALYSIS,
    ROLE_AUTO_CONTEXT_SUPPLEMENT,
    ROLE_AUTO_FINAL_PLAN,
    ROLE_AUTO_VERIFICATION,
    ROLE_AUTO_IMPLEMENTATION,
    ROLE_AUTO_REPAIR,
    ROLE_AUTO_CODEX_TAKEOVER,
    auto_stage_label,
    build_auto_stage_buttons,
    is_active_auto_stage,
    is_auto_collecting_context,
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
    safe_text_preview,
)
from wlcodex.carryover import (
    CarryoverSource,
    build_continuity_brief,
    build_carryover_preview,
    build_source_fingerprint,
)
from wlcodex.router import (
    AutoModeCommand,
    CarryWorkbenchCommand,
    ClaudeDirectCommand,
    ClaudePermissionCommand,
    CodexDirectCommand,
    CodexSessionsCommand,
    DiffCommand,
    ExecModeCommand,
    FilesCommand,
    HealthCommand,
    HelpCommand,
    ModelCommand,
    NewConversationCommand,
    ParseError,
    StatusCommand,
    StopCurrentCommand,
    SwitchWorkspaceCommand,
    TraceCommand,
    VerifyCommand,
    WorkbenchHistoryCommand,
    WorkspaceListCommand,
    parse_command,
)
from wlcodex.status import (
    MODE_LABELS,
    render_carryover_brief_view,
    render_carryover_cancelled,
    render_carryover_candidates,
    render_carryover_target_created,
    render_conversation_help,
    render_conversation_status,
    render_prepared_carryover,
    render_workspace_list,
)
from wlcodex.models import ConversationMode
from wlcodex.status import (
    render_health_card,
    render_help,
)
from wlcodex.task_service import TaskService

logger = logging.getLogger(__name__)

HELP_TEXT = render_conversation_help()


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
        execution_scheduler: object | None = None,
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
        self._execution_scheduler = (
            execution_scheduler
            if execution_scheduler is not None
            else ExecutionScheduler(task_service, ledger) if ledger is not None else None
        )
        self._legacy_diagnostics = LegacyDiagnosticsController(
            task_service, backend, inspector
        )
        self._background_tasks: set[asyncio.Task[None]] = set()

    def set_interaction_renderer(self, renderer: object) -> None:
        """Set the interaction renderer after construction (created after handlers)."""
        self._interaction_renderer = renderer

    def set_orchestration_runner(self, runner: object | None) -> None:
        """Set the background orchestration runner after runtime wiring."""
        self._orchestration_runner = runner

    def set_legacy_diagnostics(self, adapter: object) -> None:
        """Replace the legacy diagnostics adapter in tests or composition."""
        self._legacy_diagnostics = adapter

    def set_execution_scheduler(self, scheduler: object) -> None:
        """Replace the internal execution scheduler in tests or composition."""
        self._execution_scheduler = scheduler

    @property
    def execution_scheduler(self) -> object | None:
        """Return the internal Workbench execution scheduler."""
        return self._execution_scheduler

    def _emit_event(self, event: RuntimeEvent) -> RuntimeEvent:
        if self._store is None:
            return event
        return self._store.append(event)

    def _new_correlation_id(self) -> str:
        return str(uuid.uuid4())

    def _reserve_execution_lease(
        self,
        *,
        conversation_id: int,
        workspace_alias: str,
        prompt: str,
        telegram_chat_id: int | None,
        purpose: str,
    ) -> object:
        if self._execution_scheduler is None:
            raise RuntimeError("execution scheduler unavailable")
        lease = self._execution_scheduler.reserve(RunIntent(
            conversation_id=conversation_id,
            workspace_alias=workspace_alias,
            prompt=prompt,
            telegram_chat_id=telegram_chat_id,
            purpose=purpose,
        ))
        task = getattr(lease, "task", None)
        if task is not None:
            return task
        return self._service.get_task(lease.hidden_task_id)

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
            if self._legacy_diagnostics.can_handle(command):
                response = await self._legacy_diagnostics.handle(
                    command, telegram_context
                )
                return ControllerResponse(
                    response.text,
                    buttons=getattr(response, "buttons", []),
                    already_rendered=getattr(response, "already_rendered", False),
                )

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

            elif isinstance(command, StatusCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        runs = self._ledger.list_agent_runs(active.id, limit=1)
                        latest_run = runs[0] if runs else None
                        orch_runs = self._ledger.list_orchestration_runs(active.id, limit=1)
                        orch_run = orch_runs[0] if orch_runs else None
                        surface_mode = self._latest_surface_mode(active.id) or "product"
                        return ControllerResponse(
                            render_conversation_status(
                                active, latest_run=latest_run, orch_run=orch_run,
                                surface_mode=surface_mode,
                            )
                        )
                return ControllerResponse(
                    "当前还没有工作台。发送 /new 开始一个新的工作台。"
                )

            elif isinstance(command, TraceCommand):
                if self._ledger is None or self._store is None:
                    return ControllerResponse("运行时事件记录不可用。")
                chat_id = telegram_context.get("chat_id", 0) if telegram_context else 0
                active = self._ledger.get_active_conversation(chat_id)
                if active is None:
                    return ControllerResponse("暂无活跃对话。")
                result = self._inspector.trace_runtime(
                    self._store,
                    active.id,
                    limit=command.limit,
                    visibility_filter="operator",
                )
                return ControllerResponse(f"{result.title}\n\n{result.body}")

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
                    return ControllerResponse(
                        "当前还没有可查看变更的工作台。发送 /new 开始新的工作台。"
                    )
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
                    return ControllerResponse(
                        "当前还没有可查看文件的工作台。发送 /new 开始新的工作台。"
                    )
                result = self._inspector.files(task_id)
                return ControllerResponse(
                    f"{result.title}\n\n{result.body}"
                )

            elif isinstance(command, CodexSessionsCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        from wlcodex.workbench.rendering import render_session_library
                        from wlcodex.workbench.sessions import AgentSessionLibrary

                        sessions = AgentSessionLibrary(self._ledger).list_for_workbench(
                            active.id
                        )
                        return ControllerResponse(render_session_library(sessions))
                return ControllerResponse(
                    "当前还没有工作台。发送 /new 开始一个新的工作台。"
                )

            elif isinstance(command, WorkbenchHistoryCommand):
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    sessions = self._ledger.list_conversations_by_chat(
                        chat_id, include_archived=True
                    )
                    active_count = sum(1 for s in sessions if s.archived_at is None)
                    archived_count = sum(1 for s in sessions if s.archived_at is not None)
                    if not sessions:
                        return ControllerResponse(
                            "还没有历史工作台。发送 /new 开始新的工作台。"
                        )
                    return ControllerResponse(
                        f"工作台历史 — {active_count} 个进行中，{archived_count} 个已归档\n"
                        f"点击下方按钮恢复已归档的工作台："
                    )
                return ControllerResponse(
                    "还没有历史工作台。发送 /new 开始新的工作台。"
                )

            elif isinstance(command, WorkspaceListCommand):
                active_alias = ""
                if self._ledger is not None and telegram_context:
                    chat_id = telegram_context.get("chat_id", 0)
                    active = self._ledger.get_active_conversation(chat_id)
                    if active is not None:
                        active_alias = active.workspace_alias
                workspaces = list(self._service._workspaces.values())
                return ControllerResponse(
                    render_workspace_list(workspaces, active_alias=active_alias),
                    buttons=self._workspace_selection_buttons(
                        workspaces, active_alias=active_alias
                    ),
                )

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

            elif isinstance(command, ExecModeCommand):
                return await self.handle_exec_mode(command, telegram_context)

            elif isinstance(command, VerifyCommand):
                return await self.handle_verify(command, telegram_context)

            elif isinstance(command, CarryWorkbenchCommand):
                return await self.handle_carry_workbench(command, telegram_context)

            else:
                return ControllerResponse("未处理的命令类型。")

        except Exception as exc:
            logger.exception("Command handler error")
            return ControllerResponse(f"错误：{exc}")

    # --- Conversation handlers ---

    def _workspace_selection_buttons(
        self, workspaces: list[object], *, active_alias: str = ""
    ) -> list[list[dict[str, str]]]:
        buttons: list[list[dict[str, str]]] = []
        for workspace in workspaces:
            alias = str(getattr(workspace, "alias", "")).strip()
            if not alias:
                continue
            label = f"当前 {alias}" if alias == active_alias else f"切换 {alias}"
            buttons.append([{
                "text": label,
                "callback_data": f"settings:workspace:{alias}",
            }])
        return buttons

    async def handle_conversation_text(
        self, text: str, telegram_context: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Route plain text through the conversation state machine.

        Diagnostic commands → read-only handler.
        Normal text → either creates new conversation or appends to active one
        based on conversation state.
        Never injects status/log into model prompts.
        """
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = telegram_context.get("chat_id", 0) if telegram_context else 0
        user_id = telegram_context.get("user_id", 0) if telegram_context else 0

        # --- Lightweight greeting: fast path ---
        if _is_lightweight_greeting(text):
            active = self._ledger.get_active_conversation(chat_id)
            if active is None:
                active = self._ledger.create_conversation(
                    chat_id=chat_id, user_id=user_id,
                    title=default_title(text),
                    mode=self._default_mode,
                    workspace_alias=self._default_workspace,
                )
            self._ledger.update_conversation_summary(
                active.id,
                trim_to_budget(
                    "用户打招呼，等待具体需求。",
                    ContextBudget().conversation_summary_tokens,
                ),
            )
            return ControllerResponse("你好！直接说需要我看什么就行。")

        # --- Route diagnostic commands to the existing command parser ---
        intent = classify_intent(text)
        if intent == "diagnostic":
            try:
                return await self.handle(text, telegram_context)
            except Exception:
                return ControllerResponse("命令处理出错，请重试。")

        # --- Consume pending carryover: next non-command text creates target ---
        pending = self._ledger.get_latest_prepared_carryover(chat_id)
        if pending is not None:
            return await self._consume_prepared_carryover(
                pending, text, telegram_context,
            )

        # --- Get active conversation and its runtime state ---
        active = self._ledger.get_active_conversation(chat_id)
        conv_state: str | None = None
        if active is not None and self._store is not None:
            conv_state = self._store.get_conversation_runtime_state(active.id)

        # --- Check workspace busy for new-conversation triggers ---
        workspace_busy = False
        blocking_task_id: int | None = None
        blocking_run_id: int | None = None
        if intent == "new_trigger" or active is None or conv_state in (None, "passed", "failed", "aborted", "done"):
            blocker = self._service.blocker_for_workspace(self._default_workspace)
            if blocker is not None:
                workspace_busy = True
                blocking_task_id = blocker.id

        # --- Route ---
        decision = route_message(
            text,
            active_conversation_id=active.id if active else None,
            active_conversation_state=conv_state,
            chat_id=chat_id,
            workspace_busy=workspace_busy,
            blocking_task_id=blocking_task_id,
            blocking_run_id=blocking_run_id,
        )

        # --- Emit routing events ---
        cid = self._new_correlation_id()
        if active is None and decision.route == "new_conversation":
            # Create conversation first so we have an id for events
            title = default_title(text)
            active = self._ledger.create_conversation(
                chat_id=chat_id, user_id=user_id,
                title=title, mode=self._default_mode,
                workspace_alias=self._default_workspace,
            )
            # Emit conversation.started
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_STARTED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"chat_id": chat_id, "title": title,
                         "mode": self._default_mode,
                         "workspace_alias": self._default_workspace},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))

        # Always emit user.message.received
        conv_id = active.id if active else 0
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(conv_id),
            correlation_id=cid,
            source=EventSource.TELEGRAM,
            actor="user",
            visibility=Visibility.USER,
            payload={
                "text_preview": safe_text_preview(text),
                "text_length": len(text),
                "chat_id": chat_id,
            },
            occurred_at=now_iso(),
            conversation_id=conv_id if conv_id else None,
        ))

        # --- Handle route ---
        if decision.route == "workspace_busy":
            return await self._handle_workspace_busy(
                decision, active, blocking_task_id, blocking_run_id, cid,
                original_text=text,
            )

        # /auto owns plain text while its staged workflow is active.  This must
        # run before the generic append/reanalysis route, otherwise supplement
        # text cancels the auto run and falls back to long Codex-direct output.
        if active is not None and self._ledger is not None:
            auto_run = self._latest_active_auto_run(active.id)
            if auto_run is not None:
                step = getattr(auto_run, "current_step", "")

                if step == AUTO_COLLECTING_CONTEXT:
                    return await self._handle_auto_context_supplement(
                        text, active, auto_run, telegram_context, cid
                    )

                if step == AUTO_DRAFT_READY:
                    self._ledger.update_orchestration_run(
                        auto_run.id,
                        status="running",
                        current_step=AUTO_COLLECTING_CONTEXT,
                    )
                    return await self._handle_auto_context_supplement(
                        text, active, auto_run, telegram_context, cid
                    )

                if step in (AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
                    self._ledger.update_conversation_summary(
                        active.id,
                        trim_to_budget(
                            f"{active.conversation_summary}\n[用户备注] {text[:200]}",
                            ContextBudget().conversation_summary_tokens,
                        ),
                    )
                    buttons = build_auto_stage_buttons(active.id, step)
                    return ControllerResponse(
                        f"已记录备注：{text[:100]}",
                        buttons=buttons,
                    )

        if decision.route == "append_active_conversation":
            return await self._handle_append_to_conversation(
                decision, text, active, telegram_context, cid
            )

        # --- new_conversation (default) ---
        # If active conversation is terminal, archive it first.
        if active is not None and conv_state in ("passed", "failed", "aborted", "done"):
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_CLOSED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"reason": conv_state, "next_conversation": True},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))
            self._ledger.archive_conversation(active.id)
            title = default_title(text)
            active = self._ledger.create_conversation(
                chat_id=chat_id, user_id=user_id,
                title=title, mode=self._default_mode,
                workspace_alias=self._default_workspace,
            )
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_STARTED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"chat_id": chat_id, "title": title,
                         "mode": self._default_mode,
                         "workspace_alias": self._default_workspace},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))

        # Emit conversation.message.routed
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_MESSAGE_ROUTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={
                "chat_id": chat_id,
                "route": decision.route,
                "reason": decision.reason,
                "conversation_state": conv_state,
                "conversation_id": active.id,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # --- Plain text: read-only Codex analysis by default.
        # Full Codex -> Claude -> Codex orchestration is explicit via /auto.
        return await self._handle_codex_analysis_only(
            text, active, telegram_context, cid
        )

    async def _start_codex_turn_for_conversation(
        self,
        *,
        active: object,
        task: object,
        workspace_path: str,
        prompt: str,
        interaction_mode: str = "general",
    ) -> str:
        thread_id = str(getattr(active, "codex_thread_id", "") or "")
        if thread_id:
            self._service.set_task_thread(task.id, thread_id)
            continue_prompt_turn = getattr(self._backend, "continue_prompt_turn", None)
            continue_turn = getattr(self._backend, "continue_turn", None)
            if interaction_mode != "general" and callable(continue_prompt_turn):
                await continue_prompt_turn(thread_id, prompt, interaction_mode)
            elif callable(continue_turn):
                await continue_turn(thread_id, prompt)
            else:
                await self._backend.start_turn(thread_id, prompt)
            return thread_id

        create_prompt_thread = getattr(self._backend, "create_prompt_thread", None)
        if interaction_mode != "general" and callable(create_prompt_thread):
            thread_id = await create_prompt_thread(workspace_path, interaction_mode)
        else:
            thread_id = await self._backend.create_thread(workspace_path)
        self._service.set_task_thread(task.id, thread_id)
        self._ledger.set_conversation_codex_thread(active.id, thread_id)
        start_prompt_turn = getattr(self._backend, "start_prompt_turn", None)
        if interaction_mode != "general" and callable(start_prompt_turn):
            await start_prompt_turn(thread_id, prompt, interaction_mode)
        else:
            await self._backend.start_turn(thread_id, prompt)
        return thread_id

    async def _handle_codex_analysis_only(
        self,
        text: str,
        active: object,
        ctx: dict[str, Any] | None,
        correlation_id: str,
    ) -> ControllerResponse:
        """Codex-direct or Claude-disabled: Codex analysis only."""
        chat_id = ctx.get("chat_id", 0) if ctx else 0
        budget = ContextBudget()
        packet = build_codex_analysis_packet(
            user_goal=text,
            conversation_summary=trim_to_budget(
                active.conversation_summary, budget.conversation_summary_tokens
            ),
            constraints=[],
            workspace=active.workspace_alias,
            budget=budget,
            handoff=False,
        )
        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=text,
            telegram_chat_id=chat_id,
            purpose="codex_analysis",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id, agent="codex", role="analysis",
            hidden_task_id=task.id, prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")
        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
        try:
            await self._start_codex_turn_for_conversation(
                active=active,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))
        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(f"用户请求：{text[:200]}", budget.conversation_summary_tokens),
        )
        buttons: list[list[dict[str, str]]] = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(active.id, CONTINUE)},
        ]]
        return ControllerResponse(
            "我先看一下。完成后会把结论发在这里。",
            buttons=buttons,
        )

    async def _handle_auto_context_supplement(
        self,
        text: str,
        active: object,
        auto_run: object,
        ctx: dict[str, Any] | None,
        correlation_id: str,
    ) -> ControllerResponse:
        """Handle plain text during auto collecting_context stage.

        Steers existing Codex analysis turn if one is active,
        or starts a new read-only context supplement turn.
        Does NOT create a new task or start Claude.
        """
        # Update conversation summary with supplemented context
        summary_prefix = active.conversation_summary or ""
        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"{summary_prefix}\n[Auto补充] {text[:200]}",
                ContextBudget().conversation_summary_tokens,
            ),
        )

        # Try to steer the current active Codex turn
        task_id = getattr(active, "active_codex_task_id", None)
        if task_id:
            try:
                task = self._service.get_task(task_id)
                if (
                    task is not None
                    and getattr(task, "codex_thread_id", None)
                    and getattr(task, "active_turn_id", None)
                ):
                    await self._backend.steer_turn(
                        task.codex_thread_id,
                        task.active_turn_id,
                        text,
                    )
                    buttons = build_auto_stage_buttons(active.id, AUTO_COLLECTING_CONTEXT)
                    return ControllerResponse(
                        f"已补充到当前 Codex 分析：{text[:100]}",
                        buttons=buttons,
                    )
            except Exception:
                logger.debug("Failed to steer existing auto Codex turn", exc_info=True)

        # If no active turn to steer, start a new read-only context supplement turn
        budget = ContextBudget()
        packet = build_auto_context_packet(
            user_goal=text,
            conversation_summary=trim_to_budget(
                active.conversation_summary, budget.conversation_summary_tokens
            ),
            workspace=active.workspace_alias,
            budget=budget,
        )

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=text,
            telegram_chat_id=chat_id,
            purpose="auto_context_supplement",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role=ROLE_AUTO_CONTEXT_SUPPLEMENT,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=active,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        buttons = build_auto_stage_buttons(active.id, AUTO_COLLECTING_CONTEXT)
        return ControllerResponse(
            f"已补充信息到当前 /auto 分析。{text[:100]}",
            buttons=buttons,
        )

    async def _handle_append_to_conversation(
        self,
        decision: RouteDecision,
        text: str,
        active: object,
        ctx: dict[str, Any] | None,
        correlation_id: str,
    ) -> ControllerResponse:
        """Append user context to active non-terminal conversation."""
        # Emit conversation.message.routed
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_MESSAGE_ROUTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={
                "chat_id": ctx.get("chat_id", 0) if ctx else 0,
                "route": decision.route,
                "reason": decision.reason,
                "conversation_state": decision.conversation_state,
                "conversation_id": active.id,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # Approval supersession: cancel pending approvals
        if decision.requires_approval_supersession:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.APPROVAL_SUPERSEDED,
                aggregate_type=AggregateType.APPROVAL,
                aggregate_id=f"approval-conv-{active.id}",
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"reason": "user_context_appended",
                         "conversation_id": active.id},
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))

        # Emit user.context.appended
        delivery_policy = decision.delivery_policy or "codex_immediate_review"
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_CONTEXT_APPENDED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="user",
            visibility=Visibility.USER,
            payload={
                "chat_id": ctx.get("chat_id", 0) if ctx else 0,
                "text_preview": safe_text_preview(text),
                "text_length": len(text),
                "conversation_state_at_append": decision.conversation_state,
                "delivery_policy": delivery_policy,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # --- Implementation / verification: record pending context ---
        if delivery_policy == "codex_phase_boundary_review":
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_PENDING_CONTEXT_RECORDED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(active.id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={
                    "text_preview": safe_text_preview(text),
                    "text_length": len(text),
                    "conversation_state": decision.conversation_state,
                },
                occurred_at=now_iso(),
                conversation_id=active.id,
            ))
            # Update conversation summary with pending context note
            self._ledger.update_conversation_summary(
                active.id,
                trim_to_budget(
                    f"{active.conversation_summary}\n[待审核追加] {text[:200]}",
                    500,
                ),
            )
            return ControllerResponse(MID_RUN_ACKNOWLEDGEMENT)

        # --- Analysis / waiting_approval / needs_user: immediate Codex review ---
        # Cancel the active orchestration run (if any) before re-analysis.
        await self._cancel_active_runs_for_conversation(active.id, correlation_id)

        # Update conversation summary with appended context.
        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"{active.conversation_summary}\n[用户补充] {text[:200]}",
                500,
            ),
        )
        # Plain text follow-ups remain read-only by default. Use /auto for
        # explicit Codex -> Claude -> Codex implementation flow.
        return await self._handle_codex_analysis_only(
            text, active, ctx, correlation_id,
        )

    async def _cancel_active_runs_for_conversation(
        self, conversation_id: int, correlation_id: str,
    ) -> None:
        """Cancel all active orchestration/agent runs for a conversation.

        Emits run.cancelled events so the re-analyze path doesn't hit
        WorkspaceBusy from the previous run.
        """
        try:
            orch_runs = self._ledger.list_orchestration_runs(conversation_id, limit=10)
            for orch_run in orch_runs:
                if orch_run.status not in ("passed", "failed", "aborted"):
                    self._emit_event(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.RUN_CANCEL_REQUESTED,
                        aggregate_type=AggregateType.ORCHESTRATION_RUN,
                        aggregate_id=str(orch_run.id),
                        correlation_id=correlation_id,
                        source=EventSource.CONTROLLER,
                        actor="controller",
                        visibility=Visibility.OPERATOR,
                        payload={"reason": "user_context_appended_reanalysis",
                                 "conversation_id": conversation_id},
                        occurred_at=now_iso(),
                        conversation_id=conversation_id,
                        orchestration_run_id=orch_run.id,
                    ))
                    self._emit_event(RuntimeEvent(
                        schema_version=1,
                        event_type=EventType.RUN_CANCELLED,
                        aggregate_type=AggregateType.ORCHESTRATION_RUN,
                        aggregate_id=str(orch_run.id),
                        correlation_id=correlation_id,
                        source=EventSource.CONTROLLER,
                        actor="controller",
                        visibility=Visibility.OPERATOR,
                        payload={"reason": "user_context_appended_reanalysis",
                                 "conversation_id": conversation_id},
                        occurred_at=now_iso(),
                        conversation_id=conversation_id,
                        orchestration_run_id=orch_run.id,
                    ))
                    # Abort the underlying task
                    task_id = getattr(orch_run, "task_id", None)
                    if task_id is None:
                        # Find task by conversation
                        conv = self._ledger.get_conversation(conversation_id)
                        task_id = conv.active_codex_task_id
                    if task_id is not None:
                        try:
                            self._service.abort_task(task_id)
                        except Exception:
                            pass
        except Exception:
            logger.debug("Failed to cancel active runs for reanalysis", exc_info=True)

    def _execution_lane_decision(
        self, active_run: object | None, incoming_kind: str
    ) -> str:
        """Return idle, append, explicit_choice, or onsite_input.

        Rules (Repair Plan Task 2):
        - Cockpit ordinary text + idle -> idle
        - Cockpit ordinary text + active task/run -> append
        - explicit /codex or /claude + active task/run -> explicit_choice
        - Onsite text + selected session -> onsite_input
        """
        if incoming_kind == "onsite_text":
            return "onsite_input"
        if incoming_kind in ("codex_direct", "claude_direct"):
            if active_run is not None:
                return "explicit_choice"
            return "idle"
        if active_run is not None:
            return "append"
        return "idle"

    def _blocking_task_for(self, workspace_alias: str) -> object | None:
        try:
            return self._service.blocker_for_workspace(workspace_alias)
        except Exception:
            logger.debug("Failed to check workspace blocker", exc_info=True)
            return None

    def _busy_append_agent_label(self, convo: object, fallback: str = "现场") -> str:
        if getattr(convo, "active_codex_task_id", None):
            return "Codex"
        if getattr(convo, "active_claude_run_id", None):
            return "Claude"
        return fallback

    async def _direct_command_busy_response(
        self,
        *,
        active: object,
        original_text: str,
        agent_label: str,
    ) -> ControllerResponse | None:
        blocker = self._blocking_task_for(active.workspace_alias)
        if blocker is None:
            return None
        decision = RouteDecision(
            route="workspace_busy",
            reason="direct_command_workspace_busy",
            new_conversation=False,
            intent="diagnostic",
            conversation_state="running",
        )
        return await self._handle_workspace_busy(
            decision,
            active,
            getattr(blocker, "id", None),
            None,
            self._new_correlation_id(),
            original_text=original_text,
            agent_label=self._busy_append_agent_label(active, agent_label),
        )

    async def handle_terminal_workspace_busy(
        self,
        active: object,
        original_text: str,
        *,
        agent_label: str = "现场",
    ) -> ControllerResponse:
        """Return the same busy choice card for terminal/onsite input."""
        decision = RouteDecision(
            route="workspace_busy",
            reason="terminal_workspace_busy",
            new_conversation=False,
            intent="normal_text",
            conversation_state="running",
        )
        return await self._handle_workspace_busy(
            decision,
            active,
            getattr(active, "active_codex_task_id", None),
            getattr(active, "active_claude_run_id", None),
            self._new_correlation_id(),
            original_text=original_text,
            agent_label=agent_label,
        )

    def _prompt_from_pending_text(self, text: str) -> str:
        stripped = text.strip()
        try:
            command = parse_command(stripped)
        except Exception:
            return stripped
        return str(getattr(command, "prompt", stripped) or stripped)

    async def _send_pending_to_current_session(
        self, convo: object, original_text: str
    ) -> ControllerResponse | None:
        prompt = self._prompt_from_pending_text(original_text)
        if not prompt:
            return None

        task_id = getattr(convo, "active_codex_task_id", None)
        if task_id:
            try:
                task = self._service.get_task(task_id)
            except Exception:
                task = None
            if (
                task is not None
                and getattr(task, "codex_thread_id", None)
                and getattr(task, "active_turn_id", None)
            ):
                await self._backend.steer_turn(
                    task.codex_thread_id,
                    task.active_turn_id,
                    prompt,
                )
                return ControllerResponse("已发给当前 Codex。")

        run_id = getattr(convo, "active_claude_run_id", None)
        if run_id and self._claude is not None:
            try:
                run = self._ledger.get_agent_run(run_id)
            except Exception:
                run = None
            session_id = getattr(run, "external_session_id", "") if run else ""
            if session_id and hasattr(self._claude, "send_terminal_input"):
                result = await self._claude.send_terminal_input(session_id, prompt)
                text = getattr(result, "text", "") if result is not None else ""
                if text:
                    return ControllerResponse(text)
                return ControllerResponse("已发给当前 Claude。")

        return None

    async def _abort_active_execution(self, convo: object) -> None:
        task_id = getattr(convo, "active_codex_task_id", None)
        if task_id:
            try:
                task = self._service.get_task(task_id)
                if getattr(task, "codex_thread_id", None) and getattr(
                    task, "active_turn_id", None
                ):
                    try:
                        await self._backend.interrupt_turn(
                            task.codex_thread_id,
                            task.active_turn_id,
                        )
                    except Exception as exc:
                        logger.warning("interrupt_turn failed: %s", exc)
                self._service.abort_task(task_id)
            except Exception:
                logger.debug("Failed to abort active Codex task", exc_info=True)

        run_id = getattr(convo, "active_claude_run_id", None)
        if run_id:
            if self._claude is not None:
                try:
                    self._claude.interrupt()
                except Exception as exc:
                    logger.warning("Claude interrupt failed: %s", exc)
            try:
                self._ledger.update_agent_run_status(run_id, "aborted")
            except Exception:
                logger.debug("Failed to mark Claude run aborted", exc_info=True)

    async def _handle_workspace_busy(
        self,
        decision: RouteDecision,
        active: object | None,
        blocking_task_id: int | None,
        blocking_run_id: int | None,
        correlation_id: str,
        *,
        original_text: str = "",
        agent_label: str = "现场",
    ) -> ControllerResponse:
        """Handle workspace busy: emit events and return user choice buttons."""
        conv_id = active.id if active else 0
        workspace_alias = (
            str(getattr(active, "workspace_alias", "") or self._default_workspace)
            if active else self._default_workspace
        )

        # Store the original message text so callbacks can carry it.
        if original_text and self._ledger is not None and conv_id:
            self._ledger.update_conversation_summary(
                conv_id,
                trim_to_budget(
                    f"[工作区忙待处理] {original_text[:300]}",
                    500,
                ),
            )

        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.WORKSPACE_BUSY_DETECTED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id=f"workspace-{workspace_alias}",
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={
                "blocking_task_id": blocking_task_id,
                "blocking_conversation_id": conv_id,
                "blocking_run_id": blocking_run_id,
                "blocking_state": decision.conversation_state,
                "requested_route": decision.route,
                "original_text_preview": safe_text_preview(original_text),
                "original_text_length": len(original_text),
            },
            occurred_at=now_iso(),
            conversation_id=conv_id if conv_id else None,
        ))
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_REQUESTED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id=f"workspace-{workspace_alias}",
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.USER,
            payload={
                "blocking_task_id": blocking_task_id,
                "choices": ["append", "interrupt", "queue", "new_session", "cancel"],
                "original_text_preview": safe_text_preview(original_text),
                "original_text_length": len(original_text),
                "agent_label": agent_label,
            },
            occurred_at=now_iso(),
            conversation_id=conv_id if conv_id else None,
        ))
        buttons = build_workspace_busy_buttons(conv_id, agent_label=agent_label)
        return ControllerResponse(
            "当前工作区正在执行，新的话不会丢。\n\n"
            f"你刚发的新话可以这样处理：\n"
            f"- 发给当前 {agent_label}：把这句话送进当前正在跑的现场\n"
            "- 打断并执行这句：停止当前任务，立刻改做最新输入\n"
            "- 排队稍后：不影响当前任务，结束后再执行\n"
            "- 新开隔离现场：另开现场，不混入当前上下文",
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

        title = command.title if command.title else "新工作台"
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
            f"新工作台已创建：「{convo.title}」\n"
            f"模式：{mode_label}\n"
            f"工作区：{convo.workspace_alias}\n\n"
            f"直接发消息继续这个工作台，或用 /codex /claude /auto 切换执行模式。",
            buttons=buttons,
        )

    async def handle_codex_direct(
        self, command: CodexDirectCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Codex Direct Mode — Codex-only work, independent from /auto."""
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

        busy = await self._direct_command_busy_response(
            active=active,
            original_text=f"/codex {command.prompt}".strip(),
            agent_label="Codex",
        )
        if busy is not None:
            return busy

        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=command.prompt,
            telegram_chat_id=chat_id,
            purpose="codex_direct",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role="implementation",
            hidden_task_id=task.id,
            prompt_packet_summary=command.prompt[:200],
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
        try:
            thread_id = await self._start_codex_turn_for_conversation(
                active=active,
                task=task,
                workspace_path=workspace_path,
                prompt=command.prompt,
            )
            # Persist thread reference so /sessions shows this as resumable.
            self._ledger.update_agent_run_status(
                agent_run.id, "running", external_session_id=thread_id,
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"Codex 单智能体干活：{command.prompt[:200]}",
                ContextBudget().conversation_summary_tokens,
            ),
        )
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
            "这次只交给 Codex 独立干活，不会调用 Claude 或进入 /auto 编排。",
            buttons=buttons,
        )

    async def handle_claude_direct(
        self, command: ClaudeDirectCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Claude Direct Mode — Claude-only implementation, no automatic Codex
        analysis or verification.  Offers a 让 Codex 验收 action after completion."""
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

        busy = await self._direct_command_busy_response(
            active=active,
            original_text=f"/claude {command.prompt}".strip(),
            agent_label="Claude",
        )
        if busy is not None:
            return busy

        return await self._handle_claude_direct_impl(command, active, ctx)

    async def _handle_claude_direct_impl(
        self,
        command: ClaudeDirectCommand,
        active: object,
        ctx: dict[str, Any] | None = None,
    ) -> ControllerResponse:
        """Claude-only direct run — no Codex pre-analysis, no auto Codex verify.

        Creates a Claude agent run and task, then launches Claude as a
        background asyncio task so the controller can return immediately.
        The response includes a 让 Codex 验收 button so the user can
        explicitly request verification after Claude completes.
        """
        chat_id = ctx.get("chat_id", 0) if ctx else 0
        budget = ContextBudget()

        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=command.prompt,
            telegram_chat_id=chat_id,
            purpose="claude_direct",
        )

        claude_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="claude",
            role="implementation",
            hidden_task_id=task.id,
            prompt_packet_summary=command.prompt[:200],
        )
        self._ledger.update_agent_run_status(claude_run.id, "running")
        self._ledger.set_conversation_active_claude_run(active.id, claude_run.id)

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"Claude 直接实施：{command.prompt[:200]}",
                budget.conversation_summary_tokens,
            ),
        )
        # Emit user message received event
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
            payload={
                "text_preview": safe_text_preview(command.prompt),
                "text_length": len(command.prompt),
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)

        # Launch Claude as a background task — must actually execute,
        # not just record a database row.
        bg_task = asyncio.create_task(
            self._run_claude_direct_async(
                agent_run_id=claude_run.id,
                task_id=task.id,
                conversation_id=active.id,
                prompt=command.prompt,
                workspace_path=workspace_path,
                chat_id=chat_id,
                correlation_id=cid,
            ),
            name=f"claude-direct-{claude_run.id}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        # Streaming path: delegate rendering to interaction renderer
        if self._interaction_renderer is not None:
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

        # Static response: mode label + verification affordance
        buttons: list[list[dict[str, str]]] = [
            [
                {
                    "text": "让 Codex 验收",
                    "callback_data": encode_conversation_callback(active.id, VERIFY),
                },
            ],
            [
                {
                    "text": "查看状态",
                    "callback_data": encode_conversation_callback(active.id, CONTINUE),
                },
            ],
        ]
        return ControllerResponse(
            "这次直接交给 Claude 实施。完成后你可以点\"让 Codex 验收\"。",
            buttons=buttons,
        )

    async def _run_claude_direct_async(
        self,
        *,
        agent_run_id: int,
        task_id: int,
        conversation_id: int,
        prompt: str,
        workspace_path: str,
        chat_id: int,
        correlation_id: str = "",
    ) -> None:
        """Background coroutine that actually invokes Claude as a subprocess.

        Called via asyncio.create_task so the controller can return
        immediately.  Updates the agent run status on completion/failure.
        """
        from wlcodex.agent_backend import AgentRequest

        try:
            try:
                self._ledger.set_task_status(
                    task_id,
                    TaskStatus.RUNNING,
                    phase="claude_direct",
                )
                self._ledger.add_event(
                    task_id,
                    "claude_direct_started",
                    {"agent_run_id": agent_run_id},
                )
            except Exception:
                logger.debug("Unable to mark Claude direct task running", exc_info=True)

            try:
                conversation = self._ledger.get_conversation(conversation_id)
                resume_session_id = conversation.claude_session_id
            except Exception:
                resume_session_id = ""
            extra = (
                {"resume_session_id": resume_session_id}
                if resume_session_id
                else {}
            )
            request = AgentRequest(
                prompt=prompt,
                workspace_path=workspace_path,
                extra=extra,
            )
            result = None
            had_error = False
            error_text = ""
            claude_session_id: str = ""
            if self._interaction_renderer is not None:
                stream = self._claude.send_streaming(request)
                if hasattr(stream, "__aiter__"):
                    async for stream_event in stream:
                        event_type = getattr(stream_event, "event_type", "")
                        delta = getattr(stream_event, "delta", "")
                        # Capture latest non-empty session_id for persistence.
                        sid = getattr(stream_event, "session_id", "")
                        if sid:
                            claude_session_id = sid
                        if event_type == "error":
                            had_error = True
                            error_text = delta or "Claude streaming returned error"
                        from wlcodex.interaction.events import InteractionEvent
                        await self._interaction_renderer.handle(
                            InteractionEvent(
                                event_type="claude_delta",
                                chat_id=chat_id,
                                task_id=task_id,
                                conversation_id=conversation_id,
                                metadata={"delta": delta, "event_type": event_type},
                            )
                        )
                else:
                    result = await stream
            else:
                # Non-streaming path: call send() and capture result
                result = await self._claude.send(request)

            # Capture session_id from non-streaming result.
            if not claude_session_id and result is not None:
                claude_session_id = getattr(result, "session_id", "") or ""

            if had_error:
                try:
                    self._service.fail_task(task_id, error_text or "Claude direct failed")
                except Exception:
                    logger.debug("Unable to mark Claude direct task failed", exc_info=True)
                self._ledger.update_agent_run_status(
                    agent_run_id,
                    "failed",
                    completion_summary=error_text[:2000],
                    external_session_id=claude_session_id or None,
                )
                if claude_session_id:
                    self._ledger.set_conversation_claude_session(
                        conversation_id, claude_session_id
                    )
                # Update staged-auto orchestration run if applicable
                self._transition_auto_claude_completed(
                    conversation_id, agent_status="failed",
                    completion_summary=error_text[:5000],
                )
                self._emit_event(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.RUN_FAILED,
                    aggregate_type=AggregateType.AGENT_RUN,
                    aggregate_id=str(agent_run_id),
                    correlation_id=correlation_id,
                    source=EventSource.CONTROLLER,
                    actor="claude",
                    visibility=Visibility.OPERATOR,
                    payload={"status": "failed", "reason": error_text[:500],
                             "task_id": task_id, "session_id": claude_session_id},
                    occurred_at=now_iso(),
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                ))
                return

            # On success: mark agent run as done
            completion_summary = ""
            if result is not None:
                try:
                    completion_summary = result.text[:2000]
                except Exception:
                    completion_summary = ""
            self._ledger.update_agent_run_status(
                agent_run_id,
                "done",
                completion_summary=completion_summary or "Claude 执行完成",
                external_session_id=claude_session_id or None,
            )
            if claude_session_id:
                self._ledger.set_conversation_claude_session(
                    conversation_id, claude_session_id
                )
            # Update staged-auto orchestration run if applicable
            self._transition_auto_claude_completed(
                conversation_id, agent_status="done",
                completion_summary=completion_summary or "Claude 执行完成",
            )
            try:
                self._ledger.set_task_status(
                    task_id,
                    TaskStatus.DONE,
                    phase="claude_direct",
                    summary=completion_summary or "Claude 执行完成",
                )
                self._ledger.add_event(
                    task_id,
                    "claude_direct_completed",
                    {"agent_run_id": agent_run_id},
                )
            except Exception:
                logger.debug("Unable to mark Claude direct task done", exc_info=True)

            # Emit run.completed
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_COMPLETED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="claude",
                visibility=Visibility.OPERATOR,
                payload={"status": "done", "task_id": task_id},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
            ))

        except Exception as exc:
            logger.exception("Claude direct background task failed")
            try:
                self._service.fail_task(task_id, str(exc))
            except Exception:
                logger.debug("Failed to mark Claude direct task failed", exc_info=True)
            self._ledger.update_agent_run_status(
                agent_run_id,
                "failed",
                completion_summary=str(exc)[:2000],
            )
            self._transition_auto_claude_completed(
                conversation_id, agent_status="failed",
                completion_summary=str(exc)[:5000],
            )
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_FAILED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="claude",
                visibility=Visibility.OPERATOR,
                payload={"status": "failed", "reason": str(exc)[:500],
                         "task_id": task_id},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
            ))

    async def handle_auto_mode(
        self, command: AutoModeCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        """Stage-gated /auto: starts in collecting_context, never starts Claude
        automatically. User must explicitly advance through button clicks."""
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
                mode=ConversationMode.CHIEF_ENGINEER.value,
                workspace_alias=self._default_workspace,
            )

        # Check if workspace is busy
        busy = await self._direct_command_busy_response(
            active=active,
            original_text=f"/auto {command.prompt}".strip(),
            agent_label="Auto",
        )
        if busy is not None:
            return busy

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
            payload={
                "text_preview": safe_text_preview(command.prompt),
                "text_length": len(command.prompt),
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # Emit auto.stage.transitioned event
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_STARTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(active.id),
            correlation_id=cid,
            source=EventSource.CONTROLLER,
            actor="user",
            visibility=Visibility.OPERATOR,
            payload={
                "goal": command.prompt,
                "stage": AUTO_COLLECTING_CONTEXT,
                "mode": "staged_auto",
                "chat_id": chat_id,
            },
            occurred_at=now_iso(),
            conversation_id=active.id,
        ))

        # Create orchestration_run with collecting_context stage
        orch_run = self._ledger.create_orchestration_run(
            conversation_id=active.id,
            goal=command.prompt,
        )
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_COLLECTING_CONTEXT,
        )

        # Start Codex in read-only analysis mode (NOT the eager orchestration runner)
        budget = ContextBudget()
        packet = build_auto_context_packet(
            user_goal=command.prompt,
            conversation_summary=trim_to_budget(
                active.conversation_summary, budget.conversation_summary_tokens
            ),
            workspace=active.workspace_alias,
            budget=budget,
        )

        task = self._reserve_execution_lease(
            conversation_id=active.id,
            workspace_alias=active.workspace_alias,
            prompt=command.prompt,
            telegram_chat_id=chat_id,
            purpose="auto_analysis",
        )

        codex_analysis_run = self._ledger.create_agent_run(
            conversation_id=active.id,
            agent="codex",
            role=ROLE_AUTO_ANALYSIS,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(codex_analysis_run.id, "running")

        workspace_path = str(self._service.get_workspace(active.workspace_alias).path)

        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.RUN_REQUESTED,
            aggregate_type=AggregateType.ORCHESTRATION_RUN,
            aggregate_id=str(orch_run.id),
            correlation_id=cid,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.OPERATOR,
            payload={"goal": command.prompt, "stage": AUTO_COLLECTING_CONTEXT},
            occurred_at=now_iso(),
            conversation_id=active.id,
            orchestration_run_id=orch_run.id,
        ))

        try:
            await self._start_codex_turn_for_conversation(
                active=active,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                codex_analysis_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            self._ledger.update_orchestration_run(
                orch_run.id,
                status="failed",
                last_codex_analysis=str(exc)[:5000],
            )
            return ControllerResponse(classify_user_error(exc))

        self._ledger.update_conversation_summary(
            active.id,
            trim_to_budget(
                f"[Auto] {command.prompt[:200]}",
                budget.conversation_summary_tokens,
            ),
        )

        buttons = [[
            {
                "text": "查看状态",
                "callback_data": encode_conversation_callback(active.id, AUTO_VIEW_STATUS),
            },
            {
                "text": "取消",
                "callback_data": encode_conversation_callback(active.id, AUTO_CANCEL),
            },
        ]]
        start_text = (
            "Codex 开始分析。你可以继续补充信息，"
            "分析完成后会显示「生成最终方案」。\n\n"
            "注意：当前不会启动 Claude；Codex 会按任务需要执行查询和核验。"
        )

        if self._interaction_renderer is not None:
            from wlcodex.interaction.events import InteractionEvent
            await self._interaction_renderer.handle(
                InteractionEvent(
                    event_type="run_started",
                    chat_id=chat_id,
                    task_id=task.id,
                    conversation_id=active.id,
                    text=start_text,
                    buttons=buttons,
                )
            )
            return ControllerResponse("", already_rendered=True)

        return ControllerResponse(start_text, buttons=buttons)

    # --- Staged-auto callback handlers ---

    def _latest_active_auto_run(self, conversation_id: int) -> object | None:
        """Find the latest orchestration run for this conversation that is
        in an active auto stage."""
        if self._ledger is None:
            return None
        return self._ledger.get_latest_active_auto_run(conversation_id)

    def _transition_auto_claude_completed(
        self,
        conversation_id: int,
        *,
        agent_status: str,
        completion_summary: str = "",
    ) -> None:
        """Transition staged-auto orchestration run from claude_running to
        claude_done when a Claude background task completes, and send the
        new stage buttons to Telegram."""
        if self._ledger is None:
            return
        orch_run = self._latest_active_auto_run(conversation_id)
        if orch_run is None:
            return
        if orch_run.current_step not in (AUTO_CLAUDE_RUNNING,):
            return
        new_status = "needs_user" if agent_status == "done" else "failed"
        new_step = AUTO_CLAUDE_DONE if agent_status == "done" else orch_run.current_step
        self._ledger.update_orchestration_run(
            orch_run.id,
            status=new_status,
            current_step=new_step,
            last_claude_summary=completion_summary[:5000],
        )

        # Send stage buttons to Telegram
        if self._interaction_renderer is not None and agent_status == "done":
            try:
                convo = self._ledger.get_conversation(conversation_id)
                chat_id = convo.chat_id
            except Exception:
                return
            buttons = build_auto_stage_buttons(
                conversation_id, new_step,
                last_codex_analysis=orch_run.last_codex_analysis or "",
            )
            from wlcodex.interaction.events import InteractionEvent
            asyncio.create_task(
                self._interaction_renderer.handle(
                    InteractionEvent(
                        event_type="run_completed",
                        chat_id=chat_id,
                        conversation_id=conversation_id,
                        text=f"Claude 执行完成。\n\n{completion_summary[:500]}\n\n请选择下一步：",
                        buttons=buttons,
                    )
                )
            )

    async def _handle_auto_final_plan(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """User clicked '生成最终方案': start Codex read-only final plan generation.
        Does NOT start Claude."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        wait_buttons = [[
            {
                "text": "查看状态",
                "callback_data": encode_conversation_callback(convo.id, AUTO_VIEW_STATUS),
            },
            {
                "text": "取消",
                "callback_data": encode_conversation_callback(convo.id, AUTO_CANCEL),
            },
        ]]
        if orch_run.current_step == AUTO_COLLECTING_CONTEXT and orch_run.status != "needs_user":
            return ControllerResponse(
                "Codex 正在生成最终方案，请等待完成。",
                buttons=wait_buttons,
            )
        rewrite_from_draft = (
            callback.action == AUTO_REWRITE_PLAN
            and orch_run.status == "needs_user"
            and orch_run.current_step == AUTO_DRAFT_READY
        )
        if not (is_auto_collecting_context(orch_run) or rewrite_from_draft):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "无法生成最终方案。请先回到上下文收集阶段。"
            )

        # Reset the orchestration run context and start final plan Codex turn
        goal = orch_run.goal
        conversation_summary = convo.conversation_summary
        budget = ContextBudget()
        packet = build_auto_final_plan_packet(
            user_goal=goal,
            conversation_summary=trim_to_budget(
                conversation_summary, budget.conversation_summary_tokens
            ),
            workspace=convo.workspace_alias,
            budget=budget,
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=f"生成最终方案：{goal}",
            telegram_chat_id=chat_id,
            purpose="auto_final_plan",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=ROLE_AUTO_FINAL_PLAN,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_COLLECTING_CONTEXT,
        )
        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        return ControllerResponse(
            "Codex 正在生成最终方案，完成后将显示方案和执行按钮。",
            buttons=wait_buttons,
        )

    async def _handle_auto_send_to_claude(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """User clicked '交给 Claude 执行': start Claude with the Codex-generated
        prompt. Exactly one Claude run is started. Does not auto-verify."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_DRAFT_READY, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不能启动 Claude 执行。"
            )

        if self._claude is None or not getattr(self._claude, "enabled", False):
            return ControllerResponse(
                "Claude Code 未启用。请在配置中设置 claude.enabled = true 后重试。"
            )

        # Extract the Claude execution prompt from the orchestration run's analysis
        claude_prompt = (orch_run.last_codex_analysis or "").strip()
        if not claude_prompt:
            return ControllerResponse(
                "没有可见的最终方案正文，不能交给 Claude 执行。\n"
                "请先继续补充上下文。",
                buttons=build_auto_stage_buttons(
                    convo.id,
                    orch_run.current_step,
                    last_codex_analysis=orch_run.last_codex_analysis or "",
                ),
            )
        goal = orch_run.goal
        chat_id = convo.chat_id
        budget = ContextBudget()

        # Update orchestration run to claude_running
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_CLAUDE_RUNNING,
        )

        # Create Claude agent run
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=claude_prompt,
            telegram_chat_id=chat_id,
            purpose="auto_implementation" if orch_run.current_step != AUTO_RETRY_READY else "auto_repair",
        )

        role = ROLE_AUTO_IMPLEMENTATION if orch_run.current_step != AUTO_RETRY_READY else ROLE_AUTO_REPAIR
        claude_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="claude",
            role=role,
            hidden_task_id=task.id,
            prompt_packet_summary=claude_prompt[:200],
        )
        self._ledger.update_agent_run_status(claude_run.id, "running")
        self._ledger.set_conversation_active_claude_run(convo.id, claude_run.id)

        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        # Launch Claude as a background task
        bg_task = asyncio.create_task(
            self._run_claude_direct_async(
                agent_run_id=claude_run.id,
                task_id=task.id,
                conversation_id=convo.id,
                prompt=claude_prompt,
                workspace_path=workspace_path,
                chat_id=chat_id,
                correlation_id=self._new_correlation_id(),
            ),
            name=f"auto-claude-{claude_run.id}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        buttons = build_auto_stage_buttons(convo.id, AUTO_CLAUDE_RUNNING)
        return ControllerResponse("Claude 开始执行。完成后请点「Codex 验收」。", buttons=buttons)

    async def _handle_auto_codex_verify(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """User clicked 'Codex 验收': start Codex read-only verification.
        Only starts after explicit click, never automatically."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不是等待验收阶段。请等 Claude 完成后再点「Codex 验收」。"
            )

        goal = orch_run.goal
        codex_analysis = orch_run.last_codex_analysis or ""
        claude_summary = orch_run.last_claude_summary or ""
        conversation_summary = convo.conversation_summary
        verify_round = orch_run.verify_round + 1

        # Collect diff evidence
        diff_summary = ""
        changed_files: list[str] = []
        try:
            task_id = convo.active_codex_task_id or convo.active_claude_run_id
            if task_id:
                workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)
                diff_result = self._inspector.diff(task_id, workspace_path)
                if diff_result and diff_result.body:
                    diff_summary = diff_result.body[:1500]
                files_result = self._inspector.files(task_id)
                if files_result and files_result.body:
                    for line in files_result.body.split("\n"):
                        stripped = line.strip()
                        if stripped and not stripped.startswith("#"):
                            changed_files.append(stripped[:200])
        except Exception:
            pass

        budget = ContextBudget()
        packet = build_auto_verification_packet(
            user_goal=goal,
            codex_plan_summary=codex_analysis[:800],
            claude_completion_summary=claude_summary[:1500],
            changed_files=changed_files[:20],
            diff_summary=diff_summary[:1500],
            workspace=convo.workspace_alias,
            budget=budget,
            verify_round=verify_round,
        )

        # Increment verify round
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_VERIFYING,
            verify_round=verify_round,
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=f"验收：{goal}",
            telegram_chat_id=chat_id,
            purpose="auto_verification",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=ROLE_AUTO_VERIFICATION,
            hidden_task_id=task.id,
            prompt_packet_summary=packet.summary(),
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")

        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=packet.render(),
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        buttons = build_auto_stage_buttons(convo.id, AUTO_VERIFYING)
        return ControllerResponse("Codex 开始验收，完成后将显示验收结果。", buttons=buttons)

    async def _handle_auto_codex_takeover(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """User clicked 'Codex 接管修': explicitly allow Codex to write code."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_DRAFT_READY, AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不能在此阶段接管。"
            )

        goal = orch_run.goal
        analysis = orch_run.last_codex_analysis or ""
        claude_summary = orch_run.last_claude_summary or ""
        verification = orch_run.last_verification_result or ""

        # Update orchestration run to codex_takeover_running
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_CODEX_TAKEOVER_RUNNING,
        )

        budget = ContextBudget()
        prompt = (
            f"Codex 接管修复任务\n\n"
            f"原始目标：{goal}\n\n"
            f"Codex 方案：{analysis[:500]}\n\n"
            f"Claude 产出：{claude_summary[:300]}\n\n"
            f"验收结果：{verification[:300]}\n\n"
            f"用户明确要求 Codex 直接修复，请直接修改代码完成目标。"
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=prompt,
            telegram_chat_id=chat_id,
            purpose="auto_codex_takeover",
        )
        agent_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="codex",
            role=ROLE_AUTO_CODEX_TAKEOVER,
            hidden_task_id=task.id,
            prompt_packet_summary=prompt[:200],
        )
        self._ledger.update_agent_run_status(agent_run.id, "running")
        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        try:
            await self._start_codex_turn_for_conversation(
                active=convo,
                task=task,
                workspace_path=workspace_path,
                prompt=prompt,
                interaction_mode="general",
            )
        except Exception as exc:
            task = self._service.fail_task(task.id, str(exc))
            self._ledger.update_agent_run_status(
                agent_run.id, "failed", completion_summary=str(exc)[:2000],
            )
            return ControllerResponse(classify_user_error(exc))

        buttons = build_auto_stage_buttons(convo.id, AUTO_CODEX_TAKEOVER_RUNNING)
        return ControllerResponse("Codex 开始直接修复。", buttons=buttons)

    async def _handle_auto_send_repair_to_claude(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """User clicked '发给 Claude 返工' or '发给 Claude 返工' from retry_ready:
        start Claude repair with Codex-generated repair prompt."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。请用 /auto 开始。")
        if orch_run.current_step not in (AUTO_CLAUDE_DONE, AUTO_RETRY_READY):
            return ControllerResponse(
                f"当前阶段是 {auto_stage_label(orch_run.current_step)}，"
                "不能返工。"
            )

        if self._claude is None or not getattr(self._claude, "enabled", False):
            return ControllerResponse(
                "Claude Code 未启用。请在配置中设置 claude.enabled = true 后重试。"
            )

        goal = orch_run.goal
        verification = orch_run.last_verification_result or ""
        analysis = orch_run.last_codex_analysis or ""

        # Build repair prompt from verification result
        budget = ContextBudget()
        repair_packet = build_auto_repair_packet(
            user_goal=goal,
            codex_plan_summary=analysis[:500],
            claude_completion_summary="",
            verification_result=verification[:500],
            workspace=convo.workspace_alias,
            budget=budget,
        )
        claude_prompt = repair_packet.render()

        self._ledger.update_orchestration_run(
            orch_run.id,
            status="running",
            current_step=AUTO_CLAUDE_RUNNING,
        )

        chat_id = convo.chat_id
        task = self._reserve_execution_lease(
            conversation_id=convo.id,
            workspace_alias=convo.workspace_alias,
            prompt=claude_prompt,
            telegram_chat_id=chat_id,
            purpose="auto_repair",
        )

        claude_run = self._ledger.create_agent_run(
            conversation_id=convo.id,
            agent="claude",
            role=ROLE_AUTO_REPAIR,
            hidden_task_id=task.id,
            prompt_packet_summary=claude_prompt[:200],
        )
        self._ledger.update_agent_run_status(claude_run.id, "running")
        self._ledger.set_conversation_active_claude_run(convo.id, claude_run.id)

        workspace_path = str(self._service.get_workspace(convo.workspace_alias).path)

        bg_task = asyncio.create_task(
            self._run_claude_direct_async(
                agent_run_id=claude_run.id,
                task_id=task.id,
                conversation_id=convo.id,
                prompt=claude_prompt,
                workspace_path=workspace_path,
                chat_id=chat_id,
                correlation_id=self._new_correlation_id(),
            ),
            name=f"auto-repair-{claude_run.id}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        buttons = build_auto_stage_buttons(convo.id, AUTO_CLAUDE_RUNNING)
        return ControllerResponse("Claude 开始返工。完成后请点「Codex 验收」。", buttons=buttons)

    async def _handle_auto_close(self, callback: ConversationCallback) -> ControllerResponse:
        """User clicked '结束任务': mark the auto run as completed."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。")
        self._ledger.update_orchestration_run(
            orch_run.id,
            status="passed",
            current_step=AUTO_COMPLETED,
        )
        buttons = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(convo.id, STATUS)},
        ]]
        return ControllerResponse("自动工作流已结束。", buttons=buttons)

    async def _handle_auto_cancel(self, callback: ConversationCallback) -> ControllerResponse:
        """User clicked '取消': abort the auto run."""
        convo = self._ledger.get_conversation(callback.conversation_id)
        orch_run = self._latest_active_auto_run(callback.conversation_id)
        if orch_run is None:
            return ControllerResponse("没有活跃的自动工作流。")

        # Abort any active tasks
        if convo.active_codex_task_id:
            try:
                self._service.abort_task(convo.active_codex_task_id)
            except Exception:
                pass
        if convo.active_claude_run_id and self._claude is not None:
            try:
                self._claude.interrupt()
            except Exception:
                pass

        self._ledger.update_orchestration_run(
            orch_run.id,
            status="aborted",
            current_step=AUTO_COMPLETED,
        )
        buttons = [[
            {"text": "查看状态", "callback_data": encode_conversation_callback(convo.id, STATUS)},
        ]]
        return ControllerResponse("自动工作流已取消。", buttons=buttons)

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
                                        f"变更文件：{len(changed_files)} 个\n" \
                                        f"验收证据：Codex 分析计划 + Claude 输出摘要 + diff"
                else:
                    verification_text = "Codex 线程未就绪，无法发送验收。"
            else:
                workspace_path = str(self._service.get_workspace(active.workspace_alias).path)
                thread_id = await self._backend.create_thread(workspace_path)
                task = self._reserve_execution_lease(
                    conversation_id=active.id,
                    workspace_alias=active.workspace_alias,
                    prompt=f"验证：{verify_payload}",
                    telegram_chat_id=chat_id,
                    purpose="codex_verification",
                )
                self._service.set_task_thread(task.id, thread_id)
                await self._backend.start_turn(thread_id, packet.render())
                verification_text = "已向 Codex 发送验收请求。\n" \
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

    # --- Carryover handlers ---

    async def handle_carry_workbench(
        self, command: CarryWorkbenchCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")
        chat_id = ctx.get("chat_id", 0) if ctx else 0
        query = command.query.strip()
        if query.isdigit():
            return await self._prepare_workbench_carryover(int(query), chat_id)
        conversations = self._ledger.list_conversations_by_chat(
            chat_id, limit=20, include_archived=True
        )
        # Build carryover sources for all candidates so we can deep-search.
        candidates: list[tuple[object, CarryoverSource, str]] = []
        for convo in conversations:
            source = self._build_carryover_source(convo)
            brief = build_continuity_brief(source)
            candidates.append((convo, source, brief))
        if query:
            lowered = query.lower()
            candidates = [
                (convo, source, brief)
                for convo, source, brief in candidates
                if lowered in convo.title.lower()
                or lowered in convo.workspace_alias.lower()
                or lowered in convo.conversation_summary.lower()
                or lowered in source.latest_codex_summary.lower()
                or lowered in source.latest_claude_summary.lower()
                or lowered in source.latest_verification_result.lower()
                or lowered in brief.lower()
            ]
        # Render up to 8 candidates with previews.
        items: list[tuple] = [
            (convo, build_carryover_preview(source))
            for convo, source, _brief in candidates[:8]
        ]
        buttons = self._carryover_candidate_buttons(
            [item[0] for item in items]
        )
        return ControllerResponse(
            render_carryover_candidates(items), buttons=buttons,
        )

    def _build_carryover_source(
        self, conversation: object
    ) -> CarryoverSource:
        evidence = self._ledger.list_carryover_evidence(conversation.id)  # type: ignore[union-attr]
        agent_runs = evidence.agent_runs
        orch_runs = evidence.orchestration_runs
        latest_codex = next(
            (
                r.completion_summary
                for r in agent_runs
                if r.agent == "codex" and r.completion_summary
            ),
            "",
        )
        latest_claude = next(
            (
                r.completion_summary
                for r in agent_runs
                if r.agent == "claude" and r.completion_summary
            ),
            "",
        )
        # Fall back to orchestration_run fields when agent_runs don't have summaries.
        if not latest_codex:
            latest_codex = next(
                (r.last_codex_analysis for r in orch_runs if r.last_codex_analysis),
                "",
            )
        if not latest_claude:
            latest_claude = next(
                (r.last_claude_summary for r in orch_runs if r.last_claude_summary),
                "",
            )
        latest_verification = next(
            (
                r.last_verification_result
                for r in orch_runs
                if r.last_verification_result
            ),
            "",
        )
        refs = [
            *(f"agent_run={run.id}:{run.agent}/{run.role}/{run.status}" for run in agent_runs[:3]),
            *(f"orchestration_run={run.id}:{run.status}/{run.current_step}" for run in orch_runs[:3]),
        ]
        return CarryoverSource(
            source_conversation_id=conversation.id,
            title=conversation.title,
            workspace_alias=conversation.workspace_alias,
            conversation_summary=conversation.conversation_summary,
            latest_codex_summary=latest_codex,
            latest_claude_summary=latest_claude,
            latest_verification_result=latest_verification,
            evidence_refs=refs,
        )

    def _carryover_candidate_buttons(
        self, conversations: list
    ) -> list[list[dict[str, str]]]:
        button_rows: list[list[dict[str, str]]] = []
        for convo in conversations:
            button_rows.append([
                {
                    "text": "接棒开新工作台",
                    "callback_data": encode_conversation_callback(convo.id, CARRY_START),
                },
                {
                    "text": "查看接棒摘要",
                    "callback_data": encode_conversation_callback(convo.id, CARRY_SHOW),
                },
                {
                    "text": "刷新摘要",
                    "callback_data": encode_conversation_callback(convo.id, CARRY_REFRESH),
                },
            ])
        return button_rows

    async def _cancel_previous_prepared_for_chat(
        self, chat_id: int,
    ) -> None:
        """Cancel all previously prepared carryovers for this chat."""
        if self._ledger is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._ledger._conn.execute(
            """
            UPDATE workbench_carryovers
            SET status = 'cancelled', updated_at = ?
            WHERE chat_id = ? AND status = 'prepared'
            """,
            (now, chat_id),
        )
        self._ledger._conn.commit()

    async def _prepare_workbench_carryover(
        self, source_conversation_id: int, chat_id: int
    ) -> ControllerResponse:
        try:
            source_convo = self._ledger.get_conversation(source_conversation_id)  # type: ignore[union-attr]
        except KeyError:
            return ControllerResponse("工作台不存在或已被删除。")
        if source_convo.chat_id != chat_id:
            return ControllerResponse("不能接棒其他聊天里的工作台。")
        await self._cancel_previous_prepared_for_chat(chat_id)
        source = self._build_carryover_source(source_convo)
        brief = build_continuity_brief(source)
        preview = build_carryover_preview(source)
        fingerprint = build_source_fingerprint(
            conversation_id=source_convo.id,
            latest_agent_run_ids=[
                run.id
                for run in self._ledger.list_recent_agent_runs(source_convo.id, limit=5)  # type: ignore[union-attr]
            ],
            latest_orchestration_run_ids=[
                run.id
                for run in self._ledger.list_orchestration_runs(source_convo.id, limit=5)  # type: ignore[union-attr]
            ],
        )
        self._ledger.create_workbench_carryover(  # type: ignore[union-attr]
            chat_id=chat_id,
            source_conversation_id=source_convo.id,
            workspace_alias=source_convo.workspace_alias,
            brief_text=brief,
            preview_text=preview,
            source_fingerprint=fingerprint,
            status="prepared",
        )
        return ControllerResponse(
            render_prepared_carryover(
                source_conversation_id=source_convo.id,
                source_title=source_convo.title,
                workspace_alias=source_convo.workspace_alias,
                preview=preview,
            ),
            buttons=[[
                {
                    "text": "查看接棒摘要",
                    "callback_data": encode_conversation_callback(source_convo.id, CARRY_SHOW),
                },
                {
                    "text": "取消接棒",
                    "callback_data": encode_conversation_callback(source_convo.id, CARRY_CANCEL),
                },
            ]],
        )

    async def _handle_carry_start(
        self, convo: object,
    ) -> ControllerResponse:
        return await self._prepare_workbench_carryover(convo.id, convo.chat_id)

    async def _handle_carry_show(
        self, convo: object,
    ) -> ControllerResponse:
        source = self._build_carryover_source(convo)
        brief = build_continuity_brief(source)
        buttons: list[list[dict[str, str]]] = [[
            {
                "text": "刷新摘要",
                "callback_data": encode_conversation_callback(convo.id, CARRY_REFRESH),
            },
        ]]
        return ControllerResponse(
            render_carryover_brief_view(
                source_conversation_id=convo.id, brief_text=brief,
            ),
            buttons=buttons,
        )

    async def _handle_carry_refresh(
        self, convo: object,
    ) -> ControllerResponse:
        source = self._build_carryover_source(convo)
        brief = build_continuity_brief(source)
        preview = build_carryover_preview(source)
        prepared = self._ledger.get_latest_prepared_carryover(convo.chat_id)  # type: ignore[union-attr]
        if prepared is not None and prepared.source_conversation_id == convo.id:
            fingerprint = build_source_fingerprint(
                conversation_id=convo.id,
                latest_agent_run_ids=[
                    run.id
                    for run in self._ledger.list_recent_agent_runs(convo.id, limit=5)  # type: ignore[union-attr]
                ],
                latest_orchestration_run_ids=[
                    run.id
                    for run in self._ledger.list_orchestration_runs(convo.id, limit=5)  # type: ignore[union-attr]
                ],
            )
            self._ledger.update_workbench_carryover_brief(  # type: ignore[union-attr]
                prepared.id,
                brief_text=brief,
                preview_text=preview,
                source_fingerprint=fingerprint,
            )
        buttons: list[list[dict[str, str]]] = [[
            {
                "text": "接棒开新工作台",
                "callback_data": encode_conversation_callback(convo.id, CARRY_START),
            },
        ]]
        return ControllerResponse(
            "摘要已刷新。\n\n"
            + render_carryover_brief_view(
                source_conversation_id=convo.id, brief_text=brief,
            ),
            buttons=buttons,
        )

    async def _handle_carry_cancel(
        self, convo: object,
    ) -> ControllerResponse:
        chat_id = convo.chat_id
        if chat_id:
            prepared = self._ledger.get_latest_prepared_carryover(chat_id)  # type: ignore[union-attr]
            if prepared is not None:
                self._ledger.update_workbench_carryover_status(prepared.id, "cancelled")  # type: ignore[union-attr]
        return ControllerResponse(render_carryover_cancelled())

    async def _consume_prepared_carryover(
        self,
        carryover: object,
        text: str,
        ctx: dict[str, Any] | None,
    ) -> ControllerResponse:
        chat_id = (
            ctx.get("chat_id", 0)
            if ctx
            else getattr(carryover, "chat_id", 0)
        )
        user_id = ctx.get("user_id", 0) if ctx else 0
        try:
            source = self._ledger.get_conversation(carryover.source_conversation_id)  # type: ignore[union-attr]
        except KeyError:
            self._ledger.update_workbench_carryover_status(carryover.id, "cancelled")  # type: ignore[union-attr]
            return ControllerResponse("接棒来源工作台已不存在，已取消。")
        if source.chat_id != chat_id:
            self._ledger.update_workbench_carryover_status(carryover.id, "cancelled")  # type: ignore[union-attr]
            return ControllerResponse("接棒来源不属于当前聊天，已取消。")
        try:
            self._service.get_workspace(carryover.workspace_alias)
        except Exception:
            return ControllerResponse(
                f"来源工作区 {carryover.workspace_alias} 当前未配置。"
                "请先在配置中加入该工作区，或取消接棒后使用 /switch。"
            )
        blocker = self._service.blocker_for_workspace(carryover.workspace_alias)
        if blocker is not None:
            busy_convo = self._find_workspace_busy_conversation(
                chat_id=chat_id,
                workspace_alias=carryover.workspace_alias,
                blocking_task_id=blocker.id,
            )
            decision = RouteDecision(
                route="workspace_busy",
                reason="carryover_workspace_busy",
                new_conversation=False,
                intent="normal_text",
            )
            response = await self._handle_workspace_busy(
                decision,
                busy_convo or source,
                blocker.id,
                None,
                self._new_correlation_id(),
                original_text=text,
                agent_label="现场",
            )
            return ControllerResponse(
                response.text + "\n\n接棒状态已保留，不会丢失。",
                buttons=response.buttons,
            )
        old = self._ledger.get_active_conversation(chat_id)  # type: ignore[union-attr]
        if old is not None:
            self._ledger.archive_conversation(old.id)  # type: ignore[union-attr]
        title = default_title(text)
        target = self._ledger.create_conversation(  # type: ignore[union-attr]
            chat_id=chat_id,
            user_id=user_id,
            title=title,
            mode=self._default_mode,
            workspace_alias=carryover.workspace_alias,
        )
        summary = trim_to_budget(
            f"{carryover.brief_text}\n\n当前用户新任务：{text[:500]}",
            ContextBudget().conversation_summary_tokens,
        )
        self._ledger.update_conversation_summary(target.id, summary)  # type: ignore[union-attr]
        self._ledger.mark_workbench_carryover_used(carryover.id, target.id)  # type: ignore[union-attr]
        return ControllerResponse(
            render_carryover_target_created(
                source_conversation_id=source.id,
                target_title=target.title,
                workspace_alias=target.workspace_alias,
            ),
            buttons=[[
                {
                    "text": "查看状态",
                    "callback_data": encode_conversation_callback(target.id, STATUS),
                },
            ]],
        )

    def _find_workspace_busy_conversation(
        self,
        *,
        chat_id: int,
        workspace_alias: str,
        blocking_task_id: int | None,
    ) -> object | None:
        if self._ledger is None:
            return None
        candidates: list[object] = []
        active = self._ledger.get_active_conversation(chat_id)
        if active is not None:
            candidates.append(active)
        for convo in self._ledger.list_conversations_by_chat(
            chat_id, limit=20, include_archived=False
        ):
            if active is not None and convo.id == active.id:
                continue
            candidates.append(convo)

        workspace_candidates = [
            convo
            for convo in candidates
            if getattr(convo, "workspace_alias", "") == workspace_alias
        ]
        if blocking_task_id is not None:
            for convo in workspace_candidates:
                if getattr(convo, "active_codex_task_id", None) == blocking_task_id:
                    return convo
        for convo in workspace_candidates:
            if getattr(convo, "active_codex_task_id", None) or getattr(
                convo, "active_claude_run_id", None
            ):
                return convo
        return workspace_candidates[0] if workspace_candidates else None

    async def _handle_restore_workbench(self, conversation_id: int) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")
        try:
            target = self._ledger.get_conversation(conversation_id)
            previous_active = self._ledger.get_active_conversation(target.chat_id)
            restored = self._ledger.restore_conversation(conversation_id)
        except KeyError:
            return ControllerResponse("工作台不存在或已被删除。")
        self._record_workbench_restore_events(restored, previous_active)
        try:
            self._service.get_workspace(restored.workspace_alias)
            workspace_note = f"工作区：{restored.workspace_alias}"
        except Exception:
            workspace_note = (
                f"工作区：{restored.workspace_alias}（当前配置不存在，"
                "请先添加该 workspace 后再执行任务）"
            )
        return ControllerResponse(
            f"已恢复工作台 #{restored.id}：「{restored.title}」\n"
            f"{workspace_note}\n\n"
            "直接发消息会继续这个工作台。"
        )

    def _record_workbench_restore_events(
        self, restored: object, previous_active: object | None
    ) -> None:
        correlation_id = self._new_correlation_id()
        if previous_active is not None and previous_active.id != restored.id:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_CLOSED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(previous_active.id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.USER,
                payload={
                    "chat_id": previous_active.chat_id,
                    "conversation_id": previous_active.id,
                    "reason": "workbench_restore",
                    "activated_conversation_id": restored.id,
                },
                occurred_at=now_iso(),
                conversation_id=previous_active.id,
            ))
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.CONVERSATION_ACTIVATED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(restored.id),
            correlation_id=correlation_id,
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.USER,
            payload={
                "chat_id": restored.chat_id,
                "conversation_id": restored.id,
                "previous_conversation_id": (
                    previous_active.id if previous_active is not None else None
                ),
                "workspace_alias": restored.workspace_alias,
                "mode": restored.mode,
            },
            occurred_at=now_iso(),
            conversation_id=restored.id,
        ))
        previous_surface_mode = self._latest_surface_mode(restored.id)
        if previous_surface_mode is not None and previous_surface_mode != "product":
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_MODE_SWITCHED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(restored.id),
                correlation_id=correlation_id,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.USER,
                payload={
                    "chat_id": restored.chat_id,
                    "conversation_id": restored.id,
                    "from_mode": previous_surface_mode,
                    "to_mode": "product",
                    "reason": "workbench_restore",
                },
                occurred_at=now_iso(),
                conversation_id=restored.id,
            ))

    def _latest_surface_mode(self, conversation_id: int) -> str | None:
        if self._store is None:
            return None
        try:
            events = self._store.list_recent_for_conversation(conversation_id, limit=200)
        except Exception:
            logger.debug("Failed to read latest surface mode", exc_info=True)
            return None
        for event in reversed(events):
            if event.event_type != EventType.CONVERSATION_MODE_SWITCHED:
                continue
            mode = event.payload.get("to_mode")
            return mode if isinstance(mode, str) else None
        return None

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
                stopped_items.append("Codex 执行")
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

        detail = "；".join(stopped_items) if stopped_items else "无活跃执行"
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
            return ControllerResponse(
                f"工作区 '{command.workspace_alias}' 不存在。发送 /workspaces 查看可用工作区。"
            )

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

    async def handle_exec_mode(
        self, command: ExecModeCommand, ctx: dict[str, Any] | None = None
    ) -> ControllerResponse:
        if self._ledger is None:
            return ControllerResponse("系统未完全初始化。请检查配置。")

        chat_id = ctx.get("chat_id", 0) if ctx else 0
        active = self._ledger.get_active_conversation(chat_id)
        if active is None:
            return ControllerResponse("当前没有活跃工作台。请先发送 /new。")

        mode_aliases = {
            "orchestrated": ConversationMode.CHIEF_ENGINEER.value,
            "chief_engineer": ConversationMode.CHIEF_ENGINEER.value,
            "auto": ConversationMode.CHIEF_ENGINEER.value,
            "codex": ConversationMode.CODEX_DIRECT.value,
            "codex_direct": ConversationMode.CODEX_DIRECT.value,
            "claude": ConversationMode.CLAUDE_DIRECT.value,
            "claude_direct": ConversationMode.CLAUDE_DIRECT.value,
        }
        target_mode = mode_aliases.get(command.mode_name)
        if target_mode is None:
            return ControllerResponse(
                "未知执行模式。可选：orchestrated、codex_direct、claude_direct。"
            )

        updated = self._ledger.set_active_conversation_mode(active.id, target_mode)
        self._emit_event(RuntimeEvent(
            schema_version=1,
            event_type=EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
            aggregate_type=AggregateType.CONVERSATION,
            aggregate_id=str(updated.id),
            correlation_id=self._new_correlation_id(),
            source=EventSource.CONTROLLER,
            actor="controller",
            visibility=Visibility.USER,
            payload={
                "chat_id": updated.chat_id,
                "conversation_id": updated.id,
                "from_mode": active.mode,
                "to_mode": updated.mode,
            },
            occurred_at=now_iso(),
            conversation_id=updated.id,
        ))
        mode_label = MODE_LABELS.get(updated.mode, updated.mode)
        return ControllerResponse(
            f"已切换执行模式：{mode_label}\n"
            "直接发消息会按这个模式继续当前工作台。"
        )

    # --- Conversation callback handler ---

    async def handle_conversation_callback(
        self, callback: ConversationCallback
    ) -> ControllerResponse:
        """Route conversation inline button callbacks (conv:* protocol)."""
        try:
            convo = self._ledger.get_conversation(callback.conversation_id)
        except KeyError:
            return ControllerResponse("对话不存在或已被删除。")

        # --- Staged-auto callback actions ---
        if callback.action == AUTO_FINAL_PLAN:
            return await self._handle_auto_final_plan(callback)
        elif callback.action == AUTO_SHOW_DRAFT:
            orch_run = self._latest_active_auto_run(callback.conversation_id)
            if orch_run and orch_run.last_codex_analysis:
                return ControllerResponse(
                    f"当前方案：\n\n{orch_run.last_codex_analysis[:3500]}",
                    buttons=build_auto_stage_buttons(
                        callback.conversation_id, orch_run.current_step,
                        last_codex_analysis=orch_run.last_codex_analysis or "",
                    ),
                )
            return ControllerResponse("暂无方案草稿。")
        elif callback.action == AUTO_CANCEL:
            return await self._handle_auto_cancel(callback)
        elif callback.action == AUTO_SEND_TO_CLAUDE:
            return await self._handle_auto_send_to_claude(callback)
        elif callback.action == AUTO_CONTINUE_CONTEXT:
            # Re-enter collecting_context from draft_ready or retry_ready
            orch_run = self._latest_active_auto_run(callback.conversation_id)
            if orch_run is not None and orch_run.current_step in (
                AUTO_DRAFT_READY, AUTO_RETRY_READY
            ):
                self._ledger.update_orchestration_run(
                    orch_run.id,
                    status="running",
                    current_step=AUTO_COLLECTING_CONTEXT,
                )
                return ControllerResponse(
                    "已回到上下文收集阶段。请补充信息，然后点「生成最终方案」。",
                    buttons=build_auto_stage_buttons(
                        callback.conversation_id, AUTO_COLLECTING_CONTEXT
                    ),
                )
            return ControllerResponse(
                "当前不在方案就绪阶段，无法回到上下文收集。"
            )
        elif callback.action == AUTO_REWRITE_PLAN:
            return await self._handle_auto_final_plan(callback)
        elif callback.action == AUTO_CODEX_TAKEOVER:
            return await self._handle_auto_codex_takeover(callback)
        elif callback.action == AUTO_CLOSE:
            return await self._handle_auto_close(callback)
        elif callback.action == AUTO_CODEX_VERIFY:
            return await self._handle_auto_codex_verify(callback)
        elif callback.action == AUTO_SEND_REPAIR_TO_CLAUDE:
            return await self._handle_auto_send_repair_to_claude(callback)
        elif callback.action == AUTO_REWRITE_REPAIR:
            # Re-verify to regenerate repair prompt
            return await self._handle_auto_codex_verify(callback)
        elif callback.action == AUTO_INTERRUPT_CLAUDE:
            # Interrupt the active Claude run
            if convo.active_claude_run_id and self._claude is not None:
                try:
                    self._claude.interrupt()
                except Exception:
                    pass
            return ControllerResponse("已请求打断 Claude。", buttons=[[{
                "text": "查看状态",
                "callback_data": encode_conversation_callback(callback.conversation_id, STATUS),
            }]])
        elif callback.action == AUTO_VIEW_DIFF:
            return await self.handle(
                "/diff",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == AUTO_VIEW_STATUS:
            return await self.handle(
                "/status",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )

        # --- Carryover callback actions ---
        if callback.action == CARRY_START:
            return await self._handle_carry_start(convo)
        elif callback.action == CARRY_SHOW:
            return await self._handle_carry_show(convo)
        elif callback.action == CARRY_REFRESH:
            return await self._handle_carry_refresh(convo)
        elif callback.action == CARRY_CANCEL:
            return await self._handle_carry_cancel(convo)

        # --- Original callback actions ---
        if callback.action == RESTORE_WORKBENCH:
            return await self._handle_restore_workbench(callback.conversation_id)

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
            return ControllerResponse(
                f"继续工作台「{convo.title}」。\n"
                "直接发消息即可继续，不会新开工作台。"
            )
        elif callback.action == STATUS:
            return await self.handle(
                "/status",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        elif callback.action == NEW_CONVO:
            return await self.handle(
                "/new",
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )
        else:
            return ControllerResponse(f"未知的对话操作：{callback.action}")

    async def handle_workspace_busy_callback(
        self, action: str, conversation_id: int
    ) -> ControllerResponse:
        """Handle workspace busy inline button callbacks."""
        cid = self._new_correlation_id()

        try:
            convo = self._ledger.get_conversation(conversation_id)
        except KeyError:
            return ControllerResponse("对话不存在或已被删除。")
        workspace_alias = str(
            getattr(convo, "workspace_alias", "") or self._default_workspace
        )

        # Retrieve original message text from conversation summary
        original_text = ""
        try:
            summary = getattr(convo, "conversation_summary", "")
            marker = "[工作区忙待处理] "
            if marker in summary:
                original_text = summary.split(marker, 1)[1][:500]
        except Exception:
            pass

        if action == BUSY_APPEND:
            sent = await self._send_pending_to_current_session(convo, original_text)
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "append_to_current",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            if sent is not None:
                return sent
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.USER_CONTEXT_APPENDED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(conversation_id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.USER,
                payload={
                    "conversation_id": conversation_id,
                    "text_preview": safe_text_preview(original_text),
                    "text_length": len(original_text),
                    "conversation_state_at_append": "implementation",
                    "delivery_policy": "codex_phase_boundary_review",
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            return ControllerResponse("已追加到当前执行。当前阶段结束后由 Codex 判断处理。")

        elif action == BUSY_INTERRUPT:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "interrupt_and_run_latest",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            await self._abort_active_execution(convo)
            if original_text.strip().startswith("/"):
                return await self.handle(
                    original_text,
                    {"chat_id": convo.chat_id, "user_id": convo.user_id},
                )
            return await self.handle_conversation_text(
                original_text,
                {"chat_id": convo.chat_id, "user_id": convo.user_id},
            )

        elif action == BUSY_QUEUE:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "queue_new_task",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_QUEUED,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=f"queued-{conversation_id}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={
                    "goal": original_text[:500],
                    "conversation_id": conversation_id,
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            return ControllerResponse("已安排在当前执行之后，工作区空闲后自动启动。")

        elif action == BUSY_NEW_SESSION:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "new_isolated_session",
                         "conversation_id": conversation_id,
                         "original_text_preview": safe_text_preview(original_text),
                         "original_text_length": len(original_text)},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            title = default_title(self._prompt_from_pending_text(original_text))
            new_convo = self._ledger.create_conversation(
                chat_id=convo.chat_id,
                user_id=convo.user_id,
                title=title,
                mode=self._default_mode,
                workspace_alias=convo.workspace_alias,
            )
            self._ledger.update_conversation_summary(
                new_convo.id,
                trim_to_budget(
                    f"[工作区忙待处理] {original_text[:300]}",
                    500,
                ),
            )
            return ControllerResponse(
                f"已新开隔离现场：「{new_convo.title}」。当前工作区仍在执行，"
                "这句话已放到新现场里，等你选择排队或当前释放后再启动。",
                buttons=build_workspace_busy_buttons(
                    new_convo.id, agent_label="现场"
                ),
            )

        elif action == BUSY_CANCEL:
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.WORKSPACE_BUSY_USER_CHOICE_RECORDED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"workspace-{workspace_alias}",
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={"decision": "cancel",
                         "conversation_id": conversation_id},
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
            return ControllerResponse("已取消。")

        return ControllerResponse("未知的操作。")

    async def process_queued_runs(self, workspace_alias: str) -> None:
        """Consume unconsumed ``run.queued`` events when workspace is free.

        Called by EventBridge after drain_workspace detects the workspace
        is no longer blocked.  Finds queued runs that haven't been started
        yet and launches them through the chief-engineer loop.
        """
        if self._store is None or self._ledger is None:
            return

        # Check if workspace is actually free.
        blocker = self._service.blocker_for_workspace(workspace_alias)
        if blocker is not None:
            return

        # Find unconsumed run.queued events: those without a later
        # run.started or run.queued.consumed for the same conversation.
        rows = self._store._conn.execute(
            """
            SELECT q.id AS queued_id, q.payload_json, q.conversation_id, q.correlation_id
            FROM runtime_events q
            WHERE q.event_type = 'run.queued'
              AND NOT EXISTS (
                SELECT 1 FROM runtime_events c
                WHERE c.conversation_id = q.conversation_id
                  AND c.id > q.id
                  AND c.event_type IN ('run.queued.consumed', 'run.started')
              )
            ORDER BY q.id ASC
            LIMIT 1
            """
        ).fetchall()

        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except Exception:
                payload = {}
            conversation_id = int(row["conversation_id"]) if row["conversation_id"] else 0
            goal = str(payload.get("goal", "") or payload.get("text_preview", ""))
            if not goal or not conversation_id:
                continue

            # Emit consumed marker first to prevent double-processing.
            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_QUEUED_CONSUMED,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=f"queued-{conversation_id}",
                correlation_id=self._new_correlation_id(),
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={
                    "conversation_id": conversation_id,
                    "workspace_alias": workspace_alias,
                    "goal_preview": goal[:200],
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))

            # Get the conversation and start the chief-engineer loop.
            try:
                conv = self._ledger.get_conversation(conversation_id)
            except KeyError:
                logger.warning("Queued conversation %d not found", conversation_id)
                continue

            # Check if orchestrator is available.
            if self._orchestration_runner is None:
                logger.warning(
                    "run.queued consumer: orchestrator not available for conv %d",
                    conversation_id,
                )
                continue

            from wlcodex.router import AutoModeCommand
            cmd = AutoModeCommand(prompt=goal)
            from wlcodex.models import ConversationMode
            cid = self._new_correlation_id()

            orch_run = self._ledger.create_orchestration_run(
                conversation_id=conv.id, goal=goal,
            )
            codex_analysis_run = self._ledger.create_agent_run(
                conversation_id=conv.id, agent="codex", role="analysis",
            )
            self._ledger.update_agent_run_status(codex_analysis_run.id, "running")

            self._emit_event(RuntimeEvent(
                schema_version=1,
                event_type=EventType.RUN_REQUESTED,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=str(orch_run.id),
                correlation_id=cid,
                source=EventSource.CONTROLLER,
                actor="controller",
                visibility=Visibility.OPERATOR,
                payload={"goal": goal, "from": "run_queued_consumer"},
                occurred_at=now_iso(),
                conversation_id=conv.id,
                orchestration_run_id=orch_run.id,
            ))

            workspace_path = str(
                self._service.get_workspace(conv.workspace_alias).path
            )
            task = self._reserve_execution_lease(
                conversation_id=conv.id,
                workspace_alias=conv.workspace_alias,
                prompt=goal,
                telegram_chat_id=conv.chat_id,
                purpose="queued_chief_engineer",
            )

            logger.info(
                "run.queued consumer: starting chief-engineer for conv %d, goal=%s",
                conversation_id, goal[:80],
            )
            self._orchestration_runner.start_chief_engineer(
                prompt=goal,
                conversation=conv,
                task_id=task.id,
                orchestration_run_id=orch_run.id,
                codex_analysis_run_id=codex_analysis_run.id,
                chat_id=conv.chat_id,
                workspace_path=workspace_path,
                codex_thread_id=getattr(conv, "codex_thread_id", "") or "",
                claude_session_id=getattr(conv, "claude_session_id", "") or "",
                correlation_id=cid,
            )

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
