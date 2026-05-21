from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class OutputSurface(str, Enum):
    PRODUCT = "product"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class ChunkPolicy:
    min_chars: int = 900
    max_chars: int = 3200
    final_max_chars: int = 3900


class SemanticChunker:
    def __init__(self, policy: ChunkPolicy | None = None) -> None:
        self.policy = policy or ChunkPolicy()
        self.buffer = ""

    def append(self, text: str) -> None:
        if text:
            self.buffer += text

    def ready_chunks(self, *, force: bool = False) -> list[str]:
        chunks: list[str] = []
        while self.buffer:
            limit = self.policy.max_chars
            if not force and len(self.buffer) < self.policy.min_chars:
                break
            if (
                not force
                and _starts_markdown_list_item(self.buffer)
                and _rfind_list_boundary(self.buffer, limit) <= 0
            ):
                break
            split_at = _find_split(self.buffer, limit)
            if split_at >= len(self.buffer):
                # No good split found within buffer
                if not force:
                    break
                chunks.append(self.buffer)
                self.buffer = ""
                break
            chunk, remainder = _split_readable_chunk(self.buffer, split_at)
            if not chunk:
                if not force:
                    break
                chunk = self.buffer[:limit]
                remainder = self.buffer[limit:]
            chunks.append(chunk)
            self.buffer = remainder.lstrip()
        return chunks

    def final_chunks(self, *, number_parts: bool = False) -> list[str]:
        # Use a temporary policy with final chunk size
        saved_policy = self.policy
        self.policy = ChunkPolicy(
            min_chars=1,
            max_chars=self.policy.final_max_chars,
            final_max_chars=self.policy.final_max_chars,
        )
        try:
            chunks = self.ready_chunks(force=True)
        finally:
            self.policy = saved_policy
        if number_parts and len(chunks) > 1:
            total = len(chunks)
            return [f"{idx}/{total}\n{chunk}" for idx, chunk in enumerate(chunks, 1)]
        return chunks


# ---------------------------------------------------------------------------
# Split helpers
# ---------------------------------------------------------------------------


def _find_split(text: str, limit: int) -> int:
    safe_limit = _safe_limit(text, limit)
    candidates = [
        text.rfind("\n\n", 0, safe_limit),
        _rfind_list_boundary(text, safe_limit),
        _rfind_sentence_boundary(text, safe_limit),
        _rfind_whitespace_boundary(text, safe_limit),
    ]
    for pos in candidates:
        if pos > 0 and _is_safe_split(text, pos):
            return pos
    # No semantic boundary found → don't split (return end-of-text)
    return safe_limit


def _safe_limit(text: str, limit: int) -> int:
    return min(limit, len(text))


def _is_safe_split(text: str, pos: int) -> bool:
    return not _inside_markdown_link(text, pos) and not _inside_code_fence(text, pos)


def _inside_markdown_link(text: str, pos: int) -> bool:
    # Check if pos is inside [label](url) — simplified scan
    in_link_label = False
    in_link_url = False
    for i, ch in enumerate(text):
        if i >= pos:
            break
        if ch == "[" and not in_link_label and not in_link_url:
            in_link_label = True
        elif ch == "]" and in_link_label:
            in_link_label = False
        elif ch == "(" and not in_link_label and not in_link_url:
            # Potential url start — check if preceded by ]
            if i > 0 and text[i - 1] == "]":
                in_link_url = True
        elif ch == ")" and in_link_url:
            in_link_url = False
    return in_link_label or in_link_url


def _inside_code_fence(text: str, pos: int) -> bool:
    # Count triple-backtick pairs before pos
    fence_count = 0
    i = 0
    while i < pos:
        if text[i:i + 3] == "```":
            fence_count += 1
            i += 3
            continue
        i += 1
    return fence_count % 2 == 1


def _split_readable_chunk(text: str, split_at: int) -> tuple[str, str]:
    chunk = text[:split_at].rstrip()
    remainder = text[split_at:]
    if _inside_code_fence(text, split_at):
        opener = _open_code_fence_marker(text, split_at)
        if chunk and not chunk.endswith("\n"):
            chunk += "\n"
        chunk += "```"
        remainder = opener + "\n" + remainder.lstrip("\n")
    return chunk, remainder


def _open_code_fence_marker(text: str, pos: int) -> str:
    prefix = text[:pos]
    start = prefix.rfind("```")
    if start < 0:
        return "```"
    line_end = prefix.find("\n", start)
    if line_end < 0:
        marker = prefix[start:].strip()
    else:
        marker = prefix[start:line_end].strip()
    return marker if marker.startswith("```") else "```"


def _rfind_list_boundary(text: str, limit: int) -> int:
    # Find start of a markdown list item: \n-  or \n*  or \n1.
    search_region = text[:limit]
    # Look for \n- pattern
    pos = search_region.rfind("\n- ")
    if pos > 0:
        return pos
    pos = search_region.rfind("\n* ")
    if pos > 0:
        return pos
    # Numbered lists: \n1. \n2. etc.
    for i in range(len(search_region) - 2, 0, -1):
        if search_region[i] == "\n" and i + 1 < len(search_region):
            rest = search_region[i + 1:]
            if rest and rest[0].isdigit() and ". " in rest[:4]:
                return i
    return -1


def _starts_markdown_list_item(text: str) -> bool:
    if text.startswith(("- ", "* ")):
        return True
    dot = text.find(". ")
    return 0 < dot <= 3 and text[:dot].isdigit()


def _rfind_sentence_boundary(text: str, limit: int) -> int:
    # Find Chinese or English sentence boundaries
    search_region = text[:limit]
    for punctuation in ("。", "！", "？", ". ", "! ", "? "):
        pos = search_region.rfind(punctuation)
        if pos > 0:
            return pos + len(punctuation)
    return -1


def _rfind_whitespace_boundary(text: str, limit: int) -> int:
    search_region = text[:limit]
    for i in range(len(search_region) - 1, 0, -1):
        if search_region[i].isspace():
            return i
    return -1


# ---------------------------------------------------------------------------
# Output session types (used by Task 4, defined here for import order)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputRunKey:
    chat_id: int
    conversation_id: int
    run_id: str


@dataclass
class TelegramOutputSession:
    key: OutputRunKey
    surface: OutputSurface
    chunker: SemanticChunker
    preview_message_id: int | None = None
    last_status_text: str = ""
    last_status_edit_at: float = 0.0
    idle_flush_task: asyncio.Task[None] | None = None
    is_closed: bool = False


class TelegramOutputManager:
    def __init__(
        self,
        *,
        transport,
        semantic_min_chars: int = 900,
        semantic_max_chars: int = 3200,
        final_chunk_chars: int = 3900,
        preview_enabled: bool = True,
        preview_edit_min_interval_seconds: float = 2.0,
        product_body_mode: str = "final",
        terminal_body_mode: str = "semantic_blocks",
        terminal_block_idle_seconds: float = 2.0,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._transport = transport
        self._policy = ChunkPolicy(
            min_chars=semantic_min_chars,
            max_chars=semantic_max_chars,
            final_max_chars=final_chunk_chars,
        )
        self.preview_enabled = preview_enabled
        self._preview_edit_min_interval_seconds = preview_edit_min_interval_seconds
        self._product_body_mode = product_body_mode
        self._terminal_body_mode = terminal_body_mode
        self._terminal_block_idle_seconds = terminal_block_idle_seconds
        self._time = time_fn or time.monotonic
        self.sessions: dict[OutputRunKey, TelegramOutputSession] = {}

    async def start(self, key: OutputRunKey, *, surface: OutputSurface, text: str) -> None:
        old_session = self.sessions.pop(key, None)
        if old_session is not None:
            self._cancel_idle_flush(old_session)
        session = TelegramOutputSession(
            key=key,
            surface=surface,
            chunker=SemanticChunker(self._policy),
            last_status_text=text,
            last_status_edit_at=self._time(),
        )
        if self.preview_enabled:
            msg_id = await self._transport.send_preview(key.chat_id, text)
            session.preview_message_id = msg_id if msg_id > 0 else None
        self.sessions[key] = session

    def body_mode_for(self, surface: OutputSurface) -> str:
        return (
            self._terminal_body_mode
            if surface == OutputSurface.TERMINAL
            else self._product_body_mode
        )

    async def update_status(self, key: OutputRunKey, text: str, *, force: bool = False) -> None:
        session = self.sessions.get(key)
        if (
            session is None
            or session.preview_message_id is None
            or session.is_closed
            or not self.preview_enabled
        ):
            return
        now = self._time()
        if text == session.last_status_text:
            return
        if (
            not force
            and now - session.last_status_edit_at < self._preview_edit_min_interval_seconds
        ):
            return
        await self._transport.edit_preview(
            key.chat_id,
            session.preview_message_id,
            text,
        )
        session.last_status_text = text
        session.last_status_edit_at = now

    async def append_text(self, key: OutputRunKey, text: str) -> None:
        session = self.sessions.get(key)
        if session is None or session.is_closed:
            return
        session.chunker.append(text)
        if self.body_mode_for(session.surface) == "semantic_blocks":
            chunks = await self._flush_body_chunks(session, force=False)
            if not chunks:
                self._schedule_idle_flush(session)

    async def complete(self, key: OutputRunKey, buttons=None, *, status_text: str = "运行完成") -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        self._cancel_idle_flush(session)
        chunks = session.chunker.final_chunks(number_parts=True)
        if chunks:
            for idx, chunk in enumerate(chunks):
                chunk_buttons = buttons if idx == len(chunks) - 1 else None
                await self._transport.send_body(key.chat_id, chunk, chunk_buttons)
        elif buttons:
            await self._transport.send_body(key.chat_id, status_text, buttons)
        elif session.preview_message_id is None:
            await self._transport.send_body(key.chat_id, status_text)
        if session.preview_message_id is not None:
            await self.update_status(key, status_text, force=True)
        session.is_closed = True
        self.sessions.pop(key, None)

    async def fail(self, key: OutputRunKey, *, error_summary: str = "") -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        self._cancel_idle_flush(session)
        status = f"运行失败: {error_summary[:200]}" if error_summary else "运行失败"
        if session.preview_message_id is not None:
            await self.update_status(key, status, force=True)
        else:
            await self._transport.send_body(key.chat_id, status)
        session.is_closed = True
        self.sessions.pop(key, None)

    async def interrupt(self, key: OutputRunKey) -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        self._cancel_idle_flush(session)
        if session.preview_message_id is not None:
            await self.update_status(key, "已打断", force=True)
        else:
            await self._transport.send_body(key.chat_id, "已打断")
        session.is_closed = True
        self.sessions.pop(key, None)

    async def wait_for_idle_flush(self, key: OutputRunKey) -> None:
        session = self.sessions.get(key)
        task = session.idle_flush_task if session is not None else None
        if task is not None:
            await task

    async def _flush_body_chunks(
        self, session: TelegramOutputSession, *, force: bool
    ) -> list[str]:
        chunks = session.chunker.ready_chunks(force=force)
        for chunk in chunks:
            await self._transport.send_body(session.key.chat_id, chunk)
        return chunks

    def _schedule_idle_flush(self, session: TelegramOutputSession) -> None:
        self._cancel_idle_flush(session)
        if self._terminal_block_idle_seconds <= 0:
            return
        session.idle_flush_task = asyncio.create_task(
            self._idle_flush_after_delay(session.key)
        )

    def _cancel_idle_flush(self, session: TelegramOutputSession) -> None:
        task = session.idle_flush_task
        if task is not None and not task.done():
            task.cancel()
        session.idle_flush_task = None

    async def _idle_flush_after_delay(self, key: OutputRunKey) -> None:
        try:
            await asyncio.sleep(self._terminal_block_idle_seconds)
            session = self.sessions.get(key)
            if (
                session is None
                or session.is_closed
                or self.body_mode_for(session.surface) != "semantic_blocks"
            ):
                return
            await self._flush_body_chunks(session, force=True)
            session.idle_flush_task = None
        except asyncio.CancelledError:
            return
