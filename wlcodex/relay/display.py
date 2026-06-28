from __future__ import annotations

import json
import re
from typing import Any

from wlcodex.relay.models import RELAY_ROLE_DISPLAY_NAMES, RELAY_ROLE_IDS


_LEGACY_ROLE_LABELS: dict[str, tuple[str, ...]] = {
    "architect": ("Computer Agent",),
    "implementer": ("App Agent",),
    "tester": ("Search Agent",),
    "auditor": ("File Agent", "Browser Agent"),
}
_LEGACY_ROLE_SLUGS: dict[str, tuple[str, ...]] = {
    "architect": ("computer-agent",),
    "implementer": ("app-agent",),
    "tester": ("search-agent",),
    "auditor": ("file-agent", "browser-agent"),
}
_CURRENT_ROLE_SLUGS: dict[str, str] = {
    "architect": "architect",
    "implementer": "implementer",
    "tester": "tester",
    "auditor": "auditor",
}


def relay_role_label(role: str) -> str:
    return RELAY_ROLE_DISPLAY_NAMES.get(role, role)


def replace_legacy_role_identifiers(text: str) -> str:
    value = str(text or "")
    for role in RELAY_ROLE_IDS:
        current_label = relay_role_label(role)
        for legacy_label in _LEGACY_ROLE_LABELS.get(role, ()):
            value = value.replace(legacy_label, current_label)
        current_slug = _CURRENT_ROLE_SLUGS.get(role, role)
        for legacy_slug in _LEGACY_ROLE_SLUGS.get(role, ()):
            value = value.replace(legacy_slug, current_slug)
    return value


def routing_route_label(route: str) -> str:
    return {
        "director_only": "总工程师直接完成",
        "core_relay": "核心接力",
        "full_relay": "完整五角色接力",
        "audit_first": "审计优先",
        "waiting_user": "等待用户确认",
        "blocked": "已阻塞",
    }.get(route, route or "等待总工程师判断")


def routing_risk_label(risk: str) -> str:
    return {
        "low": "低",
        "medium": "中",
        "high": "高",
        "critical": "关键",
    }.get(risk, risk or "待判断")


def humanize_display_text(text: str, *, english_fallback: str = "") -> str:
    value = str(text or "")
    value = re.sub(
        r"(?:Marvis/)?(?:App|File|Search|Computer|Browser)(?:/(?:App|File|Search|Computer|Browser))+ Agent",
        "英文角色名",
        value,
    )
    value = replace_legacy_role_identifiers(value)
    replacements = (
        ("路由为director_only", "由总工程师直接处理"),
        ("director_only", "总工程师直接处理"),
        ("core_relay", "核心角色接力"),
        ("full_relay", "五角色完整接力"),
        ("audit_first", "先审计再推进"),
        ("waiting_user", "等待你补充"),
        (
            "complete directly after routing by checking current market sources and returning the latest available gold price",
            "由总工程师核验最新行情来源并给出结果",
        ),
        ("complete directly after routing", "由总工程师直接处理"),
        ("complete directly", "直接处理"),
        ("dispatch next role", "交给下一位角色处理"),
        ("dispatch task", "任务分配"),
        ("safe-area-inset-top", "顶部安全区"),
        ("safe-area", "安全区"),
    )
    for source, target in replacements:
        value = value.replace(source, target)
    if english_fallback and text_needs_chinese_fallback(value):
        return english_fallback
    return value


def text_needs_chinese_fallback(text: str) -> bool:
    value = replace_legacy_role_identifiers(text).strip()
    if not value:
        return False
    normalized = value
    for public_name in (
        "开发工程师",
        "架构工程师",
        "测试工程师",
        "审核工程师",
        "总工程师",
        "Marvis",
    ):
        normalized = normalized.replace(public_name, "")
    if not re.search(r"[A-Za-z]{3,}", normalized):
        return False
    if not re.search(r"[\u4e00-\u9fff]", normalized):
        return True
    return bool(re.search(r"[A-Za-z]{3,}(?:[ -]+[A-Za-z]{2,}){1,}", normalized))


def join_text_list(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def humanize_role_envelope(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = str(
        payload.get("summary") or payload.get("output") or payload.get("reason") or ""
    ).strip()
    if summary:
        summary = humanize_display_text(
            summary,
            english_fallback="该角色已返回结构化结果，详情见结构化数据。",
        )
        lines.append(f"结论：{summary}")
    next_action = str(payload.get("next_action") or "").strip()
    if next_action:
        next_action = humanize_display_text(
            next_action,
            english_fallback="下一步见结构化数据。",
        )
        lines.append(f"下一步：{next_action}")
    questions = join_text_list(payload.get("open_questions"))
    if questions:
        questions = humanize_display_text(
            questions,
            english_fallback="待确认内容见结构化数据。",
        )
        lines.append(f"待确认：{questions}")
    route = str(payload.get("route") or "").strip()
    risk = str(payload.get("risk") or "").strip()
    if route or risk:
        parts: list[str] = []
        if route:
            parts.append(f"路径：{routing_route_label(route)}")
        if risk:
            parts.append(f"风险：{routing_risk_label(risk)}")
        lines.append(" · ".join(parts))
    acceptance = join_text_list(payload.get("acceptance_criteria"))
    if acceptance:
        acceptance = humanize_display_text(
            acceptance,
            english_fallback="验收依据见结构化数据。",
        )
        lines.append(f"验收依据：{acceptance}")
    if not lines:
        lines.append("角色已返回结构化结果。")
    return "\n".join(lines)


def protocol_output_hidden_text(role: str) -> str:
    role_label = relay_role_label(role)
    return f"{role_label}的结构化输出已由系统处理，原始协议内容不在主会话展示。"


def role_output_error_text(role: str, error: str) -> str:
    role_label = relay_role_label(role)
    lines = [f"{role_label}输出格式异常，任务已阻塞。"]
    if error:
        lines.append(f"错误：{humanize_display_text(error)}")
    lines.append("请补充确认后重新调度，原始结构化输出不在主会话展示。")
    return "\n".join(lines)


def sanitize_protocol_leak_text(role: str, text: str) -> str:
    value = humanize_display_text(text)
    sentinel = "原始结构化输出不在主会话展示。"
    if sentinel in value:
        return value.split(sentinel, 1)[0] + sentinel
    markers = (
        "artifact_type",
        "expected_output_envelope",
        "routing_decisioncomplexity",
        "required_roles",
        "handoff_to",
    )
    if "{" in value and any(marker in value for marker in markers):
        return protocol_output_hidden_text(role)
    return value


def dict_looks_like_role_envelope(payload: dict[str, Any]) -> bool:
    return any(
        key in payload
        for key in (
            "artifact_type",
            "relay_role",
            "summary",
            "next_action",
            "open_questions",
            "required_roles",
            "acceptance_criteria",
        )
    )


def followup_response_display_text(role: str, text: str) -> str:
    value = text.strip()
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and dict_looks_like_role_envelope(parsed):
        return humanize_role_envelope(parsed)
    return sanitize_protocol_leak_text(role, value)
