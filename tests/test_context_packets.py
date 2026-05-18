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
