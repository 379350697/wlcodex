"""Tests for agent backend protocol."""

from wlcodex.agent_backend import AgentRequest, AgentResult, AgentStreamEvent


def test_agent_request_defaults() -> None:
    req = AgentRequest(prompt="hello")
    assert req.prompt == "hello"
    assert req.workspace_path == ""
    assert req.model == ""
    assert req.extra == {}


def test_agent_result_defaults() -> None:
    result = AgentResult()
    assert result.text == ""
    assert result.exit_code == 0
    assert result.token_input == 0
    assert result.token_output == 0


def test_agent_stream_event() -> None:
    event = AgentStreamEvent(delta="hi", event_type="text")
    assert event.delta == "hi"
    assert event.event_type == "text"


def test_agent_request_with_extra() -> None:
    req = AgentRequest(
        prompt="test",
        workspace_path="/tmp",
        model="claude-sonnet-4-6",
        extra={"max_tokens": 1000},
    )
    assert req.workspace_path == "/tmp"
    assert req.model == "claude-sonnet-4-6"
    assert req.extra["max_tokens"] == 1000
