from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
            split_at = _find_split(self.buffer, limit)
            if split_at >= len(self.buffer):
                # No good split found within buffer
                if not force:
                    break
                chunks.append(self.buffer)
                self.buffer = ""
                break
            chunk = self.buffer[:split_at].rstrip()
            if not chunk:
                if not force:
                    break
                chunk = self.buffer[:limit]
                split_at = limit
            chunks.append(chunk)
            self.buffer = self.buffer[split_at:].lstrip()
        return chunks

    def final_chunks(self, *, number_parts: bool = False) -> list[str]:
        old_max = self.policy.max_chars
        # Temporarily override policy for final output
        object.__setattr__(self.policy, "max_chars", self.policy.final_max_chars)
        object.__setattr__(self.policy, "min_chars", 1)
        chunks = self.ready_chunks(force=True)
        object.__setattr__(self.policy, "max_chars", old_max)
        object.__setattr__(self.policy, "min_chars", self.policy.min_chars)
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
    for i in range(limit - 2, 0, -1):
        if search_region[i] == "\n" and i + 1 < limit:
            rest = search_region[i + 1:]
            if rest and rest[0].isdigit() and ". " in rest[:4]:
                return i
    return -1


def _rfind_sentence_boundary(text: str, limit: int) -> int:
    # Find Chinese or English sentence boundaries
    search_region = text[:limit]
    for punctuation in ("。", "！", "？", ". ", "! ", "? "):
        pos = search_region.rfind(punctuation)
        if pos > 0:
            return pos + len(punctuation)
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
    is_closed: bool = False


class TelegramOutputManager:
    def __init__(
        self,
        *,
        transport,
        semantic_min_chars: int = 900,
        semantic_max_chars: int = 3200,
        final_chunk_chars: int = 3900,
    ) -> None:
        self._transport = transport
        self._policy = ChunkPolicy(
            min_chars=semantic_min_chars,
            max_chars=semantic_max_chars,
            final_max_chars=final_chunk_chars,
        )
        self.sessions: dict[OutputRunKey, TelegramOutputSession] = {}

    async def start(self, key: OutputRunKey, *, surface: OutputSurface, text: str) -> None:
        session = TelegramOutputSession(
            key=key,
            surface=surface,
            chunker=SemanticChunker(self._policy),
        )
        session.preview_message_id = await self._transport.send_preview(key.chat_id, text)
        self.sessions[key] = session

    async def update_status(self, key: OutputRunKey, text: str) -> None:
        session = self.sessions.get(key)
        if session is None or session.preview_message_id is None or session.is_closed:
            return
        await self._transport.edit_preview(
            key.chat_id,
            session.preview_message_id,
            text,
        )

    async def append_text(self, key: OutputRunKey, text: str) -> None:
        session = self.sessions.get(key)
        if session is None or session.is_closed:
            return
        session.chunker.append(text)
        if session.surface == OutputSurface.TERMINAL:
            for chunk in session.chunker.ready_chunks(force=False):
                await self._transport.send_body(key.chat_id, chunk)

    async def complete(self, key: OutputRunKey, buttons=None) -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        chunks = session.chunker.final_chunks(number_parts=True)
        if chunks:
            for idx, chunk in enumerate(chunks):
                chunk_buttons = buttons if idx == len(chunks) - 1 else None
                await self._transport.send_body(key.chat_id, chunk, chunk_buttons)
        elif buttons:
            await self._transport.send_body(key.chat_id, "运行完成", buttons)
        if session.preview_message_id is not None:
            await self._transport.edit_preview(
                key.chat_id,
                session.preview_message_id,
                "运行完成",
            )
        session.is_closed = True
        self.sessions.pop(key, None)

    async def interrupt(self, key: OutputRunKey) -> None:
        session = self.sessions.get(key)
        if session is None:
            return
        if session.preview_message_id is not None:
            await self._transport.edit_preview(
                key.chat_id,
                session.preview_message_id,
                "已打断",
            )
        session.is_closed = True
        self.sessions.pop(key, None)
