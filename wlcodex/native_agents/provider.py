from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)


class NativeAgentProvider(Protocol):
    provider: str
    provider_engine: str

    async def status(self) -> NativeAgentStatus: ...

    def capabilities(self) -> NativeAgentCapabilities: ...

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]: ...

    async def list_cached_sessions(
        self,
        limit: int = 50,
    ) -> list[NativeAgentSession]: ...

    async def list_models(self) -> list[dict[str, Any]]: ...

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult: ...

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult: ...

    async def read_session(self, native_session_id: str) -> dict[str, Any]: ...

    async def peek_session(self, native_session_id: str) -> dict[str, Any]: ...

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult: ...

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult: ...

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult: ...

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult: ...

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult: ...

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]: ...


class NativeAgentRegistry:
    _CLAUDE_ENGINE_PROVIDER_NAMES = {"claude-cli", "claude-deepseek"}

    def __init__(self, providers: Iterable[NativeAgentProvider]) -> None:
        self._providers: dict[str, NativeAgentProvider] = {}
        for provider in providers:
            self._register(provider)

    def _register(self, provider: NativeAgentProvider) -> None:
        provider_name = provider.provider.strip()
        if not provider_name:
            raise ValueError("native provider name cannot be empty")
        if provider_name in self._CLAUDE_ENGINE_PROVIDER_NAMES:
            raise ValueError("Claude engines must not be providers")
        if provider_name in self._providers:
            raise ValueError(f"duplicate native provider: {provider_name}")
        self._providers[provider_name] = provider

    def get(self, provider: str) -> NativeAgentProvider:
        provider_name = provider.strip()
        try:
            return self._providers[provider_name]
        except KeyError:
            raise KeyError(f"unknown native provider: {provider_name}") from None

    def maybe_get(self, provider: str) -> NativeAgentProvider | None:
        return self._providers.get(provider.strip())

    def list_provider_summaries(self) -> list[dict[str, str]]:
        return [
            {
                "provider": provider.provider.strip(),
                "provider_engine": provider.provider_engine,
            }
            for provider in self._providers.values()
        ]
