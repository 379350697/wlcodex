"""Throttled streaming renderer for Telegram message edits.

Maintains a message buffer and edits an existing Telegram message
no more often than the configured interval. Falls back to sending
a new message if the edit target is invalid.
"""

from __future__ import annotations

import time


class StreamingRenderer:
    def __init__(
        self,
        send_fn,
        edit_fn,
        min_interval_seconds: float = 1.0,
        max_text_length: int = 3900,
        clock=None,
    ) -> None:
        self._send = send_fn
        self._edit = edit_fn
        self._min_interval = min_interval_seconds
        self._max_text_length = max_text_length
        self._clock = clock or time
        self._buffer: list[str] = []
        self._last_edit_time = -float("inf")
        self._message_id: int | None = None
        self._edit_count = 0
        self._chat_id: int | None = None

    @property
    def edit_count(self) -> int:
        return self._edit_count

    async def start(self, chat_id: int, initial_text: str = "") -> None:
        self._chat_id = chat_id
        self._buffer = [initial_text] if initial_text else []
        if initial_text:
            self._message_id = await self._send(chat_id, initial_text)

    async def append(self, text: str) -> None:
        self._buffer.append(text)
        await self._maybe_flush()

    async def finish(self, buttons: list[list[dict[str, str]]] | None = None) -> None:
        await self._flush(buttons=buttons)

    async def _maybe_flush(self) -> None:
        now = self._clock.time()
        if now - self._last_edit_time >= self._min_interval:
            await self._flush()

    async def _flush(
        self, buttons: list[list[dict[str, str]]] | None = None
    ) -> None:
        if self._chat_id is None:
            return
        text = _fit_text("".join(self._buffer), self._max_text_length)
        if self._message_id is not None:
            try:
                await self._edit(self._chat_id, self._message_id, text, buttons=buttons)
                self._edit_count += 1
                self._last_edit_time = self._clock.time()
                return
            except Exception:
                pass
        self._message_id = await self._send(self._chat_id, text)
        self._last_edit_time = self._clock.time()


def _fit_text(text: str, max_length: int) -> str:
    if max_length <= 0 or len(text) <= max_length:
        return text
    marker = "\n\n...内容过长，已截断。"
    if max_length <= len(marker):
        return text[:max_length]
    return text[: max_length - len(marker)] + marker
