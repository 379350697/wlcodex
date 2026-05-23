"""Concise Chinese digests for Telegram cockpit messages."""

from __future__ import annotations

import re


_SECTION_PATTERNS = {
    "conclusion": re.compile(
        r"^(?:最终结论|结论|总结|summary|diagnosis|verification_result|最终方案)\s*[:：]\s*(.*)$",
        re.IGNORECASE,
    ),
    "evidence": re.compile(r"^(?:依据|证据|关键依据|evidence)\s*[:：]\s*(.*)$", re.IGNORECASE),
    "risk": re.compile(r"^(?:风险|风险等级|risk|confidence)\s*[:：]\s*(.*)$", re.IGNORECASE),
    "next": re.compile(
        r"^(?:下一步|建议下一步|建议|next_action|next step|next steps)\s*[:：]\s*(.*)$",
        re.IGNORECASE,
    ),
    "claude_task": re.compile(
        r"^(?:Claude\s*任务|给\s*Claude\s*的任务|交给\s*Claude\s*执行|Claude\s*执行|claude_prompt|handoff_prompt)\s*[:：]\s*(.*)$",
        re.IGNORECASE,
    ),
}


def render_auto_draft_digest(text: str, *, max_chars: int = 700) -> str:
    """Render a short Chinese digest for /auto draft_ready cockpit cards.

    The full model output stays in the orchestration run. This function only
    prepares the Telegram-visible preview.
    """
    lines = _clean_lines(text)
    if not lines:
        return "关键摘要：\n结论：暂无可展示内容。\n依据：未收到模型正文。\n风险：未知。\n下一步：请重写方案或继续补充上下文。"
    if not _contains_cjk(" ".join(lines)):
        return (
            "关键摘要：\n"
            "结论：模型返回了非中文内容，已保留全文。\n"
            "依据：原文可在当前草稿/全文中查看。\n"
            "风险：驾驶舱不直接展示非中文长文，避免误读。\n"
            "下一步：请查看全文、重写方案或继续补充上下文。"
        )

    conclusion = _find_section(lines, "conclusion")
    evidence = _find_evidence(lines)
    risk = _find_section(lines, "risk")
    next_step = _find_section(lines, "next")
    claude_task = _find_claude_task(lines)

    if not conclusion:
        conclusion = _first_informative_line(lines)
    if not evidence:
        evidence = _fallback_evidence(lines, skip={conclusion, risk, next_step})
    if not risk:
        risk = "未明确风险。"
    if not next_step:
        next_step = "按下方按钮继续：交给 Claude、继续补充、重写或结束。"
    next_step = _with_claude_task(next_step, claude_task)

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
        value = match.group(1).strip()
        if value:
            return _normalize_sentence(value)
        if idx + 1 < len(lines):
            return _normalize_sentence(lines[idx + 1])
    return ""


def _find_evidence(lines: list[str]) -> list[str]:
    evidence = _find_section(lines, "evidence")
    if not evidence:
        return []
    parts = re.split(r"[；;]\s*", evidence)
    return [_trim_sentence(part, 140) for part in parts if part.strip()][:3]


def _find_claude_task(lines: list[str]) -> str:
    task = _find_section(lines, "claude_task")
    if not task:
        return ""
    return _brief_claude_task(task)


def _with_claude_task(next_step: str, claude_task: str) -> str:
    if not claude_task:
        return next_step
    if "claude" not in next_step.lower():
        return f"{next_step}；Claude 任务：{claude_task}"
    generic = re.sub(r"[。.!！\s]+$", "", next_step)
    if claude_task in generic:
        return next_step
    return f"{generic}：{claude_task}"


def _brief_claude_task(task: str) -> str:
    task = re.split(r"[；;。.!！\n]", _normalize_sentence(task), maxsplit=1)[0]
    task = re.sub(r"，?(?:让|并|同时)?下一步.*$", "", task)
    task = re.sub(r"，?(?:具体)?文件.*$", "", task)
    task = re.sub(r"，?目标.*$", "", task)
    task = re.sub(r"，?验收.*$", "", task)
    return _trim_sentence(task, 52)


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
    evidence_text = "；".join(evidence) if evidence else "未提取到明确证据。"
    return (
        "关键摘要：\n"
        f"结论：{_ensure_sentence(conclusion)}\n"
        f"依据：{_ensure_sentence(evidence_text)}\n"
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
