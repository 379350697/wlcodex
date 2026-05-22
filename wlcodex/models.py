from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# Re-export runtime event types so all lanes can import from one place.
from wlcodex.runtime_events import (  # noqa: F401  — re-exported for convenience
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    WAITING_SLOT = "waiting_slot"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"
    ARCHIVED = "archived"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ApprovalKind(StrEnum):
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    PERMISSIONS = "permissions"


class BackendRequestStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Task:
    id: int
    workspace_alias: str
    workspace_path: str
    title: str
    status: TaskStatus
    codex_thread_id: str | None
    active_turn_id: str | None
    parent_task_id: int | None
    telegram_chat_id: int | None
    telegram_status_message_id: int | None
    created_at: datetime
    updated_at: datetime
    last_summary: str
    last_phase: str
    last_error: str
    changed_file_count: int = 0
    pending_approval_count: int = 0
    token_input: int = 0
    token_output: int = 0
    worktree_path: str = ""
    worktree_branch: str = ""
    is_force_parallel: bool = False


@dataclass(frozen=True)
class TaskEvent:
    id: int
    task_id: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class ApprovalRequest:
    id: int
    task_id: int
    codex_request_id: str
    codex_item_id: str | None
    codex_turn_id: str | None
    kind: ApprovalKind
    summary: str
    command_json: str
    status: ApprovalStatus
    telegram_message_id: int | None
    resolution: str | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True)
class BackendRequest:
    id: int
    jsonrpc_id: int
    method: str
    task_id: int | None
    status: BackendRequestStatus
    created_at: datetime
    completed_at: datetime | None
    error: str | None


@dataclass(frozen=True)
class TouchedFile:
    id: int
    task_id: int
    path: str
    change_kind: str
    created_at: datetime


@dataclass(frozen=True)
class TelegramUpdate:
    id: int
    telegram_update_id: int
    user_id: int
    chat_id: int
    update_type: str
    allowed: bool
    created_at: datetime


@dataclass(frozen=True)
class TaskSnapshot:
    """Read-only render snapshot for status cards."""
    task: Task
    events: list[TaskEvent] = field(default_factory=list)
    approvals: list[ApprovalRequest] = field(default_factory=list)
    touched_files: list[TouchedFile] = field(default_factory=list)


# --- Conversation models ---


class ConversationMode(StrEnum):
    CHIEF_ENGINEER = "chief_engineer"
    CODEX_DIRECT = "codex_direct"
    CLAUDE_DIRECT = "claude_direct"


class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


class OrchestrationStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_USER = "needs_user"
    ABORTED = "aborted"


@dataclass(frozen=True)
class ConversationSession:
    id: int
    chat_id: int
    user_id: int
    title: str
    mode: str
    workspace_alias: str
    active_codex_task_id: int | None
    active_claude_run_id: int | None
    conversation_summary: str
    current_model: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    codex_thread_id: str = ""
    claude_session_id: str = ""


@dataclass(frozen=True)
class AgentRun:
    id: int
    conversation_id: int
    agent: str
    role: str
    status: str
    hidden_task_id: int | None
    external_session_id: str | None
    prompt_packet_summary: str
    completion_summary: str
    token_input: int
    token_output: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OrchestrationRun:
    id: int
    conversation_id: int
    goal: str
    status: str
    current_step: str
    verify_round: int
    max_verify_rounds: int
    last_codex_analysis: str
    last_claude_summary: str
    last_verification_result: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OrchestrationDecision:
    id: int
    run_id: int
    decision: str
    reason: str
    next_agent: str
    created_at: datetime


@dataclass(frozen=True)
class UsageEvent:
    """Append-only usage ledger entry for a single model request or workflow event."""
    id: int
    created_at: datetime
    conversation_id: int | None
    orchestration_run_id: int | None
    agent_run_id: int | None
    task_id: int | None
    agent: str          # codex / claude / workflow
    role: str           # direct / analysis / implementation / verification
    phase: str
    request_kind: str
    request_index: int
    model: str
    external_thread_id: str | None
    external_turn_id: str | None
    external_session_id: str | None
    status: str
    source: str         # exact / estimated / derived
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    workflow_overhead_input_tokens: int
    workflow_overhead_output_tokens: int
    latency_ms: int
    metadata_json: str
