from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.antigravity_provider import (
    AntigravitySdkProvider,
    AntigravitySdkRunner,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


class FakeAntigravityRunner:
    available = True
    error = ""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        conversation_id: str = "",
        save_dir: str = "",
        app_data_dir: str = "",
        model: str = "",
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "session_id": session_id,
                "conversation_id": conversation_id,
                "save_dir": save_dir,
                "app_data_dir": app_data_dir,
                "model": model,
            }
        )
        if self.fail:
            raise RuntimeError("antigravity failed")
        yield {
            "type": "assistant",
            "text": "done",
            "conversation_id": conversation_id or f"ag-{session_id}",
        }


class BlockingAntigravityRunner:
    available = True
    error = ""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = []

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        conversation_id: str = "",
        save_dir: str = "",
        app_data_dir: str = "",
        model: str = "",
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "session_id": session_id,
                "conversation_id": conversation_id,
                "save_dir": save_dir,
                "app_data_dir": app_data_dir,
                "model": model,
            }
        )
        await self.release.wait()
        yield {
            "type": "assistant",
            "text": "background done",
            "conversation_id": conversation_id or f"ag-{session_id}",
        }


def _provider(
    tmp_path: Path,
    *,
    runner=None,
) -> tuple[
    AntigravitySdkProvider,
    NativeAgentSessionStore,
    RuntimeEventStore,
    object,
]:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = NativeAgentSessionStore(ledger)
    runtime_store = RuntimeEventStore(ledger._conn)
    fake_runner = runner or FakeAntigravityRunner()
    return (
        AntigravitySdkProvider(
            session_store=store,
            runner=fake_runner,
            runtime_store=runtime_store,
        ),
        store,
        runtime_store,
        fake_runner,
    )


@pytest.mark.asyncio
async def test_status_reports_sdk_not_installed(tmp_path: Path) -> None:
    class MissingRunner:
        available = False
        error = "No module named google.antigravity"

    provider, _store, _runtime_store, _runner = _provider(
        tmp_path,
        runner=MissingRunner(),
    )

    status = await provider.status()

    assert status.connected is False
    assert status.status_code == "sdk_not_installed"
    assert "google.antigravity" in status.message


@pytest.mark.asyncio
async def test_start_session_returns_before_sdk_runner_finishes_and_streams_events(
    tmp_path: Path,
) -> None:
    runner = BlockingAntigravityRunner()
    provider, store, runtime_store, _runner = _provider(tmp_path, runner=runner)

    result = await asyncio.wait_for(
        provider.start_session(str(tmp_path), "fix it"),
        timeout=0.05,
    )

    assert result.status == "started"
    assert result.turn_running is True
    assert result.turn_id
    assert result.active_turn_id == result.turn_id
    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="sdk",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "running"
    assert session.last_turn_id == result.turn_id
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
    ]
    assert events[0].payload["native_turn_id"] == result.turn_id
    assert events[1].payload["text"] == "fix it"

    await asyncio.sleep(0)
    assert runner.calls[0]["prompt"] == "fix it"
    assert runner.calls[0]["cwd"] == str(tmp_path)
    assert runner.calls[0]["session_id"] == result.native_session_id
    runner.release.set()
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="sdk",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.MODEL_TEXT_DELTA,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert events[2].payload["delta"] == "background done"


@pytest.mark.asyncio
async def test_start_session_uses_sdk_runner(tmp_path: Path) -> None:
    runner = FakeAntigravityRunner()
    provider, store, _runtime_store, _runner = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "fix it")

    assert result.provider == "antigravity"
    assert result.provider_engine == "sdk"
    assert result.status == "started"
    await provider.wait_for_background_tasks()
    assert runner.calls[0]["prompt"] == "fix it"
    assert runner.calls[0]["cwd"] == str(tmp_path)
    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="sdk",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"


@pytest.mark.asyncio
async def test_create_then_continue_uses_existing_session_id(tmp_path: Path) -> None:
    runner = FakeAntigravityRunner()
    provider, _store, _runtime_store, _runner = _provider(tmp_path, runner=runner)
    created = await provider.create_session(str(tmp_path))

    result = await provider.continue_session(created.native_session_id, "continue")

    assert result.status == "continued"
    await provider.wait_for_background_tasks()
    assert runner.calls[0]["prompt"] == "continue"
    assert runner.calls[0]["cwd"] == str(tmp_path)
    assert runner.calls[0]["session_id"] == created.native_session_id


@pytest.mark.asyncio
async def test_failed_run_marks_session_failed(tmp_path: Path) -> None:
    runner = FakeAntigravityRunner(fail=True)
    provider, store, runtime_store, _runner = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "fail")

    assert result.status == "started"
    await provider.wait_for_background_tasks()
    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="sdk",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "failed"
    assert session.metadata["error"] == "antigravity failed"
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert events[-1].event_type == EventType.AGENT_RUN_FAILED
    assert events[-1].payload["error"] == "antigravity failed"


def test_capabilities_expose_antigravity_sdk_provider(tmp_path: Path) -> None:
    provider, _store, _runtime_store, _runner = _provider(tmp_path)

    assert provider.provider == "antigravity"
    assert provider.provider_engine == "sdk"
    assert provider.capabilities().can_start_session is True
    assert provider.capabilities().can_list_models is True
    assert provider.capabilities().can_steer_active_turn is False


@pytest.mark.asyncio
async def test_sdk_provider_lists_antigravity_model_catalog(tmp_path: Path) -> None:
    provider, _store, _runtime_store, _runner = _provider(tmp_path)

    models = await provider.list_models()

    assert models[0] == {
        "id": "Claude Opus 4.6 (Thinking)",
        "model": "Claude Opus 4.6 (Thinking)",
        "displayName": "Claude Opus 4.6 (Thinking)",
        "isDefault": True,
    }
    assert {model["model"] for model in models} == {
        "Claude Opus 4.6 (Thinking)",
        "Claude Sonnet 4.6 (Thinking)",
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (High)",
        "Gemini 3.5 Flash (Low)",
        "Gemini 3.1 Pro (High)",
        "Gemini 3.1 Pro (Low)",
        "GPT-OSS 120B (Medium)",
    }


def test_antigravity_runtime_event_source_exists() -> None:
    from wlcodex.runtime_events import EventSource

    assert EventSource.ANTIGRAVITY == "antigravity"


@pytest.mark.asyncio
async def test_sdk_runner_passes_workspace_to_local_agent_config(tmp_path: Path) -> None:
    class FakeConfig:
        calls = []

        def __init__(self, **kwargs):
            self.calls.append(kwargs)

    class FakeResponse:
        async def text(self):
            return "done"

    class FakeAgent:
        def __init__(self, config):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def chat(self, prompt):
            return FakeResponse()

    runner = AntigravitySdkRunner.__new__(AntigravitySdkRunner)
    runner.available = True
    runner.error = ""
    runner._agent_cls = FakeAgent
    runner._config_cls = FakeConfig

    events = [
        event
        async for event in runner.run(
            prompt="list files",
            cwd=str(tmp_path),
            session_id="ag-1",
        )
    ]

    assert FakeConfig.calls == [{"workspaces": [str(tmp_path)]}]
    assert events[0]["text"] == "done"
