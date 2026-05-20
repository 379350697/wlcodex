"""Event-sourced conversation state machine for Telegram message routing.

Pure decision engine: takes conversation state and produces routing decisions.
All side effects (event appends, Telegram sends) happen in the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wlcodex.runtime_events import EventType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONVERSATION_TERMINAL_STATES = frozenset({"passed", "failed", "aborted", "done"})

_DIAGNOSTIC_COMMANDS = frozenset({
    "/status", "/trace", "/health", "/diff", "/files",
    "/tail", "/events", "/list", "/help", "/start",
    "/model", "/sessions", "/stop", "/switch",
    "/codex", "/claude", "/auto", "/verify",
    "/pause", "/abort", "/archive", "/fork",
    "/continue", "/steer", "/task", "/show",
    "/permission",
})

# States where follow-ups trigger immediate Codex re-evaluation.
_IMMEDIATE_REVIEW_STATES = frozenset({
    "new", "analysis", "waiting_approval", "needs_user",
})

# States where follow-ups are held for phase boundary review.
_PHASE_BOUNDARY_STATES = frozenset({
    "implementation", "verification",
})

# The acknowledgement message sent when follow-up arrives during impl/verify.
MID_RUN_ACKNOWLEDGEMENT = (
    "已记录，当前阶段结束后由 Codex 判断是否中断/重跑。"
)


# ---------------------------------------------------------------------------
# Route decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteDecision:
    """Immutable routing decision for an inbound message."""

    route: str
    reason: str
    new_conversation: bool
    intent: str  # new_trigger, diagnostic, normal_text
    conversation_state: str | None = None
    delivery_policy: str | None = None
    user_acknowledgement: str | None = None
    requires_approval_supersession: bool = False

    # Event payloads the caller must append.
    expected_events: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


def classify_intent(text: str) -> str:
    """Classify user message intent from raw text.

    Returns one of: ``new_trigger``, ``diagnostic``, ``normal_text``.
    """
    normalized = text.strip()

    # Explicit new-Workbench trigger. Natural-language phrases like "新任务"
    # are normal text inside the current Workbench; only /new starts a new one.
    if normalized.startswith("/new"):
        return "new_trigger"

    # Diagnostic / inspection commands.
    cmd = normalized.split()[0] if normalized else ""
    if cmd in _DIAGNOSTIC_COMMANDS:
        return "diagnostic"

    return "normal_text"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def route_message(
    text: str,
    *,
    active_conversation_id: int | None = None,
    active_conversation_state: str | None = None,
    chat_id: int = 0,
    workspace_busy: bool = False,
    blocking_task_id: int | None = None,
    blocking_run_id: int | None = None,
    telegram_message_id: int = 0,
) -> RouteDecision:
    """Determine the route for an inbound Telegram message.

    Pure function: no database access, no side effects.

    Returns a ``RouteDecision`` that the caller uses to emit events and take action.
    """
    intent = classify_intent(text)
    is_terminal = active_conversation_state in _CONVERSATION_TERMINAL_STATES
    no_active = active_conversation_state is None

    # --- Diagnostic commands: read-only, never create work ---
    if intent == "diagnostic":
        return RouteDecision(
            route="diagnostic",
            reason="inspection_command",
            new_conversation=False,
            intent=intent,
        )

    # --- Explicit new-conversation triggers ---
    if intent == "new_trigger":
        if workspace_busy:
            return _busy_decision(
                active_conversation_state, blocking_task_id, blocking_run_id
            )
        return RouteDecision(
            route="new_conversation",
            reason="explicit_new_trigger",
            new_conversation=True,
            intent=intent,
        )

    # --- No active conversation -> create new ---
    if no_active:
        if workspace_busy:
            return _busy_decision(None, blocking_task_id, blocking_run_id)
        return RouteDecision(
            route="new_conversation",
            reason="no_active_conversation",
            new_conversation=True,
            intent=intent,
        )

    # --- Terminal active conversation -> keep the Workbench ---
    if is_terminal:
        if workspace_busy:
            return _busy_decision(
                active_conversation_state, blocking_task_id, blocking_run_id
            )
        return RouteDecision(
            route="append_active_conversation",
            reason=f"active_conversation_terminal_{active_conversation_state}",
            new_conversation=False,
            intent=intent,
            conversation_state=active_conversation_state,
            delivery_policy="codex_immediate_review",
        )

    # --- Active non-terminal -> append ---
    delivery_policy = _delivery_policy(active_conversation_state)
    needs_supersession = active_conversation_state == "waiting_approval"
    user_ack = MID_RUN_ACKNOWLEDGEMENT if active_conversation_state in _PHASE_BOUNDARY_STATES else None

    return RouteDecision(
        route="append_active_conversation",
        reason="active_conversation_non_terminal",
        new_conversation=False,
        intent=intent,
        conversation_state=active_conversation_state,
        delivery_policy=delivery_policy,
        user_acknowledgement=user_ack,
        requires_approval_supersession=needs_supersession,
    )


def _delivery_policy(conversation_state: str | None) -> str:
    if conversation_state in _IMMEDIATE_REVIEW_STATES:
        return "codex_immediate_review"
    if conversation_state in _PHASE_BOUNDARY_STATES:
        return "codex_phase_boundary_review"
    return "codex_immediate_review"


def _busy_decision(
    conversation_state: str | None,
    blocking_task_id: int | None,
    blocking_run_id: int | None,
) -> RouteDecision:
    return RouteDecision(
        route="workspace_busy",
        reason="workspace_locked",
        new_conversation=False,
        intent="new_trigger",
        conversation_state=conversation_state,
        expected_events=[{
            "event_type": EventType.WORKSPACE_BUSY_DETECTED,
            "payload": {
                "blocking_task_id": blocking_task_id,
                "blocking_run_id": blocking_run_id,
                "conversation_state": conversation_state,
            },
        }],
    )


# ---------------------------------------------------------------------------
# Workspace busy button callbacks
# ---------------------------------------------------------------------------

# Inline callback data prefixes for workspace busy choices.
BUSY_APPEND = "busy_append"
BUSY_QUEUE = "busy_queue"
BUSY_CANCEL = "busy_cancel"


def build_workspace_busy_buttons(
    conversation_id: int,
) -> list[list[dict[str, str]]]:
    """Build inline keyboard buttons for workspace busy user choice."""
    return [[
        {
            "text": "追加到当前执行",
            "callback_data": f"{BUSY_APPEND}:{conversation_id}",
        },
        {
            "text": "等当前执行结束",
            "callback_data": f"{BUSY_QUEUE}:{conversation_id}",
        },
        {
            "text": "取消",
            "callback_data": f"{BUSY_CANCEL}:{conversation_id}",
        },
    ]]


def decode_busy_callback(data: str) -> tuple[str, int] | None:
    """Parse a workspace-busy callback data string.

    Returns (action, conversation_id) or None.
    """
    for prefix in (BUSY_APPEND, BUSY_QUEUE, BUSY_CANCEL):
        if data.startswith(f"{prefix}:"):
            try:
                return (prefix, int(data[len(prefix) + 1:]))
            except ValueError:
                return None
    return None
