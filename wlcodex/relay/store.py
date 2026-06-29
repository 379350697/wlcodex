from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from wlcodex.models import TeamAgentJob, TeamArtifact, TeamRun
from wlcodex.relay.artifact_types import is_relay_artifact_type
from wlcodex.relay.context import build_relay_board
from wlcodex.relay.lifecycle import RelayLifecycleStore
from wlcodex.relay.models import (
    RELAY_ROLE_IDS,
    HandoffPacket,
    RelayBoard,
    RelayPendingInput,
    RelayRoleJob,
    RelaySessionLink,
    RelayTask,
    RelayTaskDetail,
    RelayTaskSummary,
    RoleContextPacket,
)


RELAY_ASSIGNMENT_PREFIX = "relay.assignment."


class RelayStore:
    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self.lifecycle = RelayLifecycleStore(ledger)
        self.lifecycle.backfill_all_relay_tasks()

    def create_task(
        self,
        *,
        title: str,
        prompt: str,
        workspace: str,
        provider: str,
        role_providers: dict[str, str] | None = None,
    ) -> RelayTask:
        role_provider_snapshot = _normalize_role_providers(
            role_providers,
            fallback=provider,
        )
        team_run = self._ledger.create_team_run(
            0,
            None,
            _encode_goal(
                title=title,
                prompt=prompt,
                workspace=workspace,
                provider=provider,
                role_providers=role_provider_snapshot,
            ),
            route="relay",
            risk_level="medium",
        )
        for role in RELAY_ROLE_IDS:
            self._ledger.create_team_agent_job(
                team_run_id=team_run.id,
                role=role,
                model_profile=role_provider_snapshot.get(role, provider),
                status="queued" if role == "director" else "idle",
            )
        self.lifecycle.create_initial_round(team_run.id)
        task = self._task_from_run(team_run)
        board = build_relay_board(
            task,
            latest_user_input=prompt,
            current_dispatch="director",
            next_step="director review",
        )
        self.save_artifact(
            task.id,
            "director",
            "relay_board",
            board.to_json_dict(),
            summary="RelayBoard initialized",
        )
        return task

    def list_tasks(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
    ) -> list[RelayTaskSummary]:
        rows = self._ledger._conn.execute(
            "SELECT * FROM team_runs WHERE route = 'relay' ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        summaries: list[RelayTaskSummary] = []
        for row in rows:
            task = self._task_from_run(_row_to_team_run(row))
            if workspace and task.workspace != workspace:
                continue
            if status and task.status != status:
                continue
            artifacts = self._relay_artifacts(task.id)
            current_artifacts = _current_round_artifacts(artifacts)
            jobs = self._role_jobs(task.id, artifacts=current_artifacts)
            summaries.append(
                RelayTaskSummary.from_task(
                    task,
                    role_statuses={job.role: job.status for job in jobs},
                    role_providers={job.role: job.provider for job in jobs},
                    director_decision_summary=_latest_summary(
                        current_artifacts, "routing_decision"
                    ),
                    latest_handoff_summary=_latest_summary(current_artifacts, "handoff_packet"),
                )
            )
        return summaries

    def get_runtime_setting(self, key: str, default: str | None = None) -> str | None:
        if hasattr(self._ledger, "get_runtime_setting"):
            return self._ledger.get_runtime_setting(key, default)
        return default

    def set_runtime_setting(self, key: str, value: str) -> None:
        if not hasattr(self._ledger, "set_runtime_setting"):
            raise RuntimeError("relay runtime settings are unavailable")
        self._ledger.set_runtime_setting(key, value)

    def today_token_stats(self) -> dict[str, Any]:
        return self._relay_token_stats()

    def task_token_stats(self, task_id: int) -> dict[str, Any]:
        return self._relay_token_stats(task_id=task_id)

    def get_task_detail(self, task_id: int) -> RelayTaskDetail:
        team_run = self._ledger.get_team_run(task_id)
        if team_run is None or team_run.route != "relay":
            raise KeyError(f"unknown relay task id: {task_id}")
        self.lifecycle.backfill_task(task_id)
        self.lifecycle.sync_legacy_projection(task_id)
        team_run = self._ledger.get_team_run(task_id)
        task = self._task_from_run(team_run)
        artifacts = self._relay_artifacts(task_id)
        current_round_id = self.current_round_id(task_id)
        current_artifacts = _artifacts_for_round(artifacts, current_round_id)
        board = self._latest_board(task, current_artifacts or artifacts)
        latest_handoff = self._latest_handoff(current_artifacts)
        routing_decision = _latest_routing_decision(current_artifacts)
        role_jobs = self._role_jobs(task_id, artifacts=current_artifacts)
        return RelayTaskDetail(
            task=task,
            board=board,
            role_jobs=role_jobs,
            artifacts=artifacts,
            latest_handoff=latest_handoff,
            session_links=[
                RelaySessionLink(
                    role=job.role,
                    provider=job.provider,
                    native_session_id=job.native_session_id,
                    url=f"/native/{job.provider}?native_thread_id={job.native_session_id}",
                )
                for job in role_jobs
                if job.provider and job.native_session_id
            ],
            routing_decision=routing_decision,
            current_round_id=current_round_id,
            pending_inputs=self.list_pending_inputs(task_id),
        )

    def save_context_packet(
        self,
        task_id: int,
        role: str,
        packet: RoleContextPacket,
    ):
        job = self._team_job_for_role(task_id, role)
        return self._ledger.record_team_context_packet(
            team_run_id=task_id,
            agent_job_id=job.id,
            packet_json=packet.to_json_dict(),
            prompt_text=_packet_prompt(packet),
            prompt_tokens=0,
        )

    def save_artifact(
        self,
        task_id: int,
        role: str,
        artifact_type: str,
        payload: dict[str, Any],
        *,
        summary: str = "",
    ) -> TeamArtifact:
        if not is_relay_artifact_type(artifact_type):
            raise ValueError(f"unknown relay artifact_type: {artifact_type}")
        job = self._team_job_for_role(task_id, role) if role else None
        next_payload = dict(payload)
        if role:
            next_payload.setdefault("relay_role", role)
        next_payload.setdefault("artifact_type", artifact_type)
        next_payload.setdefault("round_id", self.current_round_id(task_id))
        artifact = self._ledger.record_team_artifact(
            team_run_id=task_id,
            agent_job_id=job.id if job else None,
            artifact_type=artifact_type,
            summary=summary or str(next_payload.get("summary") or artifact_type),
            payload=next_payload,
        )
        self.lifecycle.observe_artifact(task_id, artifact.id, artifact.payload)
        self.lifecycle.sync_legacy_projection(task_id)
        return artifact

    def save_handoff_packet(
        self,
        task_id: int,
        *,
        from_role: str,
        to_role: str,
        packet: HandoffPacket,
    ) -> TeamArtifact:
        payload = packet.to_json_dict()
        payload["handoff_to"] = to_role
        return self.save_artifact(
            task_id,
            from_role,
            "handoff_packet",
            payload,
            summary=packet.summary,
        )

    def handoffs_for_role(self, task_id: int, role: str) -> list[HandoffPacket]:
        artifacts = _current_round_artifacts(self._relay_artifacts(task_id))
        return [
            HandoffPacket.from_payload(artifact)
            for artifact in artifacts
            if artifact.get("artifact_type") == "handoff_packet"
            and str(artifact.get("handoff_to") or artifact.get("to_role") or "") in (role, "")
        ]

    def current_round_id(self, task_id: int) -> int:
        return self.lifecycle.current_round_id(
            task_id,
            fallback=_current_round_id(self._relay_artifacts(task_id)),
        )

    def next_round_id(self, task_id: int) -> int:
        return self.current_round_id(task_id) + 1

    def start_followup_round(self, task_id: int, *, trigger_artifact_id: int | None = None) -> int:
        return self.lifecycle.start_followup_round(
            task_id,
            trigger_artifact_id=trigger_artifact_id,
        )

    def queue_pending_input(
        self,
        task_id: int,
        *,
        text: str,
        attachments: dict[str, Any] | None = None,
        queued_after_round_id: int | None = None,
    ) -> RelayPendingInput:
        now = _now_text()
        round_id = queued_after_round_id or self.current_round_id(task_id)
        self._ledger._conn.execute(
            """
            INSERT INTO relay_pending_inputs (
                team_run_id, queued_after_round_id, status, text, attachments_json,
                created_at, updated_at
            )
            VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                task_id,
                round_id,
                text,
                json.dumps(attachments or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._ledger._conn.commit()
        row = self._ledger._conn.execute(
            "SELECT * FROM relay_pending_inputs WHERE id = last_insert_rowid()"
        ).fetchone()
        return _pending_input_from_row(row)

    def get_pending_input(self, task_id: int, pending_input_id: int) -> RelayPendingInput:
        row = self._ledger._conn.execute(
            """
            SELECT * FROM relay_pending_inputs
            WHERE id = ? AND team_run_id = ?
            """,
            (pending_input_id, task_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown relay pending input: {pending_input_id}")
        return _pending_input_from_row(row)

    def list_pending_inputs(self, task_id: int, *, status: str | None = None) -> list[RelayPendingInput]:
        sql = """
            SELECT * FROM relay_pending_inputs
            WHERE team_run_id = ?
        """
        params: list[Any] = [task_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id ASC"
        rows = self._ledger._conn.execute(sql, params).fetchall()
        return [_pending_input_from_row(row) for row in rows]

    def first_pending_input_after_round(
        self,
        task_id: int,
        round_id: int,
    ) -> RelayPendingInput | None:
        row = self._ledger._conn.execute(
            """
            SELECT * FROM relay_pending_inputs
            WHERE team_run_id = ?
              AND queued_after_round_id <= ?
              AND status = 'pending'
            ORDER BY queued_after_round_id ASC, id ASC
            LIMIT 1
            """,
            (task_id, round_id),
        ).fetchone()
        return _pending_input_from_row(row) if row is not None else None

    def mark_pending_input_steered(
        self,
        task_id: int,
        pending_input_id: int,
        *,
        round_id: int,
        role: str,
        attempt_no: int,
    ) -> RelayPendingInput:
        now = _now_text()
        self._ledger._conn.execute(
            """
            UPDATE relay_pending_inputs
            SET status = 'steered',
                updated_at = ?,
                steered_round_id = ?,
                steered_role = ?,
                steered_attempt_no = ?
            WHERE id = ? AND team_run_id = ? AND status = 'pending'
            """,
            (now, round_id, role, attempt_no, pending_input_id, task_id),
        )
        self._ledger._conn.commit()
        return self.get_pending_input(task_id, pending_input_id)

    def mark_pending_input_consumed(
        self,
        task_id: int,
        pending_input_id: int,
        *,
        consumed_round_id: int,
    ) -> RelayPendingInput:
        now = _now_text()
        self._ledger._conn.execute(
            """
            UPDATE relay_pending_inputs
            SET status = 'consumed',
                updated_at = ?,
                consumed_round_id = ?
            WHERE id = ? AND team_run_id = ? AND status = 'pending'
            """,
            (now, consumed_round_id, pending_input_id, task_id),
        )
        self._ledger._conn.commit()
        return self.get_pending_input(task_id, pending_input_id)

    def cancel_pending_input(self, task_id: int, pending_input_id: int) -> RelayPendingInput:
        now = _now_text()
        self._ledger._conn.execute(
            """
            UPDATE relay_pending_inputs
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND team_run_id = ? AND status = 'pending'
            """,
            (now, pending_input_id, task_id),
        )
        self._ledger._conn.commit()
        return self.get_pending_input(task_id, pending_input_id)

    def update_role_status(self, task_id: int, role: str, status: str) -> RelayRoleJob:
        if status == "idle":
            round_id = self.current_round_id(task_id)
            try:
                self.lifecycle.latest_attempt(task_id, round_id, role)
            except KeyError:
                job = self._team_job_for_role(task_id, role)
                self._ledger.update_team_agent_job_status(job.id, status)
            else:
                self.lifecycle.update_attempt(task_id, round_id, role, status="superseded")
                self.lifecycle.sync_legacy_projection(task_id)
            return next(
                role_job
                for role_job in self._role_jobs(task_id)
                if role_job.role == role
            )
        round_id = self.current_round_id(task_id)
        self.lifecycle.update_attempt(task_id, round_id, role, status=status)
        self.lifecycle.sync_legacy_projection(task_id)
        return next(role_job for role_job in self._role_jobs(task_id) if role_job.role == role)

    def update_task_status(self, task_id: int, status: str) -> None:
        self.lifecycle.set_round_status(task_id, self.current_round_id(task_id), status)
        self.lifecycle.sync_legacy_projection(task_id)

    def find_role_by_agent_run_id(self, agent_run_id: int) -> tuple[int, str] | None:
        row = self._ledger._conn.execute(
            """
            SELECT team_runs.id AS task_id, team_agent_jobs.role AS role
            FROM team_agent_jobs
            JOIN team_runs ON team_runs.id = team_agent_jobs.team_run_id
            WHERE team_runs.route = 'relay'
              AND team_agent_jobs.agent_run_id = ?
            ORDER BY team_agent_jobs.updated_at DESC, team_agent_jobs.id DESC
            LIMIT 1
            """,
            (agent_run_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row["task_id"]), str(row["role"])

    def update_role_metadata(
        self,
        task_id: int,
        role: str,
        *,
        provider: str = "",
        provider_engine: str = "",
        model: str = "",
        native_session_id: str = "",
        agent_run_id: int | None = None,
        turn_id: str = "",
        active_turn_id: str = "",
        turn_running: bool = False,
        dispatch_verified: bool = False,
        fallback_reason: str = "",
        provider_mode: dict[str, Any] | None = None,
    ) -> RelayRoleJob:
        job = self._team_job_for_role(task_id, role)
        if agent_run_id is not None:
            self._ledger._conn.execute(
                "UPDATE team_agent_jobs SET agent_run_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (agent_run_id, job.id),
            )
            self._ledger._conn.commit()
        round_id = self.current_round_id(task_id)
        artifact = self.save_artifact(
            task_id,
            role,
            "role_dispatch_metadata",
            {
                "relay_role": role,
                "provider": provider,
                "provider_engine": provider_engine,
                "model": model,
                "native_session_id": native_session_id,
                "agent_run_id": agent_run_id,
                "turn_id": turn_id,
                "active_turn_id": active_turn_id,
                "turn_running": turn_running,
                "dispatch_verified": dispatch_verified,
                "fallback_reason": fallback_reason,
                "provider_mode": provider_mode or {},
            },
            summary=(
                fallback_reason
                if fallback_reason
                else f"{role} dispatched via {provider or 'provider'}"
            ),
        )
        mode_payload = provider_mode if isinstance(provider_mode, dict) else {}
        self.lifecycle.update_attempt(
            task_id,
            round_id,
            role,
            status="streaming" if dispatch_verified else None,
            provider=provider,
            native_session_id=native_session_id,
            agent_run_id=agent_run_id,
            turn_id=turn_id,
            active_turn_id=active_turn_id,
            dispatch_artifact_id=artifact.id,
        )
        self.lifecycle.set_attempt_execution(
            task_id,
            round_id,
            role,
            execution_mode=str(mode_payload.get("execution_mode") or "simple"),
            team_strategy=str(mode_payload.get("team_strategy") or "none"),
            provider_mode=mode_payload,
        )
        self.lifecycle.sync_legacy_projection(task_id)
        return next(
            role_job
            for role_job in self._role_jobs(task_id, artifacts=self._relay_artifacts(task_id))
            if role_job.role == role
        )

    def _task_from_run(self, team_run: TeamRun) -> RelayTask:
        metadata = _decode_goal(team_run.goal)
        return RelayTask(
            id=team_run.id,
            title=metadata["title"],
            prompt=metadata["prompt"],
            workspace=metadata["workspace"],
            provider=metadata["provider"],
            status=team_run.status,
            phase=str(metadata.get("phase") or "director"),
            created_at=team_run.created_at.isoformat(),
            updated_at=team_run.updated_at.isoformat(),
            role_providers=_normalize_role_providers(
                metadata.get("role_providers"),
                fallback=str(metadata.get("provider") or "codex"),
            ),
        )

    def _role_jobs(
        self,
        task_id: int,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> list[RelayRoleJob]:
        artifacts = artifacts if artifacts is not None else self._relay_artifacts(task_id)
        metadata_by_role = _latest_role_metadata(artifacts)
        output_by_role = _latest_output_by_role(artifacts)
        latest_handoff_by_role = _latest_handoff_by_role(artifacts)
        latest_error_by_role = _latest_error_by_role(artifacts)
        routing_decision = _latest_routing_decision(artifacts)
        jobs = []
        for job in self._ledger.list_team_agent_jobs(task_id):
            metadata = metadata_by_role.get(job.role, {})
            output_payload = output_by_role.get(job.role, {})
            handoff = latest_handoff_by_role.get(job.role, {})
            error_payload = latest_error_by_role.get(job.role, {})
            if str(job.status) not in {"blocked", "failed", "interrupted"}:
                error_payload = {}
            jobs.append(
                RelayRoleJob(
                    id=job.id,
                    task_id=task_id,
                    role=job.role,
                    status=job.status,
                    provider=str(metadata.get("provider") or job.model_profile or ""),
                    provider_engine=str(metadata.get("provider_engine") or ""),
                    model=str(metadata.get("model") or ""),
                    native_session_id=str(metadata.get("native_session_id") or ""),
                    agent_run_id=job.agent_run_id,
                    turn_id=str(metadata.get("turn_id") or ""),
                    active_turn_id=str(metadata.get("active_turn_id") or ""),
                    turn_running=bool(metadata.get("turn_running") or False),
                    dispatch_verified=bool(metadata.get("dispatch_verified") or False),
                    fallback_reason=str(metadata.get("fallback_reason") or ""),
                    output=str(output_payload.get("output") or ""),
                    latest_handoff_summary=str(handoff.get("summary") or ""),
                    open_questions=list(output_payload.get("open_questions") or []),
                    error_message=str(
                        error_payload.get("error") or error_payload.get("summary") or ""
                    ),
                    idle_reason=_role_idle_reason(job.role, routing_decision),
                    updated_at=job.updated_at.isoformat(),
                )
            )
        return jobs

    def _team_job_for_role(self, task_id: int, role: str) -> TeamAgentJob:
        for job in self._ledger.list_team_agent_jobs(task_id):
            if job.role == role:
                return job
        raise KeyError(f"unknown relay role {role!r} for task {task_id}")

    def _relay_artifacts(self, task_id: int) -> list[dict[str, Any]]:
        artifacts = [
            {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "summary": artifact.summary,
                "created_at": artifact.created_at.isoformat(),
                **artifact.payload,
            }
            for artifact in self._ledger.list_team_artifacts(task_id)
        ]
        return _annotate_artifact_rounds(artifacts)

    def _relay_token_stats(self, *, task_id: int | None = None) -> dict[str, Any]:
        buckets: dict[str, dict[str, int | str]] = {}
        consumed_tokens = 0
        total_consumed_tokens = 0
        usage_turn_keys: set[tuple[int, int, str, str]] = set()

        def add_row(
            *,
            agent: str,
            role: str,
            total_tokens: int,
            input_tokens: int = 0,
            cached_input_tokens: int = 0,
            output_tokens: int = 0,
            reasoning_output_tokens: int = 0,
            is_today: bool,
        ) -> None:
            nonlocal consumed_tokens, total_consumed_tokens
            total_tokens = max(0, int(total_tokens or 0))
            if total_tokens <= 0:
                return
            agent_key = _relay_usage_agent_key(agent, role)
            bucket = buckets.setdefault(
                agent_key,
                {
                    "agent": agent_key,
                    "today_tokens": 0,
                    "total_tokens": 0,
                    "today_input_tokens": 0,
                    "today_output_tokens": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            )
            bucket["total_tokens"] = int(bucket["total_tokens"]) + total_tokens
            bucket["input_tokens"] = int(bucket["input_tokens"]) + max(0, int(input_tokens or 0))
            bucket["cached_input_tokens"] = int(bucket["cached_input_tokens"]) + max(
                0, int(cached_input_tokens or 0)
            )
            bucket["output_tokens"] = int(bucket["output_tokens"]) + max(0, int(output_tokens or 0))
            bucket["reasoning_output_tokens"] = int(bucket["reasoning_output_tokens"]) + max(
                0, int(reasoning_output_tokens or 0)
            )
            total_consumed_tokens += total_tokens
            if is_today:
                bucket["today_tokens"] = int(bucket["today_tokens"]) + total_tokens
                bucket["today_input_tokens"] = int(bucket["today_input_tokens"]) + max(
                    0, int(input_tokens or 0)
                )
                bucket["today_output_tokens"] = int(bucket["today_output_tokens"]) + max(
                    0, int(output_tokens or 0)
                )
                consumed_tokens += total_tokens

        usage_sql = """
            SELECT
                u.*,
                COALESCE(task_run.id, job_run.id) AS relay_task_id,
                COALESCE(job.role, u.role, '') AS relay_role,
                COALESCE(job.model_profile, '') AS relay_model_profile,
                date(u.created_at, 'localtime') = date('now', 'localtime') AS is_today
            FROM usage_events u
            LEFT JOIN team_runs task_run
              ON task_run.id = u.task_id AND task_run.route = 'relay'
            LEFT JOIN team_agent_jobs job
              ON job.agent_run_id = u.agent_run_id
            LEFT JOIN team_runs job_run
              ON job_run.id = job.team_run_id AND job_run.route = 'relay'
            WHERE (task_run.id IS NOT NULL OR job_run.id IS NOT NULL)
        """
        usage_params: list[Any] = []
        if task_id is not None:
            usage_sql += " AND COALESCE(task_run.id, job_run.id) = ?"
            usage_params.append(int(task_id))
        usage_sql += " ORDER BY u.id ASC"
        for row in self._ledger._conn.execute(usage_sql, usage_params).fetchall():
            relay_task_id = int(row["relay_task_id"])
            agent_run_id = int(row["agent_run_id"] or 0)
            role = str(row["relay_role"] or row["role"] or "")
            agent = _relay_usage_agent_key(
                str(row["agent"] or row["relay_model_profile"] or ""), role
            )
            turn_id = str(row["external_turn_id"] or "")
            if agent_run_id and turn_id:
                usage_turn_keys.add((relay_task_id, agent_run_id, agent, turn_id))
            add_row(
                agent=agent,
                role=role,
                total_tokens=int(row["total_tokens"] or 0),
                input_tokens=int(row["input_tokens"] or 0),
                cached_input_tokens=int(row["cached_input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                reasoning_output_tokens=int(row["reasoning_output_tokens"] or 0),
                is_today=bool(row["is_today"]),
            )

        runtime_sql = """
            SELECT
                r.id,
                r.occurred_at,
                r.agent_run_id,
                r.actor,
                r.payload_json,
                COALESCE(task_run.id, job_run.id) AS relay_task_id,
                COALESCE(job.role, '') AS relay_role,
                COALESCE(job.model_profile, '') AS relay_model_profile,
                date(r.occurred_at, 'localtime') = date('now', 'localtime') AS is_today
            FROM runtime_events r
            LEFT JOIN team_runs task_run
              ON task_run.id = r.task_id AND task_run.route = 'relay'
            LEFT JOIN team_agent_jobs job
              ON job.agent_run_id = r.agent_run_id
            LEFT JOIN team_runs job_run
              ON job_run.id = job.team_run_id AND job_run.route = 'relay'
            WHERE (
                r.event_type = 'model.usage.updated'
                OR (
                    json_valid(r.payload_json)
                    AND (
                        json_type(r.payload_json, '$.usage') IS NOT NULL
                        OR json_type(r.payload_json, '$.total_tokens') IS NOT NULL
                        OR json_type(r.payload_json, '$.totalTokens') IS NOT NULL
                    )
                )
            )
              AND (task_run.id IS NOT NULL OR job_run.id IS NOT NULL)
        """
        runtime_params: list[Any] = []
        if task_id is not None:
            runtime_sql += " AND COALESCE(task_run.id, job_run.id) = ?"
            runtime_params.append(int(task_id))
        runtime_sql += " ORDER BY r.id ASC"

        latest_runtime_by_turn: dict[
            tuple[int, int, str, str],
            tuple[int, dict[str, Any], str, str, bool],
        ] = {}
        for row in self._ledger._conn.execute(runtime_sql, runtime_params).fetchall():
            payload = _relay_json_payload(row["payload_json"])
            if not payload:
                continue
            relay_task_id = int(row["relay_task_id"])
            agent_run_id = int(row["agent_run_id"] or 0)
            agent = _relay_usage_agent_key(
                str(
                    payload.get("provider")
                    or payload.get("agent")
                    or payload.get("source_kind")
                    or row["relay_model_profile"]
                    or row["actor"]
                    or ""
                ),
                str(row["relay_role"] or ""),
            )
            turn_id = str(
                payload.get("native_turn_id")
                or payload.get("external_turn_id")
                or payload.get("turn_id")
                or ""
            )
            if not turn_id:
                turn_id = f"event:{int(row['id'])}"
            key = (relay_task_id, agent_run_id, agent, turn_id)
            latest_runtime_by_turn[key] = (
                int(row["id"]),
                payload,
                str(row["relay_role"] or ""),
                agent,
                bool(row["is_today"]),
            )

        for key, (_event_id, payload, role, agent, is_today) in latest_runtime_by_turn.items():
            relay_task_id, agent_run_id, _agent, turn_id = key
            if (relay_task_id, agent_run_id, agent, turn_id) in usage_turn_keys:
                continue
            usage = _relay_usage_tokens_from_payload(payload)
            add_row(
                agent=agent,
                role=role,
                total_tokens=usage["total_tokens"],
                input_tokens=usage["input_tokens"],
                cached_input_tokens=usage["cached_input_tokens"],
                output_tokens=usage["output_tokens"],
                reasoning_output_tokens=usage["reasoning_output_tokens"],
                is_today=is_today,
            )

        agents = sorted(
            buckets.values(),
            key=lambda item: (
                -int(item["today_tokens"]),
                -int(item["total_tokens"]),
                str(item["agent"]),
            ),
        )
        return {
            "consumed_tokens": consumed_tokens,
            "total_consumed_tokens": total_consumed_tokens,
            "agents": agents,
        }

    def _latest_board(
        self,
        task: RelayTask,
        artifacts: list[dict[str, Any]],
    ) -> RelayBoard:
        for artifact in reversed(artifacts):
            if artifact.get("artifact_type") == "relay_board":
                payload = dict(artifact)
                return RelayBoard(
                    task_id=task.id,
                    current_goal=str(payload.get("current_goal") or task.prompt),
                    phase=str(payload.get("phase") or task.phase),
                    latest_user_input=str(payload.get("latest_user_input") or task.prompt),
                    confirmed_facts=list(payload.get("confirmed_facts") or []),
                    open_questions=list(payload.get("open_questions") or []),
                    risks=list(payload.get("risks") or []),
                    current_dispatch=str(payload.get("current_dispatch") or ""),
                    next_step=str(payload.get("next_step") or ""),
                )
        return build_relay_board(task, latest_user_input=task.prompt)

    def _latest_handoff(self, artifacts: list[dict[str, Any]]) -> HandoffPacket | None:
        for artifact in reversed(artifacts):
            if artifact.get("artifact_type") == "handoff_packet":
                return HandoffPacket.from_payload(artifact)
        return None


def _encode_goal(
    *,
    title: str,
    prompt: str,
    workspace: str,
    provider: str,
    role_providers: dict[str, str] | None = None,
    phase: str = "director",
) -> str:
    return "relay:" + __import__("json").dumps(
        {
            "title": title,
            "prompt": prompt,
            "workspace": workspace,
            "provider": provider,
            "phase": phase,
            "role_providers": _normalize_role_providers(
                role_providers,
                fallback=provider,
            ),
        },
        ensure_ascii=False,
    )


def _decode_goal(goal: str) -> dict[str, Any]:
    if goal.startswith("relay:"):
        try:
            payload = __import__("json").loads(goal.removeprefix("relay:"))
            provider = str(payload.get("provider") or "codex")
            return {
                "title": str(payload.get("title") or payload.get("prompt") or "Relay Task"),
                "prompt": str(payload.get("prompt") or payload.get("title") or ""),
                "workspace": str(payload.get("workspace") or ""),
                "provider": provider,
                "phase": str(payload.get("phase") or "director"),
                "role_providers": _normalize_role_providers(
                    payload.get("role_providers"),
                    fallback=provider,
                ),
            }
        except Exception:
            pass
    return {
        "title": goal,
        "prompt": goal,
        "workspace": "",
        "provider": "codex",
        "phase": "director",
        "role_providers": _normalize_role_providers(None, fallback="codex"),
    }


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pending_input_from_row(row: Any) -> RelayPendingInput:
    try:
        attachments = json.loads(str(row["attachments_json"] or "{}"))
    except json.JSONDecodeError:
        attachments = {}
    if not isinstance(attachments, dict):
        attachments = {}
    return RelayPendingInput(
        id=int(row["id"]),
        task_id=int(row["team_run_id"]),
        queued_after_round_id=int(row["queued_after_round_id"] or 1),
        status=str(row["status"] or "pending"),
        text=str(row["text"] or ""),
        attachments=attachments,
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        consumed_round_id=(
            int(row["consumed_round_id"]) if row["consumed_round_id"] is not None else None
        ),
        steered_round_id=(
            int(row["steered_round_id"]) if row["steered_round_id"] is not None else None
        ),
        steered_role=str(row["steered_role"] or ""),
        steered_attempt_no=(
            int(row["steered_attempt_no"]) if row["steered_attempt_no"] is not None else None
        ),
    )


def _normalize_role_providers(
    values: Any,
    *,
    fallback: str,
) -> dict[str, str]:
    source = values if isinstance(values, dict) else {}
    fallback_provider = str(fallback or "codex")
    return {
        role: str(source.get(role) or fallback_provider).strip() or fallback_provider
        for role in RELAY_ROLE_IDS
    }


def _coerce_round_id(value: Any) -> int:
    try:
        round_id = int(value)
    except (TypeError, ValueError):
        return 0
    return round_id if round_id > 0 else 0


def _annotate_artifact_rounds(
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_round = 1
    annotated: list[dict[str, Any]] = []
    for artifact in artifacts:
        next_artifact = dict(artifact)
        explicit_round = _coerce_round_id(next_artifact.get("round_id"))
        if explicit_round:
            current_round = explicit_round
        elif str(next_artifact.get("artifact_type") or "") == "user_followup":
            current_round += 1
        next_artifact["round_id"] = current_round
        annotated.append(next_artifact)
    return annotated


def _current_round_id(artifacts: list[dict[str, Any]]) -> int:
    return max([_coerce_round_id(artifact.get("round_id")) for artifact in artifacts] or [1]) or 1


def _artifacts_for_round(
    artifacts: list[dict[str, Any]],
    round_id: int,
) -> list[dict[str, Any]]:
    target = _coerce_round_id(round_id) or 1
    return [
        artifact
        for artifact in artifacts
        if (_coerce_round_id(artifact.get("round_id")) or 1) == target
    ]


def _current_round_artifacts(
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _artifacts_for_round(artifacts, _current_round_id(artifacts))


def _row_to_team_run(row: Any) -> TeamRun:
    from wlcodex.db import _row_to_team_run

    return _row_to_team_run(row)


def _packet_prompt(packet: RoleContextPacket) -> str:
    return "\n".join(
        [
            f"task_id: {packet.task_id}",
            f"role: {packet.role}",
            f"workspace: {packet.workspace}",
            f"goal: {packet.current_goal}",
            f"latest_user_input: {packet.latest_user_input}",
            "handoff_summaries:",
            *[f"- {summary}" for summary in packet.handoff_summaries],
            "constraints:",
            *[f"- {constraint}" for constraint in packet.constraints],
            "expected_output_envelope:",
            __import__("json").dumps(
                packet.expected_output_envelope,
                ensure_ascii=False,
                sort_keys=True,
            ),
            "output_contract:",
            "- Return only valid JSON, with no Markdown fences or prose before/after.",
            "- Prefer the expected_output_envelope fields at the top level.",
            "- A single top-level role_envelope wrapper is allowed, but no other wrapper is allowed.",
            "- evidence_refs and open_questions must be JSON arrays.",
        ]
    )


def _latest_summary(artifacts: list[dict[str, Any]], artifact_type: str) -> str:
    for artifact in reversed(artifacts):
        if artifact.get("artifact_type") == artifact_type:
            return str(artifact.get("summary") or "")
    return ""


def _relay_json_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _relay_usage_agent_key(agent: str, role: str = "") -> str:
    value = str(agent or "").strip().lower()
    role_value = str(role or "").strip().lower()
    if value in {"codex", "codex_native", "app-server", "gpt-5"}:
        return "codex"
    if value.startswith("claude") or value in {"sdk-deepseek", "deepseek"}:
        return "claude"
    if value.startswith("antigravity"):
        return "antigravity"
    if role_value:
        return role_value
    return value or "unknown"


def _relay_usage_tokens_from_payload(payload: dict[str, Any]) -> dict[str, int]:
    candidate = _relay_usage_payload_candidate(payload)
    input_tokens = _relay_payload_int(candidate, "input_tokens", "inputTokens")
    output_tokens = _relay_payload_int(candidate, "output_tokens", "outputTokens")
    cached_input_tokens = _relay_payload_int(
        candidate,
        "cached_input_tokens",
        "cachedInputTokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cacheReadInputTokens",
    )
    reasoning_output_tokens = _relay_payload_int(
        candidate,
        "reasoning_output_tokens",
        "reasoningOutputTokens",
    )
    total_tokens = _relay_payload_int(
        candidate,
        "total_tokens",
        "totalTokens",
        "tokens",
        "consumed_tokens",
    )
    if total_tokens <= 0:
        total_tokens = input_tokens + cached_input_tokens + output_tokens + reasoning_output_tokens
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
    }


def _relay_usage_payload_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = [payload]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage)
        usage_total = usage.get("total")
        if isinstance(usage_total, dict):
            candidates.append(usage_total)
    payload_total = payload.get("total")
    if isinstance(payload_total, dict):
        candidates.append(payload_total)
    for candidate in candidates:
        if _relay_payload_int(
            candidate,
            "total_tokens",
            "totalTokens",
            "tokens",
            "consumed_tokens",
        ):
            return candidate
    return candidates[1] if len(candidates) > 1 else payload


def _relay_payload_int(payload: dict[str, Any], *keys: str) -> int:
    total = 0
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            total += int(value)
        elif isinstance(value, str):
            try:
                total += int(float(value.strip()))
            except ValueError:
                continue
    return max(0, total)


def _latest_role_metadata(artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        artifact_type = artifact.get("artifact_type")
        is_legacy_dispatch_metadata = (
            artifact_type == "routing_decision" and "dispatch_verified" in artifact
        )
        if artifact_type == "role_dispatch_metadata" or is_legacy_dispatch_metadata:
            role = str(artifact.get("relay_role") or "")
            if role:
                metadata[role] = artifact
    return metadata


def _latest_routing_decision(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        if artifact.get("artifact_type") != "routing_decision":
            continue
        if "dispatch_verified" in artifact:
            continue
        return artifact
    return None


def _role_idle_reason(role: str, decision: dict[str, Any] | None) -> str:
    if not decision:
        return "等待总工程师分配或上一角色交接"
    route = str(decision.get("route") or "")
    if route == "waiting_user":
        return "等待用户补充后再调度"
    if route == "blocked":
        return "被风险门禁阻塞"
    required_roles = {str(item) for item in decision.get("required_roles", []) if str(item).strip()}
    if role in required_roles:
        if role == "director" and route == "director_only":
            return "总工程师直接完成"
        return "等待上一角色交接"
    return "未纳入本轮路线"


def _latest_output_by_role(artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        role = str(artifact.get("relay_role") or "")
        if role and artifact.get("artifact_type") in {
            "architecture_plan",
            "implementation_report",
            "test_report",
            "audit_report",
            "final_summary",
        }:
            output[role] = artifact
    return output


def _latest_error_by_role(artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    errors: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        role = str(artifact.get("relay_role") or "")
        if not role:
            continue
        if artifact.get("artifact_type") == "role_error":
            errors[role] = artifact
            continue
        if _artifact_resolves_role_error(artifact):
            errors.pop(role, None)
    return errors


def _artifact_resolves_role_error(artifact: dict[str, Any]) -> bool:
    artifact_type = str(artifact.get("artifact_type") or "")
    if artifact_type not in {
        "architecture_plan",
        "implementation_report",
        "test_report",
        "audit_report",
        "final_summary",
        "followup_response",
        "routing_decision",
    }:
        return False
    status = str(artifact.get("status") or "").strip()
    if status:
        return status in {"passed", "completed", "success", "succeeded", "done"}
    return artifact_type in {"followup_response", "final_summary"}


def _latest_handoff_by_role(
    artifacts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    handoffs: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        role = str(artifact.get("relay_role") or "")
        if role and artifact.get("artifact_type") == "handoff_packet":
            handoffs[role] = artifact
    return handoffs
