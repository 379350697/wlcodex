from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from wlcodex.codex_native.projector import NativeCodexEventProjector
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore


_DEFAULT_TAIL_LINES = 2_000
_DEFAULT_MAX_BYTES = 8_000_000
_SOURCE_KIND = "codex_jsonl"


@dataclass(frozen=True)
class _TranscriptItem:
    role: str
    text: str
    timestamp: str
    turn_id: str
    item_id: str


class CodexSessionTranscriptMirror:
    """Mirror the official Codex session JSONL tail into WLCodex events.

    The native app-server websocket is still the control path. This mirror is a
    read-only catch-up path for the official remote transcript file, which is
    the source the Codex mobile remote updates while this local server may be
    attached to a separate app-server process.
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        session_store: NativeCodexSessionStore,
        runtime_store: RuntimeEventStore,
        tail_lines: int = _DEFAULT_TAIL_LINES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self._root = root or Path.home() / ".codex" / "sessions"
        self._session_store = session_store
        self._runtime_store = runtime_store
        self._projector = NativeCodexEventProjector(session_store, runtime_store)
        self._tail_lines = max(1, tail_lines)
        self._max_bytes = max(1024, max_bytes)
        self._path_cache: dict[str, Path] = {}
        self._seen_item_ids: set[str] = set()

    def sync_thread(self, native_thread_id: str) -> int:
        native_thread_id = native_thread_id.strip()
        if not native_thread_id:
            return 0
        path = self._find_session_file(native_thread_id)
        if path is None:
            return 0

        session = self._session_store.get_or_create_session(
            native_thread_id=native_thread_id,
            source_kind=_SOURCE_KIND,
        )
        fallback_turn_id = f"jsonl-tail:{native_thread_id}"
        projected_count = 0
        for item in _parse_transcript_items(
            _read_tail_lines(path, limit=self._tail_lines, max_bytes=self._max_bytes),
            native_thread_id=native_thread_id,
            fallback_turn_id=fallback_turn_id,
        ):
            if item.item_id in self._seen_item_ids or (
                self._runtime_store.has_payload_item_id(
                    session.agent_run_id,
                    item.item_id,
                )
            ):
                self._seen_item_ids.add(item.item_id)
                if _is_official_turn_id(item.turn_id):
                    self._runtime_store.correct_payload_item_turn_id(
                        session.agent_run_id,
                        item.item_id,
                        native_turn_id=item.turn_id,
                        native_thread_id=native_thread_id,
                    )
                    self._session_store.update_session(
                        native_thread_id=native_thread_id,
                        last_turn_id=item.turn_id,
                    )
                continue
            if item.role == "user":
                projected = self._projector.project_user_message(
                    native_thread_id=native_thread_id,
                    native_turn_id=item.turn_id,
                    text=item.text,
                    item_id=item.item_id,
                )
            elif item.role == "assistant":
                projected = self._projector.project_notification(
                    "item/agentMessage/delta",
                    {
                        "threadId": native_thread_id,
                        "turnId": item.turn_id,
                        "delta": item.text,
                        "item": {"id": item.item_id},
                    },
                )
            else:
                projected = []
            self._seen_item_ids.add(item.item_id)
            if projected:
                self._session_store.update_session(
                    native_thread_id=native_thread_id,
                    status="running",
                    last_turn_id=item.turn_id,
                )
                projected_count += len(projected)
        return projected_count

    def _find_session_file(self, native_thread_id: str) -> Path | None:
        cached = self._path_cache.get(native_thread_id)
        if cached is not None and cached.exists():
            return cached
        if not self._root.exists():
            return None
        candidates = list(self._root.rglob(f"*{native_thread_id}.jsonl"))
        if not candidates:
            return None
        path = max(candidates, key=lambda item: item.stat().st_mtime)
        self._path_cache[native_thread_id] = path
        return path


def _read_tail_lines(path: Path, *, limit: int, max_bytes: int) -> list[str]:
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as handle:
        handle.seek(start)
        raw = handle.read(max_bytes)
    lines = raw.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return [line.decode("utf-8", errors="replace") for line in lines[-limit:]]


def _parse_transcript_items(
    lines: Iterable[str],
    *,
    native_thread_id: str,
    fallback_turn_id: str,
) -> list[_TranscriptItem]:
    items: list[_TranscriptItem] = []
    current_turn_id = fallback_turn_id
    for line in lines:
        row = _json_object(line)
        if not row:
            continue
        row_type = str(row.get("type") or "")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        row_turn_id = _turn_id(payload)
        if row_type == "turn_context":
            if row_turn_id:
                current_turn_id = row_turn_id
            continue
        if row_type != "event_msg":
            continue
        payload_type = str(payload.get("type") or "")
        timestamp = str(row.get("timestamp") or "")
        if row_turn_id:
            current_turn_id = row_turn_id
        if payload_type in {"task_started", "task_complete", "turn_aborted"}:
            continue
        if payload_type == "user_message":
            text = _text(payload.get("message"))
            if not text:
                continue
            if current_turn_id == fallback_turn_id:
                current_turn_id = _stable_id("jsonl-turn", timestamp, text)
            items.append(
                _TranscriptItem(
                    role="user",
                    text=text,
                    timestamp=timestamp,
                    turn_id=current_turn_id,
                    item_id=_stable_id("jsonl-user", timestamp, text),
                )
            )
        elif payload_type == "agent_message":
            text = _text(payload.get("message"))
            if not text:
                continue
            items.append(
                _TranscriptItem(
                    role="assistant",
                    text=text,
                    timestamp=timestamp,
                    turn_id=current_turn_id,
                    item_id=_stable_id(
                        "jsonl-assistant",
                        native_thread_id,
                        timestamp,
                        text,
                    ),
                )
            )
    return items


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _text(value: object) -> str:
    return str(value or "").strip()


def _turn_id(payload: dict[str, Any]) -> str:
    for key in ("turn_id", "turnId"):
        turn_id = _text(payload.get(key))
        if turn_id:
            return turn_id
    return ""


def _is_official_turn_id(turn_id: str) -> bool:
    return bool(turn_id and not turn_id.startswith("jsonl-"))


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"
