from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import InteractionProfile
from wlcodex.interaction.transport import TelegramTransport
from wlcodex.streaming import StreamingRenderer
from wlcodex.telegram_output import (
    OutputRunKey,
    OutputSurface,
    TelegramOutputManager,
)

if TYPE_CHECKING:
    from wlcodex.interaction.runtime_renderer import RuntimeProgressManager


@dataclass
class _StreamSession:
    renderer: StreamingRenderer
    conversation_id: int | None = None


class InteractionRenderer:
    def __init__(
        self,
        *,
        transport: TelegramTransport,
        profile: InteractionProfile,
        min_interval_seconds: float = 1.0,
        runtime_progress: "RuntimeProgressManager | None" = None,
        surface_resolver=None,
        telegram_output_config=None,
    ) -> None:
        self._transport = transport
        self._profile = profile
        self._min_interval = min_interval_seconds
        self._runtime_progress = runtime_progress
        self._sessions: dict[tuple[int, int], _StreamSession] = {}
        self._typing_tasks: dict[tuple[int, int], object] = {}
        self._surface_resolver = surface_resolver or (lambda _chat_id: "product")
        self._output_manager = TelegramOutputManager(
            transport=transport,
            semantic_min_chars=getattr(telegram_output_config, "semantic_min_chars", 900),
            semantic_max_chars=getattr(telegram_output_config, "semantic_max_chars", 3200),
            final_chunk_chars=getattr(telegram_output_config, "final_chunk_chars", 3900),
            preview_enabled=getattr(telegram_output_config, "preview_enabled", True),
            preview_edit_min_interval_seconds=getattr(
                telegram_output_config,
                "preview_edit_min_interval_seconds",
                2.0,
            ),
            product_body_mode=getattr(telegram_output_config, "product_body_mode", "final"),
            terminal_body_mode=getattr(
                telegram_output_config,
                "terminal_body_mode",
                "semantic_blocks",
            ),
            terminal_block_idle_seconds=getattr(
                telegram_output_config,
                "terminal_block_idle_seconds",
                2.0,
            ),
        ) if telegram_output_config is not None else None

    def _output_key(self, event: InteractionEvent) -> OutputRunKey:
        run_id = str(event.task_id or event.thread_id or "chat")
        return OutputRunKey(
            chat_id=event.chat_id,
            conversation_id=event.conversation_id or 0,
            run_id=run_id,
        )

    def has_runtime_status_surface(self) -> bool:
        return self._output_manager_owns_status() or self._runtime_progress is not None

    def _output_manager_owns_status(self) -> bool:
        return bool(
            self._output_manager is not None
            and self._output_manager.preview_enabled
        )

    async def _ensure_output_session(self, event: InteractionEvent) -> OutputRunKey:
        if self._output_manager is None:
            raise RuntimeError("output manager is not configured")
        key = self._output_key(event)
        if key in self._output_manager.sessions:
            return key
        surface = (
            OutputSurface.TERMINAL
            if self._surface_resolver(event.chat_id) == "terminal"
            else OutputSurface.PRODUCT
        )
        text = self._profile.started_text(event) or "正在处理"
        await self._output_manager.start(key, surface=surface, text=text)
        return key

    async def handle(self, event: InteractionEvent) -> None:
        try:
            if event.event_type == "run_started":
                await self._handle_started(event)
                return
            if event.event_type == "text_delta":
                await self._handle_text_delta(event)
                return
            if event.event_type == "run_completed":
                await self._handle_completed(event)
                return
            if event.event_type == "run_failed":
                await self._handle_failed(event)
                return
            if event.event_type == "runtime_progress":
                await self._handle_runtime_progress(event)
                return
            if event.event_type == "runtime_heartbeat":
                await self._handle_runtime_heartbeat(event)
                return
            if event.event_type == "runtime_final":
                await self._handle_runtime_final(event)
                return
        except Exception:
            key = self._key(event)
            self._cancel_typing(key)
            raise

    async def _handle_started(self, event: InteractionEvent) -> None:
        key = self._key(event)
        if self._output_manager is not None:
            if not self._output_manager_owns_status():
                typing_task = await self._transport.typing(event.chat_id)
                if typing_task is not None:
                    self._typing_tasks[key] = typing_task
            await self._ensure_output_session(event)
            return
        typing_task = await self._transport.typing(event.chat_id)
        if typing_task is not None:
            self._typing_tasks[key] = typing_task
        text = self._profile.started_text(event)
        if text:
            await self._transport.send(event.chat_id, text)

    async def _handle_text_delta(self, event: InteractionEvent) -> None:
        if not event.text:
            return
        if self._output_manager is not None:
            key = await self._ensure_output_session(event)
            await self._output_manager.append_text(key, event.text)
            return
        key = self._key(event)
        session = self._sessions.get(key)
        if session is None:
            renderer = StreamingRenderer(
                self._transport.send,
                self._transport.edit,
                min_interval_seconds=self._min_interval,
            )
            await renderer.start(event.chat_id)
            session = _StreamSession(
                renderer=renderer,
                conversation_id=event.conversation_id,
            )
            self._sessions[key] = session
        if event.conversation_id is not None:
            session.conversation_id = event.conversation_id
        await session.renderer.append(event.text)

    async def _handle_completed(self, event: InteractionEvent) -> None:
        key = self._key(event)
        self._cancel_typing(key)
        if self._output_manager is not None:
            conversation_id = event.conversation_id or 0
            buttons = self._profile.completion_buttons(
                conversation_id=conversation_id,
                has_diff=bool(event.metadata.get("has_diff", False)),
            )
            await self._output_manager.complete(self._output_key(event), buttons=buttons)
            if self._output_manager_owns_status() or self._runtime_progress is None:
                return
        session = self._sessions.get(key)
        if session is not None:
            conversation_id = event.conversation_id or session.conversation_id
            buttons = self._profile.completion_buttons(
                conversation_id=conversation_id,
                has_diff=bool(event.metadata.get("has_diff", False)),
            )
            await session.renderer.finish(buttons=buttons)
            self._sessions.pop(key, None)
        if self._runtime_progress is not None:
            state = event.metadata.get("runtime_state")
            if state is not None:
                await self._runtime_progress.finish(
                    state,
                    chat_id=event.chat_id,
                    conversation_id=event.conversation_id or 0,
                )

    async def _handle_failed(self, event: InteractionEvent) -> None:
        key = self._key(event)
        self._cancel_typing(key)
        if self._output_manager is not None:
            state = event.metadata.get("runtime_state")
            state_value = getattr(state, "phase", state)
            event_text = (event.text or "").lower()
            if state_value in ("cancelled", "aborted") or "interrupted" in event_text:
                await self._output_manager.interrupt(self._output_key(event))
            else:
                await self._output_manager.fail(
                    self._output_key(event),
                    error_summary=event.text or event.summary or "",
                )
            if self._output_manager_owns_status() or self._runtime_progress is None:
                return
        session = self._sessions.get(key)
        text = self._profile.error_text(event.text or event.summary)
        if session is None:
            await self._transport.send(event.chat_id, text)
        else:
            await session.renderer.append("\n\n" + text)
            await session.renderer.finish()
            self._sessions.pop(key, None)
        if self._runtime_progress is not None:
            state = event.metadata.get("runtime_state")
            if state is not None:
                await self._runtime_progress.finish(
                    state,
                    chat_id=event.chat_id,
                    conversation_id=event.conversation_id or 0,
                )

    async def _handle_runtime_progress(self, event: InteractionEvent) -> None:
        state = event.metadata.get("runtime_state")
        if state is None:
            return
        if self._output_manager_owns_status():
            text = _runtime_progress_text(state)
            if text:
                await self._output_manager.update_status(self._output_key(event), text)
            return
        if self._runtime_progress is None:
            return
        await self._runtime_progress.update_progress(
            state,
            chat_id=event.chat_id,
            conversation_id=event.conversation_id or 0,
        )

    async def _handle_runtime_heartbeat(self, event: InteractionEvent) -> None:
        state = event.metadata.get("runtime_state")
        if state is None:
            return
        if self._output_manager_owns_status():
            text = _runtime_progress_text(state)
            if text:
                await self._output_manager.update_status(self._output_key(event), text)
            return
        if self._runtime_progress is None:
            return
        await self._runtime_progress.update_progress(
            state,
            chat_id=event.chat_id,
            conversation_id=event.conversation_id or 0,
        )

    async def _handle_runtime_final(self, event: InteractionEvent) -> None:
        state = event.metadata.get("runtime_state")
        if state is None:
            return
        if self._output_manager is not None:
            text = _runtime_final_text(state)
            phase = getattr(state, "phase", "")
            key = self._output_key(event)
            if phase in ("cancelled", "aborted"):
                await self._output_manager.interrupt(key)
            elif phase == "failed":
                await self._output_manager.fail(
                    key,
                    error_summary=getattr(state, "error_summary", ""),
                )
            elif getattr(state, "is_terminal", False) or phase == "completed":
                buttons = event.buttons if event.buttons else None
                await self._output_manager.complete(
                    key,
                    buttons=buttons,
                    status_text=text or "运行完成",
                )
            elif self._output_manager_owns_status() and text:
                await self._output_manager.update_status(key, text)
            if self._output_manager_owns_status() or self._runtime_progress is None:
                return
        if self._runtime_progress is None:
            return
        buttons = event.buttons if event.buttons else None
        await self._runtime_progress.finish(
            state,
            chat_id=event.chat_id,
            conversation_id=event.conversation_id or 0,
            buttons=buttons,
        )

    def _cancel_typing(self, key: tuple[int, int]) -> None:
        task = self._typing_tasks.pop(key, None)
        if task is not None and hasattr(task, "cancel"):
            task.cancel()

    def _key(self, event: InteractionEvent) -> tuple[int, int]:
        task_key = event.task_id if event.task_id is not None else 0
        return (event.chat_id, task_key)


def _runtime_progress_text(state) -> str:
    """Convert a RuntimeRunState to a short status line for the preview bubble."""
    from wlcodex.interaction.runtime_renderer import KNOWN_PHASES

    phase = getattr(state, "phase", "")
    phase_label = KNOWN_PHASES.get(phase, phase)

    if not phase_label:
        return ""

    status = getattr(state, "agent_status", "")

    if status == "waiting_for_approval":
        return "等待审批"

    return phase_label


def _runtime_final_text(state) -> str:
    """Convert a RuntimeRunState to a final status text."""
    from wlcodex.interaction.runtime_renderer import KNOWN_PHASES

    error = getattr(state, "error_summary", "")
    phase = getattr(state, "phase", "")
    if phase == "failed":
        brief = (error or "未知错误")[:200].split("\n")[0].strip()
        return f"运行失败: {brief}" if brief else "运行失败，请重试"
    if phase == "cancelled":
        return "运行已取消"
    if phase == "completed":
        return "运行完成"
    phase_label = KNOWN_PHASES.get(phase, phase) if hasattr(state, "phase") else ""
    return phase_label or "运行完成"
