"""Shared agent result and agent backend protocol.

All agent backends (Codex, Claude) implement this interface so the
conversation controller and orchestrator can treat them uniformly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class AgentRequest:
    prompt: str
    workspace_path: str = ""
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    text: str = ""
    exit_code: int = 0
    token_input: int = 0
    token_output: int = 0
    session_id: str = ""


@dataclass
class AgentStreamEvent:
    delta: str = ""
    event_type: str = "text"


class AgentBackend(Protocol):
    """Protocol for agent backends. Not used at runtime; serves as documentation."""

    async def send(self, request: AgentRequest) -> AgentResult:
        ...

    async def send_streaming(self, request: AgentRequest) -> AsyncIterator[AgentStreamEvent]:
        ...

    def interrupt(self, session_id: str | None = None) -> None:
        ...

    def health(self) -> object:
        ...
