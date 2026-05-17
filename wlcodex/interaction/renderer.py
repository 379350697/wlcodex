from __future__ import annotations

from dataclasses import dataclass

from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import InteractionProfile
from wlcodex.interaction.transport import TelegramTransport
from wlcodex.streaming import StreamingRenderer


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
    ) -> None:
        self._transport = transport
        self._profile = profile
        self._min_interval = min_interval_seconds
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
        if session is None:
            return
        conversation_id = event.conversation_id or session.conversation_id
        buttons = self._profile.completion_buttons(
            conversation_id=conversation_id,
            has_diff=bool(event.metadata.get("has_diff", False)),
        )
        await session.renderer.finish(buttons=buttons)
        self._sessions.pop(key, None)

    async def _handle_failed(self, event: InteractionEvent) -> None:
        key = self._key(event)
        self._cancel_typing(key)
        session = self._sessions.get(key)
        text = self._profile.error_text(event.text or event.summary)
        if session is None:
            await self._transport.send(event.chat_id, text)
            return
        await session.renderer.append("\n\n" + text)
        await session.renderer.finish()
        self._sessions.pop(key, None)

    def _cancel_typing(self, key: tuple[int, int]) -> None:
        task = self._typing_tasks.pop(key, None)
        if task is not None and hasattr(task, "cancel"):
            task.cancel()

    def _key(self, event: InteractionEvent) -> tuple[int, int]:
        task_key = event.task_id if event.task_id is not None else 0
        return (event.chat_id, task_key)
