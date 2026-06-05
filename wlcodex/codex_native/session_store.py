from __future__ import annotations

import json
import sqlite3
from typing import Any

from wlcodex.codex_native.models import NativeCodexSession
from wlcodex.db import Ledger, _now

_NATIVE_CHAT_ID = 0
_NATIVE_USER_ID = 0
_NATIVE_TITLE = "Codex Native"
_NATIVE_MODE = "codex_native"
_NATIVE_WORKSPACE_ALIAS = "wlcodex"


class NativeCodexSessionStore:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._conn = ledger._conn

    def get_or_create_native_conversation(self) -> int:
        row = self._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE chat_id = ? AND user_id = ? AND mode = ?
              AND workspace_alias = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (
                _NATIVE_CHAT_ID,
                _NATIVE_USER_ID,
                _NATIVE_MODE,
                _NATIVE_WORKSPACE_ALIAS,
            ),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        conversation = self._ledger.create_conversation(
            chat_id=_NATIVE_CHAT_ID,
            user_id=_NATIVE_USER_ID,
            title=_NATIVE_TITLE,
            mode=_NATIVE_MODE,
            workspace_alias=_NATIVE_WORKSPACE_ALIAS,
        )
        return conversation.id

    def get_by_thread_id(self, native_thread_id: str) -> NativeCodexSession | None:
        row = self._conn.execute(
            "SELECT * FROM native_codex_sessions WHERE native_thread_id = ?",
            (native_thread_id,),
        ).fetchone()
        return _session(row) if row is not None else None

    def get_or_create_session(
        self,
        *,
        native_thread_id: str,
        title: str = "",
        cwd: str = "",
        source_kind: str = "unknown",
        status: str = "unknown",
        last_turn_id: str = "",
        activity_at: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> NativeCodexSession:
        existing = self.get_by_thread_id(native_thread_id)
        if existing is not None:
            return self.update_session(
                native_thread_id=native_thread_id,
                title=title or existing.title,
                cwd=cwd or existing.cwd,
                source_kind=source_kind or existing.source_kind,
                status=status or existing.status,
                last_turn_id=last_turn_id or existing.last_turn_id,
                activity_at=activity_at or existing.activity_at,
                metadata=metadata,
            )

        conversation_id = self.get_or_create_native_conversation()
        agent_run = self._ledger.create_agent_run(
            conversation_id,
            agent="codex",
            role="codex_native",
            external_session_id=native_thread_id,
            prompt_packet_summary="Official Codex IDE session",
        )
        self._ledger.update_agent_run_status(
            agent_run.id,
            _agent_run_status(status),
            external_session_id=native_thread_id,
        )
        now = _now()
        self._conn.execute(
            """
            INSERT INTO native_codex_sessions (
                native_thread_id, agent_run_id, conversation_id, title, cwd,
                source_kind, status, last_turn_id, activity_at, metadata_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                native_thread_id,
                agent_run.id,
                conversation_id,
                title,
                cwd,
                source_kind,
                status,
                last_turn_id,
                activity_at or now,
                _metadata_json(metadata or {}),
                now,
                now,
            ),
        )
        self._conn.commit()
        created = self.get_by_thread_id(native_thread_id)
        if created is None:
            raise KeyError(f"native session was not created: {native_thread_id}")
        return created

    def update_session(
        self,
        session_id: int | None = None,
        *,
        native_thread_id: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
        source_kind: str | None = None,
        status: str | None = None,
        last_turn_id: str | None = None,
        activity_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NativeCodexSession:
        existing = self._lookup_session(session_id, native_thread_id)
        if status is not None:
            self._ledger.update_agent_run_status(
                existing.agent_run_id,
                _agent_run_status(status),
                external_session_id=existing.native_thread_id,
            )
        self._conn.execute(
            """
            UPDATE native_codex_sessions
            SET title = ?, cwd = ?, source_kind = ?, status = ?,
                last_turn_id = ?, activity_at = ?, metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title if title is not None else existing.title,
                cwd if cwd is not None else existing.cwd,
                source_kind if source_kind is not None else existing.source_kind,
                status if status is not None else existing.status,
                last_turn_id if last_turn_id is not None else existing.last_turn_id,
                activity_at if activity_at is not None else existing.activity_at,
                _metadata_json(_merged_metadata(existing.metadata, metadata)),
                _now(),
                existing.id,
            ),
        )
        self._conn.commit()
        updated = self.get_by_thread_id(existing.native_thread_id)
        if updated is None:
            raise KeyError(
                f"unknown native Codex thread after update: {existing.native_thread_id}"
            )
        return updated

    def list_recent(self, *, limit: int = 50) -> list[NativeCodexSession]:
        rows = self._conn.execute(
            """
            SELECT * FROM native_codex_sessions
            ORDER BY
                CASE
                    WHEN activity_at IS NOT NULL AND activity_at != '' THEN activity_at
                    ELSE updated_at
                END DESC,
                id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_session(row) for row in rows]

    def _lookup_session(
        self,
        session_id: int | None,
        native_thread_id: str | None,
    ) -> NativeCodexSession:
        if native_thread_id is not None:
            existing = self.get_by_thread_id(native_thread_id)
            if existing is None:
                raise KeyError(f"unknown native Codex thread: {native_thread_id}")
            return existing
        if session_id is None:
            raise TypeError("update_session requires session_id or native_thread_id")
        row = self._conn.execute(
            "SELECT * FROM native_codex_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown native Codex session id: {session_id}")
        return _session(row)


def _agent_run_status(status: str) -> str:
    if status in ("running", "active"):
        return "running"
    if status in ("done", "completed"):
        return "done"
    if status in ("failed", "error"):
        return "failed"
    return "queued"


def _session(row: sqlite3.Row) -> NativeCodexSession:
    return NativeCodexSession(
        id=int(row["id"]),
        native_thread_id=str(row["native_thread_id"]),
        agent_run_id=int(row["agent_run_id"]),
        conversation_id=int(row["conversation_id"]),
        title=str(row["title"]),
        cwd=str(row["cwd"]),
        source_kind=str(row["source_kind"]),
        status=str(row["status"]),
        last_turn_id=str(row["last_turn_id"]),
        activity_at=str(row["activity_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        metadata=_row_metadata(row),
    )


def _row_metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw = row["metadata_json"]
    except (IndexError, KeyError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _merged_metadata(
    existing: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if metadata is None:
        return dict(existing)
    merged = dict(existing)
    merged.update(metadata)
    return merged


def _metadata_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)
