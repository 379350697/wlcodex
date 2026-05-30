from __future__ import annotations

from wlcodex.live_stream.models import stream_event_from_runtime
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


def _event(event_type: str, payload: dict, *, event_id: int = 10) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id="42",
        correlation_id="corr-live",
        source=EventSource.CODEX,
        actor="codex",
        visibility=Visibility.USER,
        payload=payload,
        occurred_at=now_iso(),
        conversation_id=7,
        agent_run_id=42,
        id=event_id,
    )


def test_model_text_delta_maps_to_text_delta_kind() -> None:
    runtime = _event(EventType.MODEL_TEXT_DELTA, {"delta": "hello"}, event_id=11)

    stream = stream_event_from_runtime(runtime)

    assert stream.id == 11
    assert stream.type == EventType.MODEL_TEXT_DELTA
    assert stream.kind == "text_delta"
    assert stream.agent_run_id == 42
    assert stream.conversation_id == 7
    assert stream.payload == {"delta": "hello"}


def test_command_output_maps_to_command_output_kind() -> None:
    runtime = _event(EventType.COMMAND_OUTPUT_DELTA, {"delta": "pytest"}, event_id=12)

    stream = stream_event_from_runtime(runtime)

    assert stream.kind == "command_output"
    assert stream.payload["delta"] == "pytest"


def test_to_json_dict_keeps_runtime_metadata() -> None:
    runtime = _event(EventType.APPROVAL_REQUESTED, {"summary": "needs approval"}, event_id=13)

    data = stream_event_from_runtime(runtime).to_json_dict()

    assert data["id"] == 13
    assert data["type"] == EventType.APPROVAL_REQUESTED
    assert data["kind"] == "approval_requested"
    assert data["source"] == EventSource.CODEX
    assert data["actor"] == "codex"
    assert data["visibility"] == Visibility.USER
    assert data["payload"] == {"summary": "needs approval"}
