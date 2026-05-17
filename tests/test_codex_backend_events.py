"""Backend event translation tests."""

import pytest

from wlcodex.codex_backend import (
    AppServerCodexBackend,
    BackendEvent,
    FakeCodexBackend,
)


@pytest.mark.asyncio
async def test_backend_start_turn_sends_turn_start() -> None:
    backend = FakeCodexBackend()
    thread_id = await backend.create_thread("/tmp/demo")
    turn_id = await backend.start_turn(thread_id, "Fix bug")

    assert turn_id.startswith("fake-turn-")
    assert backend.turns == [(thread_id, "Fix bug")]


@pytest.mark.asyncio
async def test_backend_continue_turn_sends_resume_and_start() -> None:
    backend = FakeCodexBackend()
    thread_id = await backend.create_thread("/tmp/demo")
    await backend.start_turn(thread_id, "Initial")
    turn_id = await backend.continue_turn(thread_id, "Continue prompt")

    assert turn_id is not None
    assert len(backend.turns) == 2


@pytest.mark.asyncio
async def test_backend_steer_turn_does_not_add_turn() -> None:
    backend = FakeCodexBackend()
    thread_id = await backend.create_thread("/tmp/demo")
    await backend.start_turn(thread_id, "Initial")

    pre_count = len(backend.turns)
    await backend.steer_turn(thread_id, "fake-turn-1", "Steer prompt")

    assert len(backend.turns) == pre_count
    assert len(backend.steers) == 1
    assert backend.steers[0] == (thread_id, "fake-turn-1", "Steer prompt")


@pytest.mark.asyncio
async def test_backend_injects_events_for_testing() -> None:
    backend = FakeCodexBackend()
    backend.inject_event(BackendEvent("test_event", {"key": "value"}))
    backend.inject_event(BackendEvent("turn_started", {"threadId": "t1"}))

    events = []
    async for ev in backend.events():
        events.append(ev)

    assert len(events) == 2
    assert events[0].event_type == "test_event"
    assert events[1].event_type == "turn_started"


@pytest.mark.asyncio
async def test_backend_resolve_approval_records_decision() -> None:
    backend = FakeCodexBackend()
    await backend.resolve_approval("req-5", {"decision": "accept"})

    assert backend._approval_resolutions == [("req-5", {"decision": "accept"})]


@pytest.mark.asyncio
async def test_backend_interrupt_turn() -> None:
    backend = FakeCodexBackend()
    await backend.interrupt_turn("thread-1", "turn-1")

    assert backend._interrupts == [("thread-1", "turn-1")]


@pytest.mark.asyncio
async def test_backend_fork_thread() -> None:
    backend = FakeCodexBackend()
    new_id = await backend.fork_thread("thread-parent", "/tmp/demo")

    assert new_id.startswith("fake-fork-")
    assert new_id != "thread-parent"


@pytest.mark.asyncio
async def test_backend_archive_thread() -> None:
    backend = FakeCodexBackend()
    await backend.archive_thread("thread-old")
    assert "thread-old" in backend._archive_thread_ids


@pytest.mark.asyncio
async def test_app_server_legacy_exec_approval_emits_normalized_event() -> None:
    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")

    await backend._on_legacy_exec_approval_request(
        {
            "conversationId": "thread-1",
            "callId": "call-1",
            "command": ["python3", "probe.py"],
            "reason": "needs write access",
        },
        "req-legacy",
    )

    event = backend._event_queue.pop(0)
    assert event.event_type == "approval_requested"
    assert event.payload["threadId"] == "thread-1"
    assert event.payload["codexRequestId"] == "req-legacy"
    assert event.payload["codexItemId"] == "call-1"
    assert event.payload["kind"] == "command"
    assert event.payload["responseSchema"] == "legacy_review_decision"
    assert "python3 probe.py" in str(event.payload["summary"])


@pytest.mark.asyncio
async def test_app_server_legacy_patch_approval_emits_normalized_event() -> None:
    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")

    await backend._on_legacy_patch_approval_request(
        {
            "conversationId": "thread-1",
            "callId": "patch-1",
            "fileChanges": {"README.md": {"type": "update", "unified_diff": "..."}},
        },
        "req-patch",
    )

    event = backend._event_queue.pop(0)
    assert event.event_type == "approval_requested"
    assert event.payload["threadId"] == "thread-1"
    assert event.payload["codexRequestId"] == "req-patch"
    assert event.payload["codexItemId"] == "patch-1"
    assert event.payload["kind"] == "file_change"
    assert event.payload["responseSchema"] == "legacy_review_decision"
    assert "README.md" in str(event.payload["summary"])
