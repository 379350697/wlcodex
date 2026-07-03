from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from wlcodex.runtime_events import EventType, RuntimeEvent, now_iso


@dataclass(frozen=True)
class NativeTimelineEvent:
    sequence: int
    type: str
    kind: str
    provider: str
    native_thread_id: str
    occurred_at: str
    payload: dict[str, Any]
    runtime_event_id: int | None = None
    agent_run_id: int | None = None
    conversation_id: int | None = None
    item_row_id: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.sequence,
            "sequence": self.sequence,
            "type": self.type,
            "kind": self.kind,
            "provider": self.provider,
            "native_thread_id": self.native_thread_id,
            "occurred_at": self.occurred_at,
            "runtime_event_id": self.runtime_event_id,
            "agent_run_id": self.agent_run_id,
            "conversation_id": self.conversation_id,
            "item_row_id": self.item_row_id,
            "payload": dict(self.payload),
        }

    def to_display_json_dict(self) -> dict[str, Any]:
        payload = dict(self.payload)
        role = _display_role_for_kind(self.kind, payload)
        return {
            "id": self.sequence,
            "sequence": self.sequence,
            "type": self.kind,
            "source_type": self.type,
            "kind": self.kind,
            "role": role,
            "visible": _is_visible_display_kind(self.kind),
            "provider": self.provider,
            "native_thread_id": self.native_thread_id,
            "occurred_at": self.occurred_at,
            "runtime_event_id": self.runtime_event_id,
            "agent_run_id": self.agent_run_id,
            "conversation_id": self.conversation_id,
            "item_row_id": self.item_row_id,
            "payload": payload,
        }


@dataclass(frozen=True)
class NativeTimelineItem:
    id: int
    cursor: int
    provider: str
    native_thread_id: str
    turn_key: str
    item_key: str
    role: str
    kind: str
    text: str
    status: str
    payload: dict[str, Any]
    updated_at: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cursor": self.cursor,
            "provider": self.provider,
            "native_thread_id": self.native_thread_id,
            "turn_key": self.turn_key,
            "item_key": self.item_key,
            "role": self.role,
            "kind": self.kind,
            "text": self.text,
            "status": self.status,
            "payload": dict(self.payload),
            "updated_at": self.updated_at,
        }


class NativeTimelineStore:
    """Read-model for user-visible native session timeline events."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[NativeTimelineEvent]]] = (
            defaultdict(set)
        )
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS native_timeline_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                native_thread_id TEXT NOT NULL,
                turn_key TEXT NOT NULL,
                item_key TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'streaming',
                payload_json TEXT NOT NULL DEFAULT '{}',
                source_priority INTEGER NOT NULL DEFAULT 0,
                last_sequence INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider, native_thread_id, turn_key, item_key)
            )
            """
        )
        item_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(native_timeline_items)")
        }
        if "last_sequence" not in item_columns:
            self._conn.execute(
                "ALTER TABLE native_timeline_items "
                "ADD COLUMN last_sequence INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS native_timeline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                native_thread_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                item_row_id INTEGER,
                runtime_event_id INTEGER,
                event_type TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                occurred_at TEXT NOT NULL,
                agent_run_id INTEGER,
                conversation_id INTEGER,
                UNIQUE(provider, native_thread_id, sequence)
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_native_timeline_events_thread_sequence
            ON native_timeline_events(provider, native_thread_id, sequence)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_native_timeline_items_thread_updated
            ON native_timeline_items(provider, native_thread_id, updated_at)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_native_timeline_items_thread_sequence
            ON native_timeline_items(provider, native_thread_id, last_sequence)
            """
        )
        self._conn.commit()

    def project_runtime_event(self, event: RuntimeEvent) -> list[NativeTimelineEvent]:
        event_type = str(event.event_type or "")
        if event_type == EventType.PROVIDER_RAW_FRAME:
            return []
        payload = dict(event.payload or {})
        native_thread_id = _first_text(
            payload,
            "native_thread_id",
            "native_session_id",
            "thread_id",
            "session_id",
        )
        if not native_thread_id:
            return []
        provider = _normalize_provider(
            _first_text(payload, "provider", "native_provider") or str(event.source or "")
        )
        turn_key = _first_text(
            payload,
            "native_turn_id",
            "turnId",
            "turn_id",
            "active_turn_id",
        ) or str(event.aggregate_id or event.agent_run_id or "turn")

        if event_type == EventType.USER_MESSAGE_RECEIVED:
            text = _first_text(payload, "text", "content", "message", "body")
            item_key = _first_text(payload, "itemId", "item_id", "message_id") or (
                "user-" + _text_fingerprint(text)
            )
            item_payload = {
                "text": text,
                "itemId": item_key,
                "native_turn_id": turn_key,
            }
            if isinstance(payload.get("images"), list):
                item_payload["images"] = payload["images"]
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="user",
                kind="user_message",
                text=text,
                status="completed",
                payload=item_payload,
                source_priority=100,
                merge_local=True,
            )
            return [self._event_from_runtime(event, item_id, "user_message", item_payload)]

        if event_type in (EventType.PROVIDER_DISPLAY_DELTA, EventType.MODEL_TEXT_DELTA):
            delta = _first_text(payload, "delta", "text", "content")
            if not delta:
                return []
            item_key = _first_text(payload, "itemId", "item_id", "message_id") or (
                "assistant-" + turn_key
            )
            existing = self._find_item(provider, native_thread_id, turn_key, item_key)
            if (
                existing is not None
                and str(existing["status"]) == "completed"
                and int(existing["source_priority"]) > 50
            ):
                return []
            if (
                existing is not None
                and _is_compatibility_projection(payload)
                and _ends_with_delta(str(existing["text"]), delta)
            ):
                return []
            text = (str(existing["text"]) if existing is not None else "") + delta
            item_payload = {
                "delta": delta,
                "itemId": item_key,
                "native_turn_id": turn_key,
            }
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="assistant",
                kind="message",
                text=text,
                status="streaming",
                payload=item_payload,
                source_priority=50,
                merge_local=False,
            )
            return [self._event_from_runtime(event, item_id, "text_delta", item_payload)]

        if event_type in (
            EventType.PROVIDER_DISPLAY_COMPLETED,
            EventType.MODEL_MESSAGE_COMPLETED,
        ):
            text = _first_text(payload, "text", "content", "message", "summary")
            if not text:
                return []
            item_key = _first_text(payload, "itemId", "item_id", "message_id") or (
                "assistant-" + turn_key
            )
            existing = self._find_item(provider, native_thread_id, turn_key, item_key)
            if (
                existing is not None
                and _is_compatibility_projection(payload)
                and str(existing["text"]) == text
            ):
                return []
            item_payload = {
                "text": text,
                "itemId": item_key,
                "native_turn_id": turn_key,
            }
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="assistant",
                kind="message",
                text=text,
                status="completed",
                payload=item_payload,
                source_priority=90,
                merge_local=False,
            )
            return [self._event_from_runtime(event, item_id, "message_completed", item_payload)]

        if event_type == EventType.MODEL_REASONING_DELTA:
            delta = _first_text(payload, "delta", "text", "content", "summary")
            if not delta:
                return []
            item_key = _first_text(payload, "itemId", "item_id", "message_id") or (
                "reasoning-" + turn_key
            )
            existing = self._find_item(provider, native_thread_id, turn_key, item_key)
            text = (str(existing["text"]) if existing is not None else "") + delta
            item_payload = {
                "delta": delta,
                "text": text,
                "itemId": item_key,
                "native_turn_id": turn_key,
            }
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="assistant",
                kind="reasoning",
                text=text,
                status="streaming",
                payload=item_payload,
                source_priority=40,
                merge_local=False,
            )
            return [self._event_from_runtime(event, item_id, "reasoning_delta", item_payload)]

        if event_type == EventType.AGENT_RUN_ACTIVITY and payload.get("action") == "plan_updated":
            text = _timeline_text(payload, "text", "summary", "title", "plan")
            if not text:
                return []
            item_key = _first_text(payload, "itemId", "item_id", "plan_id") or (
                "plan-" + turn_key
            )
            item_payload = {
                "text": text,
                "action": "plan_updated",
                "plan": payload.get("plan"),
                "title": _first_text(payload, "title"),
                "native_turn_id": turn_key,
                "itemId": item_key,
                "status": _first_text(payload, "status") or "completed",
            }
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="assistant",
                kind="activity",
                text=text,
                status=str(item_payload["status"]),
                payload=item_payload,
                source_priority=70,
                merge_local=False,
            )
            return [self._event_from_runtime(event, item_id, "activity", item_payload)]

        if event_type in (
            EventType.APPROVAL_REQUESTED,
            EventType.AGENT_RUN_WAITING_FOR_APPROVAL,
        ):
            text = _timeline_text(payload, "text", "summary", "prompt", "message", "title")
            item_key = _approval_item_key(payload, turn_key)
            item_payload = {
                "text": text,
                "summary": _first_text(payload, "summary"),
                "prompt": _first_text(payload, "prompt"),
                "request_id": _first_text(payload, "request_id", "requestId", "codexRequestId"),
                "native_turn_id": turn_key,
                "itemId": item_key,
                "status": "pending",
            }
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="system",
                kind="approval_requested",
                text=text,
                status="pending",
                payload=item_payload,
                source_priority=70,
                merge_local=False,
            )
            return [
                self._event_from_runtime(
                    event,
                    item_id,
                    "approval_requested",
                    item_payload,
                )
            ]

        if event_type in (EventType.APPROVAL_RESOLVED, EventType.APPROVAL_EXPIRED):
            resolved_status = "expired" if event_type == EventType.APPROVAL_EXPIRED else "resolved"
            text = _timeline_text(payload, "text", "summary", "prompt", "message", "title")
            item_key = _approval_item_key(payload, turn_key)
            item_payload = {
                "text": text,
                "summary": _first_text(payload, "summary"),
                "request_id": _first_text(payload, "request_id", "requestId", "codexRequestId"),
                "native_turn_id": turn_key,
                "itemId": item_key,
                "status": resolved_status,
            }
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="system",
                kind="approval_requested",
                text=text,
                status=resolved_status,
                payload=item_payload,
                source_priority=80,
                merge_local=False,
            )
            return [
                self._event_from_runtime(
                    event,
                    item_id,
                    "approval_resolved",
                    item_payload,
                )
            ]

        if event_type in (
            EventType.COMMAND_STARTED,
            EventType.COMMAND_OUTPUT_DELTA,
            EventType.COMMAND_COMPLETED,
            EventType.COMMAND_FAILED,
        ):
            kind = {
                EventType.COMMAND_STARTED: "command_started",
                EventType.COMMAND_OUTPUT_DELTA: "command_output",
                EventType.COMMAND_COMPLETED: "command_completed",
                EventType.COMMAND_FAILED: "command_failed",
            }[event_type]
            item_key = _first_text(payload, "command_id", "call_id", "itemId", "item_id") or (
                "command-" + turn_key
            )
            text = _timeline_text(payload, "delta", "output", "command", "summary", "text")
            status = "failed" if event_type == EventType.COMMAND_FAILED else (
                "completed" if event_type == EventType.COMMAND_COMPLETED else "streaming"
            )
            existing = self._find_item(provider, native_thread_id, turn_key, item_key)
            if event_type == EventType.COMMAND_OUTPUT_DELTA and existing is not None:
                text = str(existing["text"] or "") + text
            item_payload = {
                "text": text,
                "delta": _first_text(payload, "delta", "output"),
                "command": _first_text(payload, "command"),
                "summary": _first_text(payload, "summary"),
                "native_turn_id": turn_key,
                "itemId": item_key,
                "status": status,
            }
            item_id = self._upsert_item(
                provider=provider,
                native_thread_id=native_thread_id,
                turn_key=turn_key,
                item_key=item_key,
                role="system",
                kind="command",
                text=text,
                status=status,
                payload=item_payload,
                source_priority=50,
                merge_local=False,
            )
            return [self._event_from_runtime(event, item_id, kind, item_payload)]

        return []

    def list_events(
        self,
        provider: str,
        native_thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> list[NativeTimelineEvent]:
        provider_key = _normalize_provider(provider)
        thread_id = str(native_thread_id or "").strip()
        safe_limit = max(1, min(int(limit or 100), 500))
        if before is not None and before > 0:
            rows = self._conn.execute(
                """
                SELECT * FROM native_timeline_events
                WHERE provider = ? AND native_thread_id = ? AND sequence < ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (provider_key, thread_id, int(before), safe_limit),
            ).fetchall()
            return [_event_from_row(row) for row in reversed(rows)]
        rows = self._conn.execute(
            """
            SELECT * FROM native_timeline_events
            WHERE provider = ? AND native_thread_id = ? AND sequence > ?
            ORDER BY sequence ASC
            LIMIT ?
            """,
            (provider_key, thread_id, int(after or 0), safe_limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_items(
        self,
        provider: str,
        native_thread_id: str,
        *,
        limit: int = 100,
    ) -> list[NativeTimelineItem]:
        rows = self._conn.execute(
            """
            SELECT * FROM native_timeline_items
            WHERE provider = ? AND native_thread_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (_normalize_provider(provider), str(native_thread_id or "").strip(), limit),
        ).fetchall()
        return [_item_from_row(row) for row in rows]

    def list_conversation_items(
        self,
        provider: str,
        native_thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> list[NativeTimelineItem]:
        provider_key = _normalize_provider(provider)
        thread_id = str(native_thread_id or "").strip()
        safe_limit = max(1, min(int(limit or 100), 500))
        if before is not None and before > 0:
            rows = self._conn.execute(
                f"""
                SELECT * FROM native_timeline_items
                WHERE provider = ? AND native_thread_id = ?
                  AND kind IN ({_visible_item_kind_placeholders()})
                  AND COALESCE(NULLIF(last_sequence, 0), id) < ?
                ORDER BY COALESCE(NULLIF(last_sequence, 0), id) DESC, id DESC
                LIMIT ?
                """,
                (
                    provider_key,
                    thread_id,
                    *_VISIBLE_CONVERSATION_ITEM_KINDS,
                    int(before),
                    safe_limit,
                ),
            ).fetchall()
            return [_item_from_row(row) for row in reversed(rows)]
        after_sequence = int(after or 0)
        if after_sequence > 0:
            rows = self._conn.execute(
                f"""
                SELECT * FROM native_timeline_items
                WHERE provider = ? AND native_thread_id = ?
                  AND kind IN ({_visible_item_kind_placeholders()})
                  AND COALESCE(NULLIF(last_sequence, 0), id) > ?
                ORDER BY COALESCE(NULLIF(last_sequence, 0), id) ASC, id ASC
                LIMIT ?
                """,
                (
                    provider_key,
                    thread_id,
                    *_VISIBLE_CONVERSATION_ITEM_KINDS,
                    after_sequence,
                    safe_limit,
                ),
            ).fetchall()
            return [_item_from_row(row) for row in rows]
        rows = self._conn.execute(
            f"""
            SELECT * FROM native_timeline_items
            WHERE provider = ? AND native_thread_id = ?
              AND kind IN ({_visible_item_kind_placeholders()})
            ORDER BY COALESCE(NULLIF(last_sequence, 0), id) DESC, id DESC
            LIMIT ?
            """,
            (provider_key, thread_id, *_VISIBLE_CONVERSATION_ITEM_KINDS, safe_limit),
        ).fetchall()
        return [_item_from_row(row) for row in reversed(rows)]

    def list_conversation_items_by_id(
        self,
        provider: str,
        native_thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> list[NativeTimelineItem]:
        provider_key = _normalize_provider(provider)
        thread_id = str(native_thread_id or "").strip()
        safe_limit = max(1, min(int(limit or 100), 500))
        if before is not None and before > 0:
            rows = self._conn.execute(
                f"""
                SELECT * FROM native_timeline_items
                WHERE provider = ? AND native_thread_id = ?
                  AND kind IN ({_visible_item_kind_placeholders()})
                  AND id < ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    provider_key,
                    thread_id,
                    *_VISIBLE_CONVERSATION_ITEM_KINDS,
                    int(before),
                    safe_limit,
                ),
            ).fetchall()
            return [_item_from_row(row) for row in reversed(rows)]
        after_id = int(after or 0)
        if after_id > 0:
            rows = self._conn.execute(
                f"""
                SELECT * FROM native_timeline_items
                WHERE provider = ? AND native_thread_id = ?
                  AND kind IN ({_visible_item_kind_placeholders()})
                  AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (
                    provider_key,
                    thread_id,
                    *_VISIBLE_CONVERSATION_ITEM_KINDS,
                    after_id,
                    safe_limit,
                ),
            ).fetchall()
            return [_item_from_row(row) for row in rows]
        rows = self._conn.execute(
            f"""
            SELECT * FROM native_timeline_items
            WHERE provider = ? AND native_thread_id = ?
              AND kind IN ({_visible_item_kind_placeholders()})
            ORDER BY id DESC
            LIMIT ?
            """,
            (provider_key, thread_id, *_VISIBLE_CONVERSATION_ITEM_KINDS, safe_limit),
        ).fetchall()
        return [_item_from_row(row) for row in reversed(rows)]

    def get_conversation_item(self, item_row_id: int | None) -> NativeTimelineItem | None:
        if item_row_id is None:
            return None
        row = self._conn.execute(
            f"""
            SELECT * FROM native_timeline_items
            WHERE id = ? AND kind IN ({_visible_item_kind_placeholders()})
            """,
            (int(item_row_id), *_VISIBLE_CONVERSATION_ITEM_KINDS),
        ).fetchone()
        return _item_from_row(row) if row is not None else None

    def count_conversation_items_before(
        self,
        provider: str,
        native_thread_id: str,
        *,
        before: int,
    ) -> int:
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM native_timeline_items
            WHERE provider = ? AND native_thread_id = ?
              AND kind IN ({_visible_item_kind_placeholders()})
              AND COALESCE(NULLIF(last_sequence, 0), id) < ?
            """,
            (
                _normalize_provider(provider),
                str(native_thread_id or "").strip(),
                *_VISIBLE_CONVERSATION_ITEM_KINDS,
                int(before),
            ),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def count_conversation_items_before_id(
        self,
        provider: str,
        native_thread_id: str,
        *,
        before: int,
    ) -> int:
        row = self._conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM native_timeline_items
            WHERE provider = ? AND native_thread_id = ?
              AND kind IN ({_visible_item_kind_placeholders()})
              AND id < ?
            """,
            (
                _normalize_provider(provider),
                str(native_thread_id or "").strip(),
                *_VISIBLE_CONVERSATION_ITEM_KINDS,
                int(before),
            ),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def latest_sequence(self, provider: str, native_thread_id: str) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) AS latest_sequence
            FROM native_timeline_events
            WHERE provider = ? AND native_thread_id = ?
            """,
            (_normalize_provider(provider), str(native_thread_id or "").strip()),
        ).fetchone()
        return int(row["latest_sequence"] if row is not None else 0)

    def latest_turn_run_state(self, provider: str, native_thread_id: str) -> dict[str, Any]:
        provider_key = _normalize_provider(provider)
        thread_id = str(native_thread_id or "").strip()
        placeholders = ",".join("?" for _ in _NATIVE_TURN_STATE_EVENT_TYPES)
        rows = self._conn.execute(
            f"""
            SELECT event_type, payload_json
            FROM runtime_events
            WHERE event_type IN ({placeholders})
            ORDER BY id DESC
            LIMIT 200
            """,
            _NATIVE_TURN_STATE_EVENT_TYPES,
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if _normalize_provider(
                _first_text(payload, "provider", "native_provider") or provider_key
            ) != provider_key:
                continue
            payload_thread = _first_text(
                payload,
                "native_thread_id",
                "native_session_id",
                "thread_id",
                "threadId",
                "session_id",
            )
            if payload_thread != thread_id:
                continue
            event_type = str(row["event_type"])
            turn_key = _first_text(
                payload,
                "native_turn_id",
                "turnId",
                "turn_id",
                "active_turn_id",
            )
            if event_type in _NATIVE_TURN_TERMINAL_EVENT_TYPES or _is_terminal_turn_payload(
                payload
            ):
                return {
                    "active": False,
                    "status": _terminal_turn_status(event_type, payload),
                    "active_turn_id": "",
                }
            latest_item = self._latest_visible_item_for_run_state(provider_key, thread_id)
            if latest_item is not None and str(latest_item["turn_key"]) != turn_key:
                latest_status = str(latest_item["status"] or "").strip().lower()
                if latest_status in _NATIVE_ACTIVE_ITEM_STATUSES:
                    return {
                        "active": True,
                        "status": latest_status,
                        "active_turn_id": str(latest_item["turn_key"]),
                    }
                return {"active": False, "status": "idle", "active_turn_id": ""}
            return {
                "active": True,
                "status": _active_turn_status(event_type, payload),
                "active_turn_id": turn_key,
            }
        return {"active": False, "status": "idle", "active_turn_id": ""}

    def _latest_visible_item_for_run_state(
        self,
        provider: str,
        native_thread_id: str,
    ) -> sqlite3.Row | None:
        placeholders = _visible_item_kind_placeholders()
        return self._conn.execute(
            f"""
            SELECT *
            FROM native_timeline_items
            WHERE provider = ? AND native_thread_id = ?
              AND kind IN ({placeholders})
            ORDER BY last_sequence DESC, id DESC
            LIMIT 1
            """,
            (provider, native_thread_id, *_VISIBLE_CONVERSATION_ITEM_KINDS),
        ).fetchone()

    def list_item_events(
        self,
        provider: str,
        native_thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> list[NativeTimelineEvent]:
        provider_key = _normalize_provider(provider)
        thread_id = str(native_thread_id or "").strip()
        safe_limit = max(1, min(int(limit or 100), 500))
        after_sequence = int(after or 0)
        if before is not None and before > 0:
            rows = self._conn.execute(
                """
                SELECT
                    e.*,
                    i.turn_key AS item_turn_key,
                    i.item_key AS item_item_key,
                    i.role AS item_role,
                    i.kind AS item_kind,
                    i.text AS item_text,
                    i.status AS item_status,
                    i.payload_json AS item_payload_json
                FROM native_timeline_events e
                JOIN native_timeline_items i ON i.id = e.item_row_id
                JOIN (
                    SELECT item_row_id, MAX(sequence) AS max_sequence
                    FROM native_timeline_events
                    WHERE provider = ? AND native_thread_id = ?
                      AND item_row_id IS NOT NULL
                      AND item_row_id IN (
                          SELECT id FROM native_timeline_items
                          WHERE provider = ? AND native_thread_id = ?
                            AND kind IN (
                                'user_message', 'message',
                                'activity', 'approval_requested'
                            )
                      )
                    GROUP BY item_row_id
                    HAVING max_sequence < ?
                    ORDER BY max_sequence DESC
                    LIMIT ?
                ) latest
                  ON latest.item_row_id = e.item_row_id
                 AND latest.max_sequence = e.sequence
                ORDER BY e.sequence ASC
                """,
                (provider_key, thread_id, provider_key, thread_id, int(before), safe_limit),
            ).fetchall()
            return [_item_event_from_row(row) for row in rows]
        if after_sequence > 0:
            rows = self._conn.execute(
                """
                SELECT
                    e.*,
                    i.turn_key AS item_turn_key,
                    i.item_key AS item_item_key,
                    i.role AS item_role,
                    i.kind AS item_kind,
                    i.text AS item_text,
                    i.status AS item_status,
                    i.payload_json AS item_payload_json
                FROM native_timeline_events e
                JOIN native_timeline_items i ON i.id = e.item_row_id
                JOIN (
                    SELECT item_row_id, MAX(sequence) AS max_sequence
                    FROM native_timeline_events
                    WHERE provider = ? AND native_thread_id = ?
                      AND sequence > ?
                      AND item_row_id IS NOT NULL
                      AND item_row_id IN (
                          SELECT id FROM native_timeline_items
                          WHERE provider = ? AND native_thread_id = ?
                            AND kind IN (
                                'user_message', 'message',
                                'activity', 'approval_requested'
                            )
                      )
                    GROUP BY item_row_id
                    ORDER BY max_sequence ASC
                    LIMIT ?
                ) latest
                  ON latest.item_row_id = e.item_row_id
                 AND latest.max_sequence = e.sequence
                ORDER BY e.sequence ASC
                """,
                (
                    provider_key,
                    thread_id,
                    after_sequence,
                    provider_key,
                    thread_id,
                    safe_limit,
                ),
            ).fetchall()
            return [_item_event_from_row(row) for row in rows]
        rows = self._conn.execute(
            """
            SELECT
                e.*,
                i.turn_key AS item_turn_key,
                i.item_key AS item_item_key,
                i.role AS item_role,
                i.kind AS item_kind,
                i.text AS item_text,
                i.status AS item_status,
                i.payload_json AS item_payload_json
            FROM native_timeline_events e
            JOIN native_timeline_items i ON i.id = e.item_row_id
            JOIN (
                SELECT item_row_id, MAX(sequence) AS max_sequence
                FROM native_timeline_events
                WHERE provider = ? AND native_thread_id = ?
                  AND item_row_id IS NOT NULL
                  AND item_row_id IN (
                      SELECT id FROM native_timeline_items
                      WHERE provider = ? AND native_thread_id = ?
                        AND kind IN (
                            'user_message', 'message',
                            'activity', 'approval_requested'
                        )
                  )
                GROUP BY item_row_id
                ORDER BY max_sequence DESC
                LIMIT ?
            ) latest
              ON latest.item_row_id = e.item_row_id
             AND latest.max_sequence = e.sequence
            ORDER BY e.sequence ASC
            """,
            (provider_key, thread_id, provider_key, thread_id, safe_limit),
        ).fetchall()
        return [_item_event_from_row(row) for row in rows]

    def count_events_before(
        self,
        provider: str,
        native_thread_id: str,
        *,
        before: int,
    ) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM native_timeline_events
            WHERE provider = ? AND native_thread_id = ? AND sequence < ?
            """,
            (_normalize_provider(provider), str(native_thread_id or "").strip(), int(before)),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def count_item_events_before(
        self,
        provider: str,
        native_thread_id: str,
        *,
        before: int,
    ) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT MAX(sequence) AS max_sequence
                FROM native_timeline_events
                WHERE provider = ? AND native_thread_id = ?
                  AND item_row_id IS NOT NULL
                  AND item_row_id IN (
                      SELECT id FROM native_timeline_items
                      WHERE provider = ? AND native_thread_id = ?
                        AND kind IN (
                            'user_message', 'message',
                            'activity', 'approval_requested'
                        )
                  )
                GROUP BY item_row_id
                HAVING max_sequence < ?
            )
            """,
            (
                _normalize_provider(provider),
                str(native_thread_id or "").strip(),
                _normalize_provider(provider),
                str(native_thread_id or "").strip(),
                int(before),
            ),
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def subscribe(
        self,
        *,
        provider: str,
        native_thread_id: str,
    ) -> asyncio.Queue[NativeTimelineEvent]:
        queue: asyncio.Queue[NativeTimelineEvent] = asyncio.Queue(maxsize=200)
        self._subscribers[(_normalize_provider(provider), native_thread_id)].add(queue)
        return queue

    def unsubscribe(
        self,
        *,
        provider: str,
        native_thread_id: str,
        queue: asyncio.Queue[NativeTimelineEvent],
    ) -> None:
        key = (_normalize_provider(provider), native_thread_id)
        queues = self._subscribers.get(key)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(key, None)

    def _event_from_runtime(
        self,
        event: RuntimeEvent,
        item_row_id: int,
        kind: str,
        payload: dict[str, Any],
    ) -> NativeTimelineEvent:
        event_payload = dict(payload)
        provider = _normalize_provider(
            _first_text(event_payload, "provider", "native_provider")
            or _first_text(event.payload, "provider", "native_provider")
            or str(event.source or "")
        )
        native_thread_id = _first_text(
            event.payload,
            "native_thread_id",
            "native_session_id",
            "thread_id",
            "session_id",
        )
        return self._append_event(
            provider=provider,
            native_thread_id=native_thread_id,
            item_row_id=item_row_id,
            runtime_event_id=event.id,
            event_type=str(event.event_type),
            kind=kind,
            payload=_compact_payload(event_payload),
            occurred_at=str(event.occurred_at),
            agent_run_id=event.agent_run_id,
            conversation_id=event.conversation_id,
        )

    def _upsert_item(
        self,
        *,
        provider: str,
        native_thread_id: str,
        turn_key: str,
        item_key: str,
        role: str,
        kind: str,
        text: str,
        status: str,
        payload: dict[str, Any],
        source_priority: int,
        merge_local: bool,
    ) -> int:
        now = now_iso()
        if merge_local and role == "user":
            local = self._conn.execute(
                """
                SELECT * FROM native_timeline_items
                WHERE provider = ? AND native_thread_id = ? AND role = 'user'
                  AND status = 'pending' AND text = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (provider, native_thread_id, text),
            ).fetchone()
            if local is not None:
                self._conn.execute(
                    """
                    UPDATE native_timeline_items
                    SET turn_key = ?, item_key = ?, kind = ?, status = ?,
                        payload_json = ?, source_priority = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        turn_key,
                        item_key,
                        kind,
                        status,
                        json.dumps(payload, ensure_ascii=False),
                        source_priority,
                        now,
                        int(local["id"]),
                    ),
                )
                self._conn.commit()
                return int(local["id"])

        existing = self._find_item(provider, native_thread_id, turn_key, item_key)
        if existing is None:
            cur = self._conn.execute(
                """
                INSERT INTO native_timeline_items (
                    provider, native_thread_id, turn_key, item_key, role, kind,
                    text, status, payload_json, source_priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    native_thread_id,
                    turn_key,
                    item_key,
                    role,
                    kind,
                    text,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    source_priority,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

        existing_priority = int(existing["source_priority"])
        existing_status = str(existing["status"])
        if existing_priority > source_priority and existing_status == "completed":
            return int(existing["id"])
        if existing_priority > source_priority and status != "streaming":
            return int(existing["id"])
        self._conn.execute(
            """
            UPDATE native_timeline_items
            SET role = ?, kind = ?, text = ?, status = ?, payload_json = ?,
                source_priority = MAX(source_priority, ?), updated_at = ?
            WHERE id = ?
            """,
            (
                role,
                kind,
                text,
                status,
                json.dumps(payload, ensure_ascii=False),
                source_priority,
                now,
                int(existing["id"]),
            ),
        )
        self._conn.commit()
        return int(existing["id"])

    def _find_item(
        self,
        provider: str,
        native_thread_id: str,
        turn_key: str,
        item_key: str,
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM native_timeline_items
            WHERE provider = ? AND native_thread_id = ? AND turn_key = ? AND item_key = ?
            """,
            (provider, native_thread_id, turn_key, item_key),
        ).fetchone()

    def _append_event(
        self,
        *,
        provider: str,
        native_thread_id: str,
        item_row_id: int | None,
        runtime_event_id: int | None,
        event_type: str,
        kind: str,
        payload: dict[str, Any],
        occurred_at: str,
        agent_run_id: int | None,
        conversation_id: int | None,
    ) -> NativeTimelineEvent:
        row = self._conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM native_timeline_events
            WHERE provider = ? AND native_thread_id = ?
            """,
            (provider, native_thread_id),
        ).fetchone()
        sequence = int(row["next_sequence"] if row is not None else 1)
        self._conn.execute(
            """
            INSERT INTO native_timeline_events (
                provider, native_thread_id, sequence, item_row_id, runtime_event_id,
                event_type, kind, payload_json, occurred_at, agent_run_id, conversation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                native_thread_id,
                sequence,
                item_row_id,
                runtime_event_id,
                event_type,
                kind,
                json.dumps(payload, ensure_ascii=False),
                occurred_at,
                agent_run_id,
                conversation_id,
            ),
        )
        if item_row_id is not None:
            self._conn.execute(
                """
                UPDATE native_timeline_items
                SET last_sequence = MAX(last_sequence, ?), updated_at = ?
                WHERE id = ?
                """,
                (sequence, occurred_at, int(item_row_id)),
            )
        self._conn.commit()
        event = NativeTimelineEvent(
            sequence=sequence,
            type=event_type,
            kind=kind,
            provider=provider,
            native_thread_id=native_thread_id,
            occurred_at=occurred_at,
            payload=dict(payload),
            runtime_event_id=runtime_event_id,
            agent_run_id=agent_run_id,
            conversation_id=conversation_id,
            item_row_id=item_row_id,
        )
        self._publish(event)
        return event

    def _publish(self, event: NativeTimelineEvent) -> None:
        for queue in list(
            self._subscribers.get((event.provider, event.native_thread_id), ())
        ):
            _offer(queue, event)


def _event_from_row(row: sqlite3.Row) -> NativeTimelineEvent:
    return NativeTimelineEvent(
        sequence=int(row["sequence"]),
        type=str(row["event_type"]),
        kind=str(row["kind"]),
        provider=str(row["provider"]),
        native_thread_id=str(row["native_thread_id"]),
        occurred_at=str(row["occurred_at"]),
        payload=json.loads(str(row["payload_json"] or "{}")),
        runtime_event_id=row["runtime_event_id"],
        agent_run_id=row["agent_run_id"],
        conversation_id=row["conversation_id"],
        item_row_id=row["item_row_id"],
    )


def _item_event_from_row(row: sqlite3.Row) -> NativeTimelineEvent:
    payload = json.loads(str(row["item_payload_json"] or "{}"))
    item_kind = str(row["item_kind"])
    item_status = str(row["item_status"])
    item_text = str(row["item_text"] or "")
    item_key = str(row["item_item_key"])
    turn_key = str(row["item_turn_key"])
    payload["text"] = item_text
    payload.setdefault("itemId", item_key)
    payload.setdefault("native_turn_id", turn_key)
    payload["status"] = item_status
    payload["role"] = str(row["item_role"])
    kind = str(row["kind"])
    if item_kind == "message":
        kind = "message_completed" if item_status == "completed" else "text_delta"
    elif item_kind in {"user_message", "activity", "approval_requested"}:
        kind = item_kind
    elif item_kind == "command" and kind not in {
        "command_started",
        "command_output",
        "command_completed",
        "command_failed",
    }:
        kind = "command_completed" if item_status == "completed" else "command_started"
    return NativeTimelineEvent(
        sequence=int(row["sequence"]),
        type=str(row["event_type"]),
        kind=kind,
        provider=str(row["provider"]),
        native_thread_id=str(row["native_thread_id"]),
        occurred_at=str(row["occurred_at"]),
        payload=_compact_payload(payload),
        runtime_event_id=row["runtime_event_id"],
        agent_run_id=row["agent_run_id"],
        conversation_id=row["conversation_id"],
        item_row_id=row["item_row_id"],
    )


def _item_from_row(row: sqlite3.Row) -> NativeTimelineItem:
    last_sequence = int(row["last_sequence"] or 0)
    item_id = int(row["id"])
    return NativeTimelineItem(
        id=item_id,
        cursor=last_sequence or item_id,
        provider=str(row["provider"]),
        native_thread_id=str(row["native_thread_id"]),
        turn_key=str(row["turn_key"]),
        item_key=str(row["item_key"]),
        role=str(row["role"]),
        kind=str(row["kind"]),
        text=str(row["text"] or ""),
        status=str(row["status"]),
        payload=json.loads(str(row["payload_json"] or "{}")),
        updated_at=str(row["updated_at"]),
    )


_VISIBLE_CONVERSATION_ITEM_KINDS = (
    "user_message",
    "message",
    "activity",
    "approval_requested",
)

_NATIVE_TURN_ACTIVE_EVENT_TYPES = (
    EventType.AGENT_RUN_QUEUED,
    EventType.AGENT_RUN_STARTED,
    EventType.AGENT_RUN_ACTIVITY,
    EventType.AGENT_RUN_HEARTBEAT,
    EventType.AGENT_RUN_WAITING_FOR_APPROVAL,
)

_NATIVE_TURN_TERMINAL_EVENT_TYPES = (
    EventType.AGENT_RUN_COMPLETED,
    EventType.AGENT_RUN_FAILED,
    EventType.AGENT_RUN_TIMED_OUT,
    EventType.AGENT_RUN_ORPHANED,
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
)

_NATIVE_TURN_STATE_EVENT_TYPES = (
    *_NATIVE_TURN_ACTIVE_EVENT_TYPES,
    *_NATIVE_TURN_TERMINAL_EVENT_TYPES,
)

_NATIVE_ACTIVE_ITEM_STATUSES = {"queued", "streaming", "waiting", "pending", "running"}


def _visible_item_kind_placeholders() -> str:
    return ",".join("?" for _ in _VISIBLE_CONVERSATION_ITEM_KINDS)


def _is_terminal_turn_payload(payload: dict[str, Any]) -> bool:
    action = _first_text(payload, "action").strip().lower()
    return action in {"turn_completed", "turn_failed", "turn_cancelled", "turn_interrupted"}


def _terminal_turn_status(event_type: str, payload: dict[str, Any]) -> str:
    status = _first_text(payload, "status").strip().lower()
    if status:
        return status
    if event_type == EventType.AGENT_RUN_COMPLETED or event_type == EventType.RUN_COMPLETED:
        return "completed"
    if event_type == EventType.AGENT_RUN_TIMED_OUT:
        return "timed_out"
    if event_type == EventType.AGENT_RUN_ORPHANED:
        return "orphaned"
    if event_type == EventType.RUN_CANCELLED:
        return "cancelled"
    return "failed"


def _active_turn_status(event_type: str, payload: dict[str, Any]) -> str:
    status = _first_text(payload, "status").strip().lower()
    if status and status not in {"completed", "done", "succeeded", "success"}:
        return status
    if event_type == EventType.AGENT_RUN_QUEUED:
        return "queued"
    if event_type == EventType.AGENT_RUN_WAITING_FOR_APPROVAL:
        return "waiting"
    return "running"


def _display_role_for_kind(kind: str, payload: dict[str, Any]) -> str:
    payload_role = str(payload.get("role") or "").strip().lower()
    if payload_role in {"user", "assistant", "system"}:
        return payload_role
    if kind == "user_message":
        return "user"
    if kind in {
        "text_delta",
        "message_completed",
        "reasoning_delta",
        "activity",
        "approval_requested",
        "approval_resolved",
    }:
        return "assistant"
    return "system"


def _is_visible_display_kind(kind: str) -> bool:
    return kind in {
        "user_message",
        "text_delta",
        "message_completed",
        "activity",
        "approval_requested",
        "approval_resolved",
    }


def _normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    return value or "codex"


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _timeline_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return "\n".join(parts)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)
    return ""


def _approval_item_key(payload: dict[str, Any], turn_key: str) -> str:
    return _first_text(
        payload,
        "itemId",
        "item_id",
        "request_id",
        "requestId",
        "codexRequestId",
        "approval_id",
    ) or ("approval-" + turn_key)


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in (
        "text",
        "delta",
        "summary",
        "prompt",
        "message",
        "title",
        "action",
        "plan",
        "request_id",
        "requestId",
        "codexRequestId",
        "itemId",
        "item_id",
        "native_turn_id",
        "turn_id",
        "status",
        "role",
        "local",
        "images",
        "command",
        "output",
    ):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            compacted[key] = value
        elif isinstance(value, (int, float, bool)) or value is None:
            compacted[key] = value
        elif key == "plan" and isinstance(value, list):
            compacted[key] = value[:50]
        elif key == "images" and isinstance(value, list):
            compacted[key] = [
                {
                    image_key: image.get(image_key)
                    for image_key in ("filename", "mime_type")
                    if isinstance(image, dict) and image.get(image_key)
                }
                for image in value[:8]
            ]
    return compacted


def _text_fingerprint(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _is_compatibility_projection(payload: dict[str, Any]) -> bool:
    return bool(payload.get("compatibility_projection"))


def _ends_with_delta(text: str, delta: str) -> bool:
    return bool(delta) and text.endswith(delta)


def _offer(
    queue: asyncio.Queue[NativeTimelineEvent],
    event: NativeTimelineEvent,
) -> None:
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    with suppress(asyncio.QueueFull):
        queue.put_nowait(event)
