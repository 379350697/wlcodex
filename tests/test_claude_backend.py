"""Tests for Claude backend."""

from pathlib import Path
import pytest

from wlcodex.claude_backend import ClaudeBackend, ClaudeConfig
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
    assert config.request_timeout_seconds == 600.0


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
