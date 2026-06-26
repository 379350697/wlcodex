from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from wlcodex.codex_native.projector import NativeCodexEventProjector
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


_DEFAULT_TAIL_LINES = 2_000
_DEFAULT_MAX_BYTES = 8_000_000
_DEFAULT_INDEX_HEAD_LINES = 600
_SOURCE_KIND = "codex_jsonl"


@dataclass(frozen=True)
class _TranscriptItem:
    role: str
    text: str
    timestamp: str
    turn_id: str
    item_id: str


@dataclass(frozen=True)
class _SessionIndexEntry:
    native_thread_id: str
    title: str
    cwd: str
    activity_at: str
    originator: str
    source: str
    thread_source: str
    path: Path


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
        existing_item_turn_ids = self._runtime_store.payload_item_turn_ids_by_agent_run(
            session.agent_run_id
        )
        existing_item_ids = set(existing_item_turn_ids)
        recent_user_events = self._runtime_store.list_by_agent_run_tail(
            session.agent_run_id,
            limit=60,
        )
        projected_count = 0
        for item in _parse_transcript_items(
            _read_tail_lines(path, limit=self._tail_lines, max_bytes=self._max_bytes),
            native_thread_id=native_thread_id,
            fallback_turn_id=fallback_turn_id,
        ):
            if (
                item.item_id in self._seen_item_ids
                or item.item_id in existing_item_ids
            ):
                self._seen_item_ids.add(item.item_id)
                stored_turn_ids = existing_item_turn_ids.get(item.item_id, set())
                if _is_official_turn_id(item.turn_id) and (
                    item.turn_id not in stored_turn_ids
                ):
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
                    existing_item_turn_ids.setdefault(item.item_id, set()).add(
                        item.turn_id
                    )
                continue
            if item.role == "user":
                if self._has_duplicate_user_item(
                    item,
                    recent_user_events,
                ):
                    self._seen_item_ids.add(item.item_id)
                    existing_item_ids.add(item.item_id)
                    existing_item_turn_ids.setdefault(item.item_id, set()).add(
                        item.turn_id
                    )
                    continue
                projected = self._projector.project_user_message(
                    native_thread_id=native_thread_id,
                    native_turn_id=item.turn_id,
                    text=item.text,
                    item_id=item.item_id,
                )
            elif item.role == "plan":
                projected = self._projector.project_notification(
                    "turn/plan/updated",
                    {
                        "threadId": native_thread_id,
                        "turnId": item.turn_id,
                        "plan": item.text,
                        "itemId": item.item_id,
                    },
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
            elif item.role == "assistant_final":
                projected = self._projector.project_agent_message(
                    native_thread_id=native_thread_id,
                    native_turn_id=item.turn_id,
                    text=item.text,
                    item_id=item.item_id,
                )
            else:
                projected = []
            self._seen_item_ids.add(item.item_id)
            existing_item_ids.add(item.item_id)
            existing_item_turn_ids.setdefault(item.item_id, set()).add(item.turn_id)
            if projected:
                recent_user_events.extend(
                    event
                    for event in projected
                    if event.event_type == EventType.USER_MESSAGE_RECEIVED
                )
                self._session_store.update_session(
                    native_thread_id=native_thread_id,
                    status="running",
                    last_turn_id=item.turn_id,
                )
                projected_count += len(projected)
        return projected_count

    def index_recent_sessions(self, *, limit: int = 100) -> int:
        if not self._root.exists():
            return 0
        indexed = 0
        for path in _recent_session_files(self._root, limit=max(1, limit)):
            entry = _parse_session_index_entry(path)
            if entry is None:
                continue
            self._path_cache[entry.native_thread_id] = entry.path
            existing = self._session_store.get_by_thread_id(entry.native_thread_id)
            self._session_store.get_or_create_session(
                native_thread_id=entry.native_thread_id,
                title=entry.title or (existing.title if existing else ""),
                cwd=entry.cwd or (existing.cwd if existing else ""),
                source_kind=_SOURCE_KIND,
                status=(existing.status if existing else "") or "idle",
                last_turn_id=existing.last_turn_id if existing else "",
                activity_at=entry.activity_at or (existing.activity_at if existing else ""),
                metadata={
                    "originator": entry.originator,
                    "source": entry.source,
                    "thread_source": entry.thread_source,
                    "rollout_path": str(entry.path),
                },
            )
            indexed += 1
        return indexed

    def _has_duplicate_user_item(
        self,
        item: _TranscriptItem,
        recent_user_events: Iterable[Any],
    ) -> bool:
        if not item.text:
            return False
        normalized_text = _canonical_user_text(item.text)
        if not normalized_text:
            return False
        for event in recent_user_events:
            if event.event_type != EventType.USER_MESSAGE_RECEIVED:
                continue
            payload = event.payload if isinstance(event.payload, dict) else {}
            if _canonical_user_text(str(payload.get("text") or "")) != normalized_text:
                continue
            existing_turn = str(
                payload.get("turnId") or payload.get("native_turn_id") or ""
            )
            if not _turn_ids_compatible_for_local_user_dedupe(
                item.turn_id,
                existing_turn,
            ):
                continue
            existing_item_id = str(
                payload.get("itemId") or payload.get("item_id") or ""
            )
            if existing_item_id.startswith("local-user-") or (
                item.turn_id == existing_turn
            ):
                return True
        return False

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


def _recent_session_files(root: Path, *, limit: int) -> list[Path]:
    candidates = [path for path in root.rglob("*.jsonl") if path.is_file()]
    return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)[
        :limit
    ]


def _parse_session_index_entry(path: Path) -> _SessionIndexEntry | None:
    native_thread_id = ""
    cwd = ""
    title = ""
    created_at = ""
    originator = ""
    source = ""
    thread_source = ""
    try:
        lines = _read_head_lines(path, limit=_DEFAULT_INDEX_HEAD_LINES)
    except OSError:
        return None
    for line in lines:
        row = _json_object(line)
        if not row:
            continue
        row_type = str(row.get("type") or "")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if row_type == "session_meta":
            native_thread_id = (
                _text(payload.get("id")) or _text(payload.get("session_id"))
            )
            cwd = cwd or _text(payload.get("cwd"))
            created_at = created_at or _text(payload.get("timestamp"))
            originator = _text(payload.get("originator"))
            source = _text(payload.get("source"))
            thread_source = _text(payload.get("thread_source"))
            continue
        if row_type == "turn_context":
            cwd = cwd or _text(payload.get("cwd"))
            continue
        if row_type == "event_msg" and payload.get("type") == "user_message":
            title = title or _title_from_text(_text(payload.get("message")))
            if native_thread_id and cwd and title:
                break
    if not native_thread_id:
        native_thread_id = _thread_id_from_path(path)
    if not native_thread_id:
        return None
    if thread_source and thread_source != "user":
        return None
    return _SessionIndexEntry(
        native_thread_id=native_thread_id,
        title=title,
        cwd=cwd,
        activity_at=_path_mtime_iso(path) or created_at,
        originator=originator,
        source=source,
        thread_source=thread_source,
        path=path,
    )


def _read_head_lines(path: Path, *, limit: int) -> list[str]:
    lines: list[str] = []
    with path.open("rb") as handle:
        for index, line in enumerate(handle):
            if index >= limit:
                break
            lines.append(line.decode("utf-8", errors="replace"))
    return lines


def _thread_id_from_path(path: Path) -> str:
    stem = path.stem
    marker = "-019"
    index = stem.find(marker)
    if index < 0:
        return ""
    return stem[index + 1 :]


def _path_mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return ""


def _title_from_text(value: str) -> str:
    title = " ".join(value.split())
    if len(title) <= 80:
        return title
    return title[:77].rstrip() + "..."


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
        payload_type = str(payload.get("type") or "")
        timestamp = str(row.get("timestamp") or "")
        if row_type == "turn_context":
            if row_turn_id:
                current_turn_id = row_turn_id
            continue
        if row_type == "response_item":
            if row_turn_id:
                current_turn_id = row_turn_id
            if payload_type != "plan":
                continue
            text = _response_item_text(payload)
            if not text:
                continue
            item_turn_id = row_turn_id or current_turn_id
            item_id = _text(payload.get("id")) or _stable_id(
                "jsonl-plan",
                native_thread_id,
                item_turn_id,
                text,
            )
            items.append(
                _TranscriptItem(
                    role="plan",
                    text=text,
                    timestamp=timestamp,
                    turn_id=item_turn_id,
                    item_id=item_id,
                )
            )
            continue
        if row_type != "event_msg":
            continue
        if row_turn_id:
            current_turn_id = row_turn_id
        if payload_type in {"task_started", "turn_aborted"}:
            continue
        if payload_type == "task_complete":
            text = _text(payload.get("last_agent_message"))
            if not text:
                continue
            item_turn_id = row_turn_id or current_turn_id
            items.append(
                _TranscriptItem(
                    role="assistant_final",
                    text=text,
                    timestamp=timestamp,
                    turn_id=item_turn_id,
                    item_id=_stable_id(
                        "jsonl-assistant-final",
                        native_thread_id,
                        item_turn_id,
                        text,
                    ),
                )
            )
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
    return _dedupe_transcript_items(items)


def _dedupe_transcript_items(items: list[_TranscriptItem]) -> list[_TranscriptItem]:
    final_texts = {
        (item.turn_id, item.text)
        for item in items
        if item.role == "assistant_final"
    }
    result: list[_TranscriptItem] = []
    for item in items:
        if item.role == "assistant" and (item.turn_id, item.text) in final_texts:
            continue
        result.append(item)
    return result


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _text(value: object) -> str:
    return str(value or "").strip()


def _response_item_text(payload: dict[str, Any]) -> str:
    for key in ("text", "plan", "summary", "content"):
        text = _text(payload.get(key))
        if text:
            return text
    return ""


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


def _canonical_user_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _turn_ids_compatible_for_local_user_dedupe(
    left_turn_id: str,
    right_turn_id: str,
) -> bool:
    if left_turn_id == right_turn_id:
        return True
    if not left_turn_id or not right_turn_id:
        return True
    if left_turn_id.startswith("jsonl-turn:") or right_turn_id.startswith(
        "jsonl-turn:"
    ):
        return True
    return False
