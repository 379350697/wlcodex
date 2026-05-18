from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import InteractionProfile
from wlcodex.interaction.transport import TelegramTransport
from wlcodex.streaming import StreamingRenderer

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
    ) -> None:
        self._transport = transport
        self._profile = profile
        self._min_interval = min_interval_seconds
        self._runtime_progress = runtime_progress
        self._sessions: dict[tuple[int, int], _StreamSession] = {}
        self._typing_tasks: dict[tuple[int, int], object] = {}

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
        typing_task = await self._transport.typing(event.chat_id)
        if typing_task is not None:
            self._typing_tasks[key] = typing_task
        text = self._profile.started_text(event)
        if text:
            await self._transport.send(event.chat_id, text)

    async def _handle_text_delta(self, event: InteractionEvent) -> None:
        if not event.text:
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
        if self._runtime_progress is None:
            return
        state = event.metadata.get("runtime_state")
        if state is None:
            return
        await self._runtime_progress.update_progress(
            state,
            chat_id=event.chat_id,
            conversation_id=event.conversation_id or 0,
        )

    async def _handle_runtime_heartbeat(self, event: InteractionEvent) -> None:
        if self._runtime_progress is None:
            return
        state = event.metadata.get("runtime_state")
        if state is None:
            return
        await self._runtime_progress.update_progress(
            state,
            chat_id=event.chat_id,
            conversation_id=event.conversation_id or 0,
        )

    async def _handle_runtime_final(self, event: InteractionEvent) -> None:
        if self._runtime_progress is None:
            return
        state = event.metadata.get("runtime_state")
        if state is None:
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
