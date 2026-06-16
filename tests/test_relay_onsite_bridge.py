from __future__ import annotations

import asyncio

from wlcodex.db import Ledger
from wlcodex.native_agents.models import NativeAgentCapabilities, NativeAgentControlResult
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.relay.onsite_bridge import RelayOnsiteBridge
from wlcodex.relay.service import RelayService
from wlcodex.relay.store import RelayStore
from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
)
from wlcodex.surfaces.terminal.manager import TerminalSessionManager


class FakeProvider:
    provider = "codex"
    provider_engine = "app-server"

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(can_start_session=True)

    async def start_session(self, cwd: str, prompt: str, **kwargs):
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="native-1",
            agent_run_id=101,
            status="started",
        )


class FakeTerminalAdapter:
    async def send_input(self, session_ref, text):
        return None


def _runtime_event(event_type: str, payload: dict) -> RuntimeEvent:
    return RuntimeEvent(
        schema_version=1,
        event_type=event_type,
        aggregate_type=AggregateType.AGENT_RUN,
        aggregate_id="101",
        correlation_id="agent:101",
        source=EventSource.CODEX,
        actor="codex_native",
        visibility=Visibility.USER,
        payload=payload,
        occurred_at=now_iso(),
        agent_run_id=101,
    )


def test_relay_onsite_bridge_attaches_records_and_detaches(tmp_path):
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    service = RelayService(
        store=RelayStore(ledger),
        registry=NativeAgentRegistry([FakeProvider()]),
        default_provider="codex",
    )
    terminal_manager = TerminalSessionManager(adapters={"codex": FakeTerminalAdapter()})
    bridge = RelayOnsiteBridge(
        relay_service=service,
        terminal_manager=terminal_manager,
    )
    service.add_event_projector(bridge.project_relay_event)

    task = service.create_task(
        title="Relay",
        prompt="Build it",
        workspace="/repo",
        provider="codex",
    )
    asyncio.run(service.dispatch_role(task.id, "director"))

    ref = terminal_manager.active_for_conversation(task.id)
    assert ref is not None
    assert ref.external_session_id == "native-1"

    bridge.project_runtime_event(
        _runtime_event(EventType.MODEL_TEXT_DELTA, {"delta": "hello"})
    )

    frames = terminal_manager.tail(ref, limit=10)
    assert any(frame.text == "hello" for frame in frames)

    bridge.project_runtime_event(_runtime_event(EventType.AGENT_RUN_COMPLETED, {}))

    assert terminal_manager.active_for_conversation(task.id) is None
