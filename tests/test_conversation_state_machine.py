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
from wlcodex.runtime_state import _ORCH_PHASE_TO_CONVERSATION_STATE
from wlcodex.conversation_state_machine import (
    classify_intent,
    route_message,
    _IMMEDIATE_REVIEW_STATES,
    _PHASE_BOUNDARY_STATES,
    MID_RUN_ACKNOWLEDGEMENT,
    build_workspace_busy_buttons,
    decode_busy_callback,
    BUSY_APPEND,
    BUSY_INTERRUPT,
    BUSY_QUEUE,
    BUSY_CANCEL,
    BUSY_NEW_SESSION,
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


# ===========================================================================
# Tests: new conversation creation via production route_message()
# ===========================================================================

def test_no_active_conversation_creates_new():
    """A chat with no active conversation creates one on normal text."""
    decision = route_message("fix the login bug", active_conversation_state=None)
    assert decision.route == "new_conversation"
    assert decision.reason == "no_active_conversation"
    assert decision.new_conversation is True


@pytest.mark.parametrize("terminal_state", ["passed", "failed", "aborted", "done"])
def test_terminal_conversation_stays_in_workbench(terminal_state):
    """Normal text after a terminal run stays in the same Workbench."""
    decision = route_message("next task please", active_conversation_state=terminal_state)
    assert decision.route == "append_active_conversation"
    assert decision.reason == f"active_conversation_terminal_{terminal_state}"
    assert decision.new_conversation is False
    assert decision.delivery_policy == "codex_immediate_review"


def test_slash_new_always_creates_new():
    """/new always creates a new conversation."""
    for state in ("new", "analysis", "implementation", "verification", "passed", None):
        decision = route_message("/new", active_conversation_state=state)
        assert decision.route == "new_conversation"
        assert decision.reason == "explicit_new_trigger"
        assert decision.new_conversation is True


@pytest.mark.parametrize("trigger_phrase", [
    "新任务", "另起一个", "重新开始", "重来", "新对话",
])
def test_plain_language_new_phrases_do_not_create_workbench(trigger_phrase):
    """Only /new creates a Workbench; natural phrases remain normal text."""
    decision = route_message(trigger_phrase, active_conversation_state="implementation")
    assert decision.route == "append_active_conversation"
    assert decision.new_conversation is False


# ===========================================================================
# Tests: append to active conversation via production route_message()
# ===========================================================================

@pytest.mark.parametrize("active_state", [
    "new", "analysis", "waiting_approval", "needs_user",
])
def test_non_terminal_followup_appends(active_state):
    """Follow-up during non-terminal states appends to active conversation."""
    decision = route_message("also, please add tests", active_conversation_state=active_state)
    assert decision.route == "append_active_conversation"
    assert decision.new_conversation is False


def test_followup_during_implementation_appends():
    """Follow-up during implementation appends (not sent directly to Claude)."""
    decision = route_message("don't forget the edge case", active_conversation_state="implementation")
    assert decision.route == "append_active_conversation"
    assert decision.new_conversation is False


def test_followup_during_verification_appends():
    """Follow-up during verification appends."""
    decision = route_message("one more thing to check", active_conversation_state="verification")
    assert decision.route == "append_active_conversation"
    assert decision.new_conversation is False


# ===========================================================================
# Tests: diagnostic commands never create work via production route_message()
# ===========================================================================

@pytest.mark.parametrize("diag_cmd", [
    "/status", "/trace", "/health", "/diff", "/files",
    "/help", "/list", "/sessions", "/model", "/permission",
])
def test_diagnostic_commands_do_not_create_conversations(diag_cmd):
    """Diagnostic/inspection commands are reads, never create work."""
    for state in ("new", "analysis", "implementation", "verification", None):
        decision = route_message(diag_cmd, active_conversation_state=state)
        assert decision.route == "diagnostic"
        assert decision.new_conversation is False, \
            f"{diag_cmd} should not create conversation, state={state}"


# ===========================================================================
# Tests: follow-up delivery policy via production route_message()
# ===========================================================================

def test_analysis_phase_followup_is_immediate_review():
    """Follow-up during analysis/planning goes to Codex immediately."""
    for state in ("analysis", "waiting_approval", "needs_user"):
        decision = route_message("clarify", active_conversation_state=state)
        assert decision.delivery_policy == "codex_immediate_review"


def test_implementation_phase_followup_is_phase_boundary():
    """Follow-up during implementation waits for phase boundary."""
    decision = route_message("also add tests", active_conversation_state="implementation")
    assert decision.delivery_policy == "codex_phase_boundary_review"


def test_verification_phase_followup_is_phase_boundary():
    """Follow-up during verification waits for phase boundary."""
    decision = route_message("check this too", active_conversation_state="verification")
    assert decision.delivery_policy == "codex_phase_boundary_review"


# ===========================================================================
# Tests: workspace busy via production route_message()
# ===========================================================================

def test_workspace_busy_returns_busy_route():
    """When workspace is busy and user triggers new work, return busy route."""
    decision = route_message(
        "/new",
        active_conversation_state="implementation",
        workspace_busy=True,
        blocking_task_id=42,
    )
    assert decision.route == "workspace_busy"


def test_workspace_busy_on_terminal():
    """Workspace busy on terminal conversation still returns busy."""
    decision = route_message(
        "new task",
        active_conversation_state="passed",
        workspace_busy=True,
        blocking_task_id=99,
    )
    assert decision.route == "workspace_busy"


# ===========================================================================
# Tests: orchestration phase -> conversation state mapping
# ===========================================================================

def test_orch_phase_maps_to_correct_conversation_state():
    """Verify the production phase-to-state mapping covers all phases."""
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["running_analysis"] == "analysis"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["running_implementation"] == "implementation"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["running_verification"] == "verification"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["completed"] == "passed"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["failed"] == "failed"
    assert _ORCH_PHASE_TO_CONVERSATION_STATE["cancelled"] == "aborted"


# ===========================================================================
# Tests: conversation state reconstruction from events
# ===========================================================================

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


@pytest.mark.parametrize("text", ["/new", "/new 测试"])
def test_production_route_message_explicit_new_trigger(text):
    """Production route_message: only /new creates a new Workbench."""
    decision = route_message(text, active_conversation_state="analysis")
    assert decision.route == "new_conversation"
    assert decision.new_conversation is True


@pytest.mark.parametrize("state", ["analysis", "waiting_approval", "needs_user"])
def test_production_route_message_non_terminal_appends(state):
    """Production route_message: non-terminal states append by default."""
    decision = route_message("clarification", active_conversation_state=state)
    assert decision.route == "append_active_conversation"
    assert decision.new_conversation is False


def test_production_route_message_terminal_stays_in_workbench():
    """Production route_message: terminal state keeps the active Workbench."""
    for state in ("passed", "failed", "aborted", "done"):
        decision = route_message("next task", active_conversation_state=state)
        assert decision.route == "append_active_conversation"
        assert decision.new_conversation is False


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
    assert classify_intent("新任务") == "normal_text"
    assert classify_intent("重新开始") == "normal_text"


def test_production_classify_intent_diagnostic():
    assert classify_intent("/status") == "diagnostic"
    assert classify_intent("/trace") == "diagnostic"
    assert classify_intent("/help") == "diagnostic"


def test_production_classify_intent_normal():
    assert classify_intent("fix the login bug") == "normal_text"
    assert classify_intent("你好") == "normal_text"


def test_production_busy_buttons():
    buttons = build_workspace_busy_buttons(42, agent_label="Codex")
    flat = [button for row in buttons for button in row]
    assert [button["text"] for button in flat] == [
        "发给当前 Codex",
        "打断并执行这句",
        "排队稍后",
        "新开隔离现场",
        "先不处理",
    ]
    assert flat[0]["callback_data"] == "busy_append:42"
    assert flat[1]["callback_data"] == "busy_interrupt:42"
    assert flat[2]["callback_data"] == "busy_queue:42"
    assert flat[3]["callback_data"] == "busy_new_session:42"
    assert flat[4]["callback_data"] == "busy_cancel:42"


def test_production_decode_busy_callback():
    assert decode_busy_callback("busy_append:42") == (BUSY_APPEND, 42)
    assert decode_busy_callback("busy_interrupt:42") == (BUSY_INTERRUPT, 42)
    assert decode_busy_callback("busy_queue:99") == (BUSY_QUEUE, 99)
    assert decode_busy_callback("busy_cancel:7") == (BUSY_CANCEL, 7)
    assert decode_busy_callback("busy_new_session:8") == (BUSY_NEW_SESSION, 8)
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
    decision1 = route_message("fix login bug", active_conversation_state=None)
    assert decision1.route == "new_conversation"

    # Second message: active conversation in analysis -> append
    decision2 = route_message("also handle null pointer", active_conversation_state="analysis")
    assert decision2.route == "append_active_conversation"
    assert decision2.new_conversation is False


def test_followup_after_passed_stays_in_workbench():
    """After a run passes, follow-up starts more work inside the Workbench."""
    decision = route_message("next thing to fix", active_conversation_state="passed")
    assert decision.route == "append_active_conversation"
    assert decision.new_conversation is False


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
