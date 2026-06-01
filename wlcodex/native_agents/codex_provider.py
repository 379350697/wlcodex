from __future__ import annotations

from typing import Any

from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)


class CodexAppServerProvider:
    provider = "codex"
    provider_engine = "app-server"

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    async def status(self) -> NativeAgentStatus:
        status = await self._controller.status()
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=bool(getattr(status, "enabled", True)),
            connected=bool(getattr(status, "connected", False)),
            status_code=str(getattr(status, "remote_control_status", "unknown")),
            message=str(getattr(status, "error", "") or ""),
            metadata={
                "server_name": str(getattr(status, "server_name", "") or ""),
                "installation_id": str(getattr(status, "installation_id", "") or ""),
                "environment_id": getattr(status, "environment_id", None),
            },
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
            can_steer_active_turn=True,
            can_interrupt=True,
            can_resolve_approval=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        sessions = await self._controller.list_sessions(limit)
        return [_session_from_codex(session) for session in sessions]

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._controller.list_models()

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        result = await self._controller.start_session(cwd, prompt, **kwargs)
        return _result_from_codex(result)

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        result = await self._controller.create_session(cwd, **kwargs)
        return _result_from_codex(result)

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        return await self._controller.read_session(native_session_id)

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        return _result_from_codex(await self._controller.attach_session(native_session_id))

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        return _result_from_codex(await self._controller.sync_session(native_session_id))

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        return _result_from_codex(
            await self._controller.continue_session(
                native_session_id,
                prompt,
                **kwargs,
            )
        )

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        return _result_from_codex(
            await self._controller.steer_session(
                native_session_id,
                expected_turn_id,
                prompt,
                **kwargs,
            )
        )

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult:
        return _result_from_codex(
            await self._controller.interrupt_session(native_session_id, turn_id)
        )

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._controller.resolve_approval(request_id, body)


def _session_from_codex(session: Any) -> NativeAgentSession:
    return NativeAgentSession(
        id=int(session.id),
        provider="codex",
        provider_engine="app-server",
        native_session_id=str(session.native_thread_id),
        agent_run_id=int(session.agent_run_id),
        conversation_id=int(session.conversation_id),
        title=str(session.title),
        cwd=str(session.cwd),
        source_kind=str(session.source_kind),
        status=str(session.status),
        last_turn_id=str(session.last_turn_id),
        activity_at=str(getattr(session, "activity_at", "") or ""),
        created_at=str(session.created_at),
        updated_at=str(session.updated_at),
    )


def _result_from_codex(result: Any) -> NativeAgentControlResult:
    return NativeAgentControlResult(
        provider="codex",
        provider_engine="app-server",
        native_session_id=str(result.native_thread_id),
        agent_run_id=int(result.agent_run_id),
        turn_id=str(getattr(result, "turn_id", "") or ""),
        active_turn_id=str(getattr(result, "active_turn_id", "") or ""),
        turn_running=bool(getattr(result, "turn_running", False)),
        status=str(getattr(result, "status", "ok")),
    )
