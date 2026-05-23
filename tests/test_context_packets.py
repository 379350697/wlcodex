"""Tests for compact context packet builders."""

import pytest
from wlcodex.context_packets import (
    ContextBudget,
    ContextPacket,
    CodexAnalysisPacket,
    ClaudeHandoffPacket,
    CodexVerificationPacket,
    approx_tokens,
    trim_to_budget,
    build_codex_analysis_packet,
    build_claude_handoff_packet,
    build_codex_verification_packet,
    build_auto_context_packet,
    build_auto_final_plan_packet,
    build_auto_verification_packet,
    build_auto_repair_packet,
)


def test_approx_tokens_estimate() -> None:
    assert approx_tokens("") == 1
    assert approx_tokens("hello") == 1
    assert approx_tokens("hello world") == 2
    assert approx_tokens("abc") == 1
    assert approx_tokens("hello world! test") == 4


def test_trim_to_budget_keeps_short_text() -> None:
    result = trim_to_budget("short text", max_tokens=100)
    assert "short text" in result


def test_trim_to_budget_truncates_long_text() -> None:
    long_text = "x" * 10000
    result = trim_to_budget(long_text, max_tokens=100)
    assert approx_tokens(result) <= 100


def test_codex_analysis_packet_render() -> None:
    packet = CodexAnalysisPacket(
        mode="chief_engineer",
        workspace="wlcodex",
        user_goal="Fix login bug",
        current_request="Analyze auth.py null path",
        conversation_summary="User wants null safety",
        relevant_files=["auth.py", "tests/test_auth.py"],
        recent_user_constraints=["Must pass CI"],
        acceptance_criteria=["No crash on null user"],
        token_budget=2500,
        requested_output="Root cause analysis and implementation plan",
    )

    rendered = packet.render()
    assert "Fix login bug" in rendered
    assert "auth.py" in rendered
    assert "chief_engineer" in rendered
    assert "Root cause analysis" in rendered


def test_codex_analysis_packet_can_execute_user_query_without_handoff() -> None:
    packet = build_codex_analysis_packet(
        user_goal="查一下结构是否臃肿",
        handoff=False,
    )

    rendered = packet.render()
    assert "真实执行必要的查询和核验" in rendered
    assert "不要只输出执行计划" in rendered
    assert "不要输出 Claude 交接包" in rendered
    assert "只读分析" not in rendered
    assert "禁止创建、修改、删除任何工作区文件" not in rendered
    assert "禁止部署、重启、写配置" not in rendered
    assert "Chief-engineer Claude handoff packet" not in rendered


def test_claude_handoff_packet_render() -> None:
    packet = ClaudeHandoffPacket(
        mode="chief_engineer",
        workspace="wlcodex",
        user_goal="Fix login bug",
        current_request="Implement null-check in auth.py",
        conversation_summary="User wants null safety",
        relevant_files=["auth.py", "tests/test_auth.py"],
        recent_user_constraints=["Must pass CI"],
        acceptance_criteria=["No crash on null user"],
        token_budget=1500,
        handoff_from_codex=ClaudeHandoffPacket.HandoffFromCodex(
            objective="Add null check in auth.py",
            files_to_touch=["auth.py", "tests/test_auth.py"],
            steps=["Guard user.name access", "Add test for None"],
            constraints=["Do not change public API"],
            acceptance_criteria=["No crash on null user"],
            prohibited_changes=["auth.py signature"],
        ),
    )

    rendered = packet.render()
    assert "Fix login bug" in rendered
    assert "Add null check" in rendered
    assert "auth.py" in rendered
    assert "Guard user.name" in rendered


def test_codex_verification_packet_render() -> None:
    packet = CodexVerificationPacket(
        mode="chief_engineer",
        workspace="wlcodex",
        user_goal="Fix login bug",
        current_request="Verify changes",
        conversation_summary="User wants null safety",
        relevant_files=["auth.py", "tests/test_auth.py"],
        recent_user_constraints=["Must pass CI"],
        acceptance_criteria=["No crash on null user"],
        token_budget=2500,
        original_goal="Fix login bug",
        codex_plan_summary="Add null guard in auth.py",
        claude_completion_summary="Modified 2 files, tests pass",
        changed_files=["auth.py", "tests/test_auth.py"],
        diff_excerpt_or_summary="+if user.name is None: ...",
        test_results="All tests pass",
        verification_question="Does this fix the null path?",
    )

    rendered = packet.render()
    assert "Fix login bug" in rendered
    assert "Modified 2 files" in rendered
    assert "All tests pass" in rendered
    assert "verification" in rendered.lower()


def test_codex_to_claude_packet_excludes_full_transcript() -> None:
    packet = build_claude_handoff_packet(
        user_goal="修复登录 bug",
        codex_analysis="根因是 auth.py 没有处理 user.name 为空。",
        implementation_steps=["修改 auth.py", "补 tests/test_auth.py"],
        acceptance_criteria=["空用户不崩溃", "测试通过"],
        telegram_transcript="USER: " + "很长聊天 " * 1000,
        budget=ContextBudget(codex_to_claude_tokens=300),
    )

    rendered = packet.render()
    assert "很长聊天" not in rendered
    assert "修复登录 bug" in rendered
    assert "修改 auth.py" in rendered


def test_claude_handoff_packet_tells_claude_to_background_long_commands() -> None:
    packet = build_claude_handoff_packet(
        user_goal="跑完整验收",
        codex_analysis="需要执行较长测试。",
    )

    rendered = packet.render()

    assert "run_in_background" in rendered
    assert "BashOutput" in rendered


def test_packet_within_budget() -> None:
    budget = ContextBudget(codex_to_claude_tokens=100)
    packet = ClaudeHandoffPacket(
        mode="chief_engineer",
        workspace="wlcodex",
        user_goal="Fix bug",
        current_request="Implement fix",
        conversation_summary="Bug report",
        relevant_files=["file.py"],
        recent_user_constraints=[],
        acceptance_criteria=["Fix works"],
        token_budget=100,
        handoff_from_codex=ClaudeHandoffPacket.HandoffFromCodex(
            objective="Fix bug",
            files_to_touch=["file.py"],
            steps=["Fix it"],
            constraints=[],
            acceptance_criteria=["Fix works"],
            prohibited_changes=[],
        ),
    )
    assert packet.within_budget()


def test_packet_over_budget() -> None:
    budget = ContextBudget(codex_to_claude_tokens=5)
    packet = ClaudeHandoffPacket(
        mode="chief_engineer",
        workspace="wlcodex",
        user_goal="Fix bug",
        current_request="Implement fix",
        conversation_summary="Bug report",
        relevant_files=["file.py"],
        recent_user_constraints=[],
        acceptance_criteria=["Fix works"],
        token_budget=5,
        handoff_from_codex=ClaudeHandoffPacket.HandoffFromCodex(
            objective="Fix bug",
            files_to_touch=["file.py"],
            steps=["Fix it"],
            constraints=[],
            acceptance_criteria=["Fix works"],
            prohibited_changes=[],
        ),
    )
    assert not packet.within_budget()


def test_build_codex_analysis_packet() -> None:
    packet = build_codex_analysis_packet(
        user_goal="Analyze auth module",
        conversation_summary="User exploring auth",
        relevant_files=["auth.py"],
        workspace="wlcodex",
    )
    rendered = packet.render()
    assert "Analyze auth module" in rendered
    assert "auth.py" in rendered
    assert "必须使用中文" in rendered


def test_build_codex_analysis_packet_is_analysis_only() -> None:
    packet = build_codex_analysis_packet(
        user_goal="实现 /summary 命令",
        workspace="wlcodex",
    )

    rendered = packet.render()

    assert "不要直接完成 Claude 的实现补丁" in rendered
    assert "可以调用 skill" in rendered
    assert "docs/ 或 .wlcodex/" in rendered
    assert "不要修改业务代码、测试代码、依赖锁或配置" in rendered
    assert "交给 Claude" in rendered


def test_build_codex_verification_packet() -> None:
    packet = build_codex_verification_packet(
        user_goal="Fix login bug",
        codex_plan_summary="Add null guard",
        claude_completion_summary="Modified 2 files",
        changed_files=["auth.py"],
        test_results="All pass",
        workspace="wlcodex",
    )
    rendered = packet.render()
    assert "Fix login bug" in rendered
    assert "Add null guard" in rendered
    assert "Modified 2 files" in rendered
    assert "必须使用中文" in rendered


def test_all_built_packets_require_chinese_output() -> None:
    packets = [
        build_codex_analysis_packet(user_goal="分析问题"),
        build_claude_handoff_packet(user_goal="实现修复", codex_analysis="方案"),
        build_codex_verification_packet(user_goal="验收修复"),
    ]

    for packet in packets:
        rendered = packet.render()
        assert "必须使用中文" in rendered
        assert "不要输出英文" in rendered


def test_packet_summary_is_compact() -> None:
    packet = build_codex_analysis_packet(
        user_goal="Analyze auth module for security issues",
        conversation_summary="User wants a security review",
        relevant_files=["auth.py", "middleware.py"],
        workspace="wlcodex",
    )
    summary = packet.summary()
    assert approx_tokens(summary) <= 200


def test_context_packet_base_render() -> None:
    packet = ContextPacket(
        mode="codex_direct",
        workspace="wlcodex",
        user_goal="Test",
        current_request="Test request",
        conversation_summary="Test summary",
        relevant_files=["test.py"],
        recent_user_constraints=["Test constraint"],
        acceptance_criteria=["Test works"],
        token_budget=1000,
    )
    rendered = packet.render()
    assert "Test" in rendered
    assert "test.py" in rendered
    assert "Test constraint" in rendered


# --- Staged-auto context packet tests ---


def test_auto_context_collection_packet_allows_real_diagnostics_and_not_handoff() -> None:
    packet = build_auto_context_packet(
        user_goal="定位偶发失败",
        conversation_summary="用户会继续补充日志",
        workspace="lightfeev2",
    )
    rendered = packet.render()

    assert "真实执行必要的查询和远程核验" in rendered
    assert "ssh/curl/systemctl/journalctl/git log/docker ps" in rendered
    assert "不要只输出核验计划" in rendered
    assert "只读分析" not in rendered
    assert "禁止创建、修改、删除任何工作区文件" not in rendered
    assert "禁止部署、重启、写配置" not in rendered
    # Must NOT contain Claude handoff instructions
    assert "Claude handoff packet" not in rendered
    assert "交给 Claude" not in rendered
    assert "auto_collecting_context" in rendered


def test_auto_context_collection_packet_mentions_workspace() -> None:
    packet = build_auto_context_packet(
        user_goal="查一下问题",
        workspace="lightfeev2",
    )
    rendered = packet.render()
    assert "lightfeev2" in rendered


def test_auto_final_plan_packet_answers_query_or_requests_claude_prompt() -> None:
    packet = build_auto_final_plan_packet(
        user_goal="修复登录错误",
        conversation_summary="已确认是空用户路径",
        workspace="wlcodex",
    )
    rendered = packet.render()

    assert "最终方案" in rendered
    assert "查询/核验类任务" in rendered
    assert "如果需要实现，再包含给 Claude 的执行提示词" in rendered
    assert "acceptance_criteria" in rendered or "验收标准" in rendered.lower()
    assert "不要只输出下一步计划" in rendered


def test_auto_final_plan_packet_mentions_no_implementation_flag() -> None:
    packet = build_auto_final_plan_packet(
        user_goal="检查冗余代码",
        workspace="wlcodex",
    )
    rendered = packet.render()
    assert "needs_implementation: false" in rendered
    assert "最终结论" in rendered


def test_auto_verification_packet_includes_round() -> None:
    packet = build_auto_verification_packet(
        user_goal="验证修复",
        codex_plan_summary="修改 auth.py",
        claude_completion_summary="已修改",
        verify_round=2,
        workspace="wlcodex",
    )
    rendered = packet.render()
    assert "第 2 轮验收" in rendered
    assert "repair_prompt" in rendered or "返工" in rendered


def test_auto_verification_packet_is_read_only() -> None:
    packet = build_auto_verification_packet(
        user_goal="验证修复",
        workspace="wlcodex",
    )
    rendered = packet.render()
    assert "只读" in rendered
    assert "不要发送 Telegram 消息" in rendered


def test_auto_repair_packet_restricts_scope() -> None:
    packet = build_auto_repair_packet(
        user_goal="修复登录错误",
        codex_plan_summary="修改 auth.py",
        claude_completion_summary="已修改但未通过验收",
        verification_result="tests/test_auth.py 第 42 行断言失败",
        workspace="wlcodex",
    )
    rendered = packet.render()
    assert "修复" in rendered
    assert "不要扩大范围" in rendered
    # Must not contain Telegram delivery language
    assert "绝对不要发送 Telegram" in rendered


def test_all_auto_packets_require_chinese_output() -> None:
    packets = [
        build_auto_context_packet(user_goal="分析问题"),
        build_auto_final_plan_packet(user_goal="实现修复"),
        build_auto_verification_packet(user_goal="验收修复"),
        build_auto_repair_packet(user_goal="返工修复"),
    ]
    for packet in packets:
        rendered = packet.render()
        assert "必须使用中文" in rendered
