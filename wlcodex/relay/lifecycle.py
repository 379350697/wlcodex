from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from wlcodex.relay.models import RELAY_ROLE_IDS


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_round_id(value: Any) -> int:
    try:
        round_id = int(value)
    except (TypeError, ValueError):
        return 0
    return round_id if round_id > 0 else 0


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
            self.set_round_status(team_run_id, current_round_id, current_status)
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
            self.sync_legacy_projection(team_run_id)
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
        self.ensure_attempt(
            team_run_id,
            round_id=next_round,
            role="director",
            status="queued",
        )
        self.sync_legacy_projection(team_run_id)
        return next_round

    def current_round_id(self, team_run_id: int, *, fallback: int = 1) -> int:
        self.backfill_task(team_run_id)
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

    def set_round_status(self, team_run_id: int, round_id: int, status: str) -> None:
        self.ensure_round(team_run_id, round_id=round_id, trigger_kind="backfill")
        now = _now()
        closed_at = now if status in ROUND_TERMINAL_STATUSES else None
        self._conn.execute(
            """
            UPDATE relay_rounds
            SET status = ?, updated_at = ?, closed_at = COALESCE(?, closed_at)
            WHERE team_run_id = ? AND round_id = ?
            """,
            (status, now, closed_at, team_run_id, round_id),
        )
        self._conn.commit()
        self.sync_legacy_projection(team_run_id)

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
                closed_at = COALESCE(?, closed_at)
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
        if not role:
            return
        if artifact_type == "role_dispatch_metadata":
            if self._attempt_artifact_link_exists(
                team_run_id,
                round_id,
                role,
                "dispatch_artifact_id",
                artifact_id,
            ):
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

    def sync_legacy_projection(self, team_run_id: int) -> None:
        round_id = self._active_round_id(team_run_id)
        round_status = self.round_status(team_run_id, round_id)
        self._ledger.update_team_run_status(team_run_id, round_status)
        attempts = self.attempts_for_round(team_run_id, round_id)
        for job in self._ledger.list_team_agent_jobs(team_run_id):
            attempt = attempts.get(job.role)
            status = (
                attempt.status
                if attempt is not None and attempt.status != "superseded"
                else "idle"
            )
            self._ledger.update_team_agent_job_status(job.id, status)

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
            self.set_round_status(team_run_id, current_round_id, current_status)
        for artifact in artifacts:
            self.observe_artifact(team_run_id, artifact["id"], artifact["payload"])
        self.sync_legacy_projection(team_run_id)

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
        )
