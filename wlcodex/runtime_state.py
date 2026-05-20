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

_CONVERSATION_TERMINAL_STATES = frozenset({
    "passed", "failed", "aborted", "done",
})

# Orchestration phase -> conversation state
_ORCH_PHASE_TO_CONVERSATION_STATE: dict[str, str] = {
    "queued": "new",
    "running_analysis": "analysis",
    "running_implementation": "implementation",
    "running_verification": "verification",
    "retrying_implementation": "implementation",
    "completed": "passed",
    "failed": "failed",
    "cancelled": "aborted",
}

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
class TerminalSessionInfo:
    """Terminal session lifecycle state reconstructed from events.

    Keeps track of attach/detach/abort without depending on process liveness.
    Actual reattach logic lives in recovery; this only replays recorded facts.
    """

    agent: str = ""
    external_session_id: str = ""
    strategy: str = ""
    status: str = "detached"
    conversation_id: int | None = None
    chat_id: int = 0
    last_event_id: int = 0
    last_event_at: str = ""


@dataclass
class SurfaceCursorState:
    """Per-surface cursor position."""

    surface: str = ""
    position: int = 0


@dataclass
class SurfaceViewState:
    """Per-chat surface state reconstructed from runtime events.

    This is the materialised view that both Product and Terminal surfaces
    consult.  It is derived purely from events — never from mutable state.
    """

    chat_id: int = 0
    conversation_id: int | None = None
    active_mode: str = "product"
    selected_terminal_agent: str = ""
    cursors: dict[str, SurfaceCursorState] = field(default_factory=dict)
    terminal_sessions: dict[str, TerminalSessionInfo] = field(default_factory=dict)
    pending_context: list[dict] = field(default_factory=list)
    last_event_id: int = 0
    last_event_at: str = ""


@dataclass
class SurfaceStateSnapshot:
    """Complete surface state across all chats at a point in the event stream."""

    by_chat: dict[int, SurfaceViewState] = field(default_factory=dict)
    last_event_id: int = 0
    last_event_at: str = ""


@dataclass
class RuntimeConversationState:
    """Current state of one conversation reconstructed from events."""

    aggregate_id: str
    conversation_id: int | None = None
    chat_id: int = 0
    state: str = "new"
    title: str = ""
    mode: str = ""
    workspace_alias: str = ""
    active_task_id: int | None = None
    pending_context: list[dict] = field(default_factory=list)
    last_event_id: int = 0
    last_event_type: str = ""
    last_event_at: str = ""
    started_at: str = ""
    closed_at: str = ""
    has_pass_verification: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in _CONVERSATION_TERMINAL_STATES


@dataclass
class RuntimeStateSnapshot:
    """Complete runtime state at a point in the event stream."""

    agents: dict[str, RuntimeAgentState] = field(default_factory=dict)
    orchestrations: dict[str, RuntimeOrchestrationState] = field(default_factory=dict)
    approvals: dict[str, RuntimeApprovalState] = field(default_factory=dict)
    conversations: dict[str, RuntimeConversationState] = field(default_factory=dict)
    token_totals: dict[str, dict[str, int]] = field(default_factory=dict)
    last_event_id: int = 0
    last_event_at: str = ""

    def agent(self, aggregate_id: str) -> RuntimeAgentState | None:
        return self.agents.get(aggregate_id)

    def orchestration(self, aggregate_id: str) -> RuntimeOrchestrationState | None:
        return self.orchestrations.get(aggregate_id)

    def approval(self, aggregate_id: str) -> RuntimeApprovalState | None:
        return self.approvals.get(aggregate_id)

    def conversation(self, aggregate_id: str) -> RuntimeConversationState | None:
        return self.conversations.get(aggregate_id)

    def active_conversation_for_chat(self, chat_id: int) -> RuntimeConversationState | None:
        """Return the active non-terminal conversation for a chat, or None."""
        for conv in self.conversations.values():
            if conv.chat_id == chat_id and not conv.is_terminal:
                return conv
        return None


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
# Surface state replay reducer (dual-surface core)
# ---------------------------------------------------------------------------


def replay_surface_events(events: list[Any]) -> SurfaceStateSnapshot:
    """Replay runtime events into a SurfaceStateSnapshot.

    Pure function: no side effects, no database access.
    Deterministic: same events always produce the same state.

    Reconstructs per-chat active mode, per-surface cursors, terminal session
    lifecycle, and pending product context.  Both Product and Terminal surfaces
    share this as their single source of truth.
    """
    snap = SurfaceStateSnapshot()

    for event in events:
        _apply_surface_event(snap, event)

    return snap


def _get_or_create_chat(snap: SurfaceStateSnapshot, chat_id: int) -> SurfaceViewState:
    if chat_id not in snap.by_chat:
        view = SurfaceViewState(chat_id=chat_id)
        view.cursors["product"] = SurfaceCursorState(surface="product", position=0)
        view.cursors["terminal"] = SurfaceCursorState(surface="terminal", position=0)
        snap.by_chat[chat_id] = view
    return snap.by_chat[chat_id]


def _ensure_cursor(view: SurfaceViewState, surface: str) -> SurfaceCursorState:
    if surface not in view.cursors:
        view.cursors[surface] = SurfaceCursorState(surface=surface, position=0)
    return view.cursors[surface]


def _apply_surface_event(snap: SurfaceStateSnapshot, event: Any) -> None:
    snap.last_event_id = event.id
    snap.last_event_at = event.occurred_at

    etype = event.event_type
    payload = event.payload
    chat_id = int(payload.get("chat_id", 0))
    if not chat_id:
        return

    view = _get_or_create_chat(snap, chat_id)
    view.last_event_id = event.id
    view.last_event_at = event.occurred_at
    view.conversation_id = event.conversation_id or view.conversation_id

    # --- Mode switching ---
    if etype == EventType.CONVERSATION_MODE_SWITCHED:
        view.active_mode = str(payload.get("to_mode", view.active_mode))
        agent = str(payload.get("active_agent", ""))
        if agent:
            view.selected_terminal_agent = agent

    # --- Cursor advancement ---
    elif etype == EventType.SURFACE_CURSOR_ADVANCED:
        surface = str(payload.get("surface", ""))
        position = int(payload.get("position", 0))
        if surface:
            cursor = _ensure_cursor(view, surface)
            if position > cursor.position:
                cursor.position = position

    # --- Terminal session lifecycle ---
    elif etype == EventType.TERMINAL_SESSION_ATTACHED:
        agent = str(payload.get("agent", ""))
        if agent:
            view.terminal_sessions[agent] = TerminalSessionInfo(
                agent=agent,
                external_session_id=str(payload.get("external_session_id", "")),
                strategy=str(payload.get("strategy", "")),
                status="attached",
                conversation_id=event.conversation_id,
                chat_id=chat_id,
                last_event_id=event.id,
                last_event_at=event.occurred_at,
            )

    elif etype == EventType.TERMINAL_SESSION_DETACHED:
        agent = str(payload.get("agent", ""))
        status = str(payload.get("status", "detached"))
        if agent and agent in view.terminal_sessions:
            view.terminal_sessions[agent].status = status
            view.terminal_sessions[agent].last_event_id = event.id
            view.terminal_sessions[agent].last_event_at = event.occurred_at

    elif etype == EventType.TERMINAL_SESSION_ABORTED:
        agent = str(payload.get("agent", ""))
        if agent and agent in view.terminal_sessions:
            view.terminal_sessions[agent].status = "aborted"
            view.terminal_sessions[agent].last_event_id = event.id
            view.terminal_sessions[agent].last_event_at = event.occurred_at

    # --- Product display frame advances product cursor ---
    elif etype == EventType.PRODUCT_DISPLAY_FRAME:
        surface = str(payload.get("surface", "product"))
        position = int(payload.get("position", 0))
        cursor = _ensure_cursor(view, surface)
        if position > cursor.position:
            cursor.position = position

    # --- Terminal output frame advances terminal cursor ---
    elif etype == EventType.TERMINAL_SESSION_OUTPUT_FRAME:
        surface = str(payload.get("surface", "terminal"))
        position = int(payload.get("position", 0))
        cursor = _ensure_cursor(view, surface)
        if position > cursor.position:
            cursor.position = position

    # --- Pending context for product mode ---
    elif etype == EventType.PRODUCT_PENDING_CONTEXT_RECORDED:
        view.pending_context.append({
            "telegram_message_id": payload.get("telegram_message_id", 0),
            "text_preview": str(payload.get("text_preview", "")),
            "recorded_at": event.occurred_at,
        })


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

    # --- Conversation events ---
    elif etype in (
        EventType.CONVERSATION_STARTED,
        EventType.CONVERSATION_ACTIVATED,
        EventType.CONVERSATION_STATE_CHANGED,
        EventType.CONVERSATION_CLOSED,
        EventType.CONVERSATION_MODE_SWITCHED,
        EventType.USER_CONTEXT_APPENDED,
        EventType.CONVERSATION_PENDING_CONTEXT_RECORDED,
        EventType.CONVERSATION_PENDING_CONTEXT_REVIEWED,
    ):
        _apply_conversation_event(snap, event)

    # --- Approval supersession ---
    elif etype == EventType.APPROVAL_SUPERSEDED:
        _apply_approval_superseded(snap, event)

    # --- Surface / recovery pass-through (handled by replay_surface_events) ---
    elif etype in (
        EventType.SURFACE_CURSOR_ADVANCED,
        EventType.TERMINAL_SESSION_ATTACHED,
        EventType.TERMINAL_SESSION_DETACHED,
        EventType.TERMINAL_SESSION_ABORTED,
        EventType.TERMINAL_SESSION_INPUT_SENT,
        EventType.TERMINAL_SESSION_OUTPUT_FRAME,
        EventType.PRODUCT_DISPLAY_FRAME,
        EventType.PRODUCT_PENDING_CONTEXT_RECORDED,
        EventType.WORKBENCH_CREATED,
        EventType.WORKBENCH_VIEW_CHANGED,
        EventType.WORKBENCH_EXECUTION_MODE_SELECTED,
        EventType.WORKBENCH_ROUTE_DECIDED,
        EventType.ONSITE_SESSION_STARTED,
        EventType.ONSITE_SESSION_ATTACHED,
        EventType.ONSITE_SESSION_DETACHED,
        EventType.ONSITE_SESSION_ORPHANED,
        EventType.ONSITE_INPUT_SENT,
        EventType.ONSITE_OUTPUT_FRAME,
        EventType.ONSITE_CURSOR_ADVANCED,
        EventType.COCKPIT_CURSOR_ADVANCED,
        EventType.COCKPIT_SUMMARY_RENDERED,
        EventType.SYSTEM_RECOVERY_STARTED,
        EventType.SYSTEM_RECOVERY_COMPLETED,
        EventType.RUNTIME_CAPABILITY_MISSING,
    ):
        pass  # informational — surface/workbench state is in projection replay

    elif etype == EventType.APPROVAL_STALE_BUTTON_IGNORED:
        pass  # informational diagnostic

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
        # Propagate phase change to conversation state.
        conv_state = _ORCH_PHASE_TO_CONVERSATION_STATE.get(phase)
        if conv_state and event.conversation_id is not None:
            for conv in snap.conversations.values():
                if conv.conversation_id == event.conversation_id and not conv.is_terminal:
                    conv.state = conv_state
                    conv.last_event_id = event.id
                    conv.last_event_at = event.occurred_at
                    break

    elif event.event_type == EventType.RUN_COMPLETED:
        if orch.last_verification_result != "pass":
            orch.status = "failed"
            orch.completed_at = event.occurred_at
            orch.failure_reason = "run_completed_without_verification_pass"
            orch.current_phase = "failed"
            _propagate_conversation_state(snap, event, "failed")
            return
        orch.status = "completed"
        orch.completed_at = event.occurred_at
        orch.current_phase = "completed"
        _propagate_conversation_state(snap, event, "passed")

    elif event.event_type == EventType.RUN_FAILED:
        orch.status = "failed"
        orch.completed_at = event.occurred_at
        orch.failure_reason = str(payload.get("reason", ""))
        orch.last_active_agent = str(payload.get("last_active_agent", orch.last_active_agent))
        _propagate_conversation_state(snap, event, "failed")

    elif event.event_type == EventType.RUN_CANCELLED:
        orch.status = "cancelled"
        orch.completed_at = event.occurred_at
        _propagate_conversation_state(snap, event, "aborted")

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

    # Also update conversation's pass verification flag.
    if event.event_type == EventType.VERIFICATION_DECISION_RECORDED:
        conv_id = event.conversation_id
        if conv_id is not None:
            for conv in snap.conversations.values():
                if conv.conversation_id == conv_id:
                    if str(payload.get("decision", "")) == "pass":
                        conv.has_pass_verification = True
                    break


def _propagate_conversation_state(
    snap: RuntimeStateSnapshot, event: Any, state: str
) -> None:
    """Propagate a terminal or phase state to the linked conversation."""
    if event.conversation_id is None:
        return
    for conv in snap.conversations.values():
        if conv.conversation_id == event.conversation_id and not conv.is_terminal:
            conv.state = state
            conv.last_event_id = event.id
            conv.last_event_at = event.occurred_at
            break


# ---------------------------------------------------------------------------
# Conversation reducer
# ---------------------------------------------------------------------------


def _apply_conversation_event(snap: RuntimeStateSnapshot, event: Any) -> None:
    agg_id = event.aggregate_id
    conv = snap.conversations.get(agg_id)

    if conv is None:
        conv = RuntimeConversationState(aggregate_id=agg_id)
        snap.conversations[agg_id] = conv

    # Terminal state protection.
    if conv.is_terminal and event.event_type not in (
        EventType.CONVERSATION_CLOSED,
        EventType.CONVERSATION_STATE_CHANGED,
    ):
        return

    conv.last_event_id = event.id
    conv.last_event_type = event.event_type
    conv.last_event_at = event.occurred_at
    conv.conversation_id = event.conversation_id or conv.conversation_id

    payload = event.payload

    if event.event_type == EventType.CONVERSATION_STARTED:
        conv.state = "new"
        conv.chat_id = int(payload.get("chat_id", conv.chat_id))
        conv.title = str(payload.get("title", conv.title))
        conv.mode = str(payload.get("mode", conv.mode))
        conv.workspace_alias = str(payload.get("workspace_alias", conv.workspace_alias))
        if not conv.started_at:
            conv.started_at = event.occurred_at

    elif event.event_type == EventType.CONVERSATION_ACTIVATED:
        conv.state = str(payload.get("state", conv.state))

    elif event.event_type == EventType.CONVERSATION_STATE_CHANGED:
        new_state = str(payload.get("to", ""))
        if new_state:
            conv.state = new_state
        # Phase-based state also reflected from run.phase.changed
        phase = str(payload.get("phase", ""))
        mapped = _ORCH_PHASE_TO_CONVERSATION_STATE.get(phase)
        if mapped:
            conv.state = mapped

    elif event.event_type == EventType.CONVERSATION_CLOSED:
        conv.state = str(payload.get("reason", conv.state))
        conv.closed_at = event.occurred_at

    elif event.event_type == EventType.USER_CONTEXT_APPENDED:
        conv.pending_context.append({
            "telegram_message_id": payload.get("telegram_message_id", 0),
            "text_preview": str(payload.get("text_preview", "")),
            "delivery_policy": str(payload.get("delivery_policy", "")),
            "appended_at": event.occurred_at,
        })

    elif event.event_type == EventType.CONVERSATION_PENDING_CONTEXT_RECORDED:
        conv.pending_context.append({
            "telegram_message_id": payload.get("telegram_message_id", 0),
            "text_preview": str(payload.get("text_preview", "")),
            "delivery_policy": "codex_phase_boundary_review",
            "recorded_at": event.occurred_at,
        })

    elif event.event_type == EventType.CONVERSATION_MODE_SWITCHED:
        new_mode = str(payload.get("to_mode", ""))
        if new_mode:
            conv.mode = new_mode

    elif event.event_type == EventType.CONVERSATION_PENDING_CONTEXT_REVIEWED:
        conv.pending_context = [
            item for item in conv.pending_context
            if item.get("telegram_message_id") != payload.get("telegram_message_id")
        ]


def _apply_approval_superseded(snap: RuntimeStateSnapshot, event: Any) -> None:
    """Mark all pending approvals as superseded for the conversation."""
    conv_id = event.conversation_id
    for approval in snap.approvals.values():
        if approval.conversation_id == conv_id and not approval.is_terminal:
            approval.status = "cancelled"
            approval.error = "superseded_by_user_context"


# ---------------------------------------------------------------------------
# Workbench runtime state — pure replay for Task 6 recovery projection
# ---------------------------------------------------------------------------

_VIEW_MODE_MAP: dict[str, str] = {
    "product": "cockpit",
    "terminal": "onsite",
    "cockpit": "cockpit",
    "onsite": "onsite",
}

_VALID_EXECUTION_MODES = frozenset({"orchestrated", "codex_direct", "claude_direct"})

_COCKPIT_SURFACES = frozenset({"product", "cockpit"})
_ONSITE_SURFACES = frozenset({"terminal", "onsite"})


@dataclass
class WorkbenchRuntimeState:
    """Workbench state reconstructed purely from runtime events.

    This is NOT the source of truth — the runtime event log is.
    This projection exists so recovery can answer "what view /
    execution mode / agent / cursor were we in" after a restart.
    """

    view: str = "cockpit"
    execution_mode: str = "orchestrated"
    active_agent: str = ""
    cockpit_cursor: int = 0
    onsite_cursor: int = 0
    onsite_session_status: str = "detached"
    onsite_external_session_id: str = ""
    onsite_orphan_reason: str = ""


def replay_workbench_events(events: list[Any]) -> WorkbenchRuntimeState:
    """Replay runtime events into a workbench-level projection.

    Pure function: no side effects, no database access.
    Deterministic: same events always produce the same state.

    Reconstructs current view, execution mode, active onsite agent,
    cockpit/onsite cursors, and onsite session lifecycle including
    orphaned status.
    """
    state = WorkbenchRuntimeState()

    for event in events:
        _apply_workbench_event(state, event)

    return state


def _apply_workbench_event(state: WorkbenchRuntimeState, event: Any) -> None:
    etype = event.event_type
    payload = event.payload

    # --- View change via conversation.mode.switched ---
    if etype == EventType.CONVERSATION_MODE_SWITCHED:
        to_mode = str(payload.get("to_mode", ""))
        if to_mode:
            state.view = _VIEW_MODE_MAP.get(to_mode, state.view)
        agent = str(payload.get("active_agent", ""))
        if agent:
            state.active_agent = agent

    # --- Execution mode ---
    elif etype == EventType.WORKBENCH_EXECUTION_MODE_SELECTED:
        mode = str(payload.get("execution_mode", ""))
        if mode in _VALID_EXECUTION_MODES:
            state.execution_mode = mode

    # --- View change via workbench.view.changed ---
    elif etype == EventType.WORKBENCH_VIEW_CHANGED:
        view = str(payload.get("view", ""))
        if view:
            state.view = _VIEW_MODE_MAP.get(view, state.view)

    # --- Cursor advancement ---
    elif etype == EventType.SURFACE_CURSOR_ADVANCED:
        surface = str(payload.get("surface", ""))
        position = int(payload.get("position", 0))
        if surface in _COCKPIT_SURFACES and position > state.cockpit_cursor:
            state.cockpit_cursor = position
        elif surface in _ONSITE_SURFACES and position > state.onsite_cursor:
            state.onsite_cursor = position

    # --- Terminal / onsite session attach ---
    elif etype == EventType.TERMINAL_SESSION_ATTACHED:
        agent = str(payload.get("agent", ""))
        if agent:
            state.active_agent = agent
            state.onsite_session_status = "attached"
            state.onsite_external_session_id = str(
                payload.get("external_session_id", "")
            )
            state.onsite_orphan_reason = ""

    # --- Terminal / onsite session detach ---
    elif etype == EventType.TERMINAL_SESSION_DETACHED:
        agent = str(payload.get("agent", ""))
        if agent and agent == state.active_agent:
            if state.onsite_session_status != "orphaned":
                state.onsite_session_status = str(
                    payload.get("status", "detached")
                )

    # --- Terminal / onsite session aborted ---
    elif etype == EventType.TERMINAL_SESSION_ABORTED:
        agent = str(payload.get("agent", ""))
        if agent and agent == state.active_agent:
            state.onsite_session_status = "aborted"

    # --- Onsite session orphaned ---
    elif etype == EventType.ONSITE_SESSION_ORPHANED:
        agent = str(payload.get("agent", ""))
        if agent:
            state.active_agent = agent
            state.onsite_session_status = "orphaned"
            state.onsite_orphan_reason = str(payload.get("reason", ""))

    # --- Agent run orphaned → mark onsite session orphaned ---
    elif etype == EventType.AGENT_RUN_ORPHANED:
        agent = str(payload.get("agent", ""))
        if agent and agent == state.active_agent:
            state.onsite_session_status = "orphaned"
        elif agent and not state.active_agent:
            state.active_agent = agent
            state.onsite_session_status = "orphaned"

    # --- System recovery started → orphan active sessions ---
    elif etype == EventType.SYSTEM_RECOVERY_STARTED:
        if state.onsite_session_status == "attached":
            state.onsite_session_status = "orphaned"
            if not state.onsite_orphan_reason:
                state.onsite_orphan_reason = str(
                    payload.get("reason", "daemon_restart")
                )
