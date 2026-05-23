from __future__ import annotations

from datetime import datetime, timezone

from wlcodex.carryover import (
    CarryoverSource,
    build_continuity_brief,
    build_carryover_preview,
    redact_sensitive_text,
    build_source_fingerprint,
)


def test_continuity_brief_is_delimited_advisory_and_short() -> None:
    source = CarryoverSource(
        source_conversation_id=36,
        title="云上部署核验",
        workspace_alias="lightfeev2",
        generated_at=datetime(2026, 5, 23, 16, 40, tzinfo=timezone.utc),
        conversation_summary="围绕云上部署和交易所状态异常展开。",
        latest_codex_summary=(
            "最新版部署后服务可运行，但业务状态异常。"
            "真实交易所无非零持仓，本地状态残留 ALTUSDT open position。"
        ),
        latest_claude_summary="",
        latest_verification_result="Binance reduce-only 400 body 尚未完整确认。",
        evidence_refs=["latest_auto_run=58", "latest_codex_analysis_run=80"],
    )

    brief = build_continuity_brief(source)

    assert brief.startswith("<carryover_context>")
    assert brief.endswith("</carryover_context>")
    assert "历史背景，仅供参考" in brief
    assert "当前用户最新输入优先" in brief
    assert "source_conversation_id=36" in brief
    assert "ALTUSDT" in brief
    assert len(brief) <= 2200


def test_carryover_redacts_credentials_and_strips_code_blocks() -> None:
    source = CarryoverSource(
        source_conversation_id=9,
        title="Sensitive",
        workspace_alias="demo",
        conversation_summary=(
            "password: secret-password\n"
            "```python\nprint('do not include code')\n```\n"
            "API key sk-test-1234567890abcdef"
        ),
        latest_codex_summary="确认问题仍未闭环。",
        evidence_refs=[],
    )

    brief = build_continuity_brief(source)

    assert "secret-password" not in brief
    assert "sk-test" not in brief
    assert "print(" not in brief
    assert "```" not in brief
    assert "[已隐藏敏感信息]" in brief


def test_carryover_preview_is_compact() -> None:
    source = CarryoverSource(
        source_conversation_id=12,
        title="Telegram 摘要优化",
        workspace_alias="wlcodex",
        conversation_summary="已实现短摘要，但下一步动作仍需更明确。",
        latest_codex_summary="需要展示 Claude 要做什么，而不是只写交给 Claude。",
    )

    preview = build_carryover_preview(source)

    assert "Claude" in preview
    assert len(preview) <= 220


def test_carryover_preview_falls_back_to_conversation_summary() -> None:
    source = CarryoverSource(
        source_conversation_id=15,
        title="Fallback",
        workspace_alias="wlcodex",
        conversation_summary="已实现短摘要，但下一步动作仍需更明确。",
    )

    preview = build_carryover_preview(source)

    assert "短摘要" in preview
    assert len(preview) <= 220


def test_redact_sensitive_text_masks_common_secret_shapes() -> None:
    text = "ssh password: abc123\nOPENAI_API_KEY=sk-abcdef1234567890\n普通结论保留。"

    redacted = redact_sensitive_text(text)

    assert "abc123" not in redacted
    assert "sk-abcdef" not in redacted
    assert "普通结论保留" in redacted


def test_source_fingerprint_prefers_latest_run_ids() -> None:
    fingerprint = build_source_fingerprint(
        conversation_id=36,
        latest_agent_run_ids=[80, 81],
        latest_orchestration_run_ids=[58],
    )

    assert fingerprint == "conversation=36;agent_runs=80,81;orchestration_runs=58"
