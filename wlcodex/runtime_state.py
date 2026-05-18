"""Runtime state dataclasses and pure replay reducers.

Rebuilds current orchestration, agent, and approval state from
runtime events without side effects.  All functions are pure:
a sequence of RuntimeEvents deterministically produces the same state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wlcodex.runtime_events import EventType

# ---------------------------------------------------------------------------
# Terminal-state sets (spec state machines)
# ---------------------------------------------------------------------------

_AGENT_TERMINAL_STATES = frozenset({
    "completed", "failed", "timed_out", "cancelled", "orphaned",
})

_ORCHESTRATION_TERMINAL_STATES = frozenset({
    "completed", "failed", "cancelled",
})

_APPROVAL_TERMINAL_STATES = frozenset({
    "resolved", "expired", "cancelled",
})

# ---------------------------------------------------------------------------
# Agent phase mapping:  event_type -> agent status
# ---------------------------------------------------------------------------

_AGENT_STATUS_MAP: dict[str, str] = {
    EventType.AGENT_RUN_QUEUED: "queued",
    EventType.AGENT_RUN_STARTED: "running",
    EventType.AGENT_RUN_ACTIVITY: "running",
    EventType.AGENT_RUN_HEARTBEAT: "running",
    EventType.AGENT_RUN_WAITING_FOR_APPROVAL: "waiting_for_approval",
    EventType.AGENT_RUN_COMPLETED: "completed",
    EventType.AGENT_RUN_FAILED: "failed",
    EventType.AGENT_RUN_TIMED_OUT: "timed_out",
    EventType.AGENT_RUN_ORPHANED: "orphaned",
}

# ---------------------------------------------------------------------------
# Orchestration phase mapping from run.phase.changed payload.phase
# ---------------------------------------------------------------------------

_VALID_ORCHESTRATION_PHASES = frozenset({
    "queued",
    "running_analysis",
    "running_implementation",
    "running_verification",
    "retrying_implementation",
    "completed",
    "failed",
    "cancelled",
})

_ORCH_STATUS_MAP: dict[str, str] = {
    EventType.RUN_REQUESTED: "queued",
    EventType.RUN_STARTED: "running",
    EventType.RUN_COMPLETED: "completed",
    EventType.RUN_FAILED: "failed",
    EventType.RUN_CANCELLED: "cancelled",
}

# ---------------------------------------------------------------------------
# State models
# ---------------------------------------------------------------------------


@dataclass
class RuntimeAgentState:
    """Current state of one agent run reconstructed from events."""

    aggregate_id: str
    agent: str = ""
    role: str = ""
    status: str = "queued"
    conversation_id: int | None = None
    orchestration_run_id: int | None = None
    agent_run_id: int | None = None
    task_id: int | None = None
    last_activity_id: int = 0
    last_activity_type: str = ""
    last_activity_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    completion_summary: str = ""
    token_input: int = 0
    token_output: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    external_session_id: str = ""
    idle_timeout_at: str = ""
    hard_timeout_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in _AGENT_TERMINAL_STATES


@dataclass
class RuntimeOrchestrationState:
    """Current state of one orchestration run reconstructed from events."""

    aggregate_id: str
    goal: str = ""
    status: str = "queued"
    current_phase: str = ""
    verify_round: int = 0
    max_verify_rounds: int = 3
    conversation_id: int | None = None
    orchestration_run_id: int | None = None
    last_codex_analysis: str = ""
    last_claude_summary: str = ""
    last_verification_result: str = ""
    last_event_id: int = 0
    last_event_type: str = ""
    last_event_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    failure_reason: str = ""
    last_active_agent: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in _ORCHESTRATION_TERMINAL_STATES


@dataclass
class RuntimeApprovalState:
    """Current state of one approval reconstructed from events."""

    aggregate_id: str
    status: str = "requested"
    kind: str = ""
    summary: str = ""
    command_summary: str = ""
    tool_name: str = ""
    conversation_id: int | None = None
    orchestration_run_id: int | None = None
    agent_run_id: int | None = None
    task_id: int | None = None
    decision: str = ""
    resolver: str = ""
    telegram_callback_id: str = ""
    requested_at: str = ""
    resolved_at: str = ""
    error: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in _APPROVAL_TERMINAL_STATES


@dataclass
class RuntimeStateSnapshot:
    """Complete runtime state at a point in the event stream."""

    agents: dict[str, RuntimeAgentState] = field(default_factory=dict)
    orchestrations: dict[str, RuntimeOrchestrationState] = field(default_factory=dict)
    approvals: dict[str, RuntimeApprovalState] = field(default_factory=dict)
    token_totals: dict[str, dict[str, int]] = field(default_factory=dict)
    last_event_id: int = 0
    last_event_at: str = ""

    def agent(self, aggregate_id: str) -> RuntimeAgentState | None:
        return self.agents.get(aggregate_id)

    def orchestration(self, aggregate_id: str) -> RuntimeOrchestrationState | None:
        return self.orchestrations.get(aggregate_id)

    def approval(self, aggregate_id: str) -> RuntimeApprovalState | None:
        return self.approvals.get(aggregate_id)


# ---------------------------------------------------------------------------
# Pure replay reducer
# ---------------------------------------------------------------------------


def replay_events(events: list[Any]) -> RuntimeStateSnapshot:
    """Replay a chronological sequence of RuntimeEvents into a snapshot.

    Pure function: no side effects, no database access.
    Deterministic: same events always produce the same state.

    Terminal-state protection:
      Once an agent/orchestration/approval enters a terminal state,
      subsequent non-terminal events cannot change its status.
    """
    snapshot = RuntimeStateSnapshot()

    for event in events:
        _apply_event(snapshot, event)

    return snapshot


# ---------------------------------------------------------------------------
# Per-event dispatch
# ---------------------------------------------------------------------------


def _apply_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    """Apply a single RuntimeEvent to the snapshot (mutates snap in place)."""
    snap.last_event_id = event.id
    snap.last_event_at = event.occurred_at

    etype = event.event_type
    agg_type = event.aggregate_type
    agg_id = event.aggregate_id

    # --- Orchestration lifecycle ---
    if etype in (
        EventType.RUN_REQUESTED,
        EventType.RUN_STARTED,
        EventType.RUN_PHASE_CHANGED,
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
        EventType.RUN_CANCEL_REQUESTED,
    ):
        _apply_orchestration_event(snap, event)

    # --- Agent lifecycle ---
    elif etype in (
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
        _apply_agent_event(snap, event)

    # --- Model usage ---
    elif etype == EventType.MODEL_USAGE_UPDATED:
        _apply_usage_event(snap, event)

    # --- Approval ---
    elif etype in (
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
        EventType.APPROVAL_EXPIRED,
    ):
        _apply_approval_event(snap, event)

    # --- Timeout events (can affect agent state) ---
    elif etype in (EventType.WATCHDOG_IDLE_TIMEOUT, EventType.WATCHDOG_HARD_TIMEOUT):
        _apply_timeout_event(snap, event)

    # --- Verification events -> update orchestration context ---
    elif etype in (
        EventType.VERIFICATION_STARTED,
        EventType.VERIFICATION_DECISION_RECORDED,
        EventType.VERIFICATION_COMPLETED,
        EventType.VERIFICATION_RETRY_REQUESTED,
    ):
        _apply_verification_event(snap, event)

    # --- Projection events ---
    elif etype == EventType.PROJECTION_REBUILT:
        pass  # informational only for replay

    elif etype == EventType.PROJECTION_FAILED:
        pass  # informational only for replay


# ---------------------------------------------------------------------------
# Orchestration reducer
# ---------------------------------------------------------------------------


def _apply_orchestration_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    agg_id = event.aggregate_id
    orch = snap.orchestrations.get(agg_id)

    if orch is None:
        orch = RuntimeOrchestrationState(aggregate_id=agg_id)
        snap.orchestrations[agg_id] = orch

    # Never overwrite terminal state with non-terminal activity.
    if orch.is_terminal and event.event_type not in (
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
    ):
        return

    orch.last_event_id = event.id
    orch.last_event_type = event.event_type
    orch.last_event_at = event.occurred_at
    orch.conversation_id = event.conversation_id or orch.conversation_id
    orch.orchestration_run_id = event.orchestration_run_id or orch.orchestration_run_id

    payload = event.payload

    if event.event_type == EventType.RUN_REQUESTED:
        orch.status = "queued"
        orch.goal = str(payload.get("goal", orch.goal))
        orch.max_verify_rounds = int(payload.get("max_verify_rounds", orch.max_verify_rounds))

    elif event.event_type == EventType.RUN_STARTED:
        orch.status = "running"
        orch.started_at = event.occurred_at
        orch.current_phase = str(payload.get("phase", "running_analysis"))
        orch.goal = str(payload.get("goal", orch.goal))

    elif event.event_type == EventType.RUN_PHASE_CHANGED:
        phase = str(payload.get("phase", ""))
        if phase in _VALID_ORCHESTRATION_PHASES:
            orch.current_phase = phase
        if phase == "retrying_implementation":
            pass  # keep running status, phase changed
        orch.verify_round = int(payload.get("verify_round", orch.verify_round))
        orch.last_codex_analysis = str(payload.get("codex_analysis", orch.last_codex_analysis))
        orch.last_claude_summary = str(payload.get("claude_summary", orch.last_claude_summary))

    elif event.event_type == EventType.RUN_COMPLETED:
        if orch.last_verification_result != "pass":
            orch.status = "failed"
            orch.completed_at = event.occurred_at
            orch.failure_reason = "run_completed_without_verification_pass"
            orch.current_phase = "failed"
            return
        orch.status = "completed"
        orch.completed_at = event.occurred_at
        orch.current_phase = "completed"

    elif event.event_type == EventType.RUN_FAILED:
        orch.status = "failed"
        orch.completed_at = event.occurred_at
        orch.failure_reason = str(payload.get("reason", ""))
        orch.last_active_agent = str(payload.get("last_active_agent", orch.last_active_agent))

    elif event.event_type == EventType.RUN_CANCELLED:
        orch.status = "cancelled"
        orch.completed_at = event.occurred_at

    elif event.event_type == EventType.RUN_CANCEL_REQUESTED:
        # Does not change status — only run.cancelled does.
        pass


# ---------------------------------------------------------------------------
# Agent reducer
# ---------------------------------------------------------------------------


def _apply_agent_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    agg_id = event.aggregate_id
    agent = snap.agents.get(agg_id)

    if agent is None:
        agent = RuntimeAgentState(aggregate_id=agg_id)
        snap.agents[agg_id] = agent

    # Terminal state protection.
    if agent.is_terminal and event.event_type not in (
        EventType.AGENT_RUN_COMPLETED,
        EventType.AGENT_RUN_FAILED,
        EventType.AGENT_RUN_TIMED_OUT,
        EventType.AGENT_RUN_ORPHANED,
    ):
        return

    payload = event.payload

    agent.last_activity_id = event.id
    agent.last_activity_type = event.event_type
    agent.last_activity_at = event.occurred_at
    agent.conversation_id = event.conversation_id or agent.conversation_id
    agent.orchestration_run_id = event.orchestration_run_id or agent.orchestration_run_id
    agent.agent_run_id = event.agent_run_id or agent.agent_run_id
    agent.task_id = event.task_id or agent.task_id

    # Agent identity from payload on first assignment.
    if not agent.agent:
        agent.agent = str(payload.get("agent", ""))
    if not agent.role:
        agent.role = str(payload.get("role", ""))

    new_status = _AGENT_STATUS_MAP.get(event.event_type)
    if new_status:
        agent.status = new_status

    if event.event_type == EventType.AGENT_RUN_STARTED:
        if not agent.started_at:
            agent.started_at = event.occurred_at
        agent.external_session_id = str(payload.get("external_session_id", agent.external_session_id))

    elif event.event_type == EventType.AGENT_RUN_COMPLETED:
        agent.completed_at = event.occurred_at
        agent.completion_summary = str(payload.get("summary", payload.get("completion_summary", "")))

    elif event.event_type == EventType.AGENT_RUN_FAILED:
        agent.completed_at = event.occurred_at

    elif event.event_type == EventType.AGENT_RUN_TIMED_OUT:
        agent.completed_at = event.occurred_at
        agent.idle_timeout_at = str(payload.get("idle_timeout_at", ""))
        agent.hard_timeout_at = str(payload.get("hard_timeout_at", ""))

    elif event.event_type == EventType.AGENT_RUN_ORPHANED:
        agent.completed_at = event.occurred_at

    elif event.event_type == EventType.AGENT_RUN_WAITING_FOR_APPROVAL:
        pass  # status already set by _AGENT_STATUS_MAP


# ---------------------------------------------------------------------------
# Usage reducer
# ---------------------------------------------------------------------------


def _apply_usage_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    """Accumulate token counts on the associated agent state."""
    payload = event.payload
    agent_run_id = event.agent_run_id

    # Update agent-scoped token totals.
    _in = int(payload.get("input_tokens", 0))
    _out = int(payload.get("output_tokens", 0))
    _total = int(payload.get("total_tokens", _in + _out))

    if agent_run_id is not None:
        # Find the agent by aggregate_id that has this agent_run_id.
        for agent in snap.agents.values():
            if agent.agent_run_id == agent_run_id:
                agent.token_input += _in
                agent.token_output += _out
                agent.cached_input_tokens += int(payload.get("cached_input_tokens", 0))
                agent.reasoning_output_tokens += int(payload.get("reasoning_output_tokens", 0))
                agent.total_tokens += _total
                agent.model = str(payload.get("model", agent.model))
                break

    # Also update cross-agent token totals keyed by agent name.
    agent_name = str(payload.get("agent", event.actor))
    totals = snap.token_totals.setdefault(agent_name, {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    })
    _in = int(payload.get("input_tokens", 0))
    _out = int(payload.get("output_tokens", 0))
    _total = int(payload.get("total_tokens", _in + _out))
    totals["input_tokens"] += _in
    totals["output_tokens"] += _out
    totals["cached_input_tokens"] += int(payload.get("cached_input_tokens", 0))
    totals["reasoning_output_tokens"] += int(payload.get("reasoning_output_tokens", 0))
    totals["total_tokens"] += _total
    totals["requests"] += 1


# ---------------------------------------------------------------------------
# Approval reducer
# ---------------------------------------------------------------------------


def _apply_approval_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    agg_id = event.aggregate_id
    approval = snap.approvals.get(agg_id)

    if approval is None:
        approval = RuntimeApprovalState(aggregate_id=agg_id)
        snap.approvals[agg_id] = approval

    # Terminal state protection.
    if approval.is_terminal:
        return

    payload = event.payload
    approval.conversation_id = event.conversation_id or approval.conversation_id
    approval.orchestration_run_id = event.orchestration_run_id or approval.orchestration_run_id
    approval.agent_run_id = event.agent_run_id or approval.agent_run_id
    approval.task_id = event.task_id or approval.task_id

    if event.event_type == EventType.APPROVAL_REQUESTED:
        approval.status = "requested"
        approval.kind = str(payload.get("kind", ""))
        approval.summary = str(payload.get("summary", ""))
        approval.command_summary = str(payload.get("command_summary", payload.get("command", "")))
        approval.tool_name = str(payload.get("tool_name", ""))
        approval.telegram_callback_id = str(payload.get("telegram_callback_id", ""))
        approval.requested_at = event.occurred_at

    elif event.event_type == EventType.APPROVAL_RESOLVED:
        approval.status = "resolved"
        approval.decision = str(payload.get("decision", ""))
        approval.resolver = str(payload.get("resolver", ""))
        approval.resolved_at = event.occurred_at

    elif event.event_type == EventType.APPROVAL_EXPIRED:
        approval.status = "expired"
        approval.resolved_at = event.occurred_at


# ---------------------------------------------------------------------------
# Timeout / verification event handlers
# ---------------------------------------------------------------------------


def _apply_timeout_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    """Record timeout info on the affected agent state."""
    payload = event.payload
    agent_run_id = event.agent_run_id

    if agent_run_id is not None:
        for agent in snap.agents.values():
            if agent.agent_run_id == agent_run_id and not agent.is_terminal:
                if event.event_type == EventType.WATCHDOG_IDLE_TIMEOUT:
                    agent.idle_timeout_at = event.occurred_at
                elif event.event_type == EventType.WATCHDOG_HARD_TIMEOUT:
                    agent.hard_timeout_at = event.occurred_at
                break


def _apply_verification_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    """Update orchestration context from verification events."""
    payload = event.payload
    orch_run_id = event.orchestration_run_id

    if orch_run_id is not None:
        for orch in snap.orchestrations.values():
            if orch.orchestration_run_id == orch_run_id and not orch.is_terminal:
                orch.last_event_id = event.id
                orch.last_event_type = event.event_type
                orch.last_event_at = event.occurred_at

                if event.event_type == EventType.VERIFICATION_DECISION_RECORDED:
                    orch.last_verification_result = str(payload.get("decision", ""))
                elif event.event_type == EventType.VERIFICATION_RETRY_REQUESTED:
                    orch.verify_round = int(payload.get("verify_round", orch.verify_round))
                break
