from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from wlcodex.codex_native.controller import CodexNativeController
from wlcodex.codex_native.models import (
    NativeCodexControlResult,
    NativeCodexSession,
    NativeCodexStatus,
)
from wlcodex.codex_native.projector import _METHOD_TO_BACKEND_EVENT
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.db import Ledger
from wlcodex.jsonrpc import JsonRpcError
from wlcodex.native_timeline import NativeTimelineStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[dict[str, Any], str], Awaitable[None]]


class FakeNativeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.handlers: dict[str, NotificationHandler] = {}
        self.server_handlers: dict[str, ServerRequestHandler] = {}
        self.resolved_requests: list[tuple[str, dict[str, Any]]] = []
        self.sessions = [
            {
                "id": "thread-1",
                "title": "First task",
                "cwd": "/workspace/one",
                "sourceKind": "ide",
                "status": "idle",
            }
        ]
        self.details: dict[str, dict[str, Any]] = {
            "thread-1": {
                "thread": {
                    "id": "thread-1",
                    "title": "First task",
                    "cwd": "/workspace/one",
                    "sourceKind": "ide",
                    "status": "idle",
                },
                "turns": [],
            }
        }

    async def status(self) -> dict[str, Any]:
        self.calls.append(("status",))
        return {"connected": True}

    async def list_sessions(self, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("list_sessions", limit))
        return self.sessions

    async def read_session(
        self,
        thread: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        if include_turns:
            self.calls.append(("read_session", thread))
        else:
            self.calls.append(("read_session", thread, include_turns))
        return self.details[thread]

    async def attach_session(self, thread: str) -> dict[str, Any]:
        self.calls.append(("attach_session", thread))
        return self.details[thread]

    async def continue_session(
        self,
        thread: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
    ) -> str:
        if (
            model is None
            and effort is None
            and service_tier is None
            and images is None
            and approval_policy is None
            and approvals_reviewer is None
            and sandbox_policy is None
            and collaboration_mode is None
        ):
            self.calls.append(("continue_session", thread, prompt))
        else:
            if (
                approval_policy is None
                and approvals_reviewer is None
                and sandbox_policy is None
                and collaboration_mode is None
            ):
                self.calls.append(
                    (
                        "continue_session",
                        thread,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                    )
                )
            elif collaboration_mode is None:
                self.calls.append(
                    (
                        "continue_session",
                        thread,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                        approval_policy,
                        approvals_reviewer,
                        sandbox_policy,
                    )
                )
            else:
                self.calls.append(
                    (
                        "continue_session",
                        thread,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                        approval_policy,
                        approvals_reviewer,
                        sandbox_policy,
                        collaboration_mode,
                    )
                )
        detail = self.details.setdefault(thread, {"thread": {"id": thread}})
        detail_thread = detail.setdefault("thread", {"id": thread})
        if isinstance(detail_thread, dict):
            detail_thread["status"] = "active"
            detail_thread["turns"] = [{"id": "turn-2", "status": "running", "items": []}]
        return "turn-2"

    async def start_turn(
        self,
        thread: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, object] | None = None,
        collaboration_mode: dict[str, object] | None = None,
    ) -> str:
        if (
            model is None
            and effort is None
            and service_tier is None
            and images is None
            and approval_policy is None
            and approvals_reviewer is None
            and sandbox_policy is None
            and collaboration_mode is None
        ):
            self.calls.append(("start_turn", thread, prompt))
        else:
            if (
                approval_policy is None
                and approvals_reviewer is None
                and sandbox_policy is None
                and collaboration_mode is None
            ):
                self.calls.append(
                    ("start_turn", thread, prompt, model, effort, service_tier, images)
                )
            elif collaboration_mode is None:
                self.calls.append(
                    (
                        "start_turn",
                        thread,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                        approval_policy,
                        approvals_reviewer,
                        sandbox_policy,
                    )
                )
            else:
                self.calls.append(
                    (
                        "start_turn",
                        thread,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                        approval_policy,
                        approvals_reviewer,
                        sandbox_policy,
                        collaboration_mode,
                    )
                )
        detail = self.details.setdefault(thread, {"thread": {"id": thread}})
        detail_thread = detail.setdefault("thread", {"id": thread})
        if isinstance(detail_thread, dict):
            detail_thread["status"] = "active"
            detail_thread["turns"] = [{"id": "turn-2", "status": "running", "items": []}]
        return "turn-2"

    async def start_thread(
        self,
        cwd: str,
        *,
        model: str | None = None,
        service_tier: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox: str | None = None,
    ) -> dict[str, Any]:
        if approval_policy is None and approvals_reviewer is None and sandbox is None:
            self.calls.append(("start_thread", cwd, model, service_tier))
        else:
            self.calls.append(
                (
                    "start_thread",
                    cwd,
                    model,
                    service_tier,
                    approval_policy,
                    approvals_reviewer,
                    sandbox,
                )
            )
        thread = {
            "id": "thread-new",
            "title": "",
            "cwd": cwd,
            "sourceKind": "appServer",
            "status": "idle",
        }
        self.details["thread-new"] = {"thread": thread, "turns": []}
        return {
            "thread": thread,
            "model": model or "gpt-5.5",
            "serviceTier": service_tier,
            "cwd": cwd,
        }

    async def list_models(self) -> list[dict[str, Any]]:
        self.calls.append(("list_models",))
        return [{"model": "gpt-5.5", "displayName": "GPT-5.5"}]

    async def steer_turn(
        self,
        thread: str,
        turn: str,
        prompt: str,
        *,
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        if images is None:
            self.calls.append(("steer_turn", thread, turn, prompt))
        else:
            self.calls.append(("steer_turn", thread, turn, prompt, images))

    async def interrupt_turn(self, thread: str, turn: str) -> None:
        self.calls.append(("interrupt_turn", thread, turn))

    def register_notification_handler(
        self,
        method: str,
        handler: NotificationHandler,
    ) -> None:
        self.handlers[method] = handler

    def register_server_request_handler(
        self,
        method: str,
        handler: ServerRequestHandler,
    ) -> None:
        self.server_handlers[method] = handler

    def resolve_request(self, request_id: str, result: dict[str, Any]) -> None:
        self.resolved_requests.append((request_id, result))


def _controller(
    tmp_path: Path,
) -> tuple[CodexNativeController, FakeNativeClient, NativeCodexSessionStore, RuntimeEventStore]:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    session_store = NativeCodexSessionStore(ledger)
    runtime_store = RuntimeEventStore(ledger._conn)
    client = FakeNativeClient()
    return CodexNativeController(client, session_store, runtime_store), client, session_store, runtime_store


@pytest.mark.asyncio
async def test_controller_lists_and_maps_sessions(tmp_path: Path) -> None:
    controller, client, session_store, _runtime_store = _controller(tmp_path)
    client.sessions[0]["updatedAt"] = "1780291245"

    sessions = await controller.list_sessions(limit=5)

    assert client.calls == [("list_sessions", 5)]
    assert len(sessions) == 1
    session = sessions[0]
    assert isinstance(session, NativeCodexSession)
    assert session.native_thread_id == "thread-1"
    assert session.title == "First task"
    assert session.cwd == "/workspace/one"
    assert session.source_kind == "ide"
    assert session.status == "idle"
    assert session.agent_run_id > 0
    assert session.activity_at == "2026-06-01T05:20:45+00:00"
    assert session_store.get_by_thread_id("thread-1") == session


@pytest.mark.asyncio
async def test_controller_does_not_project_exception_names_as_model_metadata(
    tmp_path: Path,
) -> None:
    controller, client, session_store, _runtime_store = _controller(tmp_path)
    client.sessions[0]["model"] = "FileNotFoundError"

    sessions = await controller.list_sessions(limit=5)

    assert sessions[0].metadata == {}
    stored = session_store.get_by_thread_id("thread-1")
    assert stored is not None
    assert stored.metadata == {}


@pytest.mark.asyncio
async def test_controller_clears_cached_exception_name_model_metadata(
    tmp_path: Path,
) -> None:
    controller, client, session_store, _runtime_store = _controller(tmp_path)
    session_store.get_or_create_session(
        native_thread_id="thread-1",
        title="Cached task",
        metadata={"model": "FileNotFoundError"},
    )
    client.sessions[0]["model"] = "FileNotFoundError"

    await controller.list_sessions(limit=5)

    stored = session_store.get_by_thread_id("thread-1")
    assert stored is not None
    assert stored.metadata == {}


@pytest.mark.asyncio
async def test_native_recent_sessions_sort_by_official_activity_time(
    tmp_path: Path,
) -> None:
    controller, _client, session_store, _runtime_store = _controller(tmp_path)
    _client.sessions = [
        {
            "id": "thread-old",
            "title": "Old task",
            "cwd": "/workspace/old",
            "sourceKind": "ide",
            "status": "idle",
            "updatedAt": "1780290683",
        },
        {
            "id": "thread-new",
            "title": "New task",
            "cwd": "/workspace/new",
            "sourceKind": "ide",
            "status": "idle",
            "updatedAt": "1780291245",
        },
    ]

    await controller.list_sessions()
    session_store.update_session(native_thread_id="thread-old", status="running")

    recent = session_store.list_recent(limit=10)

    assert [session.native_thread_id for session in recent[:2]] == [
        "thread-new",
        "thread-old",
    ]
    assert recent[0].activity_at == "2026-06-01T05:20:45+00:00"
    assert recent[1].activity_at == "2026-06-01T05:11:23+00:00"


@pytest.mark.asyncio
async def test_controller_status_returns_error_status_when_native_client_fails(
    tmp_path: Path,
) -> None:
    class FailingStatusClient(FakeNativeClient):
        async def status(self) -> dict[str, Any]:
            raise RuntimeError("daemon unavailable")

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    controller = CodexNativeController(
        FailingStatusClient(),
        NativeCodexSessionStore(ledger),
        RuntimeEventStore(ledger._conn),
    )

    status = await controller.status()

    assert status == NativeCodexStatus(
        enabled=True,
        connected=False,
        remote_control_status="error",
        error="daemon unavailable",
    )


@pytest.mark.asyncio
async def test_controller_read_session_projects_turn_history(tmp_path: Path) -> None:
    controller, client, session_store, runtime_store = _controller(tmp_path)
    client.details["thread-history"] = {
        "thread": {
            "id": "thread-history",
            "title": "History task",
            "cwd": "/workspace/history",
            "sourceKind": "ide",
            "status": "done",
        },
        "turns": [
            {
                "id": "turn-history",
                "status": "completed",
                "items": [
                    {
                        "id": "item-message",
                        "type": "agentMessage",
                        "text": "historical answer",
                    }
                ],
            }
        ],
    }

    detail = await controller.read_session("thread-history")

    assert detail["thread"]["id"] == "thread-history"
    session = session_store.get_by_thread_id("thread-history")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_COMPLETED,
        EventType.MODEL_MESSAGE_COMPLETED,
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
    ]
    assert events[3].payload["text"] == "historical answer"


@pytest.mark.asyncio
async def test_controller_read_session_adds_native_status_from_jsonl(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    session_path = tmp_path / "rollout.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "thread-status"}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {"total_tokens": 140_000},
                                "model_context_window": 260_000,
                            },
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 2.0,
                                    "window_minutes": 300,
                                    "resets_at": 1_781_213_931,
                                },
                                "secondary": {
                                    "used_percent": 7.0,
                                    "window_minutes": 10_080,
                                    "resets_at": 1_781_746_761,
                                },
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client.details["thread-status"] = {
        "thread": {
            "id": "thread-status",
            "title": "Status task",
            "cwd": "/workspace/status",
            "path": str(session_path),
            "sourceKind": "ide",
            "status": "done",
        },
        "turns": [],
    }

    detail = await controller.read_session("thread-status")

    metadata = detail["thread"]["metadata"]
    assert metadata["context"]["used_tokens"] == 140_000
    assert metadata["context"]["total_tokens"] == 260_000
    assert metadata["context"]["remaining_percent"] == pytest.approx(
        (260_000 - 140_000) / 260_000 * 100
    )
    assert metadata["rate_limits"]["five_hour"]["remaining_percent"] == 98
    assert metadata["rate_limits"]["five_hour"]["reset_at"] == 1_781_213_931
    assert metadata["rate_limits"]["seven_day"]["remaining_percent"] == 93
    assert metadata["rate_limits"]["seven_day"]["reset_at"] == 1_781_746_761
    assert metadata["native_status"]["context"] == metadata["context"]


@pytest.mark.asyncio
async def test_controller_attach_session_refreshes_without_projecting_history(
    tmp_path: Path,
) -> None:
    controller, client, session_store, runtime_store = _controller(tmp_path)
    client.details["thread-live"] = {
        "thread": {
            "id": "thread-live",
            "title": "Live task",
            "cwd": "/workspace/live",
            "sourceKind": "ide",
            "status": "running",
            "turns": [
                {
                    "id": "turn-live",
                    "status": "running",
                    "items": [
                        {
                            "id": "message-live",
                            "type": "agentMessage",
                            "text": "live answer",
                        }
                    ],
                }
            ],
        }
    }

    first = await controller.attach_session("thread-live")
    client.details["thread-live"]["thread"]["turns"] = [
        {
            "id": "turn-next",
            "status": "running",
            "items": [
                {
                    "id": "message-next",
                    "type": "agentMessage",
                    "text": "new live answer",
                }
            ],
        }
    ]
    second = await controller.attach_session("thread-live")

    assert first == NativeCodexControlResult(
        native_thread_id="thread-live",
        agent_run_id=first.agent_run_id,
        turn_id="turn-live",
        active_turn_id="turn-live",
        turn_running=True,
        status="attached",
    )
    assert second.turn_id == "turn-next"
    assert second.active_turn_id == "turn-next"
    assert second.turn_running is True
    assert client.calls == [
        ("attach_session", "thread-live"),
        ("attach_session", "thread-live"),
    ]
    session = session_store.get_by_thread_id("thread-live")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert events == []


@pytest.mark.asyncio
async def test_controller_sync_session_projects_plan_history(
    tmp_path: Path,
) -> None:
    controller, client, session_store, runtime_store = _controller(tmp_path)
    client.details["thread-sync"] = {
        "thread": {
            "id": "thread-sync",
            "title": "Sync task",
            "cwd": "/workspace/sync",
            "sourceKind": "ide",
            "status": "running",
            "turns": [
                {
                    "id": "turn-sync",
                    "status": "completed",
                    "items": [
                        {
                            "id": "plan-sync",
                            "type": "plan",
                            "text": "# Synced Plan\n\n## Summary\nRender this plan.",
                        }
                    ],
                }
            ],
        }
    }

    result = await controller.sync_session("thread-sync")

    assert result == NativeCodexControlResult(
        native_thread_id="thread-sync",
        agent_run_id=result.agent_run_id,
        turn_id="turn-sync",
        active_turn_id="",
        turn_running=False,
        status="synced",
    )
    assert client.calls == [("read_session", "thread-sync")]
    session = session_store.get_by_thread_id("thread-sync")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
        EventType.PROVIDER_RAW_FRAME,
        EventType.AGENT_RUN_ACTIVITY,
    ]
    assert events[3].payload["action"] == "plan_updated"
    assert events[3].payload["plan"].startswith("# Synced Plan")
    assert events[3].payload["itemId"] == "plan-sync"
    assert events[3].payload["native_turn_id"] == "turn-sync"


@pytest.mark.asyncio
async def test_controller_continue_steer_interrupt(tmp_path: Path) -> None:
    controller, client, session_store, _runtime_store = _controller(tmp_path)
    await controller.list_sessions()

    continued = await controller.continue_session("thread-1", "continue")
    steered = await controller.steer_session("thread-1", "turn-2", "steer")
    interrupted = await controller.interrupt_session("thread-1", "turn-2")

    assert continued == NativeCodexControlResult(
        native_thread_id="thread-1",
        agent_run_id=continued.agent_run_id,
        turn_id="turn-2",
        active_turn_id="turn-2",
        turn_running=True,
    )
    assert continued.agent_run_id > 0
    assert steered.status == "ok"
    assert steered.turn_id == "turn-2"
    assert steered.agent_run_id == continued.agent_run_id
    assert interrupted.status == "ok"
    assert interrupted.turn_id == "turn-2"
    assert interrupted.agent_run_id == continued.agent_run_id
    assert session_store.get_by_thread_id("thread-1").status == "aborted"
    assert session_store.get_by_thread_id("thread-1").last_turn_id == "turn-2"
    assert client.calls == [
        ("list_sessions", 50),
        ("attach_session", "thread-1"),
        ("start_turn", "thread-1", "continue"),
        ("attach_session", "thread-1"),
        ("steer_turn", "thread-1", "turn-2", "steer"),
        ("interrupt_turn", "thread-1", "turn-2"),
    ]


@pytest.mark.asyncio
async def test_controller_continue_projects_sent_prompt_as_visible_user_message(
    tmp_path: Path,
) -> None:
    controller, _client, session_store, runtime_store = _controller(tmp_path)
    await controller.list_sessions()

    result = await controller.continue_session("thread-1", "show this on phone")

    session = session_store.get_by_thread_id("thread-1")
    assert session is not None
    assert result.turn_id == "turn-2"
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.USER_MESSAGE_RECEIVED,
    ]
    assert events[1].payload["text"] == "show this on phone"
    assert events[1].payload["native_thread_id"] == "thread-1"
    assert events[1].payload["native_turn_id"] == "turn-2"
    assert events[1].payload["provider"] == "codex"
    assert events[1].payload["itemId"] == "local-user-turn-2"


@pytest.mark.asyncio
async def test_controller_continue_seeds_native_timeline_before_history_sync(
    tmp_path: Path,
) -> None:
    controller, _client, _session_store, runtime_store = _controller(tmp_path)
    timeline_store = NativeTimelineStore(runtime_store._conn)
    runtime_store.add_projector(timeline_store.project_runtime_event)
    await controller.list_sessions()

    result = await controller.continue_session("thread-1", "show this immediately")

    events = timeline_store.list_item_events("codex", "thread-1", limit=10)
    assert result.turn_id == "turn-2"
    assert len(events) == 1
    assert events[0].kind == "user_message"
    assert events[0].payload["text"] == "show this immediately"
    assert events[0].payload["native_turn_id"] == "turn-2"


@pytest.mark.asyncio
async def test_controller_continue_refreshes_and_steers_current_official_active_turn(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    client.details["thread-active"] = {
        "thread": {
            "id": "thread-active",
            "title": "Active task",
            "cwd": "/workspace/active",
            "sourceKind": "ide",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
            "turns": [
                {
                    "id": "turn-active",
                    "status": "inProgress",
                    "items": [],
                }
            ],
        }
    }

    await controller.attach_session("thread-active")
    client.details["thread-active"]["thread"]["turns"] = [
        {"id": "turn-stale", "status": "completed", "startedAt": 100, "items": []},
        {"id": "turn-current", "status": "running", "startedAt": 200, "items": []},
    ]
    client.calls.clear()
    result = await controller.continue_session("thread-active", "keep going")

    assert result == NativeCodexControlResult(
        native_thread_id="thread-active",
        agent_run_id=result.agent_run_id,
        turn_id="turn-current",
        active_turn_id="turn-current",
        turn_running=True,
    )
    assert client.calls == [
        ("attach_session", "thread-active"),
        ("steer_turn", "thread-active", "turn-current", "keep going"),
    ]


@pytest.mark.asyncio
async def test_controller_continue_can_force_new_turn_when_current_turn_is_active(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    client.details["thread-active"] = {
        "thread": {
            "id": "thread-active",
            "title": "Active task",
            "cwd": "/workspace/active",
            "sourceKind": "ide",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
            "turns": [
                {
                    "id": "turn-active",
                    "status": "inProgress",
                    "items": [],
                }
            ],
        }
    }

    result = await controller.continue_session(
        "thread-active",
        "show this on native clients",
        force_new_turn=True,
    )

    assert result.turn_id == "turn-2"
    assert result.active_turn_id == "turn-2"
    assert client.calls == [
        ("start_turn", "thread-active", "show this on native clients"),
    ]


@pytest.mark.asyncio
async def test_controller_continue_uses_newest_active_turn_when_resume_order_is_stale(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    client.details["thread-active"] = {
        "thread": {
            "id": "thread-active",
            "title": "Active task",
            "cwd": "/workspace/active",
            "sourceKind": "ide",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
            "turns": [
                {
                    "id": "turn-current",
                    "status": "inProgress",
                    "startedAt": 200,
                    "items": [],
                },
                {
                    "id": "turn-stale-approval",
                    "status": "inProgress",
                    "startedAt": 100,
                    "items": [],
                },
            ],
        }
    }

    result = await controller.continue_session("thread-active", "keep going")

    assert result.turn_id == "turn-current"
    assert result.active_turn_id == "turn-current"
    assert client.calls == [
        ("attach_session", "thread-active"),
        ("steer_turn", "thread-active", "turn-current", "keep going"),
    ]


@pytest.mark.asyncio
async def test_controller_continue_starts_new_turn_when_only_stale_approval_is_active(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    client.details["thread-active"] = {
        "thread": {
            "id": "thread-active",
            "title": "Active task",
            "cwd": "/workspace/active",
            "sourceKind": "ide",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
            "turns": [
                {
                    "id": "turn-newer-completed",
                    "status": "completed",
                    "startedAt": 200,
                    "items": [],
                },
                {
                    "id": "turn-stale-approval",
                    "status": "inProgress",
                    "startedAt": 100,
                    "items": [],
                },
            ],
        }
    }

    result = await controller.continue_session("thread-active", "new work")

    assert result.turn_id == "turn-2"
    assert client.calls == [
        ("attach_session", "thread-active"),
        ("start_turn", "thread-active", "new work"),
    ]


@pytest.mark.asyncio
async def test_controller_steer_ignores_stale_expected_turn_after_refresh(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    client.details["thread-active"] = {
        "thread": {
            "id": "thread-active",
            "title": "Active task",
            "cwd": "/workspace/active",
            "sourceKind": "ide",
            "status": "active",
            "turns": [
                {"id": "turn-old", "status": "completed", "items": []},
                {"id": "turn-current", "status": "running", "items": []},
            ],
        }
    }

    await controller.attach_session("thread-active")
    client.details["thread-active"]["thread"]["turns"] = [
        {"id": "turn-old", "status": "completed", "startedAt": 100, "items": []},
        {
            "id": "turn-current",
            "status": "completed",
            "startedAt": 200,
            "items": [],
        },
        {
            "id": "turn-live-now",
            "status": "running",
            "startedAt": 300,
            "items": [],
        },
    ]
    client.calls.clear()
    result = await controller.steer_session(
        "thread-active",
        "turn-current",
        "adjust current turn",
    )

    assert result.turn_id == "turn-live-now"
    assert result.active_turn_id == "turn-live-now"
    assert result.turn_running is True
    assert client.calls == [
        ("attach_session", "thread-active"),
        ("steer_turn", "thread-active", "turn-live-now", "adjust current turn"),
    ]


@pytest.mark.asyncio
async def test_controller_steer_does_not_project_visible_user_message(
    tmp_path: Path,
) -> None:
    controller, client, session_store, runtime_store = _controller(tmp_path)
    client.details["thread-active"] = {
        "thread": {
            "id": "thread-active",
            "title": "Active task",
            "cwd": "/workspace/active",
            "sourceKind": "ide",
            "status": "active",
            "turns": [
                {"id": "turn-current", "status": "running", "items": []},
            ],
        }
    }

    result = await controller.steer_session(
        "thread-active",
        "turn-current",
        "adjust current turn",
    )

    assert result.turn_id == "turn-current"
    session = session_store.get_by_thread_id("thread-active")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == []


@pytest.mark.asyncio
async def test_controller_steer_retries_once_when_active_turn_changes_between_refresh_and_send(
    tmp_path: Path,
) -> None:
    class RacingTurnClient(FakeNativeClient):
        async def steer_turn(
            self,
            thread: str,
            turn: str,
            prompt: str,
            *,
            images: list[dict[str, Any]] | None = None,
        ) -> None:
            self.calls.append(("steer_turn", thread, turn, prompt))
            if turn == "turn-before-race":
                raise JsonRpcError(
                    -32000,
                    "expected active turn id `turn-before-race` but found `turn-after-race`",
                )

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    client = RacingTurnClient()
    client.details["thread-race"] = {
        "thread": {
            "id": "thread-race",
            "title": "Race",
            "cwd": "/workspace/race",
            "sourceKind": "ide",
            "status": "active",
            "turns": [{"id": "turn-before-race", "status": "running", "items": []}],
        }
    }
    controller = CodexNativeController(
        client,
        NativeCodexSessionStore(ledger),
        RuntimeEventStore(ledger._conn),
    )

    result = await controller.steer_session("thread-race", "turn-stale", "adjust")

    assert result.turn_id == "turn-after-race"
    assert result.active_turn_id == "turn-after-race"
    assert result.turn_running is True
    assert client.calls == [
        ("attach_session", "thread-race"),
        ("steer_turn", "thread-race", "turn-before-race", "adjust"),
        ("steer_turn", "thread-race", "turn-after-race", "adjust"),
    ]


@pytest.mark.asyncio
async def test_controller_continue_starts_new_turn_when_rpc_reports_older_stale_active_turn(
    tmp_path: Path,
) -> None:
    class StaleMismatchClient(FakeNativeClient):
        async def steer_turn(
            self,
            thread: str,
            turn: str,
            prompt: str,
            *,
            images: list[dict[str, Any]] | None = None,
        ) -> None:
            self.calls.append(("steer_turn", thread, turn, prompt))
            raise JsonRpcError(
                -32000,
                "expected active turn id `019e7987-fdb1-79e3-9243-e6c52f6f2ac5` "
                "but found `019e78af-e5d0-7643-9a23-3679652f923b`",
            )

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    client = StaleMismatchClient()
    client.details["thread-stale"] = {
        "thread": {
            "id": "thread-stale",
            "title": "Stale active",
            "cwd": "/workspace/stale",
            "sourceKind": "ide",
            "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
            "turns": [
                {
                    "id": "019e7987-fdb1-79e3-9243-e6c52f6f2ac5",
                    "status": "inProgress",
                    "startedAt": 200,
                    "items": [],
                },
                {
                    "id": "019e78af-e5d0-7643-9a23-3679652f923b",
                    "status": "inProgress",
                    "startedAt": 100,
                    "items": [],
                },
            ],
        }
    }
    controller = CodexNativeController(
        client,
        NativeCodexSessionStore(ledger),
        RuntimeEventStore(ledger._conn),
    )

    result = await controller.continue_session("thread-stale", "new current work")

    assert result.turn_id == "turn-2"
    assert client.calls == [
        ("attach_session", "thread-stale"),
        (
            "steer_turn",
            "thread-stale",
            "019e7987-fdb1-79e3-9243-e6c52f6f2ac5",
            "new current work",
        ),
        ("interrupt_turn", "thread-stale", "019e78af-e5d0-7643-9a23-3679652f923b"),
        ("start_turn", "thread-stale", "new current work"),
    ]


@pytest.mark.asyncio
async def test_controller_steer_starts_new_turn_when_thread_is_not_active(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    client.details["thread-done"] = {
        "thread": {
            "id": "thread-done",
            "title": "Done task",
            "cwd": "/workspace/done",
            "sourceKind": "ide",
            "status": "completed",
            "turns": [{"id": "turn-done", "status": "completed", "items": []}],
        }
    }

    result = await controller.steer_session(
        "thread-done",
        "turn-done",
        "new follow-up",
    )

    assert result.turn_id == "turn-2"
    assert client.calls == [
        ("attach_session", "thread-done"),
        ("start_turn", "thread-done", "new follow-up"),
    ]


@pytest.mark.asyncio
async def test_controller_passes_native_model_settings_and_images(tmp_path: Path) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    await controller.list_sessions()

    result = await controller.continue_session(
        "thread-1",
        "describe",
        model="gpt-5.5",
        effort="high",
        service_tier="fast",
        images=[{"url": "data:image/png;base64,abc"}],
    )
    steered = await controller.steer_session(
        "thread-1",
        "turn-2",
        "adjust",
        images=[{"url": "data:image/jpeg;base64,abc"}],
    )

    assert result.turn_id == "turn-2"
    assert steered.turn_id == "turn-2"
    assert client.calls == [
        ("list_sessions", 50),
        ("attach_session", "thread-1"),
        (
            "start_turn",
            "thread-1",
            "describe",
            "gpt-5.5",
            "high",
            "fast",
            [{"url": "data:image/png;base64,abc"}],
        ),
        ("attach_session", "thread-1"),
        (
            "steer_turn",
            "thread-1",
            "turn-2",
            "adjust",
            [{"url": "data:image/jpeg;base64,abc"}],
        ),
    ]


@pytest.mark.asyncio
async def test_controller_starts_new_project_session_with_model_settings(
    tmp_path: Path,
) -> None:
    controller, client, session_store, runtime_store = _controller(tmp_path)

    result = await controller.start_session(
        "/workspace/two",
        "start work",
        model="gpt-5.5",
        effort="medium",
        service_tier="fast",
        images=[{"url": "data:image/png;base64,abc"}],
    )

    session = session_store.get_by_thread_id("thread-new")
    assert result.native_thread_id == "thread-new"
    assert result.turn_id == "turn-2"
    assert result.turn_running is True
    assert session is not None
    assert session.cwd == "/workspace/two"
    assert session.metadata == {
        "model": "gpt-5.5",
        "effort": "medium",
        "service_tier": "fast",
    }
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.USER_MESSAGE_RECEIVED,
    ]
    assert events[1].payload["text"] == "start work"
    assert events[1].payload["native_thread_id"] == "thread-new"
    assert events[1].payload["native_turn_id"] == "turn-2"
    assert events[1].payload["itemId"] == "local-user-turn-2"
    assert client.calls == [
        ("start_thread", "/workspace/two", "gpt-5.5", "fast"),
        (
            "start_turn",
            "thread-new",
            "start work",
            "gpt-5.5",
            "medium",
            "fast",
            [{"url": "data:image/png;base64,abc"}],
        ),
    ]


@pytest.mark.asyncio
async def test_controller_passes_codex_permission_overrides_to_new_turns(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)
    await controller.list_sessions()

    await controller.continue_session(
        "thread-1",
        "continue with permissions",
        approval_policy="never",
        approvals_reviewer="auto_review",
        sandbox_policy={"type": "dangerFullAccess"},
    )
    await controller.start_session(
        "/workspace/two",
        "start with permissions",
        approval_policy="on-request",
        approvals_reviewer="auto_review",
        sandbox="workspace-write",
        sandbox_policy={"type": "workspaceWrite", "writableRoots": []},
    )

    assert client.calls == [
        ("list_sessions", 50),
        ("attach_session", "thread-1"),
        (
            "start_turn",
            "thread-1",
            "continue with permissions",
            None,
            None,
            None,
            None,
            "never",
            "auto_review",
            {"type": "dangerFullAccess"},
        ),
        (
            "start_thread",
            "/workspace/two",
            None,
            None,
            "on-request",
            "auto_review",
            "workspace-write",
        ),
        (
            "start_turn",
            "thread-new",
            "start with permissions",
            None,
            None,
            None,
            None,
            "on-request",
            "auto_review",
            {"type": "workspaceWrite", "writableRoots": []},
        ),
    ]


@pytest.mark.asyncio
async def test_controller_continue_session_updates_model_settings_metadata(
    tmp_path: Path,
) -> None:
    controller, _client, session_store, _runtime_store = _controller(tmp_path)
    await controller.list_sessions()

    await controller.continue_session(
        "thread-1",
        "continue with settings",
        model="gpt-5.5",
        effort="xhigh",
        service_tier="priority",
    )

    session = session_store.get_by_thread_id("thread-1")
    assert session is not None
    assert session.metadata == {
        "model": "gpt-5.5",
        "effort": "xhigh",
        "service_tier": "priority",
    }


@pytest.mark.asyncio
async def test_controller_start_session_retries_when_rollout_stays_briefly_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowRolloutClient(FakeNativeClient):
        def __init__(self) -> None:
            super().__init__()
            self.continue_failures = 0

        async def attach_session(self, thread: str) -> dict[str, Any]:
            self.calls.append(("attach_session", thread))
            raise JsonRpcError(-32600, f"no rollout found for thread id {thread}")

        async def start_turn(
            self,
            thread: str,
            prompt: str,
            *,
            model: str | None = None,
            effort: str | None = None,
            service_tier: str | None = None,
            images: list[dict[str, Any]] | None = None,
            **_extra: Any,
        ) -> str:
            self.calls.append(
                ("start_turn", thread, prompt, model, effort, service_tier, images)
            )
            if self.continue_failures < 6:
                self.continue_failures += 1
                raise JsonRpcError(
                    -32600,
                    f"no rollout found for thread id {thread}",
                )
            return "turn-after-slow-rollout"

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("wlcodex.codex_native.controller.asyncio.sleep", no_sleep)
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    client = SlowRolloutClient()
    controller = CodexNativeController(
        client,
        NativeCodexSessionStore(ledger),
        RuntimeEventStore(ledger._conn),
    )

    result = await controller.start_session(
        "/workspace/two",
        "start after slow rollout",
        model="gpt-5.5",
        effort="medium",
        service_tier="fast",
    )

    assert result.turn_id == "turn-after-slow-rollout"
    assert client.calls[0] == ("start_thread", "/workspace/two", "gpt-5.5", "fast")
    assert client.calls.count(("attach_session", "thread-new")) == 6
    start_turn_calls = [call for call in client.calls if call[0] == "start_turn"]
    assert len(start_turn_calls) == 7
    assert set(start_turn_calls) == {
        (
            "start_turn",
            "thread-new",
            "start after slow rollout",
            "gpt-5.5",
            "medium",
            "fast",
            None,
        )
    }


@pytest.mark.asyncio
async def test_controller_start_session_raises_when_rollout_never_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingRolloutClient(FakeNativeClient):
        async def attach_session(self, thread: str) -> dict[str, Any]:
            self.calls.append(("attach_session", thread))
            raise JsonRpcError(-32600, f"no rollout found for thread id {thread}")

        async def start_turn(
            self,
            thread: str,
            prompt: str,
            *,
            model: str | None = None,
            effort: str | None = None,
            service_tier: str | None = None,
            images: list[dict[str, Any]] | None = None,
            **_extra: Any,
        ) -> str:
            self.calls.append(
                ("start_turn", thread, prompt, model, effort, service_tier, images)
            )
            raise JsonRpcError(-32600, f"no rollout found for thread id {thread}")

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("wlcodex.codex_native.controller.asyncio.sleep", no_sleep)
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    client = MissingRolloutClient()
    controller = CodexNativeController(
        client,
        NativeCodexSessionStore(ledger),
        RuntimeEventStore(ledger._conn),
    )

    with pytest.raises(JsonRpcError, match="no rollout found"):
        await controller.start_session(
            "/workspace/two",
            "start later",
            model="gpt-5.5",
            effort="medium",
            service_tier="fast",
        )

    session = NativeCodexSessionStore(ledger).get_by_thread_id("thread-new")
    assert session is not None
    assert session.cwd == "/workspace/two"
    assert client.calls[0] == ("start_thread", "/workspace/two", "gpt-5.5", "fast")
    assert client.calls.count(("attach_session", "thread-new")) >= 1


@pytest.mark.asyncio
async def test_controller_start_session_retries_when_rollout_is_not_ready(
    tmp_path: Path,
) -> None:
    class RolloutRaceClient(FakeNativeClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        async def start_turn(
            self,
            thread: str,
            prompt: str,
            *,
            model: str | None = None,
            effort: str | None = None,
            service_tier: str | None = None,
            images: list[dict[str, Any]] | None = None,
            **_extra: Any,
        ) -> str:
            self.calls.append(
                ("start_turn", thread, prompt, model, effort, service_tier, images)
            )
            if not self.failed_once:
                self.failed_once = True
                raise JsonRpcError(
                    -32600,
                    f"no rollout found for thread id {thread}",
                )
            return "turn-after-rollout"

    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    client = RolloutRaceClient()
    controller = CodexNativeController(
        client,
        NativeCodexSessionStore(ledger),
        RuntimeEventStore(ledger._conn),
    )

    result = await controller.start_session(
        "/workspace/two",
        "start after rollout",
        model="gpt-5.5",
        effort="medium",
        service_tier="fast",
    )

    session = NativeCodexSessionStore(ledger).get_by_thread_id("thread-new")
    assert result.native_thread_id == "thread-new"
    assert result.turn_id == "turn-after-rollout"
    assert result.turn_running is True
    assert session is not None
    assert session.last_turn_id == "turn-after-rollout"
    assert client.calls == [
        ("start_thread", "/workspace/two", "gpt-5.5", "fast"),
        (
            "start_turn",
            "thread-new",
            "start after rollout",
            "gpt-5.5",
            "medium",
            "fast",
            None,
        ),
        ("attach_session", "thread-new"),
        (
            "start_turn",
            "thread-new",
            "start after rollout",
            "gpt-5.5",
            "medium",
            "fast",
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_controller_creates_empty_project_session(tmp_path: Path) -> None:
    controller, client, session_store, _runtime_store = _controller(tmp_path)

    result = await controller.create_session(
        "/workspace/two",
        model="gpt-5.5",
        service_tier="fast",
    )

    session = session_store.get_by_thread_id("thread-new")
    assert result.native_thread_id == "thread-new"
    assert result.turn_id == ""
    assert result.turn_running is False
    assert result.status == "created"
    assert session is not None
    assert session.cwd == "/workspace/two"
    assert client.calls == [("start_thread", "/workspace/two", "gpt-5.5", "fast")]


@pytest.mark.asyncio
async def test_controller_lists_native_models(tmp_path: Path) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)

    models = await controller.list_models()

    assert models == [{"model": "gpt-5.5", "displayName": "GPT-5.5"}]
    assert client.calls == [("list_models",)]


@pytest.mark.asyncio
async def test_controller_rejects_empty_thread_id_without_creating_session(
    tmp_path: Path,
) -> None:
    controller, client, session_store, _runtime_store = _controller(tmp_path)

    with pytest.raises(ValueError, match="native_thread_id is required"):
        await controller.continue_session("", "continue")
    with pytest.raises(ValueError, match="native_thread_id is required"):
        await controller.steer_session("", "turn-1", "steer")
    with pytest.raises(ValueError, match="native_thread_id is required"):
        await controller.interrupt_session("", "turn-1")

    assert session_store.list_recent(limit=10) == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_controller_registers_notification_handlers(tmp_path: Path) -> None:
    _controller_instance, client, session_store, runtime_store = _controller(tmp_path)

    assert set(client.handlers) == set(_METHOD_TO_BACKEND_EVENT)

    await client.handlers["item/agentMessage/delta"](
        {
            "threadId": "thread-handler",
            "turnId": "turn-handler",
            "delta": "hello",
            "item": {"id": "item-handler"},
        }
    )

    session = session_store.get_by_thread_id("thread-handler")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.PROVIDER_DISPLAY_DELTA,
        EventType.MODEL_TEXT_DELTA,
    ]
    assert events[2].payload["delta"] == "hello"


@pytest.mark.asyncio
async def test_controller_projects_and_resolves_native_approvals(
    tmp_path: Path,
) -> None:
    controller, client, session_store, runtime_store = _controller(tmp_path)

    assert "item/commandExecution/requestApproval" in client.server_handlers

    await client.server_handlers["item/commandExecution/requestApproval"](
        {
            "threadId": "thread-approval",
            "turnId": "turn-approval",
            "command": ["python", "-m", "pytest"],
            "reason": "run tests",
        },
        "req-approval",
    )

    session = session_store.get_by_thread_id("thread-approval")
    assert session is not None
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.PROVIDER_RAW_FRAME,
        EventType.APPROVAL_REQUESTED,
    ]
    assert events[1].payload["codexRequestId"] == "req-approval"
    assert events[1].payload["native_thread_id"] == "thread-approval"

    result = await controller.resolve_approval(
        "req-approval",
        {"action": "approve_once"},
    )

    assert result == {"codex_request_id": "req-approval", "status": "resolved"}
    assert client.resolved_requests == [
        ("req-approval", {"decision": "accept"})
    ]
    resolved_events = [
        event
        for event in runtime_store.list_by_agent_run(session.agent_run_id)
        if event.event_type != EventType.PROVIDER_RAW_FRAME
    ]
    assert [event.event_type for event in resolved_events] == [
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
    ]
    assert resolved_events[1].payload["codexRequestId"] == "req-approval"


@pytest.mark.asyncio
async def test_controller_rejects_unknown_approval_request_id(
    tmp_path: Path,
) -> None:
    controller, client, _session_store, _runtime_store = _controller(tmp_path)

    with pytest.raises(KeyError, match="unknown native approval request"):
        await controller.resolve_approval("missing", {"action": "approve_once"})

    assert client.resolved_requests == []
