from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wlcodex.agent_backend import AgentStreamEvent
from wlcodex.db import Ledger
from wlcodex.native_agents.claude_cli_provider import ClaudeCliLocalProvider
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


class FakeClaudeEngine:
    enabled = True

    def __init__(
        self,
        *,
        session_id: str = "claude-real-1",
        errors: list[str] | None = None,
    ) -> None:
        self.session_id = session_id
        self.errors = errors or []
        self.requests = []

    async def send_streaming(self, request):
        self.requests.append(request)
        if self.errors:
            yield AgentStreamEvent(
                delta=self.errors.pop(0),
                event_type="error",
                session_id=self.session_id,
            )
            return
        yield AgentStreamEvent(delta="hello", event_type="text")
        yield AgentStreamEvent(event_type="session", session_id=self.session_id)


class BlockingClaudeEngine:
    enabled = True

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.requests = []

    async def send_streaming(self, request):
        self.requests.append(request)
        await self.release.wait()
        yield AgentStreamEvent(
            delta="background hello",
            event_type="text",
            session_id="claude-real-1",
        )


def _provider(
    tmp_path: Path,
    *,
    engine=None,
) -> tuple[
    ClaudeCliLocalProvider,
    NativeAgentSessionStore,
    object,
    RuntimeEventStore,
]:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = NativeAgentSessionStore(ledger)
    runtime_store = RuntimeEventStore(ledger._conn)
    fake_engine = engine or FakeClaudeEngine()
    return (
        ClaudeCliLocalProvider(
            engine=fake_engine,
            session_store=store,
            runtime_store=runtime_store,
            default_cwd=str(tmp_path),
        ),
        store,
        fake_engine,
        runtime_store,
    )


@pytest.mark.asyncio
async def test_claude_cli_provider_start_session_runs_in_background_and_streams_events(
    tmp_path: Path,
) -> None:
    engine = BlockingClaudeEngine()
    provider, store, _engine, runtime_store = _provider(tmp_path, engine=engine)

    result = await asyncio.wait_for(
        provider.start_session(str(tmp_path), "say hi"),
        timeout=0.05,
    )

    assert result.status == "started"
    assert result.turn_running is True
    assert result.turn_id
    assert result.active_turn_id == result.turn_id
    initial_events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [event.event_type for event in initial_events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
    ]
    assert initial_events[0].payload["native_turn_id"] == result.turn_id
    await asyncio.sleep(0)
    assert engine.requests[0].prompt == "say hi"

    engine.release.set()
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.MODEL_TEXT_DELTA,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert events[2].payload["delta"] == "background hello"


@pytest.mark.asyncio
async def test_claude_cli_provider_starts_session_and_records_agent_run(
    tmp_path: Path,
) -> None:
    provider, store, engine, _runtime_store = _provider(tmp_path)

    result = await provider.start_session(str(tmp_path), "say hi")

    assert result.provider == "claude"
    assert result.provider_engine == "cli-local"
    assert result.native_session_id.startswith("claude-cli-")
    assert result.agent_run_id > 0
    assert result.status == "started"
    assert result.turn_running is True
    await provider.wait_for_background_tasks()
    assert engine.requests[0].prompt == "say hi"
    assert engine.requests[0].workspace_path == str(tmp_path)
    assert engine.requests[0].extra == {}
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    assert session.metadata["claude_session_id"] == "claude-real-1"


@pytest.mark.asyncio
async def test_claude_cli_provider_continues_with_real_claude_session_id(
    tmp_path: Path,
) -> None:
    provider, _store, engine, _runtime_store = _provider(tmp_path)
    started = await provider.start_session(str(tmp_path), "first")
    await provider.wait_for_background_tasks()
    engine.session_id = "claude-real-2"

    result = await provider.continue_session(started.native_session_id, "second")
    await provider.wait_for_background_tasks()

    assert result.status == "continued"
    assert result.turn_running is True
    assert engine.requests[1].prompt == "second"
    assert engine.requests[1].extra == {"resume_session_id": "claude-real-1"}


@pytest.mark.asyncio
async def test_claude_cli_provider_create_session_does_not_call_engine(
    tmp_path: Path,
) -> None:
    provider, _store, engine, _runtime_store = _provider(tmp_path)

    result = await provider.create_session(str(tmp_path))

    assert result.status == "created"
    assert result.native_session_id.startswith("claude-cli-")
    assert engine.requests == []


@pytest.mark.asyncio
async def test_claude_cli_provider_create_then_continue_starts_without_resume(
    tmp_path: Path,
) -> None:
    provider, store, engine, _runtime_store = _provider(tmp_path)
    created = await provider.create_session(str(tmp_path))

    result = await provider.continue_session(created.native_session_id, "first prompt")

    assert result.status == "continued"
    assert result.turn_running is True
    await provider.wait_for_background_tasks()
    assert engine.requests[0].extra == {}
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=created.native_session_id,
    )
    assert session is not None
    assert session.metadata["claude_session_id"] == "claude-real-1"


def test_claude_cli_provider_capabilities_disable_active_turn_steering(
    tmp_path: Path,
) -> None:
    provider, _store, _engine, _runtime_store = _provider(tmp_path)

    caps = provider.capabilities()

    assert caps.can_start_session is True
    assert caps.can_continue_session is True
    assert caps.can_steer_active_turn is False
    assert "can_steer_active_turn" in caps.disabled_reasons


@pytest.mark.asyncio
async def test_claude_cli_provider_lists_only_cli_local_sessions(
    tmp_path: Path,
) -> None:
    provider, store, _engine, _runtime_store = _provider(tmp_path)
    cli = store.get_or_create_session(
        provider="claude",
        provider_engine="cli-local",
        native_session_id="cli-1",
        title="CLI",
    )
    for index in range(41):
        store.get_or_create_session(
            provider="claude",
            provider_engine="sdk-deepseek",
            native_session_id=f"sdk-{index}",
            title="SDK",
        )

    sessions = await provider.list_sessions(1)

    assert [session.id for session in sessions] == [cli.id]


@pytest.mark.asyncio
async def test_claude_cli_provider_clears_stale_error_after_success(
    tmp_path: Path,
) -> None:
    engine = FakeClaudeEngine(errors=["boom"])
    provider, store, _engine, runtime_store = _provider(tmp_path, engine=engine)
    created = await provider.create_session(str(tmp_path))

    failed = await provider.continue_session(created.native_session_id, "fail")
    assert failed.status == "continued"
    assert failed.turn_running is True
    await provider.wait_for_background_tasks()
    failed_session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=created.native_session_id,
    )
    assert failed_session is not None
    assert failed_session.status == "failed"
    failed_events = runtime_store.list_by_agent_run(failed.agent_run_id)
    assert failed_events[-1].event_type == EventType.AGENT_RUN_FAILED
    assert failed_events[-1].payload["error"] == "boom"

    engine.session_id = "claude-real-2"
    succeeded = await provider.continue_session(created.native_session_id, "recover")
    await provider.wait_for_background_tasks()

    assert succeeded.status == "continued"
    assert succeeded.turn_running is True
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=created.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    assert "error" not in session.metadata
    assert session.metadata["claude_session_id"] == "claude-real-2"
