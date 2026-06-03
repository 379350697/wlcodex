from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.runtime_events import (
    NativeAgentRuntimeEmitter,
    extract_native_agent_text,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore


@dataclass(frozen=True)
class _RunOutcome:
    status: str
    error: str = ""


class AntigravitySdkRunner:
    def __init__(self) -> None:
        try:
            from google.antigravity import Agent, LocalAgentConfig
        except ImportError as exc:
            self.available = False
            self.error = str(exc)
            self._agent_cls = None
            self._config_cls = None
        else:
            self.available = True
            self.error = ""
            self._agent_cls = Agent
            self._config_cls = LocalAgentConfig

    async def run(self, *, prompt: str, cwd: str, session_id: str):
        if not self.available:
            raise RuntimeError(self.error)
        if self._agent_cls is None or self._config_cls is None:
            raise RuntimeError("Antigravity SDK is not available")
        config_kwargs = {"workspaces": [cwd]} if cwd else {}
        config = self._config_cls(**config_kwargs)
        async with self._agent_cls(config) as agent:
            response = await agent.chat(prompt)
            yield {
                "type": "assistant",
                "text": await response.text(),
                "cwd": cwd,
                "session_id": session_id,
            }


class AntigravitySdkProvider:
    provider = "antigravity"
    provider_engine = "sdk"

    def __init__(
        self,
        *,
        session_store: NativeAgentSessionStore,
        runtime_store: RuntimeEventStore | None = None,
        runner: Any | None = None,
    ) -> None:
        self._session_store = session_store
        self._runtime_store = runtime_store
        self._runner = runner or AntigravitySdkRunner()
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def wait_for_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    async def status(self) -> NativeAgentStatus:
        if not bool(getattr(self._runner, "available", False)):
            return NativeAgentStatus(
                provider=self.provider,
                provider_engine=self.provider_engine,
                enabled=True,
                connected=False,
                status_code="sdk_not_installed",
                message=str(getattr(self._runner, "error", "")),
            )
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": (
                    "Antigravity SDK steering is not exposed in this slice."
                ),
                "can_interrupt": (
                    "Antigravity SDK cancellation is not exposed in this slice."
                ),
                "can_resolve_approval": (
                    "Antigravity SDK approval mapping is not enabled in this slice."
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
        return []

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        native_session_id = f"antigravity-sdk-{uuid4()}"
        native_turn_id = _new_turn_id()
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=prompt.strip()[:80] or "Antigravity SDK session",
            cwd=cwd,
            source_kind="antigravity_sdk",
            status="running",
            last_turn_id=native_turn_id,
        )
        self._start_background_prompt(
            session=session,
            prompt=prompt,
            native_turn_id=native_turn_id,
        )
        return _control_result(
            session,
            status="started",
            turn_id=native_turn_id,
            turn_running=True,
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"antigravity-sdk-{uuid4()}",
            title="Antigravity SDK session",
            cwd=cwd,
            source_kind="antigravity_sdk",
            status="created",
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
        session = self._lookup_session(native_session_id)
        native_turn_id = _new_turn_id()
        session = self._session_store.update_session(
            session.id,
            status="running",
            last_turn_id=native_turn_id,
        )
        self._start_background_prompt(
            session=session,
            prompt=prompt,
            native_turn_id=native_turn_id,
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
        raise NotImplementedError("Antigravity SDK provider does not support steering")

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult:
        raise NotImplementedError("Antigravity SDK provider does not support interrupt yet")

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
        prompt: str,
        native_turn_id: str,
    ) -> _RunOutcome:
        emitter = self._emitter()
        try:
            async for event in self._runner.run(
                prompt=prompt,
                cwd=session.cwd,
                session_id=session.native_session_id,
            ):
                text = extract_native_agent_text(event)
                if text and emitter is not None:
                    emitter.text_delta(session, native_turn_id=native_turn_id, delta=text)
        except Exception as exc:
            return _RunOutcome(status="failed", error=str(exc))
        return _RunOutcome(status="done")

    def _start_background_prompt(
        self,
        *,
        session: NativeAgentSession,
        prompt: str,
        native_turn_id: str,
    ) -> None:
        emitter = self._emitter()
        if emitter is not None:
            emitter.started(session, native_turn_id=native_turn_id)
            emitter.user_message(session, native_turn_id=native_turn_id, text=prompt)
        task = asyncio.create_task(
            self._run_prompt_to_terminal_state(
                session=session,
                prompt=prompt,
                native_turn_id=native_turn_id,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_prompt_to_terminal_state(
        self,
        *,
        session: NativeAgentSession,
        prompt: str,
        native_turn_id: str,
    ) -> None:
        outcome = await self._run_prompt(
            session=session,
            prompt=prompt,
            native_turn_id=native_turn_id,
        )
        updated = self._update_after_run(session, outcome)
        emitter = self._emitter()
        if emitter is None:
            return
        if outcome.status == "done":
            emitter.completed(updated, native_turn_id=native_turn_id)
        else:
            emitter.failed(updated, native_turn_id=native_turn_id, error=outcome.error)

    def _update_after_run(
        self,
        session: NativeAgentSession,
        outcome: _RunOutcome,
    ) -> NativeAgentSession:
        metadata = dict(session.metadata)
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

    def _emitter(self) -> NativeAgentRuntimeEmitter | None:
        if self._runtime_store is None:
            return None
        return NativeAgentRuntimeEmitter(
            runtime_store=self._runtime_store,
            provider=self.provider,
            provider_engine=self.provider_engine,
            source_kind="antigravity_sdk",
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


def _new_turn_id() -> str:
    return f"antigravity-sdk-turn-{uuid4()}"
