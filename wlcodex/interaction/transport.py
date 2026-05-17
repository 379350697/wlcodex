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
    ) -> None:
        self._send = send_fn
        self._edit = edit_fn
        self._typing = typing_fn
        self._answer_callback = answer_callback_fn

    async def send(self, chat_id: int, text: str, buttons: Buttons = None) -> int:
        return await self._send(chat_id, text, buttons)

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: Buttons = None
    ) -> None:
        await self._edit(chat_id, message_id, text, buttons)

    async def typing(self, chat_id: int) -> object:
        return await self._typing(chat_id)

    async def answer_callback(self, text: str) -> None:
        if self._answer_callback is not None:
            await self._answer_callback(text)
