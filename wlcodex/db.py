from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from wlcodex.models import (
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    BackendRequest,
    BackendRequestStatus,
    Task,
    TaskEvent,
    TaskStatus,
    TouchedFile,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Ledger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path) -> "Ledger":
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path))

    def migrate(self) -> None:
        # Create tables idempotently
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_alias TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                codex_thread_id TEXT,
                active_turn_id TEXT,
                parent_task_id INTEGER,
                telegram_chat_id INTEGER,
                telegram_status_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_summary TEXT NOT NULL DEFAULT '',
                last_phase TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                changed_file_count INTEGER NOT NULL DEFAULT 0,
                pending_approval_count INTEGER NOT NULL DEFAULT 0,
                token_input INTEGER NOT NULL DEFAULT 0,
                token_output INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_workspace_status
                ON tasks(workspace_alias, status);

            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_task_events_task_id_id
                ON task_events(task_id, id);

            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                codex_request_id TEXT NOT NULL,
                codex_item_id TEXT,
                codex_turn_id TEXT,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                command_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                telegram_message_id INTEGER,
                resolution TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_approval_requests_task_id
                ON approval_requests(task_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_task_codex_id
                ON approval_requests(task_id, codex_request_id);

            CREATE TABLE IF NOT EXISTS touched_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                change_kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_touched_files_task_id
                ON touched_files(task_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_touched_files_unique
                ON touched_files(task_id, path, change_kind);

            CREATE TABLE IF NOT EXISTS backend_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jsonrpc_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                task_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS telegram_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_update_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                update_type TEXT NOT NULL,
                allowed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_telegram_updates_update_id
                ON telegram_updates(telegram_update_id);

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        # Guarded column upgrades for legacy databases that already have
        # a tasks table but lack columns added after the initial schema.
        self._add_column_if_missing(
            "tasks", "active_turn_id", "active_turn_id TEXT"
        )
        self._add_column_if_missing(
            "tasks", "parent_task_id", "parent_task_id INTEGER"
        )
        self._add_column_if_missing(
            "tasks", "telegram_chat_id", "telegram_chat_id INTEGER"
        )
        self._add_column_if_missing(
            "tasks", "telegram_status_message_id", "telegram_status_message_id INTEGER"
        )
        self._add_column_if_missing(
            "tasks", "last_summary", "last_summary TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "last_phase", "last_phase TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "last_error", "last_error TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "changed_file_count", "changed_file_count INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "pending_approval_count", "pending_approval_count INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "token_input", "token_input INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "token_output", "token_output INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "worktree_path", "worktree_path TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "worktree_branch", "worktree_branch TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "is_force_parallel", "is_force_parallel INTEGER NOT NULL DEFAULT 0"
        )
        # Approval request columns
        self._add_column_if_missing(
            "approval_requests", "telegram_message_id", "telegram_message_id INTEGER"
        )
        self._add_column_if_missing(
            "approval_requests", "resolution", "resolution TEXT"
        )
        self._add_column_if_missing(
            "approval_requests", "resolved_at", "resolved_at TEXT"
        )
        # codex_request_id values are scoped to a task/thread by the app-server.
        # Older databases had a global unique index, which incorrectly dropped
        # approvals when different tasks reused ids like "0" or "1".
        self._conn.execute("DROP INDEX IF EXISTS idx_approval_requests_codex_id")
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_task_codex_id
                ON approval_requests(task_id, codex_request_id)
            """
        )

        # Record schema version
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "2"),
        )

        self._conn.commit()

    # --- Tasks ---

    def create_task(
        self,
        workspace_alias: str,
        workspace_path: str,
        title: str,
        codex_thread_id: str | None,
        parent_task_id: int | None,
        telegram_chat_id: int | None = None,
        status: TaskStatus = TaskStatus.QUEUED,
    ) -> Task:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO tasks (
                workspace_alias, workspace_path, title, status, codex_thread_id,
                active_turn_id, parent_task_id, telegram_chat_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                workspace_alias,
                workspace_path,
                title,
                status.value,
                codex_thread_id,
                parent_task_id,
                telegram_chat_id,
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_task(int(cur.lastrowid))

    def get_task(self, task_id: int) -> Task:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task id: {task_id}")
        return _task(row)

    def list_tasks(
        self, limit: int = 20, include_archived: bool = False
    ) -> list[Task]:
        if include_archived:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status != ? ORDER BY updated_at DESC, id DESC LIMIT ?",
                (TaskStatus.ARCHIVED.value, limit),
            ).fetchall()
        return [_task(row) for row in rows]

    def set_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        phase: str = "",
        summary: str = "",
        error: str = "",
    ) -> None:
        self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, last_phase = ?, last_summary = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status.value, phase, summary, error, _now(), task_id),
        )
        self._conn.commit()

    def set_active_turn(self, task_id: int, turn_id: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET active_turn_id = ?, updated_at = ? WHERE id = ?",
            (turn_id, _now(), task_id),
        )
        self._conn.commit()

    def clear_active_turn(self, task_id: int) -> None:
        self._conn.execute(
            "UPDATE tasks SET active_turn_id = NULL, updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        self._conn.commit()

    def set_status_message(
        self, task_id: int, chat_id: int, message_id: int
    ) -> None:
        self._conn.execute(
            "UPDATE tasks SET telegram_chat_id = ?, telegram_status_message_id = ?, updated_at = ? WHERE id = ?",
            (chat_id, message_id, _now(), task_id),
        )
        self._conn.commit()

    def increment_changed_files(self, task_id: int, delta: int = 1) -> None:
        self._conn.execute(
            "UPDATE tasks SET changed_file_count = changed_file_count + ?, updated_at = ? WHERE id = ?",
            (delta, _now(), task_id),
        )
        self._conn.commit()

    def increment_pending_approvals(self, task_id: int, delta: int = 1) -> None:
        self._conn.execute(
            "UPDATE tasks SET pending_approval_count = pending_approval_count + ?, updated_at = ? WHERE id = ?",
            (delta, _now(), task_id),
        )
        self._conn.commit()

    def set_token_usage(self, task_id: int, token_input: int, token_output: int) -> None:
        self._conn.execute(
            "UPDATE tasks SET token_input = ?, token_output = ?, updated_at = ? WHERE id = ?",
            (token_input, token_output, _now(), task_id),
        )
        self._conn.commit()

    # --- Events ---

    def add_event(self, task_id: int, event_type: str, payload: dict[str, Any]) -> TaskEvent:
        self._conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, event_type, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self._conn.commit()
        return self.list_events(task_id)[-1]

    def list_events(self, task_id: int, limit: int = 200) -> list[TaskEvent]:
        rows = self._conn.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        return [_event(row) for row in rows]

    # --- Approval requests ---

    def create_approval(
        self,
        task_id: int,
        codex_request_id: str,
        codex_item_id: str | None,
        codex_turn_id: str | None,
        kind: ApprovalKind | str,
        summary: str,
        command_json: str = "{}",
        telegram_message_id: int | None = None,
    ) -> ApprovalRequest:
        kind_str = kind.value if isinstance(kind, ApprovalKind) else kind
        now = _now()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO approval_requests (
                task_id, codex_request_id, codex_item_id, codex_turn_id,
                kind, summary, command_json, status, telegram_message_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                task_id, codex_request_id, codex_item_id, codex_turn_id,
                kind_str, summary, command_json, telegram_message_id, now,
            ),
        )
        self._conn.commit()
        approval = self.get_approval_by_codex_id(codex_request_id, task_id=task_id)
        if approval is None:
            raise KeyError(
                f"unknown approval for task #{task_id} codex_request_id: {codex_request_id}"
            )
        return approval

    def get_approval(self, approval_id: int) -> ApprovalRequest:
        row = self._conn.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown approval id: {approval_id}")
        return _approval(row)

    def get_approval_by_codex_id(
        self, codex_request_id: str, *, task_id: int | None = None
    ) -> ApprovalRequest | None:
        if task_id is not None:
            row = self._conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE task_id = ? AND codex_request_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id, codex_request_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE codex_request_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (codex_request_id,),
            ).fetchone()
        return _approval(row) if row else None

    def resolve_approval(
        self, approval_id: int, status: ApprovalStatus | str, resolution: str = ""
    ) -> ApprovalRequest:
        status_str = status.value if isinstance(status, ApprovalStatus) else status
        now = _now()
        cur = self._conn.execute(
            """
            UPDATE approval_requests
            SET status = ?, resolution = ?, resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status_str, resolution, now, approval_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            # Already resolved — return existing
            return self.get_approval(approval_id)
        return self.get_approval(approval_id)

    def pending_approvals(self, task_id: int) -> list[ApprovalRequest]:
        rows = self._conn.execute(
            """
            SELECT * FROM approval_requests
            WHERE task_id = ? AND status = 'pending'
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
        return [_approval(row) for row in rows]

    # --- Touched files ---

    def record_touched_file(
        self, task_id: int, path: str, change_kind: str
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO touched_files (task_id, path, change_kind, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, path, change_kind, _now()),
        )
        self._conn.commit()

    def list_touched_files(self, task_id: int) -> list[TouchedFile]:
        rows = self._conn.execute(
            "SELECT * FROM touched_files WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [_touched(row) for row in rows]

    # --- Backend requests ---

    def create_backend_request(
        self, jsonrpc_id: int, method: str, task_id: int | None = None
    ) -> BackendRequest:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO backend_requests (jsonrpc_id, method, task_id, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (jsonrpc_id, method, task_id, BackendRequestStatus.PENDING.value, now),
        )
        self._conn.commit()
        return self.get_backend_request(int(cur.lastrowid))

    def complete_backend_request(
        self, request_id: int, error: str | None = None
    ) -> None:
        status = BackendRequestStatus.FAILED.value if error else BackendRequestStatus.COMPLETED.value
        self._conn.execute(
            "UPDATE backend_requests SET status = ?, completed_at = ?, error = ? WHERE id = ?",
            (status, _now(), error or "", request_id),
        )
        self._conn.commit()

    def get_backend_request(self, request_id: int) -> BackendRequest:
        row = self._conn.execute(
            "SELECT * FROM backend_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown backend request id: {request_id}")
        return _backend_request(row)

    # --- Telegram updates ---

    def record_telegram_update(
        self, update_id: int, user_id: int, chat_id: int, update_type: str, allowed: bool
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO telegram_updates (telegram_update_id, user_id, chat_id, update_type, allowed, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (update_id, user_id, chat_id, update_type, int(allowed), _now()),
        )
        self._conn.commit()

    # --- Column upgrade helpers ---

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _add_column_if_missing(self, table: str, column: str, ddl: str) -> None:
        if column not in self._table_columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    # --- Thread id helper ---

    def set_thread_id(self, task_id: int, codex_thread_id: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET codex_thread_id = ?, updated_at = ? WHERE id = ?",
            (codex_thread_id, _now(), task_id),
        )
        self._conn.commit()

    def set_worktree_info(
        self, task_id: int, worktree_path: str, worktree_branch: str
    ) -> None:
        self._conn.execute(
            "UPDATE tasks SET worktree_path = ?, worktree_branch = ?, updated_at = ? WHERE id = ?",
            (worktree_path, worktree_branch, _now(), task_id),
        )
        self._conn.commit()

    def set_force_parallel(self, task_id: int) -> None:
        self._conn.execute(
            "UPDATE tasks SET is_force_parallel = 1, updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        self._conn.commit()

    # --- Pending approval helpers ---

    def decrement_pending_approvals(self, task_id: int) -> None:
        self._conn.execute(
            """
            UPDATE tasks
            SET pending_approval_count = CASE
                WHEN pending_approval_count > 0 THEN pending_approval_count - 1
                ELSE 0
            END,
            updated_at = ?
            WHERE id = ?
            """,
            (_now(), task_id),
        )
        self._conn.commit()

    def set_approval_error(self, approval_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE approval_requests SET resolution = ?, resolved_at = ? WHERE id = ?",
            (f"error: {error[:240]}", _now(), approval_id),
        )
        self._conn.commit()

    # --- Recovery ---

    def mark_active_tasks_recovery_paused(self) -> list[int]:
        """Mark running/queued/waiting_approval tasks as paused on startup.

        Returns the list of task ids that were paused.
        """
        rows = self._conn.execute(
            """
            SELECT id, status FROM tasks
            WHERE status IN (?, ?, ?)
            """,
            (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value, TaskStatus.WAITING_APPROVAL.value),
        ).fetchall()

        paused_ids = [int(row["id"]) for row in rows]
        if paused_ids:
            now = _now()
            placeholders = ",".join("?" for _ in paused_ids)
            self._conn.execute(
                f"UPDATE tasks SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
                (TaskStatus.PAUSED.value, now, *paused_ids),
            )
            self._conn.commit()

            for task_id in paused_ids:
                self.add_event(
                    task_id,
                    "recovery_paused",
                    {"previous_status": dict(row)["status"] for row in rows if int(row["id"]) == task_id},
                )

        return paused_ids

    # --- Liveness helpers ---

    def list_active_tasks(self, limit: int = 100) -> list[Task]:
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE status IN (?, ?, ?, ?)
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.WAITING_APPROVAL.value,
                TaskStatus.PAUSED.value,
                limit,
            ),
        ).fetchall()
        return [_task(row) for row in rows]

    def list_waiting_tasks(self, workspace_alias: str) -> list[Task]:
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE workspace_alias = ? AND status = ?
            ORDER BY created_at ASC, id ASC
            """,
            (workspace_alias, TaskStatus.WAITING_SLOT.value),
        ).fetchall()
        return [_task(row) for row in rows]

    def mark_task_timeout(
        self,
        task_id: int,
        *,
        status: TaskStatus,
        age_seconds: int,
        threshold_seconds: int,
    ) -> Task:
        error = (
            f"task timed out in {status.value} after "
            f"{age_seconds}s (limit {threshold_seconds}s)"
        )
        self.set_task_status(
            task_id,
            TaskStatus.FAILED,
            phase="timeout",
            error=error[:240],
        )
        self.add_event(
            task_id,
            "task_timeout",
            {
                "status": status.value,
                "age_seconds": age_seconds,
                "threshold_seconds": threshold_seconds,
            },
        )
        self.clear_active_turn(task_id)
        return self.get_task(task_id)

    def mark_backend_dead(self, task_id: int, summary: str) -> Task:
        error = f"backend dead: {summary}"
        self.set_task_status(
            task_id,
            TaskStatus.FAILED,
            phase="backend_dead",
            error=error[:240],
        )
        self.add_event(task_id, "backend_dead", {"summary": summary[:500]})
        self.clear_active_turn(task_id)
        return self.get_task(task_id)


# --- Row mappers ---

def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=int(row["id"]),
        workspace_alias=str(row["workspace_alias"]),
        workspace_path=str(row["workspace_path"]),
        title=str(row["title"]),
        status=TaskStatus(str(row["status"])),
        codex_thread_id=row["codex_thread_id"],
        active_turn_id=row["active_turn_id"],
        parent_task_id=row["parent_task_id"],
        telegram_chat_id=row["telegram_chat_id"],
        telegram_status_message_id=row["telegram_status_message_id"],
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
        last_summary=str(row["last_summary"]),
        last_phase=str(row["last_phase"]),
        last_error=str(row["last_error"]),
        changed_file_count=int(row["changed_file_count"] or 0),
        pending_approval_count=int(row["pending_approval_count"] or 0),
        token_input=int(row["token_input"] or 0),
        token_output=int(row["token_output"] or 0),
        worktree_path=str(row["worktree_path"] or ""),
        worktree_branch=str(row["worktree_branch"] or ""),
        is_force_parallel=bool(row["is_force_parallel"]),
    )


def _event(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        event_type=str(row["event_type"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=_dt(str(row["created_at"])),
    )


def _approval(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        codex_request_id=str(row["codex_request_id"]),
        codex_item_id=row["codex_item_id"],
        codex_turn_id=row["codex_turn_id"],
        kind=ApprovalKind(str(row["kind"])),
        summary=str(row["summary"]),
        command_json=str(row["command_json"]),
        status=ApprovalStatus(str(row["status"])),
        telegram_message_id=row["telegram_message_id"],
        resolution=row["resolution"],
        created_at=_dt(str(row["created_at"])),
        resolved_at=_dt(str(row["resolved_at"])) if row["resolved_at"] else None,
    )


def _touched(row: sqlite3.Row) -> TouchedFile:
    return TouchedFile(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        path=str(row["path"]),
        change_kind=str(row["change_kind"]),
        created_at=_dt(str(row["created_at"])),
    )


def _backend_request(row: sqlite3.Row) -> BackendRequest:
    return BackendRequest(
        id=int(row["id"]),
        jsonrpc_id=int(row["jsonrpc_id"]),
        method=str(row["method"]),
        task_id=row["task_id"] and int(row["task_id"]),
        status=BackendRequestStatus(str(row["status"])),
        created_at=_dt(str(row["created_at"])),
        completed_at=_dt(str(row["completed_at"])) if row["completed_at"] else None,
        error=str(row["error"]) if row["error"] else None,
    )
