from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from wlcodex.agent_backend import AgentStreamEvent
from wlcodex.db import Ledger
from wlcodex.native_agents.claude_local_sessions import ClaudeLocalSessionIndex
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


class SilentClaudeEngine:
    enabled = True

    def __init__(self) -> None:
        self.requests = []

    async def send_streaming(self, request):
        self.requests.append(request)
        if False:
            yield AgentStreamEvent(delta="", event_type="text")


class JsonlAppendingClaudeEngine:
    enabled = True

    def __init__(self, *, session_id: str, session_path: Path) -> None:
        self.session_id = session_id
        self.session_path = session_path
        self.requests = []

    async def send_streaming(self, request):
        self.requests.append(request)
        with self.session_path.open("a", encoding="utf-8") as handle:
            for row in [
                {
                    "type": "user",
                    "uuid": "user-continued",
                    "sessionId": self.session_id,
                    "timestamp": "2026-06-03T09:00:00.000Z",
                    "message": {"role": "user", "content": request.prompt},
                },
                {
                    "type": "assistant",
                    "uuid": "assistant-continued",
                    "sessionId": self.session_id,
                    "timestamp": "2026-06-03T09:00:01.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "jsonl answer"}],
                    },
                },
            ]:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        yield AgentStreamEvent(event_type="session", session_id=self.session_id)


class JsonlStreamingClaudeEngine:
    enabled = True

    def __init__(self, *, session_id: str, session_path: Path) -> None:
        self.session_id = session_id
        self.session_path = session_path
        self.requests = []

    async def send_streaming(self, request):
        self.requests.append(request)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        with self.session_path.open("a", encoding="utf-8") as handle:
            for row in [
                {
                    "type": "user",
                    "uuid": "user-live",
                    "sessionId": self.session_id,
                    "timestamp": "2026-06-03T09:01:00.000Z",
                    "message": {"role": "user", "content": request.prompt},
                },
                {
                    "type": "assistant",
                    "uuid": "assistant-live",
                    "sessionId": self.session_id,
                    "timestamp": "2026-06-03T09:01:01.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "live answer"}],
                    },
                },
            ]:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        yield AgentStreamEvent(
            delta="live answer",
            event_type="text",
            session_id=self.session_id,
        )
        yield AgentStreamEvent(event_type="session", session_id=self.session_id)


class StaticSessionIndex:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_recent(self, limit: int = 50):
        return self.sessions[:limit]

    def get(self, session_id: str):
        for session in self.sessions:
            if session.session_id == session_id:
                return session
        return None

    def read_transcript(self, session_id: str):
        return []


def _provider(
    tmp_path: Path,
    *,
    engine=None,
    session_index: ClaudeLocalSessionIndex | None = None,
    heartbeat_interval_seconds: float = 15.0,
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
            session_index=session_index or ClaudeLocalSessionIndex(tmp_path / ".claude"),
            heartbeat_interval_seconds=heartbeat_interval_seconds,
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
    engine._config = SimpleNamespace(model="deepseek-v4-pro", effort="max")
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
    assert session.metadata["model"] == "deepseek-v4-pro"
    assert session.metadata["effort"] == "max"
    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.MODEL_TEXT_DELTA,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert events[2].payload["delta"] == "background hello"


@pytest.mark.asyncio
async def test_claude_cli_provider_emits_heartbeat_while_engine_is_silent(
    tmp_path: Path,
) -> None:
    engine = BlockingClaudeEngine()
    provider, _store, _engine, runtime_store = _provider(
        tmp_path,
        engine=engine,
        heartbeat_interval_seconds=0.01,
    )

    result = await provider.start_session(str(tmp_path), "slow review")

    heartbeat = await _wait_for_runtime_event(
        runtime_store,
        result.agent_run_id,
        EventType.AGENT_RUN_HEARTBEAT,
    )
    assert heartbeat.payload["native_thread_id"] == result.native_session_id
    assert heartbeat.payload["native_turn_id"] == result.turn_id
    assert heartbeat.payload["status"] == "running"

    engine.release.set()
    await provider.wait_for_background_tasks()


@pytest.mark.asyncio
async def test_claude_cli_provider_starts_session_and_records_agent_run(
    tmp_path: Path,
) -> None:
    provider, store, engine, _runtime_store = _provider(tmp_path)

    result = await provider.start_session(str(tmp_path), "say hi")

    assert result.provider == "claude"
    assert result.provider_engine == "cli-local"
    UUID(result.native_session_id)
    assert result.agent_run_id > 0
    assert result.status == "started"
    assert result.turn_running is True
    await provider.wait_for_background_tasks()
    assert engine.requests[0].prompt == "say hi"
    assert engine.requests[0].workspace_path == str(tmp_path)
    assert engine.requests[0].extra == {"session_id": result.native_session_id}
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    assert session.metadata["claude_session_id"] == "claude-real-1"


@pytest.mark.asyncio
async def test_claude_cli_provider_marks_silent_cli_run_failed(
    tmp_path: Path,
) -> None:
    provider, store, engine, runtime_store = _provider(
        tmp_path,
        engine=SilentClaudeEngine(),
    )

    result = await provider.start_session(str(tmp_path), "say hi")
    await provider.wait_for_background_tasks()

    assert engine.requests[0].prompt == "say hi"
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "failed"
    assert session.metadata["error"] == (
        "Claude CLI completed without output or session id."
    )
    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.AGENT_RUN_FAILED,
    ]
    assert events[-1].payload["error"] == (
        "Claude CLI completed without output or session id."
    )


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
    UUID(result.native_session_id)
    assert engine.requests == []


@pytest.mark.asyncio
async def test_claude_cli_provider_create_then_continue_starts_without_resume(
    tmp_path: Path,
) -> None:
    engine = FakeClaudeEngine()
    engine._config = SimpleNamespace(model="deepseek-v4-pro", effort="high")
    provider, store, engine, _runtime_store = _provider(tmp_path, engine=engine)
    created = await provider.create_session(str(tmp_path))

    result = await provider.continue_session(created.native_session_id, "first prompt")

    assert result.status == "continued"
    assert result.turn_running is True
    await provider.wait_for_background_tasks()
    assert engine.requests[0].extra == {"session_id": created.native_session_id}
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=created.native_session_id,
    )
    assert session is not None
    assert session.metadata["claude_session_id"] == "claude-real-1"
    assert session.metadata["model"] == "deepseek-v4-pro"
    assert session.metadata["effort"] == "high"


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
async def test_claude_cli_provider_lists_configured_model_and_reasoning(
    tmp_path: Path,
) -> None:
    engine = FakeClaudeEngine()
    engine._config = SimpleNamespace(model="deepseek-v4-pro", effort="max")
    provider, _store, _engine, _runtime_store = _provider(tmp_path, engine=engine)

    models = await provider.list_models()

    assert models == [
        {
            "id": "deepseek-v4-pro",
            "model": "deepseek-v4-pro",
            "displayName": "deepseek-v4-pro",
            "isDefault": True,
            "defaultReasoningEffort": "max",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "轻量"},
                {"reasoningEffort": "medium", "description": "正常"},
                {"reasoningEffort": "high", "description": "深度"},
                {"reasoningEffort": "xhigh", "description": "极深"},
                {"reasoningEffort": "max", "description": "最大"},
            ],
            "serviceTiers": [],
        }
    ]


@pytest.mark.asyncio
async def test_claude_cli_provider_defaults_missing_reasoning_to_max(
    tmp_path: Path,
) -> None:
    engine = FakeClaudeEngine()
    engine._config = SimpleNamespace(model="deepseek-v4-pro")
    provider, _store, _engine, _runtime_store = _provider(tmp_path, engine=engine)

    models = await provider.list_models()

    assert models[0]["defaultReasoningEffort"] == "max"


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
async def test_claude_cli_provider_imports_local_claude_sessions_on_list(
    tmp_path: Path,
) -> None:
    _write_claude_session(
        tmp_path,
        "33333333-3333-4333-8333-333333333333",
        [
            {
                "type": "user",
                "sessionId": "33333333-3333-4333-8333-333333333333",
                "timestamp": "2026-06-03T08:36:15.853Z",
                "cwd": "/repo",
                "entrypoint": "claude-code",
                "version": "2.1.161",
                "message": {"role": "user", "content": "private prompt"},
            }
        ],
    )
    provider, store, _engine, _runtime_store = _provider(tmp_path)

    sessions = await provider.list_sessions(50)

    assert [session.native_session_id for session in sessions] == [
        "33333333-3333-4333-8333-333333333333"
    ]
    imported = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id="33333333-3333-4333-8333-333333333333",
    )
    assert imported is not None
    assert imported.cwd == "/repo"
    assert imported.title == "Claude 33333333"
    assert imported.metadata["claude_session_id"] == (
        "33333333-3333-4333-8333-333333333333"
    )
    assert imported.metadata["entrypoint"] == "claude-code"
    assert "private" not in imported.title


@pytest.mark.asyncio
async def test_claude_cli_provider_refreshes_fallback_title_from_local_session(
    tmp_path: Path,
) -> None:
    session_id = "37373737-3737-4737-8737-373737373737"
    provider, store, _engine, _runtime_store = _provider(
        tmp_path,
        session_index=StaticSessionIndex(
            [
                SimpleNamespace(
                    session_id=session_id,
                    title="Claude history recap",
                    cwd="/repo",
                    created_at="2026-06-03T08:36:15.853Z",
                    updated_at="2026-06-03T08:36:19.298Z",
                    source_path="/repo/session.jsonl",
                    entrypoint="",
                    version="",
                    git_branch="",
                    permission_mode="",
                )
            ]
        ),
    )
    store.get_or_create_session(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=session_id,
        title="Claude 37373737",
        cwd="/repo",
        source_kind="claude_cli_local",
        status="done",
        metadata={"claude_session_id": session_id},
    )

    await provider.list_sessions(50)

    imported = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=session_id,
    )
    assert imported is not None
    assert imported.title == "Claude history recap"


@pytest.mark.asyncio
async def test_claude_cli_provider_shortens_previous_summary_title(
    tmp_path: Path,
) -> None:
    session_id = "38383838-3838-4838-8838-383838383838"
    provider, store, _engine, _runtime_store = _provider(
        tmp_path,
        session_index=StaticSessionIndex(
            [
                SimpleNamespace(
                    session_id=session_id,
                    title="First sentence.",
                    cwd="/repo",
                    created_at="2026-06-03T08:36:15.853Z",
                    updated_at="2026-06-03T08:36:19.298Z",
                    source_path="/repo/session.jsonl",
                    entrypoint="",
                    version="",
                    git_branch="",
                    permission_mode="",
                )
            ]
        ),
    )
    store.get_or_create_session(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=session_id,
        title="First sentence. Second sentence that used to make the UI too long.",
        cwd="/repo",
        source_kind="claude_cli_local",
        status="done",
        metadata={"claude_session_id": session_id},
    )

    await provider.list_sessions(50)

    imported = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=session_id,
    )
    assert imported is not None
    assert imported.title == "First sentence."


@pytest.mark.asyncio
async def test_claude_cli_provider_continues_imported_local_session_with_resume(
    tmp_path: Path,
) -> None:
    _write_claude_session(
        tmp_path,
        "44444444-4444-4444-8444-444444444444",
        [
            {
                "type": "user",
                "sessionId": "44444444-4444-4444-8444-444444444444",
                "timestamp": "2026-06-03T08:36:15.853Z",
                "cwd": "/repo",
                "message": {"role": "user", "content": "first"},
            }
        ],
    )
    provider, _store, engine, _runtime_store = _provider(tmp_path)

    result = await provider.continue_session(
        "44444444-4444-4444-8444-444444444444",
        "continue from phone",
    )
    await provider.wait_for_background_tasks()

    assert result.status == "continued"
    assert engine.requests[0].extra == {
        "resume_session_id": "44444444-4444-4444-8444-444444444444"
    }


@pytest.mark.asyncio
async def test_claude_cli_provider_forwards_permission_mode(
    tmp_path: Path,
) -> None:
    provider, _store, engine, _runtime_store = _provider(tmp_path)

    result = await provider.start_session(
        str(tmp_path),
        "hello",
        permission_mode="never",
    )
    await provider.wait_for_background_tasks()
    assert result.status == "started"
    assert engine.requests[0].extra.get("permission_mode") == "never"

    await provider.continue_session(
        result.native_session_id,
        "continue",
        permission_mode="on_request",
    )
    await provider.wait_for_background_tasks()
    assert engine.requests[1].extra.get("permission_mode") == "on_request"


@pytest.mark.asyncio
async def test_claude_cli_provider_syncs_jsonl_output_after_continue(
    tmp_path: Path,
) -> None:
    session_id = "66666666-6666-4666-8666-666666666666"
    session_path = _write_claude_session(
        tmp_path,
        session_id,
        [
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": session_id,
                "timestamp": "2026-06-03T08:36:15.853Z",
                "cwd": "/repo",
                "message": {"role": "user", "content": "hello"},
            },
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "sessionId": session_id,
                "timestamp": "2026-06-03T08:36:19.298Z",
                "cwd": "/repo",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "world"}],
                },
            },
        ],
    )
    engine = JsonlAppendingClaudeEngine(
        session_id=session_id,
        session_path=session_path,
    )
    provider, _store, _engine, runtime_store = _provider(tmp_path, engine=engine)
    attached = await provider.attach_session(session_id)

    await provider.continue_session(session_id, "next question")
    await provider.wait_for_background_tasks()

    events = runtime_store.list_by_agent_run(attached.agent_run_id)
    assert [
        (event.event_type, event.payload.get("text") or event.payload.get("delta"))
        for event in events
    ] == [
        (EventType.USER_MESSAGE_RECEIVED, "hello"),
        (EventType.MODEL_MESSAGE_COMPLETED, "world"),
        (EventType.AGENT_RUN_STARTED, None),
        (EventType.USER_MESSAGE_RECEIVED, "next question"),
        (EventType.MODEL_MESSAGE_COMPLETED, "jsonl answer"),
        (EventType.AGENT_RUN_COMPLETED, None),
    ]
    assistant_events = [
        event
        for event in events
        if event.event_type == EventType.MODEL_MESSAGE_COMPLETED
    ]
    assert str(assistant_events[0].payload["itemId"]).startswith(
        "jsonl-assistant-final:"
    )


@pytest.mark.asyncio
async def test_claude_cli_provider_replaces_streamed_jsonl_with_completed_block_on_sync(
    tmp_path: Path,
) -> None:
    session_id = "77777777-7777-4777-8777-777777777777"
    session_path = (
        tmp_path / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    )
    engine = JsonlStreamingClaudeEngine(
        session_id=session_id,
        session_path=session_path,
    )
    provider, store, _engine, runtime_store = _provider(tmp_path, engine=engine)

    result = await provider.start_session(str(tmp_path), "live question")
    await provider.wait_for_background_tasks()
    await provider.read_session(result.native_session_id)

    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [
        (event.event_type, event.payload.get("text") or event.payload.get("delta"))
        for event in events
    ] == [
        (EventType.AGENT_RUN_STARTED, None),
        (EventType.USER_MESSAGE_RECEIVED, "live question"),
        (EventType.MODEL_TEXT_DELTA, "live answer"),
        (EventType.MODEL_MESSAGE_COMPLETED, "live answer"),
        (EventType.AGENT_RUN_COMPLETED, None),
    ]
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.metadata["claude_synced_message_count"] == 2


@pytest.mark.asyncio
async def test_claude_cli_provider_syncs_selected_local_transcript_once(
    tmp_path: Path,
) -> None:
    _write_claude_session(
        tmp_path,
        "55555555-5555-4555-8555-555555555555",
        [
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": "55555555-5555-4555-8555-555555555555",
                "timestamp": "2026-06-03T08:36:15.853Z",
                "cwd": "/repo",
                "message": {"role": "user", "content": "hello"},
            },
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "sessionId": "55555555-5555-4555-8555-555555555555",
                "timestamp": "2026-06-03T08:36:19.298Z",
                "cwd": "/repo",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "world"}],
                },
            },
        ],
    )
    provider, _store, _engine, runtime_store = _provider(tmp_path)

    attached = await provider.attach_session(
        "55555555-5555-4555-8555-555555555555"
    )
    await provider.sync_session("55555555-5555-4555-8555-555555555555")

    events = runtime_store.list_by_agent_run(attached.agent_run_id)
    assert [(event.event_type, event.payload.get("text") or event.payload.get("delta")) for event in events] == [
        (EventType.USER_MESSAGE_RECEIVED, "hello"),
        (EventType.MODEL_MESSAGE_COMPLETED, "world"),
    ]


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


def _write_claude_session(
    root: Path,
    session_id: str,
    rows: list[dict],
) -> Path:
    path = root / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


async def _wait_for_runtime_event(
    runtime_store: RuntimeEventStore,
    agent_run_id: int,
    event_type: str,
    *,
    timeout: float = 0.5,
):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for event in runtime_store.list_by_agent_run(agent_run_id):
            if event.event_type == event_type:
                return event
        await asyncio.sleep(0.005)
    raise AssertionError(f"Timed out waiting for {event_type}")
