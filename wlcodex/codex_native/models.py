from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativeCodexStatus:
    enabled: bool
    connected: bool
    remote_control_status: str
    server_name: str = ""
    installation_id: str = ""
    environment_id: str | None = None
    error: str = ""


@dataclass(frozen=True)
class NativeCodexSession:
    id: int
    native_thread_id: str
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

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "native_thread_id": self.native_thread_id,
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
        }


@dataclass(frozen=True)
class NativeCodexControlResult:
    native_thread_id: str
    agent_run_id: int
    turn_id: str = ""
    active_turn_id: str = ""
    turn_running: bool = False
    status: str = "ok"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
            "turn_id": self.turn_id,
            "active_turn_id": self.active_turn_id,
            "turn_running": self.turn_running,
            "status": self.status,
        }


class NativeCodexError(RuntimeError):
    pass
