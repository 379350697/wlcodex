from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore


@dataclass(frozen=True)
class ClaudeSdkDeepSeekConfig:
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com/anthropic"
    model: str = "deepseek-v4-pro"


@dataclass(frozen=True)
class _RunOutcome:
    status: str
    error: str = ""


class ClaudeAgentSdkRunner:
    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
    ):
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as exc:
            raise RuntimeError("claude-agent-sdk is not installed") from exc

        api_key = os.environ.get(config.api_key_env, "")
        old_api_key = os.environ.get("ANTHROPIC_API_KEY")
        old_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        old_base_url = os.environ.get("ANTHROPIC_BASE_URL")
        os.environ["ANTHROPIC_API_KEY"] = api_key
        os.environ["ANTHROPIC_AUTH_TOKEN"] = api_key
        os.environ["ANTHROPIC_BASE_URL"] = config.base_url
        try:
            options = ClaudeAgentOptions(cwd=cwd, model=config.model)
            async for message in query(prompt=prompt, options=options):
                yield message
        finally:
            _restore_env("ANTHROPIC_API_KEY", old_api_key)
            _restore_env("ANTHROPIC_AUTH_TOKEN", old_auth_token)
            _restore_env("ANTHROPIC_BASE_URL", old_base_url)


class ClaudeSdkDeepSeekProvider:
    provider = "claude"
    provider_engine = "sdk-deepseek"

    def __init__(
        self,
        *,
        config: ClaudeSdkDeepSeekConfig,
        session_store: NativeAgentSessionStore,
        runner: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._session_store = session_store
        self._runner = runner or ClaudeAgentSdkRunner()
        self._env = env if env is not None else os.environ

    async def status(self) -> NativeAgentStatus:
        if not self._api_key():
            return NativeAgentStatus(
                provider=self.provider,
                provider_engine=self.provider_engine,
                enabled=True,
                connected=False,
                status_code="missing_api_key",
                message=f"{self._config.api_key_env} is not set.",
                metadata=self._metadata(self._config),
            )
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
            metadata=self._metadata(self._config),
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_list_models=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": (
                    "Claude Agent SDK sessions continue by prompt, not same-turn steering."
                ),
                "can_interrupt": (
                    "SDK cancellation is not exposed through the first native-agent slice."
                ),
                "can_resolve_approval": (
                    "SDK human-in-loop approval mapping is not enabled in this slice."
                ),
            },
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        return self._session_store.list_recent(
            provider=self.provider,
            provider_engine=self.provider_engine,
            limit=limit,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        return [{"id": self._config.model}]

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._ensure_api_key()
        run_config = self._config_for_kwargs(kwargs)
        native_session_id = f"claude-sdk-{uuid4()}"
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=prompt.strip()[:80] or "Claude DeepSeek SDK session",
            cwd=cwd,
            source_kind="claude_sdk_deepseek",
            status="running",
            metadata=self._metadata(run_config),
        )
        outcome = await self._run_prompt(
            prompt=prompt,
            cwd=session.cwd,
            session_id=session.native_session_id,
            config=run_config,
        )
        session = self._update_after_run(session, outcome, run_config)
        return _control_result(
            session,
            status="started" if outcome.status == "done" else outcome.status,
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        self._ensure_api_key()
        run_config = self._config_for_kwargs(kwargs)
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"claude-sdk-{uuid4()}",
            title="Claude DeepSeek SDK session",
            cwd=cwd,
            source_kind="claude_sdk_deepseek",
            status="created",
            metadata=self._metadata(run_config),
        )
        return _control_result(session, status="created")

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        session = self._lookup_session(native_session_id)
        return {"thread": session.to_json_dict(), "turns": []}

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._lookup_session(native_session_id)
        return _control_result(session, status="attached")

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        return await self.attach_session(native_session_id)

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._ensure_api_key()
        session = self._lookup_session(native_session_id)
        run_config = self._config_for_kwargs(kwargs)
        outcome = await self._run_prompt(
            prompt=prompt,
            cwd=session.cwd,
            session_id=session.native_session_id,
            config=run_config,
        )
        session = self._update_after_run(session, outcome, run_config)
        return _control_result(
            session,
            status="continued" if outcome.status == "done" else outcome.status,
        )

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        raise NotImplementedError(
            "Claude DeepSeek SDK provider does not support active-turn steering"
        )

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult:
        raise NotImplementedError("Claude DeepSeek SDK provider does not support interrupt yet")

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        raise KeyError(request_id)

    async def _run_prompt(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
    ) -> _RunOutcome:
        try:
            async for _event in self._runner.run(
                prompt=prompt,
                cwd=cwd,
                session_id=session_id,
                config=config,
            ):
                pass
        except Exception as exc:
            return _RunOutcome(status="failed", error=str(exc))
        return _RunOutcome(status="done")

    def _update_after_run(
        self,
        session: NativeAgentSession,
        outcome: _RunOutcome,
        config: ClaudeSdkDeepSeekConfig,
    ) -> NativeAgentSession:
        metadata = dict(session.metadata)
        metadata.update(self._metadata(config))
        if outcome.error:
            metadata["error"] = outcome.error
        else:
            metadata.pop("error", None)
        return self._session_store.update_session(
            session.id,
            status=outcome.status,
            metadata=metadata,
        )

    def _lookup_session(self, native_session_id: str) -> NativeAgentSession:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None:
            raise KeyError(native_session_id)
        return session

    def _config_for_kwargs(self, kwargs: dict[str, Any]) -> ClaudeSdkDeepSeekConfig:
        model = str(kwargs.get("model") or "").strip()
        return replace(self._config, model=model) if model else self._config

    def _metadata(self, config: ClaudeSdkDeepSeekConfig) -> dict[str, Any]:
        return {"base_url": config.base_url, "model": config.model}

    def _api_key(self) -> str:
        return str(self._env.get(self._config.api_key_env, "") or "")

    def _ensure_api_key(self) -> None:
        if not self._api_key():
            raise RuntimeError(f"{self._config.api_key_env} is not set")


def _control_result(
    session: NativeAgentSession,
    *,
    status: str,
) -> NativeAgentControlResult:
    return NativeAgentControlResult(
        provider=session.provider,
        provider_engine=session.provider_engine,
        native_session_id=session.native_session_id,
        agent_run_id=session.agent_run_id,
        status=status,
    )


def _restore_env(key: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous
