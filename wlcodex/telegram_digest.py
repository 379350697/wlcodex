"""Concise Chinese digests for Telegram cockpit messages."""

from __future__ import annotations

import json
import re


_SECTION_PATTERNS = {
    "conclusion": re.compile(
        r"^(?:最终结论|结论|总结|summary|最终方案)(?:\s*[:：]\s*(.*))?$",
        re.IGNORECASE,
    ),
    "verification_result": re.compile(
        r"^(?:verification_result|验收结果|核验结论)(?:\s*[:：]\s*(.*))?$",
        re.IGNORECASE,
    ),
    "diagnosis": re.compile(
        r"^(?:diagnosis|诊断)(?:\s*[:：]\s*(.*))?$",
        re.IGNORECASE,
    ),
    "evidence": re.compile(r"^(?:依据|证据|关键依据|evidence)(?:\s*[:：]\s*(.*))?$", re.IGNORECASE),
    "executed_check": re.compile(
        r"^(?:已执行核验|核验结果|关键事实|事实)(?:\s*[:：]\s*(.*))?$",
        re.IGNORECASE,
    ),
    "risk": re.compile(r"^(?:风险|风险等级|risk|confidence)(?:\s*[:：]\s*(.*))?$", re.IGNORECASE),
    "next": re.compile(
        r"^(?:下一步|建议下一步|建议|next_action|next step|next steps)(?:\s*[:：]\s*(.*))?$",
        re.IGNORECASE,
    ),
    "claude_task": re.compile(
        r"^(?:Claude\s*任务|给\s*Claude\s*的任务|交给\s*Claude\s*执行|Claude\s*执行|claude_prompt|handoff_prompt)(?:\s*[:：]\s*(.*))?$",
        re.IGNORECASE,
    ),
}

_CLAUDE_SECTION_NAMES = {
    "claude 任务",
    "claude_task",
    "给 claude 的任务",
    "交给 claude 执行",
    "claude 执行",
    "claude_prompt",
    "handoff_prompt",
}

_KNOWN_MARKDOWN_SECTIONS = {
    "diagnosis",
    "evidence",
    "confidence",
    "confidence_reason",
    "risk",
    "files_to_touch",
    "claude_prompt",
    "handoff_prompt",
    "acceptance_criteria",
    "verification_result",
    "needs_implementation",
}

_KEY_POINT_RE = re.compile(
    r"^(?:新问题|老问题|部署状态|当前问题|问题)\s*\d*\s*[:：].+"
    r"|.*(?:开放仓位|平仓卡住|reduce-only|ReduceOnly|HTTP 400|Bad Request|"
    r"fail_closed|risk_only|状态收敛|本地状态|真实交易所|非零持仓|开放订单|"
    r"local L2|stale/rebuild|deploy_version|Git HEAD).*",
    re.IGNORECASE,
)

_LOW_VALUE_PREFIXES = (
    "你说得对",
    "基于已经",
    "我已经在",
    "完整结论如下",
)

_NOOP_NEXT_VALUES = {
    "无",
    "没有",
    "不需要",
    "无需",
    "none",
    "n/a",
    "null",
}

_STRUCTURED_REPORT_KEYS = {
    "audit_report",
    "implementation_report",
    "change_location",
    "change_summary",
    "changed_files",
    "decision",
    "files_modified",
    "verdict",
    "status",
    "conclusion",
    "summary",
    "risk",
    "risk_level",
    "recommended_next_action",
    "test_evidence_refs",
    "passed_checks",
    "failed_checks",
    "findings",
}

_PROTOCOL_FIELD_NAMES = {
    "artifact_type",
    "audit_report",
    "changed_files",
    "commands_run",
    "confidence_score",
    "implementation",
    "implementation_report",
    "needs_implementation",
    "output_schema",
    "passed_checks",
    "recommended_next_action",
    "required_output_schema",
    "schema_version",
    "test_evidence_refs",
    "tests_attempted",
    "verdict",
}

_DECISION_LABELS = {
    "pass": "pass",
    "passed": "pass",
    "approve": "pass",
    "approved": "pass",
    "success": "pass",
    "block": "block",
    "blocked": "block",
    "fail": "block",
    "failed": "block",
    "failure": "block",
    "retry": "block",
    "repair": "block",
    "needs_repair": "block",
    "need_repair": "block",
    "needs_user": "need_user",
    "need_user": "need_user",
    "ask_user": "need_user",
}

_RISK_LABELS = {
    "low": "低。",
    "medium": "中。",
    "high": "高。",
    "critical": "严重。",
}

_NEXT_ACTION_LABELS = {
    "close": "可以结束本次任务。",
    "done": "可以结束本次任务。",
    "finish": "可以结束本次任务。",
    "repair": "可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束。",
    "retry": "可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束。",
    "send_back_to_claude": "可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束。",
    "send_back_to_codex": "可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束。",
    "ask_user": "请补充确认后再继续。",
}

_PROTOCOL_PAYLOAD_RE = re.compile(
    r'"(?:audit_report|implementation_report|verdict|files_modified|'
    r'commands_run|tests_attempted|test_evidence_refs|needs_implementation|'
    r'changed_files|recommended_next_action)"\s*:'
    r"|^(?:audit_report|implementation_report|commands_run|tests_attempted|"
    r"changed_files|needs_implementation)\s*[:=]",
    re.IGNORECASE | re.MULTILINE,
)


def render_auto_draft_digest(
    text: str,
    *,
    max_chars: int = 700,
    fallback_next: str | None = None,
) -> str:
    """Render a short Chinese digest for /auto draft_ready cockpit cards.

    The full model output stays in the orchestration run. This function only
    prepares the Telegram-visible preview.
    """
    structured = _render_structured_report_digest(
        text,
        max_chars=max_chars,
        fallback_next=fallback_next,
    )
    if structured:
        return structured

    lines = _clean_lines(text)
    if not lines:
        return "关键摘要：\n结论：暂无可展示内容。\n依据：未收到模型正文。\n风险：未知。\n下一步：请继续补充上下文。"
    if not _contains_cjk(" ".join(lines)):
        return (
            "关键摘要：\n"
            "结论：模型返回了非中文内容，已保留全文。\n"
            "依据：原文可在当前草稿/全文中查看。\n"
            "风险：驾驶舱不直接展示非中文长文，避免误读。\n"
            "下一步：请查看全文或继续补充上下文。"
        )

    conclusion = _find_conclusion(lines)
    evidence = _find_evidence(lines)
    key_points = _find_key_points(lines)
    risk = _find_section(lines, "risk")
    next_step = _find_section(lines, "next")
    claude_task = _find_claude_task(text, lines)

    if not conclusion:
        conclusion = _first_informative_line(lines)
    raw_conclusion = conclusion
    conclusion = _brief_conclusion(
        conclusion,
        completion_heading=_completion_heading(lines, conclusion),
    )
    if key_points:
        evidence = _merge_evidence(key_points, evidence)
    elif not evidence or _is_low_value_evidence(evidence):
        evidence = key_points
    evidence = _drop_duplicate_evidence(
        evidence,
        raw_conclusion=raw_conclusion,
        conclusion=conclusion,
    )
    if not evidence:
        evidence = _fallback_evidence(lines, skip={conclusion, risk, next_step})
    if not risk:
        risk = "未明确风险。"
    if _is_noop_instruction(next_step):
        next_step = ""
    if not next_step:
        next_step = fallback_next or _infer_next_step(key_points, conclusion, risk)
    next_step = _with_claude_task(next_step, claude_task)
    next_step = _sanitize_next_step(next_step)

    rendered = _render_digest(conclusion, evidence, risk, next_step)
    if len(rendered) <= max_chars:
        return rendered

    evidence = evidence[:2] or evidence
    rendered = _render_digest(
        _trim_sentence(conclusion, 120),
        [_trim_sentence(item, 100) for item in evidence],
        _trim_sentence(risk, 100),
        _trim_sentence(next_step, 100),
    )
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 12].rstrip() + "\n...（已精简）"


def render_missing_diagnose_digest() -> str:
    return (
        "关键摘要：\n"
        "结论：结构化诊断证据没有采集成功，暂时不能给出确定的线上交易判断。\n"
        "依据：\n"
        "- 缺少诊断脚本产出的结构化结果\n"
        "- 当前只能确认诊断采集失败，不能确认交易状态\n"
        "风险：高。证据不完整，置信度低，不要推送或执行交易操作。\n"
        "下一步：请重新触发诊断采集，或检查诊断日志后再继续。"
    )


def sanitize_telegram_user_text(text: str) -> str:
    """Last-mile guard for Telegram-visible text.

    Higher-level flows should render human summaries themselves. This function
    is the shared safety net at the actual Telegram send/edit boundary, so a
    missed caller cannot leak artifact JSON or protocol field names to users.
    """
    if not text or not _contains_protocol_payload(text):
        return text

    digest = render_auto_draft_digest(text)
    heading = _sanitize_heading(text)
    suffix = "\n\n请选择下一步：" if "请选择下一步" in text else ""
    if heading:
        return f"{heading}\n\n{digest}{suffix}"
    return f"{digest}{suffix}"


def _contains_protocol_payload(text: str) -> bool:
    if _PROTOCOL_PAYLOAD_RE.search(text):
        return True
    return any(_looks_like_protocol_line(line.strip()) for line in text.splitlines())


def _sanitize_heading(text: str) -> str:
    normalized = _normalize_sentence(text)
    if "验收未通过" in normalized:
        return "验收未通过。"
    if "验收通过" in normalized:
        return "验收通过。"
    if "Claude 执行完成" in normalized or "返工完成" in normalized or "实现完成" in normalized:
        return "实现完成。"
    return ""


def _clean_lines(text: str) -> list[str]:
    cleaned: list[str] = []
    in_code_block = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not line:
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", "", line)
        line = re.sub(r"^\*\*(.+?)\*\*$", r"\1", line)
        line = line.strip()
        if line and not _looks_like_protocol_line(line):
            cleaned.append(line)
    return cleaned


def _render_structured_report_digest(
    text: str,
    *,
    max_chars: int,
    fallback_next: str | None,
) -> str:
    report = _extract_structured_report(text)
    if report is None:
        return ""

    report_kind = str(report.get("__report_kind") or "")
    if report_kind == "implementation_report":
        return _render_implementation_report_digest(
            report,
            max_chars=max_chars,
            fallback_next=fallback_next,
        )

    decision = _structured_decision(report)
    summary = _structured_text(
        report.get("summary")
        or report.get("diagnosis")
        or report.get("result")
        or report.get("message")
    )
    if summary:
        if decision == "pass" and "验收" not in summary[:8]:
            conclusion = f"验收通过：{summary}"
        elif decision == "block" and "验收" not in summary[:8]:
            conclusion = f"验收未通过：{summary}"
        else:
            conclusion = summary
    elif decision == "pass":
        conclusion = "验收通过。"
    elif decision == "block":
        conclusion = "验收未通过。"
    elif decision == "need_user":
        conclusion = "需要你确认后继续。"
    else:
        conclusion = "已收到结构化验收结果。"

    evidence = _structured_evidence(report)
    risk = _structured_risk(report)
    next_step = _structured_next_step(report, fallback_next=fallback_next)

    rendered = _render_digest(conclusion, evidence, risk, next_step)
    if len(rendered) <= max_chars:
        return rendered
    return _render_digest(
        _trim_sentence(conclusion, 120),
        [_trim_sentence(item, 100) for item in evidence[:2]],
        _trim_sentence(risk, 100),
        _trim_sentence(next_step, 100),
    )


def _extract_structured_report(text: str) -> dict[str, object] | None:
    decoder = json.JSONDecoder()
    reports: list[dict[str, object]] = []
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        report = _unwrap_structured_report(parsed)
        if report is not None:
            reports.append(report)
    return reports[-1] if reports else None


def _unwrap_structured_report(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("audit_report"), dict):
        value = value["audit_report"]
        report_kind = "audit_report"
    elif isinstance(value.get("implementation_report"), dict):
        value = value["implementation_report"]
        report_kind = "implementation_report"
    else:
        report_kind = ""
    if not isinstance(value, dict):
        return None
    if _STRUCTURED_REPORT_KEYS.isdisjoint(str(key) for key in value.keys()):
        return None
    report = dict(value)
    if not report_kind and any(
        key in report
        for key in (
            "change_summary",
            "changed_files",
            "files_modified",
            "files_changed",
            "modified_files",
        )
    ):
        report_kind = "implementation_report"
    if report_kind:
        report["__report_kind"] = report_kind
    return report


def _render_implementation_report_digest(
    report: dict[str, object],
    *,
    max_chars: int,
    fallback_next: str | None,
) -> str:
    summary = _structured_text(
        report.get("change_summary")
        or report.get("summary")
        or report.get("diff_summary")
        or report.get("status")
    )
    conclusion = (
        f"返工完成：{summary}"
        if summary
        else "返工已完成，等待重新验收。"
    )
    evidence = _implementation_report_evidence(report)
    risk = "未明确风险。"
    next_step = fallback_next or "可以重新验收。"
    rendered = _render_digest(conclusion, evidence, risk, next_step)
    if len(rendered) <= max_chars:
        return rendered
    return _render_digest(
        _trim_sentence(conclusion, 120),
        [_trim_sentence(item, 100) for item in evidence[:2]],
        risk,
        next_step,
    )


def _implementation_report_evidence(report: dict[str, object]) -> list[str]:
    evidence: list[str] = []
    changed_files = (
        report.get("changed_files")
        or report.get("files_modified")
        or report.get("files_changed")
        or report.get("modified_files")
    )
    files = []
    if isinstance(changed_files, list | tuple):
        files = [
            path
            for item in changed_files
            if (path := _normalize_sentence(str(item)))
        ]
    if files:
        evidence.append("改动文件：" + "、".join(files[:5]))

    location = report.get("change_location")
    if isinstance(location, dict):
        file_name = _structured_text(location.get("file"))
        line = _structured_text(location.get("line"))
        if file_name and line:
            evidence.append(f"位置：{file_name} 第 {line} 行")
        elif file_name:
            evidence.append(f"位置：{file_name}")

    commands = _structured_items(report.get("commands_run"))
    tests = _structured_items(report.get("tests_attempted"))
    for item in [*commands[:2], *tests[:2]]:
        if item and not _looks_like_protocol_line(item):
            evidence.append(item)
    return list(dict.fromkeys(evidence))[:5]


def _structured_decision(report: dict[str, object]) -> str:
    raw = _structured_text(
        report.get("decision")
        or report.get("verdict")
        or report.get("status")
        or report.get("conclusion")
    )
    normalized = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if normalized.startswith(("pass", "passed", "approve", "approved", "success")):
        return "pass"
    if normalized.startswith((
        "block",
        "blocked",
        "fail",
        "failed",
        "failure",
        "retry",
        "repair",
        "needs_repair",
        "need_repair",
    )):
        return "block"
    if normalized.startswith(("needs_user", "need_user", "ask_user")):
        return "need_user"
    return _DECISION_LABELS.get(normalized, "")


def _structured_evidence(report: dict[str, object]) -> list[str]:
    evidence: list[str] = []
    for key in ("evidence", "test_evidence_refs"):
        evidence.extend(_structured_items(report.get(key)))
    for key in (
        "passed_checks",
        "failed_checks",
        "checks",
        "verification",
        "verification_results",
        "findings",
    ):
        evidence.extend(_structured_items(report.get(key)))

    deduped: list[str] = []
    for item in evidence:
        item = _trim_sentence(item, 140)
        if not item or _looks_like_protocol_line(item):
            continue
        if any(_same_key_point(item, existing) for existing in deduped):
            continue
        deduped.append(item)
        if len(deduped) >= 5:
            break
    return deduped


def _structured_items(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _normalize_sentence(value)
        if _looks_like_protocol_line(text) or _looks_like_internal_ref(text):
            return []
        return [text]
    if isinstance(value, (int, float, bool)):
        return []
    if isinstance(value, list | tuple):
        items: list[str] = []
        for item in value:
            items.extend(_structured_items(item))
        return items
    if isinstance(value, dict):
        items: list[str] = []
        for key in (
            "evidence",
            "evidence_ref",
            "evidence_refs",
            "test_evidence_refs",
            "detail",
            "details",
            "message",
            "summary",
            "title",
        ):
            items.extend(_structured_items(value.get(key)))
        return items
    return []


def _structured_risk(report: dict[str, object]) -> str:
    raw = _structured_text(
        report.get("risk")
        or report.get("risk_level")
        or report.get("confidence")
        or report.get("confidence_reason")
    )
    if not raw:
        return "未明确风险。"
    normalized = raw.strip().lower().replace("_", " ")
    return _RISK_LABELS.get(normalized, raw)


def _structured_next_step(
    report: dict[str, object],
    *,
    fallback_next: str | None,
) -> str:
    raw = _structured_text(
        report.get("recommended_next_action")
        or report.get("next_action")
        or report.get("next_step")
    )
    normalized = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return _NEXT_ACTION_LABELS.get(normalized) or raw or fallback_next or "请继续补充上下文。"


def _structured_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalize_sentence(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list | tuple):
        return "；".join(item for item in (_structured_text(v) for v in value) if item)
    if isinstance(value, dict):
        for key in ("summary", "message", "detail", "title"):
            text = _structured_text(value.get(key))
            if text:
                return text
    return ""


def _looks_like_internal_ref(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:team_artifact|agent_job|team_run|conversation|orchestration_run|task)=",
            _normalize_sentence(text).lower(),
        )
    )


def _find_section(lines: list[str], key: str) -> str:
    pattern = _SECTION_PATTERNS[key]
    for idx, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        value = (match.group(1) or "").strip()
        if value:
            return _normalize_sentence(value)
        if idx + 1 < len(lines):
            return _normalize_sentence(lines[idx + 1])
    return ""


def _find_conclusion(lines: list[str]) -> str:
    for key in ("conclusion", "verification_result", "diagnosis"):
        value = _find_section(lines, key)
        if value:
            return value
    return ""


def _brief_conclusion(text: str, *, completion_heading: str = "") -> str:
    text = re.sub(r"^(?:最终结论是|最终结论)\s*[:：]\s*", "", _normalize_sentence(text))
    if completion_heading and re.match(r"^(?:我会|我将|会先|先把)", text):
        return completion_heading
    if (
        "文档-only" in text
        and "只改文档" in text
        and "最小验证" in text
        and re.match(r"^(?:我会|我将|会先|先把)", text)
    ):
        return "文档-only小任务已收敛为只改文档并做最小验证。"
    text = re.sub(r"旧的[^。；;]*没有复现[。；;]?", "", text)
    text = re.sub(r"之前的[^。；;]*没有(?:按原形)?复现[。；;]?", "", text)
    if "新的问题是" in text:
        before, after = text.split("新的问题是", 1)
        issue = re.split(r"[。；;]", after, maxsplit=1)[0].strip()
        prefix = before.strip(" ；;。")
        text = f"{prefix}；新问题是：{issue}" if prefix else f"新问题是：{issue}"
    return _trim_sentence(text, 180)


def _completion_heading(lines: list[str], conclusion: str) -> str:
    if not lines:
        return ""
    first = _normalize_sentence(lines[0])
    if not re.search(r"(?:开发|实现|任务).*(?:完成|通过)|测试通过", first):
        return ""
    if "文档-only" in conclusion or "只改文档" in conclusion:
        return "文档-only小任务已完成，测试通过。"
    return _trim_sentence(first, 80)


def _find_evidence(lines: list[str]) -> list[str]:
    evidence_items = _collect_section_lines(lines, "evidence")
    if not evidence_items:
        evidence_items = _collect_section_lines(lines, "executed_check")
    if not evidence_items:
        return []

    parts: list[str] = []
    for evidence in evidence_items:
        parts.extend(part for part in re.split(r"[；;]\s*", evidence) if part.strip())

    rendered: list[str] = []
    for part in parts:
        item = _humanize_evidence_item(part)
        if not item:
            continue
        if any(_same_key_point(item, existing) for existing in rendered):
            continue
        rendered.append(_trim_sentence(item, 140))
        if len(rendered) >= 5:
            break
    return rendered[:3]


def _drop_duplicate_evidence(
    evidence: list[str],
    *,
    raw_conclusion: str,
    conclusion: str,
) -> list[str]:
    filtered: list[str] = []
    for item in evidence:
        normalized = _normalize_sentence(item)
        if normalized in {"推荐方案"}:
            continue
        if raw_conclusion and _same_key_point(normalized, raw_conclusion):
            continue
        if conclusion and _same_key_point(normalized, conclusion):
            continue
        filtered.append(item)
    return filtered


def _collect_section_lines(lines: list[str], key: str) -> list[str]:
    pattern = _SECTION_PATTERNS[key]
    for idx, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        value = (match.group(1) or "").strip()
        if value:
            return [_normalize_sentence(value)]
        collected: list[str] = []
        for candidate in lines[idx + 1 :]:
            if any(section_pattern.match(candidate) for section_pattern in _SECTION_PATTERNS.values()):
                break
            normalized = _normalize_sentence(candidate)
            if normalized:
                collected.append(normalized)
        return collected
    return []


def _humanize_evidence_item(text: str) -> str:
    normalized = _normalize_sentence(text)
    lowered = normalized.lower()
    if any(field in lowered for field in (
        "needs_implementation",
        "files_to_touch",
        "implementation_steps",
    )):
        if (
            re.search(r"needs_implementation\s*=\s*false", lowered)
            or "needs_implementation:false" in lowered
        ) and (
            re.search(r"files_to_touch\s*=\s*\[\]", lowered)
            or "files_to_touch:[]" in lowered
        ):
            return "任务包没有给出明确的代码改动范围或执行步骤。"
        return "任务包包含内部执行字段，需先转成明确的人类任务说明。"
    if "无错误栈" in normalized or "无失败日志" in normalized:
        return "没有错误栈或失败日志。"
    if "git status --short" in lowered and "无未提交改动" in normalized:
        return "工作区没有未提交改动。"
    if lowered.startswith("gitnexus://") and "落后 head" in lowered:
        return "代码索引可用于导航，但落后当前代码；执行前需要以本地文件为准。"
    if "diagnosis_report schema" in lowered:
        return "诊断报告还需要补齐症状、预期、证据、根因、修复计划和回归测试。"
    return normalized


def _find_key_points(lines: list[str]) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        normalized = _normalize_sentence(line)
        if _looks_like_noise(normalized) or _is_heading_only(normalized):
            continue
        if not _KEY_POINT_RE.match(normalized):
            continue
        if _starts_with_low_value_prefix(normalized):
            continue
        point = _trim_sentence(normalized, 140)
        if any(_same_key_point(point, existing[2]) for existing in ranked):
            continue
        ranked.append((_key_point_rank(normalized), idx, point))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [point for _, _, point in ranked[:5]]


def _key_point_rank(text: str) -> int:
    lowered = text.lower()
    if text.startswith(("新问题", "老问题")):
        return 0
    if any(token in lowered for token in ("fail_closed", "risk_only")):
        return 0
    if any(token in lowered for token in (
        "状态收敛",
        "本地状态",
        "真实交易所",
        "reduce-only",
        "http 400",
        "bad request",
        "local l2",
    )):
        return 1
    if any(token in text for token in ("deploy_version", "Git HEAD", "HEAD=")):
        return 2
    if "开放仓位" in text:
        return 3
    return 4


def _merge_evidence(evidence: list[str], key_points: list[str]) -> list[str]:
    merged = [item for item in evidence if not _starts_with_low_value_prefix(item)]
    for point in key_points:
        if any(_same_key_point(point, existing) for existing in merged):
            continue
        merged.append(point)
        if len(merged) >= 5:
            break
    return merged[:5]


def _is_low_value_evidence(evidence: list[str]) -> bool:
    return bool(evidence) and all(_starts_with_low_value_prefix(item) for item in evidence)


def _starts_with_low_value_prefix(text: str) -> bool:
    return _normalize_sentence(text).startswith(_LOW_VALUE_PREFIXES)


def _same_key_point(left: str, right: str) -> bool:
    left = _normalize_sentence(left)
    right = _normalize_sentence(right)
    return left in right or right in left


def _is_heading_only(line: str) -> bool:
    return _normalize_heading(line) in {
        "结论",
        "部署状态",
        "新问题",
        "老问题",
        "已执行核验",
        "核验结果",
        "关键事实",
        "事实",
    }


def _find_claude_task(text: str, lines: list[str]) -> str:
    task = _find_section(lines, "claude_task")
    if not task:
        task = _find_markdown_claude_section(text)
    if not task:
        return ""
    brief = _brief_claude_task(task)
    return "" if _is_noop_instruction(brief) else brief


def _find_markdown_claude_section(text: str) -> str:
    body: list[str] = []
    capturing = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _normalize_heading(line)
        if capturing:
            if heading in _KNOWN_MARKDOWN_SECTIONS:
                break
            body.append(raw_line.rstrip())
            continue
        if heading in _CLAUDE_SECTION_NAMES:
            inline_value = _inline_section_value(line)
            if inline_value and inline_value not in {"|", ">"}:
                body.append(inline_value)
            capturing = True
    return _strip_code_fences("\n".join(body))


def _normalize_heading(line: str) -> str:
    line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line.strip())
    line = re.sub(r"^\*\*(.+?)\*\*$", r"\1", line)
    line = re.sub(r"^`(.+?)`$", r"\1", line)
    line = line.rstrip(":：").strip()
    return re.sub(r"\s+", " ", line).lower()


def _inline_section_value(line: str) -> str:
    match = re.match(r"^.*?[:：]\s*(.+)$", line)
    return _normalize_sentence(match.group(1)) if match else ""


def _strip_code_fences(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _with_claude_task(next_step: str, claude_task: str) -> str:
    if not claude_task:
        return next_step
    if _looks_like_generic_next_step(next_step):
        return f"交给 DeepSeek 开发工程师执行：{claude_task}"
    if "claude" not in next_step.lower():
        return f"{next_step}；DeepSeek 开发工程师任务：{claude_task}"
    generic = re.sub(r"[。.!！\s]+$", "", next_step)
    if claude_task in generic:
        return next_step
    return f"{generic}：{claude_task}"


def _sanitize_next_step(next_step: str) -> str:
    text = _normalize_sentence(next_step)
    replacements = (
        ("请重写方案或继续补充上下文", "请继续补充上下文"),
        ("请查看全文、重写方案或继续补充上下文", "请查看全文或继续补充上下文"),
        ("也可继续补充或重写方案", "也可继续补充或结束"),
        ("也可继续补充、重写或结束", "也可继续补充或结束"),
        ("继续补充、重写方案、", "继续补充、"),
        ("继续补充、重写或结束", "继续补充或结束"),
        ("、重写方案", ""),
        ("、重写", ""),
        ("重写方案、", ""),
        ("重写方案或", ""),
        ("重写或", ""),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text).strip(" ，,、")
    return text or "请继续补充上下文。"


def _looks_like_generic_next_step(next_step: str) -> bool:
    normalized = _normalize_sentence(next_step).lower()
    return (
        "按下方按钮继续" in next_step
        or "继续补充、重写或结束" in next_step
        or normalized.startswith("可选：交给 claude")
        or normalized.startswith("可选：交给 deepseek")
        or normalized.startswith("可选：交给 deepseek 开发工程师")
        or _normalize_sentence(next_step) in {
            "交给 Claude 执行",
            "交给 Claude",
            "交给 DeepSeek 开发工程师执行",
            "交给 DeepSeek 开发工程师",
        }
    )


def _brief_claude_task(task: str) -> str:
    parts = [
        _normalize_sentence(part)
        for part in re.split(r"[；;。.!！\n]+", _strip_code_fences(task))
    ]
    candidates = [
        part
        for part in parts
        if part and not _looks_like_claude_prompt_boilerplate(part)
    ]
    task = next(
        (
            part
            for part in candidates
            if re.search(r"(修复|实现|修改|排查|核验|验收|部署|解决|补充|检查)", part)
        ),
        candidates[0] if candidates else _normalize_sentence(task),
    )
    task = re.sub(r"，?(?:让|并|同时)?下一步.*$", "", task)
    task = re.sub(r"，?(?:具体)?文件.*$", "", task)
    task = re.sub(r"，?目标.*$", "", task)
    task = re.sub(r"，?验收.*$", "", task)
    return _trim_sentence(task, 52)


def _is_noop_instruction(text: str) -> bool:
    normalized = _normalize_sentence(text).rstrip("。.!！?？").lower()
    if not normalized:
        return False
    if normalized in _NOOP_NEXT_VALUES:
        return True
    return normalized.startswith((
        "不需要",
        "无需",
        "无需交给",
        "该任务无需",
        "不要交给 claude",
    ))


def _infer_next_step(key_points: list[str], conclusion: str, risk: str) -> str:
    combined = " ".join([*key_points, conclusion, risk]).lower()
    if any(token in combined for token in ("状态收敛", "本地状态", "真实交易所", "残留")):
        return "可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理状态收敛/残留仓位问题，也可继续补充或结束"
    if any(token in combined for token in ("平仓", "reduce-only", "http 400", "bad request")):
        return "可选：交给 DeepSeek 开发工程师或 GPT 开发工程师排查平仓失败，也可继续补充或结束"
    if any(token in combined for token in ("bug", "问题", "风险", "异常", "失败", "优化")):
        return "可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束"
    return "可选：继续补充、交给开发工程师处理或结束"


def _looks_like_claude_prompt_boilerplate(part: str) -> bool:
    lowered = part.lower()
    return (
        part.startswith(("不要启动", "本提示", "必须遵守", "完成后", "线上证据", "证据"))
        or "仅作为后续实现任务交接使用" in part
        or lowered in {"text", "yaml"}
    )


def _fallback_evidence(lines: list[str], *, skip: set[str]) -> list[str]:
    evidence: list[str] = []
    for line in lines:
        normalized = _normalize_sentence(line)
        if normalized in skip:
            continue
        if any(pattern.match(line) for pattern in _SECTION_PATTERNS.values()):
            continue
        if _looks_like_noise(line):
            continue
        evidence.append(_trim_sentence(normalized, 140))
        if len(evidence) >= 3:
            break
    return evidence


def _first_informative_line(lines: list[str]) -> str:
    for line in lines:
        if not _looks_like_noise(line):
            return _trim_sentence(_normalize_sentence(line), 160)
    return _trim_sentence(_normalize_sentence(lines[0]), 160)


def _looks_like_noise(line: str) -> bool:
    lowered = line.lower()
    return (
        "mkdir -p" in lowered
        or "skill.md" in lowered
        or lowered.startswith(("操作步骤", "以下是", "markdown"))
    )


def _looks_like_protocol_line(line: str) -> bool:
    stripped = _normalize_sentence(line).rstrip(",")
    lowered = stripped.lower()
    if stripped in {"{", "}", "[", "]", "},", "],"}:
        return True
    if re.match(r'^[{}\[\]],?$', stripped):
        return True
    if re.match(r'^"[^"]+"\s*:', stripped):
        return True
    match = re.match(r"^([a-z][a-z0-9_ -]*)\s*[:=]\s*", lowered)
    if not match:
        return False
    field = match.group(1).replace("-", "_").replace(" ", "_")
    return field in _PROTOCOL_FIELD_NAMES


def _render_digest(
    conclusion: str,
    evidence: list[str],
    risk: str,
    next_step: str,
) -> str:
    if evidence:
        evidence_text = "\n" + "\n".join(f"- {_ensure_sentence(item)}" for item in evidence)
    else:
        evidence_text = "未提取到明确证据。"
    return (
        "关键摘要：\n"
        f"结论：{_ensure_sentence(conclusion)}\n"
        f"依据：{evidence_text}\n"
        f"风险：{_ensure_sentence(risk)}\n"
        f"下一步：{_ensure_sentence(next_step)}"
    )


def _normalize_sentence(text: str) -> str:
    text = _role_label_text(text)
    return re.sub(r"\s+", " ", text).strip(" -:：")


def _role_label_text(text: str) -> str:
    replacements = (
        ("Claude Code", "DeepSeek 开发工程师"),
        ("Claude", "DeepSeek 开发工程师"),
        ("Codex 验收", "审计工程师验收"),
        ("Codex 分析", "诊断工程师分析"),
        ("Codex 核验", "诊断工程师核验"),
        ("Codex", "GPT 开发工程师"),
        ("claude", "DeepSeek 开发工程师"),
        ("codex", "GPT 开发工程师"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = re.sub(
        r"(DeepSeek 开发工程师|GPT 开发工程师|审计工程师|诊断工程师)\s+(?=[\u4e00-\u9fff])",
        r"\1",
        text,
    )
    return text


def _trim_sentence(text: str, max_chars: int) -> str:
    text = _normalize_sentence(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip(" ，,；;。.") + "…"


def _ensure_sentence(text: str) -> str:
    text = _normalize_sentence(text)
    if not text:
        return "未明确。"
    if text[-1] in "。！？.!?":
        return text
    return text + "。"


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


# ---------------------------------------------------------------------------
# Structured diagnose digest \u2014 consumes diagnose_live.py JSON, not raw text
# ---------------------------------------------------------------------------


def render_auto_diagnose_digest(
    diagnose_json_str: str,
    *,
    max_chars: int = 700,
) -> str:
    """Render a concise Chinese digest from structured diagnose JSON.

    Unlike render_auto_draft_digest which regex-parses free-text model output,
    this function consumes the stable JSON output of diagnose_live.py so
    Telegram shows the same structured facts as the local diagnose.

    Returns "" if diagnose_json is missing or unparseable \u2014 callers must
    detect this and render a low-confidence fallback explicitly.
    """
    import json as _json

    try:
        diag = _json.loads(diagnose_json_str)
    except (_json.JSONDecodeError, ValueError):
        return ""

    if not isinstance(diag, dict) or not diag:
        return ""

    # Must have schema_version to be a valid diagnose JSON
    if "schema_version" not in diag and "conclusion" not in diag:
        return ""

    conclusion = diag.get("conclusion", {})
    if not isinstance(conclusion, dict):
        conclusion = {}

    health = diag.get("health", {})
    if not isinstance(health, dict):
        health = {}

    local_state = diag.get("local_state", {})
    if not isinstance(local_state, dict):
        local_state = {}

    deploy = diag.get("deploy_status", {})
    if not isinstance(deploy, dict):
        deploy = {}

    state_consistency = diag.get("state_consistency", {})
    if not isinstance(state_consistency, dict):
        state_consistency = {}

    # Support both old "evidence_completeness" and new "evidence_quality" keys
    ev_completeness = diag.get("evidence_quality", diag.get("evidence_completeness", {}))
    if not isinstance(ev_completeness, dict):
        ev_completeness = {}

    order_errors = diag.get("order_error_evidence", [])
    if not isinstance(order_errors, list):
        order_errors = []

    service_status = diag.get("service_status", {})
    if not isinstance(service_status, dict):
        service_status = {}

    exchange_truth = diag.get("exchange_truth", {})
    if not isinstance(exchange_truth, dict):
        exchange_truth = {}

    top_errors = diag.get("top_exchange_errors", [])
    if not isinstance(top_errors, list):
        top_errors = []

    # Build conclusion line
    status = str(conclusion.get("status", "unknown"))
    risk = str(conclusion.get("risk", "unknown"))
    summary = str(conclusion.get("summary", ""))
    concl_line = "{} ({}): {}".format(
        _diagnose_status_label(status), _diagnose_risk_label(risk), summary,
    )

    # Build evidence from structured data
    evidence_lines: list[str] = []

    # Service status
    svc_parts = []
    for svc_name, svc in sorted(service_status.items()):
        active = str(svc.get("active", "unknown"))
        n_restarts = svc.get("n_restarts", 0)
        if n_restarts:
            svc_parts.append("{}={} (R{})".format(svc_name, active, n_restarts))
        else:
            svc_parts.append("{}={}".format(svc_name, active))
    if svc_parts:
        evidence_lines.append("\u670d\u52a1: {}".format(", ".join(svc_parts)))

    # Deploy status
    if deploy.get("version_mismatch"):
        evidence_lines.append("\u7248\u672c\u4e0d\u4e00\u81f4: HEAD={}, deploy={}".format(
            deploy.get("git_head", "?"), deploy.get("deploy_version", "?"),
        ))

    # Exchange truth
    if exchange_truth.get("available"):
        evidence_lines.append("\u4ea4\u6613\u6240: available venues={}".format(
            ", ".join(exchange_truth.get("available_venues", [])),
        ))
    else:
        et_confidence = exchange_truth.get("confidence", "low")
        et_errors = exchange_truth.get("errors", ["no credentials"])
        et_err = (et_errors[0] if et_errors else "unknown")[:60]
        evidence_lines.append("\u4ea4\u6613\u6240: unavailable ({}) confidence={}".format(
            et_err, et_confidence,
        ))

    # Local state
    evidence_lines.append("\u672c\u5730: {}/{} \u5f00\u4ed3{} \u5f85\u5f00{} \u5f85\u5e73{}".format(
        local_state.get("lifecycle", "?"),
        local_state.get("risk_mode", "?"),
        local_state.get("open_position_count", 0),
        local_state.get("pending_entry_count", 0),
        local_state.get("pending_close_count", 0),
    ))

    # State mismatch \u2014 show detail with symbols
    if state_consistency.get("local_open_exchange_flat"):
        local_syms = []
        for d in state_consistency.get("details", []):
            for s in d.get("local_symbols", []):
                if s not in local_syms:
                    local_syms.append(s)
        syms_str = " ({})".format(", ".join(local_syms[:3])) if local_syms else ""
        evidence_lines.append(
            "\u4e25\u91cd: \u672c\u5730\u6709\u4ed3/\u4ea4\u6613\u6240\u65e0\u4ed3 (local_open_exchange_flat=true){}".format(syms_str),
        )
    elif state_consistency.get("state_mismatch"):
        evidence_lines.append("\u72b6\u6001\u4e0d\u4e00\u81f4: state_mismatch=true")

    # Top exchange errors \u2014 show HTTP status, code, msg, body presence
    errors_to_show = top_errors if top_errors else order_errors[:3]
    for err in errors_to_show[:4]:
        # Support both new top-level fields and legacy nested exchange_error dict
        ex_err = err.get("exchange_error", {}) if isinstance(err.get("exchange_error"), dict) else {}
        venue = str(err.get("venue", ""))
        http_status = int(err.get("http_status", ex_err.get("http_status", 0)))
        ex_code = str(err.get("exchange_code", ex_err.get("exchange_code", "")))
        ex_msg = str(err.get("exchange_msg", ex_err.get("exchange_msg", "")))
        err_count = int(err.get("count", 0))
        has_body = bool(err.get("raw_body_present", False))
        if not has_body and ex_err:
            has_body = bool(ex_err.get("raw_body", ""))
        completeness = str(err.get("evidence_completeness", ex_err.get("evidence_completeness", "")))

        body_flag = "[body]" if has_body else "[NO body]"
        count_str = " x{}".format(err_count) if err_count > 1 else ""

        if ex_code:
            line = "{} HTTP{} {}: {} {}{}{}".format(
                venue, http_status, body_flag, ex_code,
                ex_msg[:60], "" if not ex_msg else "", count_str,
            )
        elif http_status > 0:
            line = "{} HTTP{} {} (no exchange code){}".format(
                venue, http_status, body_flag, count_str,
            )
        else:
            line = "{} error {} (transport){}".format(
                venue, body_flag, count_str,
            )

        if completeness and completeness != "complete":
            line += " completeness={}".format(completeness)

        evidence_lines.append(line)

    # Evidence completeness
    completeness = str(ev_completeness.get("overall", ""))
    confidence = str(ev_completeness.get("confidence", ""))
    missing = ev_completeness.get("missing_evidence", [])
    if not isinstance(missing, list):
        missing = []

    if completeness in ("partial", "missing"):
        missing_str = ", ".join(missing[:4]) if missing else "\u672a\u77e5"
        evidence_lines.append(
            "\u8bc1\u636e\u4e0d\u5b8c\u6574({}/{}): {}".format(completeness, confidence, missing_str),
        )

    # Health fingerprints
    fingerprints = health.get("fingerprints", [])
    if isinstance(fingerprints, list) and fingerprints:
        evidence_lines.append("\u5065\u5eb7\u544a\u8b66: {}".format(", ".join(fingerprints[:3])))

    # Risk
    risk_line = "\u98ce\u9669={}".format(_diagnose_risk_label(risk))
    if completeness != "complete":
        evidence_confidence = completeness if completeness else "missing"
        risk_line += ", \u8bc1\u636e={}, \u4e0d\u5f97\u6807\u8bb0\u4e3ahigh confidence".format(evidence_confidence)

    # Next actions
    next_actions = conclusion.get("next_actions", [])
    if not isinstance(next_actions, list):
        next_actions = []
    next_line = "; ".join(next_actions[:2]) if next_actions else "\u65e0\u660e\u786e\u4e0b\u4e00\u6b65"

    rendered = "\u5173\u952e\u6458\u8981\uff1a\n\u7ed3\u8bba\uff1a{}\n\u4f9d\u636e\uff1a\n{}\n\u98ce\u9669\uff1a{}\n\u4e0b\u4e00\u6b65\uff1a{}".format(
        _ensure_sentence(concl_line),
        "\n".join("- {}".format(e) for e in evidence_lines[:8])
        if evidence_lines
        else "\u672a\u63d0\u53d6\u5230\u660e\u786e\u8bc1\u636e\u3002",
        _ensure_sentence(risk_line),
        _ensure_sentence(next_line),
    )

    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 12].rstrip() + "\n...\uff08\u5df2\u7cbe\u7b80\uff09"


def _diagnose_status_label(status: str) -> str:
    labels = {
        "healthy": "\u5065\u5eb7",
        "unhealthy": "\u5f02\u5e38",
        "degraded": "\u964d\u7ea7",
        "critical": "\u4e25\u91cd",
        "unknown": "\u672a\u77e5",
    }
    return labels.get(status, status)


def _diagnose_risk_label(risk: str) -> str:
    labels = {
        "low": "\u4f4e\u98ce\u9669",
        "medium": "\u4e2d\u98ce\u9669",
        "high": "\u9ad8\u98ce\u9669",
        "critical": "\u4e25\u91cd",
    }
    return labels.get(risk, risk)
