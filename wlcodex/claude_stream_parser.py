"""Parse Claude stream-json lines into normalized runtime events.

Pure functions — no I/O, no store dependency, no side effects.
Separates visible implementation text (model.text.delta, model.message.completed)
from activity/tool/usage/hook events so that Codex verification text is not
polluted by progress or observability events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from wlcodex.runtime_events import EventType


@dataclass
class ClaudeStreamEvent:
    """Intermediate event produced by parsing a single Claude stream-json line.

    Carries both the runtime event fields and backward-compat AgentStreamEvent
    fields.  Callers that only understand AgentStreamEvent can inspect
    *agent_delta* / *agent_event_type* / *agent_usage*; runtime-aware callers
    use *runtime_event_type* and *runtime_payload*.
    """

    runtime_event_type: str
    runtime_payload: dict[str, Any] = field(default_factory=dict)

    # AgentStreamEvent backward compat
    agent_delta: str = ""
    agent_event_type: str = "text"
    agent_usage: dict | None = None

    # Session tracking
    session_id: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_line(
    line: str,
    *,
    assistant_text: str = "",
    has_emitted_text: bool = False,
) -> tuple[list[ClaudeStreamEvent], str]:
    """Parse one raw stdout line into structured events.

    Returns ``(events, updated_assistant_text)``.  Every valid JSON object
    produces at least one event so that idle timeouts are always refreshed.
    """
    stripped = line.strip()
    if not stripped:
        return [], assistant_text

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return [
            ClaudeStreamEvent(
                runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
                runtime_payload={"raw": line[:2000]},
                agent_delta=line,
                agent_event_type="text",
            )
        ], assistant_text

    if not isinstance(payload, dict):
        return [], assistant_text

    event_type = str(payload.get("type") or "")

    if event_type == "stream_event":
        return _handle_stream_event(payload, assistant_text)
    if event_type == "assistant":
        return _handle_assistant(payload, assistant_text)
    if event_type == "result":
        return _handle_result(payload, assistant_text, has_emitted_text)
    if event_type == "system":
        return _handle_system(payload, assistant_text)
    if event_type in {"error", "api_error"}:
        return _handle_error(payload, assistant_text)
    if event_type.startswith("hook."):
        return _handle_hook(payload, assistant_text)

    # Any other valid JSON object → activity heartbeat
    return [
        ClaudeStreamEvent(
            runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
            runtime_payload={"event_type": event_type},
        )
    ], assistant_text


# ---------------------------------------------------------------------------
# stream_event handlers
# ---------------------------------------------------------------------------


def _handle_stream_event(
    payload: dict[str, Any],
    assistant_text: str,
) -> tuple[list[ClaudeStreamEvent], str]:
    event = payload.get("event")
    if not isinstance(event, dict):
        return [
            ClaudeStreamEvent(
                runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
                runtime_payload={"raw_type": "stream_event"},
            )
        ], assistant_text

    inner_type = str(event.get("type") or "")
    delta = event.get("delta")
    if not isinstance(delta, dict):
        delta = {}

    if inner_type == "content_block_start":
        return _handle_content_block_start(event, assistant_text)

    if delta.get("type") == "text_delta":
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return [
                ClaudeStreamEvent(
                    runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
                    runtime_payload={"stream_type": "text_delta_empty"},
                )
            ], assistant_text
        return [
            ClaudeStreamEvent(
                runtime_event_type=EventType.MODEL_TEXT_DELTA,
                runtime_payload={"text": text},
                agent_delta=text,
                agent_event_type="text",
            )
        ], assistant_text + text

    if delta.get("type") == "input_json_delta":
        partial = delta.get("partial_json", "")
        return [
            ClaudeStreamEvent(
                runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
                runtime_payload={
                    "stream_type": "input_json_delta",
                    "partial_json": str(partial)[:2000],
                },
            )
        ], assistant_text

    # content_block_stop, ping, or other stream events → activity
    return [
        ClaudeStreamEvent(
            runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
            runtime_payload={"stream_type": inner_type},
        )
    ], assistant_text


def _handle_content_block_start(
    event: dict[str, Any],
    assistant_text: str,
) -> tuple[list[ClaudeStreamEvent], str]:
    content_block = event.get("content_block")
    if not isinstance(content_block, dict):
        return [
            ClaudeStreamEvent(
                runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
                runtime_payload={"stream_type": "content_block_start"},
            )
        ], assistant_text

    if content_block.get("type") == "tool_use":
        tool_name = str(content_block.get("name") or "unknown")
        tool_id = str(content_block.get("id") or "")
        return [
            ClaudeStreamEvent(
                runtime_event_type=EventType.TOOL_CALL_STARTED,
                runtime_payload={
                    "tool_name": tool_name,
                    "tool_id": tool_id,
                },
            )
        ], assistant_text

    # text content block start → activity
    return [
        ClaudeStreamEvent(
            runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
            runtime_payload={"stream_type": "content_block_start", "block_type": str(content_block.get("type", ""))},
        )
    ], assistant_text


# ---------------------------------------------------------------------------
# assistant message handler
# ---------------------------------------------------------------------------


def _handle_assistant(
    payload: dict[str, Any],
    assistant_text: str,
) -> tuple[list[ClaudeStreamEvent], str]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return [], assistant_text

    content = message.get("content")
    events: list[ClaudeStreamEvent] = []

    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                t = item.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif item.get("type") == "tool_use":
                tool_name = str(item.get("name") or "unknown")
                tool_id = str(item.get("id") or "")
                tool_input = item.get("input")
                if isinstance(tool_input, dict):
                    events.append(
                        ClaudeStreamEvent(
                            runtime_event_type=EventType.TOOL_CALL_COMPLETED,
                            runtime_payload={
                                "tool_name": tool_name,
                                "tool_id": tool_id,
                                "input_summary": _summarize_tool_input(tool_input),
                            },
                        )
                    )
                else:
                    events.append(
                        ClaudeStreamEvent(
                            runtime_event_type=EventType.TOOL_CALL_STARTED,
                            runtime_payload={
                                "tool_name": tool_name,
                                "tool_id": tool_id,
                            },
                        )
                    )
        current_text = "".join(text_parts)

        if current_text:
            events.insert(
                0,
                ClaudeStreamEvent(
                    runtime_event_type=EventType.MODEL_MESSAGE_COMPLETED,
                    runtime_payload={"text": current_text},
                ),
            )
            if current_text.startswith(assistant_text):
                delta = current_text[len(assistant_text):]
            else:
                delta = current_text
                assistant_text += delta
            if delta:
                events.append(
                    ClaudeStreamEvent(
                        runtime_event_type=EventType.MODEL_TEXT_DELTA,
                        runtime_payload={"text": delta},
                        agent_delta=delta,
                        agent_event_type="text",
                    )
                )
                assistant_text = current_text
    elif isinstance(content, str):
        current_text = content
        if current_text:
            events.append(
                ClaudeStreamEvent(
                    runtime_event_type=EventType.MODEL_MESSAGE_COMPLETED,
                    runtime_payload={"text": current_text},
                ),
            )
            if current_text.startswith(assistant_text):
                delta = current_text[len(assistant_text):]
            else:
                delta = current_text
            if delta:
                events.append(
                    ClaudeStreamEvent(
                        runtime_event_type=EventType.MODEL_TEXT_DELTA,
                        runtime_payload={"text": delta},
                        agent_delta=delta,
                        agent_event_type="text",
                    )
                )
                assistant_text = current_text

    # Always emit activity for assistant message
    events.append(
        ClaudeStreamEvent(
            runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
            runtime_payload={"message_type": "assistant"},
        )
    )

    return events, assistant_text


# ---------------------------------------------------------------------------
# result handler
# ---------------------------------------------------------------------------


def _handle_result(
    payload: dict[str, Any],
    assistant_text: str,
    has_emitted_text: bool,
) -> tuple[list[ClaudeStreamEvent], str]:
    events: list[ClaudeStreamEvent] = []
    subtype = str(payload.get("subtype") or "")

    if subtype not in {"success", "done"}:
        error_text = str(payload.get("error") or payload.get("message") or "")
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.AGENT_RUN_FAILED,
                runtime_payload={
                    "subtype": subtype,
                    "error": error_text[:2000],
                },
                agent_delta=error_text or f"Claude Code result status: {subtype}",
                agent_event_type="error",
            )
        )
        return events, assistant_text

    # Extract usage
    usage = _extract_usage(payload)
    model = payload.get("model")
    model_str = str(model) if isinstance(model, str) else ""
    session_id = str(payload.get("session_id") or "")

    if usage:
        usage_payload: dict[str, Any] = dict(usage)
        if model_str:
            usage_payload["model"] = model_str
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.MODEL_USAGE_UPDATED,
                runtime_payload=usage_payload,
                agent_delta="",
                agent_event_type="usage",
                agent_usage=usage_payload,
                session_id=session_id,
            )
        )

    # Result text (only emit as text if nothing was emitted before)
    result_text = str(payload.get("result") or "")
    if result_text and not has_emitted_text:
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.MODEL_TEXT_DELTA,
                runtime_payload={"text": result_text},
                agent_delta=result_text,
                agent_event_type="text",
                session_id=session_id,
            )
        )

    # Always emit a session-identity event so consumers can capture session_id
    # even when there is no usage block and text was already emitted.
    if session_id:
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.AGENT_RUN_COMPLETED,
                runtime_payload={"session_id": session_id, "subtype": subtype},
                session_id=session_id,
            )
        )

    return events, assistant_text


# ---------------------------------------------------------------------------
# system / error handlers
# ---------------------------------------------------------------------------


def _handle_system(
    payload: dict[str, Any],
    assistant_text: str,
) -> tuple[list[ClaudeStreamEvent], str]:
    subtype = str(payload.get("subtype") or "")
    events: list[ClaudeStreamEvent] = []

    if subtype == "api_retry":
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.MODEL_API_RETRY,
                runtime_payload={
                    "message": str(payload.get("message") or "")[:2000],
                    "retry_count": payload.get("retry_count"),
                },
            )
        )

    # Always emit activity for system events
    events.append(
        ClaudeStreamEvent(
            runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
            runtime_payload={"system_subtype": subtype, "message": str(payload.get("message") or "")[:500]},
        )
    )

    return events, assistant_text


def _handle_error(
    payload: dict[str, Any],
    assistant_text: str,
) -> tuple[list[ClaudeStreamEvent], str]:
    text = str(payload.get("message") or payload.get("error") or payload)
    return [
        ClaudeStreamEvent(
            runtime_event_type=EventType.AGENT_RUN_FAILED,
            runtime_payload={"error": text[:2000]},
            agent_delta=text,
            agent_event_type="error",
        )
    ], assistant_text


# ---------------------------------------------------------------------------
# hook event handler
# ---------------------------------------------------------------------------


def _handle_hook(
    payload: dict[str, Any],
    assistant_text: str,
) -> tuple[list[ClaudeStreamEvent], str]:
    event_type = str(payload.get("type") or "")
    hook = payload.get("hook")
    hook_info: dict[str, Any] = {}

    if isinstance(hook, dict):
        hook_info = {
            "hook_id": str(hook.get("id") or ""),
            "hook_name": str(hook.get("name") or ""),
        }
        if "output" in hook:
            hook_info["output"] = str(hook.get("output", ""))[:2000]
        if "result" in hook:
            hook_info["result"] = str(hook.get("result", ""))[:2000]

    events: list[ClaudeStreamEvent] = []

    if event_type == "hook.started":
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.TOOL_CALL_PROGRESS,
                runtime_payload={**hook_info, "phase": "started"},
            )
        )
    elif event_type == "hook.progress":
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.TOOL_CALL_PROGRESS,
                runtime_payload={**hook_info, "phase": "progress"},
            )
        )
    elif event_type == "hook.completed":
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.TOOL_CALL_COMPLETED,
                runtime_payload={**hook_info, "phase": "completed"},
            )
        )
    elif event_type == "hook.error":
        events.append(
            ClaudeStreamEvent(
                runtime_event_type=EventType.TOOL_CALL_FAILED,
                runtime_payload={**hook_info, "phase": "error", "error": str(payload.get("error") or "")[:2000]},
            )
        )

    # Always emit activity for hook events
    events.append(
        ClaudeStreamEvent(
            runtime_event_type=EventType.AGENT_RUN_ACTIVITY,
            runtime_payload={"hook_event": event_type, **hook_info},
        )
    )

    return events, assistant_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    result: dict[str, Any] = {}
    for src_key, dst_key in [
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cached_input_tokens"),
        ("cache_creation_input_tokens", "cached_input_tokens"),
    ]:
        val = usage.get(src_key)
        if isinstance(val, (int, float)):
            result[dst_key] = result.get(dst_key, 0) + int(val)
    if "input_tokens" in result and "output_tokens" in result:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
        result["source"] = "exact"
        return result
    return None


def _summarize_tool_input(input_data: dict[str, Any]) -> dict[str, Any]:
    """Return a length-capped summary of tool input for event payloads."""
    summary: dict[str, Any] = {}
    for k, v in input_data.items():
        if isinstance(v, str):
            summary[k] = v[:500]
        elif isinstance(v, (int, float, bool)):
            summary[k] = v
        else:
            summary[k] = str(type(v).__name__)
    return summary
