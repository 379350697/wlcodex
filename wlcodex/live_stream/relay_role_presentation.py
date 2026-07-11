"""Relay role vocabulary shared by task detail, log, and conversation views."""

from __future__ import annotations

from typing import Any

from wlcodex.live_stream.presentation import role_status_label as presentation_role_status_label
from wlcodex.relay.display import replace_legacy_role_identifiers
from wlcodex.relay.models import RELAY_ROLE_IDS


RELAY_ROLE_PERSONAS: dict[str, tuple[str, str]] = {
    "director": ("marvis", "Marvis"),
    "implementer": ("implementer", "开发工程师"),
    "architect": ("architect", "架构工程师"),
    "tester": ("tester", "测试工程师"),
    "auditor": ("auditor", "审核工程师"),
}

RELAY_LEGACY_ROLE_LABEL_PARTS: dict[str, tuple[tuple[str, str], ...]] = {
    "implementer": (("App", "Agent"),),
    "architect": (("Computer", "Agent"),),
    "tester": (("Search", "Agent"),),
    "auditor": (("File", "Agent"), ("Browser", "Agent")),
}

RELAY_LEGACY_ROLE_SLUG_PARTS: dict[str, tuple[tuple[str, str], ...]] = {
    "implementer": (("app", "agent"),),
    "architect": (("computer", "agent"),),
    "tester": (("search", "agent"),),
    "auditor": (("file", "agent"), ("browser", "agent")),
}


def public_role(role: str) -> tuple[str, str]:
    return RELAY_ROLE_PERSONAS.get(str(role or "").strip(), ("marvis", "Marvis"))


def handoff_role_label(role: str) -> str:
    return public_role(role)[1]


def handoff_text(from_role: str, to_role: str) -> str:
    to_name = handoff_role_label(to_role)
    if from_role == "director":
        return f"Marvis 拍了拍 {to_name} 说， 别等了，这就开始"
    from_name = handoff_role_label(from_role)
    if to_role == "auditor":
        return f"{from_name}交给{to_name}复核"
    if from_role == "auditor" and to_role == "director":
        return f"{from_name}交回Marvis收尾"
    if from_role == "auditor":
        return f"{from_name}退回{to_name}继续处理"
    return f"{from_name}交给{to_name}继续处理"


def replace_legacy_role_display_names(text: str) -> str:
    value = str(text or "")
    for role in RELAY_ROLE_IDS:
        current_label = public_role(role)[1]
        for label_parts in RELAY_LEGACY_ROLE_LABEL_PARTS.get(role, ()):
            legacy_label = " ".join(label_parts)
            if legacy_label and legacy_label != current_label:
                value = value.replace(legacy_label, current_label)
    return value


def replace_legacy_role_names(text: str) -> str:
    return replace_legacy_role_identifiers(text)


def role_status_label(status: str) -> str:
    value = str(status or "").strip()
    if value in {"passed", "completed", "success", "succeeded"}:
        return "已完成"
    if value in {"failed", "blocked", "error"}:
        return "调用失败"
    if value in {"queued", "streaming", "started", "progress"}:
        return "进行中"
    if value == "waiting_user":
        return "等待中"
    if value == "interrupted":
        return "已中断"
    return presentation_role_status_label(value) if value else "进行中"


def confirmation_source_label(source: str, provider: str = "") -> str:
    clean_source = str(source or "").strip()
    provider_name = str(provider or "").strip().lower()
    if clean_source in {"provider_native_plan", "provider_native_approval"}:
        if provider_name == "codex":
            return "Codex 原生确认"
        if provider_name.startswith("claude"):
            return "Claude 原生确认"
        return "Provider 原生确认"
    if clean_source == "relay_prompt_fallback":
        return "Relay 澄清确认"
    return ""


def action_label(role: str, payload: dict[str, Any] | None = None) -> str:
    artifact_type = str((payload or {}).get("artifact_type") or "").strip()
    confirmation_label = confirmation_source_label(
        str((payload or {}).get("confirmation_source") or ""),
        str((payload or {}).get("provider") or ""),
    )
    if confirmation_label:
        return confirmation_label
    if role == "director":
        kind = str((payload or {}).get("kind") or "").strip()
        handoff_to = str((payload or {}).get("handoff_to") or "").strip()
        if (
            artifact_type == "routing_decision"
            or kind in {"text_delta", "waiting"}
            or (artifact_type == "final_summary" and handoff_to)
        ):
            return "任务分配"
        return ""
    labels = {
        "architecture_plan": "架构计划",
        "implementation_report": "执行反馈",
        "test_report": "测试反馈",
        "audit_report": "审核反馈",
    }
    if artifact_type in labels:
        return labels[artifact_type]
    if artifact_type:
        return role_status_label(artifact_type) if artifact_type in {
            "passed", "failed", "blocked", "completed"
        } else artifact_type.replace("_", " ")
    return "任务"
