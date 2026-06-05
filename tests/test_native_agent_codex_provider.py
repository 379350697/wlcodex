from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from wlcodex.codex_native.models import (
    NativeCodexControlResult,
    NativeCodexStatus,
)
from wlcodex.native_agents.codex_provider import CodexAppServerProvider


@dataclass
class FakeCodexController:
    async def status(self):
        return NativeCodexStatus(
            enabled=True,
            connected=True,
            remote_control_status="enabled",
            server_name="Codex",
        )

    async def list_sessions(self, limit: int = 50):
        return [
            SimpleNamespace(
                id=1,
                native_thread_id="thread-1",
                agent_run_id=2,
                conversation_id=3,
                title="Codex work",
                cwd="/repo",
                source_kind="codex_native",
                status="running",
                last_turn_id="turn-1",
                activity_at="2026-06-01T00:00:00Z",
                created_at="2026-06-01T00:00:00Z",
                updated_at="2026-06-01T00:01:00Z",
                metadata={
                    "model": "gpt-5.5",
                    "effort": "xhigh",
                    "service_tier": "priority",
                },
            )
        ]

    async def list_models(self):
        return [
            {
                "id": "gpt-5.5",
                "model": "gpt-5.5",
                "displayName": "GPT-5.5",
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Light"},
                    {"reasoningEffort": "medium", "description": "Normal"},
                    {"reasoningEffort": "high", "description": "Deep"},
                    {"reasoningEffort": "xhigh", "description": "Very deep"},
                ],
            }
        ]

    async def start_session(self, cwd: str, prompt: str, **kwargs):
        return NativeCodexControlResult(
            native_thread_id="thread-2",
            agent_run_id=4,
            status="started",
        )

    async def resolve_approval(self, request_id: str, body: dict):
        return {"codex_request_id": request_id, "status": "resolved", "body": body}


@pytest.mark.asyncio
async def test_codex_provider_normalizes_status_and_sessions() -> None:
    provider = CodexAppServerProvider(FakeCodexController())

    status = await provider.status()
    sessions = await provider.list_sessions()

    assert provider.provider == "codex"
    assert provider.provider_engine == "app-server"
    assert status.provider == "codex"
    assert status.status_code == "enabled"
    assert sessions[0].provider == "codex"
    assert sessions[0].native_session_id == "thread-1"
    assert sessions[0].metadata == {
        "model": "gpt-5.5",
        "effort": "xhigh",
        "service_tier": "priority",
    }


@pytest.mark.asyncio
async def test_codex_provider_defaults_reasoning_to_highest_supported_effort() -> None:
    provider = CodexAppServerProvider(FakeCodexController())

    models = await provider.list_models()

    assert models[0]["defaultReasoningEffort"] == "xhigh"


@pytest.mark.asyncio
async def test_codex_provider_wraps_control_result() -> None:
    provider = CodexAppServerProvider(FakeCodexController())

    result = await provider.start_session("/repo", "fix it")

    assert result.provider == "codex"
    assert result.provider_engine == "app-server"
    assert result.native_session_id == "thread-2"


@pytest.mark.asyncio
async def test_codex_provider_keeps_approval_result_shape() -> None:
    provider = CodexAppServerProvider(FakeCodexController())

    result = await provider.resolve_approval("req-1", {"decision": "approved"})

    assert result == {
        "codex_request_id": "req-1",
        "status": "resolved",
        "body": {"decision": "approved"},
    }
