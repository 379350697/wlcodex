"""Runtime event envelope, type constants, enums, and redaction.

Defines the single immutable RuntimeEvent contract shared by all lanes.
Other lanes import from here; they never need raw SQL for runtime events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Payload safety
# ---------------------------------------------------------------------------

MAX_PAYLOAD_STRING_LENGTH = 10_000

REDACTED_PLACEHOLDER = "[REDACTED]"
CONTENT_REDACTED_PLACEHOLDER = "<redacted>"

# Tokens/metrics that are counts — never redacted.
_TOKEN_METRIC_SEGMENTS = frozenset({
    "input", "output", "total", "cached", "reasoning", "workflow", "overhead",
})

# Modifiers that make "key"/"keys" a sensitive match.
_KEY_SENSITIVE_MODIFIERS = frozenset({
    "api", "sign", "signing", "auth", "secret", "private", "master", "encrypt",
})

_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b("
    r"(?:[A-Za-z0-9_]*_)?"
    r"(?:password|passwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization|auth)"
    r"(?:_[A-Za-z0-9_]*)?"
    r")\s*([=:])\s*([\"']?)([^\s\"'`;,]+)",
    re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(
    r"\b(Bearer)\s+([A-Za-z0-9._~+/\-]+=*)",
    re.IGNORECASE,
)
_SK_STYLE_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{6,}\b")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AggregateType(StrEnum):
    CONVERSATION = "conversation"
    ORCHESTRATION_RUN = "orchestration_run"
    AGENT_RUN = "agent_run"
    APPROVAL = "approval"
    TELEGRAM_MESSAGE = "telegram_message"
    SURFACE_SESSION = "surface_session"
    SYSTEM = "system"


class Visibility(StrEnum):
    INTERNAL = "internal"
    OPERATOR = "operator"
    USER = "user"


class EventSource(StrEnum):
    TELEGRAM = "telegram"
    CONTROLLER = "controller"
    ORCHESTRATOR = "orchestrator"
    CODEX = "codex"
    CLAUDE = "claude"
    ANTIGRAVITY = "antigravity"
    PROJECTOR = "projector"
    WATCHDOG = "watchdog"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# Event type constants (dot-separated, shared across all lanes)
# ---------------------------------------------------------------------------

class EventType:
    """Dot-separated event type constants used by all lanes."""

    # User / Telegram
    USER_MESSAGE_RECEIVED = "user.message.received"
    TELEGRAM_MESSAGE_SENT = "telegram.message.sent"
    TELEGRAM_MESSAGE_EDITED = "telegram.message.edited"
    TELEGRAM_MESSAGE_FAILED = "telegram.message.failed"
    TELEGRAM_CALLBACK_RECEIVED = "telegram.callback.received"

    # Run lifecycle
    RUN_REQUESTED = "run.requested"
    RUN_STARTED = "run.started"
    RUN_PHASE_CHANGED = "run.phase.changed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCEL_REQUESTED = "run.cancel.requested"
    RUN_CANCELLED = "run.cancelled"

    # Agent lifecycle
    AGENT_RUN_QUEUED = "agent.run.queued"
    AGENT_RUN_STARTED = "agent.run.started"
    AGENT_RUN_ACTIVITY = "agent.run.activity"
    AGENT_RUN_HEARTBEAT = "agent.run.heartbeat"
    AGENT_RUN_WAITING_FOR_APPROVAL = "agent.run.waiting_for_approval"
    AGENT_RUN_COMPLETED = "agent.run.completed"
    AGENT_RUN_FAILED = "agent.run.failed"
    AGENT_RUN_TIMED_OUT = "agent.run.timed_out"
    AGENT_RUN_ORPHANED = "agent.run.orphaned"

    # Model / message output
    MODEL_TEXT_DELTA = "model.text.delta"
    MODEL_MESSAGE_COMPLETED = "model.message.completed"
    MODEL_REASONING_DELTA = "model.reasoning.delta"
    MODEL_USAGE_UPDATED = "model.usage.updated"
    MODEL_API_RETRY = "model.api.retry"

    # Tool / command / file / approval
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_PROGRESS = "tool.call.progress"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    TOOL_CALL_FAILED = "tool.call.failed"
    COMMAND_STARTED = "command.started"
    COMMAND_OUTPUT_DELTA = "command.output.delta"
    COMMAND_COMPLETED = "command.completed"
    COMMAND_FAILED = "command.failed"
    FILE_READ = "file.read"
    FILE_CHANGED = "file.changed"
    DIFF_UPDATED = "diff.updated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    APPROVAL_EXPIRED = "approval.expired"

    # Verification
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_DECISION_RECORDED = "verification.decision.recorded"
    VERIFICATION_COMPLETED = "verification.completed"
    VERIFICATION_RETRY_REQUESTED = "verification.retry.requested"

    # Timeout / recovery
    WATCHDOG_IDLE_TIMEOUT = "watchdog.idle_timeout"
    WATCHDOG_HARD_TIMEOUT = "watchdog.hard_timeout"
    SYSTEM_STARTED = "system.started"
    SYSTEM_RECOVERY_STARTED = "system.recovery.started"
    SYSTEM_RECOVERY_COMPLETED = "system.recovery.completed"
    PROJECTION_REBUILT = "projection.rebuilt"
    PROJECTION_FAILED = "projection.failed"
    RUNTIME_CAPABILITY_MISSING = "runtime.capability.missing"

    # Security / delivery isolation
    SECURITY_DELIVERY_BLOCKED = "security.delivery.blocked"
    SECURITY_TOKEN_ACCESS_ATTEMPTED = "security.token.access.attempted"

    # Conversation routing
    CONVERSATION_STARTED = "conversation.started"
    CONVERSATION_ACTIVATED = "conversation.activated"
    CONVERSATION_STATE_CHANGED = "conversation.state.changed"
    CONVERSATION_CLOSED = "conversation.closed"
    CONVERSATION_INTENT_CLASSIFIED = "conversation.intent.classified"
    CONVERSATION_MESSAGE_ROUTED = "conversation.message.routed"
    USER_CONTEXT_APPENDED = "user.context.appended"
    CONVERSATION_PENDING_CONTEXT_RECORDED = "conversation.pending_context.recorded"
    CONVERSATION_PENDING_CONTEXT_REVIEWED = "conversation.pending_context.reviewed"

    # Dual-surface mode switching and cursor tracking
    CONVERSATION_MODE_SWITCHED = "conversation.mode.switched"
    SURFACE_CURSOR_ADVANCED = "surface.cursor.advanced"
    TERMINAL_SESSION_ATTACHED = "terminal.session.attached"
    TERMINAL_SESSION_DETACHED = "terminal.session.detached"
    TERMINAL_SESSION_INPUT_SENT = "terminal.session.input.sent"
    TERMINAL_SESSION_OUTPUT_FRAME = "terminal.session.output.frame"
    TERMINAL_SESSION_ABORTED = "terminal.session.aborted"
    PRODUCT_DISPLAY_FRAME = "product.display.frame"
    PRODUCT_PENDING_CONTEXT_RECORDED = "product.pending_context.recorded"

    # Workbench execution mode
    WORKBENCH_EXECUTION_MODE_SELECTED = "workbench.execution_mode.selected"

    # Adaptive engineering team
    TEAM_RUN_REQUESTED = "team.run.requested"
    TEAM_RUN_ROUTED = "team.run.routed"
    TEAM_RUN_STARTED = "team.run.started"
    TEAM_RUN_COMPLETED = "team.run.completed"
    TEAM_RUN_FAILED = "team.run.failed"
    TEAM_AGENT_JOB_QUEUED = "team.agent_job.queued"
    TEAM_AGENT_JOB_STARTED = "team.agent_job.started"
    TEAM_AGENT_JOB_COMPLETED = "team.agent_job.completed"
    TEAM_AGENT_JOB_FAILED = "team.agent_job.failed"
    TEAM_CONTEXT_PACKET_RECORDED = "team.context_packet.recorded"
    TEAM_ARTIFACT_RECORDED = "team.artifact.recorded"
    TEAM_GATE_PASSED = "team.gate.passed"
    TEAM_GATE_FAILED = "team.gate.failed"
    TEAM_ASSIGNMENT_SELECTED = "team.assignment.selected"
    TEAM_ASSIGNMENT_FALLBACK_USED = "team.assignment.fallback_used"
    TEAM_SKILL_ACTIVATED = "team.skill_activated"
    TEAM_CAPABILITY_BUDGET_APPLIED = "team.capability_budget.applied"
    TEAM_OBSERVATION_RECORDED = "team.observation.recorded"
    TEAM_INSTINCT_PROPOSED = "team.instinct.proposed"
    TEAM_INSTINCT_PROMOTED = "team.instinct.promoted"
    TEAM_INSTINCT_DEPRECATED = "team.instinct.deprecated"
    TEAM_INSTINCT_SELECTED = "team.instinct.selected"

    # Onsite session lifecycle (workbench-level)
    ONSITE_SESSION_ORPHANED = "onsite.session.orphaned"

    # Workspace busy
    WORKSPACE_BUSY_DETECTED = "workspace.busy.detected"
    WORKSPACE_BUSY_USER_CHOICE_REQUESTED = "workspace.busy.user_choice.requested"
    WORKSPACE_BUSY_USER_CHOICE_RECORDED = "workspace.busy.user_choice.recorded"
    RUN_QUEUED = "run.queued"
    RUN_QUEUED_CONSUMED = "run.queued.consumed"

    # Approval supersession
    APPROVAL_SUPERSEDED = "approval.superseded"
    APPROVAL_STALE_BUTTON_IGNORED = "approval.stale_button.ignored"

    # Telegram delivery outbox
    TELEGRAM_DELIVERY_ENQUEUED = "telegram.delivery.enqueued"
    TELEGRAM_DELIVERY_STARTED = "telegram.delivery.started"
    TELEGRAM_EDIT_SKIPPED_NO_CHANGE = "telegram.edit.skipped_no_change"
    TELEGRAM_OUTBOX_RETRY_SCHEDULED = "telegram.outbox.retry_scheduled"
    TELEGRAM_OUTBOX_GAVE_UP = "telegram.outbox.gave_up"
    TELEGRAM_CALLBACK_ANSWER_FAILED = "telegram.callback.answer.failed"
    TELEGRAM_CALLBACK_EDIT_FAILED = "telegram.callback.edit.failed"

    # Telegram polling resilience
    TELEGRAM_POLLER_BOOTSTRAP_STARTED = "telegram.poller.bootstrap.started"
    TELEGRAM_POLLER_BOOTSTRAP_SUCCEEDED = "telegram.poller.bootstrap.succeeded"
    TELEGRAM_POLLER_BOOTSTRAP_FAILED = "telegram.poller.bootstrap.failed"
    TELEGRAM_POLLER_BOOTSTRAP_RETRYING = "telegram.poller.bootstrap.retrying"
    TELEGRAM_POLLER_ERROR = "telegram.poller.error"
    TELEGRAM_POLLER_RECOVERED = "telegram.poller.recovered"
    TELEGRAM_POLLER_WATCHDOG_TIMEOUT = "telegram.poller.watchdog_timeout"

    # Workbench view / execution mode
    WORKBENCH_CREATED = "workbench.created"
    WORKBENCH_VIEW_CHANGED = "workbench.view.changed"
    WORKBENCH_EXECUTION_MODE_SELECTED = "workbench.execution_mode.selected"
    WORKBENCH_ROUTE_DECIDED = "workbench.route.decided"

    # Onsite session lifecycle (workbench flavour)
    ONSITE_SESSION_STARTED = "onsite.session.started"
    ONSITE_SESSION_ATTACHED = "onsite.session.attached"
    ONSITE_SESSION_DETACHED = "onsite.session.detached"
    ONSITE_SESSION_ORPHANED = "onsite.session.orphaned"
    ONSITE_INPUT_SENT = "onsite.input.sent"
    ONSITE_OUTPUT_FRAME = "onsite.output.frame"
    ONSITE_CURSOR_ADVANCED = "onsite.cursor.advanced"

    # Cockpit
    COCKPIT_CURSOR_ADVANCED = "cockpit.cursor.advanced"
    COCKPIT_SUMMARY_RENDERED = "cockpit.summary.rendered"


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeEvent:
    """Immutable runtime event envelope — the single source of truth.

    Every meaningful runtime fact is recorded as a RuntimeEvent.
    The ``id`` is 0 before append and set by SQLite afterwards.
    """

    schema_version: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    correlation_id: str
    source: str
    actor: str
    visibility: str
    payload: dict[str, Any]
    occurred_at: str
    conversation_id: int | None = None
    orchestration_run_id: int | None = None
    agent_run_id: int | None = None
    task_id: int | None = None
    causation_id: int | None = None
    id: int = 0

    def with_id(self, event_id: int) -> "RuntimeEvent":
        """Return a copy with *id* set (after SQLite append)."""
        return replace(self, id=event_id)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _split_key(key: str) -> set[str]:
    """Split *key* on common separators into lowercase segments."""
    return set(key.lower().replace("-", "_").split("_"))


def _is_sensitive_key(key: str) -> bool:
    """Return True when *key* names a value that should be redacted.

    Uses segment-based matching (split on ``_`` and ``-``) so that
    ``input_tokens`` (a count) is not redacted while ``auth_token`` or
    standalone ``token`` is.
    """
    segments = _split_key(key)

    # Unambiguous — always redact.
    if segments & {"secret", "secrets", "password", "passwords",
                   "credential", "credentials"}:
        return True

    # "auth" as a segment, or as a substring (handles "Authorization", etc.).
    if "auth" in segments or any("auth" in seg for seg in segments):
        return True

    # "token"/"tokens" is sensitive unless it is a known metric (input_tokens, etc.).
    if ("token" in segments or "tokens" in segments) and not (
        segments & _TOKEN_METRIC_SEGMENTS
    ):
        return True

    # "key"/"keys" combined with api/sign/auth/secret/private modifiers.
    if ("key" in segments or "keys" in segments) and (
        segments & _KEY_SENSITIVE_MODIFIERS
    ):
        return True

    return False


def redact_text_content(value: str) -> str:
    """Redact secret-like content embedded in a free-form string."""
    value = _SECRET_ASSIGNMENT_RE.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}"
            f"{m.group(3)}{CONTENT_REDACTED_PLACEHOLDER}"
        ),
        value,
    )
    value = _BEARER_TOKEN_RE.sub(
        rf"\1 {CONTENT_REDACTED_PLACEHOLDER}",
        value,
    )
    return _SK_STYLE_SECRET_RE.sub(CONTENT_REDACTED_PLACEHOLDER, value)


def safe_text_preview(text: str, *, max_len: int = 200) -> str:
    """Return a short, content-redacted preview for runtime event payloads."""
    return redact_text_content(text[: max_len * 2])[:max_len]


def redact_payload(payload: dict[str, Any], *, max_str_len: int = MAX_PAYLOAD_STRING_LENGTH) -> dict[str, Any]:
    """Return a deep-copied payload with sensitive values redacted and
    string lengths capped.

    Keys matching sensitive names are replaced with ``[REDACTED]``.
    Free-form string values also receive content-level redaction so fields
    like ``text`` and ``text_preview`` cannot persist embedded passwords or
    API keys by accident.

    String values longer than *max_str_len* are truncated and suffixed with
    ``...<truncated>``.
    """
    result: dict[str, Any] = {}
    for k, v in payload.items():
        if _is_sensitive_key(k):
            result[k] = REDACTED_PLACEHOLDER
        elif isinstance(v, dict):
            result[k] = redact_payload(v, max_str_len=max_str_len)
        elif isinstance(v, list):
            result[k] = [
                redact_payload(item, max_str_len=max_str_len) if isinstance(item, dict)
                else _cap_string(redact_text_content(item), max_str_len) if isinstance(item, str)
                else item
                for item in v
            ]
        elif isinstance(v, str):
            result[k] = _cap_string(redact_text_content(v), max_str_len)
        else:
            result[k] = v
    return result


def _cap_string(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "...<truncated>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
