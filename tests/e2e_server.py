"""Minimal local service used only by the Playwright browser suite.

The fixture owns an isolated SQLite database and a deterministic native
provider.  It deliberately does not start the formal LaunchAgent, touch a
user's runtime database, or depend on a real Codex binary.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Any

from wlcodex.db import Ledger
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import WorkerLiveStreamServer
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.native_timeline import NativeTimelineStore
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)


NATIVE_THREAD_ID = "00000000-0000-4000-8000-000000000001"
NATIVE_AGENT_RUN_ID = 4242


class E2ENativeProvider:
    """Small native-provider double whose controls emit real runtime events."""

    provider = "codex"
    provider_engine = "e2e-fixture"

    def __init__(self, runtime_store: RuntimeEventStore, workspace: Path) -> None:
        self._runtime_store = runtime_store
        self._workspace = workspace
        self._last_turn_id = ""
        self._active_turn_id = ""
        self._turn_number = 0

    async def status(self) -> NativeAgentStatus:
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=True,
            status_code="ready",
            message="E2E fixture provider",
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_list_models=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_steer_active_turn=True,
            can_interrupt=True,
            can_resolve_approval=True,
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        del limit
        return [self._session()]

    async def list_cached_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        return await self.list_sessions(limit)

    async def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "e2e-model",
                "model": "e2e-model",
                "label": "E2E model",
                "isDefault": True,
            }
        ]

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        del cwd, kwargs
        return await self.continue_session(NATIVE_THREAD_ID, prompt)

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        del cwd, kwargs
        return self._control_result()

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        return self._session_payload(native_session_id)

    async def peek_session(self, native_session_id: str) -> dict[str, Any]:
        return self._session_payload(native_session_id)

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        self._require_known_session(native_session_id)
        return self._control_result()

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        self._require_known_session(native_session_id)
        return self._control_result()

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._require_known_session(native_session_id)
        del kwargs
        self._turn_number += 1
        self._last_turn_id = f"e2e-turn-{self._turn_number}"
        self._active_turn_id = self._last_turn_id
        self._append_event(
            EventType.USER_MESSAGE_RECEIVED,
            {
                "provider": self.provider,
                "native_provider": self.provider,
                "native_thread_id": native_session_id,
                "native_turn_id": self._last_turn_id,
                "itemId": f"e2e-user-{self._last_turn_id}",
                "text": prompt,
            },
        )
        self._append_event(
            EventType.AGENT_RUN_STARTED,
            {
                "provider": self.provider,
                "native_provider": self.provider,
                "native_thread_id": native_session_id,
                "native_turn_id": self._last_turn_id,
                "status": "running",
            },
        )
        return self._control_result()

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._require_known_session(native_session_id)
        if expected_turn_id != self._active_turn_id:
            raise ValueError("active turn does not match")
        del prompt, kwargs
        return self._control_result()

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult:
        self._require_known_session(native_session_id)
        if turn_id and turn_id != self._active_turn_id:
            raise ValueError("active turn does not match")
        if self._active_turn_id:
            self._append_event(
                EventType.RUN_CANCELLED,
                {
                    "provider": self.provider,
                    "native_provider": self.provider,
                    "native_thread_id": native_session_id,
                    "native_turn_id": self._active_turn_id,
                    "action": "turn_cancelled",
                    "status": "cancelled",
                },
            )
        self._active_turn_id = ""
        return self._control_result()

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return {"request_id": request_id, "status": "resolved", **body}

    def _session(self) -> NativeAgentSession:
        return NativeAgentSession(
            id=1,
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=NATIVE_THREAD_ID,
            agent_run_id=NATIVE_AGENT_RUN_ID,
            conversation_id=0,
            title="E2E Native session",
            cwd=str(self._workspace),
            source_kind="fixture",
            status="running" if self._active_turn_id else "idle",
            last_turn_id=self._last_turn_id,
            activity_at=now_iso(),
            created_at="2026-07-10T00:00:00+00:00",
            updated_at=now_iso(),
            metadata={"fixture": True},
        )

    def _session_payload(self, native_session_id: str) -> dict[str, Any]:
        self._require_known_session(native_session_id)
        return {
            **self._session().to_json_dict(),
            "native_session_source": "fixture",
            "thread": {
                "id": NATIVE_THREAD_ID,
                "threadId": NATIVE_THREAD_ID,
                "title": "E2E Native session",
            },
        }

    def _control_result(self) -> NativeAgentControlResult:
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=NATIVE_THREAD_ID,
            agent_run_id=NATIVE_AGENT_RUN_ID,
            turn_id=self._last_turn_id,
            active_turn_id=self._active_turn_id,
            turn_running=bool(self._active_turn_id),
        )

    def _append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type=event_type,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(NATIVE_AGENT_RUN_ID),
                correlation_id=f"e2e:{NATIVE_AGENT_RUN_ID}",
                source=EventSource.CODEX,
                actor="e2e-native-provider",
                visibility=Visibility.USER,
                payload=payload,
                occurred_at=now_iso(),
                agent_run_id=NATIVE_AGENT_RUN_ID,
            )
        )

    @staticmethod
    def _require_known_session(native_session_id: str) -> None:
        if native_session_id != NATIVE_THREAD_ID:
            raise KeyError(native_session_id)


class E2ELiveStreamServer(WorkerLiveStreamServer):
    """Disable autonomous Relay reconciliation so each browser test is stable."""

    def _schedule_relay_lifecycle_worker(self) -> bool:
        return False


async def _serve() -> None:
    port = int(os.environ.get("WLCODEX_E2E_PORT", "43187"))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop_event.set)

    with tempfile.TemporaryDirectory(prefix="wlcodex-e2e-") as temporary_directory:
        root = Path(temporary_directory)
        workspace = root / "workspace"
        workspace.mkdir()
        ledger = Ledger.open(root / "wlcodex.sqlite3")
        ledger.migrate()
        runtime_store = RuntimeEventStore(ledger._conn)
        timeline = NativeTimelineStore(ledger._conn)
        hub = WorkerLiveStreamHub(runtime_store)
        runtime_store.add_projector(timeline.project_runtime_event)
        runtime_store.add_projector(hub.publish)
        provider = E2ENativeProvider(runtime_store, workspace)
        registry = NativeAgentRegistry([provider])
        relay_service = RelayService(
            store=RelayStore(ledger),
            registry=registry,
            default_provider="codex",
        )
        for number in range(1, 5):
            relay_service.create_task(
                title=f"E2E Relay task {number}",
                prompt="Browser behavior verification",
                workspace=str(workspace),
            )
        confirmation_task = relay_service.create_task(
            title="E2E Relay confirmation",
            prompt="Verify the confirmation dialog behavior",
            workspace=str(workspace),
            execution_mode="plan_first",
        )
        await relay_service.handle_role_output(
            confirmation_task.id,
            "director",
            json.dumps(
                {
                    "status": "passed",
                    "reason": "plan first",
                    "role": "director",
                    "artifact_type": "routing_decision",
                    "handoff_to": "",
                    "summary": "Prepare the implementation plan first.",
                    "evidence_refs": [],
                    "open_questions": [],
                    "next_action": "plan",
                    "complexity": "medium",
                    "risk": "medium",
                    "route": "core_relay",
                    "required_roles": ["director", "architect", "implementer"],
                    "acceptance_criteria": ["approved plan"],
                    "stop_conditions": [],
                    "requires_user_approval": False,
                }
            ),
            dispatch_next=False,
        )
        await relay_service.handle_role_output(
            confirmation_task.id,
            "architect",
            json.dumps(
                {
                    "status": "waiting",
                    "reason": "needs approval",
                    "role": "architect",
                    "artifact_type": "architecture_plan",
                    "handoff_to": "",
                    "summary": "Review this plan before implementation.",
                    "evidence_refs": [],
                    "open_questions": ["Approve the plan?"],
                    "next_action": "approve plan",
                }
            ),
            dispatch_next=False,
        )
        server = E2ELiveStreamServer(
            host="127.0.0.1",
            port=port,
            hub=hub,
            native_registry=registry,
            native_timeline=timeline,
            relay_service=relay_service,
            access_token=None,
            allow_unauthenticated_loopback=True,
            workspace_catalog=(str(workspace),),
        )
        await server.start()
        print(f"WLCodex E2E fixture listening on {server.host}:{server.port}", flush=True)
        try:
            await stop_event.wait()
        finally:
            await server.stop()
            ledger._conn.close()


if __name__ == "__main__":
    asyncio.run(_serve())
