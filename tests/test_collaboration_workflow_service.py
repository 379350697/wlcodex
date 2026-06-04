from __future__ import annotations

import pytest

from wlcodex.collaboration.workflow_service import WorkflowService
from wlcodex.collaboration.workflow_store import WorkflowRunStore
from wlcodex.db import Ledger
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentStatus,
)
from wlcodex.native_agents.provider import NativeAgentRegistry


class ServiceFakeProvider:
    def __init__(
        self,
        provider: str,
        *,
        can_start_session: bool = True,
        session_payload: dict | None = None,
    ) -> None:
        self.provider = provider
        self.provider_engine = "fake"
        self.can_start_session = can_start_session
        self.session_payload = session_payload or {
            "turns": [
                {
                    "role": "user",
                    "content": "请实现这个小功能",
                },
                {
                    "role": "assistant",
                    "content": (
                        "Spec: docs/superpowers/specs/a.md\n"
                        "Plan: docs/superpowers/plans/a.md"
                    ),
                },
            ]
        }
        self.calls: list[tuple] = []

    async def status(self):
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self):
        self.calls.append(("capabilities",))
        return NativeAgentCapabilities(can_start_session=self.can_start_session)

    async def list_sessions(self, limit: int = 50):
        return []

    async def list_models(self):
        return []

    async def start_session(self, cwd: str, prompt: str, **kwargs):
        self.calls.append(("start_session", cwd, prompt, kwargs))
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=f"{self.provider}-target-session",
            agent_run_id=128,
            turn_id="turn-1",
            turn_running=True,
            status="started",
        )

    async def create_session(self, cwd: str, **kwargs):
        raise AssertionError("create_session should not be used for execute handoff")

    async def read_session(self, native_session_id: str):
        self.calls.append(("read_session", native_session_id))
        return self.session_payload

    async def attach_session(self, native_session_id: str):
        raise NotImplementedError

    async def sync_session(self, native_session_id: str):
        raise NotImplementedError

    async def continue_session(self, native_session_id: str, prompt: str, **kwargs):
        raise NotImplementedError

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs,
    ):
        raise NotImplementedError

    async def interrupt_session(self, native_session_id: str, turn_id: str = ""):
        raise NotImplementedError

    async def resolve_approval(self, request_id: str, body: dict):
        raise NotImplementedError


def _service(tmp_path, providers):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return WorkflowService(
        registry=NativeAgentRegistry(providers),
        store=WorkflowRunStore(ledger),
        default_worker_id=42,
    )


@pytest.mark.asyncio
async def test_preview_reads_source_and_does_not_start_target(tmp_path) -> None:
    source = ServiceFakeProvider("antigravity")
    target = ServiceFakeProvider("claude")
    service = _service(tmp_path, [source, target])

    preview = await service.preview_handoff(
        source_provider="antigravity",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    assert preview["intent"] == "execute_plan"
    assert preview["target_provider"] == "claude"
    assert preview["workflow_run_id"].startswith("wf_")
    assert preview["preview_id"].startswith("preview_")
    assert "docs/superpowers/specs/a.md" in preview["prompt"]
    assert ("read_session", "source-session") in source.calls
    assert not any(call[0] == "start_session" for call in target.calls)


@pytest.mark.asyncio
async def test_preview_falls_back_to_source_session_cwd(tmp_path) -> None:
    source = ServiceFakeProvider(
        "antigravity",
        session_payload={
            "thread": {"cwd": "/Users/wl/projects/wlcodex"},
            "turns": [
                {
                    "role": "user",
                    "content": "执行 docs/superpowers/plans/a.md",
                },
            ],
        },
    )
    target = ServiceFakeProvider("claude")
    service = _service(tmp_path, [source, target])

    preview = await service.preview_handoff(
        source_provider="antigravity",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude",
        cwd="",
        intent="execute_plan",
        user_note="",
    )

    assert preview["cwd"] == "/Users/wl/projects/wlcodex"
    assert "Workspace: /Users/wl/projects/wlcodex" in preview["prompt"]

    result = await service.execute_handoff(
        workflow_run_id=preview["workflow_run_id"],
        preview_id=preview["preview_id"],
        target_provider="claude",
        cwd=preview["cwd"],
        prompt=preview["prompt"],
    )

    assert result["target_provider"] == "claude"
    assert target.calls[-1][0:2] == (
        "start_session",
        "/Users/wl/projects/wlcodex",
    )


@pytest.mark.asyncio
async def test_preview_extracts_real_thread_turn_items(tmp_path) -> None:
    source = ServiceFakeProvider(
        "codex",
        session_payload={
            "thread": {
                "id": "thread-real",
                "turns": [
                    {
                        "id": "turn-1",
                        "items": [
                            {
                                "type": "userMessage",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "请让 Claude 执行 docs/superpowers/plans/a.md",
                                    }
                                ],
                            },
                            {
                                "type": "agentMessage",
                                "text": (
                                    "Spec ready at docs/superpowers/specs/a.md. "
                                    "Plan ready at docs/superpowers/plans/a.md."
                                ),
                            },
                        ],
                    }
                ],
            }
        },
    )
    target = ServiceFakeProvider("claude")
    service = _service(tmp_path, [source, target])

    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="thread-real",
        source_turn_id="",
        target_provider="claude",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    assert preview["intent"] == "execute_plan"
    assert "请让 Claude 执行 docs/superpowers/plans/a.md" in preview["prompt"]
    assert "docs/superpowers/specs/a.md" in preview["prompt"]
    assert "docs/superpowers/plans/a.md" in preview["prompt"]


@pytest.mark.asyncio
async def test_execute_handoff_uses_edited_prompt_and_returns_target_url(
    tmp_path,
) -> None:
    source = ServiceFakeProvider("codex")
    target = ServiceFakeProvider("claude")
    service = _service(tmp_path, [source, target])
    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    result = await service.execute_handoff(
        workflow_run_id=preview["workflow_run_id"],
        preview_id=preview["preview_id"],
        target_provider="claude",
        cwd="/repo",
        prompt="Edited handoff prompt.",
    )

    assert result["status"] == "running"
    assert result["target_provider"] == "claude"
    assert result["target_thread_id"] == "claude-target-session"
    assert result["target_url"] == (
        "/workers/128/live?native_provider=claude"
        "&native_thread_id=claude-target-session"
    )
    assert target.calls[-1][0:3] == (
        "start_session",
        "/repo",
        "Edited handoff prompt.",
    )


@pytest.mark.asyncio
async def test_execute_rejects_target_without_start_capability(tmp_path) -> None:
    source = ServiceFakeProvider("codex")
    target = ServiceFakeProvider("antigravity", can_start_session=False)
    service = _service(tmp_path, [source, target])
    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="antigravity",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    with pytest.raises(ValueError, match="cannot start sessions"):
        await service.execute_handoff(
            workflow_run_id=preview["workflow_run_id"],
            preview_id=preview["preview_id"],
            target_provider="antigravity",
            cwd="/repo",
            prompt=preview["prompt"],
        )


@pytest.mark.asyncio
async def test_execute_rejects_target_provider_mismatch(tmp_path) -> None:
    source = ServiceFakeProvider("codex")
    target = ServiceFakeProvider("claude")
    other_target = ServiceFakeProvider("antigravity")
    service = _service(tmp_path, [source, target, other_target])
    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    with pytest.raises(ValueError, match="target provider does not match preview"):
        await service.execute_handoff(
            workflow_run_id=preview["workflow_run_id"],
            preview_id=preview["preview_id"],
            target_provider="antigravity",
            cwd="/repo",
            prompt=preview["prompt"],
        )


@pytest.mark.asyncio
async def test_execute_rejects_cwd_mismatch(tmp_path) -> None:
    source = ServiceFakeProvider("codex")
    target = ServiceFakeProvider("claude")
    service = _service(tmp_path, [source, target])
    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    with pytest.raises(ValueError, match="workspace does not match preview"):
        await service.execute_handoff(
            workflow_run_id=preview["workflow_run_id"],
            preview_id=preview["preview_id"],
            target_provider="claude",
            cwd="/other",
            prompt=preview["prompt"],
        )


@pytest.mark.asyncio
async def test_execute_handoff_escapes_target_url_values(tmp_path) -> None:
    source = ServiceFakeProvider("codex")
    target = ServiceFakeProvider("claude/dev")
    service = _service(tmp_path, [source, target])
    preview = await service.preview_handoff(
        source_provider="codex",
        source_thread_id="source-session",
        source_turn_id="",
        target_provider="claude/dev",
        cwd="/repo",
        intent="auto",
        user_note="",
    )

    result = await service.execute_handoff(
        workflow_run_id=preview["workflow_run_id"],
        preview_id=preview["preview_id"],
        target_provider="claude/dev",
        cwd="/repo",
        prompt=preview["prompt"],
    )

    assert "native_provider=claude%2Fdev" in result["target_url"]
