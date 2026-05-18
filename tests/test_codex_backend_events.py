"""Backend event translation tests."""

import asyncio

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
async def test_app_server_send_codex_prompt_reports_created_thread_before_turn() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    callbacks: list[str] = []
    start_seen: list[list[str]] = []

    async def create_thread(_workspace_path: str) -> str:
        return "target-thread"

    async def start_turn(_thread_id: str, _prompt: str) -> str:
        start_seen.append(list(callbacks))
        backend._emit(BackendEvent("agent_message_delta", {
            "threadId": "target-thread",
            "turnId": "target-turn",
            "delta": "ok",
        }))
        backend._emit(BackendEvent("turn_completed", {
            "threadId": "target-thread",
            "turnId": "target-turn",
        }))
        return "target-turn"

    backend.create_thread = create_thread
    backend.start_turn = start_turn

    result = await backend.send_codex_prompt(
        "/tmp/demo",
        "prompt",
        on_thread_created=callbacks.append,
    )

    assert result == "ok"
    assert callbacks == ["target-thread"]
    assert start_seen == [["target-thread"]]
    assert backend._event_subscribers == []


@pytest.mark.asyncio
async def test_app_server_send_codex_prompt_uses_independent_turn_subscription() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    global_events = backend.events()

    async def create_thread(_workspace_path: str) -> str:
        return "target-thread"

    async def start_turn(_thread_id: str, _prompt: str) -> str:
        backend._emit(BackendEvent("turn_started", {
            "threadId": "other-thread",
            "turnId": "other-turn",
        }))
        backend._emit(BackendEvent("turn_started", {
            "threadId": "target-thread",
            "turnId": "target-turn",
        }))
        backend._emit(BackendEvent("agent_message_delta", {
            "threadId": "target-thread",
            "turnId": "target-turn",
            "delta": "ok",
        }))
        backend._emit(BackendEvent("turn_completed", {
            "threadId": "target-thread",
            "turnId": "target-turn",
        }))
        return "target-turn"

    backend.create_thread = create_thread
    backend.start_turn = start_turn

    result = await backend.send_codex_prompt("/tmp/demo", "prompt")

    assert result == "ok"
    assert backend._event_subscribers == []
    assert (await anext(global_events)).payload["threadId"] == "other-thread"
    assert (await anext(global_events)).payload["threadId"] == "target-thread"
    assert (await anext(global_events)).payload["delta"] == "ok"
    assert (await anext(global_events)).event_type == "turn_completed"
    await global_events.aclose()


@pytest.mark.asyncio
async def test_app_server_events_wake_waiting_consumers() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    global_events = backend.events()
    next_event = asyncio.create_task(anext(global_events))

    await asyncio.sleep(0)
    backend._emit(BackendEvent("turn_started", {
        "threadId": "thread-1",
        "turnId": "turn-1",
    }))

    event = await asyncio.wait_for(next_event, timeout=0.02)

    assert event.event_type == "turn_started"
    await global_events.aclose()


@pytest.mark.asyncio
async def test_app_server_send_codex_prompt_not_starved_by_global_events() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )

    async def create_thread(_workspace_path: str) -> str:
        return "target-thread"

    async def start_turn(_thread_id: str, _prompt: str) -> str:
        backend._emit(BackendEvent("turn_started", {
            "threadId": "target-thread",
            "turnId": "target-turn",
        }))
        backend._emit(BackendEvent("agent_message_delta", {
            "threadId": "target-thread",
            "turnId": "target-turn",
            "delta": "wlcodex",
        }))
        global_events = backend.events()
        assert (await anext(global_events)).event_type == "turn_started"
        assert (await anext(global_events)).payload["delta"] == "wlcodex"
        await global_events.aclose()
        backend._emit(BackendEvent("agent_message_delta", {
            "threadId": "target-thread",
            "turnId": "target-turn",
            "delta": " telegram live ok",
        }))
        backend._emit(BackendEvent("turn_completed", {
            "threadId": "target-thread",
            "turnId": "target-turn",
        }))
        return "target-turn"

    backend.create_thread = create_thread
    backend.start_turn = start_turn

    result = await backend.send_codex_prompt("/tmp/demo", "prompt")

    assert result == "wlcodex telegram live ok"
    assert backend._event_subscribers == []


@pytest.mark.asyncio
async def test_app_server_send_codex_prompt_cleans_subscription_on_start_error() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )

    async def create_thread(_workspace_path: str) -> str:
        return "target-thread"

    async def start_turn(_thread_id: str, _prompt: str) -> str:
        raise RuntimeError("start failed")

    backend.create_thread = create_thread
    backend.start_turn = start_turn

    with pytest.raises(RuntimeError, match="start failed"):
        await backend.send_codex_prompt("/tmp/demo", "prompt")

    assert backend._event_subscribers == []


@pytest.mark.asyncio
async def test_app_server_send_codex_analysis_prompt_uses_planning_overrides() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    requests: list[tuple[str, dict[str, object]]] = []

    class RecordingClient:
        async def request(
            self, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            requests.append((method, params or {}))
            if method == "thread/start":
                return {"threadId": "analysis-thread"}
            if method == "turn/start":
                backend._emit(BackendEvent("agent_message_delta", {
                    "threadId": "analysis-thread",
                    "turnId": "analysis-turn",
                    "delta": "{\"summary\":\"ok\"}",
                }))
                backend._emit(BackendEvent("turn_completed", {
                    "threadId": "analysis-thread",
                    "turnId": "analysis-turn",
                }))
                return {"turnId": "analysis-turn"}
            raise AssertionError(method)

    backend._client = RecordingClient()

    result = await backend.send_codex_prompt(
        "/tmp/demo",
        "prompt",
        interaction_mode="analysis",
    )

    assert result == "{\"summary\":\"ok\"}"
    thread_method, thread_params = requests[0]
    turn_method, turn_params = requests[1]
    assert thread_method == "thread/start"
    assert thread_params["approvalPolicy"] == "on-request"
    assert thread_params["sandbox"] == "workspace-write"
    assert "可以调用 skill" in str(thread_params["developerInstructions"])
    assert "不要抢 Claude 的代码实现职责" in str(thread_params["developerInstructions"])
    assert thread_params["config"] == {
        "model": "gpt-5.5",
        "model_reasoning_effort": "xhigh",
        "model_reasoning_summary": "none",
        "model_verbosity": "high",
    }
    assert thread_params["model"] == "gpt-5.5"
    assert turn_method == "turn/start"
    assert turn_params["effort"] == "xhigh"
    assert turn_params["model"] == "gpt-5.5"
    assert turn_params["approvalPolicy"] == "on-request"
    assert turn_params["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "networkAccess": False,
        "writableRoots": [],
    }
    assert "outputSchema" in turn_params


@pytest.mark.asyncio
async def test_app_server_send_codex_prompt_interrupts_and_fails_on_timeout() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.02,
    )
    interrupts: list[tuple[str, str]] = []

    async def create_thread(_workspace_path: str) -> str:
        return "target-thread"

    async def start_turn(_thread_id: str, _prompt: str) -> str:
        backend._emit(BackendEvent("agent_message_delta", {
            "threadId": "target-thread",
            "turnId": "target-turn",
            "delta": "partial analysis",
        }))
        return "target-turn"

    async def interrupt_turn(thread_id: str, turn_id: str) -> None:
        interrupts.append((thread_id, turn_id))

    backend.create_thread = create_thread
    backend.start_turn = start_turn
    backend.interrupt_turn = interrupt_turn

    with pytest.raises(TimeoutError, match="did not complete"):
        await backend.send_codex_prompt("/tmp/demo", "prompt")

    assert interrupts == [("target-thread", "target-turn")]
    assert backend._event_subscribers == []


@pytest.mark.asyncio
async def test_app_server_send_codex_analysis_prompt_refreshes_idle_timeout_on_activity() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.02,
        codex_prompt_idle_timeout_seconds=0.03,
        codex_analysis_hard_timeout_seconds=0.2,
    )

    async def create_prompt_thread(
        _workspace_path: str, _interaction_mode: str
    ) -> str:
        return "target-thread"

    async def start_prompt_turn(
        _thread_id: str, _prompt: str, _interaction_mode: str
    ) -> str:
        async def emit_later() -> None:
            await asyncio.sleep(0.02)
            backend._emit(BackendEvent("agent_message_delta", {
                "threadId": "target-thread",
                "turnId": "target-turn",
                "delta": "still ",
            }))
            await asyncio.sleep(0.02)
            backend._emit(BackendEvent("agent_message_delta", {
                "threadId": "target-thread",
                "turnId": "target-turn",
                "delta": "working",
            }))
            backend._emit(BackendEvent("turn_completed", {
                "threadId": "target-thread",
                "turnId": "target-turn",
            }))

        asyncio.create_task(emit_later())
        return "target-turn"

    backend._create_prompt_thread = create_prompt_thread
    backend._start_prompt_turn = start_prompt_turn

    result = await backend.send_codex_prompt(
        "/tmp/demo",
        "prompt",
        interaction_mode="analysis",
    )

    assert result == "still working"
    assert backend._event_subscribers == []


@pytest.mark.asyncio
async def test_app_server_send_codex_analysis_prompt_interrupts_on_idle_timeout() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.02,
        codex_prompt_idle_timeout_seconds=0.02,
        codex_analysis_hard_timeout_seconds=0.2,
    )
    interrupts: list[tuple[str, str]] = []

    async def create_prompt_thread(
        _workspace_path: str, _interaction_mode: str
    ) -> str:
        return "target-thread"

    async def start_prompt_turn(
        _thread_id: str, _prompt: str, _interaction_mode: str
    ) -> str:
        backend._emit(BackendEvent("agent_message_delta", {
            "threadId": "target-thread",
            "turnId": "target-turn",
            "delta": "partial analysis",
        }))
        return "target-turn"

    async def interrupt_turn(thread_id: str, turn_id: str) -> None:
        interrupts.append((thread_id, turn_id))

    backend._create_prompt_thread = create_prompt_thread
    backend._start_prompt_turn = start_prompt_turn
    backend.interrupt_turn = interrupt_turn

    with pytest.raises(TimeoutError, match="idle"):
        await backend.send_codex_prompt(
            "/tmp/demo",
            "prompt",
            interaction_mode="analysis",
        )

    assert interrupts == [("target-thread", "target-turn")]
    assert backend._event_subscribers == []


@pytest.mark.asyncio
async def test_app_server_send_codex_analysis_prompt_interrupts_on_hard_timeout() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.02,
        codex_prompt_idle_timeout_seconds=0.05,
        codex_analysis_hard_timeout_seconds=0.07,
    )
    interrupts: list[tuple[str, str]] = []

    async def create_prompt_thread(
        _workspace_path: str, _interaction_mode: str
    ) -> str:
        return "target-thread"

    async def start_prompt_turn(
        _thread_id: str, _prompt: str, _interaction_mode: str
    ) -> str:
        async def emit_activity() -> None:
            for _ in range(10):
                await asyncio.sleep(0.015)
                backend._emit(BackendEvent("agent_message_delta", {
                    "threadId": "target-thread",
                    "turnId": "target-turn",
                    "delta": ".",
                }))

        asyncio.create_task(emit_activity())
        return "target-turn"

    async def interrupt_turn(thread_id: str, turn_id: str) -> None:
        interrupts.append((thread_id, turn_id))

    backend._create_prompt_thread = create_prompt_thread
    backend._start_prompt_turn = start_prompt_turn
    backend.interrupt_turn = interrupt_turn

    with pytest.raises(TimeoutError, match="hard"):
        await backend.send_codex_prompt(
            "/tmp/demo",
            "prompt",
            interaction_mode="analysis",
        )

    assert interrupts == [("target-thread", "target-turn")]
    assert backend._event_subscribers == []


@pytest.mark.asyncio
async def test_app_server_send_codex_prompt_raises_on_failed_turn_status() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )

    async def create_thread(_workspace_path: str) -> str:
        return "target-thread"

    async def start_turn(_thread_id: str, _prompt: str) -> str:
        backend._emit(BackendEvent("turn_completed", {
            "threadId": "target-thread",
            "turnId": "target-turn",
            "turn": {
                "id": "target-turn",
                "status": "failed",
                "error": {"message": "analysis failed"},
            },
        }))
        return "target-turn"

    backend.create_thread = create_thread
    backend.start_turn = start_turn

    with pytest.raises(RuntimeError, match="analysis failed"):
        await backend.send_codex_prompt("/tmp/demo", "prompt")

    assert backend._event_subscribers == []


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

    event = backend._bridge_event_queue.pop(0)
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

    event = backend._bridge_event_queue.pop(0)
    assert event.event_type == "approval_requested"
    assert event.payload["threadId"] == "thread-1"
    assert event.payload["codexRequestId"] == "req-patch"
    assert event.payload["codexItemId"] == "patch-1"
    assert event.payload["kind"] == "file_change"
    assert event.payload["responseSchema"] == "legacy_review_decision"
    assert "README.md" in str(event.payload["summary"])
