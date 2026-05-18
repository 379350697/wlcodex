"""Tests for Chief Engineer orchestrator."""

import pytest
from wlcodex.orchestrator import (
    ChiefEngineerOrchestrator,
    OrchestrationProgress,
    OrchestrationResult,
    VerificationDecision,
    _analysis_says_no_implementation_needed,
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


@pytest.mark.asyncio
async def test_orchestrator_does_not_call_claude_for_noop_greeting() -> None:
    class NoopCodex:
        def __init__(self) -> None:
            self.calls = 0

        def echo(self, prompt: str) -> str:
            self.calls += 1
            return (
                "当前没有具体故障或变更请求，只有问候语，所以暂无可分析的根因。\n"
                "无需实施计划。"
            )

    fake_codex = NoopCodex()
    fake_claude = FakeClaudeForOrch()

    orchestrator = ChiefEngineerOrchestrator(fake_codex, fake_claude)
    result = await orchestrator.run("你好")

    assert result.status == "passed"
    assert result.verify_round == 0
    assert fake_codex.calls == 1
    assert fake_claude.implement_call_count == 0


@pytest.mark.asyncio
async def test_orchestrator_does_not_implement_reply_only_probe() -> None:
    codex = FakeCodexWithSendPrompt()
    codex._responses = ["wlcodex telegram live ok"]
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    result = await orchestrator.run("请用中文只回复：wlcodex telegram live ok")

    assert result.status == "passed"
    assert result.verify_round == 0
    assert len(codex.prompts) == 1
    assert claude.prompts == []


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


def test_analysis_json_false_means_no_implementation_needed() -> None:
    assert _analysis_says_no_implementation_needed(
        '{"summary":"这是查询类请求","needs_implementation":false,'
        '"files_to_touch":[],"implementation_steps":[]}'
    )


# ---------------------------------------------------------------------------
# Real interface tests: orchestrator uses send_codex_prompt and send()
# ---------------------------------------------------------------------------


class FakeCodexWithSendPrompt:
    """Fake Codex that implements send_codex_prompt (real interface)."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []  # (workspace, prompt)
        self._responses: list[str] = []

    async def send_codex_prompt(self, workspace_path: str, prompt: str) -> str:
        self.prompts.append((workspace_path, prompt))
        if self._responses:
            return self._responses.pop(0)
        return "decision: pass\nsummary: All checks passed.\nconfidence: high"


class FakeClaudeWithSend:
    """Fake Claude that implements AgentBackend.send (real interface)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._responses: list[str] = []

    async def send(self, request):
        from wlcodex.agent_backend import AgentResult
        self.prompts.append(request.prompt)
        text = self._responses.pop(0) if self._responses else "Implementation complete."
        return AgentResult(text=text, exit_code=0, token_input=100, token_output=200)


class FakeCodexWithPromptModes:
    def __init__(self) -> None:
        self.modes: list[str] = []
        self._responses = [
            "Root cause: implementation needed.",
            "decision: pass\nsummary: verified",
        ]

    async def send_codex_prompt(
        self,
        workspace_path: str,
        prompt: str,
        *,
        interaction_mode: str = "general",
    ) -> str:
        self.modes.append(interaction_mode)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_orchestrator_uses_send_codex_prompt_interface() -> None:
    """Orchestrator must prefer send_codex_prompt over echo when available."""
    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "passed"
    # Must have called send_codex_prompt (real interface)
    assert len(codex.prompts) >= 2  # analyze + verify
    # Each call must include the workspace
    for ws, prompt in codex.prompts:
        assert "修复登录 bug" in prompt or "verification" in prompt.lower() or "login" in prompt.lower()
    # Must have called send (real Claude interface)
    assert len(claude.prompts) == 1


@pytest.mark.asyncio
async def test_orchestrator_marks_codex_analysis_and_verification_modes() -> None:
    codex = FakeCodexWithPromptModes()
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    result = await orchestrator.run("实现一个小功能")

    assert result.status == "passed"
    assert codex.modes == ["analysis", "verification"]


@pytest.mark.asyncio
async def test_orchestrator_uses_send_interface_for_claude() -> None:
    """Orchestrator must prefer send() over echo for Claude calls."""
    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    await orchestrator.run("修复登录 bug")

    # The Claude prompt must be a rendered packet
    assert len(claude.prompts) == 1
    assert "mode:" in claude.prompts[0]  # Packet rendering


@pytest.mark.asyncio
async def test_orchestrator_handles_codex_retry_with_real_interfaces() -> None:
    """Retry loop must work with send_codex_prompt and send interfaces."""
    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        # Analysis response
        "Root cause: null check needed in auth.py.",
        # First verification: retry
        "decision: retry\nsummary: Need more tests.\nrequired_fix: Add edge case tests.",
        # Second verification: pass
        "decision: pass\nsummary: All good.",
    ]
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=3)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "passed"
    assert result.verify_round == 2
    assert len(codex.prompts) == 3  # analyze + 2 verifications
    assert len(claude.prompts) == 2  # 2 implementations


# ---------------------------------------------------------------------------
# Streaming orchestrator tests
# ---------------------------------------------------------------------------


class FakeClaudeStreaming:
    """Fake Claude that implements send_streaming (real streaming interface)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._deltas: list[str] = ["impl", "ementation", " done"]

    async def send_streaming(self, request):
        from wlcodex.agent_backend import AgentStreamEvent
        self.prompts.append(request.prompt)
        for delta in self._deltas:
            yield AgentStreamEvent(delta=delta, event_type="text")


@pytest.mark.asyncio
async def test_run_streaming_yields_progress_events() -> None:
    """run_streaming must yield OrchestrationProgress events at each stage."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    events = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        events.append(progress)

    # Must have at least: analysis_started, analysis_complete, impl deltas,
    # impl_complete, verify_started, verify_complete, complete
    phases = [e.phase for e in events]
    assert OrchestrationProgress.ANALYSIS_STARTED in phases
    assert OrchestrationProgress.ANALYSIS_COMPLETE in phases
    assert OrchestrationProgress.IMPL_DELTA in phases
    assert OrchestrationProgress.IMPL_COMPLETE in phases
    assert OrchestrationProgress.VERIFY_STARTED in phases
    assert OrchestrationProgress.VERIFY_COMPLETE in phases
    assert OrchestrationProgress.COMPLETE in phases


@pytest.mark.asyncio
async def test_run_streaming_forwards_claude_deltas() -> None:
    """run_streaming must yield each Claude delta as IMPL_DELTA progress."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeStreaming()
    claude._deltas = ["chunk1", "chunk2", "chunk3"]

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    deltas = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        if progress.phase == OrchestrationProgress.IMPL_DELTA:
            deltas.append(progress.text)

    assert deltas == ["chunk1", "chunk2", "chunk3"]


@pytest.mark.asyncio
async def test_run_streaming_does_not_second_call() -> None:
    """Streaming must not launch a second model call — same run, same deltas."""
    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    async for _ in orchestrator.run_streaming("修复登录 bug"):
        pass

    # One analysis call (send_codex_prompt), one Claude streaming call
    assert len(codex.prompts) == 2  # analyze + verify
    assert len(claude.prompts) == 1  # single implementation call (streaming)


@pytest.mark.asyncio
async def test_run_streaming_emits_failed_on_codex_error() -> None:
    """run_streaming must yield FAILED event when Codex analysis fails."""
    from wlcodex.orchestrator import OrchestrationProgress

    class FailingCodex:
        def __init__(self) -> None:
            self.prompts: list[tuple[str, str]] = []

        async def send_codex_prompt(self, workspace_path: str, prompt: str) -> str:
            self.prompts.append((workspace_path, prompt))
            raise RuntimeError("Codex unavailable")

    codex = FailingCodex()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    phases = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        phases.append(progress.phase)

    assert OrchestrationProgress.FAILED in phases
    assert OrchestrationProgress.COMPLETE in phases


@pytest.mark.asyncio
async def test_run_streaming_falls_back_to_blocking_send() -> None:
    """When send_streaming is not available, run_streaming falls back to send()."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeWithSend()  # Only has send(), no send_streaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    deltas = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        if progress.phase == OrchestrationProgress.IMPL_DELTA:
            deltas.append(progress.text)

    # Should still work (fallback to send)
    assert len(deltas) >= 1  # Full text returned as single delta
    assert len(claude.prompts) == 1


# ---------------------------------------------------------------------------
# result_status on COMPLETE events (Issue 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streaming_complete_has_result_status_passed() -> None:
    """COMPLETE event after verification pass must carry result_status='passed'."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    complete_events = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        if progress.phase == OrchestrationProgress.COMPLETE:
            complete_events.append(progress)

    assert len(complete_events) == 1
    assert complete_events[0].result_status == "passed"


@pytest.mark.asyncio
async def test_run_streaming_complete_has_result_status_failed_on_stop() -> None:
    """COMPLETE after verification stop must carry result_status='failed'."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        "Root cause analysis: fix needed in auth.py.",
        "decision: stop\n无法完成",
    ]
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    complete_events = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        if progress.phase == OrchestrationProgress.COMPLETE:
            complete_events.append(progress)

    assert len(complete_events) == 1
    assert complete_events[0].result_status == "failed"


@pytest.mark.asyncio
async def test_run_streaming_complete_has_result_status_needs_user() -> None:
    """COMPLETE after verification needs_user must carry result_status='needs_user'."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        "Root cause analysis: fix needed in auth.py.",
        "decision: need_user\n需要用户输入",
    ]
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    complete_events = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        if progress.phase == OrchestrationProgress.COMPLETE:
            complete_events.append(progress)

    assert len(complete_events) == 1
    assert complete_events[0].result_status == "needs_user"


@pytest.mark.asyncio
async def test_run_streaming_does_not_implement_reply_only_probe() -> None:
    codex = FakeCodexWithSendPrompt()
    codex._responses = ["wlcodex telegram live ok"]
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    events = []
    async for progress in orchestrator.run_streaming(
        "请用中文只回复：wlcodex telegram live ok"
    ):
        events.append(progress)

    assert len(codex.prompts) == 1
    assert claude.prompts == []
    assert not any(
        event.phase == OrchestrationProgress.IMPL_DELTA for event in events
    )
    complete_events = [
        event for event in events
        if event.phase == OrchestrationProgress.COMPLETE
    ]
    assert complete_events[0].result_status == "passed"
    assert complete_events[0].round_num == 0


@pytest.mark.asyncio
async def test_run_streaming_failed_has_result_status_on_codex_error() -> None:
    """COMPLETE after Codex analysis failure must carry result_status='failed'."""
    from wlcodex.orchestrator import OrchestrationProgress

    class FailingCodex:
        async def send_codex_prompt(self, workspace_path: str, prompt: str) -> str:
            raise RuntimeError("Codex unavailable")

    codex = FailingCodex()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    complete_events = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        if progress.phase == OrchestrationProgress.COMPLETE:
            complete_events.append(progress)

    assert len(complete_events) == 1
    assert complete_events[0].result_status == "failed"


# ---------------------------------------------------------------------------
# Phase event forwarding (Issue 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streaming_emits_all_phase_events() -> None:
    """run_streaming must emit analysis_started, impl_complete, verify_started,
    and verify_complete phase events so the controller can forward them."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    phases = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        phases.append(progress.phase)

    assert OrchestrationProgress.ANALYSIS_STARTED in phases
    assert OrchestrationProgress.ANALYSIS_COMPLETE in phases
    assert OrchestrationProgress.IMPL_COMPLETE in phases
    assert OrchestrationProgress.VERIFY_STARTED in phases
    assert OrchestrationProgress.VERIFY_COMPLETE in phases


@pytest.mark.asyncio
async def test_run_streaming_phase_events_have_text() -> None:
    """Phase events ANALYSIS_STARTED, VERIFY_STARTED must carry user-visible text."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        if progress.phase == OrchestrationProgress.ANALYSIS_STARTED:
            assert "Codex" in progress.text or "分析" in progress.text
        elif progress.phase == OrchestrationProgress.VERIFY_STARTED:
            assert "Codex" in progress.text or "验收" in progress.text


# ---------------------------------------------------------------------------
# Claude streaming error handling (Issue 4)
# ---------------------------------------------------------------------------


class ErrorThenTextClaudeStreaming:
    """Fake Claude that emits an error delta before text deltas."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def send_streaming(self, request):
        from wlcodex.agent_backend import AgentStreamEvent
        self.prompts.append(request.prompt)
        yield AgentStreamEvent(delta="binary missing", event_type="error")
        yield AgentStreamEvent(delta="some text after error", event_type="text")


class PureErrorClaudeStreaming:
    """Fake Claude that only emits error deltas."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def send_streaming(self, request):
        from wlcodex.agent_backend import AgentStreamEvent
        self.prompts.append(request.prompt)
        yield AgentStreamEvent(delta="Claude binary not found", event_type="error")


@pytest.mark.asyncio
async def test_run_streaming_fails_on_claude_error_delta() -> None:
    """Claude stream errors must stop chief-engineer before Codex verification."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = FakeCodexWithSendPrompt()
    claude = ErrorThenTextClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    events = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        events.append(progress)

    phases = [event.phase for event in events]
    complete_events = [
        event for event in events if event.phase == OrchestrationProgress.COMPLETE
    ]
    failed_events = [
        event for event in events if event.phase == OrchestrationProgress.FAILED
    ]

    assert failed_events
    assert complete_events
    assert complete_events[0].result_status == "failed"
    assert "binary missing" in failed_events[0].text
    assert OrchestrationProgress.VERIFY_STARTED not in phases
    assert len(codex.prompts) == 1  # analysis only; no verification after Claude error
