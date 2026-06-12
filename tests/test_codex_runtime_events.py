"""Tests for CodexRuntimeSource — maps BackendEvent → RuntimeEvent."""

from __future__ import annotations

import pytest

from wlcodex.codex_backend import BackendEvent
from wlcodex.codex_runtime_source import CodexRuntimeSource
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    Visibility,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _source(**overrides: object) -> CodexRuntimeSource:
    return CodexRuntimeSource(
        correlation_id=str(overrides.pop("correlation_id", "corr-test")),
        agent_run_id=int(overrides.pop("agent_run_id", 42)),
        conversation_id=overrides.pop("conversation_id", 1),  # type: ignore[arg-type]
        orchestration_run_id=overrides.pop("orchestration_run_id", 5),  # type: ignore[arg-type]
        task_id=overrides.pop("task_id", 10),  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def _first(payload: dict, **kw: object) -> dict:
    """Shortcut: map one event and return the first result's payload for inspection."""
    src = _source(**kw)
    events = src.map_event(BackendEvent(payload.pop("_event_type", "unknown"), payload))
    assert len(events) == 1
    return events[0].payload


# ---------------------------------------------------------------------------
# thread_started → agent.run.activity
# ---------------------------------------------------------------------------

def test_thread_started_maps_to_agent_run_activity() -> None:
    src = _source()
    events = src.map_event(BackendEvent("thread_started", {"threadId": "th-1"}))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.AGENT_RUN_ACTIVITY
    assert e.source == EventSource.CODEX
    assert e.actor == "codex"
    assert e.aggregate_type == AggregateType.AGENT_RUN
    assert e.aggregate_id == "42"
    assert e.agent_run_id == 42
    assert e.correlation_id == "corr-test"
    assert e.conversation_id == 1
    assert e.payload["action"] == "thread_started"
    assert e.payload["threadId"] == "th-1"


# ---------------------------------------------------------------------------
# turn_started → agent.run.activity
# ---------------------------------------------------------------------------

def test_turn_started_maps_to_agent_run_activity() -> None:
    src = _source()
    events = src.map_event(BackendEvent("turn_started", {
        "threadId": "th-1", "turnId": "tu-1",
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.AGENT_RUN_ACTIVITY
    assert e.payload["action"] == "turn_started"
    assert e.payload["threadId"] == "th-1"
    assert e.payload["turnId"] == "tu-1"


# ---------------------------------------------------------------------------
# turn_completed → agent.run.activity
# ---------------------------------------------------------------------------

def test_turn_completed_maps_to_agent_run_activity() -> None:
    src = _source()
    events = src.map_event(BackendEvent("turn_completed", {
        "threadId": "th-1", "turnId": "tu-1",
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.AGENT_RUN_ACTIVITY
    assert e.payload["action"] == "turn_completed"


def test_turn_completed_captures_status_from_nested_turn() -> None:
    src = _source()
    events = src.map_event(BackendEvent("turn_completed", {
        "threadId": "th-1",
        "turnId": "tu-1",
        "turn": {"id": "tu-1", "status": "failed"},
    }))
    assert events[0].payload["status"] == "failed"


def test_turn_completed_captures_status_from_flat_payload() -> None:
    src = _source()
    events = src.map_event(BackendEvent("turn_completed", {
        "threadId": "th-1", "turnId": "tu-1", "status": "cancelled",
    }))
    assert events[0].payload["status"] == "cancelled"


# ---------------------------------------------------------------------------
# item_started command → command.started
# ---------------------------------------------------------------------------

def test_item_started_command_maps_to_command_started() -> None:
    src = _source()
    events = src.map_event(BackendEvent("item_started", {
        "item": {"id": "item-cmd-1", "type": "commandExecution",
                 "command": "pytest -q"},
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.COMMAND_STARTED
    assert e.payload["itemId"] == "item-cmd-1"
    assert e.payload["command"] == "pytest -q"


def test_item_started_command_list_is_flattened() -> None:
    src = _source()
    events = src.map_event(BackendEvent("item_started", {
        "item": {"id": "item-cmd-2", "type": "commandExecution",
                 "command": ["python3", "probe.py"]},
    }))
    assert events[0].payload["command"] == "python3 probe.py"


def test_item_started_non_command_yields_empty() -> None:
    src = _source()
    events = src.map_event(BackendEvent("item_started", {
        "item": {"id": "item-msg-1", "type": "agentMessage"},
    }))
    assert events == []


def test_item_started_with_missing_item_yields_empty() -> None:
    src = _source()
    events = src.map_event(BackendEvent("item_started", {"other": 1}))
    assert events == []


# ---------------------------------------------------------------------------
# item_completed command → command.completed
# ---------------------------------------------------------------------------

def test_item_completed_command_maps_to_command_completed() -> None:
    src = _source()
    events = src.map_event(BackendEvent("item_completed", {
        "item": {"id": "item-cmd-3", "type": "commandExecution",
                 "command": "echo done"},
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.COMMAND_COMPLETED
    assert e.payload["itemId"] == "item-cmd-3"
    assert e.payload["command"] == "echo done"


def test_item_completed_non_command_yields_empty() -> None:
    src = _source()
    events = src.map_event(BackendEvent("item_completed", {
        "item": {"id": "item-fc-1", "type": "fileChange"},
    }))
    assert events == []


# ---------------------------------------------------------------------------
# command_output_delta → command.output.delta
# ---------------------------------------------------------------------------

def test_command_output_delta() -> None:
    src = _source()
    events = src.map_event(BackendEvent("command_output_delta", {
        "delta": "test output\n", "itemId": "item-cmd-1",
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.COMMAND_OUTPUT_DELTA
    assert e.payload["delta"] == "test output\n"
    assert e.payload["itemId"] == "item-cmd-1"


# ---------------------------------------------------------------------------
# file_change_delta → file.changed
# ---------------------------------------------------------------------------

def test_file_change_delta() -> None:
    src = _source()
    events = src.map_event(BackendEvent("file_change_delta", {
        "delta": "+import os\n", "itemId": "item-fc-1",
        "filePath": "wlcodex/main.py",
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.FILE_CHANGED
    assert e.visibility == Visibility.OPERATOR
    assert e.payload["filePath"] == "wlcodex/main.py"
    assert e.payload["delta"] == "+import os\n"


# ---------------------------------------------------------------------------
# agent_message_delta → model.text.delta
# ---------------------------------------------------------------------------

def test_agent_message_delta() -> None:
    src = _source()
    events = src.map_event(BackendEvent("agent_message_delta", {
        "delta": "Codex analysis:",
        "item": {"id": "item-msg-1", "type": "agentMessage"},
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.MODEL_TEXT_DELTA
    assert e.visibility == Visibility.USER
    assert e.payload["delta"] == "Codex analysis:"
    assert e.payload["itemId"] == "item-msg-1"


def test_agent_message_delta_without_item() -> None:
    src = _source()
    events = src.map_event(BackendEvent("agent_message_delta", {
        "delta": "partial text",
    }))
    assert len(events) == 1
    assert events[0].payload["delta"] == "partial text"
    assert events[0].payload["itemId"] == ""


# ---------------------------------------------------------------------------
# token_usage_updated → model.usage.updated
# ---------------------------------------------------------------------------

def test_token_usage_v2_protocol() -> None:
    src = _source()
    events = src.map_event(BackendEvent("token_usage_updated", {
        "threadId": "th-1",
        "turnId": "tu-1",
        "tokenUsage": {
            "last": {
                "inputTokens": 2000,
                "cachedInputTokens": 500,
                "outputTokens": 1200,
                "reasoningOutputTokens": 300,
                "totalTokens": 3500,
            },
            "modelContextWindow": 200000,
            "total": {
                "inputTokens": 4000,
                "outputTokens": 2400,
            },
        },
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.MODEL_USAGE_UPDATED
    p = e.payload
    assert p["input_tokens"] == 2000
    assert p["cached_input_tokens"] == 500
    assert p["output_tokens"] == 1200
    assert p["reasoning_output_tokens"] == 300
    assert p["total_tokens"] == 3500
    assert p["model_context_window"] == 200000
    assert p["total"] == {"input_tokens": 4000, "output_tokens": 2400}


def test_token_usage_legacy_flat_format() -> None:
    src = _source()
    events = src.map_event(BackendEvent("token_usage_updated", {
        "threadId": "th-1",
        "turnId": "tu-1",
        "inputTokens": 1000,
        "outputTokens": 500,
    }))
    assert len(events) == 1
    p = events[0].payload
    assert p["input_tokens"] == 1000
    assert p["output_tokens"] == 500


def test_token_usage_empty_payload_yields_empty() -> None:
    src = _source()
    events = src.map_event(BackendEvent("token_usage_updated", {
        "threadId": "th-1",
    }))
    assert events == []


def test_token_usage_non_numeric_values_are_skipped() -> None:
    src = _source()
    events = src.map_event(BackendEvent("token_usage_updated", {
        "inputTokens": "not-a-number",
        "outputTokens": None,
        "cachedInputTokens": 100,
    }))
    assert len(events) == 1
    p = events[0].payload
    assert "input_tokens" not in p
    assert "output_tokens" not in p
    assert p["cached_input_tokens"] == 100


# ---------------------------------------------------------------------------
# diff_updated → diff.updated
# ---------------------------------------------------------------------------

def test_diff_updated() -> None:
    src = _source()
    events = src.map_event(BackendEvent("diff_updated", {
        "diff": "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-foo\n+bar",
        "threadId": "th-1",
        "turnId": "tu-1",
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.DIFF_UPDATED
    assert "foo" in str(e.payload["diff"])


# ---------------------------------------------------------------------------
# approval_requested → approval.requested
# ---------------------------------------------------------------------------

def test_approval_requested() -> None:
    src = _source()
    events = src.map_event(BackendEvent("approval_requested", {
        "threadId": "th-1",
        "codexRequestId": "req-1",
        "codexItemId": "item-1",
        "kind": "command",
        "summary": "Run: pytest",
        "command": "pytest",
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.APPROVAL_REQUESTED
    assert e.aggregate_type == AggregateType.APPROVAL
    assert e.aggregate_id == "req-1"
    assert e.visibility == Visibility.USER
    assert e.payload["kind"] == "command"
    assert e.payload["codexRequestId"] == "req-1"


def test_approval_requested_preserves_original_payload() -> None:
    src = _source()
    payload = {
        "threadId": "th-1",
        "codexRequestId": "req-2",
        "kind": "file_change",
        "summary": "Apply patch: main.py",
        "command": {},
        "responseSchema": "legacy_review_decision",
    }
    events = src.map_event(BackendEvent("approval_requested", payload))
    # Summary is always present (constructed if missing); existing summary is kept.
    assert events[0].payload["summary"] == "Apply patch: main.py"
    assert events[0].payload["codexRequestId"] == "req-2"


def test_approval_requested_constructs_summary_from_command() -> None:
    """When no summary is in the backend payload, runtime event gets a constructed one."""
    src = _source()
    events = src.map_event(BackendEvent("approval_requested", {
        "codexRequestId": "req-cmd",
        "kind": "command",
        "command": "pytest tests/ -q",
    }))
    assert len(events) == 1
    assert events[0].payload["summary"] == "Run: pytest tests/ -q"
    assert events[0].payload["command"] == "pytest tests/ -q"


def test_approval_requested_constructs_summary_from_command_and_reason() -> None:
    src = _source()
    events = src.map_event(BackendEvent("approval_requested", {
        "codexRequestId": "req-cmd-r",
        "kind": "command",
        "command": "rm -rf /tmp/cache",
        "reason": "清理缓存文件",
    }))
    assert len(events) == 1
    assert events[0].payload["summary"] == "Run: rm -rf /tmp/cache\nReason: 清理缓存文件"


def test_approval_requested_constructs_summary_from_command_list() -> None:
    src = _source()
    events = src.map_event(BackendEvent("approval_requested", {
        "codexRequestId": "req-cmd-list",
        "kind": "command",
        "command": ["git", "commit", "-m", "fix"],
    }))
    assert len(events) == 1
    assert events[0].payload["summary"] == "Run: git commit -m fix"


def test_approval_requested_constructs_summary_for_file_change() -> None:
    src = _source()
    events = src.map_event(BackendEvent("approval_requested", {
        "codexRequestId": "req-file",
        "kind": "file_change",
        "filePath": "src/main.py",
    }))
    assert len(events) == 1
    assert events[0].payload["summary"] == "Edit file: src/main.py"


def test_approval_requested_fallback_summary_never_empty() -> None:
    """Even without command/reason, summary must not be empty."""
    src = _source()
    events = src.map_event(BackendEvent("approval_requested", {
        "codexRequestId": "req-empty",
        "kind": "command",
    }))
    assert len(events) == 1
    summary = str(events[0].payload.get("summary", ""))
    assert summary, f"Expected non-empty summary, got {summary!r}"


# ---------------------------------------------------------------------------
# approval_resolved → approval.resolved
# ---------------------------------------------------------------------------

def test_approval_resolved() -> None:
    src = _source()
    events = src.map_event(BackendEvent("approval_resolved", {
        "codexRequestId": "req-1",
        "response": {"decision": "accept"},
    }))
    assert len(events) == 1
    e = events[0]
    assert e.event_type == EventType.APPROVAL_RESOLVED
    assert e.aggregate_type == AggregateType.APPROVAL
    assert e.aggregate_id == "req-1"
    assert e.visibility == Visibility.USER
    assert e.payload["codexRequestId"] == "req-1"
    assert e.payload["decision"] == "accept"


def test_approval_resolved_permissions_scope() -> None:
    src = _source()
    events = src.map_event(BackendEvent("approval_resolved", {
        "codexRequestId": "req-2",
        "response": {"permissions": {"read": True}, "scope": "session"},
    }))
    assert events[0].payload["decision"] == {"read": True}
    assert events[0].payload["scope"] == "session"


# ---------------------------------------------------------------------------
# thread_status_changed, plan_updated → agent.run.activity
# ---------------------------------------------------------------------------

def test_thread_status_changed() -> None:
    src = _source()
    events = src.map_event(BackendEvent("thread_status_changed", {
        "threadId": "th-1",
        "status": "running",
    }))
    assert len(events) == 1
    assert events[0].event_type == EventType.AGENT_RUN_ACTIVITY
    assert events[0].payload["action"] == "thread_status_changed"


def test_plan_updated() -> None:
    src = _source()
    events = src.map_event(BackendEvent("plan_updated", {
        "threadId": "th-1",
        "turnId": "tu-1",
        "itemId": "plan-1",
        "title": "CL-063",
        "plan": "# CL-063\n\n## Summary\nFix the issue.",
        "summary": "Fix the issue.",
        "status": "ready",
    }))
    assert len(events) == 1
    assert events[0].event_type == EventType.AGENT_RUN_ACTIVITY
    assert events[0].payload["action"] == "plan_updated"
    assert events[0].payload["threadId"] == "th-1"
    assert events[0].payload["turnId"] == "tu-1"
    assert events[0].payload["itemId"] == "plan-1"
    assert events[0].payload["title"] == "CL-063"
    assert events[0].payload["plan"] == "# CL-063\n\n## Summary\nFix the issue."
    assert events[0].payload["summary"] == "Fix the issue."
    assert events[0].payload["status"] == "ready"


# ---------------------------------------------------------------------------
# Unknown event type → empty list
# ---------------------------------------------------------------------------

def test_unknown_event_type_yields_empty() -> None:
    src = _source()
    events = src.map_event(BackendEvent("some_unknown_event", {"x": 1}))
    assert events == []


# ---------------------------------------------------------------------------
# causation_id forwarding
# ---------------------------------------------------------------------------

def test_causation_id_is_forwarded() -> None:
    src = _source()
    events = src.map_event(
        BackendEvent("thread_started", {"threadId": "th-1"}),
        causation_id=99,
    )
    assert events[0].causation_id == 99


# ---------------------------------------------------------------------------
# Context ids are propagated
# ---------------------------------------------------------------------------

def test_context_ids_are_propagated() -> None:
    src = CodexRuntimeSource(
        correlation_id="corr-ctx",
        agent_run_id=7,
        conversation_id=3,
        orchestration_run_id=11,
        task_id=13,
    )
    events = src.map_event(BackendEvent("thread_started", {"threadId": "th-1"}))
    e = events[0]
    assert e.correlation_id == "corr-ctx"
    assert e.agent_run_id == 7
    assert e.conversation_id == 3
    assert e.orchestration_run_id == 11
    assert e.task_id == 13


# ---------------------------------------------------------------------------
# Runtime events are pure — no side effects
# ---------------------------------------------------------------------------

def test_map_event_is_pure() -> None:
    """Mapping a BackendEvent does not mutate the original payload."""
    payload = {"threadId": "th-1", "extra": [1, 2, 3]}
    event = BackendEvent("thread_started", payload)
    original = dict(payload)

    src = _source()
    result = src.map_event(event)

    assert payload == original
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Codex events can feed both old TaskService and new runtime log (AC)
# ---------------------------------------------------------------------------

def test_backend_event_still_has_original_shape_for_task_service() -> None:
    """BackendEvent is unchanged — TaskService can still consume it."""
    event = BackendEvent("token_usage_updated", {
        "threadId": "th-1",
        "turnId": "tu-1",
        "tokenUsage": {
            "last": {"inputTokens": 100, "outputTokens": 50},
        },
    })
    src = _source()
    runtime_events = src.map_event(event)

    # BackendEvent payload is untouched
    assert event.payload["threadId"] == "th-1"
    assert event.payload["tokenUsage"]["last"]["inputTokens"] == 100

    # Runtime event was also produced
    assert len(runtime_events) == 1
    assert runtime_events[0].event_type == EventType.MODEL_USAGE_UPDATED


# ---------------------------------------------------------------------------
# All mapped event type constants are valid
# ---------------------------------------------------------------------------

def test_all_mapped_event_types_are_valid() -> None:
    """Every event type emitted by CodexRuntimeSource is a defined constant."""
    valid = {
        v for k, v in vars(EventType).items()
        if not k.startswith("_") and isinstance(v, str)
    }
    src = _source()
    for backend_type in [
        "thread_started", "thread_status_changed", "turn_started",
        "turn_completed", "item_started", "item_completed",
        "agent_message_delta", "token_usage_updated", "command_output_delta",
        "file_change_delta", "diff_updated", "plan_updated",
        "approval_requested",
    ]:
        payload = {"threadId": "th", "turnId": "tu", "item": {"id": "it", "type": "commandExecution", "command": "ls"}}
        events = src.map_event(BackendEvent(backend_type, payload))
        for e in events:
            assert e.event_type in valid, f"{backend_type} → {e.event_type} not in EventType"
