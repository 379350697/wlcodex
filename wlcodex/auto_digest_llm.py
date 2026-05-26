"""Optional LLM rewriting layer for Telegram-visible /auto digests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import json
import logging
import os
import re
import time
from typing import Any
import urllib.error
import urllib.request

from wlcodex.telegram_digest import render_auto_draft_digest

logger = logging.getLogger(__name__)


_DIGEST_LABELS = {
    "diagnosis": ("关键摘要", "结论", "依据", "风险", "下一步"),
    "design": ("方案摘要", "方案", "依据", "风险", "下一步"),
    "implementation": ("执行摘要", "结果", "改动", "验证", "下一步"),
}

_PROCESS_NOISE_RE = re.compile(
    r"GitNexus|writing-plans|superpowers|npx\s+gitnexus\s+analyze|"
    r"rtk\s+proxy|bwrap|sandbox|team_artifact\s*=|agent_job\s*=|"
    r"diagnose_json\s*=|confidence\s*=\s*low",
    re.IGNORECASE,
)

_BAD_ENDING_RE = re.compile(r"(?:当前|然后|基于当前|并|以及|，|、|；|:|：)$")

_IMPLEMENTATION_NEXT_NOISE_RE = re.compile(
    r"(?:交给|转交).*(?:DeepSeek|GPT|开发工程师|执行)|处理上述问题|上述问题",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DeepSeekDigestConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    flash_model: str = "deepseek-v4-flash"
    pro_model: str = "deepseek-v4-pro"
    timeout_seconds: float = 8.0
    max_input_chars: int = 0

    @classmethod
    def from_env(cls) -> "DeepSeekDigestConfig":
        raw_enabled = os.environ.get("WLCODEX_AUTO_DIGEST_LLM", "").strip().lower()
        scoped_api_key = os.environ.get("WLCODEX_DEEPSEEK_API_KEY", "").strip()
        api_key = scoped_api_key or os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if raw_enabled in {"0", "false", "no", "off"}:
            enabled = False
        elif raw_enabled in {"1", "true", "yes", "on"}:
            enabled = True
        else:
            enabled = bool(scoped_api_key)
        return cls(
            enabled=enabled,
            api_key=api_key,
            base_url=os.environ.get("WLCODEX_DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            flash_model=os.environ.get("WLCODEX_AUTO_DIGEST_FLASH_MODEL", "deepseek-v4-flash"),
            pro_model=os.environ.get("WLCODEX_AUTO_DIGEST_PRO_MODEL", "deepseek-v4-pro"),
            timeout_seconds=float(os.environ.get("WLCODEX_AUTO_DIGEST_TIMEOUT_SECONDS", "8")),
            max_input_chars=max(0, int(os.environ.get("WLCODEX_AUTO_DIGEST_MAX_INPUT_CHARS", "0"))),
        )


@dataclass(frozen=True)
class DeepSeekDigestUsage:
    model: str = ""
    digest_kind: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    source_chars: int = 0
    prompt_chars: int = 0
    response_chars: int = 0
    digest_chars: int = 0
    status: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class DeepSeekDigestCompletion:
    content: str
    usage: DeepSeekDigestUsage | None = None


DigestClient = Callable[..., Awaitable[str | DeepSeekDigestCompletion]]
DigestUsageRecorder = Callable[[DeepSeekDigestUsage], None]


async def render_auto_draft_digest_with_llm(
    text: str,
    *,
    max_chars: int = 700,
    fallback_next: str | None = None,
    digest_kind: str = "diagnosis",
    config: DeepSeekDigestConfig | None = None,
    client: DigestClient | None = None,
    usage_recorder: DigestUsageRecorder | None = None,
) -> str:
    """Render a digest with DeepSeek Flash first, Pro fallback, then rules."""
    config = config or DeepSeekDigestConfig.from_env()
    fallback = render_auto_draft_digest(
        text,
        max_chars=max_chars,
        fallback_next=fallback_next,
        digest_kind=digest_kind,
    )
    if not config.enabled:
        return fallback
    if client is None and not config.api_key:
        return fallback

    prompt = _build_prompt(
        text,
        fallback=fallback,
        digest_kind=digest_kind,
        max_input_chars=config.max_input_chars,
    )
    if client is None:
        async def call(
            *,
            model: str,
            prompt: str,
            timeout_seconds: float,
        ) -> str | DeepSeekDigestCompletion:
            return await _call_deepseek(
                model=model,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                config=config,
            )
    else:
        call = client
    for model in (config.flash_model, config.pro_model):
        try:
            response = await call(
                model=model,
                prompt=prompt,
                timeout_seconds=config.timeout_seconds,
            )
        except Exception:
            continue
        completion = _coerce_completion(response)
        rendered = _render_model_digest(
            completion.content,
            digest_kind=digest_kind,
            max_chars=max_chars,
            source_text=text,
        )
        if completion.usage is not None:
            status, failure_reason = _usage_result_status(
                rendered=bool(rendered),
                api_status=completion.usage.status,
                content=completion.content,
            )
            _record_usage(
                usage_recorder,
                replace(
                    completion.usage,
                    model=completion.usage.model or model,
                    digest_kind=digest_kind,
                    source_chars=len(text),
                    prompt_chars=len(prompt),
                    response_chars=len(completion.content),
                    digest_chars=len(rendered),
                    status=status,
                    failure_reason=failure_reason,
                ),
            )
        if rendered:
            return rendered
    return fallback


def _build_prompt(
    text: str,
    *,
    fallback: str,
    digest_kind: str,
    max_input_chars: int,
) -> str:
    title, primary, evidence, risk, next_label = (
        _DIGEST_LABELS.get(digest_kind) or _DIGEST_LABELS["diagnosis"]
    )
    clipped = text[:max_input_chars] if max_input_chars > 0 else text
    return (
        "你是 wlcodex 的 Telegram 摘要器。请忠实压缩 Codex/Claude 返回内容，"
        "只保留用户需要看到的核心事实，不添加原文没有的事实。\n"
        "输出必须是单个 JSON 对象，不能有 Markdown。\n"
        f"场景：{digest_kind}\n"
        f"字段固定为：title={title}, primary_label={primary}, evidence_label={evidence}, "
        f"risk_label={risk}, next_label={next_label}。\n"
        "JSON schema: {\"title\": string, \"primary_label\": string, \"primary\": string, "
        "\"evidence_label\": string, \"evidence_items\": string[], "
        "\"risk_label\": string, \"risk\": string, \"next_label\": string, \"next\": string}\n"
        "要求：\n"
        "- 不展示执行过程噪音，例如 GitNexus、writing-plans、superpowers、rtk proxy、bwrap、sandbox。\n"
        "- 不展示内部字段，例如 team_artifact=、agent_job=、diagnose_json=missing、confidence=low。\n"
        "- 不要硬截断半句话；长句要改写成短句。\n"
        "- evidence_items 只能放文件改动、验证命令、真实问题、真实风险，不要重复 primary。\n"
        "- 如果原文出现当前变更范围、git status 或 diff 里的文件路径，implementation 的改动必须覆盖这些文件。\n"
        "- implementation 场景优先写结果、改动、验证。\n"
        "- 如果原文能看出 workspace 或 repo，摘要必须保留仓库名，便于复核。\n"
        "- 验证字段必须优先保留原文中的实际验证命令。\n"
        "- 改动字段必须优先使用完整相对路径，不要只写文件名。\n"
        "- implementation 已完成且验证通过时，next 固定为“可以结束任务，或继续补充。”。\n"
        "- 不要把“交给 DeepSeek/GPT 开发工程师处理”写进完成态下一步。\n"
        "- design 场景不要写“结论：我会...”。\n\n"
        f"本地规则草稿：\n{fallback}\n\n"
        f"原文：\n{clipped}"
    )


async def _call_deepseek(
    *,
    model: str,
    prompt: str,
    timeout_seconds: float,
    config: DeepSeekDigestConfig | None = None,
) -> DeepSeekDigestCompletion:
    config = config or DeepSeekDigestConfig.from_env()
    if not config.api_key:
        return DeepSeekDigestCompletion("")
    return await asyncio.to_thread(
        _call_deepseek_sync,
        model,
        prompt,
        timeout_seconds,
        config.api_key,
        config.base_url,
    )


def _call_deepseek_sync(
    model: str,
    prompt: str,
    timeout_seconds: float,
    api_key: str,
    base_url: str,
) -> DeepSeekDigestCompletion:
    started = time.monotonic()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你只输出合法 JSON，不输出推理过程。",
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "max_tokens": 600,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return DeepSeekDigestCompletion(
            "",
            usage=DeepSeekDigestUsage(
                model=model,
                latency_ms=_elapsed_ms(started),
                status="request_failed",
            ),
        )
    usage = _usage_from_response(data, model=model, latency_ms=_elapsed_ms(started))
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return DeepSeekDigestCompletion("", usage=replace(usage, status="empty_response"))
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    return DeepSeekDigestCompletion(
        str(content or ""),
        usage=replace(usage, status="api_ok" if content else "empty_response"),
    )


def _coerce_completion(response: str | DeepSeekDigestCompletion) -> DeepSeekDigestCompletion:
    if isinstance(response, DeepSeekDigestCompletion):
        return response
    return DeepSeekDigestCompletion(str(response or ""))


def _record_usage(
    usage_recorder: DigestUsageRecorder | None,
    usage: DeepSeekDigestUsage,
) -> None:
    if usage_recorder is None:
        return
    try:
        usage_recorder(usage)
    except Exception:
        logger.debug("DeepSeek digest usage recorder failed", exc_info=True)


def _usage_result_status(
    *,
    rendered: bool,
    api_status: str,
    content: str,
) -> tuple[str, str]:
    if rendered:
        return "accepted", ""
    if api_status and api_status != "api_ok":
        return api_status, api_status
    if not content:
        return "empty_response", "empty_response"
    return "validation_failed", "validation_failed"


def _usage_from_response(
    data: object,
    *,
    model: str,
    latency_ms: int,
) -> DeepSeekDigestUsage:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return DeepSeekDigestUsage(model=model, latency_ms=latency_ms, status="api_ok")
    input_tokens = _int_value(
        usage,
        "prompt_tokens",
        "input_tokens",
    )
    output_tokens = _int_value(
        usage,
        "completion_tokens",
        "output_tokens",
    )
    total_tokens = _int_value(usage, "total_tokens") or input_tokens + output_tokens
    cached_input_tokens = _int_value(
        usage,
        "prompt_cache_hit_tokens",
        "cached_input_tokens",
    )
    reasoning_output_tokens = _int_value(usage, "reasoning_tokens")
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning_output_tokens = reasoning_output_tokens or _int_value(
            details,
            "reasoning_tokens",
        )
    return DeepSeekDigestUsage(
        model=model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        status="api_ok",
    )


def _int_value(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
    return 0


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _render_model_digest(
    response: str,
    *,
    digest_kind: str,
    max_chars: int,
    source_text: str = "",
) -> str:
    payload = _parse_json_object(response)
    if not payload:
        return ""
    title, primary_label, evidence_label, risk_label, next_label = (
        _DIGEST_LABELS.get(digest_kind) or _DIGEST_LABELS["diagnosis"]
    )
    if _field(payload, "primary_label") != primary_label:
        return ""
    if _field(payload, "evidence_label") != evidence_label:
        return ""
    if _field(payload, "risk_label") != risk_label:
        return ""
    if _field(payload, "next_label") != next_label:
        return ""

    primary = _clean_value(_field(payload, "primary"))
    risk = _clean_value(_field(payload, "risk"))
    risk = _merge_source_verification_command(
        risk,
        digest_kind=digest_kind,
        source_text=source_text,
    )
    next_step = _clean_value(_field(payload, "next"))
    next_step = _sanitize_next_step(
        next_step,
        digest_kind=digest_kind,
        primary=primary,
        risk=risk,
        source_text=source_text,
    )
    if not primary or not risk or not next_step:
        return ""
    if any(_invalid_visible_text(value) for value in (primary, risk, next_step)):
        return ""

    evidence_items = payload.get("evidence_items")
    if not isinstance(evidence_items, list):
        return ""
    evidence: list[str] = []
    for item in evidence_items:
        cleaned = _clean_value(str(item))
        if not cleaned or _invalid_visible_text(cleaned):
            continue
        if _same_fact(cleaned, primary):
            continue
        if any(_same_fact(cleaned, existing) for existing in evidence):
            continue
        evidence.append(cleaned)
        if len(evidence) >= 5:
            break
    if digest_kind == "implementation":
        evidence = _merge_source_change_facts(evidence, source_text, limit=5)
    if not evidence:
        return ""

    rendered = _format_digest(
        title,
        primary_label,
        primary,
        evidence_label,
        evidence,
        risk_label,
        risk,
        next_label,
        next_step,
        workspace=_extract_workspace_name(source_text),
    )
    if len(rendered) > max_chars:
        return ""
    return rendered


def _parse_json_object(text: str) -> dict[str, object]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return str(value or "").strip() if isinstance(value, str) else ""


def _clean_value(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" -")


def _invalid_visible_text(text: str) -> bool:
    normalized = _clean_value(text)
    return bool(
        _PROCESS_NOISE_RE.search(normalized)
        or _BAD_ENDING_RE.search(normalized)
        or len(normalized) > 180
    )


def _sanitize_next_step(
    next_step: str,
    *,
    digest_kind: str,
    primary: str,
    risk: str,
    source_text: str,
) -> str:
    if digest_kind != "implementation":
        return next_step
    if not _looks_completed_with_validation(primary, risk, source_text):
        return next_step
    if _IMPLEMENTATION_NEXT_NOISE_RE.search(next_step):
        return "可以结束任务，或继续补充。"
    return next_step


def _looks_completed_with_validation(primary: str, risk: str, source_text: str) -> bool:
    combined = "\n".join((primary, risk, source_text))
    has_done = bool(re.search(r"完成|已完成|开发完成|落地", combined))
    has_passed = bool(re.search(r"测试通过|验证通过|Validation passed|passed|通过", combined, re.IGNORECASE))
    return has_done and has_passed


def _merge_source_change_facts(
    evidence: list[str],
    source_text: str,
    *,
    limit: int,
) -> list[str]:
    evidence = list(evidence)
    source_facts: list[tuple[str, str]] = []
    for path, action in _extract_changed_paths(source_text):
        matching_index = _find_evidence_index_for_path(evidence, path)
        if matching_index is not None:
            if path not in evidence[matching_index]:
                evidence[matching_index] = f"{action} {path}"
            continue
        source_facts.append((path, f"{action} {path}"))
    if not source_facts:
        return evidence[:limit]

    preserved: list[str] = []
    slots_for_model_items = max(0, limit - len(source_facts))
    for item in evidence:
        if len(preserved) >= slots_for_model_items:
            break
        preserved.append(item)

    merged = preserved[:]
    for _, fact in source_facts:
        if len(merged) >= limit:
            break
        merged.append(fact)
    return merged


def _find_evidence_index_for_path(evidence: list[str], path: str) -> int | None:
    basename = path.rsplit("/", 1)[-1]
    for index, item in enumerate(evidence):
        if path in item or (basename and basename in item):
            return index
    return None


def _extract_changed_paths(source_text: str) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in source_text.splitlines():
        match = re.match(
            r"^\s*(?P<status>\?\?|A|M|D|R|C|U)\s+(?P<path>[A-Za-z0-9_./@+-]+)",
            line,
        )
        if not match:
            continue
        path = _normalize_change_path(match.group("path"))
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append((path, _status_action(match.group("status"))))

    for match in re.finditer(r"\((?P<target>[^)\s]+)\)", source_text):
        context = source_text[max(0, match.start() - 80) : match.start()]
        if not re.search(r"新增|更新|修改|落地|生成|变更", context):
            continue
        path = _normalize_change_path(match.group("target"))
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append((path, _context_action(context)))
    return paths


def _merge_source_verification_command(
    risk: str,
    *,
    digest_kind: str,
    source_text: str,
) -> str:
    if digest_kind != "implementation":
        return risk
    command = _extract_verification_command(source_text)
    if not command or command in risk:
        return risk
    return f"命令：{command}；{risk}"


def _extract_verification_command(source_text: str) -> str:
    for block in re.findall(r"```(?:bash|sh|shell)?\s*\n(.*?)```", source_text, re.DOTALL | re.IGNORECASE):
        for line in block.splitlines():
            command = _clean_command(line)
            if _looks_like_verification_command(command):
                return command
    for line in source_text.splitlines():
        command = _clean_command(line)
        if _looks_like_verification_command(command):
            return command
    return ""


def _clean_command(line: str) -> str:
    return line.strip().strip("`").strip()


def _looks_like_verification_command(command: str) -> bool:
    if not command or command.startswith("#"):
        return False
    return bool(
        re.search(
            r"\b(validate_change\.py|pytest|npm\s+test|cargo\s+(?:test|check)|compileall|git\s+diff\s+--check)\b",
            command,
        )
    )


def _extract_workspace_name(source_text: str) -> str:
    match = re.search(r"/codex/([^/\s)]+)/", source_text)
    if match:
        return match.group(1)
    match = re.search(r"/([^/\s)]+)/(?=(?:docs|README\.md|config|wlcodex|tests|scripts)/)", source_text)
    return match.group(1) if match else ""


def _normalize_change_path(raw_path: str) -> str:
    path = raw_path.strip().strip("`'\"),.;:")
    path = re.sub(r":\d+$", "", path)
    path = re.sub(r"^[ab]/", "", path)
    for marker in (
        "/docs/",
        "/README.md",
        "/config/",
        "/wlcodex/",
        "/tests/",
        "/scripts/",
    ):
        index = path.find(marker)
        if index >= 0:
            path = path[index + 1 :]
            break
    if re.match(r"^(docs|README\.md|config|wlcodex|tests|scripts)/?", path):
        return path
    return ""


def _status_action(status: str) -> str:
    return {
        "??": "新增",
        "A": "新增",
        "M": "更新",
        "D": "删除",
        "R": "重命名",
        "C": "新增",
        "U": "更新",
    }.get(status, "更新")


def _context_action(context: str) -> str:
    if re.search(r"新增|生成|落地", context):
        return "新增"
    if "删除" in context:
        return "删除"
    return "更新"


def _same_fact(left: str, right: str) -> bool:
    left = _clean_value(left).rstrip("。.!！?")
    right = _clean_value(right).rstrip("。.!！?")
    return left in right or right in left


def _ensure_sentence(text: str) -> str:
    text = _clean_value(text)
    if not text:
        return "未明确。"
    return text if text[-1] in "。！？.!?" else text + "。"


def _format_digest(
    title: str,
    primary_label: str,
    primary: str,
    evidence_label: str,
    evidence: list[str],
    risk_label: str,
    risk: str,
    next_label: str,
    next_step: str,
    workspace: str = "",
) -> str:
    evidence_text = "\n" + "\n".join(f"- {_ensure_sentence(item)}" for item in evidence)
    workspace_text = f"仓库：{workspace}\n" if workspace else ""
    return (
        f"{title}：\n"
        f"{workspace_text}"
        f"{primary_label}：{_ensure_sentence(primary)}\n"
        f"{evidence_label}：{evidence_text}\n"
        f"{risk_label}：{_ensure_sentence(risk)}\n"
        f"{next_label}：{_ensure_sentence(next_step)}"
    )
