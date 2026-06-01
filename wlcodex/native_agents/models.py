from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NativeAgentStatus:
    provider: str
    provider_engine: str
    enabled: bool
    connected: bool
    status_code: str
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_engine": self.provider_engine,
            "enabled": self.enabled,
            "connected": self.connected,
            "status_code": self.status_code,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class NativeAgentCapabilities:
    can_list_sessions: bool = False
    can_list_models: bool = False
    can_start_session: bool = False
    can_resume_session: bool = False
    can_read_history: bool = False
    can_stream_events: bool = False
    can_continue_session: bool = False
    can_steer_active_turn: bool = False
    can_interrupt: bool = False
    can_resolve_approval: bool = False
    can_apply_file_edits: bool = False
    can_run_shell_commands: bool = False
    disabled_reasons: dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "can_list_sessions": self.can_list_sessions,
            "can_list_models": self.can_list_models,
            "can_start_session": self.can_start_session,
            "can_resume_session": self.can_resume_session,
            "can_read_history": self.can_read_history,
            "can_stream_events": self.can_stream_events,
            "can_continue_session": self.can_continue_session,
            "can_steer_active_turn": self.can_steer_active_turn,
            "can_interrupt": self.can_interrupt,
            "can_resolve_approval": self.can_resolve_approval,
            "can_apply_file_edits": self.can_apply_file_edits,
            "can_run_shell_commands": self.can_run_shell_commands,
            "disabled_reasons": self.disabled_reasons,
        }


@dataclass(frozen=True)
class NativeAgentSession:
    id: int
    provider: str
    provider_engine: str
    native_session_id: str
    agent_run_id: int
    conversation_id: int
    title: str
    cwd: str
    source_kind: str
    status: str
    last_turn_id: str
    activity_at: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "provider_engine": self.provider_engine,
            "native_session_id": self.native_session_id,
            "native_thread_id": self.native_session_id,
            "agent_run_id": self.agent_run_id,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "cwd": self.cwd,
            "source_kind": self.source_kind,
            "status": self.status,
            "last_turn_id": self.last_turn_id,
            "activity_at": self.activity_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class NativeAgentControlResult:
    provider: str
    provider_engine: str
    native_session_id: str
    agent_run_id: int
    turn_id: str = ""
    active_turn_id: str = ""
    turn_running: bool = False
    status: str = "ok"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_engine": self.provider_engine,
            "native_session_id": self.native_session_id,
            "native_thread_id": self.native_session_id,
            "agent_run_id": self.agent_run_id,
            "turn_id": self.turn_id,
            "active_turn_id": self.active_turn_id,
            "turn_running": self.turn_running,
            "status": self.status,
        }
