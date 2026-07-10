from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from wlcodex.models import TeamAgentJob, TeamArtifact, TeamRun
from wlcodex.relay.artifact_types import is_relay_artifact_type
from wlcodex.relay.context import build_relay_board
from wlcodex.relay.graph import MarvisRelayState, build_marvis_relay_state
from wlcodex.relay.lifecycle import RelayLifecycleStore
from wlcodex.relay.models import (
    GoalAcceptanceRecord,
    RELAY_ROLE_IDS,
    HandoffPacket,
    RelayBoard,
    RelayPendingInputClaim,
    RelayPendingInput,
    RelayPresentation,
    RelayRoleJob,
    RelaySessionLink,
    RelayTask,
    RelayTaskDetail,
    RelayTaskSummary,
    RoleContextPacket,
    build_relay_presentation,
)
from wlcodex.live_stream.models import stream_event_from_runtime
from wlcodex.runtime_events import RuntimeEvent


RELAY_ASSIGNMENT_PREFIX = "relay.assignment."


class RelayStore:
    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self.lifecycle = RelayLifecycleStore(ledger)
        # Do not repair legacy records during construction.  A store may be
        # instantiated while serving an initial page/SSE snapshot; lifecycle
        # reconciliation belongs to the explicit background worker.

    def assert_submissions_open(self) -> None:
        """Ask the shared ledger whether an operator has frozen new work."""

        assert_open = getattr(self._ledger, "assert_submissions_open", None)
        if callable(assert_open):
            assert_open()

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
        include_archived: bool = False,
    ) -> list[RelayTaskSummary]:
        """Return the user-facing list projection without lifecycle writes."""

        return self.list_tasks_readonly(
            workspace=workspace,
            status=status,
            include_archived=include_archived,
        )

    def list_tasks_readonly(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
    ) -> list[RelayTaskSummary]:
        """Read Relay cards without backfill, sync, dispatch or artifact writes."""

        archive_clause = "" if include_archived else (
            " AND NOT EXISTS ("
            "SELECT 1 FROM relay_task_archives "
            "WHERE relay_task_archives.team_run_id = team_runs.id)"
        )
        rows = self._ledger._conn.execute(
            "SELECT * FROM team_runs WHERE route = 'relay'"
            f"{archive_clause} ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        summaries: list[RelayTaskSummary] = []
        for row in rows:
            task = self._task_from_run(_row_to_team_run(row))
            if workspace and task.workspace != workspace:
                continue
            if status and task.status != status:
                continue
            artifacts = self._relay_artifacts(task.id)
            current_round_id = self.current_round_id_readonly(task.id, artifacts=artifacts)
            current_artifacts = _artifacts_for_round(artifacts, current_round_id)
            jobs = self._role_jobs(task.id, artifacts=current_artifacts)
            board = self._latest_board(task, current_artifacts or artifacts)
            latest_handoff = self._latest_handoff(current_artifacts)
            execution = self.lifecycle.round_execution(task.id, current_round_id)
            summaries.append(
                RelayTaskSummary.from_task(
                    task,
                    role_statuses={job.role: job.status for job in jobs},
                    role_providers={job.role: job.provider for job in jobs},
                    director_decision_summary=_latest_summary(
                        current_artifacts, "routing_decision"
                    ),
                    latest_handoff_summary=_latest_summary(current_artifacts, "handoff_packet"),
                    last_activity_at=_latest_activity_at(task, artifacts),
                    presentation=build_relay_presentation(
                        task=task,
                        role_jobs=jobs,
                        board=board,
                        round_execution=execution,
                        latest_handoff=(
                            latest_handoff.to_json_dict() if latest_handoff is not None else None
                        ),
                    ),
                )
            )
        return sorted(
            summaries,
            key=lambda summary: str(summary.last_activity_at or ""),
            reverse=True,
        )

    def list_tasks_page_readonly(
        self,
        *,
        workspace: str | None = None,
        presentation_state: str | None = None,
        page: int = 1,
        page_size: int = 20,
        include_archived: bool = False,
    ) -> tuple[list[RelayTaskSummary], int, dict[str, int]]:
        """Read one Relay task page after database-side semantic filtering.

        ``RelayPresentation`` remains a pure Python projection for the detail
        and card view.  The stable state portion (task status, active-round
        confirmation and freshness) is also expressed as a SQL CTE here so a
        list request does not hydrate every artifact merely to find page 1.
        The selected rows are then projected with the canonical Python
        builder, keeping one user-visible presentation contract.
        """

        selected_state = str(presentation_state or "").strip().lower()
        page = max(1, int(page or 1))
        page_size = min(100, max(1, int(page_size or 20)))
        cte, params = self._task_presentation_cte(
            workspace=workspace,
            include_archived=include_archived,
        )
        state_clause = ""
        state_params: list[Any] = []
        if selected_state:
            state_clause = " WHERE presentation_state = ?"
            state_params.append(selected_state)
        count_row = self._ledger._conn.execute(
            f"{cte} SELECT COUNT(*) AS total FROM task_projection{state_clause}",
            [*params, *state_params],
        ).fetchone()
        total = int(count_row["total"] if count_row is not None else 0)
        count_rows = self._ledger._conn.execute(
            f"{cte} "
            "SELECT presentation_state, COUNT(*) AS count "
            "FROM task_projection GROUP BY presentation_state",
            params,
        ).fetchall()
        state_counts = {
            str(row["presentation_state"]): int(row["count"])
            for row in count_rows
        }
        offset = (page - 1) * page_size
        rows = self._ledger._conn.execute(
            f"{cte} SELECT * FROM task_projection{state_clause} "
            "ORDER BY list_activity_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, *state_params, page_size, offset],
        ).fetchall()
        summaries = [self._summary_from_task_row(row) for row in rows]
        return summaries, total, state_counts

    def _task_presentation_cte(
        self,
        *,
        workspace: str | None,
        include_archived: bool,
    ) -> tuple[str, list[Any]]:
        """Build a bound read-only SQL projection for Relay list navigation."""

        clauses = ["team_runs.route = 'relay'"]
        params: list[Any] = []
        if not include_archived:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM relay_task_archives "
                "WHERE relay_task_archives.team_run_id = team_runs.id)"
            )
        selected_workspace = str(workspace or "").strip()
        if selected_workspace:
            # Historic non-Relay ``goal`` rows and malformed legacy goals are
            # intentionally treated as an empty workspace instead of making a
            # harmless list GET fail on ``json_extract``.
            clauses.append(
                "COALESCE(json_extract("
                "CASE WHEN substr(team_runs.goal, 1, 6) = 'relay:' "
                "AND json_valid(substr(team_runs.goal, 7)) "
                "THEN substr(team_runs.goal, 7) ELSE '{}' END, "
                "'$.workspace'), '') = ?"
            )
            params.append(selected_workspace)
        where_clause = " AND ".join(clauses)
        return (
            f"""
            WITH latest_round AS (
                SELECT team_run_id, MAX(round_id) AS round_id
                FROM relay_rounds
                WHERE status != 'superseded'
                GROUP BY team_run_id
            ), current_round AS (
                SELECT rounds.team_run_id,
                       rounds.waiting_reason,
                       rounds.confirmation_source,
                       rounds.confirmation_kind
                FROM relay_rounds AS rounds
                JOIN latest_round
                  ON latest_round.team_run_id = rounds.team_run_id
                 AND latest_round.round_id = rounds.round_id
            ), task_projection AS (
                SELECT team_runs.*,
                       COALESCE(
                           (SELECT MAX(created_at) FROM team_artifacts
                            WHERE team_artifacts.team_run_id = team_runs.id),
                           team_runs.updated_at
                       ) AS list_activity_at,
                       CASE
                           WHEN COALESCE(current_round.confirmation_source, '')
                                IN ('provider_native_resolving', 'provider_native_superseding')
                           THEN 'blocked'
                           WHEN team_runs.status IN ('queued', 'running')
                                AND (
                                    team_runs.updated_at = ''
                                    OR julianday(team_runs.updated_at) IS NULL
                                    OR julianday('now') - julianday(team_runs.updated_at) > (30.0 / 1440.0)
                                )
                           THEN 'stale'
                           WHEN team_runs.status IN ('queued', 'running') THEN 'running'
                           WHEN team_runs.status = 'waiting_user'
                                AND (
                                    COALESCE(current_round.waiting_reason, '')
                                        IN ('plan_approval', 'provider_approval')
                                    OR COALESCE(current_round.confirmation_kind, '') LIKE '%\\_approval' ESCAPE '\\'
                                )
                           THEN 'waiting_approval'
                           WHEN team_runs.status = 'waiting_user' THEN 'waiting_user'
                           WHEN team_runs.status = 'blocked' THEN 'blocked'
                           WHEN team_runs.status = 'failed' THEN 'failed'
                           WHEN team_runs.status = 'completed' THEN 'completed'
                           WHEN team_runs.status = 'interrupted' THEN 'interrupted'
                           ELSE 'stale'
                       END AS presentation_state
                FROM team_runs
                LEFT JOIN current_round ON current_round.team_run_id = team_runs.id
                WHERE {where_clause}
            )
            """,
            params,
        )

    def _summary_from_task_row(self, row: Any) -> RelayTaskSummary:
        """Build the canonical pure presentation for one selected list row."""

        task = self._task_from_run(_row_to_team_run(row))
        artifacts = self._relay_artifacts(task.id)
        current_round_id = self.current_round_id_readonly(task.id, artifacts=artifacts)
        current_artifacts = _artifacts_for_round(artifacts, current_round_id)
        jobs = self._role_jobs(task.id, artifacts=current_artifacts)
        board = self._latest_board(task, current_artifacts or artifacts)
        latest_handoff = self._latest_handoff(current_artifacts)
        execution = self.lifecycle.round_execution(task.id, current_round_id)
        return RelayTaskSummary.from_task(
            task,
            role_statuses={job.role: job.status for job in jobs},
            role_providers={job.role: job.provider for job in jobs},
            director_decision_summary=_latest_summary(current_artifacts, "routing_decision"),
            latest_handoff_summary=_latest_summary(current_artifacts, "handoff_packet"),
            last_activity_at=_latest_activity_at(task, artifacts),
            presentation=build_relay_presentation(
                task=task,
                role_jobs=jobs,
                board=board,
                round_execution=execution,
                latest_handoff=(
                    latest_handoff.to_json_dict() if latest_handoff is not None else None
                ),
            ),
        )

    def get_runtime_setting(self, key: str, default: str | None = None) -> str | None:
        if hasattr(self._ledger, "get_runtime_setting"):
            return self._ledger.get_runtime_setting(key, default)
        return default

    def set_runtime_setting(self, key: str, value: str) -> None:
        if not hasattr(self._ledger, "set_runtime_setting"):
            raise RuntimeError("relay runtime settings are unavailable")
        self._ledger.set_runtime_setting(key, value)

    def append_stream_event(
        self,
        task_id: int,
        event_type: str,
        *,
        role: str = "",
        job_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ):
        from wlcodex.relay.events import RelayEvent

        payload = dict(payload or {})
        runtime_event_id = _runtime_event_id_from_payload(payload)
        stored_payload = _relay_stream_payload_for_storage(event_type, payload)
        now = _now_text()
        if runtime_event_id is not None:
            existing = self._ledger._conn.execute(
                """
                SELECT *
                FROM relay_stream_events
                WHERE task_id = ?
                  AND event_type = ?
                  AND role = ?
                  AND runtime_event_id = ?
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (task_id, event_type, role, runtime_event_id),
            ).fetchone()
            if existing is not None:
                return RelayEvent(
                    task_id=int(existing["task_id"]),
                    event_type=str(existing["event_type"]),
                    sequence=int(existing["sequence"]),
                    role=str(existing["role"] or ""),
                    job_id=existing["job_id"],
                    payload=payload,
                    created_at=str(existing["created_at"]),
                )
        row = self._ledger._conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence
            FROM relay_stream_events
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        sequence = int(row["sequence"] if row is not None else 1)
        self._ledger._conn.execute(
            """
            INSERT INTO relay_stream_events (
                task_id, sequence, event_type, role, job_id, runtime_event_id,
                payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                sequence,
                event_type,
                role,
                job_id,
                runtime_event_id,
                json.dumps(stored_payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        self._ledger._conn.commit()
        return RelayEvent(
            task_id=task_id,
            event_type=event_type,
            sequence=sequence,
            role=role,
            job_id=job_id,
            payload=payload,
            created_at=now,
        )

    def list_stream_events(self, task_id: int, *, after: int = 0):
        from wlcodex.relay.events import RelayEvent

        rows = self._ledger._conn.execute(
            """
            SELECT * FROM relay_stream_events
            WHERE task_id = ? AND sequence > ?
            ORDER BY sequence ASC
            """,
            (task_id, after),
        ).fetchall()
        events = []
        for row in rows:
            payload = json.loads(str(row["payload_json"] or "{}"))
            runtime_event_id = row["runtime_event_id"]
            if runtime_event_id is not None:
                payload.setdefault("runtime_event_id", int(runtime_event_id))
                payload = self._hydrate_stream_event_payload(
                    str(row["event_type"]),
                    payload,
                    int(runtime_event_id),
                )
            events.append(
                RelayEvent(
                    task_id=int(row["task_id"]),
                    event_type=str(row["event_type"]),
                    sequence=int(row["sequence"]),
                    role=str(row["role"] or ""),
                    job_id=row["job_id"],
                    payload=payload,
                    created_at=str(row["created_at"]),
                )
            )
        return events

    def _hydrate_stream_event_payload(
        self,
        event_type: str,
        payload: dict[str, Any],
        runtime_event_id: int,
    ) -> dict[str, Any]:
        row = self._ledger._conn.execute(
            "SELECT * FROM runtime_events WHERE id = ?",
            (runtime_event_id,),
        ).fetchone()
        if row is None:
            return payload
        runtime_event = _runtime_event_from_row(row)
        runtime_payload = dict(runtime_event.payload)
        hydrated = dict(payload)
        hydrated.setdefault("agent_run_id", runtime_event.agent_run_id)
        hydrated.setdefault("runtime_event_id", runtime_event.id)
        for key in (
            "itemId",
            "item_id",
            "stream_key",
            "native_message_id",
            "message_id",
            "native_turn_id",
            "turnId",
            "turn_id",
            "display_source",
        ):
            if key in runtime_payload:
                hydrated.setdefault(key, runtime_payload[key])
        if event_type == "role.output_delta":
            text = _runtime_payload_text(runtime_payload)
            if text:
                hydrated.setdefault("delta", text)
            return hydrated
        if event_type == "role.native_event":
            stream_event = stream_event_from_runtime(runtime_event)
            hydrated.setdefault("kind", stream_event.kind)
            text = _runtime_payload_text(runtime_payload)
            if text:
                if stream_event.kind in {"text_delta", "reasoning_delta", "command_output"}:
                    hydrated.setdefault("delta", text)
                else:
                    hydrated.setdefault("text", text)
            for key in (
                "status",
                "title",
                "command",
                "exit_code",
                "approval_id",
                "request_id",
                "codexRequestId",
                "provider",
            ):
                value = runtime_payload.get(key)
                if isinstance(value, (str, int, float, bool)) and value not in ("", None):
                    hydrated.setdefault(key, value)
            return hydrated
        return hydrated

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
        return self.get_task_detail_readonly(task_id)

    def get_task_detail_readonly(self, task_id: int) -> RelayTaskDetail:
        """Build a Relay detail projection without changing lifecycle state.

        This is the only detail method intended for HTTP GET/SSE snapshots.
        Mutating callers retain :meth:`get_task_detail`, whose compatibility
        repair is intentionally explicit and isolated from request rendering.
        """

        team_run = self._ledger.get_team_run(task_id)
        if team_run is None or team_run.route != "relay":
            raise KeyError(f"unknown relay task id: {task_id}")
        task = self._task_from_run(team_run)
        artifacts = self._relay_artifacts(task_id)
        current_round_id = self.current_round_id_readonly(task_id, artifacts=artifacts)
        current_artifacts = _artifacts_for_round(artifacts, current_round_id)
        board = self._latest_board(task, current_artifacts or artifacts)
        latest_handoff = self._latest_handoff(current_artifacts)
        routing_decision = _latest_routing_decision(current_artifacts)
        role_jobs = self._role_jobs(task_id, artifacts=current_artifacts)
        round_execution = self.lifecycle.round_execution(task_id, current_round_id)
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
            round_execution=round_execution,
            goal_acceptance_records=self.list_goal_acceptance_records(task_id),
            presentation=build_relay_presentation(
                task=task,
                role_jobs=role_jobs,
                board=board,
                round_execution=round_execution,
                latest_handoff=(
                    latest_handoff.to_json_dict() if latest_handoff is not None else None
                ),
            ),
        )

    def build_marvis_relay_state(
        self,
        task_id: int,
        round_id: int | None = None,
    ) -> MarvisRelayState:
        detail = self.get_task_detail(task_id)
        target_round_id = int(round_id or detail.current_round_id or 1)
        if target_round_id != int(detail.current_round_id or 1):
            detail = self._task_detail_for_round_projection(detail, target_round_id)
        return build_marvis_relay_state(
            detail,
            round_id=target_round_id,
        )

    def _task_detail_for_round_projection(
        self,
        detail: RelayTaskDetail,
        round_id: int,
    ) -> RelayTaskDetail:
        target_round_id = int(round_id or 1)
        round_artifacts = _artifacts_for_round(detail.artifacts, target_round_id)
        round_status = self.lifecycle.round_status(detail.task.id, target_round_id)
        task = replace(detail.task, status=round_status)
        role_jobs = self._role_jobs_for_round(
            detail.task.id,
            target_round_id,
            artifacts=round_artifacts,
        )
        return RelayTaskDetail(
            task=task,
            board=self._latest_board(task, round_artifacts or detail.artifacts),
            role_jobs=role_jobs,
            artifacts=detail.artifacts,
            latest_handoff=self._latest_handoff(round_artifacts),
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
            routing_decision=_latest_routing_decision(round_artifacts),
            current_round_id=target_round_id,
            pending_inputs=detail.pending_inputs,
            round_execution=self.lifecycle.round_execution(detail.task.id, target_round_id),
            goal_acceptance_records=[
                record
                for record in detail.goal_acceptance_records
                if int(record.round_id) == target_round_id
            ],
            presentation=self._presentation_for_round(
                task=task,
                role_jobs=role_jobs,
                board=self._latest_board(task, round_artifacts or detail.artifacts),
                artifacts=round_artifacts,
                round_id=target_round_id,
            ),
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

    def record_goal_acceptance(
        self,
        task_id: int,
        *,
        round_id: int,
        implementation_artifact_id: int | None,
        implementation_run_id: int | None,
        verifier_artifact_id: int | None,
        verifier_role: str,
        test_declaration: dict[str, Any],
        test_execution: dict[str, Any],
        exit_code: int | None,
        status: str,
        evidence_status: str,
        reason: str = "",
    ) -> GoalAcceptanceRecord:
        """Persist one goal-verification attempt before task completion can use it.

        Attempts are append-only.  A retry receives a new ``attempt_no`` so a
        previously failed test remains available as evidence instead of being
        overwritten by a later pass.
        """

        clean_status = str(status or "not_run").strip()
        clean_evidence_status = str(evidence_status or "not_run").strip()
        allowed = {"passed", "failed", "not_run"}
        if clean_status not in allowed:
            raise ValueError(f"invalid goal acceptance status: {clean_status}")
        if clean_evidence_status not in allowed:
            raise ValueError(
                f"invalid goal acceptance evidence_status: {clean_evidence_status}"
            )
        clean_role = str(verifier_role or "").strip()
        if clean_role not in {"tester", "auditor"}:
            raise ValueError(f"invalid goal acceptance verifier role: {clean_role}")
        clean_round_id = _coerce_round_id(round_id)
        if clean_round_id <= 0:
            raise ValueError("goal acceptance requires a positive round_id")
        clean_implementation_artifact_id = _positive_int(implementation_artifact_id)
        clean_implementation_run_id = _positive_int(implementation_run_id)
        clean_verifier_artifact_id = _positive_int(verifier_artifact_id)
        if verifier_artifact_id is not None and clean_verifier_artifact_id is None:
            raise ValueError("invalid goal acceptance verifier artifact id")
        if clean_verifier_artifact_id is None:
            raise ValueError("goal acceptance requires a verifier artifact")
        self._assert_goal_acceptance_artifact(
            task_id,
            clean_verifier_artifact_id,
            round_id=clean_round_id,
            role=clean_role,
            artifact_type="test_report" if clean_role == "tester" else "audit_report",
        )
        if implementation_artifact_id is not None:
            if clean_implementation_artifact_id is None or clean_implementation_run_id is None:
                raise ValueError(
                    "bound goal acceptance requires implementation artifact and run ids"
                )
            implementation_payload = self._assert_goal_acceptance_artifact(
                task_id,
                clean_implementation_artifact_id,
                round_id=clean_round_id,
                role="implementer",
                artifact_type="implementation_report",
            )
            if _positive_int(implementation_payload.get("implementation_run_id")) != (
                clean_implementation_run_id
            ):
                raise ValueError(
                    "goal acceptance implementation_run_id does not match implementation artifact"
                )
        elif clean_evidence_status == "passed":
            raise ValueError("unbound goal acceptance cannot have passed evidence")
        row = self._ledger._conn.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) + 1 AS attempt_no
            FROM relay_goal_acceptance_records
            WHERE team_run_id = ?
              AND round_id = ?
              AND COALESCE(implementation_artifact_id, 0) = COALESCE(?, 0)
              AND verifier_role = ?
            """,
            (task_id, clean_round_id, clean_implementation_artifact_id, clean_role),
        ).fetchone()
        attempt_no = int(row["attempt_no"] if row is not None else 1)
        now = _now_text()
        cur = self._ledger._conn.execute(
            """
            INSERT INTO relay_goal_acceptance_records (
                team_run_id, round_id, implementation_artifact_id,
                implementation_run_id, verifier_artifact_id, verifier_role,
                attempt_no, test_declaration_json, test_execution_json,
                exit_code, status, evidence_status, reason, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                clean_round_id,
                clean_implementation_artifact_id,
                clean_implementation_run_id,
                clean_verifier_artifact_id,
                clean_role,
                attempt_no,
                json.dumps(test_declaration or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(test_execution or {}, ensure_ascii=False, sort_keys=True),
                exit_code,
                clean_status,
                clean_evidence_status,
                str(reason or ""),
                now,
                now,
            ),
        )
        self._ledger._conn.commit()
        saved = self._ledger._conn.execute(
            "SELECT * FROM relay_goal_acceptance_records WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        if saved is None:
            raise KeyError(f"unknown goal acceptance record id: {cur.lastrowid}")
        return _goal_acceptance_record_from_row(saved)

    def _assert_goal_acceptance_artifact(
        self,
        task_id: int,
        artifact_id: int,
        *,
        round_id: int,
        role: str,
        artifact_type: str,
    ) -> dict[str, Any]:
        """Return an artifact only when it belongs to this exact acceptance scope."""

        row = self._ledger._conn.execute(
            """
            SELECT artifact_type, payload_json FROM team_artifacts
            WHERE id = ? AND team_run_id = ?
            """,
            (artifact_id, task_id),
        ).fetchone()
        if row is None:
            raise ValueError("goal acceptance artifact does not belong to this task")
        payload = _relay_json_payload(row["payload_json"])
        if (_coerce_round_id(payload.get("round_id")) or 1) != int(round_id):
            raise ValueError("goal acceptance artifact does not belong to this round")
        if str(row["artifact_type"] or "") != artifact_type:
            raise ValueError("goal acceptance artifact type does not match verifier binding")
        if str(payload.get("relay_role") or payload.get("role") or "") != role:
            raise ValueError("goal acceptance artifact role does not match verifier binding")
        return payload

    def list_goal_acceptance_records(
        self,
        task_id: int,
        *,
        round_id: int | None = None,
    ) -> list[GoalAcceptanceRecord]:
        sql = "SELECT * FROM relay_goal_acceptance_records WHERE team_run_id = ?"
        params: list[Any] = [task_id]
        if round_id is not None:
            sql += " AND round_id = ?"
            params.append(int(round_id))
        sql += " ORDER BY id ASC"
        rows = self._ledger._conn.execute(sql, params).fetchall()
        return [_goal_acceptance_record_from_row(row) for row in rows]

    def annotate_goal_acceptance_artifact(
        self,
        task_id: int,
        artifact_id: int,
        record: GoalAcceptanceRecord,
    ) -> None:
        """Expose the durable record in the verifier artifact without mutating it.

        Existing task detail/work-log consumers already project artifact
        payloads.  Including the immutable acceptance summary there makes the
        `passed`/`failed`/`not_run` state visible even before a dedicated UI
        consumes ``goal_acceptance_records``.
        """

        row = self._ledger._conn.execute(
            """
            SELECT payload_json FROM team_artifacts
            WHERE id = ? AND team_run_id = ?
            """,
            (int(artifact_id), task_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown relay artifact id: {artifact_id}")
        payload = _relay_json_payload(row["payload_json"])
        payload["goal_acceptance"] = record.to_dict()
        self._ledger._conn.execute(
            "UPDATE team_artifacts SET payload_json = ? WHERE id = ? AND team_run_id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), int(artifact_id), task_id),
        )
        self._ledger._conn.commit()

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

    def current_round_id_readonly(
        self,
        task_id: int,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> int:
        """Read the current round without invoking backfill or legacy sync."""

        source_artifacts = artifacts if artifacts is not None else self._relay_artifacts(task_id)
        return self.lifecycle.current_round_id_readonly(
            task_id,
            fallback=_current_round_id(source_artifacts),
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

    def claim_pending_input_for_workspace(
        self,
        workspace: str,
        *,
        lease_owner: str,
        task_id: int | None = None,
        pending_input_id: int | None = None,
        queued_through_round_id: int | None = None,
        lease_seconds: int = 300,
    ) -> RelayPendingInputClaim | None:
        """Atomically lease the next eligible pending input for one workspace.

        The queue is deliberately selected by workspace, never by a global
        task order.  A lease row exists only while a worker owns the input;
        this lets the maintenance drain distinguish active work from durable
        historical input.  Expired rows are reclaimed by the next worker.

        ``task_id``/``queued_through_round_id`` narrow the claim for the
        terminal-round consumer.  Leaving them unset is useful to a dedicated
        workspace worker that scans ready work itself.
        """

        clean_workspace = str(workspace or "").strip()
        owner = str(lease_owner or "").strip()
        if not clean_workspace or not owner:
            return None
        seconds = max(1, int(lease_seconds or 300))
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=seconds)).isoformat()
        conn = self._ledger._conn
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            # A per-input lease alone permits two different tasks in the same
            # workspace to be consumed concurrently.  Claim a workspace-wide
            # fence first; ``BEGIN IMMEDIATE`` makes this check-and-claim
            # atomic across worker processes sharing the SQLite database.
            workspace_lock = conn.execute(
                """
                SELECT pending_input_id, lease_owner, lease_expires_at
                FROM relay_workspace_queue_locks
                WHERE workspace = ?
                """,
                (clean_workspace,),
            ).fetchone()
            if workspace_lock is not None:
                lock_expiry = str(workspace_lock["lease_expires_at"] or "")
                if _relay_lease_is_active(lock_expiry, now):
                    if owns_transaction:
                        conn.commit()
                    return None
                conn.execute(
                    "DELETE FROM relay_workspace_queue_locks WHERE workspace = ?",
                    (clean_workspace,),
                )

            # Rolling upgrades can have an old per-input lease without the
            # new workspace fence.  Treat it as a live fence rather than
            # starting another provider action in that workspace.
            legacy_active_lease = conn.execute(
                """
                SELECT lease_expires_at
                FROM relay_workspace_queue_leases
                WHERE workspace = ?
                  AND lease_owner != ''
                ORDER BY lease_expires_at ASC, pending_input_id ASC
                """,
                (clean_workspace,),
            ).fetchall()
            if any(
                _relay_lease_is_active(str(row["lease_expires_at"] or ""), now)
                for row in legacy_active_lease
            ):
                if owns_transaction:
                    conn.commit()
                return None
            rows = conn.execute(
                """
                SELECT pending.*,
                       lease.pending_input_id AS lease_pending_input_id,
                       lease.workspace AS lease_workspace,
                       lease.lease_owner AS current_lease_owner,
                       lease.lease_expires_at AS current_lease_expires_at,
                       lease.attempt_count AS current_attempt_count
                FROM relay_pending_inputs AS pending
                LEFT JOIN relay_workspace_queue_leases AS lease
                    ON lease.pending_input_id = pending.id
                WHERE pending.status = 'pending'
                ORDER BY pending.created_at ASC, pending.id ASC
                """
            ).fetchall()
            selected: Any | None = None
            selected_workspace = ""
            for row in rows:
                candidate_task_id = int(row["team_run_id"])
                if task_id is not None and candidate_task_id != int(task_id):
                    continue
                if pending_input_id is not None and int(row["id"]) != int(pending_input_id):
                    continue
                if (
                    queued_through_round_id is not None
                    and int(row["queued_after_round_id"] or 1)
                    > int(queued_through_round_id)
                ):
                    continue
                lease_workspace = str(row["lease_workspace"] or "").strip()
                candidate_workspace = lease_workspace or self._workspace_for_task_locked(
                    candidate_task_id
                )
                if candidate_workspace != clean_workspace:
                    continue
                previous_owner = str(row["current_lease_owner"] or "").strip()
                previous_expiry = str(row["current_lease_expires_at"] or "").strip()
                if previous_owner and _relay_lease_is_active(previous_expiry, now):
                    continue
                selected = row
                selected_workspace = candidate_workspace
                break
            if selected is None:
                if owns_transaction:
                    conn.commit()
                return None
            pending_id = int(selected["id"])
            existing_lease_id = selected["lease_pending_input_id"]
            if existing_lease_id is not None:
                cur = conn.execute(
                    """
                    UPDATE relay_workspace_queue_leases
                    SET workspace = ?,
                        lease_owner = ?,
                        lease_expires_at = ?,
                        attempt_count = attempt_count + 1,
                        last_error = '',
                        updated_at = ?
                    WHERE pending_input_id = ?
                    """,
                    (
                        selected_workspace,
                        owner,
                        expires_at,
                        now_text,
                        pending_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO relay_workspace_queue_leases (
                        pending_input_id, team_run_id, workspace, lease_owner,
                        lease_expires_at, attempt_count, last_error,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, '', ?, ?)
                    """,
                    (
                        pending_id,
                        int(selected["team_run_id"]),
                        selected_workspace,
                        owner,
                        expires_at,
                        now_text,
                        now_text,
                    ),
                )
            if int(cur.rowcount or 0) != 1:
                if owns_transaction:
                    conn.commit()
                return None
            conn.execute(
                """
                INSERT INTO relay_workspace_queue_locks (
                    workspace, pending_input_id, team_run_id, lease_owner,
                    lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selected_workspace,
                    pending_id,
                    int(selected["team_run_id"]),
                    owner,
                    expires_at,
                    now_text,
                    now_text,
                ),
            )
            row = conn.execute(
                """
                SELECT pending.*, lease.workspace, lease.lease_owner,
                       lease.lease_expires_at, lease.attempt_count
                FROM relay_pending_inputs AS pending
                JOIN relay_workspace_queue_leases AS lease
                    ON lease.pending_input_id = pending.id
                WHERE pending.id = ?
                """,
                (pending_id,),
            ).fetchone()
            if owns_transaction:
                conn.commit()
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise
        if row is None:
            return None
        return RelayPendingInputClaim(
            pending_input=_pending_input_from_row(row),
            workspace=str(row["workspace"] or selected_workspace),
            lease_owner=str(row["lease_owner"] or owner),
            lease_expires_at=str(row["lease_expires_at"] or expires_at),
            attempt_count=int(row["attempt_count"] or 1),
        )

    def renew_pending_input_claim(
        self,
        claim: RelayPendingInputClaim,
        *,
        lease_seconds: int = 300,
    ) -> RelayPendingInputClaim | None:
        """Extend an owned lease before a provider-facing side effect."""

        seconds = max(1, int(lease_seconds or 300))
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=seconds)).isoformat()
        conn = self._ledger._conn
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                UPDATE relay_workspace_queue_leases
                SET lease_expires_at = ?, updated_at = ?
                WHERE pending_input_id = ?
                  AND team_run_id = ?
                  AND workspace = ?
                  AND lease_owner = ?
                  AND lease_expires_at > ?
                """,
                (
                    expires_at,
                    now_text,
                    claim.pending_input.id,
                    claim.pending_input.task_id,
                    claim.workspace,
                    claim.lease_owner,
                    now_text,
                ),
            )
            if int(cur.rowcount or 0) == 1:
                lock = conn.execute(
                    """
                    UPDATE relay_workspace_queue_locks
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE workspace = ?
                      AND pending_input_id = ?
                      AND team_run_id = ?
                      AND lease_owner = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        expires_at,
                        now_text,
                        claim.workspace,
                        claim.pending_input.id,
                        claim.pending_input.task_id,
                        claim.lease_owner,
                        now_text,
                    ),
                )
                if int(lock.rowcount or 0) != 1:
                    raise RuntimeError("pending input workspace lock is no longer owned")
            if owns_transaction:
                conn.commit()
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise
        if int(cur.rowcount or 0) != 1:
            return None
        return RelayPendingInputClaim(
            pending_input=claim.pending_input,
            workspace=claim.workspace,
            lease_owner=claim.lease_owner,
            lease_expires_at=expires_at,
            attempt_count=claim.attempt_count,
        )

    def release_pending_input_claim(
        self,
        claim: RelayPendingInputClaim,
        *,
        error: str = "",
    ) -> bool:
        """Drop an owned lease after a recoverable failure.

        We intentionally delete rather than retain a terminal lease row:
        pending input remains the retry record, while the absence of a lease
        means no worker is live.  This also keeps maintenance drain truthful.
        """

        del error  # The pending record is the durable retry source; no live lease remains.
        conn = self._ledger._conn
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                DELETE FROM relay_workspace_queue_leases
                WHERE pending_input_id = ?
                  AND team_run_id = ?
                  AND workspace = ?
                  AND lease_owner = ?
                """,
                (
                    claim.pending_input.id,
                    claim.pending_input.task_id,
                    claim.workspace,
                    claim.lease_owner,
                ),
            )
            if int(cur.rowcount or 0) == 1:
                conn.execute(
                    """
                    DELETE FROM relay_workspace_queue_locks
                    WHERE workspace = ?
                      AND pending_input_id = ?
                      AND team_run_id = ?
                      AND lease_owner = ?
                    """,
                    (
                        claim.workspace,
                        claim.pending_input.id,
                        claim.pending_input.task_id,
                        claim.lease_owner,
                    ),
                )
            if owns_transaction:
                conn.commit()
            return int(cur.rowcount or 0) == 1
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise

    def consume_pending_input_claim(
        self,
        claim: RelayPendingInputClaim,
        *,
        consumed_round_id: int,
    ) -> RelayPendingInput:
        """Atomically mark a claimed input consumed and release its lease."""

        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        conn = self._ledger._conn
        owns_transaction = not conn.in_transaction
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            lease = conn.execute(
                """
                SELECT 1
                FROM relay_workspace_queue_leases
                WHERE pending_input_id = ?
                  AND team_run_id = ?
                  AND workspace = ?
                  AND lease_owner = ?
                  AND lease_expires_at > ?
                """,
                (
                    claim.pending_input.id,
                    claim.pending_input.task_id,
                    claim.workspace,
                    claim.lease_owner,
                    now_text,
                ),
            ).fetchone()
            if lease is None:
                raise RuntimeError("pending input claim is no longer owned")
            updated = conn.execute(
                """
                UPDATE relay_pending_inputs
                SET status = 'consumed',
                    updated_at = ?,
                    consumed_round_id = ?
                WHERE id = ?
                  AND team_run_id = ?
                  AND status = 'pending'
                """,
                (
                    now_text,
                    int(consumed_round_id),
                    claim.pending_input.id,
                    claim.pending_input.task_id,
                ),
            )
            if int(updated.rowcount or 0) != 1:
                raise RuntimeError("pending input is no longer consumable")
            conn.execute(
                """
                DELETE FROM relay_workspace_queue_leases
                WHERE pending_input_id = ? AND lease_owner = ?
                """,
                (claim.pending_input.id, claim.lease_owner),
            )
            conn.execute(
                """
                DELETE FROM relay_workspace_queue_locks
                WHERE workspace = ?
                  AND pending_input_id = ?
                  AND team_run_id = ?
                  AND lease_owner = ?
                """,
                (
                    claim.workspace,
                    claim.pending_input.id,
                    claim.pending_input.task_id,
                    claim.lease_owner,
                ),
            )
            row = conn.execute(
                "SELECT * FROM relay_pending_inputs WHERE id = ?",
                (claim.pending_input.id,),
            ).fetchone()
            if owns_transaction:
                conn.commit()
        except Exception:
            if owns_transaction:
                conn.rollback()
            raise
        if row is None:
            raise RuntimeError("pending input disappeared while being consumed")
        return _pending_input_from_row(row)

    def pending_input_transition_artifact(
        self,
        task_id: int,
        pending_input_id: int,
    ) -> dict[str, Any] | None:
        """Return the durable marker that binds a pending input to its turn."""

        for artifact in reversed(self._relay_artifacts(task_id)):
            if str(artifact.get("artifact_type") or "") != "pending_input_transition":
                continue
            if int(artifact.get("pending_input_id") or 0) == int(pending_input_id):
                return artifact
        return None

    def followup_round_for_transition_artifact(
        self,
        task_id: int,
        transition_artifact_id: int,
    ) -> int | None:
        row = self._ledger._conn.execute(
            """
            SELECT round_id
            FROM relay_rounds
            WHERE team_run_id = ? AND trigger_artifact_id = ?
            ORDER BY round_id ASC
            LIMIT 1
            """,
            (task_id, transition_artifact_id),
        ).fetchone()
        return int(row["round_id"]) if row is not None else None

    def pending_input_followup_artifact(
        self,
        task_id: int,
        pending_input_id: int,
    ) -> dict[str, Any] | None:
        """Find the idempotency record for a pending-input follow-up."""

        for artifact in reversed(self._relay_artifacts(task_id)):
            if str(artifact.get("artifact_type") or "") != "user_followup":
                continue
            if int(artifact.get("pending_input_id") or 0) == int(pending_input_id):
                return artifact
        return None

    def _workspace_for_task_locked(self, task_id: int) -> str:
        team_run = self._ledger.get_team_run(task_id)
        if team_run is None or str(getattr(team_run, "route", "") or "") != "relay":
            return ""
        workspace = str(self._task_from_run(team_run).workspace or "").strip()
        # A legacy task may not have a workspace.  Keep it isolated rather
        # than putting every such task into one global queue partition.
        return workspace or f"relay-task:{task_id}"

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

    def find_relay_attempt_by_agent_run_id(
        self,
        agent_run_id: int,
    ) -> tuple[int, str, int] | None:
        """Locate the historical Relay attempt that owns a provider run.

        The team-job table is only a current-round mirror.  Looking there
        first can map a late completion from a superseded round onto the role
        now displayed in a later round.  Relay attempts retain the immutable
        provider identity and are therefore the authoritative routing source.
        """

        attempt = self.lifecycle.attempt_by_agent_run_id(agent_run_id)
        if attempt is not None:
            return attempt.team_run_id, attempt.role, attempt.round_id
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
        return int(row["task_id"]), str(row["role"]), self.current_round_id_readonly(
            int(row["task_id"])
        )

    def find_role_by_agent_run_id(self, agent_run_id: int) -> tuple[int, str] | None:
        """Compatibility projection of :meth:`find_relay_attempt_by_agent_run_id`."""

        mapping = self.find_relay_attempt_by_agent_run_id(agent_run_id)
        if mapping is None:
            return None
        task_id, role, _round_id = mapping
        return task_id, role

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

    def _role_jobs_for_round(
        self,
        task_id: int,
        round_id: int,
        *,
        artifacts: list[dict[str, Any]],
    ) -> list[RelayRoleJob]:
        attempts = self.lifecycle.attempts_for_round(task_id, round_id)
        jobs = self._role_jobs(task_id, artifacts=artifacts)
        projected: list[RelayRoleJob] = []
        for job in jobs:
            attempt = attempts.get(job.role)
            if attempt is None:
                projected.append(replace(job, status="idle"))
                continue
            projected.append(
                replace(
                    job,
                    status=attempt.status,
                    provider=attempt.provider or job.provider,
                    native_session_id=attempt.native_session_id or job.native_session_id,
                    agent_run_id=(
                        attempt.agent_run_id
                        if attempt.agent_run_id is not None
                        else job.agent_run_id
                    ),
                    turn_id=attempt.turn_id or job.turn_id,
                    active_turn_id=attempt.active_turn_id or job.active_turn_id,
                )
            )
        return projected

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

    def _presentation_for_round(
        self,
        *,
        task: RelayTask,
        role_jobs: list[RelayRoleJob],
        board: RelayBoard,
        artifacts: list[dict[str, Any]],
        round_id: int,
    ) -> RelayPresentation:
        latest_handoff = self._latest_handoff(artifacts)
        return build_relay_presentation(
            task=task,
            role_jobs=role_jobs,
            board=board,
            round_execution=self.lifecycle.round_execution(task.id, round_id),
            latest_handoff=(
                latest_handoff.to_json_dict() if latest_handoff is not None else None
            ),
        )

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


def _goal_acceptance_record_from_row(row: Any) -> GoalAcceptanceRecord:
    return GoalAcceptanceRecord(
        id=int(row["id"]),
        task_id=int(row["team_run_id"]),
        round_id=int(row["round_id"]),
        implementation_artifact_id=(
            int(row["implementation_artifact_id"])
            if row["implementation_artifact_id"] is not None
            else None
        ),
        implementation_run_id=(
            int(row["implementation_run_id"])
            if row["implementation_run_id"] is not None
            else None
        ),
        verifier_artifact_id=(
            int(row["verifier_artifact_id"])
            if row["verifier_artifact_id"] is not None
            else None
        ),
        verifier_role=str(row["verifier_role"] or ""),
        attempt_no=int(row["attempt_no"] or 1),
        test_declaration=_relay_json_payload(row["test_declaration_json"]),
        test_execution=_relay_json_payload(row["test_execution_json"]),
        exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
        status=str(row["status"] or "not_run"),
        evidence_status=str(row["evidence_status"] or "not_run"),
        reason=str(row["reason"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _relay_lease_is_active(value: str, now: datetime) -> bool:
    """Treat malformed lease times as expired so a crashed worker is recoverable."""

    try:
        expires_at = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


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


def _runtime_event_id_from_payload(payload: dict[str, Any]) -> int | None:
    value = payload.get("runtime_event_id")
    if value in (None, ""):
        native_event = payload.get("native_event")
        if isinstance(native_event, dict):
            value = native_event.get("id")
    try:
        event_id = int(value or 0)
    except (TypeError, ValueError):
        return None
    return event_id or None


def _relay_stream_payload_for_storage(
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if event_type not in {"role.output_delta", "role.native_event"}:
        return dict(payload)
    runtime_event_id = _runtime_event_id_from_payload(payload)
    if runtime_event_id is None:
        return dict(payload)
    keep = {
        "role",
        "agent_run_id",
        "runtime_event_id",
        "round_id",
        "kind",
        "itemId",
        "item_id",
        "stream_key",
        "native_message_id",
        "message_id",
        "native_turn_id",
        "turnId",
        "turn_id",
    }
    stored = {key: payload[key] for key in keep if key in payload}
    native_event = payload.get("native_event")
    if isinstance(native_event, dict):
        stored.setdefault("kind", native_event.get("kind"))
        stored.setdefault("agent_run_id", native_event.get("agent_run_id"))
        stored.setdefault("runtime_event_id", native_event.get("id"))
        native_payload = native_event.get("payload")
        if isinstance(native_payload, dict):
            for key in (
                "itemId",
                "item_id",
                "stream_key",
                "native_message_id",
                "message_id",
                "native_turn_id",
                "turnId",
                "turn_id",
                "display_source",
            ):
                if key in native_payload:
                    stored.setdefault(key, native_payload[key])
    stored["runtime_event_id"] = runtime_event_id
    return {key: value for key, value in stored.items() if value not in (None, "")}


def _runtime_event_from_row(row: Any) -> RuntimeEvent:
    return RuntimeEvent(
        id=int(row["id"]),
        schema_version=int(row["schema_version"]),
        event_type=str(row["event_type"]),
        aggregate_type=str(row["aggregate_type"]),
        aggregate_id=str(row["aggregate_id"]),
        conversation_id=row["conversation_id"],
        orchestration_run_id=row["orchestration_run_id"],
        agent_run_id=row["agent_run_id"],
        task_id=row["task_id"],
        correlation_id=str(row["correlation_id"]),
        causation_id=row["causation_id"],
        source=str(row["source"]),
        actor=str(row["actor"]),
        visibility=str(row["visibility"]),
        payload=json.loads(str(row["payload_json"] or "{}")),
        occurred_at=str(row["occurred_at"]),
    )


def _runtime_payload_text(payload: dict[str, Any]) -> str:
    for key in ("delta", "text", "summary", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _coerce_round_id(value: Any) -> int:
    try:
        round_id = int(value)
    except (TypeError, ValueError):
        return 0
    return round_id if round_id > 0 else 0


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return None
    return candidate if candidate > 0 else None


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


def _latest_activity_at(task: RelayTask, artifacts: list[dict[str, Any]]) -> str:
    created_at_values = [
        str(artifact.get("created_at") or "")
        for artifact in artifacts
        if str(artifact.get("created_at") or "").strip()
    ]
    return max(created_at_values) if created_at_values else task.updated_at


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
