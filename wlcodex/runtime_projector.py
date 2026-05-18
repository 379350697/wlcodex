"""Runtime projector — updates compatibility tables from runtime events.

Projections update existing mutable tables (agent_runs,
orchestration_runs, usage_events, task_events) but never mutate
runtime_events.  Projection failures do not block event append.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)
# ---------------------------------------------------------------------------
# Projection-level terminal state sets (DB column values, NOT runtime names).
#
# The runtime-state module uses spec names ("completed", "cancelled").
# The projection tables use the legacy enum values ("done", "aborted", "passed").
# These two sets must stay separate — the projector guards against the DB
# values it actually writes, not the abstract runtime state names.
# ---------------------------------------------------------------------------

_DB_AGENT_TERMINAL = frozenset({"done", "failed", "aborted"})
_DB_ORCH_TERMINAL = frozenset({"passed", "failed", "aborted"})
_DB_APPROVAL_TERMINAL = frozenset({"approved", "denied", "expired", "cancelled"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Projection row helpers — lightweight lookups / upserts
# ---------------------------------------------------------------------------


def _row_exists(conn: sqlite3.Connection, table: str, row_id: int) -> bool:
    cur = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,))
    return cur.fetchone() is not None


def _current_status(
    conn: sqlite3.Connection, table: str, row_id: int
) -> str | None:
    cur = conn.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def _is_approved_decision(decision: object) -> bool:
    if isinstance(decision, dict):
        return bool(decision)
    normalized = str(decision).strip().lower()
    return normalized in {"approve", "approved", "accept", "accepted", "acceptforsession"}


def _payload_int(payload: dict[str, object], *keys: str) -> int:
    for key in keys:
        if key not in payload:
            continue
        try:
            return int(payload.get(key, 0))
        except (TypeError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------------------
# Projector
# ---------------------------------------------------------------------------


class RuntimeProjector:
    """Projects runtime events into legacy compatibility tables.

    Never mutates runtime_events.  Projection failures are caught
    internally and may emit ``projection.failed`` events through the
    optional *store* reference — they never propagate to the caller.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        store: object | None = None,
    ) -> None:
        self._conn = conn
        self._store = store  # optional RuntimeEventStore for failure events
        self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(self, event: RuntimeEvent) -> None:
        """Project a single *event* into compatibility tables.

        Failures are caught and do not propagate.  If a store is
        configured, a ``projection.failed`` event is appended.
        """
        try:
            self._project(event)
        except Exception as exc:
            self._emit_failure(event, exc)

    def rebuild(self, store: object | None = None) -> int:
        """Rebuild all projections from event id 0.

        Reads every event from *store* (or the configured store) and
        replays them through :meth:`apply`.  Returns the number of
        events replayed.
        """
        s = store or self._store
        if s is None:
            raise RuntimeError("No RuntimeEventStore available for rebuild")

        # We reach into the store's connection to read all events.
        conn = getattr(s, "_conn", None)
        if conn is None:
            raise RuntimeError("store has no _conn attribute")

        rows = conn.execute(
            "SELECT * FROM runtime_events ORDER BY id ASC"
        ).fetchall()

        count = 0
        for row in rows:
            event = _row_to_runtime_event(row)
            self.apply(event)
            count += 1

        return count

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _project(self, event: RuntimeEvent) -> None:
        etype = event.event_type

        # --- Agent lifecycle → agent_runs ---
        if etype in (
            EventType.AGENT_RUN_QUEUED,
            EventType.AGENT_RUN_STARTED,
            EventType.AGENT_RUN_ACTIVITY,
            EventType.AGENT_RUN_HEARTBEAT,
            EventType.AGENT_RUN_WAITING_FOR_APPROVAL,
            EventType.AGENT_RUN_COMPLETED,
            EventType.AGENT_RUN_FAILED,
            EventType.AGENT_RUN_TIMED_OUT,
            EventType.AGENT_RUN_ORPHANED,
        ):
            self._project_agent_event(event)

        # --- Orchestration lifecycle → orchestration_runs ---
        if etype in (
            EventType.RUN_REQUESTED,
            EventType.RUN_STARTED,
            EventType.RUN_PHASE_CHANGED,
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
            EventType.RUN_CANCEL_REQUESTED,
        ):
            self._project_orchestration_event(event)
            self._project_task_status_event(event)

        # --- Usage → usage_events ---
        if etype == EventType.MODEL_USAGE_UPDATED:
            self._project_usage_event(event)

        # --- Approval → approval_requests ---
        if etype in (
            EventType.APPROVAL_REQUESTED,
            EventType.APPROVAL_RESOLVED,
            EventType.APPROVAL_EXPIRED,
        ):
            self._project_approval_event(event)

        # --- Important events → task_events (compat summary) ---
        if etype in _TASK_EVENT_COMPAT_TYPES:
            self._project_task_event(event)

        # --- Verification events → orchestration decisions ---
        if etype in (
            EventType.VERIFICATION_DECISION_RECORDED,
            EventType.VERIFICATION_RETRY_REQUESTED,
        ):
            self._project_verification_event(event)

    def _project_task_status_event(self, event: RuntimeEvent) -> None:
        task_id = event.task_id
        if task_id is None or not _row_exists(self._conn, "tasks", task_id):
            return

        status: str | None = None
        phase = ""
        summary = ""
        error = ""
        if event.event_type in (EventType.RUN_STARTED, EventType.RUN_PHASE_CHANGED):
            status = "running"
            phase = str(event.payload.get("phase", ""))
            summary = phase
        elif event.event_type == EventType.RUN_COMPLETED:
            if self._has_pass_verification(event):
                status = "done"
                phase = "completed"
                summary = "runtime run completed"
            else:
                status = "failed"
                phase = "failed"
                error = "run_completed_without_verification_pass"
        elif event.event_type == EventType.RUN_FAILED:
            status = "failed"
            phase = "failed"
            error = str(event.payload.get("reason", "runtime run failed"))
        elif event.event_type == EventType.RUN_CANCELLED:
            status = "aborted"
            phase = "cancelled"
            summary = "runtime run cancelled"

        if status is None:
            return
        self._conn.execute(
            """
            UPDATE tasks
            SET status = ?,
                last_phase = COALESCE(NULLIF(?, ''), last_phase),
                last_summary = COALESCE(NULLIF(?, ''), last_summary),
                last_error = COALESCE(NULLIF(?, ''), last_error),
                updated_at = ?
            WHERE id = ?
            """,
            (status, phase, summary, error, _now(), task_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Agent projection
    # ------------------------------------------------------------------

    def _project_agent_event(self, event: RuntimeEvent) -> None:
        agent_run_id = event.agent_run_id
        if agent_run_id is None:
            return

        payload = event.payload
        etype = event.event_type

        if etype == EventType.AGENT_RUN_QUEUED:
            self._upsert_agent_run(event)
            return

        # Terminal-state guard: never downgrade a terminal row.
        current = _current_status(self._conn, "agent_runs", agent_run_id)
        if current in _DB_AGENT_TERMINAL and etype not in (
            EventType.AGENT_RUN_COMPLETED,
            EventType.AGENT_RUN_FAILED,
            EventType.AGENT_RUN_TIMED_OUT,
            EventType.AGENT_RUN_ORPHANED,
        ):
            return

        if etype == EventType.AGENT_RUN_STARTED:
            self._upsert_agent_run(event)
            self._conn.execute(
                "UPDATE agent_runs SET status = 'running', updated_at = ? WHERE id = ?",
                (_now(), agent_run_id),
            )
            ext_sid = payload.get("external_session_id")
            if ext_sid:
                self._conn.execute(
                    "UPDATE agent_runs SET external_session_id = ? WHERE id = ?",
                    (str(ext_sid), agent_run_id),
                )

        elif etype == EventType.AGENT_RUN_WAITING_FOR_APPROVAL:
            self._upsert_agent_run(event)
            self._conn.execute(
                "UPDATE agent_runs SET status = ?, updated_at = ? WHERE id = ?",
                ("running", _now(), agent_run_id),
            )

        elif etype in (EventType.AGENT_RUN_ACTIVITY, EventType.AGENT_RUN_HEARTBEAT):
            # Refresh idle — keeps row in running state.
            self._upsert_agent_run(event)

        elif etype == EventType.AGENT_RUN_COMPLETED:
            self._upsert_agent_run(event)
            summary = str(payload.get("summary", payload.get("completion_summary", "")))
            self._conn.execute(
                "UPDATE agent_runs SET status = 'done', completion_summary = ?, updated_at = ? WHERE id = ?",
                (summary, _now(), agent_run_id),
            )

        elif etype == EventType.AGENT_RUN_FAILED:
            self._upsert_agent_run(event)
            self._conn.execute(
                "UPDATE agent_runs SET status = 'failed', updated_at = ? WHERE id = ?",
                (_now(), agent_run_id),
            )

        elif etype == EventType.AGENT_RUN_TIMED_OUT:
            self._upsert_agent_run(event)
            self._conn.execute(
                "UPDATE agent_runs SET status = 'failed', updated_at = ? WHERE id = ?",
                (_now(), agent_run_id),
            )

        elif etype == EventType.AGENT_RUN_ORPHANED:
            self._upsert_agent_run(event)
            self._conn.execute(
                "UPDATE agent_runs SET status = 'failed', updated_at = ? WHERE id = ?",
                (_now(), agent_run_id),
            )

        self._conn.commit()

    def _upsert_agent_run(self, event: RuntimeEvent) -> None:
        """Create the agent_runs row if it does not exist yet."""
        agent_run_id = event.agent_run_id
        if agent_run_id is None:
            return
        if _row_exists(self._conn, "agent_runs", agent_run_id):
            return

        payload = event.payload
        now = _now()
        self._conn.execute(
            """
            INSERT INTO agent_runs (
                id, conversation_id, agent, role, status,
                hidden_task_id, external_session_id,
                prompt_packet_summary, token_input, token_output,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, '', 0, 0, ?, ?)
            """,
            (
                agent_run_id,
                event.conversation_id or 0,
                str(payload.get("agent", event.actor)),
                str(payload.get("role", "")),
                event.task_id,
                str(payload.get("external_session_id", "")),
                now,
                now,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Orchestration projection
    # ------------------------------------------------------------------

    def _project_orchestration_event(self, event: RuntimeEvent) -> None:
        orch_run_id = event.orchestration_run_id
        if orch_run_id is None:
            return

        payload = event.payload
        etype = event.event_type

        # Terminal-state guard.
        current = _current_status(self._conn, "orchestration_runs", orch_run_id)
        if current in _DB_ORCH_TERMINAL and etype not in (
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
        ):
            return

        if etype == EventType.RUN_REQUESTED:
            self._upsert_orchestration_run(event)
            goal = str(payload.get("goal", ""))
            max_rounds = int(payload.get("max_verify_rounds", 3))
            self._conn.execute(
                "UPDATE orchestration_runs SET goal = ?, max_verify_rounds = ?, updated_at = ? WHERE id = ?",
                (goal, max_rounds, _now(), orch_run_id),
            )

        elif etype == EventType.RUN_STARTED:
            self._upsert_orchestration_run(event)
            phase = str(payload.get("phase", "running_analysis"))
            self._conn.execute(
                "UPDATE orchestration_runs SET status = 'running', current_step = ?, updated_at = ? WHERE id = ?",
                (phase, _now(), orch_run_id),
            )

        elif etype == EventType.RUN_PHASE_CHANGED:
            self._upsert_orchestration_run(event)
            phase = str(payload.get("phase", ""))
            codex = str(payload.get("codex_analysis", ""))
            claude = str(payload.get("claude_summary", ""))
            vrfy = int(payload.get("verify_round", -1))

            sets = ["current_step = ?", "updated_at = ?"]
            params: list[object] = [phase, _now()]
            if codex:
                sets.append("last_codex_analysis = ?")
                params.append(codex)
            if claude:
                sets.append("last_claude_summary = ?")
                params.append(claude)
            if vrfy >= 0:
                sets.append("verify_round = ?")
                params.append(vrfy)
            params.append(orch_run_id)

            self._conn.execute(
                f"UPDATE orchestration_runs SET {', '.join(sets)} WHERE id = ?",
                params,
            )

        elif etype == EventType.RUN_COMPLETED:
            self._upsert_orchestration_run(event)
            if not self._has_pass_verification(event):
                self._conn.execute(
                    """
                    UPDATE orchestration_runs
                    SET status = 'failed',
                        current_step = 'failed',
                        last_verification_result = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    ("run_completed_without_verification_pass", _now(), orch_run_id),
                )
                self._conn.commit()
                return
            self._conn.execute(
                "UPDATE orchestration_runs SET status = 'passed', current_step = 'completed', updated_at = ? WHERE id = ?",
                (_now(), orch_run_id),
            )

        elif etype == EventType.RUN_FAILED:
            self._upsert_orchestration_run(event)
            self._conn.execute(
                "UPDATE orchestration_runs SET status = 'failed', updated_at = ? WHERE id = ?",
                (_now(), orch_run_id),
            )

        elif etype == EventType.RUN_CANCELLED:
            self._upsert_orchestration_run(event)
            self._conn.execute(
                "UPDATE orchestration_runs SET status = 'aborted', updated_at = ? WHERE id = ?",
                (_now(), orch_run_id),
            )

        elif etype == EventType.RUN_CANCEL_REQUESTED:
            self._upsert_orchestration_run(event)

        self._conn.commit()

    def _has_pass_verification(self, event: RuntimeEvent) -> bool:
        orch_run_id = event.orchestration_run_id
        if orch_run_id is None:
            return False

        row = self._conn.execute(
            "SELECT last_verification_result FROM orchestration_runs WHERE id = ?",
            (orch_run_id,),
        ).fetchone()
        if row is not None and str(row["last_verification_result"]) == "pass":
            return True

        if event.id:
            row = self._conn.execute(
                """
                SELECT 1 FROM runtime_events
                WHERE orchestration_run_id = ?
                  AND event_type = ?
                  AND json_extract(payload_json, '$.decision') = 'pass'
                  AND id <= ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (orch_run_id, EventType.VERIFICATION_DECISION_RECORDED, event.id),
            ).fetchone()
            return row is not None

        return False

    def _upsert_orchestration_run(self, event: RuntimeEvent) -> None:
        orch_run_id = event.orchestration_run_id
        if orch_run_id is None:
            return
        if _row_exists(self._conn, "orchestration_runs", orch_run_id):
            return

        payload = event.payload
        now = _now()
        self._conn.execute(
            """
            INSERT INTO orchestration_runs (
                id, conversation_id, goal, status, current_step,
                verify_round, max_verify_rounds,
                last_codex_analysis, last_claude_summary, last_verification_result,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'running', '', 0, 3, '', '', '', ?, ?)
            """,
            (
                orch_run_id,
                event.conversation_id or 0,
                str(payload.get("goal", "")),
                now,
                now,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Usage projection
    # ------------------------------------------------------------------

    def _project_usage_event(self, event: RuntimeEvent) -> None:
        payload = event.payload
        metadata = payload.get("metadata", {})
        if isinstance(metadata, dict):
            metadata_json = json.dumps(metadata, ensure_ascii=False)
        elif isinstance(metadata, str):
            metadata_json = metadata
        else:
            metadata_json = "{}"

        input_tokens = _payload_int(payload, "input_tokens", "inputTokens")
        output_tokens = _payload_int(payload, "output_tokens", "outputTokens")
        cached = _payload_int(payload, "cached_input_tokens", "cachedInputTokens")
        reasoning = _payload_int(
            payload, "reasoning_output_tokens", "reasoningOutputTokens"
        )
        total = _payload_int(payload, "total_tokens", "totalTokens")
        if total == 0:
            total = input_tokens + output_tokens
        latency = _payload_int(payload, "latency_ms", "latencyMs")

        self._conn.execute(
            """
            INSERT INTO usage_events (
                created_at, conversation_id, orchestration_run_id, agent_run_id,
                task_id, agent, role, phase, request_kind, request_index, model,
                external_thread_id, external_turn_id, external_session_id,
                status, source, input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens,
                workflow_overhead_input_tokens, workflow_overhead_output_tokens,
                latency_ms, metadata_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                _now(),
                event.conversation_id,
                event.orchestration_run_id,
                event.agent_run_id,
                event.task_id,
                str(payload.get("agent", event.actor)),
                str(payload.get("role", "")),
                str(payload.get("phase", "")),
                str(payload.get("request_kind", "")),
                int(payload.get("request_index", 0)),
                str(payload.get("model", "")),
                str(payload.get("external_thread_id", "")),
                str(payload.get("external_turn_id", "")),
                str(payload.get("external_session_id", "")),
                str(payload.get("status", "")),
                str(payload.get("source", "derived")),
                input_tokens,
                cached,
                output_tokens,
                reasoning,
                total,
                int(payload.get("workflow_overhead_input_tokens", 0)),
                int(payload.get("workflow_overhead_output_tokens", 0)),
                latency,
                metadata_json,
            ),
        )

        # Also update token totals on the agent_runs row.
        if event.agent_run_id is not None:
            self._conn.execute(
                "UPDATE agent_runs SET token_input = token_input + ?, token_output = token_output + ?, updated_at = ? WHERE id = ?",
                (input_tokens, output_tokens, _now(), event.agent_run_id),
            )

        self._conn.commit()

    # ------------------------------------------------------------------
    # Approval projection
    # ------------------------------------------------------------------

    def _project_approval_event(self, event: RuntimeEvent) -> None:
        payload = event.payload
        etype = event.event_type
        task_id = event.task_id
        if task_id is None:
            return

        codex_request_id = str(payload.get("codex_request_id", event.aggregate_id))

        if etype == EventType.APPROVAL_REQUESTED:
            kind = str(payload.get("kind", "command"))
            summary = str(
                payload.get("summary")
                or payload.get("reason")
                or payload.get("command")
                or payload.get("kind")
                or ""
            )
            cmd = str(payload.get("command", payload.get("command_json", "{}")))
            self._conn.execute(
                """
                INSERT OR IGNORE INTO approval_requests (
                    task_id, codex_request_id, codex_item_id, codex_turn_id,
                    kind, summary, command_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    task_id,
                    codex_request_id,
                    str(payload.get("codex_item_id", "")),
                    str(payload.get("codex_turn_id", "")),
                    kind,
                    summary,
                    cmd,
                    _now(),
                ),
            )

        elif etype == EventType.APPROVAL_RESOLVED:
            decision = payload.get("decision", "")
            resolver = str(payload.get("resolver", ""))
            status = "approved" if _is_approved_decision(decision) else "denied"
            self._conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, resolution = ?, resolved_at = ?
                WHERE task_id = ? AND codex_request_id = ? AND status = 'pending'
                """,
                (status, resolver, _now(), task_id, codex_request_id),
            )

        elif etype == EventType.APPROVAL_EXPIRED:
            self._conn.execute(
                """
                UPDATE approval_requests
                SET status = 'expired', resolved_at = ?
                WHERE task_id = ? AND codex_request_id = ? AND status = 'pending'
                """,
                (_now(), task_id, codex_request_id),
            )

        self._conn.commit()

    # ------------------------------------------------------------------
    # Task events compat projection
    # ------------------------------------------------------------------

    def _project_task_event(self, event: RuntimeEvent) -> None:
        """Create a simplified task_events row for backwards compatibility."""
        task_id = event.task_id
        if task_id is None:
            return

        compat_type = _TASK_EVENT_COMPAT_TYPES.get(event.event_type, event.event_type)
        compat_payload = _build_compat_payload(event)

        self._conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, compat_type, json.dumps(compat_payload, ensure_ascii=False), _now()),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Verification projection
    # ------------------------------------------------------------------

    def _project_verification_event(self, event: RuntimeEvent) -> None:
        """Record orchestration_decision rows from verification events."""
        orch_run_id = event.orchestration_run_id
        if orch_run_id is None:
            return
        payload = event.payload

        if event.event_type == EventType.VERIFICATION_DECISION_RECORDED:
            decision = str(payload.get("decision", ""))
            reason = str(payload.get("reason", ""))
            next_agent = str(payload.get("next_agent", ""))
            self._conn.execute(
                """
                INSERT INTO orchestration_decisions (run_id, decision, reason, next_agent, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (orch_run_id, decision, reason, next_agent, _now()),
            )
            # Also update last_verification_result on orchestration_runs.
            self._conn.execute(
                "UPDATE orchestration_runs SET last_verification_result = ?, updated_at = ? WHERE id = ?",
                (decision, _now(), orch_run_id),
            )

        self._conn.commit()

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _emit_failure(self, event: RuntimeEvent, exc: Exception) -> None:
        """Emit a ``projection.failed`` event if a store is configured."""
        if self._store is None:
            return
        try:
            fail_event = RuntimeEvent(
                schema_version=1,
                event_type=EventType.PROJECTION_FAILED,
                aggregate_type=AggregateType.SYSTEM,
                aggregate_id=f"projector-{event.id}",
                correlation_id=event.correlation_id,
                source=EventSource.PROJECTOR,
                actor="projector",
                visibility=Visibility.INTERNAL,
                payload={
                    "failed_event_id": event.id,
                    "failed_event_type": event.event_type,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                occurred_at=now_iso(),
            )
            self._store.append(fail_event)  # type: ignore[union-attr]
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Task event compat mapping
# ---------------------------------------------------------------------------

_TASK_EVENT_COMPAT_TYPES: dict[str, str] = {
    EventType.AGENT_RUN_STARTED: "agent_run_started",
    EventType.AGENT_RUN_COMPLETED: "agent_run_completed",
    EventType.AGENT_RUN_FAILED: "agent_run_failed",
    EventType.AGENT_RUN_TIMED_OUT: "agent_run_timed_out",
    EventType.AGENT_RUN_ORPHANED: "agent_run_orphaned",
    EventType.RUN_REQUESTED: "run_requested",
    EventType.RUN_STARTED: "run_started",
    EventType.RUN_PHASE_CHANGED: "run_phase_changed",
    EventType.RUN_COMPLETED: "run_completed",
    EventType.RUN_FAILED: "run_failed",
    EventType.RUN_CANCELLED: "run_cancelled",
    EventType.APPROVAL_REQUESTED: "approval_requested",
    EventType.APPROVAL_RESOLVED: "approval_resolved",
    EventType.MODEL_USAGE_UPDATED: "usage_updated",
    EventType.VERIFICATION_DECISION_RECORDED: "verification_decision",
    EventType.VERIFICATION_RETRY_REQUESTED: "verification_retry",
    EventType.COMMAND_STARTED: "command_started",
    EventType.COMMAND_COMPLETED: "command_completed",
    EventType.FILE_CHANGED: "file_changed",
    EventType.TOOL_CALL_STARTED: "tool_call_started",
    EventType.TOOL_CALL_COMPLETED: "tool_call_completed",
}


def _build_compat_payload(event: RuntimeEvent) -> dict[str, object]:
    """Build a simplified payload for the legacy task_events table."""
    return {
        "runtime_event_id": event.id,
        "runtime_event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "source": event.source,
        "actor": event.actor,
        "summary": _event_summary(event),
    }


def _event_summary(event: RuntimeEvent) -> str:
    """A one-line human summary for the task_events compat row."""
    payload = event.payload
    etype = event.event_type

    if etype == EventType.AGENT_RUN_STARTED:
        return f"Agent {payload.get('agent', '?')} started ({payload.get('role', '?')})"
    if etype == EventType.AGENT_RUN_COMPLETED:
        return f"Agent {payload.get('agent', '?')} completed"
    if etype == EventType.AGENT_RUN_FAILED:
        return f"Agent {payload.get('agent', '?')} failed"
    if etype == EventType.AGENT_RUN_TIMED_OUT:
        return f"Agent {payload.get('agent', '?')} timed out"
    if etype == EventType.MODEL_USAGE_UPDATED:
        return f"Usage: {payload.get('input_tokens', 0)}+{payload.get('output_tokens', 0)} tokens"
    if etype == EventType.APPROVAL_REQUESTED:
        return f"Approval requested: {payload.get('kind', '?')} — {payload.get('summary', '')}"
    if etype == EventType.APPROVAL_RESOLVED:
        return f"Approval resolved: {payload.get('decision', '?')}"
    if etype == EventType.RUN_PHASE_CHANGED:
        return f"Phase → {payload.get('phase', '?')}"
    return etype


# ---------------------------------------------------------------------------
# Row mapper (used by rebuild)
# ---------------------------------------------------------------------------


def _row_to_runtime_event(row: sqlite3.Row) -> RuntimeEvent:
    """Convert a runtime_events row back into a RuntimeEvent."""
    return RuntimeEvent(
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
        payload=json.loads(str(row["payload_json"])),
        occurred_at=str(row["occurred_at"]),
        id=int(row["id"]),
    )
