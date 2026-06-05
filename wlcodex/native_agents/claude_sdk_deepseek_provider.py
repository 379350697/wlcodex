from __future__ import annotations

import asyncio
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
from wlcodex.native_agents.ccswitch_deepseek import (
    DEFAULT_CCSWITCH_DB_PATH,
    DeepSeekCredentials,
    resolve_deepseek_credentials,
)
from wlcodex.native_agents.runtime_events import (
    NativeAgentRuntimeEmitter,
    extract_native_agent_text,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore


@dataclass(frozen=True)
class ClaudeSdkDeepSeekConfig:
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com/anthropic"
    model: str = "deepseek-v4-pro"
    effort: str = "xhigh"
    ccswitch_fallback_enabled: bool = True
    ccswitch_db_path: str = str(DEFAULT_CCSWITCH_DB_PATH)


@dataclass(frozen=True)
class _RunOutcome:
    status: str
    error: str = ""


_DEEPSEEK_REASONING_EFFORTS = [
    {"reasoningEffort": "low", "description": "轻量"},
    {"reasoningEffort": "medium", "description": "正常"},
    {"reasoningEffort": "high", "description": "深度"},
    {"reasoningEffort": "xhigh", "description": "极深"},
]
_DEEPSEEK_REASONING_EFFORT_IDS = {
    str(item["reasoningEffort"]) for item in _DEEPSEEK_REASONING_EFFORTS
}


class ClaudeAgentSdkRunner:
    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str,
    ):
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as exc:
            raise RuntimeError("claude-agent-sdk is not installed") from exc

        old_api_key = os.environ.get("ANTHROPIC_API_KEY")
        old_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        old_base_url = os.environ.get("ANTHROPIC_BASE_URL")
        os.environ["ANTHROPIC_API_KEY"] = api_key
        os.environ["ANTHROPIC_AUTH_TOKEN"] = api_key
        os.environ["ANTHROPIC_BASE_URL"] = config.base_url
        try:
            options = ClaudeAgentOptions(
                cwd=cwd,
                model=config.model,
                effort=config.effort or None,
            )
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
        runtime_store: RuntimeEventStore | None = None,
        runner: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._session_store = session_store
        self._runtime_store = runtime_store
        self._runner = runner or ClaudeAgentSdkRunner()
        self._env = env if env is not None else os.environ
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def status(self) -> NativeAgentStatus:
        credentials = self._credentials()
        if credentials is None:
            return NativeAgentStatus(
                provider=self.provider,
                provider_engine=self.provider_engine,
                enabled=True,
                connected=False,
                status_code="missing_api_key",
                message=(
                    f"{self._config.api_key_env} is not set and ccswitch "
                    "DeepSeek credentials were not found."
                ),
                metadata=self._metadata(self._config),
            )
        config = self._config_for_credentials(self._config, credentials)
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
            metadata=self._metadata(config, credentials),
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
        return [
            {
                "id": self._config.model,
                "model": self._config.model,
                "displayName": self._config.model,
                "defaultReasoningEffort": self._config.effort,
                "supportedReasoningEfforts": list(_DEEPSEEK_REASONING_EFFORTS),
                "serviceTiers": [],
            }
        ]

    async def wait_for_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        await asyncio.gather(*list(self._background_tasks))

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        credentials = self._ensure_credentials()
        run_config = self._config_for_credentials(
            self._config_for_kwargs(kwargs),
            credentials,
        )
        native_session_id = f"claude-sdk-{uuid4()}"
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=prompt.strip()[:80] or "Claude DeepSeek SDK session",
            cwd=cwd,
            source_kind="claude_sdk_deepseek",
            status="running",
            last_turn_id=_new_turn_id(self.provider_engine),
            metadata=self._metadata(run_config, credentials),
        )
        self._start_background_prompt(
            session=session,
            native_turn_id=session.last_turn_id,
            prompt=prompt,
            config=run_config,
            api_key=credentials.api_key,
        )
        return _control_result(
            session,
            status="started",
            turn_id=session.last_turn_id,
            turn_running=True,
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        credentials = self._ensure_credentials()
        run_config = self._config_for_credentials(
            self._config_for_kwargs(kwargs),
            credentials,
        )
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"claude-sdk-{uuid4()}",
            title="Claude DeepSeek SDK session",
            cwd=cwd,
            source_kind="claude_sdk_deepseek",
            status="created",
            metadata=self._metadata(run_config, credentials),
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
        credentials = self._ensure_credentials()
        session = self._lookup_session(native_session_id)
        run_config = self._config_for_credentials(
            self._config_for_kwargs(kwargs),
            credentials,
        )
        native_turn_id = _new_turn_id(self.provider_engine)
        session = self._session_store.update_session(
            session.id,
            status="running",
            last_turn_id=native_turn_id,
            metadata=self._metadata(run_config, credentials),
        )
        self._start_background_prompt(
            session=session,
            native_turn_id=native_turn_id,
            prompt=prompt,
            config=run_config,
            api_key=credentials.api_key,
        )
        return _control_result(
            session,
            status="continued",
            turn_id=native_turn_id,
            turn_running=True,
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
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str,
    ) -> _RunOutcome:
        try:
            async for _event in self._runner.run(
                prompt=prompt,
                cwd=cwd,
                session_id=session_id,
                config=config,
                api_key=api_key,
            ):
                text = extract_native_agent_text(_event)
                if text and self._runtime_store is not None:
                    self._emitter().text_delta(
                        session,
                        native_turn_id=native_turn_id,
                        delta=text,
                    )
        except Exception as exc:
            return _RunOutcome(status="failed", error=str(exc))
        return _RunOutcome(status="done")

    def _start_background_prompt(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str,
    ) -> None:
        if self._runtime_store is not None:
            emitter = self._emitter()
            emitter.started(session, native_turn_id=native_turn_id)
            emitter.user_message(session, native_turn_id=native_turn_id, text=prompt)
        task = asyncio.create_task(
            self._run_prompt_to_terminal_state(
                session=session,
                native_turn_id=native_turn_id,
                prompt=prompt,
                config=config,
                api_key=api_key,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_prompt_to_terminal_state(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str,
    ) -> None:
        outcome = await self._run_prompt(
            session=session,
            native_turn_id=native_turn_id,
            prompt=prompt,
            cwd=session.cwd,
            session_id=session.native_session_id,
            config=config,
            api_key=api_key,
        )
        updated = self._update_after_run(session, outcome, config, self._credentials_for_api_key(
            api_key,
            config,
        ))
        if self._runtime_store is None:
            return
        emitter = self._emitter()
        if outcome.status == "failed":
            emitter.failed(updated, native_turn_id=native_turn_id, error=outcome.error)
        else:
            emitter.completed(updated, native_turn_id=native_turn_id)

    def _update_after_run(
        self,
        session: NativeAgentSession,
        outcome: _RunOutcome,
        config: ClaudeSdkDeepSeekConfig,
        credentials: DeepSeekCredentials,
    ) -> NativeAgentSession:
        metadata = dict(session.metadata)
        metadata.update(self._metadata(config, credentials))
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
        effort = str(kwargs.get("effort") or "").strip()
        changes: dict[str, str] = {}
        if model:
            changes["model"] = model
        if effort in _DEEPSEEK_REASONING_EFFORT_IDS:
            changes["effort"] = effort
        return replace(self._config, **changes) if changes else self._config

    def _config_for_credentials(
        self,
        config: ClaudeSdkDeepSeekConfig,
        credentials: DeepSeekCredentials,
    ) -> ClaudeSdkDeepSeekConfig:
        if credentials.source == "ccswitch" and credentials.base_url:
            return replace(config, base_url=credentials.base_url)
        return config

    def _metadata(
        self,
        config: ClaudeSdkDeepSeekConfig,
        credentials: DeepSeekCredentials | None = None,
    ) -> dict[str, Any]:
        metadata = {
            "base_url": config.base_url,
            "model": config.model,
            "effort": config.effort,
        }
        if credentials is not None:
            metadata.update(credentials.safe_metadata())
        return metadata

    def _credentials(self) -> DeepSeekCredentials | None:
        db_path = self._config.ccswitch_db_path if self._config.ccswitch_fallback_enabled else ""
        return resolve_deepseek_credentials(
            env=self._env,
            db_path=db_path,
            api_key_env=self._config.api_key_env,
        )

    def _credentials_for_api_key(
        self,
        api_key: str,
        config: ClaudeSdkDeepSeekConfig,
    ) -> DeepSeekCredentials:
        credentials = self._credentials()
        if credentials is not None and credentials.api_key == api_key:
            return credentials
        return DeepSeekCredentials(api_key=api_key, base_url=config.base_url, source="env")

    def _ensure_credentials(self) -> DeepSeekCredentials:
        credentials = self._credentials()
        if credentials is None:
            raise RuntimeError(
                f"{self._config.api_key_env} is not set and ccswitch "
                "DeepSeek credentials were not found"
            )
        return credentials

    def _emitter(self) -> NativeAgentRuntimeEmitter:
        if self._runtime_store is None:
            raise RuntimeError("runtime store is not configured")
        return NativeAgentRuntimeEmitter(
            runtime_store=self._runtime_store,
            provider=self.provider,
            provider_engine=self.provider_engine,
            source_kind="claude_sdk_deepseek",
        )


def _control_result(
    session: NativeAgentSession,
    *,
    status: str,
    turn_id: str = "",
    turn_running: bool = False,
) -> NativeAgentControlResult:
    return NativeAgentControlResult(
        provider=session.provider,
        provider_engine=session.provider_engine,
        native_session_id=session.native_session_id,
        agent_run_id=session.agent_run_id,
        turn_id=turn_id,
        active_turn_id=turn_id if turn_running else "",
        turn_running=turn_running,
        status=status,
    )


def _new_turn_id(provider_engine: str) -> str:
    return f"{provider_engine}-turn-{uuid4()}"


def _restore_env(key: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous
