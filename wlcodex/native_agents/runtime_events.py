from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from wlcodex.native_agents.models import NativeAgentSession
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    SCHEMA_VERSION,
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
)


class NativeAgentRuntimeEmitter:
    def __init__(
        self,
        *,
        runtime_store: RuntimeEventStore,
        provider: str,
        provider_engine: str,
        source_kind: str,
    ) -> None:
        self._runtime_store = runtime_store
        self._provider = provider
        self._provider_engine = provider_engine
        self._source_kind = source_kind

    def started(self, session: NativeAgentSession, *, native_turn_id: str) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.AGENT_RUN_STARTED,
            payload={"status": "running"},
        )

    def user_message(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        text: str,
    ) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            payload={
                "text": text,
                "itemId": f"{self._provider}-user-{uuid4()}",
            },
        )

    def text_delta(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        delta: str,
    ) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.MODEL_TEXT_DELTA,
            payload={
                "delta": delta,
                "text": delta,
                "itemId": f"{self._provider}-assistant-{native_turn_id}",
            },
        )

    def message_completed(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        text: str,
        item_id: str = "",
    ) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.MODEL_MESSAGE_COMPLETED,
            payload={
                "text": text,
                "summary": text,
                "itemId": item_id or f"{self._provider}-assistant-final-{native_turn_id}",
            },
        )

    def usage_updated(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        usage: dict[str, Any],
    ) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.MODEL_USAGE_UPDATED,
            payload={"usage": usage},
        )

    def tool_call_started(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        tool_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.TOOL_CALL_STARTED,
            payload={
                "tool_id": tool_id,
                "tool_name": tool_name,
                "tool_input": tool_input or {},
            },
        )

    def heartbeat(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
    ) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.AGENT_RUN_HEARTBEAT,
            payload={"status": "running"},
        )

    def completed(self, session: NativeAgentSession, *, native_turn_id: str) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.AGENT_RUN_COMPLETED,
            payload={"status": "completed"},
        )

    def failed(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        error: str,
    ) -> RuntimeEvent:
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.AGENT_RUN_FAILED,
            payload={"status": "failed", "error": error},
        )

    def _append(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> RuntimeEvent:
        native_payload = {
            **payload,
            "native_thread_id": session.native_session_id,
            "native_turn_id": native_turn_id,
            "provider": self._provider,
            "provider_engine": self._provider_engine,
            "source_kind": self._source_kind,
        }
        return self._runtime_store.append(
            RuntimeEvent(
                schema_version=SCHEMA_VERSION,
                event_type=event_type,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(session.agent_run_id),
                correlation_id=f"{self._provider}:{session.native_session_id}",
                source=_event_source(self._provider),
                actor=self._source_kind,
                visibility=Visibility.OPERATOR,
                payload=native_payload,
                occurred_at=_now(),
                conversation_id=session.conversation_id,
                agent_run_id=session.agent_run_id,
            )
        )


def extract_native_agent_text(event: Any) -> str:
    if isinstance(event, str):
        return event
    if isinstance(event, dict):
        return _text_from_mapping(event)
    for attr in ("delta", "text", "content"):
        if hasattr(event, attr):
            text = _text_from_value(getattr(event, attr))
            if text:
                return text
    return ""


def _text_from_mapping(value: dict[str, Any]) -> str:
    for key in ("delta", "text", "content"):
        if key in value:
            text = _text_from_value(value[key])
            if text:
                return text
    return ""


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _text_from_mapping(value)
    if isinstance(value, list):
        return "".join(_text_from_value(item) for item in value)
    return ""


def _event_source(provider: str) -> str:
    if provider == "claude":
        return EventSource.CLAUDE
    if provider == "antigravity":
        return EventSource.ANTIGRAVITY
    if provider == "codex":
        return EventSource.CODEX
    return EventSource.SYSTEM


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
