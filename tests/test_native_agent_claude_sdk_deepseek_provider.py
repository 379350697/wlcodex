from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.claude_sdk_deepseek_provider import (
    ClaudeSdkDeepSeekConfig,
    ClaudeSdkDeepSeekProvider,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


class FakeSdkRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str = "",
    ):
        self.calls.append(
            (prompt, cwd, session_id, config.base_url, config.model, api_key, config.effort)
        )
        if self.fail:
            raise RuntimeError("sdk failed")
        yield {"type": "assistant", "text": "done"}


def _provider(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    runner: FakeSdkRunner | None = None,
    config: ClaudeSdkDeepSeekConfig | None = None,
) -> tuple[
    ClaudeSdkDeepSeekProvider,
    object,
    NativeAgentSessionStore,
    RuntimeEventStore,
]:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = NativeAgentSessionStore(ledger)
    runtime_store = RuntimeEventStore(ledger._conn)
    fake_runner = runner or FakeSdkRunner()
    provider = ClaudeSdkDeepSeekProvider(
        config=config
        or ClaudeSdkDeepSeekConfig(
            api_key_env="DEEPSEEK_API_KEY",
            ccswitch_fallback_enabled=False,
        ),
        session_store=store,
        runtime_store=runtime_store,
        runner=fake_runner,
        env={"DEEPSEEK_API_KEY": "sk-test"} if env is None else env,
    )
    return provider, fake_runner, store, runtime_store


@pytest.mark.asyncio
async def test_status_reports_missing_api_key(tmp_path: Path) -> None:
    provider, _runner, _store, _runtime_store = _provider(tmp_path, env={})

    status = await provider.status()

    assert status.connected is False
    assert status.status_code == "missing_api_key"


@pytest.mark.asyncio
async def test_start_session_uses_deepseek_anthropic_endpoint(tmp_path: Path) -> None:
    provider, runner, store, _runtime_store = _provider(tmp_path)

    result = await provider.start_session(str(tmp_path), "fix tests")
    await provider.wait_for_background_tasks()

    assert result.provider == "claude"
    assert result.provider_engine == "sdk-deepseek"
    assert result.status == "started"
    assert runner.calls[0][0] == "fix tests"
    assert runner.calls[0][1] == str(tmp_path)
    assert runner.calls[0][3] == "https://api.deepseek.com/anthropic"
    assert runner.calls[0][4] == "deepseek-v4-pro"
    assert runner.calls[0][5] == "sk-test"
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    assert session.metadata["model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_continue_session_uses_existing_sdk_session_id(
    tmp_path: Path,
) -> None:
    provider, runner, _store, _runtime_store = _provider(tmp_path)
    created = await provider.create_session(str(tmp_path))

    result = await provider.continue_session(
        created.native_session_id,
        "continue",
        model="deepseek-v4-flash",
        effort="high",
    )
    await provider.wait_for_background_tasks()

    assert result.status == "continued"
    assert runner.calls[0][2] == created.native_session_id
    assert runner.calls[0][4] == "deepseek-v4-flash"
    assert runner.calls[0][6] == "high"


@pytest.mark.asyncio
async def test_failed_run_marks_session_failed(tmp_path: Path) -> None:
    runner = FakeSdkRunner(fail=True)
    provider, _runner, store, _runtime_store = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "fail")
    await provider.wait_for_background_tasks()

    assert result.status == "started"
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "failed"
    assert session.metadata["error"] == "sdk failed"


def test_capabilities_do_not_expose_second_claude_provider(tmp_path: Path) -> None:
    provider, _runner, _store, _runtime_store = _provider(tmp_path)

    assert provider.provider == "claude"
    assert provider.provider_engine == "sdk-deepseek"
    assert provider.capabilities().can_start_session is True
    assert provider.capabilities().can_steer_active_turn is False


@pytest.mark.asyncio
async def test_list_models_exposes_deepseek_reasoning_levels(tmp_path: Path) -> None:
    provider, _runner, _store, _runtime_store = _provider(tmp_path)

    models = await provider.list_models()

    assert models == [
        {
            "id": "deepseek-v4-pro",
            "model": "deepseek-v4-pro",
            "displayName": "deepseek-v4-pro",
            "defaultReasoningEffort": "xhigh",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "轻量"},
                {"reasoningEffort": "medium", "description": "正常"},
                {"reasoningEffort": "high", "description": "深度"},
                {"reasoningEffort": "xhigh", "description": "极深"},
            ],
            "serviceTiers": [],
        }
    ]


def _ccswitch_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE providers (
            id TEXT NOT NULL,
            app_type TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_config TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}',
            is_current BOOLEAN NOT NULL DEFAULT 0,
            PRIMARY KEY (id, app_type)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO providers (
            id, app_type, name, settings_config, meta, is_current
        )
        VALUES (?, ?, ?, ?, '{}', 1)
        """,
        (
            "deepseek-current",
            "claude-desktop",
            "DeepSeek",
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                        "ANTHROPIC_AUTH_TOKEN": "sk-ccswitch",
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_start_session_falls_back_to_ccswitch_deepseek_key(
    tmp_path: Path,
) -> None:
    ccswitch_db = tmp_path / "cc-switch.db"
    _ccswitch_db(ccswitch_db)
    provider, runner, store, _runtime_store = _provider(
        tmp_path,
        env={},
        config=ClaudeSdkDeepSeekConfig(
            api_key_env="DEEPSEEK_API_KEY",
            ccswitch_db_path=str(ccswitch_db),
        ),
    )

    status = await provider.status()
    result = await provider.start_session(str(tmp_path), "use ccswitch")
    await provider.wait_for_background_tasks()

    assert status.connected is True
    assert status.metadata["auth_source"] == "ccswitch"
    assert result.status == "started"
    assert runner.calls[0][5] == "sk-ccswitch"
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.metadata["auth_source"] == "ccswitch"
    assert "api_key" not in session.metadata


class BlockingSdkRunner:
    def __init__(self) -> None:
        self.calls = []
        self.release = asyncio.Event()

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
        api_key: str = "",
    ):
        self.calls.append((prompt, cwd, session_id, config.model, api_key))
        await self.release.wait()
        yield {"type": "assistant", "text": "background done"}


@pytest.mark.asyncio
async def test_start_session_returns_before_sdk_runner_finishes_and_streams_events(
    tmp_path: Path,
) -> None:
    runner = BlockingSdkRunner()
    provider, _runner, _store, runtime_store = _provider(tmp_path, runner=runner)

    result = await asyncio.wait_for(
        provider.start_session(str(tmp_path), "stream this"),
        timeout=0.05,
    )

    assert result.status == "started"
    assert result.turn_running is True
    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
    ]
    assert events[1].payload["text"] == "stream this"

    runner.release.set()
    await provider.wait_for_background_tasks()

    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.MODEL_TEXT_DELTA,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert events[2].payload["delta"] == "background done"
