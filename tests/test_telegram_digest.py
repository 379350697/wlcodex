from wlcodex.telegram_digest import render_auto_draft_digest


def test_auto_draft_digest_uses_chinese_fallback_for_english_only_output() -> None:
    digest = render_auto_draft_digest(
        "summary: Claude Code and Codex both support reusable skills.\n"
        "evidence: Claude has .claude/skills and Codex has .agents/skills.\n"
        "risk: the cockpit is too verbose.\n"
        "next_action: add a digest layer before sending Telegram messages."
    )

    assert "结论：" in digest
    assert "依据：" in digest
    assert "风险：" in digest
    assert "下一步：" in digest
    assert "非中文" in digest
    assert "Claude Code and Codex" not in digest
