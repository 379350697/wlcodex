from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from wlcodex.db import Ledger
from wlcodex.live_stream.models import stream_event_from_runtime
from wlcodex.native_agents.runtime_events import (
    NativeAgentRuntimeEmitter,
    extract_native_agent_text,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


def _session_and_store(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    runtime_store = RuntimeEventStore(ledger._conn)
    session_store = NativeAgentSessionStore(ledger)
    session = session_store.get_or_create_session(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id="session-1",
        title="Work",
        cwd=str(tmp_path),
        source_kind="claude_sdk_deepseek",
        status="running",
    )
    return session, runtime_store


def test_emitter_appends_lifecycle_user_text_and_completion_events(
    tmp_path: Path,
) -> None:
    session, runtime_store = _session_and_store(tmp_path)
    emitter = NativeAgentRuntimeEmitter(
        runtime_store=runtime_store,
        provider="claude",
        provider_engine="sdk-deepseek",
        source_kind="claude_sdk_deepseek",
    )

    emitter.started(session, native_turn_id="turn-1")
    emitter.user_message(session, native_turn_id="turn-1", text="fix it")
    emitter.text_delta(session, native_turn_id="turn-1", delta="done")
    emitter.message_completed(
        session,
        native_turn_id="turn-1",
        text="final done",
        item_id="jsonl-assistant-final:turn-1",
    )
    emitter.completed(session, native_turn_id="turn-1")

    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
        EventType.PROVIDER_DISPLAY_COMPLETED,
        EventType.MODEL_MESSAGE_COMPLETED,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert [stream_event_from_runtime(event).kind for event in events] == [
        "lifecycle",
        "user_message",
        "text_delta",
        "compatibility_event",
        "message_completed",
        "message_completed",
        "completed",
    ]
    assert events[4].payload["text"] == "final done"
    assert events[4].payload["itemId"] == "jsonl-assistant-final:turn-1"
    assert events[3].payload["compatibility_projection"] == EventType.MODEL_TEXT_DELTA
    assert events[5].payload["compatibility_projection"] == EventType.MODEL_MESSAGE_COMPLETED
    for event in events:
        assert event.payload["native_thread_id"] == "session-1"
        assert event.payload["native_turn_id"] == "turn-1"
        assert event.payload["provider"] == "claude"
        assert event.payload["provider_engine"] == "sdk-deepseek"
        assert event.payload["source_kind"] == "claude_sdk_deepseek"


def test_emitter_records_raw_frame_and_display_events_without_json_gate(
    tmp_path: Path,
) -> None:
    session, runtime_store = _session_and_store(tmp_path)
    emitter = NativeAgentRuntimeEmitter(
        runtime_store=runtime_store,
        provider="claude",
        provider_engine="sdk-deepseek",
        source_kind="claude_sdk_deepseek",
    )
    bad_json_fragment = '{"routing_decision":'
    raw_payload = {"type": "content_block_delta", "delta": bad_json_fragment}

    raw_event = emitter.raw_frame(
        session,
        native_turn_id="turn-1",
        raw_kind="sdk.message",
        raw_payload=raw_payload,
    )
    emitter.text_delta(
        session,
        native_turn_id="turn-1",
        delta=bad_json_fragment,
    )
    emitter.message_completed(
        session,
        native_turn_id="turn-1",
        text=bad_json_fragment,
    )

    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
        EventType.PROVIDER_DISPLAY_COMPLETED,
        EventType.MODEL_MESSAGE_COMPLETED,
    ]
    assert raw_event.payload["raw_frame_id"] > 0
    assert raw_event.payload["sequence"] == 1
    assert "raw_payload" not in raw_event.payload
    raw_frame = runtime_store.get_provider_raw_frame(raw_event.payload["raw_frame_id"])
    assert raw_frame.raw_payload == raw_payload
    assert events[1].payload["delta"] == bad_json_fragment
    assert events[3].payload["text"] == bad_json_fragment


def test_emitter_appends_failure_event(tmp_path: Path) -> None:
    session, runtime_store = _session_and_store(tmp_path)
    emitter = NativeAgentRuntimeEmitter(
        runtime_store=runtime_store,
        provider="claude",
        provider_engine="sdk-deepseek",
        source_kind="claude_sdk_deepseek",
    )

    emitter.failed(session, native_turn_id="turn-1", error="sdk failed")

    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert len(events) == 1
    assert events[0].event_type == EventType.AGENT_RUN_FAILED
    assert events[0].payload["error"] == "sdk failed"
    assert stream_event_from_runtime(events[0]).kind == "failed"


def test_extract_native_agent_text_handles_common_runner_shapes() -> None:
    assert extract_native_agent_text("hello") == "hello"
    assert extract_native_agent_text({"text": "from dict"}) == "from dict"
    assert extract_native_agent_text({"delta": "from delta"}) == "from delta"
    assert extract_native_agent_text({"content": [{"text": "a"}, {"text": "b"}]}) == "ab"
    assert extract_native_agent_text(SimpleNamespace(delta="from object")) == "from object"
    assert extract_native_agent_text(SimpleNamespace(content=[{"text": "x"}])) == "x"
    assert (
        extract_native_agent_text(
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="sdk "),
                    SimpleNamespace(type="text", text="block"),
                ],
            )
        )
        == "sdk block"
    )
