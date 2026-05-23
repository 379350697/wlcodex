"""Concise Chinese digests for Telegram cockpit messages."""

from __future__ import annotations

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
    conclusion = _brief_conclusion(conclusion)
    if key_points:
        evidence = _merge_evidence(key_points, evidence)
    elif not evidence or _is_low_value_evidence(evidence):
        evidence = key_points
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
        if line:
            cleaned.append(line)
    return cleaned


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


def _brief_conclusion(text: str) -> str:
    text = re.sub(r"^(?:最终结论是|最终结论)\s*[:：]\s*", "", _normalize_sentence(text))
    text = re.sub(r"旧的[^。；;]*没有复现[。；;]?", "", text)
    text = re.sub(r"之前的[^。；;]*没有(?:按原形)?复现[。；;]?", "", text)
    if "新的问题是" in text:
        before, after = text.split("新的问题是", 1)
        issue = re.split(r"[。；;]", after, maxsplit=1)[0].strip()
        prefix = before.strip(" ；;。")
        text = f"{prefix}；新问题是：{issue}" if prefix else f"新问题是：{issue}"
    return _trim_sentence(text, 180)


def _find_evidence(lines: list[str]) -> list[str]:
    evidence = _find_section(lines, "evidence")
    if not evidence:
        evidence = _find_section(lines, "executed_check")
    if not evidence:
        return []
    parts = re.split(r"[；;]\s*", evidence)
    return [_trim_sentence(part, 140) for part in parts if part.strip()][:3]


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
        return f"交给 Claude 执行：{claude_task}"
    if "claude" not in next_step.lower():
        return f"{next_step}；Claude 任务：{claude_task}"
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
        or _normalize_sentence(next_step) in {"交给 Claude 执行", "交给 Claude"}
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
        return "可选：交给 Claude 或 Codex 处理状态收敛/残留仓位问题，也可继续补充或结束"
    if any(token in combined for token in ("平仓", "reduce-only", "http 400", "bad request")):
        return "可选：交给 Claude 或 Codex 排查平仓失败，也可继续补充或结束"
    if any(token in combined for token in ("bug", "问题", "风险", "异常", "失败", "优化")):
        return "可选：交给 Claude 或 Codex 处理上述问题，也可继续补充或结束"
    return "可选：继续补充、交给 Claude/Codex 处理或结束"


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
    return re.sub(r"\s+", " ", text).strip(" -:：")


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
