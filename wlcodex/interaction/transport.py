from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Buttons = list[list[dict[str, str]]] | None
SendFn = Callable[[int, str, Buttons], Awaitable[int]]
EditFn = Callable[[int, int, str, Buttons], Awaitable[None]]
TypingFn = Callable[[int], Awaitable[object]]
AnswerCallbackFn = Callable[[str], Awaitable[None]]


class TelegramTransport:
    def __init__(
        self,
        send_fn: SendFn,
        edit_fn: EditFn,
        typing_fn: TypingFn,
        answer_callback_fn: AnswerCallbackFn | None = None,
        *,
        outbox: object | None = None,
        preview_send_fn: Callable[[int, str], Awaitable[int]] | None = None,
        preview_edit_fn: Callable[[int, int, str, Buttons], Awaitable[None]] | None = None,
        body_send_fn: Callable[[int, str, Buttons], Awaitable[int]] | None = None,
    ) -> None:
        self._send = send_fn
        self._edit = edit_fn
        self._typing = typing_fn
        self._answer_callback = answer_callback_fn
        self._outbox = outbox
        self._preview_send = preview_send_fn
        self._preview_edit = preview_edit_fn
        self._body_send = body_send_fn

    async def send(self, chat_id: int, text: str, buttons: Buttons = None) -> int:
        if self._outbox is not None:
            self._outbox.enqueue_send(
                chat_id, text, buttons,
                send_fn=self._send, edit_fn=self._edit,
                correlation_id="outbox-send",
            )
            return -1  # queued — real message_id comes from outbox
        return await self._send(chat_id, text, buttons)

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: Buttons = None
    ) -> None:
        if self._outbox is not None:
            self._outbox.enqueue_edit(
                chat_id, message_id, text, buttons,
                edit_fn=self._edit,
                correlation_id="outbox-edit",
            )
            return
        await self._edit(chat_id, message_id, text, buttons)

    async def typing(self, chat_id: int) -> object:
        return await self._typing(chat_id)

    async def answer_callback(self, text: str) -> None:
        if self._outbox is not None and self._answer_callback is not None:
            self._outbox.enqueue_answer_callback(
                text,
                answer_fn=self._answer_callback,
                correlation_id="outbox-answer",
            )
            return
        if self._answer_callback is not None:
            await self._answer_callback(text)

    async def send_preview(self, chat_id: int, text: str) -> int:
        if self._preview_send is not None:
            return await self._preview_send(chat_id, text)
        return await self.send(chat_id, text)

    async def edit_preview(
        self, chat_id: int, message_id: int, text: str, buttons: Buttons = None
    ) -> None:
        if self._preview_edit is not None:
            await self._preview_edit(chat_id, message_id, text, buttons)
        else:
            await self.edit(chat_id, message_id, text, buttons)

    async def send_body(self, chat_id: int, text: str, buttons: Buttons = None) -> int:
        if self._body_send is not None:
            return await self._body_send(chat_id, text, buttons)
        return await self.send(chat_id, text, buttons)
