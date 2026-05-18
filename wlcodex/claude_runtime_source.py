"""ClaudeRuntimeSource wraps parsed Claude events with run context.

Adds correlation / run / agent IDs and appends RuntimeEvents to the
RuntimeEventStore.  Designed to be called per-event from the Claude backend
streaming loop.
"""

from __future__ import annotations

from wlcodex.runtime_events import (
    SCHEMA_VERSION,
    AggregateType,
    EventSource,
    EventType,
    Visibility,
    RuntimeEvent,
    now_iso,
)
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.claude_stream_parser import ClaudeStreamEvent


class ClaudeRuntimeSource:
    """Wraps each ClaudeStreamEvent with context IDs and appends to the store.

    Usage inside ``ClaudeBackend.send_streaming()``::

        source = ClaudeRuntimeSource(store, correlation_id=corr_id, ...)
        for parsed_event in parser_output:
            source.emit(parsed_event)
    """

    def __init__(
        self,
        store: RuntimeEventStore,
        *,
        correlation_id: str,
        agent_run_id: int | None = None,
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
        task_id: int | None = None,
    ) -> None:
        self._store = store
        self._correlation_id = correlation_id
        self._agent_run_id = agent_run_id
        self._conversation_id = conversation_id
        self._orchestration_run_id = orchestration_run_id
        self._task_id = task_id
        self._last_event_id: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def emit(self, parsed: ClaudeStreamEvent) -> RuntimeEvent:
        """Convert *parsed* to a ``RuntimeEvent`` and append it to the store.

        Returns the stored event (with its SQLite-assigned id).
        """
        event = self._make_event(
            event_type=parsed.runtime_event_type,
            payload=parsed.runtime_payload,
        )
        stored = self._store.append(event)
        self._last_event_id = stored.id
        return stored

    def emit_lifecycle(
        self,
        event_type: str,
        *,
        payload: dict | None = None,
        visibility: str = Visibility.INTERNAL,
    ) -> RuntimeEvent:
        """Emit a lifecycle event (started, completed, failed, etc.)."""
        return self._append(
            self._make_event(
                event_type=event_type,
                payload=payload or {},
                visibility=visibility,
            )
        )

    def emit_capability_missing(self, capability: str) -> RuntimeEvent:
        """Emit ``runtime.capability.missing`` for an unsupported CLI flag."""
        return self._append(
            self._make_event(
                event_type=EventType.RUNTIME_CAPABILITY_MISSING,
                payload={"capability": capability},
                visibility=Visibility.OPERATOR,
            )
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_event(
        self,
        *,
        event_type: str,
        payload: dict,
        visibility: str = Visibility.INTERNAL,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            schema_version=SCHEMA_VERSION,
            event_type=event_type,
            aggregate_type=AggregateType.AGENT_RUN,
            aggregate_id=str(self._agent_run_id or 0),
            correlation_id=self._correlation_id,
            source=EventSource.CLAUDE,
            actor="claude",
            visibility=visibility,
            payload=payload,
            occurred_at=now_iso(),
            conversation_id=self._conversation_id,
            orchestration_run_id=self._orchestration_run_id,
            agent_run_id=self._agent_run_id,
            task_id=self._task_id,
            causation_id=self._last_event_id,
        )

    def _append(self, event: RuntimeEvent) -> RuntimeEvent:
        stored = self._store.append(event)
        self._last_event_id = stored.id
        return stored
