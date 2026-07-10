"""Read-only presentation helpers shared by Relay and compatibility surfaces.

This module purposefully knows nothing about the HTTP server or persistence.
It maps an already-produced Relay summary into user-facing state, labels, and
freshness text; rendering code can therefore reuse one semantic vocabulary
without re-running lifecycle reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from wlcodex.presentation_contract import (
    PRESENTATION_STATES,
    task_status_label as _task_status_label,
)
from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES


_ACTIVITY_DISPLAY_TZ = timezone(timedelta(hours=8))


def summary_presentation(summary: Any) -> dict[str, Any]:
    value = getattr(summary, "presentation", None)
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return dict(value) if isinstance(value, dict) else {}


def summary_presentation_state(summary: Any) -> str:
    presentation = summary_presentation(summary)
    state = str(presentation.get("state") or "").strip()
    if state:
        return state
    raw_status = str(getattr(summary, "status", "") or "").strip()
    return {"queued": "running"}.get(raw_status, raw_status or "stale")


def presentation_state_filter(value: str) -> str:
    state = str(value or "").strip().lower()
    return state if state in PRESENTATION_STATES else ""


def status_class_name(status: str) -> str:
    safe = "".join(char if char.isalnum() else "-" for char in status.lower()).strip("-")
    return safe or "unknown"


def task_status_label(status: str) -> str:
    """Compatibility re-export of the common presentation state labels."""

    return _task_status_label(status)


def role_label(role: str) -> str:
    return RELAY_ROLE_DISPLAY_NAMES.get(role, role)


def role_status_label(status: str) -> str:
    return {
        "idle": "未调度",
        "queued": "待启动",
        "streaming": "输出中",
        "waiting": "等待",
        "passed": "已完成",
        "failed": "失败",
        "blocked": "阻塞",
        "interrupted": "中断",
    }.get(status, status or "未知")


def phase_label(phase: str) -> str:
    return {
        "director": "总工程师接收",
        "architect": "架构设计",
        "implementer": "开发实现",
        "tester": "测试验证",
        "auditor": "审计复核",
        "complete": "完成总结",
    }.get(phase, phase or "总工程师接收")


def activity_label(value: Any) -> str:
    if not value:
        return "暂无活动"
    if isinstance(value, datetime):
        parsed = value
    else:
        timestamp = str(value).strip()
        if not timestamp:
            return "暂无活动"
        normalized = re.sub(
            r"(\.\d{6})\d+([+-]\d\d:\d\d|Z)?$",
            r"\1\2",
            timestamp,
        )
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return "最近活动未知"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_ACTIVITY_DISPLAY_TZ)
    return f"最近活动 {parsed.astimezone(_ACTIVITY_DISPLAY_TZ):%m-%d %H:%M}"
