from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.claude_sdk_deepseek_provider import (
    ClaudeAgentSdkRunner,
    ClaudeSdkDeepSeekConfig,
    ClaudeSdkDeepSeekProvider,
    _allow_tool,
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
        resume_session_id: str = "",
        config: ClaudeSdkDeepSeekConfig,
        api_key: str = "",
    ):
        self.calls.append(
            (
                prompt,
                cwd,
                session_id,
                config.base_url,
                config.model,
                api_key,
                config.effort,
                config.permission_mode,
                config.system_prompt,
                config.cli_path,
                resume_session_id,
            )
        )
        if self.fail:
            raise RuntimeError("sdk failed")
        yield {"type": "assistant", "text": "done"}

    async def interrupt(self, *, session_id: str, turn_id: str) -> bool:
        return False


def _permission_behavior(result: object) -> str:
    if isinstance(result, dict):
        return str(result.get("behavior") or "")
    return str(getattr(result, "behavior", ""))


def _permission_message(result: object) -> str:
    if isinstance(result, dict):
        return str(result.get("message") or "")
    return str(getattr(result, "message", ""))


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
    assert runner.calls[0][10] == ""
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
    provider, runner, store, _runtime_store = _provider(tmp_path)
    created = await provider.create_session(str(tmp_path))
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=created.native_session_id,
    )
    assert session is not None
    store.update_session(
        session.id,
        metadata={**session.metadata, "claude_session_id": "sdk-session-real"},
    )

    result = await provider.continue_session(
        created.native_session_id,
        "continue",
        model="deepseek-v4-flash",
        effort="high",
    )
    await provider.wait_for_background_tasks()

    assert result.status == "continued"
    assert runner.calls[-1][2] == created.native_session_id
    assert runner.calls[-1][4] == "deepseek-v4-flash"
    assert runner.calls[-1][6] == "high"
    assert runner.calls[-1][10] == "sdk-session-real"


@pytest.mark.asyncio
async def test_continue_session_preserves_claude_session_metadata(tmp_path: Path) -> None:
    provider, _runner, store, _runtime_store = _provider(tmp_path)
    created = await provider.create_session(str(tmp_path))
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=created.native_session_id,
    )
    assert session is not None
    store.update_session(
        session.id,
        metadata={**session.metadata, "claude_session_id": "sdk-session-real"},
    )

    await provider.continue_session(created.native_session_id, "continue")
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=created.native_session_id,
    )
    assert session is not None
    assert session.metadata["claude_session_id"] == "sdk-session-real"


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
    assert provider.capabilities().can_interrupt is True
    assert "can_interrupt" not in provider.capabilities().disabled_reasons


@pytest.mark.asyncio
async def test_can_use_tool_uses_local_development_allowlist() -> None:
    assert _permission_behavior(await _allow_tool("Read", {}, None)) == "allow"
    assert _permission_behavior(await _allow_tool("Bash", {}, None)) == "allow"

    web_fetch = await _allow_tool("WebFetch", {"url": "https://example.com"}, None)
    assert _permission_behavior(web_fetch) == "deny"
    assert "not enabled" in _permission_message(web_fetch)

    unknown = await _allow_tool("mcp__unknown__tool", {}, None)
    assert _permission_behavior(unknown) == "deny"
    assert "not enabled" in _permission_message(unknown)


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
        self.interrupt_calls = []

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        resume_session_id: str = "",
        config: ClaudeSdkDeepSeekConfig,
        api_key: str = "",
    ):
        self.calls.append((prompt, cwd, session_id, config.model, api_key))
        await self.release.wait()
        yield {"type": "assistant", "text": "background done"}

    async def interrupt(self, *, session_id: str, turn_id: str) -> bool:
        self.interrupt_calls.append((session_id, turn_id))
        self.release.set()
        return True


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
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
        EventType.PROVIDER_DISPLAY_COMPLETED,
        EventType.MODEL_MESSAGE_COMPLETED,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert events[2].payload["raw_frame_id"] > 0
    assert events[3].payload["delta"] == "background done"
    assert events[4].payload["delta"] == "background done"
    assert events[5].payload["text"] == "background done"
    assert events[6].payload["text"] == "background done"


@pytest.mark.asyncio
async def test_interrupt_session_calls_active_sdk_runner_and_marks_interrupted(
    tmp_path: Path,
) -> None:
    runner = BlockingSdkRunner()
    provider, _runner, store, runtime_store = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "long run")
    interrupted = await provider.interrupt_session(
        result.native_session_id,
        turn_id=result.turn_id,
    )
    await provider.wait_for_background_tasks()

    assert interrupted.status == "interrupted"
    assert runner.interrupt_calls == [(result.native_session_id, result.turn_id)]
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "interrupted"
    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert events[-1].event_type == EventType.AGENT_RUN_FAILED
    assert events[-1].payload["error"] == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_session_stays_interrupted_when_sdk_raises_after_interrupt(
    tmp_path: Path,
) -> None:
    class RaisingAfterInterruptRunner(BlockingSdkRunner):
        async def run(self, **kwargs):
            self.calls.append(kwargs)
            await self.release.wait()
            raise RuntimeError("cancelled by sdk")
            yield {"type": "assistant", "text": "unreachable"}

    runner = RaisingAfterInterruptRunner()
    provider, _runner, store, runtime_store = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "long run")
    await provider.interrupt_session(result.native_session_id, turn_id=result.turn_id)
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "interrupted"
    assert session.metadata["error"] == "interrupted"
    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert events[-1].payload["error"] == "interrupted"


@pytest.mark.asyncio
async def test_interrupt_session_cancels_turn_when_sdk_interrupt_raises(
    tmp_path: Path,
) -> None:
    class InterruptRaisesRunner(BlockingSdkRunner):
        async def interrupt(self, *, session_id: str, turn_id: str) -> bool:
            self.interrupt_calls.append((session_id, turn_id))
            raise RuntimeError("sdk interrupt failed")

    runner = InterruptRaisesRunner()
    provider, _runner, store, runtime_store = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "long run")
    interrupted = await provider.interrupt_session(
        result.native_session_id,
        turn_id=result.turn_id,
    )
    await provider.wait_for_background_tasks()

    assert interrupted.status == "interrupted"
    assert runner.interrupt_calls == [(result.native_session_id, result.turn_id)]
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "interrupted"
    assert session.metadata["error"] == "interrupted"
    assert session.metadata["interrupt_error"] == "RuntimeError"
    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert events[-1].event_type == EventType.AGENT_RUN_FAILED
    assert events[-1].payload["error"] == "interrupted"


@pytest.mark.asyncio
async def test_structured_sdk_messages_emit_text_and_persist_diagnostics(
    tmp_path: Path,
) -> None:
    class StructuredRunner(FakeSdkRunner):
        async def run(self, **kwargs):
            self.calls.append(kwargs)
            yield {
                "type": "assistant",
                "content": [{"type": "text", "text": "hello "}],
            }
            yield {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ],
            }
            yield {
                "type": "result",
                "session_id": "sdk-session-123",
                "usage": {"input_tokens": 3, "output_tokens": 5},
            }

    provider, _runner, store, runtime_store = _provider(
        tmp_path,
        runner=StructuredRunner(),
    )

    result = await provider.start_session(str(tmp_path), "inspect")
    await provider.wait_for_background_tasks()

    events = runtime_store.list_by_agent_run(result.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_SEMANTIC_TOOL_CALL_STARTED,
        EventType.TOOL_CALL_STARTED,
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_SEMANTIC_USAGE_UPDATED,
        EventType.MODEL_USAGE_UPDATED,
        EventType.PROVIDER_DISPLAY_COMPLETED,
        EventType.MODEL_MESSAGE_COMPLETED,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert events[3].payload["delta"] == "hello "
    assert events[6].payload["tool_name"] == "Read"
    assert events[9].payload["usage"] == {"input_tokens": 3, "output_tokens": 5}
    assert events[11].payload["text"] == "hello "
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.metadata["claude_session_id"] == "sdk-session-123"
    assert session.metadata["assistant_text"] == "hello "
    assert session.metadata["usage"] == {"input_tokens": 3, "output_tokens": 5}
    assert session.metadata["tool_events"] == [
        {"id": "tool-1", "name": "Read", "status": "started"}
    ]


@pytest.mark.asyncio
async def test_sdk_object_blocks_emit_incremental_text_and_tools(
    tmp_path: Path,
) -> None:
    class ObjectBlockRunner(FakeSdkRunner):
        async def run(self, **kwargs):
            self.calls.append(kwargs)
            yield SimpleNamespace(
                type="assistant",
                content=[SimpleNamespace(type="text", text="hel")],
            )
            yield SimpleNamespace(
                type="assistant",
                content=[SimpleNamespace(type="text", text="hello")],
            )
            yield SimpleNamespace(
                type="assistant",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="tool-obj",
                        name="Read",
                        input={"file_path": "README.md"},
                    )
                ],
            )
            yield SimpleNamespace(
                type="result",
                session_id="sdk-object-session",
                usage={"input_tokens": 1, "output_tokens": 2},
            )

    provider, _runner, store, runtime_store = _provider(
        tmp_path,
        runner=ObjectBlockRunner(),
    )

    result = await provider.start_session(str(tmp_path), "inspect object blocks")
    await provider.wait_for_background_tasks()

    events = runtime_store.list_by_agent_run(result.agent_run_id)
    text_events = [
        event.payload["delta"]
        for event in events
        if event.event_type == EventType.MODEL_TEXT_DELTA
    ]
    assert text_events == ["hel", "lo"]
    assert any(
        event.event_type == EventType.TOOL_CALL_STARTED
        and event.payload["tool_id"] == "tool-obj"
        and event.payload["tool_name"] == "Read"
        for event in events
    )
    completed = [
        event.payload["text"]
        for event in events
        if event.event_type == EventType.MODEL_MESSAGE_COMPLETED
    ]
    assert completed == ["hello"]
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.metadata["assistant_text"] == "hello"


@pytest.mark.asyncio
async def test_agent_sdk_runner_uses_claude_sdk_client(monkeypatch, tmp_path: Path) -> None:
    created_clients = []

    class FakeClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.connected_prompt_stream = None
            self.interrupted = False
            created_clients.append(self)

        async def connect(self, prompt_stream):
            self.connected_prompt_stream = prompt_stream

        async def receive_response(self):
            prompts = []
            async for message in self.connected_prompt_stream:
                prompts.append(message)
            yield {"type": "assistant", "text": prompts[0]}

        async def interrupt(self):
            self.interrupted = True

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeAgentOptions,
            ClaudeSDKClient=FakeClaudeSDKClient,
        ),
    )
    runner = ClaudeAgentSdkRunner()
    config = ClaudeSdkDeepSeekConfig(
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-pro",
        effort="high",
        permission_mode="acceptEdits",
        system_prompt="system text",
        cli_path="/tmp/claude",
        ccswitch_fallback_enabled=False,
    )

    messages = [
        message
        async for message in runner.run(
            prompt="hello",
            cwd=str(tmp_path),
            session_id="existing-session",
            resume_session_id="sdk-session-real",
            config=config,
            api_key="sk-test",
        )
    ]

    assert messages == [
        {
            "type": "assistant",
            "text": {
                "type": "user",
                "message": {"role": "user", "content": "hello"},
                "parent_tool_use_id": None,
                "session_id": "sdk-session-real",
            },
        }
    ]
    assert len(created_clients) == 1
    options = created_clients[0].options.kwargs
    assert options["cwd"] == str(tmp_path)
    assert options["model"] == "deepseek-v4-pro"
    assert options["effort"] == "high"
    assert options["permission_mode"] == "acceptEdits"
    assert options["system_prompt"] == "system text"
    assert options["cli_path"] == "/tmp/claude"
    assert options["resume"] == "sdk-session-real"
    assert options["include_partial_messages"] is True
    assert options["include_hook_events"] is True
    assert callable(options["can_use_tool"])


@pytest.mark.asyncio
async def test_agent_sdk_runner_does_not_resume_new_native_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    created_options = []

    class FakeClaudeSDKClient:
        def __init__(self, options):
            created_options.append(options)

        async def connect(self, prompt_stream):
            async for _message in prompt_stream:
                pass

        async def receive_response(self):
            if False:
                yield {}

        async def disconnect(self):
            pass

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeAgentOptions,
            ClaudeSDKClient=FakeClaudeSDKClient,
        ),
    )

    runner = ClaudeAgentSdkRunner()
    _messages = [
        message
        async for message in runner.run(
            prompt="hello",
            cwd=str(tmp_path),
            session_id="native-id",
            resume_session_id="",
            config=ClaudeSdkDeepSeekConfig(ccswitch_fallback_enabled=False),
            api_key="sk-test",
        )
    ]

    assert created_options[0].kwargs["resume"] is None
