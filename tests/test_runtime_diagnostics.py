"""Tests for runtime diagnostics — status, trace, timeout, and recovery output."""

from __future__ import annotations

from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.runtime_diagnostics import (
    RuntimeAgentSummary,
    RuntimeStatus,
    RuntimeTrace,
    TimeoutExplanation,
    append_recovery_events,
    build_recovery_events,
    build_runtime_status,
    build_runtime_trace,
    compute_timeout_explanation,
    find_non_terminal_agent_runs,
    format_status_display,
    format_timeout_explanation,
    format_trace_display,
)
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> RuntimeEvent:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "event_type": EventType.AGENT_RUN_STARTED,
        "aggregate_type": AggregateType.AGENT_RUN,
        "aggregate_id": "ar-1",
        "correlation_id": "corr-1",
        "source": EventSource.CLAUDE,
        "actor": "claude",
        "visibility": Visibility.OPERATOR,
        "payload": {"agent": "claude", "role": "implementation"},
        "occurred_at": now_iso(),
        "conversation_id": 1,
        "agent_run_id": 1,
    }
    merged = {**defaults, **overrides}
    return RuntimeEvent(**merged)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> RuntimeEventStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return RuntimeEventStore(ledger._conn)


# ---------------------------------------------------------------------------
# build_runtime_status
# ---------------------------------------------------------------------------


def test_build_runtime_status_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status = build_runtime_status(store, conversation_id=None)
    assert status.conversation_id is None


def test_build_runtime_status_no_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    status = build_runtime_status(store, conversation_id=1)
    assert status.status == "idle"
    assert status.conversation_id == 1


def test_build_runtime_status_with_running_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_STARTED,
            payload={"agent": "claude", "role": "implementation"},
        )
    )
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_ACTIVITY,
            payload={"source": "stream_event"},
            agent_run_id=1,
        )
    )

    status = build_runtime_status(store, conversation_id=1)
    assert status.active_agent == "claude"
    assert status.active_agent_run_id == 1
    assert status.total_events == 2
    assert len(status.agents) == 1
    assert status.agents[0].status == "running"


def test_build_runtime_status_captures_phase(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.RUN_PHASE_CHANGED,
            payload={"phase": "running_implementation"},
        )
    )

    status = build_runtime_status(store, conversation_id=1)
    assert status.phase == "running_implementation"


def test_build_runtime_status_accumulates_tokens(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.MODEL_USAGE_UPDATED,
            payload={"input_tokens": 500, "output_tokens": 200},
        )
    )
    store.append(
        _make_event(
            event_type=EventType.MODEL_USAGE_UPDATED,
            payload={"input_tokens": 300, "output_tokens": 100},
        )
    )

    status = build_runtime_status(store, conversation_id=1)
    assert status.token_input == 800
    assert status.token_output == 300


def test_build_runtime_status_tracks_last_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.RUN_STARTED,
            visibility=Visibility.USER,
        )
    )
    store.append(
        _make_event(
            event_type=EventType.COMMAND_STARTED,
            visibility=Visibility.OPERATOR,
        )
    )

    status = build_runtime_status(store, conversation_id=1)
    assert status.last_event_type == EventType.COMMAND_STARTED
    # last_user_event only tracks USER visibility, not OPERATOR
    assert status.last_user_event == EventType.RUN_STARTED


def test_build_runtime_status_completed_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(event_type=EventType.RUN_STARTED, agent_run_id=1)
    )
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_COMPLETED,
            agent_run_id=1,
            payload={"agent": "claude"},
        )
    )
    store.append(_make_event(event_type=EventType.RUN_COMPLETED))

    status = build_runtime_status(store, conversation_id=1)
    assert status.status == "completed"
    assert status.active_agent == ""  # No active agent after completion


# ---------------------------------------------------------------------------
# build_runtime_trace
# ---------------------------------------------------------------------------


def test_build_runtime_trace_returns_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        store.append(
            _make_event(
                event_type=EventType.AGENT_RUN_ACTIVITY,
                payload={"n": i},
                visibility=Visibility.USER,
            )
        )

    trace = build_runtime_trace(store, conversation_id=1, limit=10)
    assert len(trace.events) == 5
    assert trace.total_events == 5
    assert not trace.truncated


def test_build_runtime_trace_truncates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(30):
        store.append(
            _make_event(
                event_type=EventType.AGENT_RUN_ACTIVITY,
                payload={"n": i},
                visibility=Visibility.USER,
            )
        )

    trace = build_runtime_trace(store, conversation_id=1, limit=10)
    assert len(trace.events) == 10
    assert trace.truncated
    assert trace.total_events == 30


def test_build_runtime_trace_filters_internal_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_ACTIVITY,
            visibility=Visibility.INTERNAL,
        )
    )
    store.append(
        _make_event(
            event_type=EventType.RUN_STARTED,
            visibility=Visibility.USER,
        )
    )

    # Default: filters out internal
    trace = build_runtime_trace(store, conversation_id=1)
    event_types = [e["event_type"] for e in trace.events]
    assert EventType.AGENT_RUN_ACTIVITY not in event_types
    assert EventType.RUN_STARTED in event_types


def test_build_runtime_trace_operator_includes_operator(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_ACTIVITY,
            visibility=Visibility.OPERATOR,
        )
    )
    store.append(
        _make_event(
            event_type=EventType.RUN_STARTED,
            visibility=Visibility.USER,
        )
    )

    trace = build_runtime_trace(
        store, conversation_id=1, visibility_filter="operator"
    )
    event_types = [e["event_type"] for e in trace.events]
    assert len(event_types) == 2


def test_build_runtime_trace_all_shows_everything(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_ACTIVITY,
            visibility=Visibility.INTERNAL,
        )
    )
    store.append(
        _make_event(
            event_type=EventType.RUN_STARTED,
            visibility=Visibility.USER,
        )
    )

    trace = build_runtime_trace(store, conversation_id=1, visibility_filter="all")
    assert len(trace.events) == 2


# ---------------------------------------------------------------------------
# Redacted event payload display
# ---------------------------------------------------------------------------


def test_trace_sanitizes_sensitive_payload_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.RUN_STARTED,
            visibility=Visibility.USER,
            payload={"auth_token": "should-be-redacted", "phase": "analysis"},
        )
    )

    trace = build_runtime_trace(store, conversation_id=1)
    payload = trace.events[0]["payload"]
    assert payload["auth_token"] == "[REDACTED]"
    assert payload["phase"] == "analysis"


# ---------------------------------------------------------------------------
# format_status_display
# ---------------------------------------------------------------------------


def test_format_status_display_idle() -> None:
    status = RuntimeStatus(conversation_id=1, status="idle", now=now_iso())
    result = format_status_display(status)
    assert "空闲" in result
    assert "1" in result


def test_format_status_display_running() -> None:
    status = RuntimeStatus(
        conversation_id=1,
        status="running",
        phase="running_implementation",
        active_agent="claude",
        active_agent_run_id=42,
        last_event_type=EventType.AGENT_RUN_ACTIVITY,
        last_event_id=100,
        idle_seconds=15.0,
        hard_elapsed_seconds=120.0,
        hard_timeout_seconds=600,
        token_input=1000,
        token_output=500,
        total_events=25,
        now=now_iso(),
    )
    result = format_status_display(status)
    assert "运行中" in result
    assert "Claude 实施" in result
    assert "claude" in result
    assert "#42" in result
    assert "15秒" in result  # idle seconds
    assert "2分" in result  # hard elapsed (120s = 2m)
    assert "1,000" in result  # token input
    assert "500" in result  # token output
    assert "25" in result  # total events


def test_format_status_display_no_conversation() -> None:
    result = format_status_display(
        RuntimeStatus(conversation_id=None, now=now_iso())
    )
    assert "活跃" in result or "对话" in result


# ---------------------------------------------------------------------------
# format_trace_display
# ---------------------------------------------------------------------------


def test_format_trace_display_empty() -> None:
    trace = RuntimeTrace(conversation_id=1)
    result = format_trace_display(trace)
    assert "暂无" in result


def test_format_trace_display_with_events() -> None:
    trace = RuntimeTrace(
        conversation_id=1,
        events=[
            {
                "id": 10,
                "event_type": EventType.RUN_STARTED,
                "source": "orchestrator",
                "occurred_at": "2026-05-18T10:00:00+00:00",
                "payload": {"phase": "analysis"},
            },
            {
                "id": 11,
                "event_type": EventType.AGENT_RUN_STARTED,
                "source": "claude",
                "occurred_at": "2026-05-18T10:01:00+00:00",
                "payload": {"agent": "claude"},
            },
        ],
    )
    result = format_trace_display(trace)
    assert "#10" in result
    assert "#11" in result
    assert "运行开始" in result
    assert "Agent 启动" in result


# ---------------------------------------------------------------------------
# compute_timeout_explanation
# ---------------------------------------------------------------------------


def test_compute_timeout_explanation_no_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    expl = compute_timeout_explanation(store, agent_run_id=99, timeout_type="idle", threshold_seconds=300)
    assert expl.agent_run_id == 99
    assert expl.timeout_type == "idle"
    assert "no events" in expl.reason


def test_compute_timeout_explanation_with_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_STARTED,
            agent_run_id=42,
            payload={"agent": "claude"},
        )
    )
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_ACTIVITY,
            agent_run_id=42,
            payload={"source": "stream_event"},
        )
    )

    expl = compute_timeout_explanation(store, agent_run_id=42, timeout_type="hard", threshold_seconds=600)
    assert expl.agent_name == "claude"
    assert expl.agent_run_id == 42
    assert expl.timeout_type == "hard"
    assert expl.threshold_seconds == 600
    assert expl.last_event_type == EventType.AGENT_RUN_ACTIVITY


def test_format_timeout_explanation() -> None:
    expl = TimeoutExplanation(
        agent_name="claude",
        agent_run_id=42,
        timeout_type="hard",
        last_event_id=100,
        last_event_type=EventType.AGENT_RUN_ACTIVITY,
        last_event_at="2026-05-18T10:00:00+00:00",
        elapsed_idle_seconds=5.0,
        elapsed_hard_seconds=610.0,
        threshold_seconds=600,
    )
    result = format_timeout_explanation(expl)
    assert "claude" in result
    assert "#42" in result
    assert "10分" in result  # 610s = 10m
    assert "硬超时" in result


# ---------------------------------------------------------------------------
# find_non_terminal_agent_runs
# ---------------------------------------------------------------------------


def test_find_non_terminal_finds_running_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_make_event(event_type=EventType.AGENT_RUN_STARTED, agent_run_id=10))
    store.append(_make_event(event_type=EventType.AGENT_RUN_ACTIVITY, agent_run_id=10))

    ids = find_non_terminal_agent_runs(store)
    assert 10 in ids


def test_find_non_terminal_excludes_completed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_make_event(event_type=EventType.AGENT_RUN_STARTED, agent_run_id=10))
    store.append(_make_event(event_type=EventType.AGENT_RUN_ACTIVITY, agent_run_id=10))
    store.append(_make_event(event_type=EventType.AGENT_RUN_COMPLETED, agent_run_id=10))

    ids = find_non_terminal_agent_runs(store)
    assert 10 not in ids


def test_find_non_terminal_excludes_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_make_event(event_type=EventType.AGENT_RUN_STARTED, agent_run_id=20))
    store.append(_make_event(event_type=EventType.AGENT_RUN_FAILED, agent_run_id=20))

    ids = find_non_terminal_agent_runs(store)
    assert 20 not in ids


def test_find_non_terminal_multiple_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_make_event(event_type=EventType.AGENT_RUN_STARTED, agent_run_id=1))
    store.append(_make_event(event_type=EventType.AGENT_RUN_STARTED, agent_run_id=2))
    store.append(_make_event(event_type=EventType.AGENT_RUN_COMPLETED, agent_run_id=2))
    store.append(_make_event(event_type=EventType.AGENT_RUN_STARTED, agent_run_id=3))
    store.append(_make_event(event_type=EventType.AGENT_RUN_ORPHANED, agent_run_id=3))

    ids = find_non_terminal_agent_runs(store)
    assert 1 in ids  # Started, no terminal
    assert 2 not in ids  # Completed
    assert 3 not in ids  # Orphaned


# ---------------------------------------------------------------------------
# build_recovery_events
# ---------------------------------------------------------------------------


def test_build_recovery_events_structure() -> None:
    events = build_recovery_events(
        orphaned_agent_run_ids=[10, 20],
        conversation_id=1,
        correlation_id="recovery-test",
    )

    event_types = [e.event_type for e in events]
    assert EventType.SYSTEM_STARTED in event_types
    assert EventType.SYSTEM_RECOVERY_STARTED in event_types
    assert EventType.AGENT_RUN_ORPHANED in event_types
    assert EventType.SYSTEM_RECOVERY_COMPLETED in event_types

    # Check ordering: system.started -> recovery.started -> orphan(x2) -> recovery.completed
    assert event_types[0] == EventType.SYSTEM_STARTED
    assert event_types[1] == EventType.SYSTEM_RECOVERY_STARTED
    assert event_types[2] == EventType.AGENT_RUN_ORPHANED
    assert event_types[3] == EventType.AGENT_RUN_ORPHANED
    assert event_types[4] == EventType.SYSTEM_RECOVERY_COMPLETED

    # All events share the same correlation_id
    for ev in events:
        assert ev.correlation_id == "recovery-test"

    # Orphan events carry the correct agent_run_id
    orphan_events = [e for e in events if e.event_type == EventType.AGENT_RUN_ORPHANED]
    orphan_ids = [e.agent_run_id for e in orphan_events]
    assert 10 in orphan_ids
    assert 20 in orphan_ids


def test_build_recovery_events_empty_orphans() -> None:
    events = build_recovery_events(
        orphaned_agent_run_ids=[],
        conversation_id=1,
    )
    event_types = [e.event_type for e in events]
    # Should still emit system.started, recovery.started, recovery.completed
    assert EventType.SYSTEM_STARTED in event_types
    assert EventType.SYSTEM_RECOVERY_STARTED in event_types
    assert EventType.SYSTEM_RECOVERY_COMPLETED in event_types
    assert EventType.AGENT_RUN_ORPHANED not in event_types


# ---------------------------------------------------------------------------
# append_recovery_events
# ---------------------------------------------------------------------------


def test_append_recovery_events_persists(tmp_path: Path) -> None:
    store = _store(tmp_path)

    persisted = append_recovery_events(
        store,
        orphaned_agent_run_ids=[42],
        conversation_id=1,
        correlation_id="recovery-test-2",
    )

    assert len(persisted) == 4  # system.started, recovery.started, orphan(1), recovery.completed
    for ev in persisted:
        assert ev.id > 0
        # Verify we can read them back
        loaded = store.get_by_id(ev.id)
        assert loaded.event_type == ev.event_type


# ---------------------------------------------------------------------------
# RuntimeStatus fields
# ---------------------------------------------------------------------------


def test_runtime_status_idle_clock_increases(tmp_path: Path) -> None:
    """Idle clock should be > 0 when last event is in the past."""
    store = _store(tmp_path)
    # Append an event with occurred_at in the past
    store.append(
        _make_event(
            event_type=EventType.AGENT_RUN_ACTIVITY,
            occurred_at="2026-05-18T00:00:00+00:00",
            visibility=Visibility.USER,
        )
    )

    status = build_runtime_status(store, conversation_id=1)
    assert status.idle_seconds > 0


def test_runtime_status_hard_clock_reads_first_event(tmp_path: Path) -> None:
    """Hard clock should be based on the first run event's timestamp."""
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.RUN_STARTED,
            occurred_at="2026-05-18T00:00:00+00:00",
        )
    )

    status = build_runtime_status(store, conversation_id=1)
    assert status.hard_elapsed_seconds > 0


# ---------------------------------------------------------------------------
# No events fallback
# ---------------------------------------------------------------------------


def test_build_runtime_trace_falls_back_to_operator_events(tmp_path: Path) -> None:
    """When there are no USER events, operator events should be shown."""
    store = _store(tmp_path)
    store.append(
        _make_event(
            event_type=EventType.RUN_STARTED,
            visibility=Visibility.OPERATOR,
        )
    )

    trace = build_runtime_trace(store, conversation_id=1)
    assert len(trace.events) == 1
    assert trace.events[0]["event_type"] == EventType.RUN_STARTED
