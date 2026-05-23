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


def test_auto_draft_digest_extracts_brief_task_from_markdown_claude_prompt() -> None:
    digest = render_auto_draft_digest(
        "```yaml\n"
        "needs_implementation: true\n"
        "do_not_start_claude: true\n"
        "```\n\n"
        "**diagnosis**\n\n"
        "线上核验发现服务运行但业务状态不健康。\n\n"
        "**evidence**\n\n"
        "- 健康检查失败。\n"
        "- 运行态有开放仓位。\n\n"
        "**claude_prompt**\n\n"
        "```text\n"
        "不要启动 Claude。本提示仅作为后续实现任务交接使用。\n\n"
        "在 LightFeeV2 仓库修复线上核验发现的问题。必须遵守 AGENTS.md。\n\n"
        "线上证据：健康检查失败，平仓路径报错。\n"
        "```\n"
    )

    assert "下一步：" in digest
    assert "修复线上核验发现的问题" in digest
    assert "按下方按钮继续" not in digest
    assert "继续补充" not in digest
    assert "不要启动 Claude" not in digest
    assert "线上证据" not in digest


def test_auto_draft_digest_preserves_key_issue_categories() -> None:
    digest = render_auto_draft_digest(
        "你说得对，前面的归纳不完整。\n\n"
        "**结论**\n\n"
        "这次不是“没有开仓”。线上已经开过仓，而且当前还挂着 1 个开放仓位。\n\n"
        "**部署状态**\n\n"
        "Git HEAD: f325b08\n"
        ".deploy_version: 8ae5629\n\n"
        "**新问题**\n\n"
        "新问题 1：ALTUSDT 平仓卡住，Binance reduce-only 被拒。\n"
        "新问题 2：risk_only + fail_closed 下仍然有重复提交行为。\n\n"
        "**老问题**\n\n"
        "老问题 1：local L2 stale/rebuild 仍在大量复现。\n\n"
        "风险等级：高。因为这是实盘仓位退出路径问题。\n",
        fallback_next="继续补充信息，或点击生成最终方案。",
    )

    assert "1 个开放仓位" in digest
    assert "ALTUSDT 平仓卡住" in digest
    assert "local L2 stale/rebuild" in digest
    assert "继续补充信息，或点击生成最终方案" in digest
    assert "你说得对" not in digest


def test_auto_draft_digest_ignores_noop_next_and_keeps_readable_actions() -> None:
    digest = render_auto_draft_digest(
        "needs_implementation: false\n"
        "diagnosis:\n"
        "最新版已部署并运行，但出现新的状态收敛问题：LightFee 本地状态仍认为有 1 笔 ALTUSDT 仓位，"
        "真实交易所已经没有非零持仓、没有开放订单。\n\n"
        "是否开仓：\n"
        "真实交易所口径：没有开仓，没有挂单。\n"
        "LightFee 本地状态口径：仍残留 1 笔 ALTUSDT open position。\n\n"
        "evidence:\n"
        "- HTTP 400 Bad Request=380\n"
        "- risk_mode=fail_closed\n"
        "- Binance 全部非零持仓：空\n"
        "- Bybit 全部开放订单：0\n\n"
        "confidence:\n"
        "高。服务状态、状态文件和交易所 API 相互印证。\n\n"
        "claude_prompt:\n"
        "不需要。该任务无需交给 Claude。\n\n"
        "next_action: 无\n"
    )

    assert "依据：\n- " in digest
    assert "状态收敛问题" in digest
    assert "真实交易所" in digest
    assert "risk_mode=fail_closed" in digest
    assert "下一步：可选：" in digest
    assert "不需要" not in digest
    assert "下一步：无" not in digest
    assert "；" not in digest


def test_auto_draft_digest_removes_rewrite_plan_from_visible_next_step() -> None:
    digest = render_auto_draft_digest(
        "结论：已经生成方案但需要用户决定。\n"
        "依据：有新的异常需要处理。\n"
        "风险：中。\n"
        "下一步：请查看全文、重写方案或继续补充上下文。"
    )

    assert "下一步：" in digest
    assert "查看全文" in digest
    assert "继续补充" in digest
    assert "重写方案" not in digest


def test_auto_draft_digest_prefers_final_verification_result_as_conclusion() -> None:
    digest = render_auto_draft_digest(
        "diagnosis:\n"
        "最新版已部署并运行，旧的 Bybit tick 对齐问题没有复现。\n\n"
        "当前主要问题是新的状态收敛问题。\n\n"
        "confidence:\n"
        "高。\n\n"
        "verification_result:\n"
        "最终结论是：最新版部署后服务运行正常，但仍有业务状态异常。新的问题是 LightFee 本地状态未与真实交易所仓位收敛。\n"
    )

    assert "结论：最新版部署后服务运行正常，但仍有业务状态异常" in digest
    assert "结论：最新版已部署并运行，旧的 Bybit" not in digest
