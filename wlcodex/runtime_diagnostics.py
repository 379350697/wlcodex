"""Runtime diagnostics: status, trace, timeout explanation, and recovery.

Provides operator views (/status, /trace) and recovery helpers that work
from the append-only runtime_events log without mutating state silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)

if TYPE_CHECKING:
    import sqlite3
    from wlcodex.runtime_event_store import RuntimeEventStore

# ---------------------------------------------------------------------------
# Non-terminal event types for recovery detection
# ---------------------------------------------------------------------------

_NON_TERMINAL_AGENT_EVENTS = frozenset({
    EventType.AGENT_RUN_QUEUED,
    EventType.AGENT_RUN_STARTED,
    EventType.AGENT_RUN_ACTIVITY,
    EventType.AGENT_RUN_HEARTBEAT,
    EventType.AGENT_RUN_WAITING_FOR_APPROVAL,
})

_TERMINAL_AGENT_EVENTS = frozenset({
    EventType.AGENT_RUN_COMPLETED,
    EventType.AGENT_RUN_FAILED,
    EventType.AGENT_RUN_TIMED_OUT,
    EventType.AGENT_RUN_ORPHANED,
    EventType.RUN_CANCELLED,
})

# Events that indicate meaningful user/operator-visible progress.
_MEANINGFUL_EVENT_TYPES = frozenset({
    EventType.RUN_REQUESTED,
    EventType.RUN_STARTED,
    EventType.RUN_PHASE_CHANGED,
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
    EventType.AGENT_RUN_STARTED,
    EventType.AGENT_RUN_COMPLETED,
    EventType.AGENT_RUN_FAILED,
    EventType.AGENT_RUN_TIMED_OUT,
    EventType.AGENT_RUN_ORPHANED,
    EventType.AGENT_RUN_ACTIVITY,
    EventType.AGENT_RUN_HEARTBEAT,
    EventType.AGENT_RUN_WAITING_FOR_APPROVAL,
    EventType.TOOL_CALL_STARTED,
    EventType.TOOL_CALL_COMPLETED,
    EventType.TOOL_CALL_FAILED,
    EventType.COMMAND_STARTED,
    EventType.COMMAND_COMPLETED,
    EventType.COMMAND_FAILED,
    EventType.FILE_CHANGED,
    EventType.APPROVAL_REQUESTED,
    EventType.APPROVAL_RESOLVED,
    EventType.APPROVAL_EXPIRED,
    EventType.VERIFICATION_STARTED,
    EventType.VERIFICATION_DECISION_RECORDED,
    EventType.VERIFICATION_COMPLETED,
    EventType.VERIFICATION_RETRY_REQUESTED,
    EventType.WATCHDOG_IDLE_TIMEOUT,
    EventType.WATCHDOG_HARD_TIMEOUT,
    EventType.MODEL_USAGE_UPDATED,
    EventType.MODEL_MESSAGE_COMPLETED,
    EventType.MODEL_API_RETRY,
    EventType.USER_MESSAGE_RECEIVED,
    EventType.TELEGRAM_MESSAGE_SENT,
    EventType.TELEGRAM_MESSAGE_FAILED,
})

# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


@dataclass
class RuntimeAgentSummary:
    agent_run_id: int
    agent: str
    status: str
    last_event_type: str = ""
    last_event_at: str = ""
    last_event_id: int = 0


@dataclass
class RuntimeStatus:
    """Snapshot of the current runtime state for /status."""

    conversation_id: int | None = None
    active_agent: str = ""
    active_agent_run_id: int | None = None
    phase: str = ""
    status: str = ""
    last_event_type: str = ""
    last_event_at: str = ""
    last_event_id: int = 0
    idle_seconds: float = 0.0
    hard_elapsed_seconds: float = 0.0
    hard_timeout_seconds: int = 0
    token_input: int = 0
    token_output: int = 0
    total_events: int = 0
    agents: list[RuntimeAgentSummary] = field(default_factory=list)
    last_user_event: str = ""
    now: str = ""


@dataclass
class RuntimeTrace:
    """Sanitized event timeline for /trace."""

    conversation_id: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    total_events: int = 0


@dataclass
class TimeoutExplanation:
    """Human-readable timeout decision record."""

    agent_name: str
    agent_run_id: int
    timeout_type: str  # "idle" or "hard"
    last_event_id: int
    last_event_type: str
    last_event_at: str
    elapsed_idle_seconds: float
    elapsed_hard_seconds: float
    threshold_seconds: int
    reason: str = ""


# ---------------------------------------------------------------------------
# Status builder
# ---------------------------------------------------------------------------


def build_runtime_status(
    store: "RuntimeEventStore",
    conversation_id: int | None,
    *,
    hard_timeout_seconds: int = 0,
) -> RuntimeStatus:
    """Build a RuntimeStatus from the event log for a conversation.

    If *conversation_id* is None, returns an empty status.
    """
    if conversation_id is None:
        return RuntimeStatus(now=now_iso())

    events = store.list_by_conversation(conversation_id, limit=500)
    if not events:
        return RuntimeStatus(
            conversation_id=conversation_id,
            status="idle",
            now=now_iso(),
        )

    now = datetime.now(timezone.utc)
    status = RuntimeStatus(
        conversation_id=conversation_id,
        total_events=len(events),
        now=now.isoformat(),
        hard_timeout_seconds=hard_timeout_seconds,
    )

    # Track per-agent state from events
    agent_states: dict[int, RuntimeAgentSummary] = {}

    for ev in events:
        aid = ev.agent_run_id
        if aid is not None:
            if aid not in agent_states:
                agent_states[aid] = RuntimeAgentSummary(
                    agent_run_id=aid,
                    agent="",
                    status="unknown",
                )
            a = agent_states[aid]
            a.last_event_type = ev.event_type
            a.last_event_at = ev.occurred_at
            a.last_event_id = ev.id

            if ev.event_type == EventType.AGENT_RUN_STARTED:
                a.agent = ev.payload.get("agent", ev.actor)
                a.status = "running"
            elif ev.event_type == EventType.AGENT_RUN_COMPLETED:
                a.status = "completed"
            elif ev.event_type == EventType.AGENT_RUN_FAILED:
                a.status = "failed"
            elif ev.event_type == EventType.AGENT_RUN_TIMED_OUT:
                a.status = "timed_out"
            elif ev.event_type == EventType.AGENT_RUN_ORPHANED:
                a.status = "orphaned"
            elif ev.event_type == EventType.AGENT_RUN_QUEUED:
                a.status = "queued"
            elif ev.event_type == EventType.AGENT_RUN_WAITING_FOR_APPROVAL:
                a.status = "waiting_for_approval"

        # Track last meaningful event
        if ev.event_type in _MEANINGFUL_EVENT_TYPES:
            status.last_event_type = ev.event_type
            status.last_event_at = ev.occurred_at
            status.last_event_id = ev.id

        # Track phase changes
        if ev.event_type == EventType.RUN_PHASE_CHANGED:
            status.phase = ev.payload.get("phase", "")
        elif ev.event_type == EventType.RUN_STARTED:
            status.status = "running"
        elif ev.event_type == EventType.RUN_COMPLETED:
            status.status = "completed"
        elif ev.event_type == EventType.RUN_FAILED:
            status.status = "failed"
        elif ev.event_type == EventType.RUN_CANCELLED:
            status.status = "cancelled"

        # Token accumulation
        if ev.event_type == EventType.MODEL_USAGE_UPDATED:
            status.token_input += int(ev.payload.get("input_tokens", 0))
            status.token_output += int(ev.payload.get("output_tokens", 0))

        # Last user-visible event (USER visibility only)
        if ev.visibility == Visibility.USER:
            status.last_user_event = ev.event_type

    # Find active agent (non-terminal)
    active_agents = [
        a
        for a in agent_states.values()
        if a.status not in ("completed", "failed", "timed_out", "orphaned")
    ]
    if active_agents:
        active = active_agents[-1]
        status.active_agent = active.agent
        status.active_agent_run_id = active.agent_run_id

    # Compute clocks from last meaningful event
    if status.last_event_at:
        try:
            last_dt = datetime.fromisoformat(status.last_event_at)
            idle_delta = (now - last_dt).total_seconds()
            status.idle_seconds = max(0.0, idle_delta)
        except (ValueError, TypeError):
            pass

    # Compute hard elapsed from first run event
    for ev in events:
        if ev.event_type in (EventType.RUN_STARTED, EventType.AGENT_RUN_STARTED):
            try:
                start_dt = datetime.fromisoformat(ev.occurred_at)
                status.hard_elapsed_seconds = max(
                    0.0, (now - start_dt).total_seconds()
                )
            except (ValueError, TypeError):
                pass
            break

    status.agents = sorted(agent_states.values(), key=lambda a: a.agent_run_id)
    return status


def format_status_display(status: RuntimeStatus) -> str:
    """Format a RuntimeStatus as a human-readable /status string."""
    if status.conversation_id is None:
        return "暂无活跃对话。发送消息开始。"

    if status.status == "idle":
        return f"当前对话 #{status.conversation_id} — 空闲中"

    lines = [
        f"对话 #{status.conversation_id} — {_status_cn(status.status)}",
    ]

    if status.phase:
        lines.append(f"阶段：{_phase_cn(status.phase)}")

    if status.active_agent:
        agent_line = f"活跃 Agent：{status.active_agent}"
        if status.active_agent_run_id:
            agent_line += f"（运行 #{status.active_agent_run_id}）"
        lines.append(agent_line)

    if status.last_event_type:
        lines.append(
            f"最近事件：{_event_cn(status.last_event_type)}"
            f"（#{status.last_event_id}）"
        )

    # Clocks
    if status.idle_seconds > 0:
        lines.append(f"空闲时钟：{_duration_cn(status.idle_seconds)}")
    if status.hard_elapsed_seconds > 0:
        hard_str = f"硬时钟：{_duration_cn(status.hard_elapsed_seconds)}"
        if status.hard_timeout_seconds:
            hard_str += f" / 上限 {_duration_cn(status.hard_timeout_seconds)}"
        lines.append(hard_str)

    # Token summary
    if status.token_input or status.token_output:
        lines.append(
            f"Token：{status.token_input:,} 输入 / {status.token_output:,} 输出"
        )

    # Agent summary
    if status.agents:
        lines.append(f"Agent 运行记录（{len(status.agents)}）：")
        for a in status.agents:
            lines.append(
                f"  #{a.agent_run_id} {a.agent or 'unknown'} — "
                f"{_agent_status_cn(a.status)}"
            )

    if status.total_events:
        lines.append(f"事件总数：{status.total_events}")

    if status.last_user_event:
        lines.append(f"最近用户事件：{_event_cn(status.last_user_event)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Trace builder
# ---------------------------------------------------------------------------


def build_runtime_trace(
    store: "RuntimeEventStore",
    conversation_id: int,
    *,
    limit: int = 20,
    visibility_filter: str | None = None,
) -> RuntimeTrace:
    """Build a sanitized trace of the last N events for a conversation.

    Events with ``visibility = "internal"`` are excluded by default unless
    *visibility_filter* is set to ``"operator"`` or ``"all"``.
    """
    all_events = store.list_recent_for_conversation(
        conversation_id, limit=500
    )
    total = len(all_events)

    # Filter by visibility
    if visibility_filter == "all":
        candidates = list(all_events)
    elif visibility_filter == "operator":
        candidates = [
            e
            for e in all_events
            if e.visibility in (Visibility.OPERATOR, Visibility.USER)
        ]
    else:
        candidates = [e for e in all_events if e.visibility == Visibility.USER]

    if not candidates:
        candidates = [
            e
            for e in all_events
            if e.visibility in (Visibility.OPERATOR, Visibility.USER)
        ]

    truncated = len(candidates) > limit
    selected = candidates[-limit:]

    trace_events: list[dict[str, Any]] = []
    for ev in selected:
        trace_events.append(_sanitize_event_for_display(ev))

    return RuntimeTrace(
        conversation_id=conversation_id,
        events=trace_events,
        truncated=truncated,
        total_events=total,
    )


def format_trace_display(trace: RuntimeTrace) -> str:
    """Format a RuntimeTrace as a human-readable /trace string."""
    if not trace.events:
        return "暂无事件记录。"

    lines = [f"对话 #{trace.conversation_id} 的事件记录：", ""]

    if trace.truncated:
        lines.append(
            f"（显示最近 {len(trace.events)} 条，共 {trace.total_events} 条）"
        )
    else:
        lines.append(f"共 {len(trace.events)} 条事件：")

    for ev in trace.events:
        eid = ev.get("id", "?")
        etype = _event_cn(str(ev.get("event_type", "")))
        source = ev.get("source", "")
        payload_summary = _payload_summary(ev.get("payload", {}))
        occurred = str(ev.get("occurred_at", ""))[:19]

        line = f"  [#{eid}] {occurred} [{source}] {etype}"
        if payload_summary:
            line += f" — {_trim(payload_summary, 100)}"
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Timeout explanation
# ---------------------------------------------------------------------------


def compute_timeout_explanation(
    store: "RuntimeEventStore",
    agent_run_id: int,
    timeout_type: str,
    threshold_seconds: int,
) -> TimeoutExplanation:
    """Build a timeout explanation record from the event log.

    Reads the events for *agent_run_id* to find the last activity and
    compute elapsed times.
    """
    events = store.list_by_agent_run(agent_run_id, limit=200)
    if not events:
        return TimeoutExplanation(
            agent_name="unknown",
            agent_run_id=agent_run_id,
            timeout_type=timeout_type,
            last_event_id=0,
            last_event_type="",
            last_event_at="",
            elapsed_idle_seconds=0,
            elapsed_hard_seconds=0,
            threshold_seconds=threshold_seconds,
            reason="no events found for agent run",
        )

    first = events[0]
    last = events[-1]
    now = datetime.now(timezone.utc)

    agent_name = ""
    for ev in events:
        if ev.event_type == EventType.AGENT_RUN_STARTED:
            agent_name = ev.payload.get("agent", ev.actor)
            break

    # Find last activity for idle computation
    last_activity = last
    for ev in reversed(events):
        if ev.event_type in (
            EventType.AGENT_RUN_ACTIVITY,
            EventType.AGENT_RUN_HEARTBEAT,
            EventType.TOOL_CALL_STARTED,
            EventType.TOOL_CALL_COMPLETED,
            EventType.COMMAND_COMPLETED,
            EventType.MODEL_TEXT_DELTA,
            EventType.MODEL_MESSAGE_COMPLETED,
            EventType.FILE_CHANGED,
        ):
            last_activity = ev
            break

    try:
        last_activity_dt = datetime.fromisoformat(last_activity.occurred_at)
        elapsed_idle = (now - last_activity_dt).total_seconds()
    except (ValueError, TypeError):
        elapsed_idle = 0.0

    try:
        first_dt = datetime.fromisoformat(first.occurred_at)
        elapsed_hard = (now - first_dt).total_seconds()
    except (ValueError, TypeError):
        elapsed_hard = 0.0

    return TimeoutExplanation(
        agent_name=agent_name,
        agent_run_id=agent_run_id,
        timeout_type=timeout_type,
        last_event_id=last.id,
        last_event_type=last.event_type,
        last_event_at=last.occurred_at,
        elapsed_idle_seconds=max(0.0, elapsed_idle),
        elapsed_hard_seconds=max(0.0, elapsed_hard),
        threshold_seconds=threshold_seconds,
    )


def format_timeout_explanation(expl: TimeoutExplanation) -> str:
    """Format a timeout explanation for display."""
    timeout_label = "空闲超时" if expl.timeout_type == "idle" else "硬超时"
    lines = [
        f"{timeout_label} — Agent {expl.agent_name}（运行 #{expl.agent_run_id}）",
        f"原因：{expl.reason}" if expl.reason else "",
        f"最后事件：{_event_cn(expl.last_event_type)}（#{expl.last_event_id}）" if expl.last_event_type else "",
        f"最后事件时间：{expl.last_event_at[:19]}" if expl.last_event_at else "",
        f"空闲时间：{_duration_cn(expl.elapsed_idle_seconds)}",
        f"总运行时间：{_duration_cn(expl.elapsed_hard_seconds)}",
        f"阈值：{_duration_cn(expl.threshold_seconds)}",
    ]
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Recovery helpers
# ---------------------------------------------------------------------------


def find_non_terminal_agent_runs(
    store: "RuntimeEventStore",
) -> list[int]:
    """Find agent_run_ids that have started but not yet reached a terminal state.

    Queries the runtime_events table directly to find runs that have a start
    or activity event but no terminal event (completed/failed/timed_out/orphaned).

    Also handles run-level cancellations: if a RUN_CANCELLED event exists
    without a per-agent agent_run_id, all agent runs sharing that
    correlation_id are excluded from the non-terminal set.
    """
    conn = store._conn
    # Find runs with non-terminal events
    non_term_rows = conn.execute(
        """
        SELECT DISTINCT agent_run_id FROM runtime_events
        WHERE event_type IN (?, ?, ?, ?, ?)
          AND agent_run_id IS NOT NULL
        """,
        tuple(_NON_TERMINAL_AGENT_EVENTS),
    ).fetchall()
    non_term_ids = {int(r["agent_run_id"]) for r in non_term_rows}

    if not non_term_ids:
        return []

    # Remove runs that have a terminal agent event
    placeholders = ",".join("?" for _ in non_term_ids)
    term_rows = conn.execute(
        f"""
        SELECT DISTINCT agent_run_id FROM runtime_events
        WHERE event_type IN (?, ?, ?, ?, ?)
          AND agent_run_id IN ({placeholders})
        """,
        tuple(_TERMINAL_AGENT_EVENTS) + tuple(non_term_ids),
    ).fetchall()
    term_ids = {int(r["agent_run_id"]) for r in term_rows}

    # Also remove runs belonging to cancelled correlations
    # (run-level cancellation may not carry per-agent agent_run_id)
    cancelled_corrs = conn.execute(
        """
        SELECT DISTINCT correlation_id FROM runtime_events
        WHERE event_type IN (?, ?)
          AND correlation_id != ''
        """,
        (EventType.RUN_CANCELLED, EventType.RUN_CANCEL_REQUESTED),
    ).fetchall()

    if cancelled_corrs:
        cancelled_corr_ids = {
            str(r["correlation_id"]) for r in cancelled_corrs
        }
        # Find agent_run_ids within cancelled correlations
        cancelled_rows = conn.execute(
            f"""
            SELECT DISTINCT agent_run_id FROM runtime_events
            WHERE correlation_id IN ({','.join('?' for _ in cancelled_corr_ids)})
              AND agent_run_id IN ({placeholders})
            """,
            tuple(cancelled_corr_ids) + tuple(non_term_ids),
        ).fetchall()
        term_ids |= {int(r["agent_run_id"]) for r in cancelled_rows}

    return sorted(non_term_ids - term_ids)


def build_recovery_events(
    *,
    orphaned_agent_run_ids: list[int],
    conversation_id: int | None = None,
    correlation_id: str = "",
) -> list[RuntimeEvent]:
    """Build recovery events for orphaned agent runs.

    Returns a list of RuntimeEvent instances to append.
    Does NOT append them — callers decide when to persist.
    """
    ts = now_iso()
    events: list[RuntimeEvent] = []

    # system.started
    events.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.SYSTEM_STARTED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id="system",
            correlation_id=correlation_id or f"recovery-{ts}",
            source=EventSource.SYSTEM,
            actor="system",
            visibility=Visibility.OPERATOR,
            payload={"started_at": ts},
            occurred_at=ts,
            conversation_id=conversation_id,
        )
    )

    # system.recovery.started
    events.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.SYSTEM_RECOVERY_STARTED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id="system",
            correlation_id=correlation_id or f"recovery-{ts}",
            causation_id=0,
            source=EventSource.SYSTEM,
            actor="system",
            visibility=Visibility.OPERATOR,
            payload={
                "orphaned_run_count": len(orphaned_agent_run_ids),
                "orphaned_run_ids": orphaned_agent_run_ids,
            },
            occurred_at=ts,
            conversation_id=conversation_id,
        )
    )

    # agent.run.orphaned for each non-terminal run
    for run_id in orphaned_agent_run_ids:
        events.append(
            RuntimeEvent(
                schema_version=1,
                event_type=EventType.AGENT_RUN_ORPHANED,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(run_id),
                correlation_id=correlation_id or f"recovery-{ts}",
                source=EventSource.SYSTEM,
                actor="system",
                visibility=Visibility.OPERATOR,
                payload={
                    "reason": "service_restart_orphaned_run",
                    "agent_run_id": run_id,
                },
                occurred_at=ts,
                agent_run_id=run_id,
                conversation_id=conversation_id,
            )
        )

    # system.recovery.completed
    events.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.SYSTEM_RECOVERY_COMPLETED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id="system",
            correlation_id=correlation_id or f"recovery-{ts}",
            source=EventSource.SYSTEM,
            actor="system",
            visibility=Visibility.OPERATOR,
            payload={
                "orphaned_count": len(orphaned_agent_run_ids),
                "completed_at": ts,
            },
            occurred_at=ts,
            conversation_id=conversation_id,
        )
    )

    return events


def append_recovery_events(
    store: "RuntimeEventStore",
    *,
    orphaned_agent_run_ids: list[int],
    conversation_id: int | None = None,
    correlation_id: str = "",
) -> list[RuntimeEvent]:
    """Append recovery events to the store and return the persisted events."""
    events = build_recovery_events(
        orphaned_agent_run_ids=orphaned_agent_run_ids,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
    )
    persisted: list[RuntimeEvent] = []
    for ev in events:
        persisted.append(store.append(ev))
    return persisted


def append_startup_recovery_events(
    store: "RuntimeEventStore",
    ledger: object,
) -> dict[str, list[int]]:
    """Append startup recovery events for legacy non-terminal projections.

    This replaces silent startup mutation of compatibility tables.  The
    RuntimeProjector registered on the store turns these events back into
    updates to tasks, orchestration_runs, and agent_runs.
    """
    conn = ledger._conn
    ts = now_iso()
    correlation_id = f"recovery-{ts}"
    task_rows = conn.execute(
        """
        SELECT id, status FROM tasks
        WHERE status IN ('running', 'queued', 'waiting_approval')
        """
    ).fetchall()
    orch_rows = conn.execute(
        "SELECT id, conversation_id FROM orchestration_runs WHERE status = 'running'"
    ).fetchall()
    agent_rows = conn.execute(
        """
        SELECT id, conversation_id, hidden_task_id FROM agent_runs
        WHERE status = 'running'
        """
    ).fetchall()

    task_ids = [int(row["id"]) for row in task_rows]
    orch_ids = [int(row["id"]) for row in orch_rows]
    agent_ids = [int(row["id"]) for row in agent_rows]

    started = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.SYSTEM_STARTED,
        aggregate_type=AggregateType.SYSTEM,
        aggregate_id="system",
        correlation_id=correlation_id,
        source=EventSource.SYSTEM,
        actor="system",
        visibility=Visibility.OPERATOR,
        payload={"started_at": ts},
        occurred_at=ts,
    ))
    recovery_started = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.SYSTEM_RECOVERY_STARTED,
        aggregate_type=AggregateType.SYSTEM,
        aggregate_id="system",
        correlation_id=correlation_id,
        source=EventSource.SYSTEM,
        actor="system",
        visibility=Visibility.OPERATOR,
        payload={
            "task_ids": task_ids,
            "orchestration_run_ids": orch_ids,
            "agent_run_ids": agent_ids,
        },
        occurred_at=ts,
        causation_id=started.id,
    ))

    last_id = recovery_started.id
    for row in task_rows:
        ev = store.append(RuntimeEvent(
            schema_version=1,
            event_type=EventType.RUN_FAILED,
            aggregate_type=AggregateType.SYSTEM,
            aggregate_id=f"task-{row['id']}",
            correlation_id=correlation_id,
            source=EventSource.SYSTEM,
            actor="system",
            visibility=Visibility.OPERATOR,
            payload={
                "reason": "service_restart_orphaned_task",
                "previous_status": str(row["status"]),
                "last_active_agent": "unknown",
            },
            occurred_at=ts,
            task_id=int(row["id"]),
            causation_id=last_id,
        ))
        last_id = ev.id

    for row in orch_rows:
        ev = store.append(RuntimeEvent(
            schema_version=1,
            event_type=EventType.RUN_FAILED,
            aggregate_type=AggregateType.ORCHESTRATION_RUN,
            aggregate_id=str(row["id"]),
            correlation_id=correlation_id,
            source=EventSource.SYSTEM,
            actor="system",
            visibility=Visibility.OPERATOR,
            payload={
                "reason": "service_restart_orphaned_run",
                "last_active_agent": "unknown",
            },
            occurred_at=ts,
            conversation_id=int(row["conversation_id"]),
            orchestration_run_id=int(row["id"]),
            causation_id=last_id,
        ))
        last_id = ev.id

    for row in agent_rows:
        ev = store.append(RuntimeEvent(
            schema_version=1,
            event_type=EventType.AGENT_RUN_ORPHANED,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(row["id"]),
            correlation_id=correlation_id,
            source=EventSource.SYSTEM,
            actor="system",
            visibility=Visibility.OPERATOR,
            payload={
                "reason": "service_restart_orphaned_run",
                "agent_run_id": int(row["id"]),
            },
            occurred_at=ts,
            conversation_id=int(row["conversation_id"]),
            agent_run_id=int(row["id"]),
            task_id=(
                int(row["hidden_task_id"])
                if row["hidden_task_id"] is not None else None
            ),
            causation_id=last_id,
        ))
        last_id = ev.id

    rebuilt = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.PROJECTION_REBUILT,
        aggregate_type=AggregateType.SYSTEM,
        aggregate_id="system",
        correlation_id=correlation_id,
        source=EventSource.PROJECTOR,
        actor="runtime_projector",
        visibility=Visibility.OPERATOR,
        payload={
            "strategy": "live_projector_registered",
            "events_replayed": 0,
            "rebuilt_at": ts,
        },
        occurred_at=ts,
        causation_id=last_id,
    ))
    last_id = rebuilt.id

    store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.SYSTEM_RECOVERY_COMPLETED,
        aggregate_type=AggregateType.SYSTEM,
        aggregate_id="system",
        correlation_id=correlation_id,
        source=EventSource.SYSTEM,
        actor="system",
        visibility=Visibility.OPERATOR,
        payload={
            "task_count": len(task_ids),
            "orchestration_run_count": len(orch_ids),
            "agent_run_count": len(agent_ids),
            "completed_at": ts,
        },
        occurred_at=ts,
        causation_id=last_id,
    ))
    return {
        "task_ids": task_ids,
        "orchestration_run_ids": orch_ids,
        "agent_run_ids": agent_ids,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_event_for_display(ev: RuntimeEvent) -> dict[str, Any]:
    """Return a display-safe dict for a runtime event (no raw secrets)."""
    payload = ev.payload.copy()
    # Already redacted on append, but double-check sensitive keys for display
    sensitive_keys = {"secret", "token", "password", "auth", "key", "credential"}
    safe_payload: dict[str, Any] = {}
    for k, v in payload.items():
        kl = k.lower()
        if any(s in kl for s in sensitive_keys):
            safe_payload[k] = "[REDACTED]"
        elif isinstance(v, str) and len(v) > 500:
            safe_payload[k] = v[:500] + "..."
        else:
            safe_payload[k] = v

    return {
        "id": ev.id,
        "event_type": ev.event_type,
        "source": ev.source,
        "actor": ev.actor,
        "occurred_at": ev.occurred_at,
        "payload": safe_payload,
    }


def _payload_summary(payload: dict[str, Any]) -> str:
    """Extract a short summary string from a payload dict."""
    if "phase" in payload:
        return _phase_cn(str(payload["phase"]))
    if "agent" in payload:
        return f"agent={payload['agent']}"
    if "model" in payload:
        model = str(payload["model"])
        in_tok = payload.get("input_tokens", "")
        out_tok = payload.get("output_tokens", "")
        return f"{model} in={in_tok} out={out_tok}"
    if "reason" in payload:
        return str(payload["reason"])[:80]
    if "summary" in payload:
        return str(payload["summary"])[:80]
    if "delta" in payload:
        return str(payload["delta"])[:80]
    if "decision" in payload:
        return f"decision={payload['decision']}"
    if "tool_name" in payload:
        return f"tool={payload['tool_name']}"
    if "orphaned_run_count" in payload:
        return f"orphaned={payload['orphaned_run_count']}"
    return ""


def _status_cn(status: str) -> str:
    mapping = {
        "running": "运行中",
        "completed": "已完成",
        "failed": "已失败",
        "cancelled": "已取消",
        "idle": "空闲",
        "unknown": "未知",
    }
    return mapping.get(status, status)


def _phase_cn(phase: str) -> str:
    mapping = {
        "running_analysis": "Codex 分析",
        "running_implementation": "Claude 实施",
        "running_verification": "Codex 验收",
        "retrying_implementation": "重新实施",
        "analysis": "分析",
        "implementation": "实施",
        "verification": "验收",
    }
    return mapping.get(phase, phase)


def _event_cn(event_type: str) -> str:
    mapping = {
        EventType.RUN_REQUESTED: "运行请求",
        EventType.RUN_STARTED: "运行开始",
        EventType.RUN_PHASE_CHANGED: "阶段变更",
        EventType.RUN_COMPLETED: "运行完成",
        EventType.RUN_FAILED: "运行失败",
        EventType.RUN_CANCELLED: "运行取消",
        EventType.AGENT_RUN_QUEUED: "Agent 排队",
        EventType.AGENT_RUN_STARTED: "Agent 启动",
        EventType.AGENT_RUN_ACTIVITY: "Agent 活动",
        EventType.AGENT_RUN_HEARTBEAT: "Agent 心跳",
        EventType.AGENT_RUN_WAITING_FOR_APPROVAL: "等待审批",
        EventType.AGENT_RUN_COMPLETED: "Agent 完成",
        EventType.AGENT_RUN_FAILED: "Agent 失败",
        EventType.AGENT_RUN_TIMED_OUT: "Agent 超时",
        EventType.AGENT_RUN_ORPHANED: "Agent 孤儿",
        EventType.TOOL_CALL_STARTED: "工具调用开始",
        EventType.TOOL_CALL_COMPLETED: "工具调用完成",
        EventType.TOOL_CALL_FAILED: "工具调用失败",
        EventType.COMMAND_STARTED: "命令开始",
        EventType.COMMAND_COMPLETED: "命令完成",
        EventType.COMMAND_FAILED: "命令失败",
        EventType.FILE_CHANGED: "文件变更",
        EventType.APPROVAL_REQUESTED: "审批请求",
        EventType.APPROVAL_RESOLVED: "审批完成",
        EventType.APPROVAL_EXPIRED: "审批过期",
        EventType.VERIFICATION_STARTED: "验收开始",
        EventType.VERIFICATION_DECISION_RECORDED: "验收决策",
        EventType.VERIFICATION_COMPLETED: "验收完成",
        EventType.VERIFICATION_RETRY_REQUESTED: "重新验收",
        EventType.WATCHDOG_IDLE_TIMEOUT: "空闲超时",
        EventType.WATCHDOG_HARD_TIMEOUT: "硬超时",
        EventType.SYSTEM_STARTED: "系统启动",
        EventType.SYSTEM_RECOVERY_STARTED: "恢复开始",
        EventType.SYSTEM_RECOVERY_COMPLETED: "恢复完成",
        EventType.PROJECTION_REBUILT: "投影重建",
        EventType.MODEL_USAGE_UPDATED: "Token 更新",
        EventType.MODEL_MESSAGE_COMPLETED: "模型消息完成",
        EventType.MODEL_TEXT_DELTA: "模型文本增量",
        EventType.MODEL_API_RETRY: "API 重试",
        EventType.USER_MESSAGE_RECEIVED: "用户消息",
        EventType.TELEGRAM_MESSAGE_SENT: "Telegram 发送",
        EventType.TELEGRAM_MESSAGE_FAILED: "Telegram 发送失败",
    }
    return mapping.get(event_type, event_type)


def _agent_status_cn(status: str) -> str:
    mapping = {
        "running": "运行中",
        "completed": "已完成",
        "failed": "已失败",
        "timed_out": "已超时",
        "orphaned": "已孤儿",
        "queued": "排队中",
        "waiting_for_approval": "等待审批",
        "unknown": "未知",
    }
    return mapping.get(status, status)


def _duration_cn(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}秒"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}分{s}秒"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}时{m}分"


def _trim(value: str, max_chars: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"
