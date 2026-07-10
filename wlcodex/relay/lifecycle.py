from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from wlcodex.relay.models import RELAY_ROLE_IDS, normalize_relay_execution_mode


ROUND_TERMINAL_STATUSES = {
    "waiting_user",
    "blocked",
    "failed",
    "completed",
    "interrupted",
    "superseded",
}
ATTEMPT_TERMINAL_STATUSES = {
    "passed",
    "waiting",
    "blocked",
    "failed",
    "interrupted",
    "superseded",
}
RESULT_ARTIFACT_TYPES = {
    "routing_decision",
    "architecture_plan",
    "implementation_report",
    "test_report",
    "audit_report",
    "final_summary",
    "followup_response",
}
LEGACY_ROUND_FAILURE_STATUSES = {"blocked", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_round_id(value: Any) -> int:
    try:
        round_id = int(value)
    except (TypeError, ValueError):
        return 0
    return round_id if round_id > 0 else 0


def _legacy_projection_status(
    round_status: str,
    attempt: RelayLifecycleAttempt | None,
) -> str:
    if attempt is None or attempt.status == "superseded":
        return "idle"
    status = attempt.status
    if (
        round_status in LEGACY_ROUND_FAILURE_STATUSES
        and status not in ATTEMPT_TERMINAL_STATUSES
    ):
        return round_status
    return status


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _completion_event_key(runtime_event_id: int, agent_run_id: int | None) -> str:
    """Build the immutable replay key for a completion projection."""

    if agent_run_id is not None and int(agent_run_id) > 0:
        return f"agent:{int(agent_run_id)}"
    if int(runtime_event_id or 0) > 0:
        return f"runtime:{int(runtime_event_id)}"
    return ""


def _clean_required_roles(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value if str(item).strip() in RELAY_ROLE_IDS]


@dataclass(frozen=True)
class RelayLifecycleAttempt:
    team_run_id: int
    round_id: int
    role: str
    attempt_no: int
    status: str
    provider: str = ""
    native_session_id: str = ""
    agent_run_id: int | None = None
    turn_id: str = ""
    active_turn_id: str = ""
    dispatch_artifact_id: int | None = None
    completion_event_id: int | None = None
    completion_artifact_id: int | None = None
    error_artifact_id: int | None = None
    retry_count: int = 0
    execution_mode: str = "standard"
    team_strategy: str = "none"
    provider_mode: dict[str, Any] | None = None
    provider_child_activity: dict[str, Any] | None = None


@dataclass(frozen=True)
class RelayCompletionEventClaim:
    """Durable ownership of one provider completion projection.

    The original round is retained for audit, but the replay key is the
    immutable provider event or agent run.  A completion can advance a task to
    the next round before its final claim write, so current-round identity is
    unsafe for idempotency.
    """

    team_run_id: int
    event_key: str
    runtime_event_id: int
    agent_run_id: int | None
    role: str
    claimed_round_id: int
    acquired: bool
    already_applied: bool = False
    recovered: bool = False


class RelayLifecycleStore:
    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._conn = ledger._conn
        self._backfilling_task_ids: set[int] = set()

    def backfill_all_relay_tasks(self) -> None:
        rows = self._conn.execute(
            "SELECT id FROM team_runs WHERE route = 'relay' ORDER BY id ASC"
        ).fetchall()
        for row in rows:
            self.backfill_task(int(row["id"]))

    def backfill_task(self, team_run_id: int) -> None:
        if team_run_id in self._backfilling_task_ids:
            return
        artifacts = self._artifact_rows(team_run_id)
        self._backfilling_task_ids.add(team_run_id)
        try:
            if self._has_rounds(team_run_id):
                self._repair_task_from_artifacts(team_run_id, artifacts)
                return
            round_ids = sorted(
                {
                    _coerce_round_id(artifact["payload"].get("round_id")) or 1
                    for artifact in artifacts
                }
                or {1}
            )
            if not round_ids:
                round_ids = [1]
            for round_id in round_ids:
                self.ensure_round(
                    team_run_id,
                    round_id=round_id,
                    trigger_kind="backfill" if round_id > 1 else "initial",
                )
            current_round_id = max(round_ids)
            team_run = self._ledger.get_team_run(team_run_id)
            current_status = str(getattr(team_run, "status", "") or "running")
            self._supersede_prior_rounds(team_run_id, current_round_id)
            self.set_round_status(
                team_run_id,
                current_round_id,
                current_status,
                preserve_activity=True,
            )
            for job in self._ledger.list_team_agent_jobs(team_run_id):
                status = str(job.status or "idle")
                if status == "idle":
                    continue
                self.ensure_attempt(
                    team_run_id,
                    round_id=current_round_id,
                    role=job.role,
                    status=status,
                    agent_run_id=job.agent_run_id,
                )
            for artifact in artifacts:
                self.observe_artifact(team_run_id, artifact["id"], artifact["payload"])
            self.sync_legacy_projection(team_run_id, preserve_activity=True)
        finally:
            self._backfilling_task_ids.discard(team_run_id)

    def ensure_round(
        self,
        team_run_id: int,
        *,
        round_id: int,
        trigger_kind: str,
        trigger_artifact_id: int | None = None,
        status: str = "running",
    ) -> None:
        now = _now()
        self._conn.execute(
            """
            INSERT INTO relay_rounds (
                team_run_id, round_id, status, trigger_kind, trigger_artifact_id,
                route, required_roles_json, created_at, updated_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, '', '[]', ?, ?, NULL)
            ON CONFLICT(team_run_id, round_id) DO UPDATE SET
                trigger_kind = CASE
                    WHEN relay_rounds.trigger_kind = '' THEN excluded.trigger_kind
                    ELSE relay_rounds.trigger_kind
                END,
                trigger_artifact_id = COALESCE(
                    relay_rounds.trigger_artifact_id,
                    excluded.trigger_artifact_id
                ),
                updated_at = excluded.updated_at
            """,
            (
                team_run_id,
                round_id,
                status,
                trigger_kind,
                trigger_artifact_id,
                now,
                now,
            ),
        )
        self._conn.commit()

    def create_initial_round(self, team_run_id: int) -> None:
        self.ensure_round(team_run_id, round_id=1, trigger_kind="initial")
        self.ensure_attempt(team_run_id, round_id=1, role="director", status="queued")
        self.sync_legacy_projection(team_run_id)

    def start_followup_round(
        self,
        team_run_id: int,
        *,
        trigger_artifact_id: int | None = None,
    ) -> int:
        current = self.current_round_id(team_run_id)
        current_execution = self.round_execution(team_run_id, current)
        next_round = current + 1
        now = _now()
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET status = 'superseded', updated_at = ?, closed_at = COALESCE(closed_at, ?)
            WHERE team_run_id = ?
              AND round_id = ?
              AND status NOT IN ('completed', 'failed', 'interrupted', 'superseded')
            """,
            (now, now, team_run_id, current),
        )
        self._conn.execute(
            """
            UPDATE relay_role_attempts
            SET status = 'superseded', updated_at = ?, closed_at = COALESCE(closed_at, ?)
            WHERE team_run_id = ?
              AND round_id = ?
              AND status NOT IN ('passed', 'failed', 'interrupted', 'superseded')
            """,
            (now, now, team_run_id, current),
        )
        self._conn.commit()
        self.ensure_round(
            team_run_id,
            round_id=next_round,
            trigger_kind="user_followup",
            trigger_artifact_id=trigger_artifact_id,
        )
        self.set_round_execution(
            team_run_id,
            next_round,
            execution_mode=normalize_relay_execution_mode(
                current_execution.get("execution_mode")
            ),
            execution_goal=str(current_execution.get("execution_goal") or ""),
            execution_strategy=dict(current_execution.get("execution_strategy") or {}),
            waiting_reason=str(current_execution.get("waiting_reason") or "none"),
        )
        self.ensure_attempt(
            team_run_id,
            round_id=next_round,
            role="director",
            status="queued",
        )
        self.set_attempt_execution(
            team_run_id,
            next_round,
            "director",
            execution_mode=normalize_relay_execution_mode(
                current_execution.get("execution_mode")
            ),
        )
        self.sync_legacy_projection(team_run_id)
        return next_round

    def current_round_id(self, team_run_id: int, *, fallback: int = 1) -> int:
        self.backfill_task(team_run_id)
        return self._active_round_id(team_run_id, fallback=fallback)

    def current_round_id_readonly(self, team_run_id: int, *, fallback: int = 1) -> int:
        """Return the latest durable round without triggering legacy repair.

        Detail pages and SSE snapshots must be projections only.  Lifecycle
        repair remains available through :meth:`current_round_id` for explicit
        workers and mutation paths, while this variant is safe for a request
        that is merely rendering state.
        """

        return self._active_round_id(team_run_id, fallback=fallback)

    def _active_round_id(self, team_run_id: int, *, fallback: int = 1) -> int:
        row = self._conn.execute(
            """
            SELECT round_id FROM relay_rounds
            WHERE team_run_id = ?
              AND status != 'superseded'
            ORDER BY round_id DESC
            LIMIT 1
            """,
            (team_run_id,),
        ).fetchone()
        if row is not None:
            return int(row["round_id"])
        row = self._conn.execute(
            """
            SELECT round_id FROM relay_rounds
            WHERE team_run_id = ?
            ORDER BY round_id DESC
            LIMIT 1
            """,
            (team_run_id,),
        ).fetchone()
        return int(row["round_id"]) if row is not None else max(1, int(fallback or 1))

    def round_status(self, team_run_id: int, round_id: int) -> str:
        row = self._conn.execute(
            """
            SELECT status FROM relay_rounds
            WHERE team_run_id = ? AND round_id = ?
            """,
            (team_run_id, round_id),
        ).fetchone()
        return str(row["status"]) if row is not None else "running"

    def set_round_status(
        self,
        team_run_id: int,
        round_id: int,
        status: str,
        *,
        preserve_activity: bool = False,
    ) -> None:
        self.ensure_round(team_run_id, round_id=round_id, trigger_kind="backfill")
        now = _now()
        closed_at = now if status in ROUND_TERMINAL_STATUSES else None
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE team_run_id = ? AND round_id = ?
            """,
            (status, now, closed_at, team_run_id, round_id),
        )
        self._conn.commit()
        self.sync_legacy_projection(team_run_id, preserve_activity=preserve_activity)

    def set_round_route(
        self,
        team_run_id: int,
        round_id: int,
        *,
        route: str,
        required_roles: list[str],
    ) -> None:
        self.ensure_round(team_run_id, round_id=round_id, trigger_kind="backfill")
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET route = ?, required_roles_json = ?, updated_at = ?
            WHERE team_run_id = ? AND round_id = ?
            """,
            (
                route,
                json.dumps(required_roles, ensure_ascii=False),
                _now(),
                team_run_id,
                round_id,
            ),
        )
        self._conn.commit()

    def set_round_execution(
        self,
        team_run_id: int,
        round_id: int,
        *,
        execution_mode: str = "standard",
        execution_goal: str = "",
        execution_strategy: dict[str, Any] | None = None,
        waiting_reason: str = "none",
    ) -> None:
        self.ensure_round(team_run_id, round_id=round_id, trigger_kind="backfill")
        mode = normalize_relay_execution_mode(execution_mode)
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET execution_mode = ?,
                execution_goal = ?,
                execution_strategy_json = ?,
                waiting_reason = ?,
                updated_at = ?
            WHERE team_run_id = ? AND round_id = ?
            """,
            (
                mode,
                str(execution_goal or ""),
                json.dumps(execution_strategy or {}, ensure_ascii=False),
                str(waiting_reason or "none") or "none",
                _now(),
                team_run_id,
                round_id,
            ),
        )
        self._conn.commit()

    def set_round_confirmation(
        self,
        team_run_id: int,
        round_id: int,
        *,
        source: str,
        kind: str,
        role: str,
        provider: str = "",
        provider_request_id: str = "",
        runtime_event_id: int = 0,
        native_session_id: str = "",
        agent_run_id: int | None = None,
        turn_id: str = "",
    ) -> None:
        self.ensure_round(team_run_id, round_id=round_id, trigger_kind="backfill")
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET confirmation_source = ?,
                confirmation_kind = ?,
                confirmation_role = ?,
                confirmation_provider = ?,
                confirmation_provider_request_id = ?,
                confirmation_runtime_event_id = ?,
                confirmation_native_session_id = ?,
                confirmation_agent_run_id = ?,
                confirmation_turn_id = ?,
                updated_at = ?
            WHERE team_run_id = ? AND round_id = ?
            """,
            (
                str(source or ""),
                str(kind or ""),
                str(role or ""),
                str(provider or ""),
                str(provider_request_id or ""),
                int(runtime_event_id or 0),
                str(native_session_id or ""),
                int(agent_run_id) if agent_run_id is not None else None,
                str(turn_id or ""),
                _now(),
                team_run_id,
                round_id,
            ),
        )
        self._conn.commit()

    def clear_round_confirmation(self, team_run_id: int, round_id: int) -> None:
        self.ensure_round(team_run_id, round_id=round_id, trigger_kind="backfill")
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET confirmation_source = '',
                confirmation_kind = '',
                confirmation_role = '',
                confirmation_provider = '',
                confirmation_provider_request_id = '',
                confirmation_runtime_event_id = 0,
                confirmation_native_session_id = '',
                confirmation_agent_run_id = NULL,
                confirmation_turn_id = '',
                updated_at = ?
            WHERE team_run_id = ? AND round_id = ?
            """,
            (_now(), team_run_id, round_id),
        )
        self._conn.commit()

    def round_execution(self, team_run_id: int, round_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT execution_mode, execution_goal, execution_strategy_json, waiting_reason,
                   confirmation_source, confirmation_kind, confirmation_role,
                   confirmation_provider, confirmation_provider_request_id,
                   confirmation_runtime_event_id, confirmation_native_session_id,
                   confirmation_agent_run_id, confirmation_turn_id
            FROM relay_rounds
            WHERE team_run_id = ? AND round_id = ?
            """,
            (team_run_id, round_id),
        ).fetchone()
        if row is None:
            return {
                "execution_mode": "standard",
                "execution_goal": "",
                "execution_strategy": {},
                "waiting_reason": "none",
                "confirmation": {
                    "source": "",
                    "kind": "",
                    "role": "",
                    "provider": "",
                    "provider_request_id": "",
                    "runtime_event_id": 0,
                    "native_session_id": "",
                    "agent_run_id": None,
                    "turn_id": "",
                },
                "pending_approval_count": 0,
            }
        try:
            strategy = json.loads(str(row["execution_strategy_json"] or "{}"))
        except json.JSONDecodeError:
            strategy = {}
        return {
            "execution_mode": normalize_relay_execution_mode(row["execution_mode"]),
            "execution_goal": str(row["execution_goal"] or ""),
            "execution_strategy": strategy if isinstance(strategy, dict) else {},
            "waiting_reason": str(row["waiting_reason"] or "none"),
            "confirmation": {
                "source": str(row["confirmation_source"] or ""),
                "kind": str(row["confirmation_kind"] or ""),
                "role": str(row["confirmation_role"] or ""),
                "provider": str(row["confirmation_provider"] or ""),
                "provider_request_id": str(row["confirmation_provider_request_id"] or ""),
                "runtime_event_id": int(row["confirmation_runtime_event_id"] or 0),
                "native_session_id": str(row["confirmation_native_session_id"] or ""),
                "agent_run_id": (
                    int(row["confirmation_agent_run_id"])
                    if row["confirmation_agent_run_id"] is not None
                    else None
                ),
                "turn_id": str(row["confirmation_turn_id"] or ""),
            },
            # Relay owns its native provider confirmations directly instead
            # of mirroring them into the unrelated legacy ``tasks`` table.
            # There can be exactly one current confirmation per round, so this
            # is the durable pending-count projection exposed to callers.
            "pending_approval_count": (
                1 if str(row["confirmation_source"] or "") else 0
            ),
        }

    def claim_provider_native_approval_action(
        self,
        team_run_id: int,
        round_id: int,
        *,
        provider_request_id: str,
        runtime_event_id: int,
        action: str,
    ) -> bool:
        """Atomically reserve one native approval for an external action.

        Provider resolution cannot participate in SQLite's transaction.  This
        claim closes that gap: only the current confirmation can transition to
        a short-lived ``provider_native_*`` state, so a stale button or a
        concurrent supersede never sends a second provider action.
        """

        action = str(action or "").strip()
        if action not in {"resolving", "superseding"}:
            raise ValueError(f"unsupported native approval action: {action}")
        request_id = str(provider_request_id or "").strip()
        event_id = int(runtime_event_id or 0)
        if not request_id or event_id <= 0:
            return False
        now = _now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """
                UPDATE relay_rounds
                SET confirmation_source = ?, updated_at = ?
                WHERE team_run_id = ?
                  AND round_id = ?
                  AND confirmation_source = 'provider_native_approval'
                  AND confirmation_provider_request_id = ?
                  AND confirmation_runtime_event_id = ?
                """,
                (
                    f"provider_native_{action}",
                    now,
                    team_run_id,
                    round_id,
                    request_id,
                    event_id,
                ),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) == 1
        except Exception:
            self._conn.rollback()
            raise

    def restore_provider_native_approval_action(
        self,
        team_run_id: int,
        round_id: int,
        *,
        provider_request_id: str,
        runtime_event_id: int,
        action: str,
    ) -> bool:
        """Return a failed provider action to the actionable approval state."""

        action = str(action or "").strip()
        request_id = str(provider_request_id or "").strip()
        event_id = int(runtime_event_id or 0)
        if action not in {"resolving", "superseding"} or not request_id or event_id <= 0:
            return False
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """
                UPDATE relay_rounds
                SET confirmation_source = 'provider_native_approval', updated_at = ?
                WHERE team_run_id = ?
                  AND round_id = ?
                  AND confirmation_source = ?
                  AND confirmation_provider_request_id = ?
                  AND confirmation_runtime_event_id = ?
                """,
                (
                    _now(),
                    team_run_id,
                    round_id,
                    f"provider_native_{action}",
                    request_id,
                    event_id,
                ),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) == 1
        except Exception:
            self._conn.rollback()
            raise

    def replace_provider_native_approval(
        self,
        team_run_id: int,
        round_id: int,
        *,
        role: str,
        provider: str,
        provider_request_id: str,
        runtime_event_id: int,
        native_session_id: str = "",
        agent_run_id: int | None = None,
        turn_id: str = "",
        kind: str = "command_approval",
        expected_previous_request_id: str = "",
        expected_previous_runtime_event_id: int = 0,
    ) -> dict[str, Any]:
        """Set the sole current native approval and its Relay projections.

        This is deliberately one ``BEGIN IMMEDIATE`` transaction.  The round,
        latest role attempt, team-run projection, and legacy role-job
        projection therefore cannot expose a half-replaced confirmation.
        ``expected_previous_*`` protects the provider-cancel supersede saga
        from overwriting a newer confirmation observed by another worker.
        """

        request_id = str(provider_request_id or "").strip()
        event_id = int(runtime_event_id or 0)
        if not request_id or event_id <= 0:
            return {"applied": False, "reason": "missing_request_identity"}
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                """
                SELECT confirmation_source, confirmation_kind, confirmation_role,
                       confirmation_provider, confirmation_provider_request_id,
                       confirmation_runtime_event_id, confirmation_native_session_id,
                       confirmation_agent_run_id, confirmation_turn_id
                FROM relay_rounds
                WHERE team_run_id = ? AND round_id = ?
                """,
                (team_run_id, round_id),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return {"applied": False, "reason": "round_missing"}
            previous = {
                "source": str(row["confirmation_source"] or ""),
                "kind": str(row["confirmation_kind"] or ""),
                "role": str(row["confirmation_role"] or ""),
                "provider": str(row["confirmation_provider"] or ""),
                "provider_request_id": str(
                    row["confirmation_provider_request_id"] or ""
                ),
                "runtime_event_id": int(row["confirmation_runtime_event_id"] or 0),
                "native_session_id": str(row["confirmation_native_session_id"] or ""),
                "agent_run_id": (
                    int(row["confirmation_agent_run_id"])
                    if row["confirmation_agent_run_id"] is not None
                    else None
                ),
                "turn_id": str(row["confirmation_turn_id"] or ""),
            }
            if (
                previous["source"] == "provider_native_approval"
                and previous["provider_request_id"] == request_id
                and previous["runtime_event_id"] == event_id
            ):
                self._conn.commit()
                return {
                    "applied": False,
                    "duplicate": True,
                    "previous": previous,
                    "pending_approval_count": 1,
                }
            if previous["runtime_event_id"] > event_id:
                self._conn.commit()
                return {
                    "applied": False,
                    "stale": True,
                    "previous": previous,
                    "pending_approval_count": 1 if previous["source"] else 0,
                }
            if expected_previous_request_id and (
                previous["provider_request_id"] != str(expected_previous_request_id)
                or previous["runtime_event_id"]
                != int(expected_previous_runtime_event_id or 0)
                or previous["source"] != "provider_native_superseding"
            ):
                self._conn.commit()
                return {
                    "applied": False,
                    "stale": True,
                    "previous": previous,
                    "pending_approval_count": 1 if previous["source"] else 0,
                }
            now = _now()
            self._conn.execute(
                """
                UPDATE relay_rounds
                SET status = 'waiting_user',
                    closed_at = ?,
                    waiting_reason = 'provider_approval',
                    confirmation_source = 'provider_native_approval',
                    confirmation_kind = ?,
                    confirmation_role = ?,
                    confirmation_provider = ?,
                    confirmation_provider_request_id = ?,
                    confirmation_runtime_event_id = ?,
                    confirmation_native_session_id = ?,
                    confirmation_agent_run_id = ?,
                    confirmation_turn_id = ?,
                    updated_at = ?
                WHERE team_run_id = ? AND round_id = ?
                """,
                (
                    now,
                    str(kind or "command_approval"),
                    str(role or "director"),
                    str(provider or ""),
                    request_id,
                    event_id,
                    str(native_session_id or ""),
                    int(agent_run_id) if agent_run_id is not None else None,
                    str(turn_id or ""),
                    now,
                    team_run_id,
                    round_id,
                ),
            )
            attempt = self._latest_attempt_row(team_run_id, round_id, role)
            if attempt is None:
                self._conn.execute(
                    """
                    INSERT INTO relay_role_attempts (
                        team_run_id, round_id, role, attempt_no, status,
                        provider, native_session_id, agent_run_id, turn_id,
                        active_turn_id, created_at, updated_at, closed_at
                    ) VALUES (?, ?, ?, 1, 'waiting', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        team_run_id,
                        round_id,
                        str(role or "director"),
                        str(provider or ""),
                        str(native_session_id or ""),
                        int(agent_run_id) if agent_run_id is not None else None,
                        str(turn_id or ""),
                        str(turn_id or ""),
                        now,
                        now,
                        now,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE relay_role_attempts
                    SET status = 'waiting', updated_at = ?, closed_at = ?
                    WHERE id = ?
                    """,
                    (now, now, int(attempt["id"])),
                )
            self._conn.execute(
                "UPDATE team_runs SET status = ?, updated_at = ? WHERE id = ?",
                ("waiting_user", now, team_run_id),
            )
            self._conn.execute(
                """
                UPDATE team_agent_jobs
                SET status = ?, updated_at = ?
                WHERE team_run_id = ? AND role = ?
                """,
                ("waiting", now, team_run_id, str(role or "director")),
            )
            self._conn.commit()
            return {
                "applied": True,
                "previous": previous,
                "pending_approval_count": 1,
            }
        except Exception:
            self._conn.rollback()
            raise

    def finalize_provider_native_approval_action(
        self,
        team_run_id: int,
        round_id: int,
        *,
        role: str,
        provider_request_id: str,
        runtime_event_id: int,
        action: str,
        decision: str,
    ) -> bool:
        """Commit a successful provider response with all Relay projections."""

        if str(action or "") not in {"resolving"}:
            raise ValueError(f"unsupported native approval finalization: {action}")
        request_id = str(provider_request_id or "").strip()
        event_id = int(runtime_event_id or 0)
        if not request_id or event_id <= 0:
            return False
        interrupted = str(decision or "") == "cancel_plan"
        next_round_status = "interrupted" if interrupted else "running"
        next_role_status = "interrupted" if interrupted else "streaming"
        now = _now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """
                UPDATE relay_rounds
                SET status = ?,
                    closed_at = ?,
                    waiting_reason = 'none',
                    confirmation_source = '',
                    confirmation_kind = '',
                    confirmation_role = '',
                    confirmation_provider = '',
                    confirmation_provider_request_id = '',
                    confirmation_runtime_event_id = 0,
                    confirmation_native_session_id = '',
                    confirmation_agent_run_id = NULL,
                    confirmation_turn_id = '',
                    updated_at = ?
                WHERE team_run_id = ?
                  AND round_id = ?
                  AND confirmation_source = 'provider_native_resolving'
                  AND confirmation_provider_request_id = ?
                  AND confirmation_runtime_event_id = ?
                """,
                (
                    next_round_status,
                    now if interrupted else None,
                    now,
                    team_run_id,
                    round_id,
                    request_id,
                    event_id,
                ),
            )
            if int(cur.rowcount or 0) != 1:
                self._conn.commit()
                return False
            attempt = self._latest_attempt_row(team_run_id, round_id, role)
            if attempt is not None:
                self._conn.execute(
                    """
                    UPDATE relay_role_attempts
                    SET status = ?, updated_at = ?, closed_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_role_status,
                        now,
                        now if interrupted else None,
                        int(attempt["id"]),
                    ),
                )
            self._conn.execute(
                "UPDATE team_runs SET status = ?, updated_at = ? WHERE id = ?",
                (next_round_status, now, team_run_id),
            )
            self._conn.execute(
                """
                UPDATE team_agent_jobs
                SET status = ?, updated_at = ?
                WHERE team_run_id = ? AND role = ?
                """,
                (next_role_status, now, team_run_id, str(role or "director")),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def claim_completion_event_result(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        runtime_event_id: int,
        *,
        agent_run_id: int | None = None,
        lease_seconds: int = 900,
    ) -> RelayCompletionEventClaim:
        """Atomically claim one immutable completion identity.

        ``relay_completion_claims`` used the mutable current round as part of
        its primary key.  This table intentionally uses only an immutable
        runtime event / agent run identity, with the original round stored as
        audit metadata.
        """

        event_id = max(0, int(runtime_event_id or 0))
        run_id = _coerce_optional_int(agent_run_id)
        if run_id is not None and run_id <= 0:
            run_id = None
        event_key = _completion_event_key(event_id, run_id)
        clean_role = str(role or "director")
        claimed_round_id = max(1, int(round_id or 1))
        if not event_key:
            return RelayCompletionEventClaim(
                team_run_id=team_run_id,
                event_key="",
                runtime_event_id=event_id,
                agent_run_id=run_id,
                role=clean_role,
                claimed_round_id=claimed_round_id,
                acquired=True,
            )

        now = _now()
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat()
        owns_transaction = not self._conn.in_transaction
        if owns_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                """
                SELECT *
                FROM relay_completion_event_claims
                WHERE team_run_id = ?
                  AND (
                    event_key = ?
                    OR (? > 0 AND runtime_event_id = ?)
                    OR (? IS NOT NULL AND ? > 0 AND agent_run_id = ?)
                  )
                ORDER BY CASE WHEN event_key = ? THEN 0 ELSE 1 END, claimed_at ASC
                LIMIT 1
                """,
                (
                    team_run_id,
                    event_key,
                    event_id,
                    event_id,
                    run_id,
                    run_id,
                    run_id,
                    event_key,
                ),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO relay_completion_event_claims (
                        team_run_id, event_key, runtime_event_id, agent_run_id,
                        role, claimed_round_id, status, artifact_id, claimed_at,
                        applied_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'claimed', NULL, ?, NULL, ?)
                    """,
                    (
                        team_run_id,
                        event_key,
                        event_id,
                        run_id,
                        clean_role,
                        claimed_round_id,
                        now,
                        now,
                    ),
                )
                result = RelayCompletionEventClaim(
                    team_run_id=team_run_id,
                    event_key=event_key,
                    runtime_event_id=event_id,
                    agent_run_id=run_id,
                    role=clean_role,
                    claimed_round_id=claimed_round_id,
                    acquired=True,
                )
            else:
                stored_key = str(row["event_key"] or event_key)
                stored_event_id = max(0, int(row["runtime_event_id"] or 0))
                stored_run_id = _coerce_optional_int(row["agent_run_id"])
                stored_role = str(row["role"] or clean_role)
                stored_round_id = max(1, int(row["claimed_round_id"] or claimed_round_id))
                status = str(row["status"] or "claimed")
                if status in {"applied", "ignored_stale"}:
                    result = RelayCompletionEventClaim(
                        team_run_id=team_run_id,
                        event_key=stored_key,
                        runtime_event_id=stored_event_id or event_id,
                        agent_run_id=stored_run_id or run_id,
                        role=stored_role,
                        claimed_round_id=stored_round_id,
                        acquired=False,
                        already_applied=True,
                    )
                elif str(row["updated_at"] or "") >= cutoff:
                    result = RelayCompletionEventClaim(
                        team_run_id=team_run_id,
                        event_key=stored_key,
                        runtime_event_id=stored_event_id or event_id,
                        agent_run_id=stored_run_id or run_id,
                        role=stored_role,
                        claimed_round_id=stored_round_id,
                        acquired=False,
                    )
                else:
                    # Preserve the original round/key/role.  Only fill a
                    # missing agent run for a pre-migration seed row.
                    self._conn.execute(
                        """
                        UPDATE relay_completion_event_claims
                        SET runtime_event_id = CASE
                                WHEN runtime_event_id > 0 THEN runtime_event_id
                                ELSE ?
                            END,
                            agent_run_id = COALESCE(agent_run_id, ?),
                            status = 'claimed',
                            claimed_at = ?,
                            updated_at = ?
                        WHERE team_run_id = ? AND event_key = ?
                        """,
                        (event_id, run_id, now, now, team_run_id, stored_key),
                    )
                    result = RelayCompletionEventClaim(
                        team_run_id=team_run_id,
                        event_key=stored_key,
                        runtime_event_id=stored_event_id or event_id,
                        agent_run_id=stored_run_id or run_id,
                        role=stored_role,
                        claimed_round_id=stored_round_id,
                        acquired=True,
                        recovered=True,
                    )
            if owns_transaction:
                self._conn.commit()
            return result
        except Exception:
            if owns_transaction:
                self._conn.rollback()
            raise

    def claim_completion_event(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        runtime_event_id: int,
        *,
        agent_run_id: int | None = None,
        lease_seconds: int = 900,
    ) -> bool:
        """Compatibility wrapper returning only whether this worker owns it."""

        return self.claim_completion_event_result(
            team_run_id,
            round_id,
            role,
            runtime_event_id,
            agent_run_id=agent_run_id,
            lease_seconds=lease_seconds,
        ).acquired

    def mark_completion_event_applied(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        runtime_event_id: int,
        *,
        agent_run_id: int | None = None,
        artifact_id: int | None = None,
    ) -> None:
        """Finalize a completion claim after its domain projection is durable."""

        del round_id, role
        event_id = max(0, int(runtime_event_id or 0))
        run_id = _coerce_optional_int(agent_run_id)
        event_key = _completion_event_key(event_id, run_id)
        if not event_key:
            return
        now = _now()
        self._conn.execute(
            """
            UPDATE relay_completion_event_claims
            SET status = 'applied',
                artifact_id = COALESCE(?, artifact_id),
                applied_at = ?,
                updated_at = ?
            WHERE team_run_id = ?
              AND (
                event_key = ?
                OR (? > 0 AND runtime_event_id = ?)
                OR (? IS NOT NULL AND ? > 0 AND agent_run_id = ?)
              )
            """,
            (
                artifact_id,
                now,
                now,
                team_run_id,
                event_key,
                event_id,
                event_id,
                run_id,
                run_id,
                run_id,
            ),
        )
        self._conn.commit()

    def mark_completion_event_ignored_stale(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        runtime_event_id: int,
        *,
        agent_run_id: int | None = None,
    ) -> None:
        """Finalize a late completion without applying it to a newer round.

        This is intentionally distinct from ``applied``: the provider event
        was observed and safely retired, but it did not produce a Relay domain
        artifact because its originating attempt is no longer current.
        """

        del round_id, role
        event_id = max(0, int(runtime_event_id or 0))
        run_id = _coerce_optional_int(agent_run_id)
        event_key = _completion_event_key(event_id, run_id)
        if not event_key:
            return
        now = _now()
        self._conn.execute(
            """
            UPDATE relay_completion_event_claims
            SET status = 'ignored_stale',
                applied_at = COALESCE(applied_at, ?),
                updated_at = ?
            WHERE team_run_id = ?
              AND status = 'claimed'
              AND (
                event_key = ?
                OR (? > 0 AND runtime_event_id = ?)
                OR (? IS NOT NULL AND ? > 0 AND agent_run_id = ?)
              )
            """,
            (
                now,
                now,
                team_run_id,
                event_key,
                event_id,
                event_id,
                run_id,
                run_id,
                run_id,
            ),
        )
        self._conn.commit()

    def release_completion_event_claim(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        runtime_event_id: int,
        *,
        agent_run_id: int | None = None,
    ) -> None:
        """Release an unprojectable event so a later durable delta can retry."""

        del round_id, role
        event_id = max(0, int(runtime_event_id or 0))
        run_id = _coerce_optional_int(agent_run_id)
        event_key = _completion_event_key(event_id, run_id)
        if not event_key:
            return
        self._conn.execute(
            """
            DELETE FROM relay_completion_event_claims
            WHERE team_run_id = ?
              AND status = 'claimed'
              AND (
                event_key = ?
                OR (? > 0 AND runtime_event_id = ?)
                OR (? IS NOT NULL AND ? > 0 AND agent_run_id = ?)
              )
            """,
            (
                team_run_id,
                event_key,
                event_id,
                event_id,
                run_id,
                run_id,
                run_id,
            ),
        )
        self._conn.commit()

    def completion_event_applied(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        runtime_event_id: int,
        *,
        agent_run_id: int | None = None,
    ) -> bool:
        del round_id, role
        event_id = max(0, int(runtime_event_id or 0))
        run_id = _coerce_optional_int(agent_run_id)
        event_key = _completion_event_key(event_id, run_id)
        if not event_key:
            return False
        row = self._conn.execute(
            """
            SELECT 1 FROM relay_completion_event_claims
            WHERE team_run_id = ?
              AND status = 'applied'
              AND (
                event_key = ?
                OR (? > 0 AND runtime_event_id = ?)
                OR (? IS NOT NULL AND ? > 0 AND agent_run_id = ?)
              )
            LIMIT 1
            """,
            (
                team_run_id,
                event_key,
                event_id,
                event_id,
                run_id,
                run_id,
                run_id,
            ),
        ).fetchone()
        return row is not None

    def claim_queued_attempt_dispatch(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        *,
        recover_stale_claim: bool = False,
    ) -> bool:
        """Compare-and-set a queued Relay attempt before provider dispatch.

        Pending-input leases guard the queue record; this second claim guards
        the provider-facing side effect if a worker expires or crashes after
        creating the follow-up round.  A recovery can retake ``dispatching``
        only when no provider identity was ever persisted.
        """

        owns_transaction = not self._conn.in_transaction
        if owns_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._latest_attempt_row(team_run_id, round_id, role)
            if row is None:
                if owns_transaction:
                    self._conn.commit()
                return False
            status = str(row["status"] or "")
            has_provider_identity = bool(
                str(row["native_session_id"] or "").strip()
                or _coerce_optional_int(row["agent_run_id"])
            )
            can_claim = status == "queued" or (
                recover_stale_claim
                and status == "dispatching"
                and not has_provider_identity
            )
            if not can_claim:
                if owns_transaction:
                    self._conn.commit()
                return False
            now = _now()
            updated = self._conn.execute(
                """
                UPDATE relay_role_attempts
                SET status = 'dispatching', updated_at = ?
                WHERE id = ?
                  AND status = ?
                """,
                (now, int(row["id"]), status),
            )
            if owns_transaction:
                self._conn.commit()
            return int(updated.rowcount or 0) == 1
        except Exception:
            if owns_transaction:
                self._conn.rollback()
            raise

    def attempt_has_provider_dispatch(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
    ) -> bool:
        """Whether a provider dispatch is durably visible for an attempt."""

        row = self._latest_attempt_row(team_run_id, round_id, role)
        if row is None:
            return False
        if str(row["native_session_id"] or "").strip():
            return True
        return bool(_coerce_optional_int(row["agent_run_id"]))

    def ensure_attempt(
        self,
        team_run_id: int,
        *,
        round_id: int,
        role: str,
        status: str,
        provider: str = "",
        native_session_id: str = "",
        agent_run_id: int | None = None,
        turn_id: str = "",
        active_turn_id: str = "",
        dispatch_artifact_id: int | None = None,
    ) -> RelayLifecycleAttempt:
        self.ensure_round(team_run_id, round_id=round_id, trigger_kind="backfill")
        row = self._latest_attempt_row(team_run_id, round_id, role)
        if row is None:
            attempt_no = 1
            now = _now()
            self._conn.execute(
                """
                INSERT INTO relay_role_attempts (
                    team_run_id, round_id, role, attempt_no, status, provider,
                    native_session_id, agent_run_id, turn_id, active_turn_id,
                    dispatch_artifact_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    team_run_id,
                    round_id,
                    role,
                    attempt_no,
                    status,
                    provider,
                    native_session_id,
                    agent_run_id,
                    turn_id,
                    active_turn_id,
                    dispatch_artifact_id,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return self.latest_attempt(team_run_id, round_id, role)
        self.update_attempt(
            team_run_id,
            round_id,
            role,
            status=status,
            provider=provider,
            native_session_id=native_session_id,
            agent_run_id=agent_run_id,
            turn_id=turn_id,
            active_turn_id=active_turn_id,
            dispatch_artifact_id=dispatch_artifact_id,
        )
        return self.latest_attempt(team_run_id, round_id, role)

    def update_attempt(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        *,
        status: str | None = None,
        provider: str = "",
        native_session_id: str = "",
        agent_run_id: int | None = None,
        turn_id: str = "",
        active_turn_id: str = "",
        dispatch_artifact_id: int | None = None,
        completion_event_id: int | None = None,
        completion_artifact_id: int | None = None,
        error_artifact_id: int | None = None,
        increment_retry: bool = False,
    ) -> None:
        row = self._latest_attempt_row(team_run_id, round_id, role)
        if row is None:
            self.ensure_attempt(
                team_run_id,
                round_id=round_id,
                role=role,
                status=status or "queued",
                provider=provider,
                native_session_id=native_session_id,
                agent_run_id=agent_run_id,
                turn_id=turn_id,
                active_turn_id=active_turn_id,
                dispatch_artifact_id=dispatch_artifact_id,
            )
            return
        current_status = str(row["status"] or "")
        next_status = status or current_status
        if current_status == "superseded":
            return
        if (
            status in {"queued", "streaming"}
            and current_status in ATTEMPT_TERMINAL_STATUSES
        ):
            attempt_no = int(row["attempt_no"] or 1) + 1
            now = _now()
            self._conn.execute(
                """
                INSERT INTO relay_role_attempts (
                    team_run_id, round_id, role, attempt_no, status, provider,
                    native_session_id, agent_run_id, turn_id, active_turn_id,
                    dispatch_artifact_id, completion_event_id, completion_artifact_id,
                    error_artifact_id, retry_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    team_run_id,
                    round_id,
                    role,
                    attempt_no,
                    status,
                    provider or str(row["provider"] or ""),
                    native_session_id or str(row["native_session_id"] or ""),
                    agent_run_id if agent_run_id is not None else row["agent_run_id"],
                    turn_id or str(row["turn_id"] or ""),
                    active_turn_id or str(row["active_turn_id"] or ""),
                    dispatch_artifact_id,
                    completion_event_id,
                    completion_artifact_id,
                    error_artifact_id,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return
        now = _now()
        closed_at = now if next_status in ATTEMPT_TERMINAL_STATUSES else None
        self._conn.execute(
            """
            UPDATE relay_role_attempts
            SET status = ?,
                provider = CASE WHEN ? != '' THEN ? ELSE provider END,
                native_session_id = CASE WHEN ? != '' THEN ? ELSE native_session_id END,
                agent_run_id = COALESCE(?, agent_run_id),
                turn_id = CASE WHEN ? != '' THEN ? ELSE turn_id END,
                active_turn_id = CASE WHEN ? != '' THEN ? ELSE active_turn_id END,
                dispatch_artifact_id = COALESCE(?, dispatch_artifact_id),
                completion_event_id = COALESCE(?, completion_event_id),
                completion_artifact_id = COALESCE(?, completion_artifact_id),
                error_artifact_id = COALESCE(?, error_artifact_id),
                retry_count = retry_count + ?,
                updated_at = ?,
                closed_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                provider,
                provider,
                native_session_id,
                native_session_id,
                agent_run_id,
                turn_id,
                turn_id,
                active_turn_id,
                active_turn_id,
                dispatch_artifact_id,
                completion_event_id,
                completion_artifact_id,
                error_artifact_id,
                1 if increment_retry else 0,
                now,
                closed_at,
                int(row["id"]),
            ),
        )
        self._conn.commit()

    def set_attempt_execution(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        *,
        execution_mode: str = "standard",
        team_strategy: str = "none",
        provider_mode: dict[str, Any] | None = None,
        provider_child_activity: dict[str, Any] | None = None,
    ) -> None:
        row = self._latest_attempt_row(team_run_id, round_id, role)
        if row is None:
            self.ensure_attempt(
                team_run_id,
                round_id=round_id,
                role=role,
                status="queued",
            )
            row = self._latest_attempt_row(team_run_id, round_id, role)
        if row is None:
            return
        self._conn.execute(
            """
            UPDATE relay_role_attempts
            SET execution_mode = ?,
                team_strategy = ?,
                provider_mode_json = ?,
                provider_child_activity_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                normalize_relay_execution_mode(execution_mode),
                str(team_strategy or "none") or "none",
                json.dumps(provider_mode or {}, ensure_ascii=False),
                json.dumps(provider_child_activity or {}, ensure_ascii=False),
                _now(),
                int(row["id"]),
            ),
        )
        self._conn.commit()

    def observe_artifact(
        self,
        team_run_id: int,
        artifact_id: int,
        payload: dict[str, Any],
    ) -> None:
        role = str(payload.get("relay_role") or payload.get("role") or "")
        round_id = _coerce_round_id(payload.get("round_id")) or self._latest_round_id(
            team_run_id,
            fallback=1,
        )
        artifact_type = str(payload.get("artifact_type") or "")
        if artifact_type == "routing_decision":
            self.set_round_route(
                team_run_id,
                round_id,
                route=str(payload.get("route") or ""),
                required_roles=_clean_required_roles(payload.get("required_roles")),
            )
        if str(payload.get("status") or "") == "waiting":
            self.set_round_confirmation(
                team_run_id,
                round_id,
                source=str(payload.get("confirmation_source") or "relay_prompt_fallback"),
                kind=str(payload.get("confirmation_kind") or "relay_question"),
                role=role,
                provider=str(payload.get("provider") or ""),
                provider_request_id=str(payload.get("provider_request_id") or ""),
                runtime_event_id=_coerce_optional_int(payload.get("runtime_event_id")) or 0,
                native_session_id=str(payload.get("native_session_id") or ""),
                agent_run_id=_coerce_optional_int(payload.get("agent_run_id")),
                turn_id=str(payload.get("turn_id") or ""),
            )
        if not role:
            return
        if artifact_type == "role_dispatch_metadata":
            provider_mode = payload.get("provider_mode")
            if not isinstance(provider_mode, dict):
                provider_mode = {}
            if self._attempt_artifact_link_exists(
                team_run_id,
                round_id,
                role,
                "dispatch_artifact_id",
                artifact_id,
            ):
                self.set_attempt_execution(
                    team_run_id,
                    round_id,
                    role,
                    execution_mode=normalize_relay_execution_mode(
                        provider_mode.get("execution_mode")
                    ),
                    team_strategy=str(provider_mode.get("team_strategy") or "none"),
                    provider_mode=provider_mode,
                )
                return
            round_status = self.round_status(team_run_id, round_id)
            row = self._latest_attempt_row(team_run_id, round_id, role)
            if round_status in {"blocked", "failed", "interrupted"}:
                status = round_status
            elif round_status == "superseded":
                status = "superseded"
            elif row is None or str(row["status"] or "") not in ATTEMPT_TERMINAL_STATUSES:
                status = "streaming"
            else:
                status = None
            initial_status = status or "streaming"
            if row is not None:
                initial_status = status or str(row["status"] or "streaming")
            self.ensure_attempt(
                team_run_id,
                round_id=round_id,
                role=role,
                status=initial_status,
                provider=str(payload.get("provider") or ""),
                native_session_id=str(payload.get("native_session_id") or ""),
                agent_run_id=(
                    int(payload["agent_run_id"])
                    if payload.get("agent_run_id") is not None
                    else None
                ),
                turn_id=str(payload.get("turn_id") or ""),
                active_turn_id=str(payload.get("active_turn_id") or ""),
                dispatch_artifact_id=artifact_id,
            )
            self.set_attempt_execution(
                team_run_id,
                round_id,
                role,
                execution_mode=normalize_relay_execution_mode(
                    provider_mode.get("execution_mode")
                ),
                team_strategy=str(provider_mode.get("team_strategy") or "none"),
                provider_mode=provider_mode,
            )
            if status is not None:
                self.update_attempt(team_run_id, round_id, role, status=status)
            return
        if artifact_type == "role_error":
            if self._attempt_artifact_link_exists(
                team_run_id,
                round_id,
                role,
                "error_artifact_id",
                artifact_id,
            ):
                return
            if self._latest_attempt_row(team_run_id, round_id, role) is None:
                self.ensure_attempt(team_run_id, round_id=round_id, role=role, status="streaming")
            row = self._latest_attempt_row(team_run_id, round_id, role)
            current_error_artifact_id = (
                int(row["error_artifact_id"])
                if row is not None and row["error_artifact_id"] is not None
                else 0
            )
            should_record_error = int(artifact_id) > current_error_artifact_id
            round_status = self.round_status(team_run_id, round_id)
            error_status = (
                round_status
                if round_status in {"blocked", "failed", "interrupted"}
                else None
            )
            self.update_attempt(
                team_run_id,
                round_id,
                role,
                status=error_status,
                error_artifact_id=artifact_id if should_record_error else None,
                increment_retry=should_record_error,
            )
            return
        if artifact_type == "role_artifact_invalid":
            if self._attempt_artifact_link_exists(
                team_run_id,
                round_id,
                role,
                "completion_artifact_id",
                artifact_id,
            ):
                return
            self.ensure_attempt(team_run_id, round_id=round_id, role=role, status="waiting")
            self.update_attempt(
                team_run_id,
                round_id,
                role,
                status="waiting",
                completion_event_id=_coerce_round_id(payload.get("runtime_event_id")) or None,
                completion_artifact_id=artifact_id,
            )
            return
        if artifact_type not in RESULT_ARTIFACT_TYPES:
            return
        if self._attempt_artifact_link_exists(
            team_run_id,
            round_id,
            role,
            "completion_artifact_id",
            artifact_id,
        ):
            return
        if (
            self.round_status(team_run_id, round_id) in {"blocked", "failed", "interrupted"}
            and not payload.get("runtime_event_id")
        ):
            return
        status = str(payload.get("status") or "passed").strip() or "passed"
        if status in {"completed", "success", "succeeded", "done"}:
            status = "passed"
        if status == "waiting" and str(payload.get("handoff_to") or "") in RELAY_ROLE_IDS:
            status = "passed"
        if status not in {
            "queued",
            "streaming",
            "passed",
            "waiting",
            "blocked",
            "failed",
            "interrupted",
        }:
            status = "passed"
        self.ensure_attempt(team_run_id, round_id=round_id, role=role, status=status)
        self.update_attempt(
            team_run_id,
            round_id,
            role,
            status=status,
            completion_event_id=_coerce_round_id(payload.get("runtime_event_id")) or None,
            completion_artifact_id=artifact_id,
        )

    def latest_attempt(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
    ) -> RelayLifecycleAttempt:
        row = self._latest_attempt_row(team_run_id, round_id, role)
        if row is None:
            raise KeyError(f"unknown relay attempt: {team_run_id}/{round_id}/{role}")
        return self._attempt_from_row(row)

    def attempt_for_completion_identity(
        self,
        team_run_id: int,
        role: str,
        *,
        agent_run_id: int | None = None,
        turn_id: str = "",
    ) -> RelayLifecycleAttempt | None:
        """Return the durable attempt that originated a provider completion.

        ``team_agent_jobs`` is deliberately a current-round projection, so it
        loses an old provider run as soon as the role is dispatched again.
        Completion projection must instead bind to the immutable attempt row.
        An agent run is authoritative; a turn is a legacy fallback for
        providers that cannot report a run id.
        """

        run_id = _coerce_optional_int(agent_run_id)
        clean_turn_id = str(turn_id or "").strip()
        clean_role = str(role or "director")
        if run_id is not None and run_id > 0:
            row = self._conn.execute(
                """
                SELECT * FROM relay_role_attempts
                WHERE team_run_id = ? AND role = ? AND agent_run_id = ?
                ORDER BY CASE
                            WHEN ? != ''
                             AND (turn_id = ? OR active_turn_id = ?)
                            THEN 0
                            ELSE 1
                         END,
                         round_id DESC,
                         attempt_no DESC
                LIMIT 1
                """,
                (
                    team_run_id,
                    clean_role,
                    run_id,
                    clean_turn_id,
                    clean_turn_id,
                    clean_turn_id,
                ),
            ).fetchone()
            if row is not None:
                return self._attempt_from_row(row)
        if not clean_turn_id:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM relay_role_attempts
            WHERE team_run_id = ?
              AND role = ?
              AND (turn_id = ? OR active_turn_id = ?)
            ORDER BY round_id DESC, attempt_no DESC
            LIMIT 1
            """,
            (team_run_id, clean_role, clean_turn_id, clean_turn_id),
        ).fetchone()
        return self._attempt_from_row(row) if row is not None else None

    def attempt_by_agent_run_id(
        self,
        agent_run_id: int,
    ) -> RelayLifecycleAttempt | None:
        """Find an originating Relay attempt without consulting job mirrors."""

        run_id = _coerce_optional_int(agent_run_id)
        if run_id is None or run_id <= 0:
            return None
        row = self._conn.execute(
            """
            SELECT relay_role_attempts.*
            FROM relay_role_attempts
            JOIN team_runs ON team_runs.id = relay_role_attempts.team_run_id
            WHERE team_runs.route = 'relay'
              AND relay_role_attempts.agent_run_id = ?
            ORDER BY relay_role_attempts.updated_at DESC,
                     relay_role_attempts.round_id DESC,
                     relay_role_attempts.attempt_no DESC,
                     relay_role_attempts.id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return self._attempt_from_row(row) if row is not None else None

    def attempts_for_round(
        self,
        team_run_id: int,
        round_id: int,
    ) -> dict[str, RelayLifecycleAttempt]:
        rows = self._conn.execute(
            """
            SELECT * FROM relay_role_attempts
            WHERE team_run_id = ? AND round_id = ?
            ORDER BY role ASC, attempt_no ASC
            """,
            (team_run_id, round_id),
        ).fetchall()
        latest: dict[str, RelayLifecycleAttempt] = {}
        for row in rows:
            latest[str(row["role"])] = self._attempt_from_row(row)
        return latest

    def sync_legacy_projection(
        self,
        team_run_id: int,
        *,
        preserve_activity: bool = False,
    ) -> None:
        round_id = self._active_round_id(team_run_id)
        round_status = self.round_status(team_run_id, round_id)
        if preserve_activity:
            self._conn.execute(
                "UPDATE team_runs SET status = ? WHERE id = ? AND status != ?",
                (round_status, team_run_id, round_status),
            )
        elif (team_run := self._ledger.get_team_run(team_run_id)) is None:
            return
        elif team_run.status != round_status:
            self._ledger.update_team_run_status(team_run_id, round_status)
        attempts = self.attempts_for_round(team_run_id, round_id)
        for job in self._ledger.list_team_agent_jobs(team_run_id):
            attempt = attempts.get(job.role)
            status = _legacy_projection_status(round_status, attempt)
            if preserve_activity:
                self._conn.execute(
                    """
                    UPDATE team_agent_jobs
                    SET status = ?
                    WHERE id = ? AND status != ?
                    """,
                    (status, job.id, status),
                )
            elif str(job.status or "idle") != status:
                self._ledger.update_team_agent_job_status(job.id, status)
        if preserve_activity:
            self._conn.commit()

    def _has_rounds(self, team_run_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM relay_rounds WHERE team_run_id = ? LIMIT 1",
            (team_run_id,),
        ).fetchone()
        return row is not None

    def _artifact_rows(self, team_run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT id, artifact_type, payload_json FROM team_artifacts
            WHERE team_run_id = ?
            ORDER BY id ASC
            """,
            (team_run_id,),
        ).fetchall()
        artifacts: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("artifact_type", str(row["artifact_type"] or ""))
            artifacts.append({"id": int(row["id"]), "payload": payload})
        return artifacts

    def _repair_task_from_artifacts(
        self,
        team_run_id: int,
        artifacts: list[dict[str, Any]],
    ) -> None:
        round_ids = sorted(
            {
                _coerce_round_id(artifact["payload"].get("round_id"))
                for artifact in artifacts
                if _coerce_round_id(artifact["payload"].get("round_id")) > 0
            }
        )
        if round_ids:
            current_round_id = max(round_ids)
            for round_id in round_ids:
                self.ensure_round(
                    team_run_id,
                    round_id=round_id,
                    trigger_kind="backfill" if round_id > 1 else "initial",
                )
            self._supersede_prior_rounds(team_run_id, current_round_id)
            team_run = self._ledger.get_team_run(team_run_id)
            existing_status = self.round_status(team_run_id, current_round_id)
            current_status = (
                existing_status
                if existing_status in ROUND_TERMINAL_STATUSES
                else str(getattr(team_run, "status", "") or "running")
            )
            self.set_round_status(
                team_run_id,
                current_round_id,
                current_status,
                preserve_activity=True,
            )
        for artifact in artifacts:
            self.observe_artifact(team_run_id, artifact["id"], artifact["payload"])
        self.sync_legacy_projection(team_run_id, preserve_activity=True)

    def _supersede_prior_rounds(self, team_run_id: int, current_round_id: int) -> None:
        if current_round_id <= 1:
            return
        now = _now()
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET status = 'superseded', updated_at = ?, closed_at = COALESCE(closed_at, ?)
            WHERE team_run_id = ?
              AND round_id < ?
              AND status NOT IN ('completed', 'failed', 'interrupted', 'superseded')
            """,
            (now, now, team_run_id, current_round_id),
        )
        self._conn.execute(
            """
            UPDATE relay_role_attempts
            SET status = 'superseded', updated_at = ?, closed_at = COALESCE(closed_at, ?)
            WHERE team_run_id = ?
              AND round_id < ?
              AND status NOT IN ('passed', 'failed', 'interrupted', 'superseded')
            """,
            (now, now, team_run_id, current_round_id),
        )
        self._conn.commit()

    def _latest_round_id(self, team_run_id: int, *, fallback: int = 1) -> int:
        row = self._conn.execute(
            """
            SELECT round_id FROM relay_rounds
            WHERE team_run_id = ?
            ORDER BY round_id DESC
            LIMIT 1
            """,
            (team_run_id,),
        ).fetchone()
        return int(row["round_id"]) if row is not None else max(1, int(fallback or 1))

    def _attempt_artifact_link_exists(
        self,
        team_run_id: int,
        round_id: int,
        role: str,
        column: str,
        artifact_id: int,
    ) -> bool:
        if column not in {
            "dispatch_artifact_id",
            "completion_artifact_id",
            "error_artifact_id",
        }:
            raise ValueError(f"unsupported artifact link column: {column}")
        row = self._conn.execute(
            f"""
            SELECT 1 FROM relay_role_attempts
            WHERE team_run_id = ?
              AND round_id = ?
              AND role = ?
              AND {column} = ?
            LIMIT 1
            """,
            (team_run_id, round_id, role, artifact_id),
        ).fetchone()
        if row is not None:
            return True
        latest = self._latest_attempt_row(team_run_id, round_id, role)
        if latest is None or latest[column] is None:
            return False
        return int(latest[column]) > int(artifact_id)

    def _latest_attempt_row(self, team_run_id: int, round_id: int, role: str) -> Any | None:
        return self._conn.execute(
            """
            SELECT * FROM relay_role_attempts
            WHERE team_run_id = ? AND round_id = ? AND role = ?
            ORDER BY attempt_no DESC
            LIMIT 1
            """,
            (team_run_id, round_id, role),
        ).fetchone()

    def _attempt_from_row(self, row: Any) -> RelayLifecycleAttempt:
        try:
            provider_mode = json.loads(str(row["provider_mode_json"] or "{}"))
        except json.JSONDecodeError:
            provider_mode = {}
        try:
            provider_child_activity = json.loads(
                str(row["provider_child_activity_json"] or "{}")
            )
        except json.JSONDecodeError:
            provider_child_activity = {}
        return RelayLifecycleAttempt(
            team_run_id=int(row["team_run_id"]),
            round_id=int(row["round_id"]),
            role=str(row["role"]),
            attempt_no=int(row["attempt_no"]),
            status=str(row["status"]),
            provider=str(row["provider"] or ""),
            native_session_id=str(row["native_session_id"] or ""),
            agent_run_id=(
                int(row["agent_run_id"]) if row["agent_run_id"] is not None else None
            ),
            turn_id=str(row["turn_id"] or ""),
            active_turn_id=str(row["active_turn_id"] or ""),
            dispatch_artifact_id=(
                int(row["dispatch_artifact_id"])
                if row["dispatch_artifact_id"] is not None
                else None
            ),
            completion_event_id=(
                int(row["completion_event_id"])
                if row["completion_event_id"] is not None
                else None
            ),
            completion_artifact_id=(
                int(row["completion_artifact_id"])
                if row["completion_artifact_id"] is not None
                else None
            ),
            error_artifact_id=(
                int(row["error_artifact_id"]) if row["error_artifact_id"] is not None else None
            ),
            retry_count=int(row["retry_count"] or 0),
            execution_mode=normalize_relay_execution_mode(row["execution_mode"]),
            team_strategy=str(row["team_strategy"] or "none"),
            provider_mode=provider_mode if isinstance(provider_mode, dict) else {},
            provider_child_activity=(
                provider_child_activity
                if isinstance(provider_child_activity, dict)
                else {}
            ),
        )
