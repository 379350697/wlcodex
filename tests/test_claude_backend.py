"""Tests for Claude backend."""

from pathlib import Path
import pytest

from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
from wlcodex.claude_permissions import ClaudePermissionState
from wlcodex.agent_backend import AgentRequest


def test_claude_disabled_reports_unavailable() -> None:
    config = ClaudeConfig(enabled=False)
    backend = ClaudeBackend(config)
    assert not backend.enabled


@pytest.mark.asyncio
async def test_claude_send_when_disabled() -> None:
    config = ClaudeConfig(enabled=False)
    backend = ClaudeBackend(config)
    result = await backend.send(AgentRequest(prompt="test"))
    assert "not enabled" in result.text.lower()
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_claude_streaming_when_disabled() -> None:
    config = ClaudeConfig(enabled=False)
    backend = ClaudeBackend(config)
    events = []
    async for event in backend.send_streaming(AgentRequest(prompt="test")):
        events.append(event)
    assert len(events) == 1
    assert "not enabled" in events[0].delta.lower()


def test_claude_health_when_disabled() -> None:
    config = ClaudeConfig(enabled=False)
    backend = ClaudeBackend(config)
    health = backend.health()
    assert health.is_healthy
    assert not health.enabled


def test_claude_health_when_enabled_but_no_binary() -> None:
    config = ClaudeConfig(enabled=True, binary="/nonexistent/claude/binary")
    backend = ClaudeBackend(config)
    health = backend.health()
    # Not healthy if enabled but binary doesn't exist
    assert not health.is_healthy


def test_claude_config_defaults() -> None:
    config = ClaudeConfig()
    assert config.enabled is False
    assert config.binary == "claude"
    assert config.startup_timeout_seconds == 15.0
    assert config.request_timeout_seconds == 3600.0
    assert config.stream_idle_timeout_seconds == 600.0
    assert config.stream_drain_grace_seconds == 0.1
    assert config.permission_mode == "acceptEdits"
    assert config.model == "deepseek-v4-pro"
    assert config.effort == "max"


@pytest.mark.asyncio
async def test_claude_send_with_fake_echo(tmp_path: Path) -> None:
    """Test Claude backend with a simple echo command as fake binary."""
    echo_script = tmp_path / "fake-claude"
    echo_script.write_text("#!/bin/sh\necho 'Fake Claude output'\n")
    echo_script.chmod(0o755)

    config = ClaudeConfig(enabled=True, binary=str(echo_script))
    backend = ClaudeBackend(config)
    result = await backend.send(AgentRequest(
        prompt="hello",
        workspace_path=str(tmp_path),
    ))
    assert "Fake Claude output" in result.text
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_claude_send_passes_current_permission_mode(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    permission_state = ClaudePermissionState("plan")
    backend = ClaudeBackend(
        ClaudeConfig(enabled=True, binary=str(fake_claude), permission_mode="acceptEdits"),
        permission_state=permission_state,
    )
    permission_state.set("允许编辑")

    result = await backend.send(AgentRequest(
        prompt="hello",
        workspace_path=str(tmp_path),
    ))

    assert result.exit_code == 0
    assert "--permission-mode" in result.text
    assert "acceptEdits" in result.text
    assert "--model" in result.text
    assert "deepseek-v4-pro" in result.text
    assert "--effort" in result.text
    assert "max" in result.text


@pytest.mark.asyncio
async def test_claude_send_normalizes_deepseek4pro_alias_for_cli(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            model="deepseek4pro",
            effort="max",
        )
    )

    result = await backend.send(AgentRequest(
        prompt="hello",
        workspace_path=str(tmp_path),
    ))

    assert result.exit_code == 0
    assert "--model" in result.text
    assert "deepseek-v4-pro" in result.text
    assert "deepseek4pro" not in result.text
    assert "--effort" in result.text
    assert "max" in result.text


@pytest.mark.asyncio
async def test_claude_streaming_turns_permission_request_into_error(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/bin/sh\nprintf '需要你的批准来编辑 router.py。我已经准备好了修改方案，等待你确认。\\n'\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(ClaudeConfig(enabled=True, binary=str(fake_claude)))

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="fix router",
            workspace_path=str(tmp_path),
        ))
    ]

    assert events
    assert events[-1].event_type == "error"
    assert "权限" in events[-1].delta or "批准" in events[-1].delta


@pytest.mark.asyncio
async def test_claude_streaming_allows_generic_confirmation_progress_text(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': "
        "{'type': 'text_delta', 'text': '实现完成前还需要确认边界值。'}}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(ClaudeConfig(enabled=True, binary=str(fake_claude)))

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="fix router",
            workspace_path=str(tmp_path),
        ))
    ]

    assert [event.event_type for event in events] == ["text"]
    assert events[0].delta == "实现完成前还需要确认边界值。"


@pytest.mark.asyncio
async def test_claude_streaming_times_out(tmp_path: Path) -> None:
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nsleep 0.2\nprintf 'late\\n'\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=0.05,
        )
    )

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="slow task",
            workspace_path=str(tmp_path),
        ))
    ]

    assert events
    assert events[-1].event_type == "error"
    assert "超时" in events[-1].delta or "timed out" in events[-1].delta.lower()


@pytest.mark.asyncio
async def test_claude_streaming_uses_stream_json_and_refreshes_idle_timeout_on_events(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake-claude"
    args_file = tmp_path / "args.txt"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        f"pathlib.Path({str(args_file)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': "
        "{'type': 'text_delta', 'text': 'first'}}}), flush=True)\n"
        "time.sleep(0.04)\n"
        "print(json.dumps({'type': 'system', 'subtype': 'api_retry', 'message': 'retrying'}), flush=True)\n"
        "time.sleep(0.04)\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': "
        "{'type': 'text_delta', 'text': ' second'}}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=0.3,
            stream_idle_timeout_seconds=0.06,
        )
    )

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="slow but active task",
            workspace_path=str(tmp_path),
        ))
    ]

    assert [event.delta for event in events if event.event_type == "text"] == [
        "first",
        " second",
    ]
    assert all(event.event_type != "error" for event in events)
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--verbose" in args
    assert "--include-partial-messages" in args


@pytest.mark.asyncio
async def test_claude_streaming_idle_timeout_errors_when_output_goes_quiet(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/bin/sh\nsleep 0.2\nprintf 'late\\n'\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=0.5,
            stream_idle_timeout_seconds=0.03,
        )
    )

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="quiet task",
            workspace_path=str(tmp_path),
        ))
    ]

    assert events
    assert events[-1].event_type == "error"
    assert "没有新的输出" in events[-1].delta


@pytest.mark.asyncio
async def test_claude_streaming_accepts_long_stream_json_lines(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake-claude"
    long_text = "x" * 70000
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"text = {long_text!r}\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': "
        "{'type': 'text_delta', 'text': text}}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=0.5,
            stream_idle_timeout_seconds=0.2,
        )
    )

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="long json line",
            workspace_path=str(tmp_path),
        ))
    ]

    assert [event.event_type for event in events] == ["text"]
    assert events[0].delta == long_text


@pytest.mark.asyncio
async def test_claude_streaming_finishes_when_child_keeps_stdout_open(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake-claude"
    pid_file = tmp_path / "held-stdout.pid"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "print('implemented', flush=True)\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(1)\n"
        "    os._exit(0)\n"
        f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(pid))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=0.2,
        )
    )

    try:
        events = [
            event
            async for event in backend.send_streaming(AgentRequest(
                prompt="implement feature",
                workspace_path=str(tmp_path),
            ))
        ]
    finally:
        if pid_file.exists():
            import os
            import signal

            try:
                os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    assert [event.delta for event in events] == ["implemented\n"]
    assert all(event.event_type == "text" for event in events)


# --- Claude usage extraction and recording ---


def test_extract_claude_usage_from_result_with_usage() -> None:
    """Extract usage from a Claude stream-json result event."""
    from wlcodex.claude_backend import extract_claude_usage_from_result

    usage = extract_claude_usage_from_result({
        "type": "result",
        "subtype": "success",
        "usage": {
            "input_tokens": 2500,
            "output_tokens": 1800,
        },
    })

    assert usage is not None
    assert usage["source"] == "exact"
    assert usage["input_tokens"] == 2500
    assert usage["output_tokens"] == 1800
    assert usage["total_tokens"] == 4300


def test_extract_claude_usage_from_result_with_cached_tokens() -> None:
    """Extract usage with cache tokens from result."""
    from wlcodex.claude_backend import extract_claude_usage_from_result

    usage = extract_claude_usage_from_result({
        "type": "result",
        "subtype": "success",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 200,
        },
    })

    assert usage is not None
    assert usage["cached_input_tokens"] == 3200  # 3000 + 200


def test_extract_claude_usage_from_result_without_usage() -> None:
    """Returns None when no usage field present."""
    from wlcodex.claude_backend import extract_claude_usage_from_result

    usage = extract_claude_usage_from_result({
        "type": "result",
        "subtype": "success",
        "result": "all good",
    })

    assert usage is None


def test_extract_claude_usage_from_result_skips_non_numeric_usage() -> None:
    """Usage with non-dict fields returns None."""
    from wlcodex.claude_backend import extract_claude_usage_from_result

    usage = extract_claude_usage_from_result({
        "type": "result",
        "usage": "some string",
    })

    assert usage is None


def test_record_claude_usage_event_exact(tmp_path: Path) -> None:
    """Record exact Claude usage from parsed result."""
    from wlcodex.claude_backend import record_claude_usage_event
    from wlcodex.db import Ledger

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    record_claude_usage_event(
        ledger,
        prompt="Implement feature",
        output_text="Done!",
        usage={
            "source": "exact",
            "input_tokens": 2500,
            "output_tokens": 1800,
            "cached_input_tokens": 400,
        },
    )

    events = ledger.list_usage_events(agent="claude")
    assert len(events) == 1
    ue = events[0]
    assert ue.source == "exact"
    assert ue.input_tokens == 2500
    assert ue.output_tokens == 1800
    assert ue.cached_input_tokens == 400


def test_record_claude_usage_event_estimated(tmp_path: Path) -> None:
    """Record estimated Claude usage via approx_tokens fallback."""
    from wlcodex.claude_backend import record_claude_usage_event
    from wlcodex.db import Ledger

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()

    record_claude_usage_event(
        ledger,
        prompt="A" * 400,      # approx_tokens: 100
        output_text="B" * 800,  # approx_tokens: 200
    )

    events = ledger.list_usage_events(agent="claude")
    assert len(events) == 1
    ue = events[0]
    assert ue.source == "estimated"
    assert ue.input_tokens == 100   # 400 // 4
    assert ue.output_tokens == 200  # 800 // 4


def test_record_claude_usage_event_never_raises(tmp_path: Path) -> None:
    """Recording failure must not raise."""
    from wlcodex.claude_backend import record_claude_usage_event

    # Passing None as ledger is invalid but must not raise
    record_claude_usage_event(
        None,
        prompt="test",
        output_text="test",
    )
    # Must not reach here if it raises


# ============================================================================
# New tests: hook-events capability, runtime source wiring, stream parser
# ============================================================================


def test_prompt_args_includes_hook_events_when_supported() -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig

    backend = ClaudeBackend(ClaudeConfig(enabled=True))
    backend._hook_events_supported = True

    args = backend._prompt_args("test", stream_json=True)
    assert "--include-hook-events" in args
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--include-partial-messages" in args


def test_prompt_args_excludes_hook_events_when_not_supported() -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig

    backend = ClaudeBackend(ClaudeConfig(enabled=True))
    backend._hook_events_supported = False

    args = backend._prompt_args("test", stream_json=True)
    assert "--include-hook-events" not in args
    assert "stream-json" in args


def test_prompt_args_no_stream_json_no_hook_events() -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig

    backend = ClaudeBackend(ClaudeConfig(enabled=True))
    backend._hook_events_supported = True

    args = backend._prompt_args("test", stream_json=False)
    assert "--include-hook-events" not in args
    assert "--output-format" not in args


def test_to_agent_stream_event_text() -> None:
    from wlcodex.claude_backend import _to_agent_stream_event
    from wlcodex.claude_stream_parser import ClaudeStreamEvent
    from wlcodex.runtime_events import EventType

    parsed = ClaudeStreamEvent(
        runtime_event_type=EventType.MODEL_TEXT_DELTA,
        runtime_payload={"text": "hello"},
        agent_delta="hello",
        agent_event_type="text",
    )
    agent_event = _to_agent_stream_event(parsed)
    assert agent_event is not None
    assert agent_event.delta == "hello"
    assert agent_event.event_type == "text"


def test_to_agent_stream_event_usage() -> None:
    from wlcodex.claude_backend import _to_agent_stream_event
    from wlcodex.claude_stream_parser import ClaudeStreamEvent
    from wlcodex.runtime_events import EventType

    parsed = ClaudeStreamEvent(
        runtime_event_type=EventType.MODEL_USAGE_UPDATED,
        runtime_payload={"input_tokens": 100},
        agent_usage={"input_tokens": 100},
    )
    agent_event = _to_agent_stream_event(parsed)
    assert agent_event is not None
    assert agent_event.event_type == "usage"
    assert agent_event.usage == {"input_tokens": 100}


def test_to_agent_stream_event_activity_returns_none() -> None:
    from wlcodex.claude_backend import _to_agent_stream_event
    from wlcodex.claude_stream_parser import ClaudeStreamEvent
    from wlcodex.runtime_events import EventType

    parsed = ClaudeStreamEvent(
        runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
        runtime_payload={},
        agent_delta="",
        agent_event_type="text",
        agent_usage=None,
    )
    assert _to_agent_stream_event(parsed) is None


def test_to_agent_stream_event_error() -> None:
    from wlcodex.claude_backend import _to_agent_stream_event
    from wlcodex.claude_stream_parser import ClaudeStreamEvent
    from wlcodex.runtime_events import EventType

    parsed = ClaudeStreamEvent(
        runtime_event_type=EventType.AGENT_RUN_FAILED,
        runtime_payload={"error": "boom"},
        agent_delta="boom",
        agent_event_type="error",
    )
    agent_event = _to_agent_stream_event(parsed)
    assert agent_event is not None
    assert agent_event.event_type == "error"


def test_emit_runtime_noop_when_no_source() -> None:
    from wlcodex.claude_backend import _emit_runtime
    from wlcodex.claude_stream_parser import ClaudeStreamEvent
    from wlcodex.runtime_events import EventType

    parsed = ClaudeStreamEvent(
        runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
        runtime_payload={},
    )
    _emit_runtime(None, parsed)


def test_emit_runtime_appends_to_store(tmp_path: Path) -> None:
    from wlcodex.claude_backend import _emit_runtime
    from wlcodex.claude_stream_parser import ClaudeStreamEvent
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store,
        correlation_id="corr-test",
        agent_run_id=1,
    )

    parsed = ClaudeStreamEvent(
        runtime_event_type=EventType.MODEL_TEXT_DELTA,
        runtime_payload={"text": "hello"},
    )
    _emit_runtime(source, parsed)

    events = store.list_by_correlation("corr-test")
    assert len(events) == 1
    assert events[0].event_type == EventType.MODEL_TEXT_DELTA
    assert events[0].payload == {"text": "hello"}


@pytest.mark.asyncio
async def test_streaming_with_runtime_source_emits_events(tmp_path: Path) -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True)\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': "
        "{'type': 'text_delta', 'text': 'Hello'}}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', "
        "'usage': {'input_tokens': 100, 'output_tokens': 50}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store,
        correlation_id="corr-stream",
        agent_run_id=42,
        conversation_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=1.0,
            stream_idle_timeout_seconds=1.0,
        ),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test",
            workspace_path=str(tmp_path),
        ))
    ]

    text_events = [e for e in events if e.event_type == "text"]
    assert len(text_events) == 1
    assert text_events[0].delta == "Hello"

    stored = store.list_by_agent_run(42)
    assert len(stored) >= 3
    stored_types = [e.event_type for e in stored]
    assert EventType.MODEL_TEXT_DELTA in stored_types
    assert EventType.MODEL_USAGE_UPDATED in stored_types


@pytest.mark.asyncio
async def test_streaming_without_runtime_source_still_works(tmp_path: Path) -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'stream_event', 'event': {'delta': "
        "{'type': 'text_delta', 'text': 'hello'}}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=1.0,
            stream_idle_timeout_seconds=1.0,
        ),
    )
    backend._hook_events_supported = False

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test",
            workspace_path=str(tmp_path),
        ))
    ]

    text_events = [e for e in events if e.event_type == "text"]
    assert len(text_events) == 1
    assert text_events[0].delta == "hello"


@pytest.mark.asyncio
async def test_streaming_emits_tool_use_as_activity(tmp_path: Path) -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'stream_event', 'event': "
        "{'type': 'content_block_start', 'index': 0, 'content_block': "
        "{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {}}}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store,
        correlation_id="corr-tool",
        agent_run_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=1.0,
            stream_idle_timeout_seconds=1.0,
        ),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="read file",
            workspace_path=str(tmp_path),
        ))
    ]

    text_events = [e for e in events if e.event_type == "text"]
    assert len(text_events) == 0

    stored = store.list_by_agent_run(1)
    stored_types = [e.event_type for e in stored]
    assert EventType.TOOL_CALL_STARTED in stored_types


@pytest.mark.asyncio
async def test_streaming_emits_api_retry_as_runtime_event(tmp_path: Path) -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'api_retry', "
        "'message': 'retrying after 429'}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store,
        correlation_id="corr-retry",
        agent_run_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=1.0,
            stream_idle_timeout_seconds=1.0,
        ),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    _events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test",
            workspace_path=str(tmp_path),
        ))
    ]

    stored = store.list_by_agent_run(1)
    stored_types = [e.event_type for e in stored]
    assert EventType.MODEL_API_RETRY in stored_types


@pytest.mark.asyncio
async def test_streaming_activity_events_do_not_pollute_text(tmp_path: Path) -> None:
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True)\n"
        "print(json.dumps({'type': 'stream_event', 'event': "
        "{'type': 'content_block_stop', 'index': 0}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(
            enabled=True,
            binary=str(fake_claude),
            request_timeout_seconds=1.0,
            stream_idle_timeout_seconds=1.0,
        ),
    )
    backend._hook_events_supported = False

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test",
            workspace_path=str(tmp_path),
        ))
    ]

    text_events = [e for e in events if e.event_type == "text"]
    assert len(text_events) == 0


# ============================================================================
# Lifecycle runtime event emission tests
# ============================================================================


@pytest.mark.asyncio
async def test_streaming_emits_agent_run_started(tmp_path: Path) -> None:
    """agent.run.started emitted after process creation when runtime_source wired."""
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/bin/sh\necho 'ok'\n", encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store, correlation_id="corr-life", agent_run_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(enabled=True, binary=str(fake_claude),
                     request_timeout_seconds=1.0, stream_idle_timeout_seconds=1.0),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    _events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test", workspace_path=str(tmp_path),
        ))
    ]

    stored = store.list_by_agent_run(1)
    stored_types = [e.event_type for e in stored]
    assert EventType.AGENT_RUN_STARTED in stored_types


@pytest.mark.asyncio
async def test_streaming_emits_agent_run_completed_on_success(tmp_path: Path) -> None:
    """agent.run.completed emitted after clean exit."""
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store, correlation_id="corr-life2", agent_run_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(enabled=True, binary=str(fake_claude),
                     request_timeout_seconds=1.0, stream_idle_timeout_seconds=1.0),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    _events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test", workspace_path=str(tmp_path),
        ))
    ]

    stored = store.list_by_agent_run(1)
    stored_types = [e.event_type for e in stored]
    assert EventType.AGENT_RUN_STARTED in stored_types
    assert EventType.AGENT_RUN_COMPLETED in stored_types
    # started must come before completed in insertion order
    started_idx = stored_types.index(EventType.AGENT_RUN_STARTED)
    completed_idx = stored_types.index(EventType.AGENT_RUN_COMPLETED)
    assert started_idx < completed_idx


@pytest.mark.asyncio
async def test_streaming_emits_agent_run_failed_on_non_zero_exit(tmp_path: Path) -> None:
    """agent.run.failed emitted when Claude exits with non-zero status."""
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store, correlation_id="corr-fail", agent_run_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(enabled=True, binary=str(fake_claude),
                     request_timeout_seconds=1.0, stream_idle_timeout_seconds=1.0),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    _events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test", workspace_path=str(tmp_path),
        ))
    ]

    stored = store.list_by_agent_run(1)
    stored_types = [e.event_type for e in stored]
    assert EventType.AGENT_RUN_STARTED in stored_types
    assert EventType.AGENT_RUN_FAILED in stored_types


@pytest.mark.asyncio
async def test_streaming_emits_watchdog_events_on_idle_timeout(tmp_path: Path) -> None:
    """watchdog.idle_timeout + agent.run.timed_out on idle timeout."""
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\nsleep 0.3\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store, correlation_id="corr-idle", agent_run_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(enabled=True, binary=str(fake_claude),
                     request_timeout_seconds=1.0, stream_idle_timeout_seconds=0.03),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    _events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test", workspace_path=str(tmp_path),
        ))
    ]

    stored = store.list_by_agent_run(1)
    stored_types = [e.event_type for e in stored]
    assert EventType.WATCHDOG_IDLE_TIMEOUT in stored_types
    assert EventType.AGENT_RUN_TIMED_OUT in stored_types


@pytest.mark.asyncio
async def test_streaming_emits_watchdog_events_on_hard_timeout(tmp_path: Path) -> None:
    """watchdog.hard_timeout + agent.run.timed_out on hard timeout."""
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.claude_runtime_source import ClaudeRuntimeSource
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.runtime_events import EventType
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "print('start', flush=True)\n"
        "time.sleep(0.3)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    source = ClaudeRuntimeSource(
        store=store, correlation_id="corr-hard", agent_run_id=1,
    )

    backend = ClaudeBackend(
        ClaudeConfig(enabled=True, binary=str(fake_claude),
                     request_timeout_seconds=0.05, stream_idle_timeout_seconds=0.1),
        runtime_source=source,
    )
    backend._hook_events_supported = False

    _events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test", workspace_path=str(tmp_path),
        ))
    ]

    stored = store.list_by_agent_run(1)
    stored_types = [e.event_type for e in stored]
    assert EventType.AGENT_RUN_STARTED in stored_types
    assert EventType.WATCHDOG_HARD_TIMEOUT in stored_types
    assert EventType.AGENT_RUN_TIMED_OUT in stored_types


@pytest.mark.asyncio
async def test_streaming_does_not_emit_lifecycle_without_runtime_source(
    tmp_path: Path,
) -> None:
    """Without runtime_source, no lifecycle events but backward compat works."""
    from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
    from wlcodex.agent_backend import AgentRequest

    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text("#!/bin/sh\necho 'ok'\n", encoding="utf-8")
    fake_claude.chmod(0o755)

    backend = ClaudeBackend(
        ClaudeConfig(enabled=True, binary=str(fake_claude),
                     request_timeout_seconds=1.0, stream_idle_timeout_seconds=1.0),
    )
    backend._hook_events_supported = False

    events = [
        event
        async for event in backend.send_streaming(AgentRequest(
            prompt="test", workspace_path=str(tmp_path),
        ))
    ]

    # Backward compat: only the raw line as text event
    text_events = [e for e in events if e.event_type == "text"]
    assert len(text_events) >= 1
