from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from wlcodex.native_agents.claude_attachments import (
    materialize_image_attachments,
    safe_images,
)
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
    provider_raw_payload,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore


@dataclass(frozen=True)
class ClaudeSdkDeepSeekConfig:
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com/anthropic"
    model: str = "deepseek-v4-pro"
    effort: str = "xhigh"
    permission_mode: str = "acceptEdits"
    system_prompt: str = ""
    cli_path: str = ""
    ccswitch_fallback_enabled: bool = True
    ccswitch_db_path: str = str(DEFAULT_CCSWITCH_DB_PATH)


@dataclass(frozen=True)
class _RunOutcome:
    status: str
    claude_session_id: str = ""
    assistant_text: str = ""
    usage: dict[str, Any] | None = None
    tool_events: list[dict[str, Any]] | None = None
    raw_tail: list[dict[str, Any]] | None = None
    materialized_images: list[dict[str, str]] | None = None
    error: str = ""


@dataclass
class _ActiveSdkTurn:
    session_id: str
    turn_id: str
    task: asyncio.Task[None]
    interrupted: bool = False


_DEEPSEEK_REASONING_EFFORTS = [
    {"reasoningEffort": "low", "description": "轻量"},
    {"reasoningEffort": "medium", "description": "正常"},
    {"reasoningEffort": "high", "description": "深度"},
    {"reasoningEffort": "xhigh", "description": "极深"},
]
_DEEPSEEK_REASONING_EFFORT_IDS = {
    str(item["reasoningEffort"]) for item in _DEEPSEEK_REASONING_EFFORTS
}

_ALLOWED_CLAUDE_SDK_TOOLS = frozenset(
    {
        "Bash",
        "BashOutput",
        "Edit",
        "Glob",
        "Grep",
        "KillBash",
        "LS",
        "MultiEdit",
        "NotebookEdit",
        "NotebookRead",
        "Read",
        "TodoWrite",
        "Write",
    }
)


class ClaudeAgentSdkRunner:
    def __init__(self) -> None:
        self._active_clients: dict[str, Any] = {}

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        resume_session_id: str = "",
        config: ClaudeSdkDeepSeekConfig,
        api_key: str,
    ):
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as exc:
            raise RuntimeError("claude-agent-sdk is not installed") from exc

        sdk_message_session_id = resume_session_id or session_id or "default"

        async def prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
                "session_id": sdk_message_session_id,
            }

        options = ClaudeAgentOptions(
            cwd=cwd,
            model=config.model,
            effort=config.effort or None,
            permission_mode=config.permission_mode or None,
            system_prompt=config.system_prompt or None,
            cli_path=config.cli_path or None,
            resume=resume_session_id or None,
            env={
                "ANTHROPIC_API_KEY": api_key,
                "ANTHROPIC_AUTH_TOKEN": api_key,
                "ANTHROPIC_BASE_URL": config.base_url,
            },
            include_partial_messages=True,
            include_hook_events=True,
            can_use_tool=_allow_tool,
        )
        client = ClaudeSDKClient(options)
        self._active_clients[session_id] = client
        try:
            await client.connect(prompt_stream())
            async for message in client.receive_response():
                yield message
        finally:
            if self._active_clients.get(session_id) is client:
                self._active_clients.pop(session_id, None)
            disconnect = getattr(client, "disconnect", None)
            if callable(disconnect):
                await disconnect()

    async def interrupt(self, *, session_id: str, turn_id: str) -> bool:
        client = self._active_clients.get(session_id)
        if client is None:
            return False
        interrupt = getattr(client, "interrupt", None)
        if not callable(interrupt):
            return False
        await interrupt()
        return True


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
        self._active_turns: dict[str, _ActiveSdkTurn] = {}

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
            can_interrupt=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": (
                    "Claude Agent SDK sessions continue by prompt, not same-turn steering."
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

    async def list_cached_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
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
            images=safe_images(kwargs.get("images")),
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

    async def peek_session(self, native_session_id: str) -> dict[str, Any]:
        return await self.read_session(native_session_id)

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
            metadata={
                **session.metadata,
                **self._metadata(run_config, credentials),
            },
        )
        self._start_background_prompt(
            session=session,
            native_turn_id=native_turn_id,
            prompt=prompt,
            config=run_config,
            api_key=credentials.api_key,
            images=safe_images(kwargs.get("images")),
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
        session = self._lookup_session(native_session_id)
        active = self._active_turns.get(turn_id or session.last_turn_id)
        if active is None or active.session_id != native_session_id:
            raise KeyError(turn_id or native_session_id)
        active.interrupted = True
        interrupt_error = ""
        interrupt = getattr(self._runner, "interrupt", None)
        interrupted = False
        if callable(interrupt):
            try:
                interrupted = bool(
                    await interrupt(session_id=native_session_id, turn_id=active.turn_id)
                )
            except Exception as exc:
                interrupt_error = exc.__class__.__name__ or str(exc)
        if not interrupted and not active.task.done():
            await asyncio.sleep(0)
            active.task.cancel()
        metadata = dict(session.metadata)
        metadata["error"] = "interrupted"
        if interrupt_error:
            metadata["interrupt_error"] = interrupt_error
        updated = self._session_store.update_session(
            session.id,
            status="interrupted",
            metadata=metadata,
        )
        return _control_result(
            updated,
            status="interrupted",
            turn_id=active.turn_id,
            turn_running=False,
        )

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
        images: list[dict[str, Any]] | None = None,
    ) -> _RunOutcome:
        prompt_for_sdk, materialized_images = materialize_image_attachments(
            cwd,
            prompt,
            images,
        )
        usage: dict[str, Any] | None = None
        claude_session_id = ""
        assistant_text = ""
        tool_events: list[dict[str, Any]] = []
        raw_tail: deque[dict[str, Any]] = deque(maxlen=20)
        try:
            async for _event in self._runner.run(
                prompt=prompt_for_sdk,
                cwd=cwd,
                session_id=session_id,
                resume_session_id=_sdk_resume_session_id(session),
                config=config,
                api_key=api_key,
            ):
                raw_tail.append(_safe_message_summary(_event))
                if self._runtime_store is not None:
                    self._emitter().raw_frame(
                        session,
                        native_turn_id=native_turn_id,
                        raw_kind=str(_event.__class__.__name__ or "sdk.message"),
                        raw_payload=provider_raw_payload(_event),
                    )
                event_session_id = _extract_session_id(_event)
                if event_session_id:
                    claude_session_id = event_session_id
                text = extract_native_agent_text(_event)
                if text:
                    delta, assistant_text = _append_assistant_text(
                        assistant_text,
                        text,
                    )
                    if delta and self._runtime_store is not None:
                        self._emitter().text_delta(
                            session,
                            native_turn_id=native_turn_id,
                            delta=delta,
                        )
                for tool_event in _extract_tool_events(_event):
                    tool_events.append(tool_event)
                    if self._runtime_store is not None:
                        self._emitter().tool_call_started(
                            session,
                            native_turn_id=native_turn_id,
                            tool_id=str(tool_event.get("id") or ""),
                            tool_name=str(tool_event.get("name") or ""),
                            tool_input=_tool_input(tool_event),
                        )
                event_usage = _extract_usage(_event)
                if event_usage:
                    usage = event_usage
                    if self._runtime_store is not None:
                        self._emitter().usage_updated(
                            session,
                            native_turn_id=native_turn_id,
                            usage=event_usage,
                        )
        except asyncio.CancelledError:
            active = self._active_turns.get(native_turn_id)
            if active is not None and active.interrupted:
                return _RunOutcome(
                    status="interrupted",
                    error="interrupted",
                    claude_session_id=claude_session_id,
                    assistant_text=assistant_text,
                    usage=usage,
                    tool_events=tool_events,
                    raw_tail=list(raw_tail),
                    materialized_images=materialized_images,
                )
            raise
        except Exception as exc:
            active = self._active_turns.get(native_turn_id)
            if active is not None and active.interrupted:
                return _RunOutcome(
                    status="interrupted",
                    error="interrupted",
                    claude_session_id=claude_session_id,
                    assistant_text=assistant_text,
                    usage=usage,
                    tool_events=tool_events,
                    raw_tail=list(raw_tail),
                    materialized_images=materialized_images,
                )
            return _RunOutcome(status="failed", error=str(exc))
        active = self._active_turns.get(native_turn_id)
        if active is not None and active.interrupted:
            return _RunOutcome(
                status="interrupted",
                error="interrupted",
                claude_session_id=claude_session_id,
                assistant_text=assistant_text,
                usage=usage,
                tool_events=tool_events,
                raw_tail=list(raw_tail),
                materialized_images=materialized_images,
            )
        return _RunOutcome(
            status="done",
            claude_session_id=claude_session_id,
            assistant_text=assistant_text,
            usage=usage,
            tool_events=tool_events,
            raw_tail=list(raw_tail),
            materialized_images=materialized_images,
        )

    def _start_background_prompt(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str,
        images: list[dict[str, Any]] | None = None,
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
                images=images,
            )
        )
        self._active_turns[native_turn_id] = _ActiveSdkTurn(
            session_id=session.native_session_id,
            turn_id=native_turn_id,
            task=task,
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(
            lambda _task: self._active_turns.pop(native_turn_id, None)
        )

    async def _run_prompt_to_terminal_state(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str,
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            outcome = await self._run_prompt(
                session=session,
                native_turn_id=native_turn_id,
                prompt=prompt,
                cwd=session.cwd,
                session_id=session.native_session_id,
                config=config,
                api_key=api_key,
                images=images,
            )
        except asyncio.CancelledError:
            active = self._active_turns.get(native_turn_id)
            if active is None or not active.interrupted:
                raise
            outcome = _RunOutcome(status="interrupted", error="interrupted")
        updated = self._update_after_run(session, outcome, config, self._credentials_for_api_key(
            api_key,
            config,
        ))
        if self._runtime_store is None:
            return
        emitter = self._emitter()
        if outcome.status in {"failed", "interrupted"}:
            emitter.failed(
                updated,
                native_turn_id=native_turn_id,
                error=outcome.error or outcome.status,
            )
        else:
            if outcome.assistant_text:
                emitter.message_completed(
                    updated,
                    native_turn_id=native_turn_id,
                    text=outcome.assistant_text,
                )
            emitter.completed(updated, native_turn_id=native_turn_id)

    def _update_after_run(
        self,
        session: NativeAgentSession,
        outcome: _RunOutcome,
        config: ClaudeSdkDeepSeekConfig,
        credentials: DeepSeekCredentials,
    ) -> NativeAgentSession:
        current = self._session_store.get_by_native_session_id(
            provider=session.provider,
            provider_engine=session.provider_engine,
            native_session_id=session.native_session_id,
        )
        if current is not None:
            session = current
        metadata = dict(session.metadata)
        metadata.update(self._metadata(config, credentials))
        if outcome.claude_session_id:
            metadata["claude_session_id"] = outcome.claude_session_id
        if outcome.assistant_text:
            metadata["assistant_text"] = outcome.assistant_text
        if outcome.usage is not None:
            metadata["usage"] = outcome.usage
        if outcome.tool_events:
            metadata["tool_events"] = [
                _tool_metadata(event) for event in outcome.tool_events
            ]
        if outcome.raw_tail:
            metadata["raw_tail"] = outcome.raw_tail
        if outcome.materialized_images:
            metadata["images"] = outcome.materialized_images
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
        permission_mode = str(kwargs.get("permission_mode") or "").strip()
        changes: dict[str, str] = {}
        if model:
            changes["model"] = model
        if effort in _DEEPSEEK_REASONING_EFFORT_IDS:
            changes["effort"] = effort
        if permission_mode:
            changes["permission_mode"] = permission_mode
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
            "permission_mode": config.permission_mode,
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


def _sdk_resume_session_id(session: NativeAgentSession) -> str:
    return str(session.metadata.get("claude_session_id") or "").strip()


def _append_assistant_text(current: str, incoming: str) -> tuple[str, str]:
    if not incoming:
        return "", current
    if incoming.startswith(current):
        delta = incoming[len(current) :]
        return delta, incoming
    return incoming, current + incoming


async def _allow_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    _context: Any,
) -> Any:
    del tool_input
    name = str(tool_name or "").strip()
    if name in _ALLOWED_CLAUDE_SDK_TOOLS:
        try:
            from claude_agent_sdk.types import PermissionResultAllow

            return PermissionResultAllow()
        except Exception:
            return {"behavior": "allow"}

    message = f"Tool {name or '<unknown>'} is not enabled for this Claude SDK session."
    try:
        from claude_agent_sdk.types import PermissionResultDeny

        return PermissionResultDeny(message=message, interrupt=False)
    except Exception:
        return {"behavior": "deny", "message": message, "interrupt": False}


def _message_mapping(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    result: dict[str, Any] = {}
    for key in (
        "type",
        "id",
        "name",
        "input",
        "content",
        "text",
        "delta",
        "session_id",
        "usage",
        "total_cost_usd",
        "duration_ms",
    ):
        if hasattr(message, key):
            result[key] = getattr(message, key)
    return result


def _extract_session_id(message: Any) -> str:
    data = _message_mapping(message)
    return str(data.get("session_id") or "")


def _extract_usage(message: Any) -> dict[str, Any] | None:
    data = _message_mapping(message)
    usage = data.get("usage")
    if isinstance(usage, dict) and usage:
        return _jsonable_mapping(usage)
    usage_fields = {}
    for key in ("total_cost_usd", "duration_ms"):
        if data.get(key) is not None:
            usage_fields[key] = data[key]
    return usage_fields or None


def _extract_tool_events(message: Any) -> list[dict[str, Any]]:
    data = _message_mapping(message)
    events: list[dict[str, Any]] = []
    for block in _content_blocks(data.get("content")):
        block_type = str(block.get("type") or "")
        if block_type != "tool_use":
            continue
        events.append(
            {
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "input": _jsonable_mapping(block.get("input")),
                "status": "started",
            }
        )
    if str(data.get("type") or "") == "tool_use":
        events.append(
            {
                "id": str(data.get("id") or ""),
                "name": str(data.get("name") or ""),
                "input": _jsonable_mapping(data.get("input")),
                "status": "started",
            }
        )
    return events


def _content_blocks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [_message_mapping(item) for item in value]
    if isinstance(value, dict):
        return [value]
    return []


def _tool_input(tool_event: dict[str, Any]) -> dict[str, Any]:
    value = tool_event.get("input")
    return value if isinstance(value, dict) else {}


def _tool_metadata(tool_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(tool_event.get("id") or ""),
        "name": str(tool_event.get("name") or ""),
        "status": str(tool_event.get("status") or ""),
    }


def _safe_message_summary(message: Any) -> dict[str, Any]:
    data = _message_mapping(message)
    summary: dict[str, Any] = {}
    for key in ("type", "session_id", "usage", "total_cost_usd", "duration_ms"):
        if key in data:
            summary[key] = _jsonable(data[key])
    text = extract_native_agent_text(message)
    if text:
        summary["text"] = text[-500:]
    tools = _extract_tool_events(message)
    if tools:
        summary["tools"] = tools
    return summary or {"repr": repr(message)[-500:]}


def _jsonable_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _jsonable(item) for key, item in value.items()}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return _jsonable_mapping(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return repr(value)
