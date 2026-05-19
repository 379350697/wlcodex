"""Contract tests for the event-sourced conversation state machine.

These tests express every routing rule from the 2026-05-19 spec.
They test the PRODUCTION code path (conversation_state_machine.route_message)
as well as pure state reconstruction helpers.
"""

from __future__ import annotations

import pytest

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.conversation_state_machine import (
    RouteDecision,
    classify_intent,
    route_message,
    _EXPLICIT_NEW_PHRASES,
    _DIAGNOSTIC_COMMANDS,
    _IMMEDIATE_REVIEW_STATES,
    _PHASE_BOUNDARY_STATES,
    MID_RUN_ACKNOWLEDGEMENT,
    build_workspace_busy_buttons,
    decode_busy_callback,
    BUSY_APPEND,
    BUSY_QUEUE,
    BUSY_CANCEL,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _event(
    event_type: str,
    aggregate_type: str = AggregateType.CONVERSATION,
    aggregate_id: str = "conv-1",
    correlation_id: str = "corr-1",
    conversation_id: int = 1,
    payload: dict | None = None,
    *,
    source: str = EventSource.CONTROLLER,
    actor: str = "controller",
    visibility: str = Visibility.OPERATOR,
    orchestration_run_id: int | None = None,
    agent_run_id: int | None = None,
    task_id: int | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        correlation_id=correlation_id,
        source=source,
        actor=actor,
        visibility=visibility,
        payload=payload or {},
        occurred_at=now_iso(),
        conversation_id=conversation_id,
        orchestration_run_id=orchestration_run_id,
        agent_run_id=agent_run_id,
        task_id=task_id,
    )


# ---------------------------------------------------------------------------
# Conversation state reducer (minimal pure impl for test validation)
# ---------------------------------------------------------------------------

_CONVERSATION_TERMINAL_STATES = frozenset({"passed", "failed", "aborted", "done"})

_CONVERSATION_STATE_EVENTS: dict[str, str] = {
    EventType.CONVERSATION_STARTED: "new",
    # state.changed with explicit "to" overrides
    EventType.RUN_REQUESTED: "new",
    EventType.RUN_STARTED: "analysis",
    EventType.RUN_PHASE_CHANGED: None,  # determined by payload.phase
    EventType.APPROVAL_REQUESTED: "waiting_approval",
}

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


def _is_new_conversation_trigger(text: str) -> bool:
    """Detect explicit new-conversation phrases."""
    if text.strip().startswith("/new"):
        return True
    normalized = text.strip()
    triggers = {"新任务", "另起一个", "重新开始", "重来", "新对话"}
    return normalized in triggers


def _is_diagnostic_command(text: str) -> bool:
    """Return True if text is a diagnostic/inspection command."""
    cmd = text.strip().split()[0] if text.strip() else ""
    return cmd in {
        "/status", "/trace", "/health", "/diff", "/files",
        "/tail", "/events", "/list", "/help", "/start",
        "/model", "/sessions", "/stop", "/switch",
        "/codex", "/claude", "/auto", "/verify",
        "/pause", "/abort", "/archive", "/fork",
        "/continue", "/steer", "/task", "/show",
        "/permission",
    }


def determine_route(
    text: str,
    active_conversation_state: str | None,
    workspace_busy: bool = False,
) -> dict:
    """Pure router: returns a route decision dict.

    This is the core logic that will be extracted into ConversationStateMachine.
    """
    is_new_trigger = _is_new_conversation_trigger(text)
    is_diagnostic = _is_diagnostic_command(text)
    is_terminal = active_conversation_state in _CONVERSATION_TERMINAL_STATES if active_conversation_state else False
    no_active = active_conversation_state is None

    # Diagnostic commands never create work.
    if is_diagnostic:
        return {
            "route": "diagnostic",
            "reason": "inspection_command",
            "new_conversation": False,
        }

    # Workspace busy: any new-conversation scenario must present user choice.
    would_create = is_new_trigger or no_active or is_terminal
    if workspace_busy and would_create:
        return {
            "route": "workspace_busy",
            "reason": "workspace_locked",
            "new_conversation": False,
            "conversation_state": active_conversation_state,
        }

    # New conversation triggers.
    if is_new_trigger:
        return {
            "route": "new_conversation",
            "reason": "explicit_new_trigger",
            "new_conversation": True,
        }

    # No active conversation -> create new.
    if no_active:
        return {
            "route": "new_conversation",
            "reason": "no_active_conversation",
            "new_conversation": True,
        }

    # Terminal active conversation -> create new.
    if is_terminal:
        return {
            "route": "new_conversation",
            "reason": f"active_conversation_terminal_{active_conversation_state}",
            "new_conversation": True,
        }

    # Active non-terminal conversation -> append.
    return {
        "route": "append_active_conversation",
        "reason": "active_conversation_non_terminal",
        "new_conversation": False,
        "conversation_state": active_conversation_state,
    }


def determine_followup_policy(conversation_state: str) -> str:
    """Return the follow-up delivery policy for a given conversation state."""
    if conversation_state in ("new", "analysis", "waiting_approval", "needs_user"):
        return "codex_immediate_review"
    elif conversation_state in ("implementation", "verification"):
        return "codex_phase_boundary_review"
    return "not_applicable"


# ---------------------------------------------------------------------------
# Tests: new conversation creation
# ---------------------------------------------------------------------------

def test_no_active_conversation_creates_new():
    """A chat with no active conversation creates one on normal text."""
    route = determine_route("fix the login bug", None)
    assert route["route"] == "new_conversation"
    assert route["reason"] == "no_active_conversation"
    assert route["new_conversation"] is True


@pytest.mark.parametrize("terminal_state", ["passed", "failed", "aborted", "done"])
def test_terminal_conversation_creates_new(terminal_state):
    """Any normal text after a terminal conversation starts a new one."""
    route = determine_route("next task please", terminal_state)
    assert route["route"] == "new_conversation"
    assert route["reason"] == f"active_conversation_terminal_{terminal_state}"
    assert route["new_conversation"] is True


def test_slash_new_always_creates_new():
    """/new always creates a new conversation."""
    for state in ("new", "analysis", "implementation", "verification", "passed", None):
        route = determine_route("/new", state)
        assert route["route"] == "new_conversation"
        assert route["reason"] == "explicit_new_trigger"
        assert route["new_conversation"] is True


@pytest.mark.parametrize("trigger_phrase", [
    "新任务", "另起一个", "重新开始", "重来", "新对话",
])
def test_explicit_new_phrases_create_new_conversation(trigger_phrase):
    """Explicit start-over phrases create a new conversation."""
    route = determine_route(trigger_phrase, "implementation")
    assert route["route"] == "new_conversation"
    assert route["new_conversation"] is True


# ---------------------------------------------------------------------------
# Tests: append to active conversation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("active_state", [
    "new", "analysis", "waiting_approval", "needs_user",
])
def test_non_terminal_followup_appends(active_state):
    """Follow-up during non-terminal states appends to active conversation."""
    route = determine_route("also, please add tests", active_state)
    assert route["route"] == "append_active_conversation"
    assert route["new_conversation"] is False


def test_followup_during_implementation_appends():
    """Follow-up during implementation appends (not sent directly to Claude)."""
    route = determine_route("don't forget the edge case", "implementation")
    assert route["route"] == "append_active_conversation"
    assert route["new_conversation"] is False


def test_followup_during_verification_appends():
    """Follow-up during verification appends."""
    route = determine_route("one more thing to check", "verification")
    assert route["route"] == "append_active_conversation"
    assert route["new_conversation"] is False


# ---------------------------------------------------------------------------
# Tests: diagnostic commands never create work
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("diag_cmd", [
    "/status", "/trace", "/health", "/diff", "/files",
    "/help", "/list", "/sessions", "/model", "/permission",
])
def test_diagnostic_commands_do_not_create_conversations(diag_cmd):
    """Diagnostic/inspection commands are reads, never create work."""
    for state in ("new", "analysis", "implementation", "verification", None):
        route = determine_route(diag_cmd, state)
        assert route["route"] == "diagnostic"
        assert route["new_conversation"] is False, \
            f"{diag_cmd} should not create conversation, state={state}"


# ---------------------------------------------------------------------------
# Tests: follow-up delivery policy
# ---------------------------------------------------------------------------

def test_analysis_phase_followup_is_immediate_review():
    """Follow-up during analysis/planning goes to Codex immediately."""
    for state in ("analysis", "waiting_approval", "needs_user"):
        assert determine_followup_policy(state) == "codex_immediate_review"


def test_implementation_phase_followup_is_phase_boundary():
    """Follow-up during implementation waits for phase boundary."""
    assert determine_followup_policy("implementation") == "codex_phase_boundary_review"


def test_verification_phase_followup_is_phase_boundary():
    """Follow-up during verification waits for phase boundary."""
    assert determine_followup_policy("verification") == "codex_phase_boundary_review"


# ---------------------------------------------------------------------------
# Tests: workspace busy produces user choice
# ---------------------------------------------------------------------------

def test_workspace_busy_returns_busy_route():
    """When workspace is busy and user triggers new work, return busy route."""
    route = determine_route(
        "/new",
        active_conversation_state="implementation",
        workspace_busy=True,
    )
    assert route["route"] == "workspace_busy"


def test_workspace_busy_on_terminal():
    """Workspace busy on terminal conversation still returns busy."""
    route = determine_route(
        "new task",
        active_conversation_state="passed",
        workspace_busy=True,
    )
    assert route["route"] == "workspace_busy"


# ---------------------------------------------------------------------------
# Tests: orchestration phase -> conversation state mapping
# ---------------------------------------------------------------------------

def test_orch_phase_maps_to_correct_conversation_state():
    """Verify the phase-to-state mapping covers all phases."""
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["running_analysis"] == "analysis"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["running_implementation"] == "implementation"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["running_verification"] == "verification"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["completed"] == "passed"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["failed"] == "failed"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["cancelled"] == "aborted"


# ---------------------------------------------------------------------------
# Tests: conversation state reconstruction from events
# ---------------------------------------------------------------------------

def test_conversation_state_from_events_analysis():
    """Reconstruct conversation state from phase change events."""
    events = [
        _event(EventType.CONVERSATION_STARTED, aggregate_id="conv-1"),
        _event(EventType.RUN_PHASE_CHANGED, aggregate_id="conv-1",
               payload={"phase": "running_analysis"}, conversation_id=1),
    ]
    state = _replay_conversation_state(events, "conv-1")
    assert state == "analysis"


def test_conversation_state_from_events_implementation():
    events = [
        _event(EventType.CONVERSATION_STARTED, aggregate_id="conv-1"),
        _event(EventType.RUN_PHASE_CHANGED, aggregate_id="conv-1",
               payload={"phase": "running_implementation"}, conversation_id=1),
    ]
    state = _replay_conversation_state(events, "conv-1")
    assert state == "implementation"


def test_conversation_state_from_events_terminal():
    events = [
        _event(EventType.CONVERSATION_STARTED, aggregate_id="conv-1"),
        _event(EventType.RUN_PHASE_CHANGED, aggregate_id="conv-1",
               payload={"phase": "running_verification"}, conversation_id=1),
        _event(EventType.VERIFICATION_DECISION_RECORDED, aggregate_id="conv-1",
               payload={"decision": "pass"}, conversation_id=1),
        _event(EventType.RUN_COMPLETED, aggregate_id="conv-1",
               payload={"phase": "completed"}, conversation_id=1),
    ]
    state = _replay_conversation_state(events, "conv-1")
    assert state == "passed"


# ===========================================================================
# Production code path tests: conversation_state_machine.route_message()
# ===========================================================================


def test_production_route_message_no_active_conversation():
    """Production route_message: no active conversation → new_conversation."""
    decision = route_message("fix the bug", active_conversation_state=None)
    assert decision.route == "new_conversation"
    assert decision.new_conversation is True
    assert decision.reason == "no_active_conversation"


@pytest.mark.parametrize("text", ["/new", "新任务", "另起一个", "重新开始"])
def test_production_route_message_explicit_new_trigger(text):
    """Production route_message: explicit new triggers always create new."""
    decision = route_message(text, active_conversation_state="analysis")
    assert decision.route == "new_conversation"
    assert decision.new_conversation is True


@pytest.mark.parametrize("state", ["analysis", "waiting_approval", "needs_user"])
def test_production_route_message_non_terminal_appends(state):
    """Production route_message: non-terminal states append by default."""
    decision = route_message("clarification", active_conversation_state=state)
    assert decision.route == "append_active_conversation"
    assert decision.new_conversation is False


def test_production_route_message_terminal_creates_new():
    """Production route_message: terminal state creates new conversation."""
    for state in ("passed", "failed", "aborted", "done"):
        decision = route_message("next task", active_conversation_state=state)
        assert decision.route == "new_conversation"
        assert decision.new_conversation is True


@pytest.mark.parametrize("cmd", ["/status", "/trace", "/health", "/diff", "/help"])
def test_production_route_message_diagnostic_commands(cmd):
    """Production route_message: diagnostic commands never create work."""
    decision = route_message(cmd, active_conversation_state=None)
    assert decision.route == "diagnostic"
    assert decision.new_conversation is False


def test_production_route_message_implementation_followup_policy():
    """Production: implementation follow-up gets phase_boundary_review."""
    decision = route_message(
        "don't forget edge case", active_conversation_state="implementation"
    )
    assert decision.route == "append_active_conversation"
    assert decision.delivery_policy == "codex_phase_boundary_review"
    assert decision.user_acknowledgement == MID_RUN_ACKNOWLEDGEMENT


def test_production_route_message_verification_followup_policy():
    """Production: verification follow-up gets phase_boundary_review."""
    decision = route_message("one more check", active_conversation_state="verification")
    assert decision.delivery_policy == "codex_phase_boundary_review"


def test_production_route_message_waiting_approval_supersession():
    """Production: waiting_approval follow-up triggers approval supersession."""
    decision = route_message(
        "clarify before approving", active_conversation_state="waiting_approval"
    )
    assert decision.route == "append_active_conversation"
    assert decision.requires_approval_supersession is True
    assert decision.delivery_policy == "codex_immediate_review"


def test_production_route_message_workspace_busy_blocks_new():
    """Production: workspace busy returns busy route for new-conversation triggers."""
    decision = route_message(
        "/new",
        active_conversation_state="implementation",
        workspace_busy=True,
        blocking_task_id=42,
    )
    assert decision.route == "workspace_busy"


def test_production_route_message_workspace_busy_on_terminal():
    """Production: workspace busy on terminal state still returns busy."""
    decision = route_message(
        "new task",
        active_conversation_state="passed",
        workspace_busy=True,
        blocking_task_id=99,
    )
    assert decision.route == "workspace_busy"


def test_production_route_message_normal_text_during_analysis_is_immediate():
    """Production: analysis follow-up gets codex_immediate_review."""
    decision = route_message("also add tests", active_conversation_state="analysis")
    assert decision.delivery_policy == "codex_immediate_review"


# ===========================================================================
# Production helper tests
# ===========================================================================


def test_production_classify_intent_new():
    assert classify_intent("/new") == "new_trigger"
    assert classify_intent("/new 测试") == "new_trigger"
    assert classify_intent("新任务") == "new_trigger"
    assert classify_intent("重新开始") == "new_trigger"


def test_production_classify_intent_diagnostic():
    assert classify_intent("/status") == "diagnostic"
    assert classify_intent("/trace") == "diagnostic"
    assert classify_intent("/help") == "diagnostic"


def test_production_classify_intent_normal():
    assert classify_intent("fix the login bug") == "normal_text"
    assert classify_intent("你好") == "normal_text"


def test_production_busy_buttons():
    buttons = build_workspace_busy_buttons(42)
    assert len(buttons) == 1
    assert len(buttons[0]) == 3
    assert buttons[0][0]["text"] == "追加到当前任务"
    assert buttons[0][0]["callback_data"] == "busy_append:42"
    assert buttons[0][1]["text"] == "排队新任务"
    assert buttons[0][2]["text"] == "取消"


def test_production_decode_busy_callback():
    assert decode_busy_callback("busy_append:42") == (BUSY_APPEND, 42)
    assert decode_busy_callback("busy_queue:99") == (BUSY_QUEUE, 99)
    assert decode_busy_callback("busy_cancel:7") == (BUSY_CANCEL, 7)
    assert decode_busy_callback("unknown:1") is None
    assert decode_busy_callback("busy_append:abc") is None


def test_production_immediate_review_states():
    assert "analysis" in _IMMEDIATE_REVIEW_STATES
    assert "waiting_approval" in _IMMEDIATE_REVIEW_STATES


def test_production_phase_boundary_states():
    assert "implementation" in _PHASE_BOUNDARY_STATES
    assert "verification" in _PHASE_BOUNDARY_STATES


def test_conversation_state_terminal_is_immutable():
    """Once terminal, non-terminal phase changes should be ignored."""
    events = [
        _event(EventType.CONVERSATION_STARTED, aggregate_id="conv-1"),
        _event(EventType.RUN_COMPLETED, aggregate_id="conv-1",
               payload={"phase": "completed"}, conversation_id=1),
        # This should NOT change the state back:
        _event(EventType.RUN_PHASE_CHANGED, aggregate_id="conv-1",
               payload={"phase": "running_analysis"}, conversation_id=1),
    ]
    state = _replay_conversation_state(events, "conv-1")
    assert state == "failed"  # RUN_COMPLETED without verification pass = failed


def test_two_messages_same_chat_one_conversation():
    """Two normal messages in same chat = 1 conversation, 2nd appends."""
    # First message: no active -> new conversation
    route1 = determine_route("fix login bug", None)
    assert route1["route"] == "new_conversation"

    # Second message: active conversation in analysis -> append
    route2 = determine_route("also handle null pointer", "analysis")
    assert route2["route"] == "append_active_conversation"
    assert route2["new_conversation"] is False


def test_followup_after_passed_creates_new():
    """After conversation passes, follow-up starts a new conversation."""
    route = determine_route("next thing to fix", "passed")
    assert route["route"] == "new_conversation"


# ---------------------------------------------------------------------------
# Pure conversation state replay helper (for testing only)
# ---------------------------------------------------------------------------

_CONVERSATION_TERMINAL = frozenset({"passed", "failed", "aborted", "done"})


def _replay_conversation_state(
    events: list[RuntimeEvent],
    aggregate_id: str,
) -> str | None:
    """Replay events to determine current conversation state."""
    state: str = "new"
    has_pass_verification = False

    for event in events:
        if event.event_type == EventType.CONVERSATION_STARTED:
            state = "new"
        elif event.event_type == EventType.CONVERSATION_STATE_CHANGED:
            new_state = event.payload.get("to", "")
            if new_state:
                state = new_state
        elif event.event_type == EventType.RUN_PHASE_CHANGED:
            phase = event.payload.get("phase", "")
            mapped = _ORCH_PHASE_TO_CONVERSATION_STATE.get(phase)
            if mapped:
                state = mapped
        elif event.event_type == EventType.VERIFICATION_DECISION_RECORDED:
            if event.payload.get("decision") == "pass":
                has_pass_verification = True
        elif event.event_type == EventType.RUN_COMPLETED:
            if has_pass_verification:
                state = "passed"
            else:
                state = "failed"
        elif event.event_type == EventType.RUN_FAILED:
            state = "failed"
        elif event.event_type == EventType.RUN_CANCELLED:
            state = "aborted"

        # Terminal state protection
        if state in _CONVERSATION_TERMINAL:
            break  # later events in replay would be ignored anyway

    return state
