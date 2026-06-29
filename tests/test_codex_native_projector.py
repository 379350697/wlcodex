from pathlib import Path

from wlcodex.codex_native.projector import NativeCodexEventProjector
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.db import Ledger
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventSource, EventType


def _stores(
    tmp_path: Path,
) -> tuple[NativeCodexSessionStore, RuntimeEventStore]:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return NativeCodexSessionStore(ledger), RuntimeEventStore(ledger._conn)


def test_projector_creates_session_and_text_delta(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)

    projected = projector.project_notification(
        "item/agentMessage/delta",
        {
            "threadId": "thread_123",
            "turnId": "turn_456",
            "delta": "hello",
            "item": {"id": "item_789"},
        },
    )

    session = session_store.get_by_thread_id("thread_123")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)

    assert projected == events
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
    ]
    raw_frame = runtime_store.get_provider_raw_frame(events[0].payload["raw_frame_id"])
    assert raw_frame.raw_payload["delta"] == "hello"
    event = events[1]
    assert event.actor == "codex_native"
    assert event.source == EventSource.CODEX
    assert event.payload["native_thread_id"] == "thread_123"
    assert event.payload["native_turn_id"] == "turn_456"
    assert event.payload["source_kind"] == "codex_native"
    assert event.payload["provider"] == "codex"
    assert event.payload["provider_engine"] == "app-server"
    assert event.payload["delta"] == "hello"
    assert event.payload["itemId"] == "item_789"


def test_projector_updates_last_turn_from_turn_started(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)

    events = projector.project_notification(
        "turn/started",
        {"thread": {"id": "thread_nested"}, "turn": {"id": "turn_nested"}},
    )

    session = session_store.get_by_thread_id("thread_nested")
    assert session is not None
    assert session.last_turn_id == "turn_nested"
    assert session.status == "running"
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
    ]
    assert events[1].payload["native_thread_id"] == "thread_nested"
    assert events[1].payload["native_turn_id"] == "turn_nested"


def test_projector_ignores_notification_without_thread_id(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)

    events = projector.project_notification("turn/started", {"turnId": "turn_only"})

    assert events == []


def test_projector_text_delta_does_not_downgrade_running_status(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    projector.project_notification(
        "turn/started",
        {"threadId": "thread_status", "turnId": "turn_1"},
    )

    projector.project_notification(
        "item/agentMessage/delta",
        {
            "threadId": "thread_status",
            "turnId": "turn_1",
            "delta": "still running",
        },
    )

    session = session_store.get_by_thread_id("thread_status")
    assert session is not None
    assert session.status == "running"


def test_projector_turn_completed_preserves_failed_status(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    projector.project_notification(
        "turn/started",
        {"threadId": "thread_failed", "turnId": "turn_1"},
    )

    projector.project_notification(
        "turn/completed",
        {"threadId": "thread_failed", "turn": {"id": "turn_1", "status": "failed"}},
    )

    session = session_store.get_by_thread_id("thread_failed")
    assert session is not None
    assert session.status == "failed"
    assert session_store._ledger.get_agent_run(session.agent_run_id).status == "failed"


def test_projector_history_preserves_in_progress_turn_status(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)

    projector.project_history(
        {
            "thread": {
                "id": "thread_active_history",
                "status": {"type": "active"},
                "turns": [
                    {
                        "id": "turn_active_history",
                        "status": "inProgress",
                        "items": [],
                    }
                ],
            }
        }
    )

    session = session_store.get_by_thread_id("thread_active_history")
    assert session is not None
    assert session.status == "running"
    assert session_store._ledger.get_agent_run(session.agent_run_id).status == "running"


def test_projector_history_dedupes_sensitive_redacted_items(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    detail = {
        "thread": {
            "id": "thread_redacted_history",
            "turns": [
                {
                    "id": "turn_redacted_history",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "id": "item_secret",
                            "text": "open https://example.test/?token=secret-token",
                        }
                    ],
                }
            ],
        }
    }

    first = projector.project_history(detail)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    second = projector.project_history(detail)

    assert [event.event_type for event in first] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
    ]
    assert second == []
    session = session_store.get_by_thread_id("thread_redacted_history")
    assert session is not None
    assert len(runtime_store.list_by_agent_run(session.agent_run_id)) == 7


def test_projector_maps_reasoning_and_patch_notifications(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)

    projector.project_notification(
        "item/reasoning/textDelta",
        {
            "threadId": "thread_extra",
            "turnId": "turn_extra",
            "delta": "thinking",
            "item": {"id": "reasoning_item"},
        },
    )
    projector.project_notification(
        "item/fileChange/patchUpdated",
        {
            "threadId": "thread_extra",
            "turnId": "turn_extra",
            "patch": "@@ patch",
            "item": {"id": "patch_item"},
        },
    )

    session = session_store.get_by_thread_id("thread_extra")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.MODEL_REASONING_DELTA,
        EventType.PROVIDER_RAW_FRAME,
        EventType.DIFF_UPDATED,
    ]
    assert events[1].payload["delta"] == "thinking"
    assert events[3].payload["diff"] == "@@ patch"


def test_projector_preserves_approval_resolved_turn_id(tmp_path: Path) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)

    projected = projector.project_approval_resolved(
        native_thread_id="thread_approval",
        native_turn_id="turn_approval",
        request_id="request_1",
        response={"decision": "accept"},
    )

    session = session_store.get_by_thread_id("thread_approval")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert projected == events
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.APPROVAL_RESOLVED,
    ]
    assert events[1].payload["native_thread_id"] == "thread_approval"
    assert events[1].payload["native_turn_id"] == "turn_approval"
    assert events[1].payload["codexRequestId"] == "request_1"


def test_projector_reads_official_thread_turns_and_deduplicates_history(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    detail = {
        "thread": {
            "id": "thread_real",
            "name": "Real Codex task",
            "cwd": "/repo",
            "source": "vscode",
            "status": {"type": "notLoaded"},
            "turns": [
                {
                    "id": "turn_real",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "id": "user_1",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "do the work",
                                    "text_elements": [],
                                }
                            ],
                        },
                        {
                            "type": "agentMessage",
                            "id": "agent_1",
                            "text": "done",
                            "phase": "final",
                            "memoryCitation": None,
                        },
                        {
                            "type": "commandExecution",
                            "id": "cmd_1",
                            "command": "pytest",
                            "cwd": "/repo",
                            "processId": None,
                            "source": "exec",
                            "status": "completed",
                            "commandActions": [],
                            "aggregatedOutput": "passed",
                            "exitCode": 0,
                            "durationMs": 12,
                        },
                    ],
                }
            ],
        }
    }

    first = projector.project_history(detail)
    second = projector.project_history(detail)

    session = session_store.get_by_thread_id("thread_real")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert second == []
    assert first == events
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.PROVIDER_RAW_FRAME,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
        EventType.PROVIDER_RAW_FRAME,
        EventType.COMMAND_STARTED,
        EventType.PROVIDER_RAW_FRAME,
        EventType.COMMAND_OUTPUT_DELTA,
        EventType.PROVIDER_RAW_FRAME,
        EventType.COMMAND_COMPLETED,
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
    ]
    assert events[3].payload["text"] == "do the work"
    assert events[5].payload["delta"] == "done"
    assert events[10].payload["delta"] == "passed"


def test_projector_maps_official_plan_history_items_to_plan_activity(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    projector = NativeCodexEventProjector(session_store, runtime_store)
    detail = {
        "thread": {
            "id": "thread_plan_history",
            "turns": [
                {
                    "id": "turn_plan_history",
                    "status": "completed",
                    "items": [
                        {
                            "type": "plan",
                            "id": "plan_item_1",
                            "text": "# WLCodex Plan\n\n## Summary\nRender the plan card.",
                        }
                    ],
                }
            ],
        }
    }

    first = projector.project_history(detail)
    second = projector.project_history(detail)

    session = session_store.get_by_thread_id("thread_plan_history")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert second == []
    assert first == events
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
    ]
    assert events[3].payload["action"] == "plan_updated"
    assert events[3].payload["plan"].startswith("# WLCodex Plan")
    assert events[3].payload["itemId"] == "plan_item_1"
    assert events[3].payload["native_thread_id"] == "thread_plan_history"
    assert events[3].payload["native_turn_id"] == "turn_plan_history"


def test_projector_restores_dedupe_keys_from_persisted_events(
    tmp_path: Path,
) -> None:
    session_store, runtime_store = _stores(tmp_path)
    first_projector = NativeCodexEventProjector(session_store, runtime_store)
    payload = {
        "threadId": "thread_persisted",
        "turnId": "turn_persisted",
        "delta": "already projected",
        "item": {"id": "item_persisted"},
    }

    first = first_projector.project_notification("item/agentMessage/delta", payload)
    restarted_projector = NativeCodexEventProjector(session_store, runtime_store)
    second = restarted_projector.project_notification("item/agentMessage/delta", payload)

    session = session_store.get_by_thread_id("thread_persisted")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert len(first) == 3
    assert second == []
    assert len(events) == 3
    assert events[1].payload["delta"] == "already projected"
