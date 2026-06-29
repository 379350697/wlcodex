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
    safe_text_preview,
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
        item_id = f"{self._provider}-assistant-{native_turn_id}"
        self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.PROVIDER_DISPLAY_DELTA,
            payload={
                "delta": delta,
                "text": delta,
                "itemId": item_id,
                "display_source": "provider",
            },
            visibility=Visibility.USER,
        )
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.MODEL_TEXT_DELTA,
            payload={
                "delta": delta,
                "text": delta,
                "itemId": item_id,
                "compatibility_projection": EventType.MODEL_TEXT_DELTA,
            },
            visibility=Visibility.USER,
        )

    def message_completed(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        text: str,
        item_id: str = "",
    ) -> RuntimeEvent:
        item_id = item_id or f"{self._provider}-assistant-final-{native_turn_id}"
        self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.PROVIDER_DISPLAY_COMPLETED,
            payload={
                "text": text,
                "summary": text,
                "itemId": item_id,
                "display_source": "provider",
            },
            visibility=Visibility.USER,
        )
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.MODEL_MESSAGE_COMPLETED,
            payload={
                "text": text,
                "summary": text,
                "itemId": item_id,
                "compatibility_projection": EventType.MODEL_MESSAGE_COMPLETED,
            },
            visibility=Visibility.USER,
        )

    def usage_updated(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        usage: dict[str, Any],
    ) -> RuntimeEvent:
        self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.PROVIDER_SEMANTIC_USAGE_UPDATED,
            payload={"usage": usage, "semantic_kind": "usage"},
        )
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
        semantic_payload = {
            "tool_id": tool_id,
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "semantic_kind": "tool_call",
        }
        self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.PROVIDER_SEMANTIC_TOOL_CALL_STARTED,
            payload=semantic_payload,
        )
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

    def raw_frame(
        self,
        session: NativeAgentSession,
        *,
        native_turn_id: str,
        raw_kind: str,
        raw_payload: dict[str, Any],
        sequence: int | None = None,
    ) -> RuntimeEvent:
        sequence = sequence or self._runtime_store.next_provider_raw_frame_sequence(
            provider=self._provider,
            provider_engine=self._provider_engine,
            native_session_id=session.native_session_id,
            native_turn_id=native_turn_id,
        )
        occurred_at = _now()
        frame = self._runtime_store.append_provider_raw_frame(
            provider=self._provider,
            provider_engine=self._provider_engine,
            native_session_id=session.native_session_id,
            native_turn_id=native_turn_id,
            sequence=sequence,
            raw_kind=raw_kind,
            raw_payload=raw_payload,
            occurred_at=occurred_at,
            conversation_id=session.conversation_id,
            agent_run_id=session.agent_run_id,
        )
        return self._append(
            session,
            native_turn_id=native_turn_id,
            event_type=EventType.PROVIDER_RAW_FRAME,
            payload={
                "raw_frame_id": frame.id,
                "sequence": sequence,
                "raw_kind": raw_kind,
                "raw_preview": safe_text_preview(str(raw_payload), max_len=500),
            },
            occurred_at=occurred_at,
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
        visibility: str = Visibility.OPERATOR,
        occurred_at: str | None = None,
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
                visibility=visibility,
                payload=native_payload,
                occurred_at=occurred_at or _now(),
                conversation_id=session.conversation_id,
                agent_run_id=session.agent_run_id,
            )
        )


def provider_raw_payload(event: Any) -> dict[str, Any]:
    return _jsonable_mapping(event)


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
    for attr in ("text", "delta", "content"):
        if hasattr(value, attr):
            text = _text_from_value(getattr(value, attr))
            if text:
                return text
    return ""


def _jsonable_mapping(value: Any) -> dict[str, Any]:
    normalized = _jsonable_value(value)
    if isinstance(normalized, dict):
        return normalized
    return {"value": normalized}


def _jsonable_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_value(item) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return _jsonable_value(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        data = {
            str(key): _jsonable_value(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
        if data:
            data.setdefault("_type", value.__class__.__name__)
            return data
    return repr(value)


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
