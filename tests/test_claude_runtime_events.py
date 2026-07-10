"""Tests for Claude stream parser and ClaudeRuntimeSource."""

from __future__ import annotations

import json
from pathlib import Path


from wlcodex.runtime_events import EventType
from wlcodex.claude_stream_parser import ClaudeStreamEvent, parse_line
from wlcodex.claude_runtime_source import ClaudeRuntimeSource
from wlcodex.db import Ledger
from wlcodex.runtime_event_store import RuntimeEventStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> RuntimeEventStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return RuntimeEventStore(ledger._conn)


def _source(store: RuntimeEventStore, **kw: object) -> ClaudeRuntimeSource:
    defaults: dict[str, object] = {
        "correlation_id": "corr-1",
        "agent_run_id": 1,
        "conversation_id": 1,
        "orchestration_run_id": 1,
        "task_id": 1,
    }
    merged = {**defaults, **kw}
    return ClaudeRuntimeSource(store=store, **merged)  # type: ignore[arg-type]


def _payload(overrides: object = None) -> dict:
    if overrides is None:
        return {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "hello"}}}
    return dict(overrides)


def _text_delta(text: str) -> dict:
    return {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": text}}}


def _assistant_msg(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool_use_stream(name: str, tool_id: str = "toolu_1") -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
        },
    }


def _assistant_with_tools(text: str, tools: list[dict]) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(tools)
    return {"type": "assistant", "message": {"content": content}}


# ============================================================================
# Parser: stream_event text_delta
# ============================================================================

def test_parse_text_delta() -> None:
    events, text = parse_line(json.dumps(_text_delta("Hello world")))
    assert len(events) == 1
    assert events[0].runtime_event_type == EventType.MODEL_TEXT_DELTA
    assert events[0].runtime_payload == {"text": "Hello world"}
    assert events[0].agent_delta == "Hello world"
    assert events[0].agent_event_type == "text"
    assert text == "Hello world"


def test_parse_text_delta_accumulates() -> None:
    events, text = parse_line(
        json.dumps(_text_delta(" world")), assistant_text="Hello"
    )
    assert len(events) == 1
    assert events[0].agent_delta == " world"
    assert text == "Hello world"


def test_parse_text_delta_empty() -> None:
    events, text = parse_line(
        json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": ""}}})
    )
    assert len(events) == 1
    assert events[0].runtime_event_type == EventType.AGENT_RUN_ACTIVITY


# ============================================================================
# Parser: assistant message
# ============================================================================

def test_parse_assistant_text() -> None:
    events, text = parse_line(json.dumps(_assistant_msg("The answer is 42.")))
    assert any(e.runtime_event_type == EventType.MODEL_MESSAGE_COMPLETED for e in events)
    assert any(e.runtime_event_type == EventType.MODEL_TEXT_DELTA for e in events)
    assert text == "The answer is 42."


def test_parse_assistant_delta_only() -> None:
    events, text = parse_line(
        json.dumps(_assistant_msg("Hello world")), assistant_text="Hello "
    )
    text_events = [e for e in events if e.runtime_event_type == EventType.MODEL_TEXT_DELTA]
    assert len(text_events) == 1
    assert text_events[0].agent_delta == "world"
    assert text == "Hello world"


def test_parse_assistant_no_new_text() -> None:
    events, text = parse_line(
        json.dumps(_assistant_msg("same")), assistant_text="same"
    )
    text_events = [e for e in events if e.runtime_event_type == EventType.MODEL_TEXT_DELTA]
    assert len(text_events) == 0
    assert text == "same"


def test_parse_assistant_content_string() -> None:
    payload = {"type": "assistant", "message": {"content": "plain string content"}}
    events, text = parse_line(json.dumps(payload))
    assert any(e.runtime_event_type == EventType.MODEL_MESSAGE_COMPLETED for e in events)
    assert text == "plain string content"


# ============================================================================
# Parser: tool_use
# ============================================================================

def test_parse_tool_use_content_block_start() -> None:
    events, text = parse_line(json.dumps(_tool_use_stream("Read", "toolu_001")))
    assert len(events) == 1
    assert events[0].runtime_event_type == EventType.TOOL_CALL_STARTED
    assert events[0].runtime_payload["tool_name"] == "Read"
    assert events[0].runtime_payload["tool_id"] == "toolu_001"


def test_parse_tool_use_in_assistant() -> None:
    tool = {"type": "tool_use", "id": "toolu_002", "name": "Bash", "input": {"command": "ls"}}
    payload = _assistant_with_tools("Let me check.", [tool])
    events, text = parse_line(json.dumps(payload))
    tool_events = [e for e in events if e.runtime_event_type == EventType.TOOL_CALL_COMPLETED]
    assert len(tool_events) == 1
    assert tool_events[0].runtime_payload["tool_name"] == "Bash"
    assert text == "Let me check."


def test_parse_assistant_tool_use_no_input() -> None:
    tool = {"type": "tool_use", "id": "toolu_003", "name": "Read"}
    payload = _assistant_with_tools("", [tool])
    events, _ = parse_line(json.dumps(payload))
    tool_events = [e for e in events if e.runtime_event_type == EventType.TOOL_CALL_STARTED]
    assert len(tool_events) == 1
    assert tool_events[0].runtime_payload["tool_name"] == "Read"


# ============================================================================
# Parser: system / api_retry
# ============================================================================

def test_parse_api_retry() -> None:
    payload = {"type": "system", "subtype": "api_retry", "message": "429 Too Many Requests"}
    events, text = parse_line(json.dumps(payload))
    retry_events = [e for e in events if e.runtime_event_type == EventType.MODEL_API_RETRY]
    assert len(retry_events) == 1
    assert "429" in retry_events[0].runtime_payload["message"]
    # Also emits activity
    activity = [e for e in events if e.runtime_event_type == EventType.AGENT_RUN_ACTIVITY]
    assert len(activity) == 1
    assert text == ""


def test_parse_system_init() -> None:
    payload = {"type": "system", "subtype": "init", "message": "session started"}
    events, _ = parse_line(json.dumps(payload))
    assert all(e.runtime_event_type == EventType.AGENT_RUN_ACTIVITY for e in events)


# ============================================================================
# Parser: result / usage
# ============================================================================

def test_parse_result_with_usage() -> None:
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "Done.",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    events, _ = parse_line(json.dumps(payload))
    usage_events = [e for e in events if e.runtime_event_type == EventType.MODEL_USAGE_UPDATED]
    assert len(usage_events) == 1
    assert usage_events[0].runtime_payload["input_tokens"] == 100
    assert usage_events[0].runtime_payload["output_tokens"] == 50
    assert usage_events[0].agent_usage is not None


def test_parse_result_without_usage() -> None:
    payload = {"type": "result", "subtype": "success", "result": "ok"}
    events, _ = parse_line(json.dumps(payload))
    usage_events = [e for e in events if e.runtime_event_type == EventType.MODEL_USAGE_UPDATED]
    assert len(usage_events) == 0


def test_parse_result_error_subtype() -> None:
    payload = {"type": "result", "subtype": "error", "error": "something broke"}
    events, _ = parse_line(json.dumps(payload))
    assert events[0].runtime_event_type == EventType.AGENT_RUN_FAILED
    assert events[0].agent_event_type == "error"


def test_parse_result_with_cached_tokens() -> None:
    payload = {
        "type": "result",
        "subtype": "success",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 30,
        },
    }
    events, _ = parse_line(json.dumps(payload))
    usage = [e for e in events if e.runtime_event_type == EventType.MODEL_USAGE_UPDATED]
    assert len(usage) == 1
    assert usage[0].runtime_payload["cached_input_tokens"] == 230


def test_parse_result_text_only_when_nothing_emitted() -> None:
    payload = {"type": "result", "subtype": "success", "result": "Final answer."}
    events, _ = parse_line(json.dumps(payload), has_emitted_text=False)
    text_events = [e for e in events if e.runtime_event_type == EventType.MODEL_TEXT_DELTA]
    assert len(text_events) == 1
    assert text_events[0].agent_delta == "Final answer."


def test_parse_result_text_skipped_when_already_emitted() -> None:
    payload = {"type": "result", "subtype": "success", "result": "Final answer."}
    events, _ = parse_line(json.dumps(payload), has_emitted_text=True)
    text_events = [e for e in events if e.runtime_event_type == EventType.MODEL_TEXT_DELTA]
    assert len(text_events) == 0


# ============================================================================
# Parser: hook events
# ============================================================================

def test_parse_hook_started() -> None:
    payload = {"type": "hook.started", "hook": {"id": "h1", "name": "PreToolUse"}}
    events, _ = parse_line(json.dumps(payload))
    progress = [e for e in events if e.runtime_event_type == EventType.TOOL_CALL_PROGRESS]
    assert len(progress) == 1
    assert progress[0].runtime_payload["hook_name"] == "PreToolUse"
    assert progress[0].runtime_payload["phase"] == "started"


def test_parse_hook_progress() -> None:
    payload = {"type": "hook.progress", "hook": {"id": "h1", "name": "PreToolUse", "output": "working..."}}
    events, _ = parse_line(json.dumps(payload))
    progress = [e for e in events if e.runtime_event_type == EventType.TOOL_CALL_PROGRESS]
    assert len(progress) == 1
    assert progress[0].runtime_payload["phase"] == "progress"


def test_parse_hook_completed() -> None:
    payload = {"type": "hook.completed", "hook": {"id": "h1", "name": "PreToolUse", "result": "approved"}}
    events, _ = parse_line(json.dumps(payload))
    completed = [e for e in events if e.runtime_event_type == EventType.TOOL_CALL_COMPLETED]
    assert len(completed) == 1


def test_parse_hook_error() -> None:
    payload = {"type": "hook.error", "hook": {"id": "h1", "name": "PreToolUse"}, "error": "timeout"}
    events, _ = parse_line(json.dumps(payload))
    failed = [e for e in events if e.runtime_event_type == EventType.TOOL_CALL_FAILED]
    assert len(failed) == 1


def test_parse_hook_always_emits_activity() -> None:
    payload = {"type": "hook.started", "hook": {"id": "h1", "name": "PreToolUse"}}
    events, _ = parse_line(json.dumps(payload))
    activity = [e for e in events if e.runtime_event_type == EventType.AGENT_RUN_ACTIVITY]
    assert len(activity) == 1


# ============================================================================
# Parser: error / unknown / non-JSON
# ============================================================================

def test_parse_error_event() -> None:
    payload = {"type": "error", "message": "connection refused"}
    events, _ = parse_line(json.dumps(payload))
    assert events[0].runtime_event_type == EventType.AGENT_RUN_FAILED
    assert events[0].agent_event_type == "error"


def test_parse_api_error_event() -> None:
    payload = {"type": "api_error", "message": "rate limited"}
    events, _ = parse_line(json.dumps(payload))
    assert events[0].runtime_event_type == EventType.AGENT_RUN_FAILED


def test_parse_unknown_json_type_emits_activity() -> None:
    payload = {"type": "custom_event", "data": "something"}
    events, _ = parse_line(json.dumps(payload))
    assert len(events) == 1
    assert events[0].runtime_event_type == EventType.AGENT_RUN_ACTIVITY


def test_parse_non_json_line_emits_activity() -> None:
    events, _ = parse_line("This is just raw text\n")
    assert len(events) == 1
    assert events[0].runtime_event_type == EventType.AGENT_RUN_ACTIVITY
    assert events[0].agent_delta == "This is just raw text\n"


def test_parse_empty_line() -> None:
    events, _ = parse_line("   \n")
    assert events == []


def test_parse_non_dict_json() -> None:
    events, _ = parse_line("[1, 2, 3]")
    assert events == []


# ============================================================================
# Parser: stream_event content_block types
# ============================================================================

def test_parse_input_json_delta() -> None:
    payload = {
        "type": "stream_event",
        "event": {"delta": {"type": "input_json_delta", "partial_json": '{"file":'}},
    }
    events, _ = parse_line(json.dumps(payload))
    assert events[0].runtime_event_type == EventType.AGENT_RUN_ACTIVITY
    assert events[0].runtime_payload["stream_type"] == "input_json_delta"


def test_parse_content_block_stop() -> None:
    payload = {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
    events, _ = parse_line(json.dumps(payload))
    assert events[0].runtime_event_type == EventType.AGENT_RUN_ACTIVITY


def test_parse_stream_event_no_event_dict() -> None:
    payload = {"type": "stream_event"}
    events, _ = parse_line(json.dumps(payload))
    assert events[0].runtime_event_type == EventType.AGENT_RUN_ACTIVITY


def test_parse_text_content_block_start() -> None:
    payload = {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    }
    events, _ = parse_line(json.dumps(payload))
    assert events[0].runtime_event_type == EventType.AGENT_RUN_ACTIVITY


# ============================================================================
# ClaudeRuntimeSource
# ============================================================================

def test_source_emit_appends_to_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(store)
    parsed = ClaudeStreamEvent(
        runtime_event_type=EventType.MODEL_TEXT_DELTA,
        runtime_payload={"text": "hello"},
    )
    stored = source.emit(parsed)
    assert stored.id > 0
    assert stored.event_type == EventType.MODEL_TEXT_DELTA
    assert stored.source == "claude"
    assert stored.aggregate_type == "agent_run"
    assert stored.correlation_id == "corr-1"
    assert stored.agent_run_id == 1

    # Read back
    retrieved = store.get_by_id(stored.id)
    assert retrieved.event_type == EventType.MODEL_TEXT_DELTA


def test_source_emit_lifecycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(store)
    event = source.emit_lifecycle(EventType.AGENT_RUN_STARTED, payload={"model": "gpt-5"})
    assert event.id > 0
    assert event.event_type == EventType.AGENT_RUN_STARTED
    assert event.payload == {"model": "gpt-5"}


def test_source_emit_capability_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(store)
    event = source.emit_capability_missing("include-hook-events")
    assert event.event_type == EventType.RUNTIME_CAPABILITY_MISSING
    assert event.payload["capability"] == "include-hook-events"
    assert event.visibility == "operator"


def test_source_causation_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(store)
    e1 = source.emit_lifecycle(EventType.AGENT_RUN_STARTED)
    e2 = source.emit(ClaudeStreamEvent(
        runtime_event_type=EventType.MODEL_TEXT_DELTA,
        runtime_payload={"text": "hello"},
    ))
    assert e2.causation_id == e1.id


def test_source_correlation_id_is_used(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(store, correlation_id="custom-corr-42")
    event = source.emit(ClaudeStreamEvent(
        runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
        runtime_payload={},
    ))
    assert event.correlation_id == "custom-corr-42"


def test_source_multiple_events_persist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(store)
    source.emit_lifecycle(EventType.AGENT_RUN_STARTED)
    source.emit(ClaudeStreamEvent(
        runtime_event_type=EventType.MODEL_TEXT_DELTA,
        runtime_payload={"text": "a"},
    ))
    source.emit(ClaudeStreamEvent(
        runtime_event_type=EventType.TOOL_CALL_STARTED,
        runtime_payload={"tool_name": "Read"},
    ))
    source.emit_lifecycle(EventType.AGENT_RUN_COMPLETED)

    events = store.list_by_agent_run(1)
    assert len(events) == 4
    assert events[0].event_type == EventType.AGENT_RUN_STARTED
    assert events[1].event_type == EventType.MODEL_TEXT_DELTA
    assert events[2].event_type == EventType.TOOL_CALL_STARTED
    assert events[3].event_type == EventType.AGENT_RUN_COMPLETED


def test_source_with_no_optional_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = ClaudeRuntimeSource(
        store=store,
        correlation_id="minimal",
    )
    event = source.emit(ClaudeStreamEvent(
        runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
        runtime_payload={},
    ))
    assert event.agent_run_id is None
    assert event.conversation_id is None
    assert event.orchestration_run_id is None


# ============================================================================
# Integration: parser output fed to runtime source
# ============================================================================

def test_full_stream_simulation(tmp_path: Path) -> None:
    """Simulate a complete Claude streaming session."""
    store = _store(tmp_path)
    source = _source(store)

    messages = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps(_text_delta("I will implement")),
        json.dumps(_text_delta(" the feature.")),
        json.dumps(_tool_use_stream("Read", "toolu_1")),
        json.dumps({"type": "system", "subtype": "api_retry", "message": "retry 1"}),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "Done.",
                "usage": {"input_tokens": 500, "output_tokens": 200},
            }
        ),
    ]

    for msg in messages:
        parsed_events, _ = parse_line(msg)
        for pe in parsed_events:
            source.emit(pe)

    all_events = store.list_by_agent_run(1)
    assert len(all_events) >= 6  # at minimum, the specific events

    event_types = [e.event_type for e in all_events]
    assert EventType.MODEL_TEXT_DELTA in event_types
    assert EventType.TOOL_CALL_STARTED in event_types
    assert EventType.MODEL_API_RETRY in event_types
    assert EventType.MODEL_USAGE_UPDATED in event_types
