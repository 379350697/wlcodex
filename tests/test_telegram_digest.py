from wlcodex.telegram_digest import (
    render_auto_draft_digest,
    render_missing_diagnose_digest,
    sanitize_telegram_user_text,
)


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
    assert "交给 DeepSeek 开发工程师执行" in digest
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


def test_auto_draft_digest_humanizes_protocol_fields_inside_evidence() -> None:
    digest = render_auto_draft_digest(
        "结论：未发现可复现的产品缺陷；当前症状更像是任务包信息不足，需要把下游执行范围收敛为文档-only方案。\n"
        "依据：\n"
        "- runtime/tasks/67.log: 当前任务摘要为 wlGPT 开发工程师 telegram live ok，"
        "needs_implementation=false，files_to_touch=[]，implementation_steps=[]，acceptance_criteria=[]。\n"
        "- runtime/tasks/16.log: 仅记录会话启动规则读取，无错误栈或失败日志。\n"
        "- git status --short: 工作区无未提交改动。\n"
        "- gitnexus://repo/wlGPT 开发工程师/context: 索引可用，但落后 HEAD 2 个提交，适合导航但执行前需以当前文件为准。\n"
        "- tests/test_team_artifacts.py: diagnosis_report schema 要求 symptom、expected_behavior、evidence、root_cause、minimal_fix_plan、regression_tests 等字段。\n"
        "风险：低。\n"
        "下一步：继续补充信息，或点击生成最终方案。"
    )

    assert "任务包没有给出明确的代码改动范围" in digest
    assert "没有错误栈或失败日志" in digest
    assert "工作区没有未提交改动" in digest
    assert "needs_implementation" not in digest
    assert "files_to_touch" not in digest
    assert "implementation_steps" not in digest
    assert "acceptance_criteria" not in digest
    assert "diagnosis_report schema" not in digest


def test_auto_draft_digest_hides_internal_artifact_evidence_refs() -> None:
    digest = render_auto_draft_digest(
        "{\n"
        '  "audit_report": {\n'
        '    "verdict": "PASS",\n'
        '    "summary": "README 任务范围审计通过。",\n'
        '    "risk": "LOW",\n'
        '    "test_evidence_refs": ["team_artifact=17", "agent_job=42"],\n'
        '    "passed_checks": [{"check": "测试", "evidence": "pytest tests/test_readme_flow.py passed"}],\n'
        '    "recommended_next_action": "close"\n'
        "  }\n"
        "}"
    )

    assert "pytest tests/test_readme_flow.py passed" in digest
    assert "team_artifact=17" not in digest
    assert "agent_job=42" not in digest
    assert "test_evidence_refs" not in digest


def test_missing_diagnose_digest_is_human_readable() -> None:
    digest = render_missing_diagnose_digest()

    assert "结构化诊断证据没有采集成功" in digest
    assert "置信度低" in digest
    assert "diagnose_json=missing" not in digest
    assert "confidence=low" not in digest
    assert "schema_version" not in digest


def test_auto_draft_digest_summarizes_docs_only_completion_without_duplicate_evidence() -> None:
    repeated = (
        "我会用 `superpowers:brainstorming` 先把这个文档-only小任务收敛成可执行范围，"
        "然后只改文档并做最小验证；不会碰当前已有的产品代码或测试改动。"
        "`rtk proxy` 在当前沙箱里读文件触发了 bwrap 网络地址错误；"
        "我会改用普通 `rtk` 包装的只读命令继续，保持同样的只读边界。当前"
    )
    digest = render_auto_draft_digest(
        "开发完成，测试通过。\n\n"
        "关键摘要：\n"
        f"结论：{repeated}。\n"
        "依据：\n"
        f"- {repeated}。\n"
        "- 推荐方案。\n"
        "- 在 `README.md` 的 Remote Workbench/Execution modes 附近补一小节，说明“文档-only任务”的执行规则。\n"
        "风险：未明确风险。\n"
        "下一步：可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束。"
    )

    assert "结论：文档-only小任务已完成，测试通过。" in digest
    assert repeated not in digest
    assert "当前…。" not in digest
    assert digest.count("只改文档") <= 1


def test_auto_draft_digest_can_render_design_template() -> None:
    digest = render_auto_draft_digest(
        "最终方案：在 README 新增 Documentation Map，帮助用户找到 docs 下的手册、协议和报告。\n"
        "依据：README 是入口文档；docs 目录已有 manual、protocol、smoke、specs。\n"
        "风险：低，只改文档。\n"
        "下一步：按方案更新 README。",
        digest_kind="design",
    )

    assert digest.startswith("方案摘要：")
    assert "方案：在 README 新增 Documentation Map" in digest
    assert "依据：" in digest
    assert "风险：" in digest
    assert "结论：" not in digest
    assert "关键摘要：" not in digest


def test_auto_draft_digest_can_render_single_agent_completion_template() -> None:
    process_line = (
        "我会按项目要求先用 GitNexus 了解文档结构，并用 writing-plans 形成可执行方案；"
        "这次是只改文档，所以不会触碰代码符号。GitNexus 索引提示落后 HEAD 3 个提交。"
    )
    digest = render_auto_draft_digest(
        "开发完成，测试通过。\n\n"
        "关键摘要：\n"
        f"结论：{process_line}\n"
        "依据：\n"
        f"- {process_line}\n"
        "- 在 [README.md](/tmp/work/README.md:8) 新增 `Documentation Map`，把 manual、protocol、smoke、specs 串起来。\n"
        "- 新增执行方案文档：[plan.md](/tmp/work/docs/superpowers/plans/plan.md)。\n"
        "风险：未明确风险。\n"
        "下一步：可选：交给 DeepSeek 开发工程师或 GPT 开发工程师处理上述问题，也可继续补充或结束。",
        digest_kind="implementation",
    )

    assert digest.startswith("执行摘要：")
    assert "结果：文档-only小任务已完成，测试通过。" in digest
    assert "改动：" in digest
    assert "验证：" in digest
    assert "关键摘要：" not in digest
    assert "结论：" not in digest
    assert process_line not in digest


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


def test_auto_draft_digest_does_not_show_audit_report_protocol_lines() -> None:
    digest = render_auto_draft_digest(
        "我会做第三轮当前状态复核，使用 focused-validation 的审计口径。\n"
        "{\n"
        '  "audit_report": {\n'
        '    "verdict": "PASS",\n'
        '    "summary": "README 只有一行说明改动，验证证据可信。",\n'
        '    "risk": "LOW",\n'
        '    "passed_checks": [\n'
        '      {"check": "diff_scope", "evidence": ["README.md 仅新增一行说明"]},\n'
        '      {"check": "tests", "evidence": ["pytest tests/test_readme_flow.py passed"]}\n'
        "    ],\n"
        '    "recommended_next_action": "close"\n'
        "  }\n"
        "}"
    )

    assert "README 只有一行说明改动" in digest
    assert "README.md 仅新增一行说明" in digest
    assert "pytest tests/test_readme_flow.py passed" in digest
    assert "audit_report" not in digest
    assert "verdict" not in digest
    assert '"PASS"' not in digest


def test_audit_digest_does_not_show_raw_protocol_json() -> None:
    text = render_auto_draft_digest("""
```json
{"audit_report":{"decision":"pass","summary":"Looks good","findings":[]}}
```
""")

    assert "audit_report" not in text
    assert '"decision"' not in text
    assert "Looks good" in text or "通过" in text


def test_auto_draft_digest_treats_task_scope_pass_with_warning_as_pass() -> None:
    digest = render_auto_draft_digest(
        "{\n"
        '  "audit_report": {\n'
        '    "verdict": "PASS_TASK_SCOPE_WITH_WORKTREE_WARNING",\n'
        '    "summary": "README 任务范围审计通过，但工作区有旁路改动。",\n'
        '    "risk": "LOW for README task; CRITICAL for full current worktree",\n'
        '    "passed_checks": [\n'
        '      {"check": "测试", "evidence": "编排包 command=pytest_q"}\n'
        "    ],\n"
        '    "recommended_next_action": "close"\n'
        "  }\n"
        "}"
    )

    assert "验收通过：README 任务范围审计通过" in digest
    assert "pytest_q" in digest
    assert "verdict" not in digest


def test_auto_draft_digest_renders_implementation_report_as_human_summary() -> None:
    digest = render_auto_draft_digest(
        "Claude 返工完成：\n"
        "```json\n"
        "{\n"
        '  "implementation_report": {\n'
        '    "status": "completed",\n'
        '    "change_summary": "README.md 第139行新增一行快速默认测试说明",\n'
        '    "files_modified": ["README.md"],\n'
        '    "change_location": {\n'
        '      "file": "README.md",\n'
        '      "line": 139,\n'
        '      "content": "# Use the fast default run for routine verification before broader accep"\n'
        "    }\n"
        "  }\n"
        "}\n"
        "```"
    )

    assert "返工完成：README.md 第139行新增一行快速默认测试说明" in digest
    assert "README.md" in digest
    assert "可以重新验收" in digest
    assert "implementation_report" not in digest
    assert "files_modified" not in digest
    assert "content" not in digest
    assert "broader accep" not in digest


def test_sanitize_telegram_user_text_rewrites_protocol_json_anywhere() -> None:
    text = sanitize_telegram_user_text(
        "Claude 执行完成。\n\n"
        "第三轮复核：确认当前状态后重新验证。\n\n"
        "```json\n"
        "{\n"
        '  "implementation_report": {\n'
        '    "status": "completed",\n'
        '    "change_summary": "README.md 第139行新增一行快速默认测试说明",\n'
        '    "files_modified": ["README.md"],\n'
        '    "change_location": {\n'
        '      "file": "README.md",\n'
        '      "line": 139,\n'
        '      "content": "# Use the fast default run for routine verification before broader acceptan"\n'
        "    }\n"
        "  }\n"
        "}\n"
        "```\n\n"
        "请选择下一步："
    )

    assert "返工完成：README.md 第139行新增一行快速默认测试说明" in text
    assert "请选择下一步" in text
    assert "implementation_report" not in text
    assert "files_modified" not in text
    assert "content" not in text
    assert "broader acceptan" not in text


def test_sanitize_telegram_user_text_leaves_normal_message_unchanged() -> None:
    text = "实现完成。\n\n关键摘要：\n结论：README 已更新。\n下一步：可以重新验收。"

    assert sanitize_telegram_user_text(text) == text
