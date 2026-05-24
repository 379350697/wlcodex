"""Backend event translation tests."""

import asyncio
import json
from pathlib import Path

import pytest

from wlcodex.codex_backend import (
    AppServerCodexBackend,
    BackendEvent,
    FakeCodexBackend,
)

pytestmark = pytest.mark.slow


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
async def test_app_server_connect_disables_transport_message_cap(monkeypatch) -> None:
    import websockets

    connect_calls: list[dict[str, object]] = []

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: asyncio.Queue[str | None] = asyncio.Queue()

        async def send(self, raw: str) -> None:
            request = json.loads(raw)
            if "id" not in request:
                return
            self.messages.put_nowait(json.dumps({
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {},
            }))

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            item = await self.messages.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def close(self) -> None:
            self.messages.put_nowait(None)

    async def fake_connect(_endpoint: str, **kwargs: object) -> FakeWebSocket:
        connect_calls.append(kwargs)
        return FakeWebSocket()

    monkeypatch.setattr(websockets, "connect", fake_connect)

    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.1,
    )

    await backend._ensure_client()
    await backend.close()

    assert connect_calls
    assert connect_calls[0]["max_size"] is None


@pytest.mark.asyncio
async def test_recv_loop_truncates_oversized_command_output_delta() -> None:
    large_delta = "x" * (2 * 1024 * 1024 + 2048)
    raw_message = json.dumps({
        "jsonrpc": "2.0",
        "method": "item/commandExecution/outputDelta",
        "params": {
            "itemId": "item-1",
            "delta": large_delta,
        },
    })

    class OneMessageWebSocket:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return raw_message

    class RecordingClient:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []
            self.closed = False

        async def receive_message(self, message: dict[str, object]) -> None:
            self.messages.append(message)

        async def close(self) -> None:
            self.closed = True

    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")
    client = RecordingClient()

    await backend._recv_loop(OneMessageWebSocket(), client)

    assert client.closed is False
    params = client.messages[0]["params"]
    assert isinstance(params, dict)
    delta = params["delta"]
    assert isinstance(delta, str)
    assert len(delta) < len(large_delta)
    assert "WLCodex truncated command output" in delta


@pytest.mark.asyncio
async def test_app_server_prompt_fails_immediately_when_receive_loop_breaks() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.1,
        codex_prompt_idle_timeout_seconds=0.2,
    )

    class FailingWebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            raise RuntimeError("frame with 1069393 bytes exceeds limit of 1048576 bytes")

    class ClosingClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    closing_client = ClosingClient()

    async def create_thread(_workspace_path: str) -> str:
        return "target-thread"

    async def start_turn(_thread_id: str, _prompt: str) -> str:
        asyncio.create_task(backend._recv_loop(FailingWebSocket(), closing_client))
        return "target-turn"

    async def interrupt_turn(_thread_id: str, _turn_id: str) -> None:
        raise AssertionError("transport failures should wake the prompt before idle timeout")

    backend.create_thread = create_thread
    backend.start_turn = start_turn
    backend.interrupt_turn = interrupt_turn

    with pytest.raises(RuntimeError, match="Codex backend transport disconnected"):
        await backend.send_codex_prompt(
            "/tmp/demo",
            "prompt",
        )

    assert closing_client.closed is True
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
    instructions = str(thread_params["developerInstructions"])
    assert "禁止 rg -S ." in instructions
    assert "无边界全文搜索" in instructions
    assert "outputSchema" in turn_params


@pytest.mark.asyncio
async def test_app_server_read_only_analysis_prompt_inherits_high_trust_policy() -> None:
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
                return {"threadId": "readonly-thread"}
            if method == "turn/start":
                backend._emit(BackendEvent("agent_message_delta", {
                    "threadId": "readonly-thread",
                    "turnId": "readonly-turn",
                    "delta": "只读结论",
                }))
                backend._emit(BackendEvent("turn_completed", {
                    "threadId": "readonly-thread",
                    "turnId": "readonly-turn",
                }))
                return {"turnId": "readonly-turn"}
            raise AssertionError(method)

    backend._client = RecordingClient()

    result = await backend.send_codex_prompt(
        "/tmp/demo",
        "prompt",
        interaction_mode="read_only_analysis",
    )

    assert result == "只读结论"
    _, thread_params = requests[0]
    _, turn_params = requests[1]
    assert "Codex 分析/核验" in str(thread_params["developerInstructions"])
    instructions = str(thread_params["developerInstructions"])
    assert "禁止创建、修改、删除" not in instructions
    assert "真实执行必要的查询" in instructions
    assert turn_params["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "networkAccess": False,
        "writableRoots": [],
    }
    assert "outputSchema" not in turn_params


@pytest.mark.asyncio
async def test_app_server_send_codex_analysis_prompt_inherits_high_trust_policy() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        approval_policy="never",
        sandbox="danger-full-access",
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
    _, thread_params = requests[0]
    _, turn_params = requests[1]
    assert thread_params["approvalPolicy"] == "never"
    assert thread_params["sandbox"] == "danger-full-access"
    assert turn_params["approvalPolicy"] == "never"
    assert turn_params["sandboxPolicy"] == {"type": "dangerFullAccess"}


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
async def test_app_server_send_codex_analysis_prompt_pauses_hard_timeout_during_approval() -> None:
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.02,
        codex_prompt_idle_timeout_seconds=0.02,
        codex_analysis_hard_timeout_seconds=0.04,
    )
    interrupts: list[tuple[str, str]] = []

    async def create_prompt_thread(
        _workspace_path: str, _interaction_mode: str
    ) -> str:
        return "target-thread"

    async def start_prompt_turn(
        _thread_id: str, _prompt: str, _interaction_mode: str
    ) -> str:
        async def emit_after_human_wait() -> None:
            backend._emit(BackendEvent("approval_requested", {
                "threadId": "target-thread",
                "turnId": "target-turn",
                "codexRequestId": "approval-1",
            }))
            await asyncio.sleep(0.07)
            backend._emit(BackendEvent("agent_message_delta", {
                "threadId": "target-thread",
                "turnId": "target-turn",
                "delta": "approved path",
            }))
            backend._emit(BackendEvent("turn_completed", {
                "threadId": "target-thread",
                "turnId": "target-turn",
            }))

        asyncio.create_task(emit_after_human_wait())
        return "target-turn"

    async def interrupt_turn(thread_id: str, turn_id: str) -> None:
        interrupts.append((thread_id, turn_id))

    backend._create_prompt_thread = create_prompt_thread
    backend._start_prompt_turn = start_prompt_turn
    backend.interrupt_turn = interrupt_turn

    result = await backend.send_codex_prompt(
        "/tmp/demo",
        "prompt",
        interaction_mode="analysis",
    )

    assert result == "approved path"
    assert interrupts == []
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


# --- Codex token usage event recording ---


def test_task_service_records_usage_event_v2_token_usage(tmp_path: Path) -> None:
    """TaskService records usage_event from v2 protocol tokenUsage.last notification."""
    from wlcodex.db import Ledger
    from wlcodex.config import WorkspaceConfig
    from wlcodex.task_service import TaskService

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    ws = WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)
    service = TaskService(ledger, [ws])

    task = ledger.create_task(
        workspace_alias="demo",
        workspace_path=str(tmp_path),
        title="Codex usage test",
        codex_thread_id="thread-v2",
        parent_task_id=None,
    )
    ledger.set_thread_id(task.id, "thread-v2")

    # Simulate v2 tokenUsage/updated notification
    event = BackendEvent("token_usage_updated", {
        "threadId": "thread-v2",
        "turnId": "turn-v2-1",
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
                "inputTokens": 2000,
                "cachedInputTokens": 500,
                "outputTokens": 1200,
                "reasoningOutputTokens": 300,
                "totalTokens": 3500,
            },
        },
    })

    service.apply_backend_event(event)

    # Verify legacy compatibility is maintained
    updated = ledger.get_task(task.id)
    assert updated.token_input == 2000
    assert updated.token_output == 1200

    # Verify usage_event was recorded
    usage_events = ledger.list_usage_events(task_id=task.id)
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue.agent == "codex"
    assert ue.source == "exact"
    assert ue.input_tokens == 2000
    assert ue.cached_input_tokens == 500
    assert ue.output_tokens == 1200
    assert ue.reasoning_output_tokens == 300
    assert ue.total_tokens == 3500
    assert ue.external_thread_id == "thread-v2"
    assert ue.external_turn_id == "turn-v2-1"


def test_task_service_records_usage_event_legacy_flat_payload(tmp_path: Path) -> None:
    """TaskService records usage_event from legacy flat inputTokens/outputTokens."""
    from wlcodex.db import Ledger
    from wlcodex.config import WorkspaceConfig
    from wlcodex.task_service import TaskService

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    ws = WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)
    service = TaskService(ledger, [ws])

    task = ledger.create_task(
        workspace_alias="demo",
        workspace_path=str(tmp_path),
        title="Codex legacy test",
        codex_thread_id="thread-legacy",
        parent_task_id=None,
    )
    ledger.set_thread_id(task.id, "thread-legacy")

    # Simulate legacy tokenUsage/updated notification
    event = BackendEvent("token_usage_updated", {
        "threadId": "thread-legacy",
        "turnId": "turn-legacy-1",
        "inputTokens": 1000,
        "outputTokens": 500,
    })

    service.apply_backend_event(event)

    updated = ledger.get_task(task.id)
    assert updated.token_input == 1000
    assert updated.token_output == 500

    usage_events = ledger.list_usage_events(task_id=task.id)
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue.source == "estimated"
    assert ue.input_tokens == 1000
    assert ue.output_tokens == 500


def test_task_service_token_usage_recording_failure_is_silent(tmp_path: Path) -> None:
    """Token usage recording failure must not affect Codex running (no raise)."""
    from wlcodex.db import Ledger
    from wlcodex.config import WorkspaceConfig
    from wlcodex.task_service import TaskService

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    ws = WorkspaceConfig(alias="demo", path=tmp_path, allow_write=True)
    service = TaskService(ledger, [ws])

    task = ledger.create_task(
        workspace_alias="demo",
        workspace_path=str(tmp_path),
        title="Faulty recording test",
        codex_thread_id="thread-noisy",
        parent_task_id=None,
    )
    ledger.set_thread_id(task.id, "thread-noisy")

    # Event with non-numeric values should still process gracefully
    event = BackendEvent("token_usage_updated", {
        "threadId": "thread-noisy",
        "turnId": "turn-noisy",
        "tokenUsage": {
            "last": {
                "inputTokens": "not-a-number",
                "outputTokens": None,
                "cachedInputTokens": None,
                "reasoningOutputTokens": None,
                "totalTokens": "also-not-number",
            },
        },
    })

    # Must not raise
    service.apply_backend_event(event)

    # Legacy fields should default to 0
    updated = ledger.get_task(task.id)
    assert updated.token_input == 0


# ---------------------------------------------------------------------------
# Runtime event callback integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_callback_receives_emitted_events() -> None:
    """BackendEvent subscribers and runtime callback both receive events."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    callbacks: list[BackendEvent] = []

    def record(event: BackendEvent) -> None:
        callbacks.append(event)

    backend.set_runtime_event_callback(record)

    backend._emit(BackendEvent("thread_started", {"threadId": "th-1"}))
    backend._emit(BackendEvent("turn_started", {"threadId": "th-1", "turnId": "tu-1"}))

    assert len(callbacks) == 2
    assert callbacks[0].event_type == "thread_started"
    assert callbacks[1].event_type == "turn_started"


@pytest.mark.asyncio
async def test_runtime_callback_does_not_block_existing_subscribers() -> None:
    """Existing BackendEvent subscribers still receive events when callback is set."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    global_events = backend.events()

    backend.set_runtime_event_callback(lambda e: None)
    backend._emit(BackendEvent("turn_started", {
        "threadId": "th-1", "turnId": "tu-1",
    }))

    event = await asyncio.wait_for(anext(global_events), timeout=0.02)
    assert event.event_type == "turn_started"
    await global_events.aclose()


@pytest.mark.asyncio
async def test_runtime_callback_error_does_not_crash_backend() -> None:
    """A crashing callback must not break the main event fan-out."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )

    def crash(_event: BackendEvent) -> None:
        raise RuntimeError("callback explosion")

    backend.set_runtime_event_callback(crash)

    # Must not raise — callback errors are caught and logged.
    backend._emit(BackendEvent("thread_started", {"threadId": "th-1"}))

    # Bridge event queue still received the event.
    assert len(backend._bridge_event_queue) == 1
    assert backend._bridge_event_queue[0].event_type == "thread_started"


@pytest.mark.asyncio
async def test_runtime_callback_error_does_not_prevent_subscriber_delivery() -> None:
    """A crashing callback must not prevent subscriber queues from receiving events."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    global_events = backend.events()

    def crash(_event: BackendEvent) -> None:
        raise RuntimeError("callback explosion")

    backend.set_runtime_event_callback(crash)
    backend._emit(BackendEvent("turn_started", {
        "threadId": "th-1", "turnId": "tu-1",
    }))

    event = await asyncio.wait_for(anext(global_events), timeout=0.02)
    assert event.event_type == "turn_started"
    await global_events.aclose()


@pytest.mark.asyncio
async def test_runtime_callback_not_set_has_no_effect() -> None:
    """Without a callback, _emit works exactly as before."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )
    global_events = backend.events()

    backend._emit(BackendEvent("agent_message_delta", {
        "threadId": "th-1",
        "turnId": "tu-1",
        "delta": "hello",
    }))

    event = await asyncio.wait_for(anext(global_events), timeout=0.02)
    assert event.payload["delta"] == "hello"
    await global_events.aclose()


@pytest.mark.asyncio
async def test_approval_events_still_work_with_runtime_callback() -> None:
    """Approval semantics are unchanged when runtime callback is active."""
    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")
    runtime_events: list[BackendEvent] = []
    backend.set_runtime_event_callback(runtime_events.append)

    await backend._on_legacy_exec_approval_request(
        {
            "conversationId": "thread-1",
            "callId": "call-1",
            "command": ["python3", "probe.py"],
            "reason": "needs write access",
        },
        "req-rt",
    )

    # Bridge queue has the approval event for TaskService
    assert len(backend._bridge_event_queue) == 1
    bridge_event = backend._bridge_event_queue[0]
    assert bridge_event.event_type == "approval_requested"
    assert bridge_event.payload["codexRequestId"] == "req-rt"

    # Runtime callback also received the event
    assert len(runtime_events) == 1
    assert runtime_events[0].event_type == "approval_requested"
    assert runtime_events[0].payload["codexRequestId"] == "req-rt"


@pytest.mark.asyncio
async def test_codex_runtime_source_wired_through_callback() -> None:
    """Full pipeline: BackendEvent → CodexRuntimeSource → RuntimeEvent."""
    from wlcodex.codex_runtime_source import CodexRuntimeSource

    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")
    runtime_events: list[object] = []

    source = CodexRuntimeSource(
        correlation_id="corr-wired",
        agent_run_id=42,
        conversation_id=1,
    )

    def wire(event: BackendEvent) -> None:
        runtime_events.extend(source.map_event(event))

    backend.set_runtime_event_callback(wire)

    # Simulate a full turn lifecycle through the real _emit path
    backend._emit(BackendEvent("thread_started", {"threadId": "th-wired"}))
    backend._emit(BackendEvent("turn_started", {
        "threadId": "th-wired", "turnId": "tu-wired",
    }))
    backend._emit(BackendEvent("agent_message_delta", {
        "threadId": "th-wired", "turnId": "tu-wired",
        "delta": "Codex 分析结果：通过验收",
        "item": {"id": "msg-1", "type": "agentMessage"},
    }))
    backend._emit(BackendEvent("token_usage_updated", {
        "threadId": "th-wired",
        "turnId": "tu-wired",
        "tokenUsage": {
            "last": {"inputTokens": 500, "outputTokens": 200, "totalTokens": 700},
        },
    }))
    backend._emit(BackendEvent("turn_completed", {
        "threadId": "th-wired", "turnId": "tu-wired",
    }))

    assert len(runtime_events) == 5

    from wlcodex.runtime_events import EventType
    types = [getattr(e, "event_type") for e in runtime_events]
    assert types == [
        EventType.AGENT_RUN_ACTIVITY,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.MODEL_TEXT_DELTA,
        EventType.MODEL_USAGE_UPDATED,
        EventType.AGENT_RUN_ACTIVITY,
    ]

    # All events share the correlation context
    for e in runtime_events:
        assert getattr(e, "correlation_id") == "corr-wired"
        assert getattr(e, "agent_run_id") == 42
        assert getattr(e, "source") == "codex"


# ---------------------------------------------------------------------------
# resolve_approval emits approval_resolved BackendEvent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_approval_emits_approval_resolved_backend_event() -> None:
    """resolve_approval emits approval_resolved so runtime source can map it."""
    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")

    class _FakeClient:
        def resolve_server_request(self, request_id: str, response: dict) -> None:
            pass

    backend._client = _FakeClient()

    runtime_events: list[BackendEvent] = []
    backend.set_runtime_event_callback(runtime_events.append)

    await backend.resolve_approval("req-1", {"decision": "accept"})

    assert len(runtime_events) == 1
    assert runtime_events[0].event_type == "approval_resolved"
    assert runtime_events[0].payload["codexRequestId"] == "req-1"
    assert runtime_events[0].payload["response"] == {"decision": "accept"}

    # Bridge queue also received it
    assert backend._bridge_event_queue[0].event_type == "approval_resolved"


@pytest.mark.asyncio
async def test_resolve_approval_with_runtime_source_full_pipeline() -> None:
    """resolve_approval → approval.resolved RuntimeEvent via CodexRuntimeSource."""
    from wlcodex.codex_runtime_source import CodexRuntimeSource

    backend = AppServerCodexBackend(endpoint="ws://127.0.0.1:17431")

    class _FakeClient:
        def resolve_server_request(self, request_id: str, response: dict) -> None:
            pass

    backend._client = _FakeClient()

    runtime_events: list[object] = []
    source = CodexRuntimeSource(
        correlation_id="corr-appr",
        agent_run_id=7,
        conversation_id=1,
    )

    def wire(event: BackendEvent) -> None:
        runtime_events.extend(source.map_event(event))

    backend.set_runtime_event_callback(wire)

    await backend.resolve_approval("req-1", {"decision": "accept"})

    assert len(runtime_events) == 1
    e = runtime_events[0]
    assert getattr(e, "event_type") == "approval.resolved"
    assert getattr(e, "aggregate_type") == "approval"
    assert getattr(e, "aggregate_id") == "req-1"
    assert getattr(e, "payload")["codexRequestId"] == "req-1"
    assert getattr(e, "payload")["decision"] == "accept"

    # Existing FakeCodexBackend resolve_approval test still works independently
    fake = FakeCodexBackend()
    await fake.resolve_approval("req-5", {"decision": "accept"})
    assert fake._approval_resolutions == [("req-5", {"decision": "accept"})]


# --- steer_thread tests (real AppServerCodexBackend, no websocket) ---


@pytest.mark.asyncio
async def test_steer_thread_delegates_to_steer_turn_with_active_turn_id():
    """steer_thread uses the active turn id from _on_turn_started to call steer_turn."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )

    # Simulate turn/started notification — populates _active_turn_ids
    await backend._on_turn_started({"threadId": "thr-1", "turn": {"id": "turn-1"}})
    assert backend._active_turn_ids == {"thr-1": "turn-1"}

    # Capture steer_turn calls
    steer_calls: list[tuple] = []

    async def fake_steer_turn(thread_id: str, turn_id: str, prompt: str) -> None:
        steer_calls.append((thread_id, turn_id, prompt))

    backend.steer_turn = fake_steer_turn  # type: ignore[method-assign]

    await backend.steer_thread("thr-1", "hello")

    assert steer_calls == [("thr-1", "turn-1", "hello")], (
        f"Expected steer_turn('thr-1', 'turn-1', 'hello'), got {steer_calls}"
    )


@pytest.mark.asyncio
async def test_steer_thread_raises_value_error_when_no_active_turn():
    """steer_thread must raise ValueError when no active turn exists for the thread."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )

    assert "thr-unknown" not in backend._active_turn_ids

    with pytest.raises(ValueError, match="No active turn found"):
        await backend.steer_thread("thr-unknown", "hello")


@pytest.mark.asyncio
async def test_steer_thread_raises_value_error_after_turn_completed():
    """After _on_turn_completed clears the active turn, steer_thread must raise."""
    backend = AppServerCodexBackend(
        endpoint="ws://127.0.0.1:17431",
        request_timeout_seconds=0.3,
    )

    # Turn starts
    await backend._on_turn_started({"threadId": "thr-2", "turn": {"id": "turn-2"}})
    assert backend._active_turn_ids == {"thr-2": "turn-2"}

    # Turn completes — clears the active turn
    await backend._on_turn_completed({"threadId": "thr-2", "turn": {"id": "turn-2"}})
    assert "thr-2" not in backend._active_turn_ids

    with pytest.raises(ValueError, match="No active turn found"):
        await backend.steer_thread("thr-2", "hello")
