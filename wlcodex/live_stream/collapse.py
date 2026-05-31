from __future__ import annotations

from dataclasses import dataclass, replace
import os
import re
from typing import Any

from wlcodex.auto_digest_llm import (
    DeepSeekDigestConfig,
    DigestClient,
    render_auto_draft_digest_with_llm,
)
from wlcodex.telegram_digest import render_auto_draft_digest
from wlcodex.live_stream.models import WorkerStreamEvent


@dataclass(frozen=True)
class LiveTurnSummaryConfig:
    enabled: bool = False
    timeout_seconds: float = 5.0
    max_input_chars: int = 6000
    max_title_chars: int = 90
    flash_model: str = "deepseek-v4-flash"
    pro_model: str = "deepseek-v4-pro"

    @classmethod
    def from_env(cls) -> "LiveTurnSummaryConfig":
        raw_enabled = os.environ.get("WLCODEX_LIVE_TURN_SUMMARY_LLM", "")
        return cls(
            enabled=raw_enabled.strip().lower() in {"1", "true", "yes", "on"},
            timeout_seconds=float(
                os.environ.get("WLCODEX_LIVE_TURN_SUMMARY_TIMEOUT_SECONDS", "5")
            ),
            max_input_chars=max(
                1000,
                int(os.environ.get("WLCODEX_LIVE_TURN_SUMMARY_MAX_INPUT_CHARS", "6000")),
            ),
            max_title_chars=max(
                40,
                int(os.environ.get("WLCODEX_LIVE_TURN_SUMMARY_MAX_TITLE_CHARS", "90")),
            ),
            flash_model=os.environ.get(
                "WLCODEX_LIVE_TURN_SUMMARY_FLASH_MODEL",
                "deepseek-v4-flash",
            ),
            pro_model=os.environ.get(
                "WLCODEX_LIVE_TURN_SUMMARY_PRO_MODEL",
                "deepseek-v4-pro",
            ),
        )


@dataclass(frozen=True)
class TurnCollapseSummary:
    native_turn_id: str
    event_count: int
    title: str
    preview: str = ""
    completed: bool = False
    failed: bool = False
    has_approval: bool = False
    should_collapse: bool = False
    source: str = "rules"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_turn_id": self.native_turn_id,
            "event_count": self.event_count,
            "title": self.title,
            "preview": self.preview,
            "completed": self.completed,
            "failed": self.failed,
            "has_approval": self.has_approval,
            "should_collapse": self.should_collapse,
            "source": self.source,
        }


async def summarize_turn_with_sidecar(
    events: list[WorkerStreamEvent],
    *,
    current_turn_id: str = "",
    config: LiveTurnSummaryConfig | None = None,
    client: DigestClient | None = None,
) -> TurnCollapseSummary:
    rule_summary = build_turn_collapse_summary(
        events,
        current_turn_id=current_turn_id,
    )
    config = config or LiveTurnSummaryConfig.from_env()
    if not config.enabled or not events:
        return rule_summary

    digest_config = _deepseek_config(config)
    if client is None and not digest_config.api_key:
        return rule_summary
    source_text = _events_to_sidecar_text(events, max_chars=config.max_input_chars)
    sidecar_fallback = render_auto_draft_digest(
        source_text,
        max_chars=700,
        fallback_next="可以展开查看完整过程。",
        digest_kind="implementation",
    )
    try:
        digest = await render_auto_draft_digest_with_llm(
            source_text,
            max_chars=700,
            fallback_next="可以展开查看完整过程。",
            digest_kind="implementation",
            config=digest_config,
            client=client,
        )
    except Exception:
        return rule_summary
    if digest == sidecar_fallback:
        return rule_summary
    title = _title_from_digest(digest, max_chars=config.max_title_chars)
    if not title:
        return rule_summary
    return replace(
        rule_summary,
        title=f"{rule_summary.event_count} 条消息：{title}",
        preview=digest,
        source="deepseek",
    )


def build_turn_collapse_summary(
    events: list[WorkerStreamEvent],
    *,
    current_turn_id: str = "",
) -> TurnCollapseSummary:
    native_turn_id = _native_turn_id(events)
    completed = any(_is_completed_event(event) for event in events)
    failed = any(_is_failed_event(event) for event in events)
    has_approval = any(event.kind == "approval_requested" for event in events)
    text = _best_rule_title(events)
    event_count = len(events)
    should_collapse = bool(
        event_count
        and not failed
        and not has_approval
        and native_turn_id
        and native_turn_id != current_turn_id
    )
    return TurnCollapseSummary(
        native_turn_id=native_turn_id,
        event_count=event_count,
        title=f"{event_count} 条消息：{text}",
        preview=text,
        completed=completed,
        failed=failed,
        has_approval=has_approval,
        should_collapse=should_collapse,
    )


def _deepseek_config(config: LiveTurnSummaryConfig) -> DeepSeekDigestConfig:
    base = DeepSeekDigestConfig.from_env()
    return DeepSeekDigestConfig(
        enabled=True,
        api_key=base.api_key,
        base_url=base.base_url,
        flash_model=config.flash_model,
        pro_model=config.pro_model,
        timeout_seconds=config.timeout_seconds,
        max_input_chars=config.max_input_chars,
    )


def _native_turn_id(events: list[WorkerStreamEvent]) -> str:
    for event in events:
        native_turn_id = str(event.payload.get("native_turn_id") or "").strip()
        if native_turn_id:
            return native_turn_id
    return ""


def _is_completed_event(event: WorkerStreamEvent) -> bool:
    payload = event.payload
    if event.kind == "completed":
        return True
    return (
        event.kind == "activity"
        and str(payload.get("action") or "") == "turn_completed"
        and not _is_failed_status(str(payload.get("status") or ""))
    )


def _is_failed_event(event: WorkerStreamEvent) -> bool:
    if event.kind == "failed":
        return True
    return _is_failed_status(str(event.payload.get("status") or ""))


def _is_failed_status(status: str) -> bool:
    return status.strip().lower() in {
        "failed",
        "error",
        "cancelled",
        "canceled",
    }


def _best_rule_title(events: list[WorkerStreamEvent]) -> str:
    for event in events:
        if event.kind == "user_message":
            text = _payload_text(event.payload)
            if text:
                return _compact_text(text)
    assistant_text = "".join(
        _payload_text(event.payload)
        for event in events
        if event.kind in {"text_delta", "reasoning_delta"}
    )
    if assistant_text:
        return _compact_text(assistant_text)
    for event in reversed(events):
        text = _payload_text(event.payload)
        if text:
            return _compact_text(text)
    return "Codex 会话"


def _events_to_sidecar_text(
    events: list[WorkerStreamEvent],
    *,
    max_chars: int,
) -> str:
    lines: list[str] = []
    for event in events:
        payload = event.payload
        text = _payload_text(payload)
        if not text and event.kind in {"completed", "failed"}:
            text = str(payload.get("status") or event.kind)
        if not text:
            continue
        lines.append(f"{event.kind}: {_compact_text(text, limit=1000)}")
    return "\n".join(lines)[:max_chars]


def _title_from_digest(text: str, *, max_chars: int) -> str:
    for line in text.splitlines():
        cleaned = _compact_text(line, limit=max_chars)
        if not cleaned:
            continue
        for prefix in ("执行摘要：", "结果：", "关键摘要：", "方案摘要："):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
        if cleaned:
            return cleaned
    return ""


def _payload_text(payload: dict[str, Any]) -> str:
    for key in ("text", "delta", "summary", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    item = payload.get("item")
    if isinstance(item, dict):
        for key in ("text", "summary", "command"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _compact_text(text: str, *, limit: int = 90) -> str:
    compacted = re.sub(r"\s+", " ", text).strip()
    if len(compacted) <= limit:
        return compacted
    return compacted[: max(0, limit - 1)].rstrip() + "…"
