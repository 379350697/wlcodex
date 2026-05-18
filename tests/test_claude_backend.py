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
