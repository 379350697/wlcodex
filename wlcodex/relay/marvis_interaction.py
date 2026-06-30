from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from wlcodex.relay.display import (
    text_contains_relay_protocol_payload,
)
from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES


CHAT_SURFACE = "chat"
WORKLOG_SURFACE = "worklog"
STATE_SURFACE = "state"

_PROTOCOL_NOISE_MARKERS = (
    "结构化结果缺少",
    "结构化结果不是合法",
    "结构化结果未采用",
    "结构化产物未采用",
    "结构化输出已由系统处理",
    "详情见结构化数据",
    "角色已返回结构化结果",
    "原始协议内容不在主会话展示",
    "输出格式异常",
    "invalid json",
    "expected_output_envelope",
)

_RELAY_PROTOCOL_ARTIFACT_TYPES = {
    "role_envelope",
    "routing_decision",
    "final_summary",
    "architecture_plan",
    "implementation_report",
    "audit_report",
    "test_report",
    "role_artifact_invalid",
    "role_error",
}

_ROLE_TO_MARVIS_AGENT = {
    "director": "Marvis",
    "architect": "架构工程师",
    "implementer": "开发工程师",
    "tester": "测试工程师",
    "auditor": "审核工程师",
}


@dataclass(frozen=True)
class MarvisInteractionEvent:
    kind: str
    surface: str
    sequence: int = 0
    role: str = ""
    title: str = ""
    body: str = ""
    status: str = ""
    tool_name: str = ""
    source_kind: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def chat_events(events: Iterable[MarvisInteractionEvent]) -> list[MarvisInteractionEvent]:
    return [event for event in events if event.surface == CHAT_SURFACE]


def worklog_events(events: Iterable[MarvisInteractionEvent]) -> list[MarvisInteractionEvent]:
    return [event for event in events if event.surface == WORKLOG_SURFACE]


def project_marvis_agui_events(raw_events: Iterable[dict[str, Any]]) -> list[MarvisInteractionEvent]:
    projected: list[MarvisInteractionEvent] = []
    for index, raw_event in enumerate(raw_events):
        event_type = _event_type(raw_event)
        sequence = _event_sequence(raw_event, fallback=index)
        data = _event_data(raw_event)
        if event_type == "HUMAN_MESSAGE":
            text = _first_text(data, "content", "text", "message")
            if text:
                projected.append(
                    MarvisInteractionEvent(
                        kind="user.message.accepted",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role="user",
                        body=text,
                        source_kind=event_type,
                        metadata={"raw_event": raw_event},
                    )
                )
            continue
        if event_type == "RUN_STARTED":
            projected.append(
                MarvisInteractionEvent(
                    kind="assistant.feedback.started",
                    surface=CHAT_SURFACE,
                    sequence=sequence,
                    role="director",
                    body="...",
                    source_kind=event_type,
                    metadata={"raw_event": raw_event},
                )
            )
            continue
        if event_type in {"TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_DELTA"}:
            text = _first_text(data, "text", "content", "message")
            if _is_natural_chat_text(text):
                role = str(data.get("role") or data.get("agentName") or "assistant")
                projected.append(
                    MarvisInteractionEvent(
                        kind="assistant.message.delta",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=role,
                        body=text,
                        source_kind=event_type,
                        metadata={"raw_event": raw_event},
                    )
                )
            continue
        if event_type == "TEXT_MESSAGE_END":
            text = _first_text(data, "text", "content", "message")
            if _is_natural_chat_text(text):
                role = str(data.get("role") or data.get("agentName") or "assistant")
                projected.append(
                    MarvisInteractionEvent(
                        kind="assistant.message.completed",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=role,
                        body=text,
                        source_kind=event_type,
                        metadata={"raw_event": raw_event},
                    )
                )
            continue
        if event_type == "TOOL_CALL_RESULT":
            tool_name = str(
                data.get("toolName")
                or data.get("tool_name")
                or data.get("name")
                or data.get("tool")
                or ""
            )
            status = "failed" if _tool_result_failed(data) else "completed"
            body = _first_text(data, "summary", "content", "result", "text")
            projected.append(
                MarvisInteractionEvent(
                    kind=f"tool.{status}",
                    surface=WORKLOG_SURFACE,
                    sequence=sequence,
                    role="director",
                    title=_tool_title(tool_name, status),
                    body=body,
                    status=status,
                    tool_name=tool_name,
                    source_kind=event_type,
                    metadata={"raw_event": raw_event},
                )
            )
            projected.append(
                MarvisInteractionEvent(
                    kind=f"tool.{status}",
                    surface=CHAT_SURFACE,
                    sequence=sequence,
                    role="director",
                    title=_tool_title(tool_name, status),
                    body=_tool_chat_body(body),
                    status=status,
                    tool_name=tool_name,
                    source_kind=event_type,
                    metadata={"raw_event": raw_event},
                )
            )
            continue
        if event_type == "CUSTOM":
            custom_name = str(data.get("name") or data.get("event") or "")
            value = _custom_value(data)
            agent_name = str(value.get("agentName") or value.get("agent_name") or "")
            if custom_name == "subagent_start" and agent_name:
                projected.append(
                    MarvisInteractionEvent(
                        kind="agent.handoff",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=agent_name,
                        body=_handoff_text("Marvis", agent_name),
                        status="started",
                        source_kind=event_type,
                        metadata={"raw_event": raw_event},
                    )
                )
                continue
            if custom_name == "subagent_end" and agent_name:
                status = str(value.get("status") or "completed")
                body = str(value.get("resultSummary") or value.get("summary") or "").strip()
                projected.append(
                    MarvisInteractionEvent(
                        kind="agent.dispatch.completed"
                        if status == "completed"
                        else "agent.dispatch.failed",
                        surface=WORKLOG_SURFACE,
                        sequence=sequence,
                        role=agent_name,
                        body=body,
                        status=status,
                        source_kind=event_type,
                        metadata={"raw_event": raw_event},
                    )
                )
                projected.append(
                    MarvisInteractionEvent(
                        kind="agent.dispatch.completed"
                        if status == "completed"
                        else "agent.dispatch.failed",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=agent_name,
                        title=f"{agent_name} 已完成" if status == "completed" else f"{agent_name} 失败",
                        body=body,
                        status=status,
                        source_kind=event_type,
                        metadata={"raw_event": raw_event},
                    )
                )
                continue
        if event_type == "RUN_FINISHED":
            projected.append(
                MarvisInteractionEvent(
                    kind="task.completed",
                    surface=STATE_SURFACE,
                    sequence=sequence,
                    status="completed",
                    source_kind=event_type,
                    metadata={"raw_event": raw_event},
                )
            )
            continue
    return projected


def project_relay_rows_to_marvis_interactions(
    rows: Iterable[dict[str, Any]],
) -> list[MarvisInteractionEvent]:
    projected: list[MarvisInteractionEvent] = []
    for index, row in enumerate(rows):
        kind = str(row.get("kind") or "")
        role = str(row.get("role") or "")
        body = str(row.get("body") or "")
        sequence = _event_sequence(row, fallback=index)
        source = {"relay_row": row}
        if kind == "user_message":
            if body.strip():
                projected.append(
                    MarvisInteractionEvent(
                        kind="user.message.accepted",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role="user",
                        body=body,
                        source_kind=kind,
                        metadata=source,
                    )
                )
            continue
        if kind == "waiting":
            projected.append(
                MarvisInteractionEvent(
                    kind="assistant.feedback.started",
                    surface=CHAT_SURFACE,
                    sequence=sequence,
                    role=role or "director",
                    body=body.strip() or "...",
                    source_kind=kind,
                    metadata=source,
                )
            )
            continue
        if kind in {"message_completed", "followup_response", "text_delta"}:
            if _is_natural_chat_text(body):
                projected.append(
                    MarvisInteractionEvent(
                        kind="assistant.message.completed",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=role or "director",
                        body=body,
                        status=str(row.get("status") or row.get("meta") or ""),
                        source_kind=kind,
                        metadata=source,
                    )
                )
            else:
                projected.append(_relay_worklog_entry(row, sequence=sequence))
            continue
        if kind == "handoff":
            from_role = str(row.get("from_role") or "").strip()
            to_role = str(row.get("to_role") or role or "").strip()
            if from_role and to_role:
                projected.append(
                    MarvisInteractionEvent(
                        kind="agent.handoff",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=to_role,
                        body=_handoff_text(_public_agent_name(from_role), _public_agent_name(to_role)),
                        source_kind=kind,
                        metadata=source,
                    )
                )
            continue
        if kind == "role_process":
            artifact_type = str(row.get("artifact_type") or "")
            if _relay_row_is_protocol_artifact(row) and not body.strip():
                projected.append(_relay_worklog_entry(row, sequence=sequence))
                continue
            if _is_natural_chat_text(body):
                projected.append(
                    MarvisInteractionEvent(
                        kind=_role_process_event_kind(row),
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=role,
                        title=_role_process_title(row),
                        body=body,
                        status=str(row.get("status") or row.get("meta") or ""),
                        source_kind=kind,
                        metadata=source,
                    )
                )
            else:
                projected.append(_relay_worklog_entry(row, sequence=sequence))
            if artifact_type in _RELAY_PROTOCOL_ARTIFACT_TYPES:
                projected.append(
                    MarvisInteractionEvent(
                        kind="worklog.entry",
                        surface=WORKLOG_SURFACE,
                        sequence=sequence,
                        role=role,
                        body=body,
                        status=str(row.get("status") or row.get("meta") or ""),
                        source_kind=kind,
                        metadata=source,
                    )
                )
            continue
        if kind == "status":
            if _is_natural_chat_text(body):
                projected.append(
                    MarvisInteractionEvent(
                        kind="task.paused",
                        surface=CHAT_SURFACE,
                        sequence=sequence,
                        role=role or "director",
                        body=body,
                        status=str(row.get("status") or row.get("meta") or "paused"),
                        source_kind=kind,
                        metadata=source,
                    )
                )
            else:
                projected.append(_relay_worklog_entry(row, sequence=sequence))
            continue
        if kind in {"role_artifact_invalid", "role_error", "role_envelope"}:
            projected.append(_relay_worklog_entry(row, sequence=sequence))
            continue
        if _relay_row_is_protocol_artifact(row) or _is_protocol_noise_text(body):
            projected.append(_relay_worklog_entry(row, sequence=sequence))
            continue
    return projected


def _relay_worklog_entry(row: dict[str, Any], *, sequence: int) -> MarvisInteractionEvent:
    body = str(row.get("body") or "")
    if not body.strip():
        payload = {key: value for key, value in row.items() if key != "metadata"}
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return MarvisInteractionEvent(
        kind="worklog.entry",
        surface=WORKLOG_SURFACE,
        sequence=sequence,
        role=str(row.get("role") or ""),
        body=body,
        status=str(row.get("status") or row.get("meta") or ""),
        source_kind=str(row.get("kind") or ""),
        metadata={"relay_row": row},
    )


def _event_type(raw_event: dict[str, Any]) -> str:
    return str(
        raw_event.get("event_type")
        or raw_event.get("type")
        or raw_event.get("kind")
        or ""
    )


def _event_sequence(raw_event: dict[str, Any], *, fallback: int) -> int:
    for key in ("seq", "sequence", "id", "event_id"):
        try:
            return int(str(raw_event.get(key) or ""))
        except (TypeError, ValueError):
            continue
    return fallback


def _event_data(raw_event: dict[str, Any]) -> dict[str, Any]:
    data = raw_event.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return {"content": data}
        return parsed if isinstance(parsed, dict) else {"content": data}
    payload = raw_event.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(raw_event, dict):
        return raw_event
    return {}


def _custom_value(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("value")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_result_failed(data: dict[str, Any]) -> bool:
    if bool(data.get("isError") or data.get("error")):
        return True
    status = str(data.get("status") or "").lower()
    return status in {"failed", "error"}


def _tool_title(tool_name: str, status: str) -> str:
    name = tool_name or "tool"
    if status == "failed":
        return f"{name} 失败"
    return f"{name} 已完成"


def _tool_chat_body(body: str) -> str:
    value = str(body or "").strip()
    if not value:
        return ""
    first_line = value.splitlines()[0].strip()
    return first_line[:240]


def _public_agent_name(role_or_name: str) -> str:
    value = str(role_or_name or "").strip()
    return _ROLE_TO_MARVIS_AGENT.get(value) or RELAY_ROLE_DISPLAY_NAMES.get(value) or value


def _handoff_text(from_agent: str, to_agent: str) -> str:
    source = from_agent or "Marvis"
    target = to_agent or "下一位"
    return f"{source} 拍了拍 {target} 说， 别等了，这就开始"


def _role_process_event_kind(row: dict[str, Any]) -> str:
    artifact_type = str(row.get("artifact_type") or "")
    if artifact_type in {"routing_decision", "final_summary"} and str(row.get("handoff_to") or ""):
        return "agent.dispatch.completed"
    return "assistant.message.completed"


def _role_process_title(row: dict[str, Any]) -> str:
    artifact_type = str(row.get("artifact_type") or "")
    if artifact_type in {"routing_decision", "final_summary"} and str(row.get("handoff_to") or ""):
        return "Marvis dispatch task 已完成"
    return ""


def _relay_row_is_protocol_artifact(row: dict[str, Any]) -> bool:
    artifact_type = str(row.get("artifact_type") or "")
    return artifact_type in _RELAY_PROTOCOL_ARTIFACT_TYPES or str(row.get("kind") or "") in {
        "role_envelope",
        "role_artifact_invalid",
        "role_error",
    }


def _is_natural_chat_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return not _is_protocol_noise_text(value)


def _is_protocol_noise_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if text_contains_relay_protocol_payload(value):
        return True
    if any(marker in value for marker in _PROTOCOL_NOISE_MARKERS):
        return True
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return any(marker in value for marker in ("artifact_type", "handoff_to", "status"))
        if isinstance(parsed, dict):
            artifact_type = str(parsed.get("artifact_type") or "")
            return bool(
                artifact_type in _RELAY_PROTOCOL_ARTIFACT_TYPES
                or parsed.get("relay_role")
                or parsed.get("handoff_to")
                or parsed.get("required_roles")
                or parsed.get("next_action")
            )
    return False
