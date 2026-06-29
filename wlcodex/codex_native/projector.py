from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any
from uuid import uuid4

from wlcodex.codex_backend import BackendEvent
from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.codex_runtime_source import CodexRuntimeSource
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import (
    EventType,
    RuntimeEvent,
    now_iso,
    redact_payload,
    safe_text_preview,
)


_SOURCE_KIND = "codex_native"

_METHOD_TO_BACKEND_EVENT = {
    "thread/started": "thread_started",
    "thread/status/changed": "thread_status_changed",
    "thread/tokenUsage/updated": "token_usage_updated",
    "turn/started": "turn_started",
    "turn/completed": "turn_completed",
    "turn/diff/updated": "diff_updated",
    "turn/plan/updated": "plan_updated",
    "item/started": "item_started",
    "item/completed": "item_completed",
    "item/agentMessage/delta": "agent_message_delta",
    "item/commandExecution/outputDelta": "command_output_delta",
    "item/fileChange/outputDelta": "file_change_delta",
    "item/fileChange/patchUpdated": "file_change_patch_updated",
    "item/reasoning/textDelta": "reasoning_delta",
    "item/reasoning/summaryTextDelta": "reasoning_delta",
    "item/tool/requestUserInput": "tool_request_user_input",
}

_SERVER_REQUEST_TO_APPROVAL_KIND = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
    "execCommandApproval": "command",
    "applyPatchApproval": "file_change",
}

_LEGACY_APPROVAL_METHODS = {"execCommandApproval", "applyPatchApproval"}


class NativeCodexEventProjector:
    def __init__(
        self,
        session_store: NativeCodexSessionStore,
        runtime_store: RuntimeEventStore,
    ) -> None:
        self._session_store = session_store
        self._runtime_store = runtime_store
        self._seen_event_keys: set[str] = set()
        self._loaded_seen_agent_run_ids: set[int] = set()

    def project_notification(
        self,
        method: str,
        payload: dict[str, Any],
    ) -> list[RuntimeEvent]:
        event_type = _METHOD_TO_BACKEND_EVENT.get(method)
        if event_type is None:
            return []

        native_thread_id = _thread_id(payload)
        if not native_thread_id:
            return []

        native_turn_id = _turn_id(payload)
        status = _status_for(method, payload)
        session = self._session_store.get_or_create_session(
            native_thread_id=native_thread_id,
            title=_payload_or_thread(payload, "title", ""),
            cwd=_payload_or_thread(payload, "cwd", ""),
            source_kind=_payload_or_thread(payload, "sourceKind", "unknown"),
            status=status,
            last_turn_id=native_turn_id,
        )

        normalized_payload = {
            **payload,
            "threadId": native_thread_id,
            "turnId": native_turn_id,
        }
        if "itemId" not in normalized_payload:
            item_id = _nested_id(payload, "item")
            if item_id:
                normalized_payload["itemId"] = item_id

        return self._append_backend_event(
            event_type=event_type,
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            payload=normalized_payload,
            session_agent_run_id=session.agent_run_id,
            session_conversation_id=session.conversation_id,
        )

    def project_history(self, detail: dict[str, Any]) -> list[RuntimeEvent]:
        thread = detail.get("thread")
        if not isinstance(thread, dict):
            return []
        native_thread_id = str(thread.get("id") or thread.get("threadId") or "")
        if not native_thread_id:
            return []

        projected: list[RuntimeEvent] = []
        turns = detail.get("turns")
        if not isinstance(turns, list):
            turns = thread.get("turns", [])
        if not isinstance(turns, list):
            return projected
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            native_turn_id = str(turn.get("id") or turn.get("turnId") or "")
            if not native_turn_id:
                continue
            projected.extend(
                self.project_notification(
                    "turn/started",
                    {
                        "thread": thread,
                        "turn": {"id": native_turn_id},
                        "threadId": native_thread_id,
                        "turnId": native_turn_id,
                    },
                )
            )
            items = turn.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        projected.extend(
                            self._project_history_item(
                                native_thread_id,
                                native_turn_id,
                                item,
                            )
                        )
            projected.extend(
                self.project_notification(
                    "turn/completed",
                    {
                        "thread": thread,
                        "turn": {
                            "id": native_turn_id,
                            "status": str(turn.get("status") or "completed"),
                        },
                        "threadId": native_thread_id,
                        "turnId": native_turn_id,
                    },
                )
            )
        return projected

    def _project_history_item(
        self,
        native_thread_id: str,
        native_turn_id: str,
        item: dict[str, Any],
    ) -> list[RuntimeEvent]:
        base = {
            "threadId": native_thread_id,
            "turnId": native_turn_id,
            "item": item,
            "itemId": str(item.get("id") or ""),
        }
        item_type = str(item.get("type") or "")
        if item_type == "userMessage":
            text = _user_message_text(item)
            if not text:
                return []
            session = self._session_store.get_or_create_session(
                native_thread_id=native_thread_id
            )
            return self._append_backend_event(
                event_type="user_message",
                native_thread_id=native_thread_id,
                native_turn_id=native_turn_id,
                payload={**base, "text": text},
                session_agent_run_id=session.agent_run_id,
                session_conversation_id=session.conversation_id,
            )
        if item_type == "agentMessage":
            text = _first_string(item, "text", "content", "message")
            if not text:
                return []
            return self.project_notification(
                "item/agentMessage/delta",
                {**base, "delta": text},
            )
        if item_type == "plan":
            text = _first_string(item, "text", "plan", "summary", "content")
            if not text:
                return []
            payload = {**base, "plan": text}
            title = _first_string(item, "title", "name")
            status = _first_string(item, "status")
            if title:
                payload["title"] = title
            if status:
                payload["status"] = status
            return self.project_notification("turn/plan/updated", payload)
        if item_type == "commandExecution":
            projected = self.project_notification("item/started", base)
            output = _first_string(
                item,
                "aggregatedOutput",
                "output",
                "outputText",
                "stdout",
                "stderr",
            )
            if output:
                projected.extend(
                    self.project_notification(
                        "item/commandExecution/outputDelta",
                        {**base, "delta": output, "itemId": str(item.get("id", ""))},
                    )
                )
            status = str(item.get("status") or "")
            if status == "failed" or item.get("exitCode") not in (None, 0):
                session = self._session_store.get_or_create_session(
                    native_thread_id=native_thread_id
                )
                projected.extend(
                    self._append_backend_event(
                        event_type="command_failed",
                        native_thread_id=native_thread_id,
                        native_turn_id=native_turn_id,
                        payload=base,
                        session_agent_run_id=session.agent_run_id,
                        session_conversation_id=session.conversation_id,
                    )
                )
            else:
                projected.extend(self.project_notification("item/completed", base))
            return projected
        if item_type == "fileChange":
            patch = _first_string(item, "patch", "diff")
            if patch:
                return self.project_notification(
                    "item/fileChange/patchUpdated",
                    {**base, "patch": patch},
                )
            delta = _first_string(item, "delta", "output")
            if delta:
                return self.project_notification(
                    "item/fileChange/outputDelta",
                    {**base, "delta": delta},
                )
        if item_type == "reasoning":
            text = _first_string(item, "text", "summary", "content")
            if text:
                return self.project_notification(
                    "item/reasoning/textDelta",
                    {**base, "delta": text},
                )
        return []

    def project_approval_request(
        self,
        method: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> list[RuntimeEvent]:
        kind = _SERVER_REQUEST_TO_APPROVAL_KIND.get(method)
        if kind is None:
            return []
        native_thread_id = _approval_thread_id(payload)
        if not native_thread_id:
            return []
        native_turn_id = _turn_id(payload)
        session = self._session_store.get_or_create_session(
            native_thread_id=native_thread_id,
            title=_payload_or_thread(payload, "title", ""),
            cwd=_payload_or_thread(payload, "cwd", ""),
            source_kind=_payload_or_thread(payload, "sourceKind", "unknown"),
            status="running",
            last_turn_id=native_turn_id,
        )
        normalized_payload = {
            **payload,
            "threadId": native_thread_id,
            "turnId": native_turn_id,
            "codexRequestId": request_id,
            "kind": kind,
        }
        if method in _LEGACY_APPROVAL_METHODS:
            normalized_payload["responseSchema"] = "legacy_review_decision"
        return self._append_backend_event(
            event_type="approval_requested",
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            payload=normalized_payload,
            session_agent_run_id=session.agent_run_id,
            session_conversation_id=session.conversation_id,
        )

    def project_approval_resolved(
        self,
        *,
        native_thread_id: str,
        native_turn_id: str = "",
        request_id: str,
        response: dict[str, Any],
    ) -> list[RuntimeEvent]:
        session = self._session_store.get_or_create_session(
            native_thread_id=native_thread_id,
        )
        return self._append_backend_event(
            event_type="approval_resolved",
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            payload={
                "threadId": native_thread_id,
                "turnId": native_turn_id,
                "codexRequestId": request_id,
                "response": response,
            },
            session_agent_run_id=session.agent_run_id,
            session_conversation_id=session.conversation_id,
        )

    def project_user_message(
        self,
        *,
        native_thread_id: str,
        native_turn_id: str,
        text: str,
        item_id: str | None = None,
    ) -> list[RuntimeEvent]:
        session = self._session_store.get_or_create_session(
            native_thread_id=native_thread_id,
        )
        return self._append_backend_event(
            event_type="user_message",
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            payload={
                "threadId": native_thread_id,
                "turnId": native_turn_id,
                "text": text,
                "itemId": item_id or f"local-user-{uuid4()}",
            },
            session_agent_run_id=session.agent_run_id,
            session_conversation_id=session.conversation_id,
        )

    def project_agent_message(
        self,
        *,
        native_thread_id: str,
        native_turn_id: str,
        text: str,
        item_id: str,
    ) -> list[RuntimeEvent]:
        session = self._session_store.get_or_create_session(
            native_thread_id=native_thread_id,
        )
        return self._append_backend_event(
            event_type="agent_message",
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            payload={
                "threadId": native_thread_id,
                "turnId": native_turn_id,
                "message": text,
                "item": {"id": item_id},
                "itemId": item_id,
            },
            session_agent_run_id=session.agent_run_id,
            session_conversation_id=session.conversation_id,
        )

    def _append_backend_event(
        self,
        *,
        event_type: str,
        native_thread_id: str,
        native_turn_id: str,
        payload: dict[str, Any],
        session_agent_run_id: int,
        session_conversation_id: int,
    ) -> list[RuntimeEvent]:
        source = CodexRuntimeSource(
            correlation_id=f"codex-native:{native_thread_id}",
            agent_run_id=session_agent_run_id,
            conversation_id=session_conversation_id,
        )
        mapped = source.map_event(BackendEvent(event_type, payload))
        projected: list[RuntimeEvent] = []
        self._load_persisted_seen_event_keys(session_agent_run_id)
        raw_payload_hash = _payload_hash(payload)
        raw_dedupe_payload = {
            "raw_kind": event_type,
            "raw_payload_hash": raw_payload_hash,
        }
        raw_dedupe_key = _dedupe_key(
            EventType.PROVIDER_RAW_FRAME,
            native_thread_id,
            native_turn_id,
            raw_dedupe_payload,
        )
        if raw_dedupe_key not in self._seen_event_keys:
            self._seen_event_keys.add(raw_dedupe_key)
            sequence = self._runtime_store.next_provider_raw_frame_sequence(
                provider="codex",
                provider_engine="app-server",
                native_session_id=native_thread_id,
                native_turn_id=native_turn_id,
            )
            occurred_at = now_iso()
            frame = self._runtime_store.append_provider_raw_frame(
                provider="codex",
                provider_engine="app-server",
                native_session_id=native_thread_id,
                native_turn_id=native_turn_id,
                sequence=sequence,
                raw_kind=event_type,
                raw_payload=payload,
                occurred_at=occurred_at,
                conversation_id=session_conversation_id,
                agent_run_id=session_agent_run_id,
            )
            raw_runtime_event = source._make(
                EventType.PROVIDER_RAW_FRAME,
                {
                    "raw_frame_id": frame.id,
                    "sequence": sequence,
                    "raw_kind": event_type,
                    "raw_payload_hash": raw_payload_hash,
                    "raw_preview": safe_text_preview(str(payload), max_len=500),
                    "threadId": native_thread_id,
                    "turnId": native_turn_id,
                },
            )
            projected.append(
                self._runtime_store.append(
                    replace(
                        raw_runtime_event,
                        actor=_SOURCE_KIND,
                        payload={
                            **raw_runtime_event.payload,
                            "native_thread_id": native_thread_id,
                            "native_turn_id": native_turn_id,
                            "source_kind": _SOURCE_KIND,
                            "provider": "codex",
                            "provider_engine": "app-server",
                        },
                    )
                )
            )
        for event in mapped:
            native_payload = {
                **event.payload,
                "native_thread_id": native_thread_id,
                "native_turn_id": native_turn_id,
                "source_kind": _SOURCE_KIND,
                "provider": "codex",
                "provider_engine": "app-server",
            }
            dedupe_key = _dedupe_key(
                event.event_type,
                native_thread_id,
                native_turn_id,
                native_payload,
            )
            if dedupe_key in self._seen_event_keys:
                continue
            self._seen_event_keys.add(dedupe_key)
            projected.append(
                self._runtime_store.append(
                    replace(event, actor=_SOURCE_KIND, payload=native_payload)
                )
            )
        return projected

    def _load_persisted_seen_event_keys(self, agent_run_id: int) -> None:
        if agent_run_id in self._loaded_seen_agent_run_ids:
            return
        for event in self._runtime_store.list_by_agent_run(agent_run_id, limit=50000):
            payload = dict(event.payload)
            if payload.get("source_kind") != _SOURCE_KIND:
                continue
            native_thread_id = str(payload.get("native_thread_id") or "")
            if not native_thread_id:
                continue
            native_turn_id = str(payload.get("native_turn_id") or "")
            if event.event_type == EventType.PROVIDER_RAW_FRAME:
                payload = {
                    "raw_kind": payload.get("raw_kind", ""),
                    "raw_payload_hash": payload.get("raw_payload_hash", ""),
                }
            self._seen_event_keys.add(
                _dedupe_key(
                    event.event_type,
                    native_thread_id,
                    native_turn_id,
                    payload,
                )
            )
        self._loaded_seen_agent_run_ids.add(agent_run_id)


def _thread_id(payload: dict[str, Any]) -> str:
    thread_id = payload.get("threadId") or _nested_id(payload, "thread")
    return str(thread_id) if thread_id else ""


def _approval_thread_id(payload: dict[str, Any]) -> str:
    thread_id = _thread_id(payload) or payload.get("conversationId", "")
    return str(thread_id) if thread_id else ""


def _turn_id(payload: dict[str, Any]) -> str:
    turn_id = payload.get("turnId") or _nested_id(payload, "turn")
    return str(turn_id) if turn_id else ""


def _nested_id(payload: dict[str, Any], key: str) -> object:
    value = payload.get(key)
    if isinstance(value, dict):
        return value.get("id", "")
    return ""


def _payload_or_thread(payload: dict[str, Any], key: str, default: str) -> str:
    value = payload.get(key)
    if value:
        return _stringify_protocol_value(value)
    thread = payload.get("thread")
    if isinstance(thread, dict):
        nested = thread.get(key)
        if nested:
            return _stringify_protocol_value(nested)
    return default


def _first_string(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _user_message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            chunks.append(str(part["text"]))
    return "\n".join(chunks)


def _stringify_protocol_value(value: object) -> str:
    if isinstance(value, dict) and value.get("type"):
        return str(value["type"])
    return str(value)


def _dedupe_key(
    event_type: str,
    native_thread_id: str,
    native_turn_id: str,
    payload: dict[str, Any],
) -> str:
    item_id = str(payload.get("itemId") or "")
    safe_payload = redact_payload(payload)
    compact_payload = {
        "event_type": event_type,
        "thread": native_thread_id,
        "turn": native_turn_id,
        "item": item_id,
        "payload": safe_payload,
    }
    encoded = json.dumps(
        compact_payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _status_for(method: str, payload: dict[str, Any]) -> str:
    if method == "turn/started":
        return "running"
    if method == "turn/completed":
        return _normalize_completed_status(_raw_status(payload))
    status = payload.get("status")
    if status:
        return _stringify_protocol_value(status)
    thread = payload.get("thread")
    if isinstance(thread, dict):
        nested = thread.get("status")
        if nested:
            return _stringify_protocol_value(nested)
    return ""


def _raw_status(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    if status:
        return _stringify_protocol_value(status)
    turn = payload.get("turn")
    if isinstance(turn, dict):
        nested = turn.get("status")
        if nested:
            return _stringify_protocol_value(nested)
    return ""


def _normalize_completed_status(status: str) -> str:
    normalized = status.lower()
    if normalized in ("active", "running", "inprogress", "in_progress"):
        return "running"
    if normalized in ("failed", "error"):
        return "failed"
    if normalized in ("cancelled", "canceled", "interrupted", "aborted"):
        return "aborted"
    return "done"
