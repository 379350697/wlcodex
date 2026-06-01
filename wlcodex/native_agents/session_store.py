from __future__ import annotations

import json
import sqlite3
from typing import Any

from wlcodex.db import Ledger, _now
from wlcodex.native_agents.models import NativeAgentSession

_NATIVE_CHAT_ID = 0
_NATIVE_USER_ID = 0
_NATIVE_WORKSPACE_ALIAS = "wlcodex"


class NativeAgentSessionStore:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._conn = ledger._conn

    def get_or_create_native_conversation(self, provider: str) -> int:
        mode = f"{provider}_native"
        row = self._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE chat_id = ? AND user_id = ? AND mode = ?
              AND workspace_alias = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (_NATIVE_CHAT_ID, _NATIVE_USER_ID, mode, _NATIVE_WORKSPACE_ALIAS),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        conversation = self._ledger.create_conversation(
            chat_id=_NATIVE_CHAT_ID,
            user_id=_NATIVE_USER_ID,
            title=f"{provider.title()} Native",
            mode=mode,
            workspace_alias=_NATIVE_WORKSPACE_ALIAS,
        )
        return conversation.id

    def get_by_native_session_id(
        self,
        *,
        provider: str,
        provider_engine: str,
        native_session_id: str,
    ) -> NativeAgentSession | None:
        row = self._conn.execute(
            """
            SELECT * FROM native_agent_sessions
            WHERE provider = ? AND provider_engine = ? AND native_session_id = ?
            """,
            (provider, provider_engine, native_session_id),
        ).fetchone()
        return _session(row) if row is not None else None

    def get_or_create_session(
        self,
        *,
        provider: str,
        provider_engine: str,
        native_session_id: str,
        title: str = "",
        cwd: str = "",
        source_kind: str = "unknown",
        status: str = "unknown",
        last_turn_id: str = "",
        activity_at: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> NativeAgentSession:
        existing = self.get_by_native_session_id(
            provider=provider,
            provider_engine=provider_engine,
            native_session_id=native_session_id,
        )
        if existing is not None:
            return self.update_session(
                session_id=existing.id,
                title=title or existing.title,
                cwd=cwd or existing.cwd,
                source_kind=source_kind or existing.source_kind,
                status=_updated_value(status, existing.status, default="unknown"),
                last_turn_id=last_turn_id or existing.last_turn_id,
                activity_at=activity_at or existing.activity_at,
                metadata=metadata if metadata is not None else existing.metadata,
            )

        conversation_id = self.get_or_create_native_conversation(provider)
        agent_run = self._ledger.create_agent_run(
            conversation_id,
            agent=provider,
            role=f"{provider}_native",
            external_session_id=native_session_id,
            prompt_packet_summary=f"{provider} native session",
        )
        self._ledger.update_agent_run_status(
            agent_run.id,
            _agent_run_status(status),
            external_session_id=native_session_id,
        )
        now = _now()
        self._conn.execute(
            """
            INSERT INTO native_agent_sessions (
                provider, provider_engine, native_session_id, agent_run_id,
                conversation_id, title, cwd, source_kind, status, last_turn_id,
                activity_at, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                provider_engine,
                native_session_id,
                agent_run.id,
                conversation_id,
                title,
                cwd,
                source_kind,
                status,
                last_turn_id,
                activity_at or now,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        self._conn.commit()
        created = self.get_by_native_session_id(
            provider=provider,
            provider_engine=provider_engine,
            native_session_id=native_session_id,
        )
        if created is None:
            raise KeyError(f"native agent session was not created: {native_session_id}")
        return created

    def update_session(
        self,
        session_id: int,
        *,
        title: str | None = None,
        cwd: str | None = None,
        source_kind: str | None = None,
        status: str | None = None,
        last_turn_id: str | None = None,
        activity_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NativeAgentSession:
        existing = self._lookup_session(session_id)
        if status is not None:
            self._ledger.update_agent_run_status(
                existing.agent_run_id,
                _agent_run_status(status),
                external_session_id=existing.native_session_id,
            )
        self._conn.execute(
            """
            UPDATE native_agent_sessions
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
                json.dumps(
                    metadata if metadata is not None else existing.metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                _now(),
                existing.id,
            ),
        )
        self._conn.commit()
        return self._lookup_session(existing.id)

    def list_recent(
        self,
        *,
        provider: str,
        provider_engine: str = "",
        limit: int = 50,
    ) -> list[NativeAgentSession]:
        where = "provider = ?"
        params: list[Any] = [provider]
        if provider_engine:
            where += " AND provider_engine = ?"
            params.append(provider_engine)
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM native_agent_sessions
            WHERE {where}
            ORDER BY
                CASE
                    WHEN activity_at IS NOT NULL AND activity_at != '' THEN activity_at
                    ELSE updated_at
                END DESC,
                id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_session(row) for row in rows]

    def _lookup_session(self, session_id: int) -> NativeAgentSession:
        row = self._conn.execute(
            "SELECT * FROM native_agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown native agent session id: {session_id}")
        return _session(row)


def _agent_run_status(status: str) -> str:
    if status in ("running", "active"):
        return "running"
    if status in ("done", "completed"):
        return "done"
    if status in ("failed", "error"):
        return "failed"
    return "queued"


def _updated_value(value: str, existing: str, *, default: str) -> str:
    return existing if value == default else value or existing


def _session(row: sqlite3.Row) -> NativeAgentSession:
    return NativeAgentSession(
        id=int(row["id"]),
        provider=str(row["provider"]),
        provider_engine=str(row["provider_engine"]),
        native_session_id=str(row["native_session_id"]),
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
        metadata=json.loads(str(row["metadata_json"] or "{}")),
    )
