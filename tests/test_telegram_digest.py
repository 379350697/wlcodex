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


def test_auto_draft_digest_shows_only_brief_claude_handoff_task() -> None:
    digest = render_auto_draft_digest(
        "最终方案：需要交给 Claude 做一次代码修复。\n"
        "依据：验收发现 /auto 完成卡片只说交给 Claude，没有说明交接任务。\n"
        "风险：用户无法判断是否应该点击执行。\n"
        "下一步：交给 Claude 执行。\n"
        "Claude 任务：修改 Telegram 驾驶舱摘要卡，让下一步显示要 Claude 修复的具体文件、目标和验收命令。\n"
        "files_to_touch: wlcodex/telegram_digest.py, tests/test_telegram_digest.py\n"
        "acceptance_criteria: 摘要里必须出现 Claude 要做什么，而不是只出现交给 Claude。\n"
    )

    assert "下一步：" in digest
    assert "交给 Claude 执行" in digest
    assert "摘要卡" in digest
    assert "wlcodex/telegram_digest.py" not in digest
    assert "tests/test_telegram_digest.py" not in digest
    assert "验收命令" not in digest
