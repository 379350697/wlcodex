"""Tests for Chief Engineer orchestrator."""

import pytest
from wlcodex.orchestrator import (
    ChiefEngineerOrchestrator,
    OrchestrationResult,
    VerificationDecision,
)


class FakeCodexForOrch:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.verify_decisions: list[str] = ["pass"]
        self._call_count = 0

    def echo(self, prompt: str) -> str:
        self._call_count += 1
        self.calls.append("echo")
        # First call is always analysis, subsequent calls are verification
        if self._call_count == 1:
            return "Root cause analysis: the fix is in auth.py. Implementation needed: add null check."
        else:
            idx = self._call_count - 2  # 0-based index into verify_decisions
            decision = self.verify_decisions[min(idx, len(self.verify_decisions) - 1)]
            return f"decision: {decision}\nsummary: verification result"


class FakeClaudeForOrch:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.implement_call_count = 0

    def echo(self, prompt: str) -> str:
        self.calls.append("implement")
        self.implement_call_count += 1
        return "Implementation complete. Modified auth.py and tests."


@pytest.mark.asyncio
async def test_orchestrator_passes_after_codex_verification() -> None:
    fake_codex = FakeCodexForOrch()
    fake_claude = FakeClaudeForOrch()

    orchestrator = ChiefEngineerOrchestrator(fake_codex, fake_claude, max_verify_rounds=3)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "passed"
    assert result.verify_round == 1
    assert len(fake_codex.calls) == 2  # analyze + verify
    assert len(fake_claude.calls) == 1  # implement


@pytest.mark.asyncio
async def test_orchestrator_retries_when_codex_rejects() -> None:
    fake_codex = FakeCodexForOrch()
    fake_codex.verify_decisions = ["retry", "pass"]
    fake_claude = FakeClaudeForOrch()

    orchestrator = ChiefEngineerOrchestrator(fake_codex, fake_claude, max_verify_rounds=3)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "passed"
    assert result.verify_round == 2
    assert fake_claude.implement_call_count == 2


@pytest.mark.asyncio
async def test_orchestrator_stops_on_max_rounds() -> None:
    fake_codex = FakeCodexForOrch()
    fake_codex.verify_decisions = ["retry", "retry", "retry"]
    fake_claude = FakeClaudeForOrch()

    orchestrator = ChiefEngineerOrchestrator(fake_codex, fake_claude, max_verify_rounds=3)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "needs_user"
    assert "Max verification rounds" in result.verification_summary


@pytest.mark.asyncio
async def test_orchestrator_stops_immediately() -> None:
    fake_codex = FakeCodexForOrch()
    fake_codex.verify_decisions = ["stop"]
    fake_claude = FakeClaudeForOrch()

    orchestrator = ChiefEngineerOrchestrator(fake_codex, fake_claude)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "failed"


@pytest.mark.asyncio
async def test_orchestrator_needs_user() -> None:
    fake_codex = FakeCodexForOrch()
    fake_codex.verify_decisions = ["need_user"]
    fake_claude = FakeClaudeForOrch()

    orchestrator = ChiefEngineerOrchestrator(fake_codex, fake_claude)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "needs_user"


def test_verification_decision_parse_pass() -> None:
    d = VerificationDecision.parse("decision: pass\nsummary: All checks passed")
    assert d.decision == "pass"


def test_verification_decision_parse_retry() -> None:
    d = VerificationDecision.parse("decision: retry\nsummary: Need more tests\nrequired_fix: add edge case test")
    assert d.decision == "retry"


def test_verification_decision_parse_stop() -> None:
    d = VerificationDecision.parse("decision: stop\n无法完成")
    assert d.decision == "stop"


def test_verification_decision_parse_need_user() -> None:
    d = VerificationDecision.parse("decision: need_user\n需要用户输入")
    assert d.decision == "need_user"


def test_verification_decision_parse_chinese() -> None:
    d = VerificationDecision.parse("验收通过")
    assert d.decision == "pass"


def test_verification_decision_parse_unknown() -> None:
    d = VerificationDecision.parse("random text without markers")
    assert d.decision == "need_user"
